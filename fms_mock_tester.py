import json
import os
import ssl
import sys
import time
import threading
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import paho.mqtt.client as mqtt
import websocket


def _get_base_dir():
    """回傳要去找設定檔的資料夾。

    - 直接執行 .py：跟 .py 檔案同一層，方便開發時使用。
    - 封裝成 Windows 單一 exe：sys.executable 就是使用者看到的那個 .exe，同一層即可。
    - 封裝成 macOS .app：sys.executable 實際指向 .app/Contents/MacOS/<n>，
      要往上跳三層（MacOS -> Contents -> .app 本身 -> .app 所在的資料夾），
      這樣設定檔才會跟 .app 放在同一層，使用者直接看得到、放得到，
      不用鑽進 App 包內部。
    """
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        if sys.platform == "darwin" and "Contents/MacOS" in exe_dir:
            return os.path.abspath(os.path.join(exe_dir, "../../.."))
        return exe_dir
    return os.path.dirname(os.path.abspath(__file__))


_BASE_DIR = _get_base_dir()
CONFIG_PATH = os.path.join(_BASE_DIR, "fms_tester_config.json")


def load_external_config():
    """從程式（或 exe/app）同層的 fms_tester_config.json 讀取 broker 連線資訊。
    找不到檔案或格式錯誤時回傳空字典，UI 會照舊使用空白欄位，由使用者手動輸入。"""
    if not os.path.exists(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_EXTERNAL_CONFIG = load_external_config()

# --- 預設組態設定（優先從 fms_tester_config.json 載入，找不到才用這裡的預設值） ---
DEFAULT_MQTT_HOST    = _EXTERNAL_CONFIG.get("mqtt_host", "emqx-stage.hino-itraq.com.tw")
DEFAULT_MQTT_PORT    = _EXTERNAL_CONFIG.get("mqtt_port", 8883)
DEFAULT_MQTT_USER    = _EXTERNAL_CONFIG.get("mqtt_user", "")
DEFAULT_MQTT_PASS    = _EXTERNAL_CONFIG.get("mqtt_pass", "")
DEFAULT_WS_URL       = _EXTERNAL_CONFIG.get("ws_url", "wss://mqtt-gateway-stage.hino-itraq.com.tw/ws")
DEFAULT_API_BASE_URL = _EXTERNAL_CONFIG.get("api_base_url", "https://mqtt-gateway-stage.hino-itraq.com.tw")
DEFAULT_DEVICE_ID    = "111112222239999"
DEFAULT_TOPIC_TEMPLATE = _EXTERNAL_CONFIG.get("topic_template", "v1/remote-start/status/{device_id}")

# 自動播放每個步驟之間的延遲秒數
AUTO_PLAY_STEP_DELAY = 1.5


class FmsTesterApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FMS 車機模擬器 & 狀態視覺化工具_v0.7")
        self.geometry("1200x780")
        self.minsize(1000, 650)

        # MQTT / WS 連線狀態
        self.mqtt_client = None
        self.mqtt_connected = False
        self.mqtt_auth_fail_count = 0
        self.last_connect_ts = None
        self.ws_app = None
        self.ws_connected = False
        self.subscribed_device_id = None
        self.last_device_ts = None

        # Session 狀態（建立 Session 後填入）
        self.current_session_id = None   # sessionId（ULID）
        self.auto_play_running = False   # 自動播放是否進行中

        self._init_ui()

        if _EXTERNAL_CONFIG:
            self.log("CONFIG", f"已從設定檔載入連線資訊: {CONFIG_PATH}")
        else:
            self.log("CONFIG", f"找不到設定檔 ({CONFIG_PATH})，請手動輸入連線資訊")

        self._load_flow_preset("PRECHECK_OK")
        self._tick_freshness()

    # =========================================================================
    # UI 初始化
    # =========================================================================

    def _init_ui(self):
        style = ttk.Style()
        style.theme_use("clam")

        main_pane = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        left_frame = ttk.Frame(main_pane)
        right_frame = ttk.Frame(main_pane)
        main_pane.add(left_frame, weight=3)
        main_pane.add(right_frame, weight=2)

        self._build_left(left_frame)
        self._build_right(right_frame)

    def _build_left(self, parent):
        # ── 1. 連線設定 ──────────────────────────────────────────────────────
        conn_group = ttk.LabelFrame(parent, text=" 1. 連線設定 ", padding=10)
        conn_group.pack(fill=tk.X, pady=(0, 8))

        g = {'padx': 5, 'pady': 3, 'sticky': tk.W}

        ttk.Label(conn_group, text="MQTT Host:").grid(row=0, column=0, **g)
        self.entry_mqtt_host = ttk.Entry(conn_group, width=26)
        self.entry_mqtt_host.insert(0, DEFAULT_MQTT_HOST)
        self.entry_mqtt_host.grid(row=0, column=1, **g)

        ttk.Label(conn_group, text="Port:").grid(row=0, column=2, **g)
        self.entry_mqtt_port = ttk.Entry(conn_group, width=7)
        self.entry_mqtt_port.insert(0, str(DEFAULT_MQTT_PORT))
        self.entry_mqtt_port.grid(row=0, column=3, **g)

        ttk.Label(conn_group, text="User:").grid(row=1, column=0, **g)
        self.entry_mqtt_user = ttk.Entry(conn_group, width=26)
        self.entry_mqtt_user.insert(0, DEFAULT_MQTT_USER)
        self.entry_mqtt_user.grid(row=1, column=1, **g)

        ttk.Label(conn_group, text="Password:").grid(row=1, column=2, **g)
        self.entry_mqtt_pass = ttk.Entry(conn_group, width=12, show="*")
        self.entry_mqtt_pass.insert(0, DEFAULT_MQTT_PASS)
        self.entry_mqtt_pass.grid(row=1, column=3, **g)

        ttk.Label(conn_group, text="WS Gateway:").grid(row=2, column=0, **g)
        self.entry_ws_url = ttk.Entry(conn_group, width=26)
        self.entry_ws_url.insert(0, DEFAULT_WS_URL)
        self.entry_ws_url.grid(row=2, column=1, **g)

        ttk.Label(conn_group, text="Device ID:").grid(row=2, column=2, **g)
        self.entry_device_id = ttk.Entry(conn_group, width=16)
        self.entry_device_id.insert(0, DEFAULT_DEVICE_ID)
        self.entry_device_id.grid(row=2, column=3, **g)

        ttk.Label(conn_group, text="API Base URL:").grid(row=3, column=0, **g)
        self.entry_api_base_url = ttk.Entry(conn_group, width=26)
        self.entry_api_base_url.insert(0, DEFAULT_API_BASE_URL)
        self.entry_api_base_url.grid(row=3, column=1, **g)

        ttk.Label(conn_group, text="Topic 樣板:").grid(row=3, column=2, **g)
        self.entry_topic_template = ttk.Entry(conn_group, width=26)
        self.entry_topic_template.insert(0, DEFAULT_TOPIC_TEMPLATE)
        self.entry_topic_template.grid(row=3, column=3, sticky=tk.EW, padx=5, pady=3)

        # 按鈕列
        btn_row = ttk.Frame(conn_group)
        btn_row.grid(row=4, column=0, columnspan=4, sticky=tk.EW, pady=(8, 0))
        for i in range(4):
            btn_row.columnconfigure(i, weight=1)

        self.btn_connect_mqtt = ttk.Button(btn_row, text="🔌 連線 MQTT", command=self._connect_mqtt_thread)
        self.btn_connect_mqtt.grid(row=0, column=0, padx=3, sticky=tk.EW)

        self.btn_connect_ws = ttk.Button(btn_row, text="🔌 連線 WebSocket", command=self._connect_ws_thread)
        self.btn_connect_ws.grid(row=0, column=1, padx=3, sticky=tk.EW)

        self.btn_switch_device = ttk.Button(btn_row, text="🔄 訂閱此 Device ID", command=self._switch_device)
        self.btn_switch_device.grid(row=0, column=2, padx=3, sticky=tk.EW)

        self.btn_reset_all = ttk.Button(btn_row, text="♻️ 全部重置", command=self._reset_all)
        self.btn_reset_all.grid(row=0, column=3, padx=3, sticky=tk.EW)

        ttk.Label(
            conn_group,
            text="※ MQTT / WebSocket / API 三個連線各自獨立，可按需求只連其中一個",
            font=("Helvetica", 8), foreground="#718096",
        ).grid(row=5, column=0, columnspan=4, sticky=tk.W, pady=(4, 0))

        # ── 2. Session 管理 ───────────────────────────────────────────────────
        session_group = ttk.LabelFrame(parent, text=" 2. Session 管理（遠端啟動流程起點）", padding=10)
        session_group.pack(fill=tk.X, pady=(0, 8))

        session_top = ttk.Frame(session_group)
        session_top.pack(fill=tk.X)
        session_top.columnconfigure(1, weight=1)

        ttk.Label(session_top, text="目前 Session ID:").grid(row=0, column=0, padx=(0, 8), sticky=tk.W)
        self.entry_session_id = ttk.Entry(session_top)
        self.entry_session_id.grid(row=0, column=1, sticky=tk.EW)

        btn_session_row = ttk.Frame(session_group)
        btn_session_row.pack(fill=tk.X, pady=(8, 0))
        btn_session_row.columnconfigure(0, weight=1)
        btn_session_row.columnconfigure(1, weight=1)

        self.btn_create_session = ttk.Button(
            btn_session_row, text="🚗 建立 Session（模擬使用者按下啟動）",
            command=self._create_session_thread
        )
        self.btn_create_session.grid(row=0, column=0, padx=(0, 4), sticky=tk.EW)

        self.btn_clear_session = ttk.Button(
            btn_session_row, text="🗑 清除 Session ID",
            command=self._clear_session
        )
        self.btn_clear_session.grid(row=0, column=1, padx=(4, 0), sticky=tk.EW)

        ttk.Label(
            session_group,
            text="※ 建立 Session 後，sid 會自動帶入下方所有 Inform 封包；不建立 Session 也可手動輸入 sid 測試常態廣播",
            font=("Helvetica", 8), foreground="#718096",
        ).pack(fill=tk.X, pady=(6, 0))

        # ── 3. 流程步驟 Preset ───────────────────────────────────────────────
        flow_group = ttk.LabelFrame(parent, text=" 3. 遠端啟動流程步驟（對齊 V0.0.0.6）", padding=10)
        flow_group.pack(fill=tk.X, pady=(0, 8))

        # 說明文字
        ttk.Label(
            flow_group,
            text="步驟按鈕會將對應的 Inform 封包填入下方編輯器；填入後按「🚀 發送」手動發送，或直接按「▶ 完整成功流程」自動播放。",
            font=("Helvetica", 8), foreground="#718096", wraplength=600,
        ).grid(row=0, column=0, columnspan=4, sticky=tk.W, pady=(0, 6))

        b = {'padx': 3, 'pady': 3, 'sticky': tk.EW}

        # 正常流程
        ttk.Label(flow_group, text="正常流程:", font=("Helvetica", 8, "bold")).grid(row=1, column=0, sticky=tk.W, padx=(0, 3))
        ttk.Button(flow_group, text="A. 前置檢查通過 ✅",
                   command=lambda: self._load_flow_preset("PRECHECK_OK")).grid(row=1, column=1, **b)
        ttk.Button(flow_group, text="B. ACC 已 ON ✅",
                   command=lambda: self._load_flow_preset("ACC_IGN_ON")).grid(row=1, column=2, **b)
        ttk.Button(flow_group, text="Gate2. TBOX 開機回報",
                   command=lambda: self._load_flow_preset("TBOX_GATE2")).grid(row=1, column=3, **b)

        ttk.Button(flow_group, text="C. 引擎已點火 ✅",
                   command=lambda: self._load_flow_preset("ENG_START")).grid(row=2, column=1, **b)
        ttk.Button(flow_group, text="C+. TBOX 車況確認 (RPM 800)",
                   command=lambda: self._load_flow_preset("TBOX_ENGINE_CONFIRM")).grid(row=2, column=2, **b)
        ttk.Button(flow_group, text="D. 正常熄火 ✅",
                   command=lambda: self._load_flow_preset("SHUTDOWN_CMD")).grid(row=2, column=3, **b)

        # 失敗/異常情境
        ttk.Label(flow_group, text="異常情境:", font=("Helvetica", 8, "bold")).grid(row=3, column=0, sticky=tk.W, padx=(0, 3), pady=(6, 0))
        ttk.Button(flow_group, text="A-失. 車門未關 ❌",
                   command=lambda: self._load_flow_preset("PRECHECK_FAIL_DOOR")).grid(row=3, column=1, **b)
        ttk.Button(flow_group, text="A-失. 鑰匙插入 ❌",
                   command=lambda: self._load_flow_preset("PRECHECK_FAIL_KEY")).grid(row=3, column=2, **b)
        ttk.Button(flow_group, text="B-失. 解鎖失敗 ❌",
                   command=lambda: self._load_flow_preset("ACC_FAIL_DEALARM")).grid(row=3, column=3, **b)

        ttk.Button(flow_group, text="C-失. 引擎啟動失敗(門開) ❌",
                   command=lambda: self._load_flow_preset("ENG_FAIL_DOOR")).grid(row=4, column=1, **b)
        ttk.Button(flow_group, text="Gate2-失. TBOX 低電壓 ❌",
                   command=lambda: self._load_flow_preset("TBOX_LOW_BATTERY")).grid(row=4, column=2, **b)
        ttk.Button(flow_group, text="D-異. 逾時熄火 ❌",
                   command=lambda: self._load_flow_preset("SHUTDOWN_TIMEOUT")).grid(row=4, column=3, **b)
        ttk.Button(flow_group, text="E. TBOX 熄火確認 ✅（開放重啟）",
                   command=lambda: self._load_flow_preset("TBOX_ENGINE_OFF")).grid(row=5, column=1, columnspan=2, **b)

        for i in range(1, 4):
            flow_group.columnconfigure(i, weight=1)

        # 自動播放按鈕
        auto_row = ttk.Frame(flow_group)
        auto_row.grid(row=6, column=0, columnspan=4, sticky=tk.EW, pady=(10, 0))
        auto_row.columnconfigure(0, weight=1)
        auto_row.columnconfigure(1, weight=1)

        self.btn_auto_play = ttk.Button(
            auto_row, text="▶ 完整成功流程自動播放",
            command=self._auto_play_success_thread
        )
        self.btn_auto_play.grid(row=0, column=0, padx=(0, 4), sticky=tk.EW)

        self.btn_auto_play_fail = ttk.Button(
            auto_row, text="▶ 失敗流程自動播放（低電壓）",
            command=self._auto_play_fail_thread
        )
        self.btn_auto_play_fail.grid(row=0, column=1, padx=(4, 0), sticky=tk.EW)

        self.lbl_auto_status = ttk.Label(flow_group, text="", foreground="#718096", font=("Helvetica", 8))
        self.lbl_auto_status.grid(row=7, column=0, columnspan=4, sticky=tk.W, pady=(4, 0))

        # ── 4. MQTT Payload 編輯器 ────────────────────────────────────────────
        payload_group = ttk.LabelFrame(parent, text=" 4. MQTT Inform Payload 編輯器 ", padding=10)
        payload_group.pack(fill=tk.BOTH, expand=True)

        self.txt_payload = scrolledtext.ScrolledText(payload_group, height=10, font=("Consolas", 10))
        self.txt_payload.pack(fill=tk.BOTH, expand=True, pady=(0, 5))

        self.btn_send = ttk.Button(payload_group, text="🚀 發送 MQTT Inform 封包", command=self._publish_mqtt)
        self.btn_send.pack(fill=tk.X)

    def _build_right(self, parent):
        # ── 實時車輛狀態 ──────────────────────────────────────────────────────
        status_group = ttk.LabelFrame(parent, text=" 實時車輛狀態 (WebSocket) ", padding=10)
        status_group.pack(fill=tk.X, pady=(0, 10))

        self.lbl_ui_state = tk.Label(
            status_group, text="等待數據中...",
            font=("Helvetica", 14, "bold"), bg="#4A5568", fg="white", pady=10
        )
        self.lbl_ui_state.pack(fill=tk.X, pady=(0, 5))

        self.lbl_freshness = tk.Label(
            status_group, text="尚未收到資料",
            font=("Helvetica", 8), fg="#718096", anchor="w"
        )
        self.lbl_freshness.pack(fill=tk.X, pady=(0, 8))

        cards_frame = ttk.Frame(status_group)
        cards_frame.pack(fill=tk.X)

        self.lbl_door_lock   = self._create_card(cards_frame, "門鎖狀態",    "未知",  0, 0)
        self.lbl_door_open   = self._create_card(cards_frame, "車門狀況",    "未知",  0, 1)
        self.lbl_engine      = self._create_card(cards_frame, "引擎狀態",    "OFF",   0, 2)
        self.lbl_battery     = self._create_card(cards_frame, "電瓶電壓",    "0 V",   0, 3)
        self.lbl_mileage     = self._create_card(cards_frame, "里程",        "0 km",  1, 0)
        self.lbl_fuel_level  = self._create_card(cards_frame, "油量",        "0 %",   1, 1)
        self.lbl_can_restart = self._create_card(cards_frame, "可否重啟",    "未知",  1, 2)
        self.lbl_retry_count = self._create_card(cards_frame, "點火嘗試次數","0",     1, 3)

        ttk.Label(
            status_group,
            text="※ 可否重啟／點火嘗試次數僅在有進行中 Session 時才會變化",
            font=("Helvetica", 8), foreground="#718096",
        ).pack(fill=tk.X, pady=(4, 0))

        self.lbl_dtc = tk.Label(status_group, text="DTC 錯誤碼: 無", font=("Helvetica", 9), fg="#4A5568", anchor="w")
        self.lbl_dtc.pack(fill=tk.X, pady=(5, 0))

        self.lbl_failure_reason = tk.Label(status_group, text="", font=("Helvetica", 9), fg="#E53E3E", anchor="w")
        self.lbl_failure_reason.pack(fill=tk.X, pady=(2, 0))

        # ── 連線狀態指示燈 ───────────────────────────────────────────────────
        conn_status_frame = ttk.Frame(parent)
        conn_status_frame.pack(fill=tk.X, pady=(0, 10))

        self.lbl_status_mqtt = tk.Label(
            conn_status_frame, text="MQTT: 未連線",
            bg="#E53E3E", fg="white", font=("Helvetica", 9, "bold"), width=15
        )
        self.lbl_status_mqtt.pack(side=tk.LEFT, padx=(0, 5))

        self.lbl_status_ws = tk.Label(
            conn_status_frame, text="WebSocket: 未連線",
            bg="#E53E3E", fg="white", font=("Helvetica", 9, "bold"), width=20
        )
        self.lbl_status_ws.pack(side=tk.LEFT)

        # ── 系統日誌 ─────────────────────────────────────────────────────────
        log_group = ttk.LabelFrame(parent, text=" 系統日誌 ", padding=10)
        log_group.pack(fill=tk.BOTH, expand=True)

        self.txt_log = scrolledtext.ScrolledText(log_group, height=10, font=("Consolas", 9), state="disabled")
        self.txt_log.pack(fill=tk.BOTH, expand=True)

    # =========================================================================
    # Session 管理
    # =========================================================================

    def _create_session_thread(self):
        self.btn_create_session.config(state="disabled", text="建立中...")
        threading.Thread(target=self._create_session, daemon=True).start()

    def _create_session(self):
        device_id = self.entry_device_id.get().strip() or DEFAULT_DEVICE_ID
        api_base  = self.entry_api_base_url.get().strip().rstrip("/")
        url       = f"{api_base}/device-command/remote-start"

        body = json.dumps({
            "imei": device_id,
            "command": "REMOTE_CHECK_CAR_STATUS"
        }).encode("utf-8")

        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            # 本機開發環境不需要 SSL 驗證
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                data = json.loads(raw)

            session_id = data.get("data", {}).get("sessionId") or data.get("sessionId")
            state      = data.get("data", {}).get("state", "")

            if not session_id:
                self.log("ERROR", f"API 回應中找不到 sessionId: {raw}")
                self._set_widget(self.btn_create_session, state="normal", text="🚗 建立 Session（模擬使用者按下啟動）")
                return

            self.current_session_id = session_id
            self.log("SESSION", f"✅ Session 建立成功！sessionId={session_id}  state={state}")
            self.after(0, self._update_session_label, session_id)

        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")
            self.log("ERROR", f"建立 Session 失敗 HTTP {e.code}: {body_text}")
        except Exception as e:
            self.log("ERROR", f"建立 Session 例外: {e}")
        finally:
            self._set_widget(self.btn_create_session, state="normal", text="🚗 建立 Session（模擬使用者按下啟動）")

    def _update_session_label(self, session_id: str):
        self.entry_session_id.delete(0, tk.END)
        self.entry_session_id.insert(0, session_id)

    def _clear_session(self):
        self.current_session_id = None
        self.entry_session_id.delete(0, tk.END)
        self.log("SESSION", "Session ID 已清除，後續 Inform 將使用 payload 內的 sid 欄位（或預設值）")

    def _get_current_sid(self) -> str:
        """取得目前有效的 sid：優先讀取輸入框的值，沒有則用預設值。"""
        sid = self.entry_session_id.get().strip()
        return sid if sid else "00000000000000000000000000"

    # =========================================================================
    # 流程步驟 Preset（對齊 V0.0.0.6）
    # =========================================================================

    def _load_flow_preset(self, key: str):
        """將對應流程步驟的 Inform 封包填入 payload 編輯器。"""
        device_id  = self.entry_device_id.get().strip() or DEFAULT_DEVICE_ID
        sid        = self._get_current_sid()
        now_ts     = int(time.time())

        presets = {
            # ── 正常流程 ──────────────────────────────────────────────────
            # A. SRDT 前置檢查通過（info 0x00000001 = SRDT_PRCHECK_OK）
            "PRECHECK_OK": {
                "sid": sid, "time": now_ts, "srdt_imei": device_id,
                "info": "0x00000001",
                "status": {
                    "alarm": 1, "lock": 1, "door_open": 0,
                    "key": 0, "brake": 0, "acc_ign": 0,
                    "eng_start": 0, "start_time": 0
                }
            },
            # B. SRDT ACC 已 ON（info 0x00000040 = ACC_ON_ALREADY）
            # start_time 在此封包才帶入實際計時開始時間
            "ACC_IGN_ON": {
                "sid": sid, "time": now_ts, "srdt_imei": device_id,
                "info": "0x00000040",
                "status": {
                    "alarm": 1, "lock": 1, "door_open": 0,
                    "key": 0, "brake": 0, "acc_ign": 1,
                    "eng_start": 0, "start_time": now_ts
                }
            },
            # Gate2. TBOX 開機回報（ACC ON 後 TBOX 才開機，回報手煞車/電壓）
            "TBOX_GATE2": {
                "sid": sid, "time": now_ts, "tbox_imei": device_id,
                "info": "GOT_REMOTE_CHECK_TASK",
                "status": {"acc_status": 1, "handbrake": 1, "battery": 23.5},
                "can": {"totalMileage": 12500, "fuelLevel": 75, "rpm": 0, "dtc": []}
            },
            # C. SRDT 引擎已點火（info 0x00002000 = ENG_START_ALREADY）
            "ENG_START": {
                "sid": sid, "time": now_ts, "srdt_imei": device_id,
                "info": "0x00002000",
                "status": {
                    "alarm": 1, "lock": 1, "door_open": 0,
                    "key": 0, "brake": 0, "acc_ign": 1,
                    "eng_start": 1, "start_time": now_ts - 30
                }
            },
            # C+. TBOX 確認引擎轉速（繞過 15 秒等待，直接發帶 RPM 的車況）
            "TBOX_ENGINE_CONFIRM": {
                "sid": sid, "time": now_ts, "tbox_imei": device_id,
                "info": "PUB_CAR_STATUS_INFO",
                "status": {"acc_status": 1, "handbrake": 1, "battery": 23.8},
                "can": {"totalMileage": 12500, "fuelLevel": 75, "rpm": 800, "dtc": []}
            },
            # D. 正常熄火（info 0x00040000 = ACC_ENG_OFF_BY_CMD）
            "SHUTDOWN_CMD": {
                "sid": sid, "time": now_ts, "srdt_imei": device_id,
                "info": "0x00040000",
                "status": {
                    "alarm": 1, "lock": 1, "door_open": 0,
                    "key": 0, "brake": 0, "acc_ign": 0,
                    "eng_start": 0, "start_time": now_ts - 120
                }
            },

            # ── 失敗 / 異常情境 ───────────────────────────────────────────
            # A-失. 車門未關（info 0x00000004 = SRDT_PRCHECK_FAIL_DOOR_OPEN）
            "PRECHECK_FAIL_DOOR": {
                "sid": sid, "time": now_ts, "srdt_imei": device_id,
                "info": "0x00000004",
                "status": {
                    "alarm": 1, "lock": 1, "door_open": 1,
                    "key": 0, "brake": 0, "acc_ign": 0,
                    "eng_start": 0, "start_time": 0
                }
            },
            # A-失. 鑰匙插入（info 0x00000008 = SRDT_PRCHECK_FAIL_KEY_INSERTED）
            "PRECHECK_FAIL_KEY": {
                "sid": sid, "time": now_ts, "srdt_imei": device_id,
                "info": "0x00000008",
                "status": {
                    "alarm": 1, "lock": 1, "door_open": 0,
                    "key": 1, "brake": 0, "acc_ign": 0,
                    "eng_start": 0, "start_time": 0
                }
            },
            # B-失. 解鎖失敗（info 0x00000080 = ACC_FAIL_DEALARM_UNLOCK）
            "ACC_FAIL_DEALARM": {
                "sid": sid, "time": now_ts, "srdt_imei": device_id,
                "info": "0x00000080",
                "status": {
                    "alarm": 1, "lock": 1, "door_open": 0,
                    "key": 0, "brake": 0, "acc_ign": 0,
                    "eng_start": 0, "start_time": 0
                }
            },
            # C-失. 引擎啟動失敗—車門開啟（info 0x00008000 = ENG_FAIL_DOOR_OPEN）
            "ENG_FAIL_DOOR": {
                "sid": sid, "time": now_ts, "srdt_imei": device_id,
                "info": "0x00008000",
                "status": {
                    "alarm": 0, "lock": 0, "door_open": 1,
                    "key": 0, "brake": 0, "acc_ign": 0,
                    "eng_start": 0, "start_time": now_ts - 60
                }
            },
            # Gate2-失. TBOX 低電壓（電瓶 11V，Gate 2 應失敗）
            "TBOX_LOW_BATTERY": {
                "sid": sid, "time": now_ts, "tbox_imei": device_id,
                "info": "GOT_REMOTE_CHECK_TASK",
                "status": {"acc_status": 1, "handbrake": 1, "battery": 11.0},
                "can": {"totalMileage": 12500, "fuelLevel": 30, "rpm": 0, "dtc": []}
            },
            # D-異. SRDT 逾時自動熄火（info 0x00080000 = ACC_ENG_OFF_BY_TIMEOUT）
            "SHUTDOWN_TIMEOUT": {
                "sid": sid, "time": now_ts, "srdt_imei": device_id,
                "info": "0x00080000",
                "status": {
                    "alarm": 1, "lock": 1, "door_open": 0,
                    "key": 0, "brake": 0, "acc_ign": 0,
                    "eng_start": 0, "start_time": now_ts - 420
                }
            },
        }

        if key in presets:
            self.txt_payload.delete("1.0", tk.END)
            self.txt_payload.insert(tk.END, json.dumps(presets[key], indent=2, ensure_ascii=False))

    # =========================================================================
    # 自動播放
    # =========================================================================

    def _auto_play_success_thread(self):
        if self.auto_play_running:
            self.log("WARN", "自動播放已在進行中，請等待完成")
            return
        threading.Thread(target=self._auto_play_success, daemon=True).start()

    def _auto_play_fail_thread(self):
        if self.auto_play_running:
            self.log("WARN", "自動播放已在進行中，請等待完成")
            return
        threading.Thread(target=self._auto_play_fail, daemon=True).start()

    def _auto_play_success(self):
        """完整成功流程自動播放：A → B → Gate2 → C → C+(x2) → D → E

        ⚠️  C+ 需要發送兩次的原因：
        後端 handleTboxInform 的 IGNITING case 在 markIgnited()（IGNITING→RUNNING）
        後直接 break，沒有立即處理同一筆 PUB_CAR_STATUS_INFO 的 completeCheck，
        導致 Session 停在 RUNNING 等下一筆才觸發 completeCheck → SHUTTING_DOWN。
        這是已知 bug，待 Sherry 修正後可移除重複步驟。
        """
        steps = [
            ("PRECHECK_OK",          "A. 前置檢查通過（SRDT）"),
            ("ACC_IGN_ON",           "B. ACC 已 ON（SRDT）"),
            ("TBOX_GATE2",           "Gate2. TBOX 開機回報"),
            ("ENG_START",            "C. 引擎已點火（SRDT）"),
            ("TBOX_ENGINE_CONFIRM",  "C+. TBOX 轉速確認（第1次）→ markIgnited"),
            ("TBOX_ENGINE_CONFIRM",  "C+. TBOX 轉速確認（第2次）→ completeCheck ⚠️ workaround"),
            ("SHUTDOWN_CMD",         "D. 正常熄火（SRDT）"),
            ("TBOX_ENGINE_OFF",      "E. TBOX 熄火確認 → can_restart = true"),
        ]
        self._run_auto_play(steps)

    def _auto_play_fail(self):
        """失敗流程自動播放（低電壓）：A → B → Gate2低電壓失敗 → 熄火"""
        steps = [
            ("PRECHECK_OK",       "A. 前置檢查通過（SRDT）"),
            ("ACC_IGN_ON",        "B. ACC 已 ON（SRDT）"),
            ("TBOX_LOW_BATTERY",  "Gate2. TBOX 低電壓 → 應觸發失敗"),
            ("SHUTDOWN_CMD",      "D. 熄火（後端下發後 SRDT 回報）"),
        ]
        self._run_auto_play(steps)

    def _run_auto_play(self, steps: list):
        """執行自動播放序列。steps = [(preset_key, 說明), ...]"""
        if not self.mqtt_connected or not self.mqtt_client:
            self.log("WARN", "自動播放需要先連線 MQTT")
            return

        self.auto_play_running = True
        self._set_widget(self.btn_auto_play, state="disabled")
        self._set_widget(self.btn_auto_play_fail, state="disabled")
        self.after(0, lambda: self.lbl_auto_status.config(text="▶ 自動播放進行中...", foreground="#DD6B20"))

        total = len(steps)
        for idx, (preset_key, description) in enumerate(steps, start=1):
            step_msg = f"[{idx}/{total}] {description}"
            self.log("AUTO", f"發送 {step_msg}")
            self.after(0, lambda m=step_msg: self.lbl_auto_status.config(text=f"▶ {m}", foreground="#DD6B20"))

            # 產生 payload 並發送
            self.after(0, lambda k=preset_key: self._load_flow_preset(k))
            time.sleep(0.1)  # 等 UI 更新完再讀 payload
            self._publish_mqtt_from_auto_play()

            if idx < total:
                time.sleep(AUTO_PLAY_STEP_DELAY)

        self.auto_play_running = False
        self._set_widget(self.btn_auto_play, state="normal")
        self._set_widget(self.btn_auto_play_fail, state="normal")
        self.after(0, lambda: self.lbl_auto_status.config(text="✅ 自動播放完成", foreground="#38A169"))
        self.log("AUTO", "✅ 自動播放序列全部完成")

    def _publish_mqtt_from_auto_play(self):
        """供自動播放呼叫的同步發送（在背景執行緒直接執行，不用 UI 互動）。"""
        try:
            raw_json = self.txt_payload.get("1.0", tk.END).strip()
            payload  = json.loads(raw_json)
            payload["time"] = int(time.time())

            device_id = self.entry_device_id.get().strip() or DEFAULT_DEVICE_ID
            device_type = "srdt" if "srdt_imei" in payload else "tbox" if "tbox_imei" in payload else "unknown"
            topic_template = self.entry_topic_template.get().strip() or DEFAULT_TOPIC_TEMPLATE
            topic = topic_template.format(device_id=device_id, device_type=device_type)

            final_json = json.dumps(payload, ensure_ascii=False)
            result = self.mqtt_client.publish(topic, final_json, qos=1)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                self.log("ERROR", f"[AUTO] 發送失敗，代碼: {result.rc}")
        except Exception as e:
            self.log("ERROR", f"[AUTO] 發送例外: {e}")

    # =========================================================================
    # MQTT 連線
    # =========================================================================

    def _connect_mqtt_thread(self):
        self.btn_connect_mqtt.config(text="🔌 連線中...", state="disabled")
        threading.Thread(target=self._connect_mqtt, daemon=True).start()

    def _connect_mqtt(self):
        host     = self.entry_mqtt_host.get().strip()
        port     = int(self.entry_mqtt_port.get().strip())
        user     = self.entry_mqtt_user.get().strip()
        password = self.entry_mqtt_pass.get().strip()

        self.log("MQTT", f"嘗試連線至 MQTTS {host}:{port} ...")
        try:
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
            self.mqtt_client.on_connect    = self._on_mqtt_connect
            self.mqtt_client.on_disconnect = self._on_mqtt_disconnect
            self.mqtt_client.on_publish    = self._on_mqtt_publish

            self.mqtt_client.connect(host, port, keepalive=60)
            self.mqtt_client.loop_start()
        except Exception as e:
            self.log("ERROR", f"MQTT 初始化失敗: {e}")
            self._set_widget(self.btn_connect_mqtt, text="🔌 連線 MQTT", state="normal")

    def _on_mqtt_connect(self, client, userdata, flags, rc):
        if rc == 0:
            self.mqtt_connected = True
            self.mqtt_auth_fail_count = 0
            self.last_connect_ts = time.time()
            self._set_widget(self.lbl_status_mqtt, text="MQTT: 已連線", bg="#38A169")
            self._set_widget(self.btn_connect_mqtt, text="✅ MQTT 已連線", state="disabled")
            for w in (self.entry_mqtt_host, self.entry_mqtt_port,
                      self.entry_mqtt_user, self.entry_mqtt_pass):
                self._set_widget(w, state="disabled")
            self.log("MQTT", "MQTTS 連線成功！")
        else:
            err_msg = {
                1: "不對應的通訊協定版本", 2: "無效的 Client ID",
                3: "MQTT 服務不可用",      4: "錯誤的 User/Password",
                5: "無權限 (ACL 阻擋)",
            }.get(rc, f"未知代碼 rc={rc}")
            self.log("ERROR", f"MQTT 連線拒絕: {err_msg}")
            self._set_widget(self.btn_connect_mqtt, text="🔌 連線 MQTT", state="normal")

            if rc in (4, 5):
                self.mqtt_auth_fail_count += 1
                if self.last_connect_ts:
                    self.log("WARN", "先前曾連線成功，此次認證失敗，可能是 token 已過期")
                if self.mqtt_auth_fail_count >= 2:
                    self.log("ERROR", f"連續 {self.mqtt_auth_fail_count} 次認證失敗，已停止自動重連")
                    self.mqtt_connected = False
                    try:
                        client.disconnect()
                    except Exception:
                        pass

    def _on_mqtt_disconnect(self, client, userdata, rc):
        self.mqtt_connected = False
        self._set_widget(self.lbl_status_mqtt, text="MQTT: 已中斷", bg="#E53E3E")
        self._set_widget(self.btn_connect_mqtt, text="🔌 連線 MQTT", state="normal")
        for w in (self.entry_mqtt_host, self.entry_mqtt_port,
                  self.entry_mqtt_user, self.entry_mqtt_pass):
            self._set_widget(w, state="normal")
        if self.last_connect_ts:
            duration = time.time() - self.last_connect_ts
            self.log("MQTT", f"MQTT 連線中斷 (rc={rc})，維持 {duration:.0f} 秒")
        else:
            self.log("MQTT", f"MQTT 連線中斷 (rc={rc})")

    def _on_mqtt_publish(self, client, userdata, mid):
        self.log("MQTT_PUB", f"Broker 已確認收到封包 (mid={mid})")

    # =========================================================================
    # MQTT 發送
    # =========================================================================

    def _publish_mqtt(self):
        if not self.mqtt_connected or not self.mqtt_client:
            messagebox.showwarning("警告", "MQTT 未連線，請先點擊「連線 MQTT」！")
            return

        raw_json = self.txt_payload.get("1.0", tk.END).strip()
        try:
            payload = json.loads(raw_json)
            payload["time"] = int(time.time())

            # 自動帶入最新的 sid（若 payload 裡已有 sid 則保留，否則帶入目前的 session）
            if "sid" not in payload or not payload["sid"]:
                payload["sid"] = self._get_current_sid()

            final_json = json.dumps(payload, indent=2, ensure_ascii=False)
            self.txt_payload.delete("1.0", tk.END)
            self.txt_payload.insert(tk.END, final_json)

            device_id = self.entry_device_id.get().strip() or DEFAULT_DEVICE_ID
            device_type = "srdt" if "srdt_imei" in payload else "tbox" if "tbox_imei" in payload else "unknown"
            topic_template = self.entry_topic_template.get().strip() or DEFAULT_TOPIC_TEMPLATE

            try:
                topic = topic_template.format(device_id=device_id, device_type=device_type)
            except (KeyError, IndexError) as e:
                messagebox.showerror("Topic 樣板錯誤", f"格式不正確: {e}")
                return

            result = self.mqtt_client.publish(topic, final_json, qos=1)
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                self.log("ERROR", f"發送失敗（本地佇列），代碼: {result.rc}")
                return

            self.log("MQTT_PUB", f"已送出至 {topic}，等待 PUBACK...")

            def wait_and_check():
                try:
                    result.wait_for_publish(timeout=5)
                    if not result.is_published():
                        self.log("WARN", f"5 秒後仍未收到 PUBACK (topic={topic})，請確認 ACL 設定")
                except Exception as e:
                    self.log("ERROR", f"等待 PUBACK 例外: {e}")

            threading.Thread(target=wait_and_check, daemon=True).start()
        except json.JSONDecodeError as e:
            messagebox.showerror("格式錯誤", f"JSON 語法無效: {e}")

    # =========================================================================
    # WebSocket 連線
    # =========================================================================

    def _connect_ws_thread(self):
        self.btn_connect_ws.config(text="🔌 連線中...", state="disabled")
        threading.Thread(target=self._connect_ws, daemon=True).start()

    def _connect_ws(self):
        ws_url    = self.entry_ws_url.get().strip()
        device_id = self.entry_device_id.get().strip() or DEFAULT_DEVICE_ID
        self.log("WS", f"嘗試連線至 WebSocket {ws_url} ...")

        def on_message(ws, message):
            try:
                data = json.loads(message)
                if "data" in data:
                    v_data      = data["data"]
                    incoming_id = self._extract_incoming_device_id(v_data)
                    self.log(
                        "WS_RECV",
                        f"裝置={incoming_id or '無法辨識'} | 訂閱={self.subscribed_device_id} | "
                        f"{self._format_broadcast_summary(v_data)}"
                    )
                    if incoming_id and self.subscribed_device_id and incoming_id != self.subscribed_device_id:
                        return
                    if not incoming_id:
                        self.log("WARN", "此筆廣播找不到裝置識別欄位，無法確認是否為目前訂閱的設備")
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
            ws.send(json.dumps({"action": "SUBSCRIBE_VEHICLES", "device_ids": [device_id]}))

        def on_close(ws, close_status_code, close_msg):
            self.ws_connected = False
            self.subscribed_device_id = None
            self._set_widget(self.lbl_status_ws, text="WebSocket: 已中斷", bg="#E53E3E")
            self._set_widget(self.btn_connect_ws, text="🔌 連線 WebSocket", state="normal")
            self._set_widget(self.entry_ws_url, state="normal")

        def on_error(ws, error):
            self.log("ERROR", f"WS 錯誤: {error}")
            self._set_widget(self.btn_connect_ws, text="🔌 連線 WebSocket", state="normal")

        self.ws_app = websocket.WebSocketApp(
            ws_url, on_open=on_open, on_message=on_message,
            on_error=on_error, on_close=on_close
        )
        self.ws_app.run_forever(sslopt={"cert_reqs": ssl.CERT_NONE, "check_hostname": False})

    def _switch_device(self):
        new_device = self.entry_device_id.get().strip()
        if not new_device:
            messagebox.showwarning("警告", "請輸入 Device ID")
            return
        if not self.ws_connected or not self.ws_app:
            messagebox.showwarning("警告", "WebSocket 尚未連線")
            return

        old_device = self.subscribed_device_id
        try:
            if old_device and old_device != new_device:
                self.ws_app.send(json.dumps({"action": "UNSUBSCRIBE_VEHICLES", "device_ids": [old_device]}))
                self.log("WS", f"取消訂閱舊設備: {old_device}")

            self.ws_app.send(json.dumps({"action": "SUBSCRIBE_VEHICLES", "device_ids": [new_device]}))
            self.subscribed_device_id = new_device
            self.log("WS", f"已訂閱新設備: {new_device}")
            self._reset_vehicle_ui()
        except Exception as e:
            self.log("ERROR", f"切換設備訂閱失敗: {e}")

    # =========================================================================
    # 全部重置
    # =========================================================================

    def _reset_all(self):
        mqtt_client = self.mqtt_client
        ws_app      = self.ws_app

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

        self.mqtt_client            = None
        self.mqtt_connected         = False
        self.mqtt_auth_fail_count   = 0
        self.last_connect_ts        = None
        self.ws_app                 = None
        self.ws_connected           = False
        self.subscribed_device_id   = None
        self.last_device_ts         = None
        self.current_session_id     = None
        self.auto_play_running      = False

        for w in (self.entry_mqtt_host, self.entry_mqtt_port,
                  self.entry_mqtt_user, self.entry_mqtt_pass, self.entry_ws_url):
            w.config(state="normal")

        self.btn_connect_mqtt.config(text="🔌 連線 MQTT", state="normal")
        self.btn_connect_ws.config(text="🔌 連線 WebSocket", state="normal")
        self.btn_auto_play.config(state="normal")
        self.btn_auto_play_fail.config(state="normal")
        self.lbl_status_mqtt.config(text="MQTT: 未連線", bg="#E53E3E")
        self.lbl_status_ws.config(text="WebSocket: 未連線", bg="#E53E3E")
        
        self.entry_session_id.delete(0, tk.END)
        
        self.lbl_auto_status.config(text="")
        self._reset_vehicle_ui()
        self.log("SYSTEM", "已重置所有連線與狀態")

    # =========================================================================
    # UI 輔助
    # =========================================================================

    def _reset_vehicle_ui(self):
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
        self.last_device_ts = None
        self.lbl_freshness.config(text="尚未收到資料", fg="#718096")

    def _tick_freshness(self):
        if self.last_device_ts is not None:
            elapsed = time.time() - self.last_device_ts
            color   = "#38A169" if elapsed < 10 else "#DD6B20" if elapsed < 30 else "#E53E3E"
            suffix  = "（資料可能已過期或已斷線）" if elapsed >= 30 else ""
            self.lbl_freshness.config(text=f"最後更新: {elapsed:.0f} 秒前{suffix}", fg=color)
        self.after(1000, self._tick_freshness)

    def _create_card(self, parent, title, default_val, row, col):
        frame = ttk.Frame(parent, relief="groove", padding=8)
        frame.grid(row=row, column=col, padx=3, pady=3, sticky="nsew")
        parent.columnconfigure(col, weight=1)
        ttk.Label(frame, text=title, font=("Helvetica", 8)).pack(anchor="w")
        lbl = tk.Label(frame, text=default_val, font=("Helvetica", 11, "bold"), anchor="w")
        lbl.pack(anchor="w", pady=(2, 0))
        return lbl

    def log(self, category, msg):
        def _do():
            self.txt_log.config(state="normal")
            self.txt_log.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] [{category}] {msg}\n")
            self.txt_log.see(tk.END)
            self.txt_log.config(state="disabled")
        self.after(0, _do)

    def _set_widget(self, widget, **kwargs):
        self.after(0, lambda: widget.config(**kwargs))

    def _format_broadcast_summary(self, v_data):
        ui_text    = v_data.get("ui_state_text", "未知")
        color      = v_data.get("status_color", "GRAY")
        details    = v_data.get("details", {}) or {}
        lock       = details.get("door_lock", "?")
        door       = "開啟" if details.get("door_open") else "關閉"
        battery    = details.get("battery_voltage", "?")
        engine     = details.get("engine_status", "?")
        can_restart = v_data.get("can_restart")
        dtc_count  = len(details.get("dtc_errors") or [])
        summary = (
            f"狀態={ui_text}({color}) | 門鎖={lock} | 車門={door} | 電瓶={battery}V | "
            f"引擎={engine} | 可重啟={can_restart} | DTC={dtc_count}"
        )
        reason = v_data.get("failure_reason")
        if reason:
            summary += f" | 失敗原因={reason}"
        return summary

    def _extract_incoming_device_id(self, v_data):
        candidate_keys = ("device_id", "deviceid", "srdt_imei", "tbox_imei", "imei", "vin")

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

    def _update_vehicle_ui(self, v_data):
        ui_text    = v_data.get("ui_state_text", "未知狀態")
        color_code = v_data.get("status_color", "GRAY")
        bg_map = {
            "GREEN": "#2F855A", "BLUE": "#2B6CB0",
            "RED":   "#C53030", "GRAY": "#4A5568", "ORANGE": "#DD6B20"
        }
        self.lbl_ui_state.config(text=ui_text, bg=bg_map.get(color_code, "#4A5568"))

        details  = v_data.get("details", {}) or {}
        lock_val = details.get("door_lock", "UNLOCKED")
        lock_str = "已上鎖 (LOCKED)" if lock_val == "LOCKED" else "解鎖失敗 (LOCK_FAILED)" if lock_val == "LOCK_FAILED" else "解鎖中 (UNLOCKED)"
        open_str = "開啟中 (OPEN)" if details.get("door_open") else "關閉 (CLOSED)"

        self.lbl_door_lock.config(text=lock_str)
        self.lbl_door_open.config(text=open_str, fg="#E53E3E" if details.get("door_open") else "black")
        self.lbl_battery.config(text=f"{details.get('battery_voltage', 0)} V")
        self.lbl_engine.config(text=str(details.get("engine_status", "OFF")))
        self.lbl_mileage.config(text=f"{details.get('mileage', 0)} km")
        self.lbl_fuel_level.config(text=f"{details.get('fuel_level', 0)} %")

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

        ts = v_data.get("timestamp")
        if isinstance(ts, (int, float)):
            self.last_device_ts = ts


if __name__ == "__main__":
    app = FmsTesterApp()
    app.mainloop()