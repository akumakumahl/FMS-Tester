import json
import os
import ssl
import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import paho.mqtt.client as mqtt
import websocket


def _get_base_dir():
    """設定檔統一放在使用者家目錄下，不管是直接跑 .py、Windows 單一 exe、
    還是 macOS 打包成 .app（執行檔實際上藏在 .app/Contents/MacOS/ 深處），
    路徑都一致、好找，使用者不需要去翻 App 包內部。"""
    return os.path.expanduser("~")


CONFIG_PATH = os.path.join(_get_base_dir(), "fms_tester_config.json")


def load_external_config():
    """從程式同層的 fms_tester_config.json 讀取 broker 連線資訊（host/port/帳密/ws url）。
    這樣帳密就不會寫死在原始碼或編譯後的 exe 裡，每個人可以用自己的設定檔，
    也方便之後帳密異動時不用重新編譯程式。找不到檔案或格式錯誤時回傳空字典，
    UI 會照舊使用空白欄位，由使用者手動輸入即可，行為跟以前一樣。"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_EXTERNAL_CONFIG = load_external_config()

# --- 預設組態設定（優先從 fms_tester_config.json 載入，找不到才用這裡的預設值） ---
DEFAULT_MQTT_HOST = _EXTERNAL_CONFIG.get("mqtt_host", "emqx-stage.hino-itraq.com.tw")
DEFAULT_MQTT_PORT = _EXTERNAL_CONFIG.get("mqtt_port", 8883)
DEFAULT_MQTT_USER = _EXTERNAL_CONFIG.get("mqtt_user", "")
DEFAULT_MQTT_PASS = _EXTERNAL_CONFIG.get("mqtt_pass", "")

DEFAULT_WS_URL = _EXTERNAL_CONFIG.get("ws_url", "ws://localhost:3000")
DEFAULT_DEVICE_ID = "111112222239999"
DEFAULT_SID = "01KY8WT171C5N4692VTXDMYPR7"
# 發送 Inform 用的 Topic 樣板，可用 {device_id} / {device_type} 動態帶入
# 2026-07-27 實測確認：真實 tbox 設備是用 v1/remote-start/status/{imei} 這個固定格式，
# device_id 放在最後一段，且沒有 device_type 分段
DEFAULT_TOPIC_TEMPLATE = _EXTERNAL_CONFIG.get("topic_template", "v1/remote-start/status/{device_id}")

class FmsTesterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FMS 車機模擬器 & 狀態視覺化工具 (Python Native)")
        self.geometry("1100x700")
        self.minsize(950, 600)

        # 核心變數
        self.mqtt_client = None
        self.mqtt_connected = False
        self.mqtt_auth_fail_count = 0
        self.last_connect_ts = None
        self.ws_app = None
        self.ws_connected = False
        self.subscribed_device_id = None
        self.last_device_ts = None

        self._init_ui()
        if _EXTERNAL_CONFIG:
            self.log("CONFIG", f"已從設定檔載入連線資訊: {CONFIG_PATH}")
        else:
            self.log("CONFIG", f"找不到設定檔 ({CONFIG_PATH})，請手動輸入連線資訊，或建立該檔案（格式參考 fms_tester_config.json.example）")
        self._load_preset("TBOX_HEALTHY")
        self._tick_freshness()

    def _init_ui(self):
        style = ttk.Style()
        style.theme_use("clam")

        # 主容器：左 7 右 5
        main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left_frame = ttk.Frame(main_pane)
        right_frame = ttk.Frame(main_pane)
        main_pane.add(left_frame, weight=3)
        main_pane.add(right_frame, weight=2)

        # ==================== 左側：MQTT 控制區 ====================
        conn_group = ttk.LabelFrame(left_frame, text=" 1. 連線設定 (MQTTS & WebSocket) ", padding=10)
        conn_group.pack(fill=tk.X, pady=(0, 10))

        grid_opts = {'padx': 5, 'pady': 3, 'sticky': tk.W}
        ttk.Label(conn_group, text="MQTT Host:").grid(row=0, column=0, **grid_opts)
        self.entry_mqtt_host = ttk.Entry(conn_group, width=28)
        self.entry_mqtt_host.insert(0, DEFAULT_MQTT_HOST)
        self.entry_mqtt_host.grid(row=0, column=1, **grid_opts)

        ttk.Label(conn_group, text="Port:").grid(row=0, column=2, **grid_opts)
        self.entry_mqtt_port = ttk.Entry(conn_group, width=8)
        self.entry_mqtt_port.insert(0, str(DEFAULT_MQTT_PORT))
        self.entry_mqtt_port.grid(row=0, column=3, **grid_opts)

        ttk.Label(conn_group, text="User:").grid(row=1, column=0, **grid_opts)
        self.entry_mqtt_user = ttk.Entry(conn_group, width=28)
        self.entry_mqtt_user.insert(0, DEFAULT_MQTT_USER)
        self.entry_mqtt_user.grid(row=1, column=1, **grid_opts)

        ttk.Label(conn_group, text="Password:").grid(row=1, column=2, **grid_opts)
        self.entry_mqtt_pass = ttk.Entry(conn_group, width=12, show="*")
        self.entry_mqtt_pass.insert(0, DEFAULT_MQTT_PASS)
        self.entry_mqtt_pass.grid(row=1, column=3, **grid_opts)

        ttk.Label(conn_group, text="WS Gateway:").grid(row=2, column=0, **grid_opts)
        self.entry_ws_url = ttk.Entry(conn_group, width=28)
        self.entry_ws_url.insert(0, DEFAULT_WS_URL)
        self.entry_ws_url.grid(row=2, column=1, **grid_opts)

        ttk.Label(conn_group, text="Device ID:").grid(row=2, column=2, **grid_opts)
        self.entry_device_id = ttk.Entry(conn_group, width=15)
        self.entry_device_id.insert(0, DEFAULT_DEVICE_ID)
        self.entry_device_id.grid(row=2, column=3, **grid_opts)

        ttk.Label(conn_group, text="發送 Topic 樣板:").grid(row=3, column=0, **grid_opts)
        self.entry_topic_template = ttk.Entry(conn_group, width=28)
        self.entry_topic_template.insert(0, DEFAULT_TOPIC_TEMPLATE)
        self.entry_topic_template.grid(row=3, column=1, columnspan=3, sticky=tk.EW, padx=5, pady=3)

        self.btn_connect_mqtt = ttk.Button(conn_group, text="🔌 連線 MQTT", command=self._connect_mqtt_thread)
        self.btn_connect_mqtt.grid(row=4, column=0, columnspan=2, pady=(8, 0), sticky=tk.EW)

        self.btn_connect_ws = ttk.Button(conn_group, text="🔌 連線 WebSocket", command=self._connect_ws_thread)
        self.btn_connect_ws.grid(row=4, column=2, pady=(8, 0), sticky=tk.EW)

        self.btn_switch_device = ttk.Button(conn_group, text="🔄 訂閱此 Device ID", command=self._switch_device)
        self.btn_switch_device.grid(row=4, column=3, pady=(8, 0), sticky=tk.EW)

        ttk.Label(
            conn_group,
            text="※ 兩個連線各自獨立，只連其中一個也能正常運作（例如沒有 MQTT 帳號時只連 WebSocket 觀看車況）",
            font=("Helvetica", 8),
            foreground="#718096",
        ).grid(row=5, column=0, columnspan=3, sticky=tk.W, pady=(4, 0))

        self.btn_reset_all = ttk.Button(conn_group, text="🔄 全部重置", command=self._reset_all)
        self.btn_reset_all.grid(row=5, column=3, pady=(4, 0), sticky=tk.EW)

        # 2. 快捷按鈕區
        preset_group = ttk.LabelFrame(left_frame, text=" 2. 快捷模擬情境 ", padding=10)
        preset_group.pack(fill=tk.X, pady=(0, 10))

        btn_opts = {'padx': 3, 'pady': 3, 'sticky': tk.EW}
        ttk.Button(preset_group, text="1. SRDT 門檢通過", command=lambda: self._load_preset("PRECHECK_OK")).grid(row=0, column=0, **btn_opts)
        ttk.Button(preset_group, text="2. SRDT 通電亮燈", command=lambda: self._load_preset("ACC_IGN_ON")).grid(row=0, column=1, **btn_opts)
        ttk.Button(preset_group, text="3. TBOX 健康車況", command=lambda: self._load_preset("TBOX_HEALTHY")).grid(row=0, column=2, **btn_opts)
        ttk.Button(preset_group, text="4. TBOX 啟動 (RPM 800)", command=lambda: self._load_preset("ENGINE_RUNNING")).grid(row=1, column=0, **btn_opts)
        ttk.Button(preset_group, text="5. 異常: 低電壓 11V", command=lambda: self._load_preset("LOW_BATTERY")).grid(row=1, column=1, **btn_opts)
        ttk.Button(preset_group, text="6. 異常: 運轉中門開", command=lambda: self._load_preset("DOOR_OPEN")).grid(row=1, column=2, **btn_opts)

        for i in range(3):
            preset_group.columnconfigure(i, weight=1)

        # 3. Payload 編輯與發送
        payload_group = ttk.LabelFrame(left_frame, text=" 3. MQTT Inform Payload 編輯器 ", padding=10)
        payload_group.pack(fill=tk.BOTH, expand=True)

        self.txt_payload = scrolledtext.ScrolledText(payload_group, height=12, font=("Consolas", 10))
        self.txt_payload.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        self.btn_send = ttk.Button(payload_group, text="🚀 發送 MQTT Inform 封包", command=self._publish_mqtt)
        self.btn_send.pack(fill=tk.X)

        # ==================== 右側：WebSocket 視覺化監視器 ====================
        status_group = ttk.LabelFrame(right_frame, text=" 實時車輛狀態 (WebSocket) ", padding=10)
        status_group.pack(fill=tk.X, pady=(0, 10))

        # 使用原生 tk.Label 支援顏色改變
        self.lbl_ui_state = tk.Label(status_group, text="等待數據中...", font=("Helvetica", 14, "bold"), bg="#4A5568", fg="white", pady=10)
        self.lbl_ui_state.pack(fill=tk.X, pady=(0, 5))

        self.lbl_freshness = tk.Label(status_group, text="尚未收到資料", font=("Helvetica", 8), fg="#718096", anchor="w")
        self.lbl_freshness.pack(fill=tk.X, pady=(0, 8))

        cards_frame = ttk.Frame(status_group)
        cards_frame.pack(fill=tk.X)

        self.lbl_door_lock = self._create_card(cards_frame, "門鎖狀態", "未知", 0, 0)
        self.lbl_door_open = self._create_card(cards_frame, "車門狀況", "未知", 0, 1)
        self.lbl_engine = self._create_card(cards_frame, "引擎狀態", "OFF", 0, 2)
        self.lbl_battery = self._create_card(cards_frame, "電瓶電壓", "0 V", 0, 3)
        self.lbl_mileage = self._create_card(cards_frame, "里程", "0 km", 1, 0)
        self.lbl_fuel_level = self._create_card(cards_frame, "油量", "0 %", 1, 1)
        self.lbl_can_restart = self._create_card(cards_frame, "可否重啟", "未知", 1, 2)
        self.lbl_retry_count = self._create_card(cards_frame, "點火嘗試次數", "0", 1, 3)

        ttk.Label(
            status_group,
            text="※ 可否重啟／點火嘗試次數僅在有進行中 Session 時才會變化，Idle 模式下固定不變",
            font=("Helvetica", 8),
            foreground="#718096",
        ).pack(fill=tk.X, pady=(4, 0))

        self.lbl_dtc = tk.Label(status_group, text="DTC 錯誤碼: 無", font=("Helvetica", 9), fg="#4A5568", anchor="w")
        self.lbl_dtc.pack(fill=tk.X, pady=(5, 0))

        self.lbl_failure_reason = tk.Label(status_group, text="", font=("Helvetica", 9), fg="#E53E3E", anchor="w")
        self.lbl_failure_reason.pack(fill=tk.X, pady=(2, 0))

        conn_status_frame = ttk.Frame(right_frame)
        conn_status_frame.pack(fill=tk.X, pady=(0, 10))

        self.lbl_status_mqtt = tk.Label(conn_status_frame, text="MQTT: 未連線", bg="#E53E3E", fg="white", font=("Helvetica", 9, "bold"), width=15)
        self.lbl_status_mqtt.pack(side=tk.LEFT, padx=(0, 5))

        self.lbl_status_ws = tk.Label(conn_status_frame, text="WebSocket: 未連線", bg="#E53E3E", fg="white", font=("Helvetica", 9, "bold"), width=18)
        self.lbl_status_ws.pack(side=tk.LEFT)

        log_group = ttk.LabelFrame(right_frame, text=" 系統日誌 (Logs) ", padding=10)
        log_group.pack(fill=tk.BOTH, expand=True)

        self.txt_log = scrolledtext.ScrolledText(log_group, height=10, font=("Consolas", 9), state='disabled')
        self.txt_log.pack(fill=tk.BOTH, expand=True)

    def _reset_all(self):
        """斷開 MQTT 與 WebSocket、解鎖所有連線設定欄位、清空儀表板，回到剛開啟程式的狀態。
        實際的斷線動作（尤其 loop_stop 需要等待背景執行緒結束）丟到另一個執行緒處理，
        避免在按鈕點擊當下卡住主執行緒，跟 callback 互相等待造成死結。"""
        mqtt_client = self.mqtt_client
        ws_app = self.ws_app

        def _do_disconnect():
            if mqtt_client:
                try:
                    mqtt_client.disconnect()
                    mqtt_client.loop_stop()
                except Exception:
                    pass
            if ws_app:
                try:
                    ws_app.close()
                except Exception:
                    pass

        threading.Thread(target=_do_disconnect, daemon=True).start()

        self.mqtt_client = None
        self.mqtt_connected = False
        self.mqtt_auth_fail_count = 0
        self.last_connect_ts = None

        self.ws_app = None
        self.ws_connected = False
        self.subscribed_device_id = None
        self.last_device_ts = None

        # 解鎖連線設定欄位（Device ID / Topic 樣板本來就不鎖，維持可編輯）
        for entry in (self.entry_mqtt_host, self.entry_mqtt_port, self.entry_mqtt_user,
                      self.entry_mqtt_pass, self.entry_ws_url):
            entry.config(state="normal")

        # 按鈕恢復初始狀態
        self.btn_connect_mqtt.config(text="🔌 連線 MQTT", state="normal")
        self.btn_connect_ws.config(text="🔌 連線 WebSocket", state="normal")

        # 連線指示燈恢復
        self.lbl_status_mqtt.config(text="MQTT: 未連線", bg="#E53E3E")
        self.lbl_status_ws.config(text="WebSocket: 未連線", bg="#E53E3E")

        # 儀表板恢復預設值
        self.lbl_ui_state.config(text="等待數據中...", bg="#4A5568")
        self.lbl_door_lock.config(text="未知")
        self.lbl_door_open.config(text="未知", fg="black")
        self.lbl_battery.config(text="0 V")
        self.lbl_engine.config(text="OFF")
        self.lbl_mileage.config(text="0 km")
        self.lbl_fuel_level.config(text="0 %")
        self.lbl_can_restart.config(text="未知", fg="black")
        self.lbl_retry_count.config(text="0")
        self.lbl_dtc.config(text="DTC 錯誤碼: 無", fg="#4A5568")
        self.lbl_failure_reason.config(text="")
        self.lbl_freshness.config(text="尚未收到資料", fg="#718096")

        self.log("SYSTEM", "已重置所有連線與欄位，可重新輸入設定並連線")

    def _tick_freshness(self):
        """每秒更新一次「最後更新：X 秒前」，就算沒有新廣播進來也會持續跳動，
        方便直接看出資料是不是已經很久沒更新（斷線/卡住）。"""
        if self.last_device_ts is not None:
            elapsed = time.time() - self.last_device_ts
            if elapsed < 10:
                color = "#38A169"
            elif elapsed < 30:
                color = "#DD6B20"
            else:
                color = "#E53E3E"
            suffix = "" if elapsed < 30 else "（資料可能已過期或已斷線）"
            self.lbl_freshness.config(text=f"最後更新: {elapsed:.0f} 秒前{suffix}", fg=color)
        self.after(1000, self._tick_freshness)

    def _create_card(self, parent, title, default_val, row, col):
        frame = ttk.Frame(parent, relief="groove", padding=8)
        frame.grid(row=row, column=col, padx=3, pady=3, sticky="nsew")
        parent.columnconfigure(col, weight=1)

        ttk.Label(frame, text=title, font=("Helvetica", 8)).pack(anchor="w")
        # 改用 tk.Label 以確保安全的屬性相容
        lbl_val = tk.Label(frame, text=default_val, font=("Helvetica", 11, "bold"), anchor="w")
        lbl_val.pack(anchor="w", pady=(2, 0))
        return lbl_val

    def log(self, category, msg):
        def _do():
            self.txt_log.config(state='normal')
            timestamp = time.strftime("%H:%M:%S")
            self.txt_log.insert(tk.END, f"[{timestamp}] [{category}] {msg}\n")
            self.txt_log.see(tk.END)
            self.txt_log.config(state='disabled')
        self.after(0, _do)

    def _set_widget(self, widget, **kwargs):
        """從背景執行緒（MQTT/WS callback）安全地更新 Tkinter 元件：
        透過 after() 排到主執行緒執行，避免多執行緒同時操作 Tk 造成死結/凍結。"""
        self.after(0, lambda: widget.config(**kwargs))

    def _load_preset(self, key):
        device_id = self.entry_device_id.get().strip() or DEFAULT_DEVICE_ID
        sid = DEFAULT_SID
        now_ts = int(time.time())

        presets = {
            "PRECHECK_OK": {
                "info": "0x00000001", "time": now_ts, "sid": sid, "srdt_imei": device_id,
                "status": {"lock": 1, "door_open": 0, "key": 0, "brake": 0, "start_time": now_ts}
            },
            "ACC_IGN_ON": {
                "info": "0x00000004", "time": now_ts, "sid": sid, "srdt_imei": device_id,
                "status": {"lock": 1, "door_open": 0, "key": 0, "brake": 0, "start_time": now_ts}
            },
            "TBOX_HEALTHY": {
                "info": "PUB_CAR_STATUS_INFO", "time": now_ts, "sid": sid, "tbox_imei": device_id,
                "status": {"acc_status": 1, "handbrake": 1, "battery": 23500, "start_time": now_ts},
                "can": {"totalMileage": 12500, "fuelLevel": 75, "rpm": 0, "dtc": []}
            },
            "ENGINE_RUNNING": {
                "info": "PUB_CAR_STATUS_INFO", "time": now_ts, "sid": sid, "tbox_imei": device_id,
                "status": {"acc_status": 1, "handbrake": 1, "battery": 23800, "start_time": now_ts},
                "can": {"totalMileage": 12500, "fuelLevel": 75, "rpm": 800, "dtc": []}
            },
            "LOW_BATTERY": {
                "info": "PUB_CAR_STATUS_INFO", "time": now_ts, "sid": sid, "tbox_imei": device_id,
                "status": {"acc_status": 1, "handbrake": 1, "battery": 11000, "start_time": now_ts},
                "can": {"totalMileage": 12500, "fuelLevel": 75, "rpm": 0, "dtc": []}
            },
            "DOOR_OPEN": {
                "info": "0x00040000", "time": now_ts, "sid": sid, "srdt_imei": device_id,
                "status": {"lock": 0, "door_open": 1, "key": 0, "brake": 0}
            }
        }

        if key in presets:
            self.txt_payload.delete("1.0", tk.END)
            self.txt_payload.insert(tk.END, json.dumps(presets[key], indent=2, ensure_ascii=False))

    def _connect_mqtt_thread(self):
        self.btn_connect_mqtt.config(text="🔌 連線中...", state="disabled")
        threading.Thread(target=self._connect_mqtt, daemon=True).start()

    def _connect_ws_thread(self):
        self.btn_connect_ws.config(text="🔌 連線中...", state="disabled")
        threading.Thread(target=self._connect_ws, daemon=True).start()

    def _connect_mqtt(self):
        host = self.entry_mqtt_host.get().strip()
        port = int(self.entry_mqtt_port.get().strip())
        user = self.entry_mqtt_user.get().strip()
        password = self.entry_mqtt_pass.get().strip()

        self.log("MQTT", f"嘗試連線至 MQTTS {host}:{port} ...")

        try:
            # 相容 paho-mqtt v2 的 Callback API Version 寫法
            try:
                self.mqtt_client = mqtt.Client(
                    mqtt.CallbackAPIVersion.VERSION1,
                    client_id=f"python_tester_{int(time.time())}"
                )
            except AttributeError:
                self.mqtt_client = mqtt.Client(client_id=f"python_tester_{int(time.time())}")

            if user:
                self.mqtt_client.username_pw_set(user, password)

            self.mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
            self.mqtt_client.tls_insecure_set(True)

            self.mqtt_client.on_connect = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self.mqtt_client.on_publish = self._on_mqtt_publish

            self.mqtt_client.connect(host, port, keepalive=60)
            self.mqtt_client.loop_start()
        except Exception as e:
            self.log("ERROR", f"MQTT 初始化失敗: {e}")
            self.btn_connect_mqtt.config(text="🔌 連線 MQTT", state="normal")

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.mqtt_connected = True
            self.mqtt_auth_fail_count = 0
            self.last_connect_ts = time.time()
            self._set_widget(self.lbl_status_mqtt, text="MQTT: 已連線", bg="#38A169")
            self._set_widget(self.btn_connect_mqtt, text="✅ MQTT 已連線", state="disabled")
            for entry in (self.entry_mqtt_host, self.entry_mqtt_port, self.entry_mqtt_user, self.entry_mqtt_pass):
                self._set_widget(entry, state="disabled")
            self.log("MQTT", "MQTTS (8883) 連線成功！")
        else:
            err_msg = {
                1: "不對應的通訊協定版本",
                2: "無效的 Client ID",
                3: "MQTT 服務不可用",
                4: "錯誤的 User/Password 認證失敗",
                5: "無權限 (Not Authorized / ACL 阻擋)"
            }.get(rc, f"未知代碼 rc={rc}")
            self.log("ERROR", f"MQTT 連線拒絕: {err_msg}")
            self._set_widget(self.btn_connect_mqtt, text="🔌 連線 MQTT", state="normal")

            if rc in (4, 5):
                self.mqtt_auth_fail_count += 1
                # 如果先前曾經連線成功過，卻在重連時開始出現認證錯誤，
                # 很可能是密碼欄位放的是有時效性的 token/SAS，已過期，而非單純打錯密碼
                if self.last_connect_ts:
                    self.log(
                        "WARN",
                        "本次認證失敗發生在先前曾連線成功之後，"
                        "若密碼欄位使用的是有時效性的 token/SAS，很可能是已過期，而非帳密打錯。"
                    )
                if self.mqtt_auth_fail_count >= 2:
                    self.log(
                        "ERROR",
                        f"連續 {self.mqtt_auth_fail_count} 次認證失敗，已停止自動重連。"
                        "請確認密碼/token 是否過期，更新後請重新按「連線 MQTT」。"
                    )
                    self.mqtt_connected = False
                    try:
                        client.disconnect()  # 停止 paho 內建的自動重連迴圈
                    except Exception:
                        pass

    def _on_mqtt_disconnect(self, client, userdata, rc):
        self.mqtt_connected = False
        self._set_widget(self.lbl_status_mqtt, text="MQTT: 已中斷", bg="#E53E3E")
        self._set_widget(self.btn_connect_mqtt, text="🔌 連線 MQTT", state="normal")
        for entry in (self.entry_mqtt_host, self.entry_mqtt_port, self.entry_mqtt_user, self.entry_mqtt_pass):
            self._set_widget(entry, state="normal")
        if self.last_connect_ts:
            duration = time.time() - self.last_connect_ts
            self.log("MQTT", f"MQTT 連線中斷 (rc={rc})，本次連線維持了約 {duration:.0f} 秒")
        else:
            self.log("MQTT", f"MQTT 連線中斷 (rc={rc})")

    def _on_mqtt_publish(self, client, userdata, mid):
        self.log("MQTT_PUB", f"Broker 已回覆 PUBACK 確認收到封包 (mid={mid})")

    def _publish_mqtt(self):
        if not self.mqtt_connected or not self.mqtt_client:
            messagebox.showwarning("警告", "MQTT 未連線，請先點擊「連線 MQTT」！")
            return

        raw_json = self.txt_payload.get("1.0", tk.END).strip()
        try:
            payload = json.loads(raw_json)
            payload["time"] = int(time.time()) # 自動校正最新時間戳

            final_json = json.dumps(payload, indent=2, ensure_ascii=False)
            self.txt_payload.delete("1.0", tk.END)
            self.txt_payload.insert(tk.END, final_json)

            device_id = self.entry_device_id.get().strip() or DEFAULT_DEVICE_ID
            if "srdt_imei" in payload:
                device_type = "srdt"
            elif "tbox_imei" in payload:
                device_type = "tbox"
            else:
                device_type = "unknown"

            topic_template = self.entry_topic_template.get().strip() or DEFAULT_TOPIC_TEMPLATE
            try:
                topic = topic_template.format(device_id=device_id, device_type=device_type)
            except (KeyError, IndexError) as e:
                messagebox.showerror("Topic 樣板錯誤", f"Topic 樣板格式不正確: {e}")
                return

            result = self.mqtt_client.publish(topic, final_json, qos=1)

            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                self.log("ERROR", f"發送失敗（本地佇列階段就出錯），代碼: {result.rc}")
                return

            self.log("MQTT_PUB", f"已送出至 {topic}，等待 broker 確認 (PUBACK)...")

            def wait_and_check():
                try:
                    result.wait_for_publish(timeout=5)
                    if not result.is_published():
                        self.log(
                            "WARN",
                            f"送出 5 秒後仍未收到 broker 的 PUBACK 確認 (topic={topic})，"
                            "很可能是 ACL 沒有這個 topic 的發布權限，或 broker 端把封包丟棄了，"
                            "請確認這個帳號對此 topic/device id 是否有發布權限。"
                        )
                except Exception as e:
                    self.log("ERROR", f"等待 PUBACK 時發生例外: {e}")

            threading.Thread(target=wait_and_check, daemon=True).start()
        except json.JSONDecodeError as e:
            messagebox.showerror("格式錯誤", f"JSON 語法無效: {e}")

    def _extract_incoming_device_id(self, v_data):
        """嘗試從推播資料中找出裝置識別碼，支援常見欄位命名與 1~2 層巢狀結構。
        找不到時回傳 None（代表這筆資料無法辨識屬於哪台設備，需要另外確認 gateway 實際欄位）。"""
        candidate_keys = ("device_id", "deviceid", "srdt_imei", "tbox_imei", "imei", "vin", "sid")

        def search(obj, depth=0):
            if depth > 2 or not isinstance(obj, dict):
                return None
            for k, v in obj.items():
                if isinstance(k, str) and k.lower() in candidate_keys and isinstance(v, (str, int)):
                    return str(v)
            for v in obj.values():
                if isinstance(v, dict):
                    found = search(v, depth + 1)
                    if found:
                        return found
            return None

        return search(v_data)

    def _format_broadcast_summary(self, v_data):
        """把廣播資料整理成一行看得懂的摘要（實際數值），取代印出一堆欄位名稱。"""
        ui_text = v_data.get("ui_state_text", "未知")
        color = v_data.get("status_color", "GRAY")
        details = v_data.get("details", {}) or {}
        lock = details.get("door_lock", "?")
        door = "開啟" if details.get("door_open") else "關閉"
        battery = details.get("battery_voltage", "?")
        engine = details.get("engine_status", "?")
        can_restart = v_data.get("can_restart")
        dtc_count = len(details.get("dtc_errors") or [])

        summary = (
            f"狀態={ui_text}({color}) | 門鎖={lock} | 車門={door} | 電瓶={battery}V | 引擎={engine} | "
            f"可重啟={can_restart} | DTC數={dtc_count}"
        )
        reason = v_data.get("failure_reason")
        if reason:
            summary += f" | 失敗原因={reason}"
        return summary

    def _connect_ws(self):
        ws_url = self.entry_ws_url.get().strip()
        device_id = self.entry_device_id.get().strip() or DEFAULT_DEVICE_ID

        self.log("WS", f"嘗試連線至 WebSocket {ws_url} ...")

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if "data" in data:
                    v_data = data["data"]
                    incoming_id = self._extract_incoming_device_id(v_data)

                    self.log(
                        "WS_RECV",
                        f"裝置={incoming_id or '無法辨識'} | 訂閱中={self.subscribed_device_id} | "
                        f"{self._format_broadcast_summary(v_data)}"
                    )

                    if incoming_id and self.subscribed_device_id and incoming_id != self.subscribed_device_id:
                        # 收到的資料明確標示是別台設備，過濾掉不更新畫面
                        return

                    if not incoming_id:
                        # 抓不到任何識別欄位：無法確定這筆資料是不是目前訂閱的設備。
                        # 這種情況下 gateway 端很可能沒有依訂閱做過濾（或是欄位命名跟預期的不同），
                        # 畫面仍會更新，但數字不保證屬於目前選的 Device ID。
                        self.log("WARN", "此筆廣播找不到可辨識裝置的欄位，無法確認是否為目前訂閱的設備")

                    # 使用 after 確保安全在 GUI 主執行緒更新元件
                    self.after(0, self._update_vehicle_ui, v_data)
            except Exception as e:
                self.log("ERROR", f"WS 解析錯誤: {e}")

        def on_open(ws):
            self.ws_connected = True
            self.subscribed_device_id = device_id
            self._set_widget(self.lbl_status_ws, text="WebSocket: 已連線", bg="#38A169")
            self._set_widget(self.btn_connect_ws, text="✅ WebSocket 已連線", state="disabled")
            self._set_widget(self.entry_ws_url, state="disabled")
            self.log("WS", "WebSocket 連線成功！發送訂閱...")
            sub_payload = json.dumps({"action": "SUBSCRIBE_VEHICLES", "device_ids": [device_id]})
            ws.send(sub_payload)

        def on_close(ws, close_status_code, close_msg):
            self.ws_connected = False
            self.subscribed_device_id = None
            self._set_widget(self.lbl_status_ws, text="WebSocket: 已連線關閉", bg="#E53E3E")
            self._set_widget(self.btn_connect_ws, text="🔌 連線 WebSocket", state="normal")
            self._set_widget(self.entry_ws_url, state="normal")

        def on_error(ws, error):
            self.log("ERROR", f"WS 錯誤: {error}")
            self._set_widget(self.btn_connect_ws, text="🔌 連線 WebSocket", state="normal")

        self.ws_app = websocket.WebSocketApp(
            ws_url,
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        # 加上 sslopt 繞過 SSL 憑證驗證
        self.ws_app.run_forever(
            sslopt={
                "cert_reqs": ssl.CERT_NONE,
                "check_hostname": False
            }
        )
        #self.ws_app.run_forever()

    def _switch_device(self):
        """在 WebSocket 已連線的狀態下，切換訂閱到輸入框目前的 Device ID，不需重新連線。"""
        new_device = self.entry_device_id.get().strip()
        if not new_device:
            messagebox.showwarning("警告", "請輸入 Device ID")
            return
        if not self.ws_connected or not self.ws_app:
            messagebox.showwarning("警告", "WebSocket 尚未連線，請先點擊「連線 WebSocket」")
            return

        old_device = self.subscribed_device_id
        try:
            if old_device and old_device != new_device:
                self.ws_app.send(json.dumps({"action": "UNSUBSCRIBE_VEHICLES", "device_ids": [old_device]}))
                self.log("WS", f"取消訂閱舊設備: {old_device}")

            self.ws_app.send(json.dumps({"action": "SUBSCRIBE_VEHICLES", "device_ids": [new_device]}))
            self.subscribed_device_id = new_device
            self.log("WS", f"已訂閱新設備: {new_device}")

            # 切換設備時重置右側顯示，避免殘留上一台車的舊資料造成誤判
            self.lbl_ui_state.config(text="等待數據中...", bg="#4A5568")
            self.lbl_door_lock.config(text="未知")
            self.lbl_door_open.config(text="未知", fg="black")
            self.lbl_battery.config(text="0 V")
            self.lbl_engine.config(text="OFF")
            self.lbl_mileage.config(text="0 km")
            self.lbl_fuel_level.config(text="0 %")
            self.lbl_can_restart.config(text="未知", fg="black")
            self.lbl_retry_count.config(text="0")
            self.lbl_dtc.config(text="DTC 錯誤碼: 無")
            self.lbl_failure_reason.config(text="")
            self.last_device_ts = None
            self.lbl_freshness.config(text="尚未收到資料", fg="#718096")
        except Exception as e:
            self.log("ERROR", f"切換設備訂閱失敗: {e}")

    def _update_vehicle_ui(self, v_data):
        ui_text = v_data.get("ui_state_text", "未知狀態")
        color_code = v_data.get("status_color", "GRAY")

        bg_map = {"GREEN": "#2F855A", "BLUE": "#2B6CB0", "RED": "#C53030", "GRAY": "#4A5568", "ORANGE": "#DD6B20"}
        self.lbl_ui_state.config(text=ui_text, bg=bg_map.get(color_code, "#4A5568"))

        details = v_data.get("details", {}) or {}
        lock_str = "已上鎖 (LOCKED)" if details.get("door_lock") == "LOCKED" else "解鎖中 (UNLOCKED)"
        open_str = "開啟中 (OPEN)" if details.get("door_open") else "關閉 (CLOSED)"
        bat_str = f"{details.get('battery_voltage', 0)} V"
        eng_str = f"{details.get('engine_status', 'OFF')}"
        mileage_str = f"{details.get('mileage', 0)} km"
        fuel_str = f"{details.get('fuel_level', 0)} %"

        # 這裡改用 tk.Label 後全屬性都可安全呼叫
        self.lbl_door_lock.config(text=lock_str)
        self.lbl_door_open.config(text=open_str, fg="#E53E3E" if details.get("door_open") else "black")
        self.lbl_battery.config(text=bat_str)
        self.lbl_engine.config(text=eng_str)
        self.lbl_mileage.config(text=mileage_str)
        self.lbl_fuel_level.config(text=fuel_str)

        can_restart = v_data.get("can_restart")
        if can_restart is None:
            self.lbl_can_restart.config(text="未知", fg="black")
        else:
            self.lbl_can_restart.config(
                text="可重啟" if can_restart else "不可重啟",
                fg="#2F855A" if can_restart else "#E53E3E",
            )
        self.lbl_retry_count.config(text=str(v_data.get("retry_countdown", 0)))

        dtc_errors = details.get("dtc_errors") or []
        if dtc_errors:
            self.lbl_dtc.config(text=f"DTC 錯誤碼: {', '.join(str(x) for x in dtc_errors)}", fg="#E53E3E")
        else:
            self.lbl_dtc.config(text="DTC 錯誤碼: 無", fg="#4A5568")

        reason = v_data.get("failure_reason")
        self.lbl_failure_reason.config(text=f"失敗原因: {reason}" if reason else "")

        # 記錄裝置回報的時間戳，用來計算「最後更新於幾秒前」
        ts = v_data.get("timestamp")
        if isinstance(ts, (int, float)):
            self.last_device_ts = ts

if __name__ == "__main__":
    app = FmsTesterApp()
    app.mainloop()