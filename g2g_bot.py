# ===================================================================================
# G2G BOT v0.1 - API-FIRST + MANUAL CHAT DELIVERY (FULLY AUTOMATED 24/7 MASTER BUILD)
# -----------------------------------------------------------------------------------
# What this bot does:
# - Uses official G2G OpenAPI V2 for offers/order lookup where available.
# - Keeps local SQLite stock database: available -> reserved -> sold.
# - Imports stock lines from import_keys_g2g/*.txt.
# - Generates CATALOG.txt with short IDs.
# - Prepares buyer delivery messages.
# - Can optionally send messages into G2G order chat through a normal logged-in browser.
# - Can optionally run a webhook listener and queue incoming orders.
#
# Important:
# - This script does NOT bypass captchas, Cloudflare, 2FA, or platform restrictions.
# - Browser mode expects that you are already logged in inside BROWSER_PROFILE_DIR.
# - Keep BROWSER_SEND_ENABLED=0 until selectors/order URL are verified.
# ===================================================================================

import os
import re
import sys
import json
import time
import uuid
import hmac
import queue
import hashlib
import shutil
import sqlite3
import argparse
import traceback
import threading
import socket
import subprocess
from typing import Optional, Dict, Any, List, Tuple
from urllib.parse import urlparse
try:
    import pyotp  # optional, only needed for G2G TOTP auto-MFA
except Exception:
    pyotp = None

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

try:
    from dotenv import load_dotenv
except ImportError as exc:
    raise RuntimeError("python-dotenv is not installed. Install it with: pip install python-dotenv") from exc

load_dotenv(dotenv_path=os.path.join(BASE_DIR, ".env"), override=True)

import requests

try:
    from DrissionPage import ChromiumPage, ChromiumOptions
except Exception:
    ChromiumPage = None
    ChromiumOptions = None


# ==============================================================================
# CONFIG
# ==============================================================================

def resolve_local_path(value: str, default_name: str) -> str:
    raw = (value or "").strip() or default_name
    if os.path.isabs(raw):
        return raw
    return os.path.join(BASE_DIR, raw)


def env_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default).strip())
    except Exception:
        return int(default)


def split_selectors(raw: str, defaults: List[str]) -> List[str]:
    value = (raw or "").strip()
    if not value:
        return defaults
    parts = [x.strip() for x in value.split("|")]
    return [x for x in parts if x]


CONFIG = {
    # G2G OpenAPI
    "G2G_API_KEY": os.getenv("G2G_API_KEY", "").strip(),
    "G2G_SECRET_KEY": os.getenv("G2G_SECRET_KEY", "").strip(),
    "G2G_USER_ID": os.getenv("G2G_USER_ID", "").strip(),
    "G2G_BASE_URL": os.getenv("G2G_BASE_URL", "https://open-api.g2g.com").strip().rstrip("/"),

    # Main paths
    "DATA_DIR": resolve_local_path(os.getenv("DATA_DIR", "data_g2g"), "data_g2g"),
    "GUIDES_DIR": resolve_local_path(os.getenv("GUIDES_DIR", "guides_g2g"), "guides_g2g"),
    "PROOFS_DIR": resolve_local_path(os.getenv("PROOFS_DIR", "proofs_g2g"), "proofs_g2g"),
    "IMPORT_DIR": resolve_local_path(os.getenv("IMPORT_DIR", "import_keys_g2g"), "import_keys_g2g"),
    "IMPORT_DONE_DIR": resolve_local_path(os.getenv("IMPORT_DONE_DIR", "import_done_g2g"), "import_done_g2g"),
    "IMPORT_FAILED_DIR": resolve_local_path(os.getenv("IMPORT_FAILED_DIR", "import_failed_g2g"), "import_failed_g2g"),
    "BROWSER_PROFILE_DIR": resolve_local_path(os.getenv("BROWSER_PROFILE_DIR", "browser_profile_g2g"), "browser_profile_g2g"),

    # Loops
    "POLL_SECONDS": env_int("POLL_SECONDS", "10"),
    "SYNC_OFFERS_EVERY_LOOPS": env_int("SYNC_OFFERS_EVERY_LOOPS", "30"),
    "IMPORT_EVERY_LOOPS": env_int("IMPORT_EVERY_LOOPS", "3"),
    "LOW_STOCK_THRESHOLD": env_int("LOW_STOCK_THRESHOLD", "3"),

    # Browser / chat
    "G2G_WEB_BASE": os.getenv("G2G_WEB_BASE", "https://www.g2g.com").strip().rstrip("/"),
    # --- G2G seller auto-login (browser session) ---
    "G2G_EMAIL": os.getenv("G2G_EMAIL", "").strip(),
    "G2G_PASSWORD": os.getenv("G2G_PASSWORD", ""),
    "G2G_TOTP_SECRET": os.getenv("G2G_TOTP_SECRET", "").replace(" ", "").strip(),
    "G2G_LOGIN_URL": os.getenv("G2G_LOGIN_URL", "https://www.g2g.com/login").strip(),
    "G2G_MFA_WAIT": env_int("G2G_MFA_WAIT", "180"),
    "G2G_ORDER_URL_TEMPLATE": os.getenv("G2G_ORDER_URL_TEMPLATE", "https://www.g2g.com/order/{order_id}").strip(),
    "BROWSER_SEND_ENABLED": env_bool("BROWSER_SEND_ENABLED", "0"),
    "CLICK_DELIVERED_AFTER_SEND": env_bool("CLICK_DELIVERED_AFTER_SEND", "0"),
    "AUTO_CLOSE_BROWSER_AFTER_ORDER": env_bool("AUTO_CLOSE_BROWSER_AFTER_ORDER", "1"),
    "CHAT_READY_TIMEOUT": env_int("CHAT_READY_TIMEOUT", "45"),
    "CHAT_VERIFY_TIMEOUT": env_int("CHAT_VERIFY_TIMEOUT", "25"),
    "MAX_MESSAGE_CHARS": env_int("MAX_MESSAGE_CHARS", "2500"),
    "CUSTOM_USER_AGENT": os.getenv("CUSTOM_USER_AGENT", "").strip(),

    # Proxy settings
    "PROXY_HOST": os.getenv("PROXY_HOST", "").strip(),
    "PROXY_PORT": os.getenv("PROXY_PORT", "").strip(),
    "PROXY_USER": os.getenv("PROXY_USER", "").strip(),
    "PROXY_PASS": os.getenv("PROXY_PASS", "").strip(),
    "PROXY_SCHEME": os.getenv("PROXY_SCHEME", "http").strip().lower() or "http",

    "BROWSER_PROXY_MODE": os.getenv("BROWSER_PROXY_MODE", "off").strip().lower(),  # off | direct | wrapper
    "BROWSER_PROXY": os.getenv("BROWSER_PROXY", "").strip(),
    "BROWSER_PROXY_LISTEN": os.getenv("BROWSER_PROXY_LISTEN", "127.0.0.1:8899").strip(),

    "API_PROXY_ENABLED": env_bool("API_PROXY_ENABLED", "0"),

    # Selectors
    "CHAT_EDITOR_SELECTORS": split_selectors(
        os.getenv("CHAT_EDITOR_SELECTORS", ""),
        [
            "css:.toastui-editor-ww-container [contenteditable='true']",
            "css:.toastui-editor-ww-mode .ProseMirror[contenteditable='true']",
            "css:div.ProseMirror[contenteditable='true']",
            "css:.toastui-editor-contents[contenteditable='true']",
            "css:[id^='editor-g2g_dm_'] [contenteditable='true']",
            "css:div[contenteditable='true']",
            "css:textarea",
            "css:[role='textbox']",
        ],
    ),
    "CHAT_SEND_SELECTORS": split_selectors(
        os.getenv("CHAT_SEND_SELECTORS", ""),
        [
            "css:.g-send-btn button",
            "css:[id^='g-drag-area_g2g_dm_'] .g-send-btn button",
            "css:button[aria-label='Send']",
            "css:button[type='submit']",
            "text:Send",
            "text:Reply",
        ],
    ),
    "MESSAGE_TEXT_SELECTORS": split_selectors(
        os.getenv("MESSAGE_TEXT_SELECTORS", ""),
        [
            "css:[class*='message']",
            "css:[class*='Message']",
            "css:[data-testid*='message']",
            "css:body",
        ],
    ),
    "DELIVER_BUTTON_SELECTORS": split_selectors(
        os.getenv("DELIVER_BUTTON_SELECTORS", ""),
        [
            "text:Delivered",
            "text:Mark as delivered",
            "text:Confirm delivery",
            "text:Order delivered",
        ],
    ),
    "CONFIRM_BUTTON_SELECTORS": split_selectors(
        os.getenv("CONFIRM_BUTTON_SELECTORS", ""),
        [
            "text:Yes",
            "text:Confirm",
            "text:Submit",
            "text:I have delivered",
        ],
    ),

    # Telegram notifications
    "TG_NOTIFY_TOKEN": os.getenv("TG_NOTIFY_TOKEN", "").strip(),
    "TG_NOTIFY_CHAT": os.getenv("TG_NOTIFY_CHAT", "").strip(),

    # Webhook server
    "WEBHOOK_ENABLED": env_bool("WEBHOOK_ENABLED", "0"),
    "WEBHOOK_HOST": os.getenv("WEBHOOK_HOST", "0.0.0.0").strip(),
    "WEBHOOK_PORT": env_int("WEBHOOK_PORT", "5000"),
    "WEBHOOK_PATH": os.getenv("WEBHOOK_PATH", "/g2g-webhook").strip() or "/g2g-webhook",
    "WEBHOOK_SECRET_TOKEN": os.getenv("WEBHOOK_SECRET_TOKEN", "").strip(),
    "WEBHOOK_PUBLIC_URL": os.getenv("WEBHOOK_PUBLIC_URL", "").strip(),
    "WEBHOOK_VERIFY_SIGNATURE": env_bool("WEBHOOK_VERIFY_SIGNATURE", "1"),
    "WEBHOOK_ALLOWED_EVENTS": {
        x.strip()
        for x in os.getenv(
            "WEBHOOK_ALLOWED_EVENTS",
            "order.confirmed,order.api_delivery,order.created"
        ).split(",")
        if x.strip()
    },

    # Defaults for manual processing
    "G2G_TEST_OFFER_ID": os.getenv("G2G_TEST_OFFER_ID", "").strip(),
}

for path_key in [
    "DATA_DIR",
    "GUIDES_DIR",
    "PROOFS_DIR",
    "IMPORT_DIR",
    "IMPORT_DONE_DIR",
    "IMPORT_FAILED_DIR",
    "BROWSER_PROFILE_DIR",
]:
    os.makedirs(CONFIG[path_key], exist_ok=True)


DEFAULT_GUIDE_CONTENT = """Hello!
Thank you for your purchase.

You received Twitch account credentials for claiming the purchased Drops.

Steps:
1. Log in to the Twitch account.
2. Link it to your Steam/Rust account.
3. Open Twitch Drops inventory and claim the items.
4. Please confirm the delivery after everything is claimed.

Important:
- Rewards can usually be claimed only once per Rust account.
- Please record a continuous video from payment to checking inventory in case you need support.
"""


# ==============================================================================
# UTILS
# ==============================================================================

def p(msg: str) -> None:
    print(msg, flush=True)


def utc_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())


def atomic_write_text(path: str, text: str) -> None:
    tmp = f"{path}.tmp.{uuid.uuid4().hex}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def sha256_file(path: str) -> str:
    if not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w.\-]+", "_", (name or "").strip(), flags=re.UNICODE)
    return name[:200] or f"file_{uuid.uuid4().hex}"


def normalize_nonempty_lines(raw_text: str) -> List[str]:
    out = []
    for line in raw_text.splitlines():
        clean = line.strip()
        if clean:
            out.append(clean)
    return out


def chunk_message_lines(lines: List[str], max_chars: int) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0

    for line in lines:
        candidate_len = len(line) if not current else len(line) + 1
        if current and current_len + candidate_len > max_chars:
            chunks.append("\n".join(current).strip())
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += candidate_len

    if current:
        chunks.append("\n".join(current).strip())

    return [c for c in chunks if c.strip()]


def escape_for_log(s: str, limit: int = 180) -> str:
    text = (s or "").replace("\n", "\\n")
    return text[:limit] + ("..." if len(text) > limit else "")


def is_disconnect_error(err_text: str) -> bool:
    t = (err_text or "").lower()
    hints = [
        "connection to the page has been lost",
        "connection to page has been lost",
        "page disconnected",
        "disconnected",
        "websocket",
        "target page",
        "target closed",
        "browser has been closed",
        "与页面的连接已断开",
    ]
    return any(x.lower() in t for x in hints)


def row_to_dict(row: Optional[sqlite3.Row]) -> Dict[str, Any]:
    if not row:
        return {}
    return {k: row[k] for k in row.keys()}


# ==============================================================================
# PROXY HELPERS
# ==============================================================================

def parse_host_port(value: str) -> Tuple[str, int]:
    raw = (value or "").strip()
    if not raw:
        return "127.0.0.1", 8899
    if ":" not in raw:
        return raw, 80
    host, port = raw.rsplit(":", 1)
    return host.strip(), int(port.strip())


def tcp_port_open(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def build_api_proxy_url() -> Optional[str]:
    host = CONFIG["PROXY_HOST"]
    port = CONFIG["PROXY_PORT"]
    user = CONFIG["PROXY_USER"]
    password = CONFIG["PROXY_PASS"]
    scheme = CONFIG["PROXY_SCHEME"] or "http"

    if not host or not port:
        raw = CONFIG["BROWSER_PROXY"]
        return raw if raw else None

    if user and password:
        return f"{scheme}://{user}:{password}@{host}:{port}"
    return f"{scheme}://{host}:{port}"


def build_api_proxies_dict() -> Optional[Dict[str, str]]:
    if not CONFIG["API_PROXY_ENABLED"]:
        return None
    proxy_url = build_api_proxy_url()
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def build_pproxy_upstream() -> Optional[str]:
    host = CONFIG["PROXY_HOST"]
    port = CONFIG["PROXY_PORT"]
    user = CONFIG["PROXY_USER"]
    password = CONFIG["PROXY_PASS"]
    scheme = CONFIG["PROXY_SCHEME"] or "http"

    if not host or not port:
        return None

    if user and password:
        return f"{scheme}://{host}:{port}#{user}:{password}"
    return f"{scheme}://{host}:{port}"


class ProxyWrapper:
    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.listen_host, self.listen_port = parse_host_port(CONFIG["BROWSER_PROXY_LISTEN"])

    def enabled(self) -> bool:
        return CONFIG["BROWSER_PROXY_MODE"] == "wrapper"

    def browser_proxy_arg(self) -> Optional[str]:
        mode = CONFIG["BROWSER_PROXY_MODE"]

        if mode == "off":
            return None

        if mode == "direct":
            if CONFIG["BROWSER_PROXY"]:
                return CONFIG["BROWSER_PROXY"]
            proxy_url = build_api_proxy_url()
            if proxy_url:
                if "@" in proxy_url:
                    p("⚠️ BROWSER_PROXY_MODE=direct with login/password may not work in Chrome. Use wrapper mode.")
                return proxy_url
            return None

        if mode == "wrapper":
            return f"http://{self.listen_host}:{self.listen_port}"

        p(f"⚠️ Unknown BROWSER_PROXY_MODE={mode!r}; proxy disabled")
        return None

    def start(self) -> bool:
        if not self.enabled():
            return True

        if tcp_port_open(self.listen_host, self.listen_port):
            p(f"🌐 Browser proxy wrapper already listening on {self.listen_host}:{self.listen_port}")
            return True

        upstream = build_pproxy_upstream()
        if not upstream:
            p("❌ BROWSER_PROXY_MODE=wrapper, but PROXY_HOST/PROXY_PORT are missing in .env")
            return False

        cmd = [
            sys.executable,
            "-m",
            "pproxy",
            "-l",
            f"http://{self.listen_host}:{self.listen_port}",
            "-r",
            upstream,
        ]

        try:
            creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creationflags,
            )
        except Exception as e:
            p(f"❌ Failed to start browser proxy wrapper: {e}")
            p("Install dependency: pip install pproxy")
            self.proc = None
            return False

        for _ in range(50):
            if tcp_port_open(self.listen_host, self.listen_port):
                p(f"🌐 Browser proxy wrapper started on {self.listen_host}:{self.listen_port}")
                return True
            if self.proc and self.proc.poll() is not None:
                break
            time.sleep(0.2)

        p("❌ Browser proxy wrapper did not start in time")
        return False

    def stop(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.proc = None

# ==============================================================================
# TELEGRAM
# ==============================================================================

class TelegramNotifier:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id

    def send(self, text: str) -> None:
        if not self.token or not self.chat_id:
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception:
            pass


# ==============================================================================
# DATABASE
# ==============================================================================

class DatabaseRepo:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=FULL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _columns(self, conn: sqlite3.Connection, table_name: str) -> List[str]:
        rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
        return [str(r["name"]) for r in rows]

    def _ensure_column(self, conn: sqlite3.Connection, table_name: str, column_name: str, ddl: str) -> None:
        cols = self._columns(conn, table_name)
        if column_name not in cols:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {ddl}")

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS offers (
                    offer_id TEXT PRIMARY KEY,
                    short_id INTEGER UNIQUE,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT '',
                    service_id TEXT NOT NULL DEFAULT '',
                    product_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT '',
                    guide_file TEXT NOT NULL DEFAULT '',
                    active INTEGER NOT NULL DEFAULT 1,
                    raw_json TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    offer_id TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    quantity INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'new',
                    delivery_marker TEXT,
                    reserved_payload TEXT,
                    sent_payload TEXT,
                    proof_path TEXT,
                    proof_sha256 TEXT,
                    buyer_notified INTEGER NOT NULL DEFAULT 0,
                    source TEXT NOT NULL DEFAULT '',
                    raw_json TEXT NOT NULL DEFAULT '',
                    error_text TEXT,
                    created_at TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT ''
                )
            """)

            conn.execute("""
                CREATE TABLE IF NOT EXISTS stock_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    offer_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available',
                    reserved_by_order_id TEXT,
                    sold_to_order_id TEXT,
                    imported_from TEXT,
                    created_at TEXT NOT NULL DEFAULT '',
                    reserved_at TEXT,
                    sold_at TEXT,
                    UNIQUE(offer_id, payload)
                )
            """)

            for table, col, ddl in [
                ("offers", "short_id", "short_id INTEGER"),
                ("offers", "category", "category TEXT NOT NULL DEFAULT ''"),
                ("offers", "service_id", "service_id TEXT NOT NULL DEFAULT ''"),
                ("offers", "product_id", "product_id TEXT NOT NULL DEFAULT ''"),
                ("offers", "status", "status TEXT NOT NULL DEFAULT ''"),
                ("offers", "guide_file", "guide_file TEXT NOT NULL DEFAULT ''"),
                ("offers", "active", "active INTEGER NOT NULL DEFAULT 1"),
                ("offers", "raw_json", "raw_json TEXT NOT NULL DEFAULT ''"),
                ("offers", "created_at", "created_at TEXT NOT NULL DEFAULT ''"),
                ("offers", "updated_at", "updated_at TEXT NOT NULL DEFAULT ''"),
                ("orders", "delivery_marker", "delivery_marker TEXT"),
                ("orders", "reserved_payload", "reserved_payload TEXT"),
                ("orders", "sent_payload", "sent_payload TEXT"),
                ("orders", "proof_path", "proof_path TEXT"),
                ("orders", "proof_sha256", "proof_sha256 TEXT"),
                ("orders", "buyer_notified", "buyer_notified INTEGER NOT NULL DEFAULT 0"),
                ("orders", "source", "source TEXT NOT NULL DEFAULT ''"),
                ("orders", "raw_json", "raw_json TEXT NOT NULL DEFAULT ''"),
                ("orders", "error_text", "error_text TEXT"),
                ("orders", "created_at", "created_at TEXT NOT NULL DEFAULT ''"),
                ("orders", "updated_at", "updated_at TEXT NOT NULL DEFAULT ''"),
                ("stock_items", "reserved_by_order_id", "reserved_by_order_id TEXT"),
                ("stock_items", "sold_to_order_id", "sold_to_order_id TEXT"),
                ("stock_items", "imported_from", "imported_from TEXT"),
                ("stock_items", "created_at", "created_at TEXT NOT NULL DEFAULT ''"),
                ("stock_items", "reserved_at", "reserved_at TEXT"),
                ("stock_items", "sold_at", "sold_at TEXT"),
            ]:
                self._ensure_column(conn, table, col, ddl)

            conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_short_id ON offers(short_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_offers_active ON offers(active)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_offer_id ON orders(offer_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_offer_status ON stock_items(offer_id, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_reserved_order ON stock_items(reserved_by_order_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_stock_sold_order ON stock_items(sold_to_order_id)")

            self._backfill_short_ids(conn)

    def _backfill_short_ids(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("SELECT offer_id, short_id, guide_file FROM offers ORDER BY offer_id").fetchall()
        used = {int(r["short_id"]) for r in rows if r["short_id"] is not None}
        next_id = max(used) + 1 if used else 1

        for r in rows:
            offer_id = str(r["offer_id"])
            short_id = r["short_id"]
            if short_id is None:
                while next_id in used:
                    next_id += 1
                conn.execute("UPDATE offers SET short_id = ?, updated_at = ? WHERE offer_id = ?", (next_id, utc_ts(), offer_id))
                short_id = next_id
                used.add(next_id)
                next_id += 1

            if not (r["guide_file"] or "").strip():
                conn.execute(
                    "UPDATE offers SET guide_file = ?, updated_at = ? WHERE offer_id = ?",
                    (f"guide_{int(short_id)}.txt", utc_ts(), offer_id),
                )

    def get_offer(self, offer_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM offers WHERE offer_id = ?", (offer_id,)).fetchone()

    def get_offer_by_short_id(self, short_id: int) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM offers WHERE short_id = ?", (short_id,)).fetchone()

    def list_active_offers(self) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM offers WHERE active = 1 ORDER BY short_id").fetchall()

    def list_offers(self) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM offers ORDER BY short_id").fetchall()

    def mark_all_offers_inactive(self) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE offers SET active = 0, updated_at = ?", (utc_ts(),))

    def upsert_offer(
        self,
        offer_id: str,
        title: str,
        category: str,
        service_id: str = "",
        product_id: str = "",
        status: str = "",
        active: int = 1,
        raw: Optional[dict] = None,
    ) -> sqlite3.Row:
        now = utc_ts()
        raw_json = json.dumps(raw or {}, ensure_ascii=False)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM offers WHERE offer_id = ?", (offer_id,)).fetchone()
            if row:
                guide_file = (row["guide_file"] or "").strip() or f"guide_{int(row['short_id'])}.txt"
                conn.execute("""
                    UPDATE offers
                    SET title = ?, category = ?, service_id = ?, product_id = ?, status = ?,
                        guide_file = ?, active = ?, raw_json = ?, updated_at = ?
                    WHERE offer_id = ?
                """, (title, category, service_id, product_id, status, guide_file, active, raw_json, now, offer_id))
            else:
                cur = conn.execute("SELECT COALESCE(MAX(short_id), 0) + 1 AS next_short_id FROM offers")
                next_short_id = int(cur.fetchone()["next_short_id"])
                guide_file = f"guide_{next_short_id}.txt"
                conn.execute("""
                    INSERT INTO offers (
                        offer_id, short_id, title, category, service_id, product_id, status,
                        guide_file, active, raw_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (offer_id, next_short_id, title, category, service_id, product_id, status,
                      guide_file, active, raw_json, now, now))

            return conn.execute("SELECT * FROM offers WHERE offer_id = ?", (offer_id,)).fetchone()

    def get_order(self, order_id: str) -> Optional[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,)).fetchone()

    def ensure_order(
        self,
        order_id: str,
        offer_id: str,
        title: str,
        category: str,
        quantity: int,
        source: str = "",
        raw: Optional[dict] = None,
    ) -> None:
        now = utc_ts()
        raw_json = json.dumps(raw or {}, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO orders (
                    order_id, offer_id, title, category, quantity, status,
                    source, raw_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)
                ON CONFLICT(order_id) DO UPDATE SET
                    offer_id = excluded.offer_id,
                    title = excluded.title,
                    category = excluded.category,
                    quantity = excluded.quantity,
                    source = CASE WHEN excluded.source != '' THEN excluded.source ELSE source END,
                    raw_json = CASE WHEN excluded.raw_json != '{}' THEN excluded.raw_json ELSE raw_json END,
                    updated_at = excluded.updated_at
            """, (order_id, offer_id, title, category, quantity, source, raw_json, now, now))

    def update_order_fields(self, order_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_ts()
        columns = ", ".join(f"{k} = ?" for k in fields.keys())
        values = list(fields.values()) + [order_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE orders SET {columns} WHERE order_id = ?", values)

    def import_stock_lines(self, offer_id: str, lines: List[str], source_file: str) -> Tuple[int, int]:
        if not lines:
            return 0, 0

        now = utc_ts()
        added = 0
        skipped = 0
        with self._connect() as conn:
            for payload in lines:
                cur = conn.execute("""
                    INSERT OR IGNORE INTO stock_items (
                        offer_id, payload, status, reserved_by_order_id, sold_to_order_id,
                        imported_from, created_at, reserved_at, sold_at
                    )
                    VALUES (?, ?, 'available', NULL, NULL, ?, ?, NULL, NULL)
                """, (offer_id, payload, source_file, now))
                if cur.rowcount == 1:
                    added += 1
                else:
                    skipped += 1
        return added, skipped

    def count_available_stock(self, offer_id: str) -> int:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT COUNT(*) AS c FROM stock_items
                WHERE offer_id = ? AND status = 'available'
            """, (offer_id,)).fetchone()
            return int(row["c"]) if row else 0

    def get_stock_summary_joined(self) -> List[sqlite3.Row]:
        with self._connect() as conn:
            return conn.execute("""
                SELECT
                    o.short_id,
                    o.offer_id,
                    o.title,
                    o.active,
                    o.status AS offer_status,
                    SUM(CASE WHEN s.status = 'available' THEN 1 ELSE 0 END) AS available_count,
                    SUM(CASE WHEN s.status = 'reserved' THEN 1 ELSE 0 END) AS reserved_count,
                    SUM(CASE WHEN s.status = 'sold' THEN 1 ELSE 0 END) AS sold_count
                FROM offers o
                LEFT JOIN stock_items s ON s.offer_id = o.offer_id
                GROUP BY o.offer_id, o.short_id, o.title, o.active, o.status
                ORDER BY o.short_id
            """).fetchall()

    def reserve_stock_rows(self, order_id: str, offer_id: str, qty: int) -> List[str]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("""
                SELECT id, payload
                FROM stock_items
                WHERE offer_id = ? AND status = 'available'
                ORDER BY id
                LIMIT ?
            """, (offer_id, qty)).fetchall()

            if len(rows) < qty:
                conn.execute("ROLLBACK")
                raise RuntimeError(f"Not enough stock. Needed={qty}, available={len(rows)}")

            ids = [int(r["id"]) for r in rows]
            payloads = [str(r["payload"]) for r in rows]
            placeholders = ",".join(["?"] * len(ids))
            conn.execute(f"""
                UPDATE stock_items
                SET status = 'reserved',
                    reserved_by_order_id = ?,
                    reserved_at = ?
                WHERE id IN ({placeholders})
            """, [order_id, utc_ts(), *ids])
            conn.execute("COMMIT")
            return payloads

    def get_reserved_stock_payloads(self, order_id: str) -> List[str]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT payload
                FROM stock_items
                WHERE reserved_by_order_id = ? AND status = 'reserved'
                ORDER BY id
            """, (order_id,)).fetchall()
            return [str(r["payload"]) for r in rows]

    def get_reserved_payload_snapshot(self, order_id: str) -> List[str]:
        row = self.get_order(order_id)
        if not row or not row["reserved_payload"]:
            return []
        try:
            data = json.loads(row["reserved_payload"])
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def release_reserved_stock(self, order_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("""
                UPDATE stock_items
                SET status = 'available',
                    reserved_by_order_id = NULL,
                    reserved_at = NULL
                WHERE reserved_by_order_id = ? AND status = 'reserved'
            """, (order_id,))
            return cur.rowcount or 0

    def finalize_reserved_stock(self, order_id: str) -> int:
        with self._connect() as conn:
            cur = conn.execute("""
                UPDATE stock_items
                SET status = 'sold',
                    sold_to_order_id = ?,
                    sold_at = ?,
                    reserved_by_order_id = NULL
                WHERE reserved_by_order_id = ? AND status = 'reserved'
            """, (order_id, utc_ts(), order_id))
            return cur.rowcount or 0

    def reconcile_reserved_rows(self) -> Dict[str, int]:
        released = 0
        kept = 0
        finalized = 0

        with self._connect() as conn:
            rows = conn.execute("""
                SELECT DISTINCT reserved_by_order_id AS order_id
                FROM stock_items
                WHERE status = 'reserved' AND reserved_by_order_id IS NOT NULL
            """).fetchall()

        for r in rows:
            order_id = str(r["order_id"])
            order = self.get_order(order_id)
            if not order:
                released += self.release_reserved_stock(order_id)
                continue

            status = (order["status"] or "").lower()
            sent_payload = (order["sent_payload"] or "").strip()

            if status in {"delivered", "sent"}:
                finalized += self.finalize_reserved_stock(order_id)
            elif status in {"reserved", "sending", "manual_ready", "manual_review"}:
                kept += 1
            elif status in {"failed", "new"} and not sent_payload:
                released += self.release_reserved_stock(order_id)
            else:
                kept += 1

        return {"released": released, "kept": kept, "finalized": finalized}


# ==============================================================================
# STOCK IMPORTER
# ==============================================================================

class StockImporter:
    def __init__(self, repo: DatabaseRepo, notifier: TelegramNotifier):
        self.repo = repo
        self.notify = notifier

    def _extract_offer_id_from_filename(self, filename: str) -> Optional[str]:
        lower = filename.lower().strip()

        m = re.match(r"^(?:stock_)?(\d+)\.txt$", lower)
        if m:
            row = self.repo.get_offer_by_short_id(int(m.group(1)))
            return str(row["offer_id"]) if row else None

        m = re.match(r"^(?:offer_)?([a-z0-9\-]+)\.txt$", lower, flags=re.IGNORECASE)
        if m:
            return m.group(1).upper() if m.group(1).lower().startswith("g") else m.group(1)

        return None

    def import_once(self) -> Dict[str, int]:
        files = sorted(
            f for f in os.listdir(CONFIG["IMPORT_DIR"])
            if os.path.isfile(os.path.join(CONFIG["IMPORT_DIR"], f)) and f.lower().endswith(".txt")
        )

        result = {"files_seen": 0, "files_done": 0, "files_failed": 0, "lines_added": 0, "lines_skipped": 0}

        for filename in files:
            result["files_seen"] += 1
            src_path = os.path.join(CONFIG["IMPORT_DIR"], filename)
            safe_name = safe_filename(filename)
            offer_id = self._extract_offer_id_from_filename(filename)

            if not offer_id:
                dst = os.path.join(CONFIG["IMPORT_FAILED_DIR"], f"{int(time.time())}__{safe_name}")
                shutil.move(src_path, dst)
                result["files_failed"] += 1
                p(f"⚠️ Import failed: invalid filename {filename}")
                continue

            offer = self.repo.get_offer(offer_id)
            if not offer:
                dst = os.path.join(CONFIG["IMPORT_FAILED_DIR"], f"{int(time.time())}__{safe_name}")
                shutil.move(src_path, dst)
                result["files_failed"] += 1
                p(f"⚠️ Import failed: unknown offer_id={offer_id} for {filename}. Run sync-offers first.")
                continue

            try:
                with open(src_path, "r", encoding="utf-8") as f:
                    raw = f.read()

                lines = normalize_nonempty_lines(raw)
                added, skipped = self.repo.import_stock_lines(offer_id, lines, filename)

                dst_name = f"{int(time.time())}__added_{added}__skipped_{skipped}__{safe_name}"
                shutil.move(src_path, os.path.join(CONFIG["IMPORT_DONE_DIR"], dst_name))

                result["files_done"] += 1
                result["lines_added"] += added
                result["lines_skipped"] += skipped

                available = self.repo.count_available_stock(offer_id)
                p(f"📥 Import done {filename} -> added={added} skipped={skipped} available={available}")
                self.notify.send(
                    f"📥 <b>G2G import done</b>\n"
                    f"Item: [{offer['short_id']}] {escape_for_log(offer['title'])}\n"
                    f"Added: <code>{added}</code>\n"
                    f"Skipped: <code>{skipped}</code>\n"
                    f"Available: <code>{available}</code>"
                )
            except Exception as e:
                dst = os.path.join(CONFIG["IMPORT_FAILED_DIR"], f"{int(time.time())}__{safe_name}")
                try:
                    shutil.move(src_path, dst)
                except Exception:
                    pass
                result["files_failed"] += 1
                p(f"⚠️ Import file error {filename}: {e}")

        return result


# ==============================================================================
# G2G API
# ==============================================================================

class G2GAPI:
    def __init__(self):
        self.base_url = CONFIG["G2G_BASE_URL"]
        self.proxies = build_api_proxies_dict()

    def _require_env(self) -> None:
        missing = []
        for key in ["G2G_API_KEY", "G2G_SECRET_KEY", "G2G_USER_ID"]:
            if not CONFIG[key]:
                missing.append(key)
        if missing:
            raise RuntimeError("Missing .env values: " + ", ".join(missing))

    def _signature(self, endpoint: str, timestamp: str) -> str:
        canonical_string = endpoint + CONFIG["G2G_API_KEY"] + CONFIG["G2G_USER_ID"] + timestamp
        return hmac.new(
            key=CONFIG["G2G_SECRET_KEY"].encode("utf-8"),
            msg=canonical_string.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

    def _headers(self, endpoint: str) -> Dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        return {
            "Content-Type": "application/json",
            "g2g-api-key": CONFIG["G2G_API_KEY"],
            "g2g-userid": CONFIG["G2G_USER_ID"],
            "g2g-timestamp": timestamp,
            "g2g-signature": self._signature(endpoint, timestamp),
        }

    def request(self, method: str, endpoint: str, json_body: Optional[Dict[str, Any]] = None, retries: int = 2) -> Optional[requests.Response]:
        self._require_env()
        if not endpoint.startswith("/"):
            raise ValueError("endpoint must start with '/', example: /v2/services")

        url = self.base_url + endpoint
        last_exc = None

        for attempt in range(retries + 1):
            try:
                response = requests.request(
                    method=method.upper(),
                    url=url,
                    headers=self._headers(endpoint),
                    json=json_body,
                    timeout=30,
                    proxies=self.proxies,
                )
                if response.status_code == 429 and attempt < retries:
                    time.sleep(5 + attempt * 5)
                    continue
                return response
            except Exception as e:
                last_exc = e
                if attempt < retries:
                    time.sleep(2 + attempt * 2)

        p(f"⚠️ G2G API request failed {method} {endpoint}: {last_exc}")
        return None

    def _json(self, response: Optional[requests.Response]) -> Dict[str, Any]:
        if not response:
            return {}
        try:
            return response.json()
        except Exception:
            return {"_status_code": response.status_code, "_text": response.text}

    def get_services(self) -> Dict[str, Any]:
        return self._json(self.request("GET", "/v2/services"))

    def search_offers(self, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self._json(self.request("POST", "/v2/offers/search", body or {}))

    def get_offer(self, offer_id: str) -> Dict[str, Any]:
        return self._json(self.request("GET", f"/v2/offers/{offer_id}"))

    def get_product_attributes(self, product_id: str) -> Dict[str, Any]:
        return self._json(self.request("GET", f"/v2/products/{product_id}/attributes"))

    def get_order(self, order_id: str) -> Dict[str, Any]:
        return self._json(self.request("GET", f"/v2/orders/{order_id}"))

    def get_deliveries(self, order_id: str) -> Dict[str, Any]:
        return self._json(self.request("GET", f"/v2/orders/{order_id}/delivery"))


# ==============================================================================
# BROWSER CHAT
# ==============================================================================

class G2GBrowserChat:
    def __init__(self, proxy_wrapper: ProxyWrapper):
        self.page = None
        self.proxy_wrapper = proxy_wrapper

    def _ensure_available(self) -> None:
        if ChromiumPage is None or ChromiumOptions is None:
            raise RuntimeError("DrissionPage is not installed. Install it with: pip install DrissionPage")

    def _page_alive(self) -> bool:
        if self.page is None:
            return False
        try:
            _ = self.page.url
            return True
        except Exception:
            return False

    def start(self, force_restart: bool = False) -> None:
        self._ensure_available()
        if not self.proxy_wrapper.start():
            raise RuntimeError("Browser proxy wrapper failed to start")

        if force_restart:
            self.quit()

        if self._page_alive():
            return

        self.quit()

        co = ChromiumOptions()
        co.set_user_data_path(CONFIG["BROWSER_PROFILE_DIR"])
        co.set_argument("--start-maximized")
        co.set_argument("--disable-infobars")
        co.set_argument("--no-first-run")

        if CONFIG["CUSTOM_USER_AGENT"]:
            co.set_user_agent(CONFIG["CUSTOM_USER_AGENT"])

        browser_proxy = self.proxy_wrapper.browser_proxy_arg()
        if browser_proxy:
            co.set_argument(f"--proxy-server={browser_proxy}")
            p(f"🌐 Browser proxy enabled: {browser_proxy}")

        self.page = ChromiumPage(co)

    def quit(self) -> None:
        if self.page:
            try:
                self.page.quit()
            except Exception:
                pass
        self.page = None

    def order_url(self, order_id: str) -> str:
        template = CONFIG["G2G_ORDER_URL_TEMPLATE"]
        if "{order_id}" in template:
            return template.format(order_id=order_id)
        return template

    def open_order(self, order_id: str, force: bool = False) -> None:
        self.start(force_restart=force)
        target = self.order_url(order_id)
        if force or not self.page.url or self.page.url != target:
            self.page.get(target)

    def open_login_check(self) -> None:
        self.start()
        self.page.get(CONFIG["G2G_WEB_BASE"])

    def _tab_objects(self) -> List[Any]:
        tabs = []
        if self.page is None:
            return tabs
        tabs.append(self.page)
        try:
            ids = getattr(self.page, "tab_ids", None)
            if ids:
                for tid in ids:
                    try:
                        t = self.page.get_tab(tid)
                        if t is not None and t not in tabs:
                            tabs.append(t)
                    except Exception:
                        continue
        except Exception:
            pass
        try:
            latest = getattr(self.page, "latest_tab", None)
            if latest is not None and latest not in tabs:
                tabs.append(latest)
        except Exception:
            pass
        return tabs

    def _contexts(self) -> List[Any]:
        contexts = []
        if self.page is None:
            return contexts

        for tab in self._tab_objects():
            contexts.append(tab)
            try:
                frames = tab.eles("tag:iframe")
            except Exception:
                frames = []
            for ifr in frames or []:
                try:
                    fr = getattr(ifr, "frame", None) or ifr.frame
                    if fr:
                        contexts.append(fr)
                except Exception:
                    continue

        return contexts

    def _ele_any(self, ctx: Any, selectors: List[str]):
        for sel in selectors:
            try:
                ele = ctx.ele(sel)
                if ele:
                    return ele
            except Exception:
                continue
        return None

    def _text_from_ctx(self, ctx: Any) -> str:
        texts: List[str] = []
        for sel in CONFIG["MESSAGE_TEXT_SELECTORS"]:
            try:
                elements = ctx.eles(sel)
                for el in elements[-100:]:
                    try:
                        txt = (el.text or "").strip()
                        if txt:
                            texts.append(txt)
                    except Exception:
                        continue
                if texts:
                    break
            except Exception:
                continue

        if not texts:
            try:
                txt = (ctx.text or "").strip()
                if txt:
                    texts.append(txt)
            except Exception:
                pass

        return "\n".join(texts)

    def _find_chat_context(self, timeout: int):
        end_at = time.time() + timeout
        editor_js = (
            "return !!document.querySelector("
            "'div.ProseMirror[contenteditable=\\'true\\'], "
            ".toastui-editor-contents[contenteditable=\\'true\\'], "
            "div[contenteditable=\\'true\\'], textarea, [role=textbox]'"
            ");"
        )
        diagnosed = False
        while time.time() < end_at:
            ctxs = self._contexts()
            for ctx in ctxs:
                if self._ele_any(ctx, CONFIG["CHAT_EDITOR_SELECTORS"]):
                    return ctx
                try:
                    if ctx.run_js(editor_js):
                        return ctx
                except Exception:
                    pass
            if not diagnosed:
                diagnosed = True
                try:
                    print("[chat-debug] scanning", len(ctxs), "context(s) for editor...")
                    for idx, ctx in enumerate(ctxs):
                        url = ""
                        has = None
                        try: url = getattr(ctx, "url", "") or ""
                        except Exception: pass
                        try: has = ctx.run_js(editor_js)
                        except Exception as ex: has = "err:" + str(ex)[:60]
                        print("[chat-debug]   ctx#%d url=%s editor=%s" % (idx, url[:80], has))
                except Exception:
                    pass
            time.sleep(0.8)
        return None
    def _chat_contains(self, ctx: Any, needle: str) -> bool:
        return needle in self._text_from_ctx(ctx)

    def _wait_chat_contains(self, ctx: Any, needle: str, timeout: int) -> bool:
        end_at = time.time() + timeout
        while time.time() < end_at:
            if self._chat_contains(ctx, needle):
                return True
            time.sleep(1.0)
        return False

    def _type_shift_enter_break(self, ctx: Any) -> bool:
        try:
            ctx.actions.key_down("Shift").key_down("Enter").key_up("Enter").key_up("Shift")
            time.sleep(0.1)
            return True
        except Exception:
            return False

    def _send_one_message(self, ctx: Any, text: str) -> bool:
        editor_q = (
            "div.ProseMirror[contenteditable='true'], "
            ".toastui-editor-ww-container [contenteditable='true'], "
            ".toastui-editor-contents[contenteditable='true'], "
            "[id^='editor-g2g_dm_'] [contenteditable='true'], "
            "div[contenteditable='true'], textarea, [role='textbox']"
        )

        rect = None
        try:
            rect = ctx.run_js(r"""
            const cands = Array.from(document.querySelectorAll(
              "div.ProseMirror[contenteditable='true'], .toastui-editor-ww-container [contenteditable='true'], [id^='editor-g2g_dm_'] [contenteditable='true'], div[contenteditable='true'], textarea, [role='textbox']"
            ));
            for (const el of cands) {
              const r = el.getBoundingClientRect();
              const st = getComputedStyle(el);
              if (r.width > 20 && r.height > 10 && st.visibility !== 'hidden' && st.display !== 'none') {
                el.setAttribute('data-bot-editor','1');
                return {x: r.left + r.width/2, y: r.top + Math.min(r.height/2, 20), w: r.width, h: r.height};
              }
            }
            return null;
            """)
        except Exception as ex:
            print("[chat-debug] rect js failed:", str(ex)[:100])
        print("[chat-debug] visible editor rect:", rect)

        if rect and rect.get("x"):
            cx, cy = float(rect["x"]), float(rect["y"])
            try:
                ctx.run_cdp("Input.dispatchMouseEvent", type="mousePressed", x=cx, y=cy, button="left", clickCount=1, buttons=1)
                ctx.run_cdp("Input.dispatchMouseEvent", type="mouseReleased", x=cx, y=cy, button="left", clickCount=1, buttons=1)
                time.sleep(0.3)
            except Exception as ex:
                print("[chat-debug] cdp mouse click failed:", str(ex)[:100])

        try:
            ctx.run_js(r"""
            const el = document.querySelector("[data-bot-editor='1']") || document.querySelector(arguments[0]);
            if (el) {
              el.focus();
              const sel = window.getSelection();
              const range = document.createRange();
              range.selectNodeContents(el);
              sel.removeAllRanges(); sel.addRange(range);
              try { document.execCommand('delete', false, null); } catch(e){}
            }
            """, editor_q)
        except Exception:
            pass
        time.sleep(0.2)

        inserted_ok = False
        try:
            ctx.run_cdp("Input.insertText", text=text)
            inserted_ok = True
        except Exception as ex:
            print("[chat-debug] CDP insertText failed:", str(ex)[:100])

        time.sleep(0.5)

        editor_now = None
        try:
            editor_now = ctx.run_js(r"""
            const el = document.querySelector("[data-bot-editor='1']") || document.querySelector(arguments[0]);
            return el ? (el.innerText||el.value||'').trim() : 'NO_EDITOR';
            """, editor_q)
        except Exception:
            editor_now = None
        snippet = (text or "")[:20]
        contains = bool(editor_now and snippet and snippet in str(editor_now))
        print("[chat-debug] insert(cdp=%s) -> editor_now=%r contains_text=%s" % (inserted_ok, str(editor_now)[:60], contains))
        if not contains:
            print("[chat-debug] insertion failed; refusing to send")
            return False

        time.sleep(0.3)

        sent = False
        try:
            sent = bool(ctx.run_js(r"""
            const sels = [".g-send-btn button","[id^='g-drag-area_g2g_dm_'] .g-send-btn button",
              "button[type='submit']","button[aria-label*='end']","button[aria-label*='Send']",
              ".g-send-btn","[class*='send'] button","form button"];
            for (const s of sels) {
              const el = document.querySelector(s);
              if (el) {
                const b = el.closest('button') || el;
                if (b.disabled) continue;
                b.scrollIntoView({block:'center', inline:'center'});
                b.click();
                return true;
              }
            }
            return false;
            """))
        except Exception as ex:
            print("[chat-debug] send-click exception:", str(ex)[:100])
            sent = False

        time.sleep(1.3)
        try:
            leftover = ctx.run_js(r"""
            const el = document.querySelector("[data-bot-editor='1']") || document.querySelector(arguments[0]);
            return el ? (el.innerText||el.value||'').trim() : 'NO_EDITOR';
            """, editor_q)
            print("[chat-debug] sent=%s editor_after=%r" % (sent, str(leftover)[:40]))
            if leftover == "" or leftover is None:
                return True
        except Exception:
            pass
        return bool(sent)
    def _send_messages(self, ctx: Any, messages: List[str]) -> bool:
        for msg in messages:
            if not (msg or "").strip():
                continue
            if not self._send_one_message(ctx, msg):
                return False
        return True

    def _click_delivered(self) -> bool:
        if not CONFIG["CLICK_DELIVERED_AFTER_SEND"]:
            return True

        found = False
        for sel in CONFIG["DELIVER_BUTTON_SELECTORS"]:
            try:
                btn = self.page.ele(sel)
                if btn:
                    btn.click()
                    found = True
                    time.sleep(1.5)
                    break
            except Exception:
                continue

        if not found:
            return False

        for sel in CONFIG["CONFIRM_BUTTON_SELECTORS"]:
            try:
                btn = self.page.ele(sel)
                if btn:
                    btn.click()
                    time.sleep(2.0)
                    return True
            except Exception:
                continue

        return True

    def send_test_message(self, order_id: str, message: str) -> Dict[str, Any]:
        result = {"sent": False, "visible": False, "error": ""}
        for attempt in range(2):
            try:
                self.open_order(order_id, force=(attempt > 0))
                ctx = self._find_chat_context(CONFIG["CHAT_READY_TIMEOUT"])
                if not ctx:
                    result["error"] = "Chat editor not found"
                    return result

                if not self._send_messages(ctx, [message]):
                    result["error"] = "Failed to send message"
                    return result

                result["sent"] = True
                result["visible"] = self._wait_chat_contains(ctx, message.splitlines()[0], CONFIG["CHAT_VERIFY_TIMEOUT"])
                return result
            except Exception as e:
                if attempt == 0 and is_disconnect_error(str(e)):
                    self.quit()
                    time.sleep(1.0)
                    continue
                result["error"] = str(e)
                return result
        result["error"] = "Browser retry failed"
        return result

    def deliver(
        self,
        order_id: str,
        stock_message: str,
        guide_messages: List[str],
        marker: str,
        proof_path: str,
    ) -> Dict[str, Any]:
        result = {
            "already_sent": False,
            "stock_send_attempted": False,
            "stock_visible": False,
            "guide_visible": False,
            "delivered_clicked": False,
            "proof_path": "",
            "error": "",
        }

        for attempt in range(2):
            try:
                self.open_order(order_id, force=(attempt > 0))
                ctx = self._find_chat_context(CONFIG["CHAT_READY_TIMEOUT"])
                if not ctx:
                    result["error"] = "Chat editor not found"
                    return result

                current_text = self._text_from_ctx(ctx)
                if marker and marker in current_text:
                    result["already_sent"] = True
                    result["stock_visible"] = True
                else:
                    if not self._send_messages(ctx, [stock_message]):
                        result["error"] = "Failed to send stock message"
                        return result
                    result["stock_send_attempted"] = True

                    result["stock_visible"] = self._wait_chat_contains(ctx, marker, CONFIG["CHAT_VERIFY_TIMEOUT"])
                    if not result["stock_visible"]:
                        self.open_order(order_id, force=True)
                        ctx = self._find_chat_context(CONFIG["CHAT_READY_TIMEOUT"])
                        if ctx:
                            result["stock_visible"] = self._wait_chat_contains(ctx, marker, 8)

                    if not result["stock_visible"]:
                        result["error"] = "Stock marker not visible after send"
                        return result

                if guide_messages:
                    first_probe = ""
                    for line in guide_messages[0].splitlines():
                        if line.strip():
                            first_probe = line.strip()[:80]
                            break

                    current_text = self._text_from_ctx(ctx)
                    if first_probe and first_probe in current_text:
                        result["guide_visible"] = True
                    else:
                        if not self._send_messages(ctx, guide_messages):
                            result["error"] = "Failed to send guide message"
                            return result
                        if first_probe:
                            result["guide_visible"] = self._wait_chat_contains(ctx, first_probe, CONFIG["CHAT_VERIFY_TIMEOUT"])
                        else:
                            result["guide_visible"] = True

                    if not result["guide_visible"]:
                        result["error"] = "Guide message not visible after send"
                        return result
                else:
                    result["guide_visible"] = True

                result["delivered_clicked"] = self._click_delivered()
                if CONFIG["CLICK_DELIVERED_AFTER_SEND"] and not result["delivered_clicked"]:
                    result["error"] = "Could not click Delivered"
                    return result

                try:
                    self.page.get_screenshot(path=proof_path)
                    result["proof_path"] = proof_path
                except Exception:
                    result["proof_path"] = ""

                return result

            except Exception as e:
                if attempt == 0 and is_disconnect_error(str(e)):
                    self.quit()
                    time.sleep(1.0)
                    continue
                result["error"] = str(e)
                return result

        result["error"] = "Browser/page disconnected and retry failed"
        return result


# ==============================================================================
# CORE BOT
# ==============================================================================

class G2GBot:
    def __init__(self):
        self.repo = DatabaseRepo(os.path.join(CONFIG["DATA_DIR"], "app.db"))
        self.notify = TelegramNotifier(CONFIG["TG_NOTIFY_TOKEN"], CONFIG["TG_NOTIFY_CHAT"])
        self.api = G2GAPI()
        self.proxy_wrapper = ProxyWrapper()
        self.browser = G2GBrowserChat(self.proxy_wrapper)
        self.importer = StockImporter(self.repo, self.notify)
        self.loop_counter = 0
        self.warned_offers = set()
        self.queue_path = os.path.join(CONFIG["DATA_DIR"], "order_queue.txt")
        self.queue_lock = threading.Lock()

    # --------------------------------------------------------------------------
    # Files / catalog / guides
    # --------------------------------------------------------------------------

    def ensure_guide_file(self, short_id: int) -> str:
        guide_file = f"guide_{short_id}.txt"
        guide_path = os.path.join(CONFIG["GUIDES_DIR"], guide_file)
        if not os.path.exists(guide_path):
            atomic_write_text(guide_path, DEFAULT_GUIDE_CONTENT.strip() + "\n")
        return guide_file

    def read_guide_lines(self, offer_id: str) -> List[str]:
        offer = self.repo.get_offer(offer_id)
        if not offer:
            return []
        guide_file = (offer["guide_file"] or "").strip()
        if not guide_file:
            return []
        path = os.path.join(CONFIG["GUIDES_DIR"], guide_file)
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return [line.rstrip() for line in f if line.strip()]

    def generate_catalog(self) -> None:
        rows = self.repo.get_stock_summary_joined()
        lines = [
            "=== G2G CATALOG ===",
            f"Generated at UTC: {utc_ts()}",
            "Import examples:",
            "  import_keys_g2g/1.txt          -> imports stock into offer with short_id=1",
            "  import_keys_g2g/stock_1.txt    -> same",
            "  import_keys_g2g/G177...GY.txt  -> imports stock into exact offer_id",
            "",
        ]

        for r in rows:
            active = "ACTIVE" if int(r["active"] or 0) == 1 else "INACTIVE"
            lines.append(
                f"[{r['short_id']}] {r['title']} | "
                f"offer_id={r['offer_id']} | offer_status={r['offer_status']} | {active} | "
                f"available={r['available_count'] or 0} | "
                f"reserved={r['reserved_count'] or 0} | sold={r['sold_count'] or 0}"
            )

        atomic_write_text(os.path.join(BASE_DIR, "CATALOG.txt"), "\n".join(lines).strip() + "\n")
        p("📄 CATALOG.txt updated")

    # --------------------------------------------------------------------------
    # API sync / diagnostics
    # --------------------------------------------------------------------------

    def sync_offers(self) -> None:
        p("🔄 Sync G2G offers via /v2/offers/search ...")
        data = self.api.search_offers({})
        results = (((data or {}).get("payload") or {}).get("results") or [])

        self.repo.mark_all_offers_inactive()

        count = 0
        for item in results:
            offer_id = str(item.get("offer_id") or "").strip()
            if not offer_id:
                continue

            title = str(item.get("title") or "Unknown").strip()
            service_id = str(item.get("service_id") or "").strip()
            product_id = str(item.get("product_id") or item.get("relation_id") or "").strip()
            status = str(item.get("status") or "").strip().lower()
            category = str(item.get("service_name") or item.get("category") or service_id or "g2g").strip()

            active = 1 if status in {"live", "active", ""} else 0
            row = self.repo.upsert_offer(
                offer_id=offer_id,
                title=title,
                category=category,
                service_id=service_id,
                product_id=product_id,
                status=status,
                active=active,
                raw=item,
            )
            self.ensure_guide_file(int(row["short_id"]))
            count += 1

        p(f"✅ synced offers: {count}")
        self.generate_catalog()

    def api_services(self) -> None:
        print(json.dumps(self.api.get_services(), indent=2, ensure_ascii=False))

    def api_offer(self, offer_id: str) -> None:
        print(json.dumps(self.api.get_offer(offer_id), indent=2, ensure_ascii=False))

    def api_order(self, order_id: str) -> None:
        print(json.dumps(self.api.get_order(order_id), indent=2, ensure_ascii=False))

    def api_deliveries(self, order_id: str) -> None:
        print(json.dumps(self.api.get_deliveries(order_id), indent=2, ensure_ascii=False))

    # --------------------------------------------------------------------------
    # Stock / status
    # --------------------------------------------------------------------------

    def run_import_once(self) -> None:
        stats = self.importer.import_once()
        if stats["files_seen"] > 0:
            p(
                f"📥 Import: files {stats['files_done']}/{stats['files_seen']}, "
                f"added {stats['lines_added']}, skipped {stats['lines_skipped']}, failed {stats['files_failed']}"
            )
            self.generate_catalog()

    def run_show_stock(self) -> None:
        rows = self.repo.get_stock_summary_joined()
        if not rows:
            p("Stock is empty. Run sync-offers first, then put stock file into import_keys_g2g/.")
            return

        p("=== G2G STOCK SUMMARY ===")
        for r in rows:
            p(
                f"[{r['short_id']}] {r['title']} | active={int(r['active'] or 0)} | "
                f"offer_status={r['offer_status']} | available={r['available_count'] or 0} | "
                f"reserved={r['reserved_count'] or 0} | sold={r['sold_count'] or 0} | "
                f"offer_id={r['offer_id']}"
            )

    def run_reconcile(self) -> None:
        stats = self.repo.reconcile_reserved_rows()
        p(f"🔁 Reconcile: released={stats['released']} kept={stats['kept']} finalized={stats['finalized']}")

    def check_low_stock(self) -> None:
        threshold = CONFIG["LOW_STOCK_THRESHOLD"]
        if threshold < 0:
            return

        for r in self.repo.get_stock_summary_joined():
            if not int(r["active"] or 0):
                continue
            offer_id = str(r["offer_id"])
            available = int(r["available_count"] or 0)
            if available <= threshold and offer_id not in self.warned_offers:
                self.warned_offers.add(offer_id)
                p(f"⚠️ Low stock: [{r['short_id']}] {r['title']} ({available} left)")
                self.notify.send(
                    f"⚠️ <b>G2G low stock</b>\n"
                    f"ID: <code>{r['short_id']}</code>\n"
                    f"Offer: <code>{offer_id}</code>\n"
                    f"Left: <code>{available}</code>\n"
                    f"Title: {escape_for_log(r['title'])}"
                )
            elif available > threshold and offer_id in self.warned_offers:
                self.warned_offers.remove(offer_id)

    # --------------------------------------------------------------------------
    # Queue
    # --------------------------------------------------------------------------

    def enqueue_order(self, order_id: str, offer_id: str = "", qty: int = 1, title: str = "", category: str = "", source: str = "") -> None:
        item = {
            "order_id": order_id,
            "offer_id": offer_id,
            "qty": int(qty or 1),
            "title": title,
            "category": category,
            "source": source,
            "queued_at": utc_ts(),
        }
        line = json.dumps(item, ensure_ascii=False)
        with self.queue_lock:
            with open(self.queue_path, "a", encoding="utf-8", newline="\n") as f:
                f.write(line + "\n")
        p(f"🧾 queued order: {order_id}")

    def _parse_queue_line(self, line: str) -> Optional[Dict[str, Any]]:
        text = (line or "").strip()
        if not text or text.startswith("#"):
            return None

        try:
            obj = json.loads(text)
            if isinstance(obj, dict) and obj.get("order_id"):
                return obj
        except Exception:
            pass

        parts = [x.strip() for x in text.split("|")]
        if not parts or not parts[0]:
            return None
        return {
            "order_id": parts[0],
            "offer_id": parts[1] if len(parts) > 1 else "",
            "qty": int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1,
            "title": "",
            "category": "",
            "source": "queue",
        }

    def take_queued_orders(self) -> List[Dict[str, Any]]:
        with self.queue_lock:
            if not os.path.exists(self.queue_path):
                return []

            processing_path = os.path.join(CONFIG["DATA_DIR"], f"order_queue.processing.{uuid.uuid4().hex}.txt")
            try:
                os.replace(self.queue_path, processing_path)
            except FileNotFoundError:
                return []

        items: List[Dict[str, Any]] = []
        try:
            with open(processing_path, "r", encoding="utf-8") as f:
                for line in f:
                    obj = self._parse_queue_line(line)
                    if obj:
                        items.append(obj)
        finally:
            done_path = os.path.join(CONFIG["DATA_DIR"], f"order_queue.done.{int(time.time())}.txt")
            try:
                os.replace(processing_path, done_path)
            except Exception:
                pass

        return items

    # --------------------------------------------------------------------------
    # Order preparation
    # --------------------------------------------------------------------------

    def _single_active_offer_id(self) -> Optional[str]:
        rows = self.repo.list_active_offers()
        if len(rows) == 1:
            return str(rows[0]["offer_id"])
        return None

    def _resolve_offer_id(self, offer_id: str = "") -> str:
        offer_id = (offer_id or "").strip()
        if offer_id:
            return offer_id

        if CONFIG["G2G_TEST_OFFER_ID"]:
            return CONFIG["G2G_TEST_OFFER_ID"]

        single = self._single_active_offer_id()
        if single:
            return single

        raise RuntimeError("offer_id not provided. Pass offer_id, set G2G_TEST_OFFER_ID, or keep only one active synced offer.")

    def _order_from_api_if_possible(self, order_id: str) -> Dict[str, Any]:
        try:
            data = self.api.get_order(order_id)
            if (data or {}).get("code") == 20000001:
                payload = (data.get("payload") or {})
                return payload if isinstance(payload, dict) else {}
        except Exception as e:
            p(f"⚠️ get_order failed or unavailable for {order_id}: {e}")
        return {}

    def _extract_order_fields(self, order_id: str, offer_id: str = "", qty: int = 1, title: str = "", category: str = "", source: str = "") -> Dict[str, Any]:
        api_payload = self._order_from_api_if_possible(order_id)

        final_offer_id = (
            str(api_payload.get("offer_id") or "").strip()
            or str(api_payload.get("offer", {}).get("offer_id") if isinstance(api_payload.get("offer"), dict) else "").strip()
            or offer_id
        )
        final_offer_id = self._resolve_offer_id(final_offer_id)

        offer = self.repo.get_offer(final_offer_id)
        final_title = (
            title
            or str(api_payload.get("title") or "").strip()
            or (str(offer["title"]) if offer else "Unknown")
        )
        final_category = (
            category
            or str(api_payload.get("offer_service_type") or "").strip()
            or (str(offer["category"]) if offer else "g2g")
        )

        final_qty = qty or 1
        for candidate in [
            api_payload.get("purchased_qty"),
            api_payload.get("quantity"),
            api_payload.get("order_quantity"),
            qty,
            1,
        ]:
            try:
                n = int(candidate)
                if n > 0:
                    final_qty = n
                    break
            except Exception:
                continue

        return {
            "order_id": order_id,
            "offer_id": final_offer_id,
            "title": final_title,
            "category": final_category,
            "quantity": final_qty,
            "source": source,
            "api_payload": api_payload,
        }

    def build_delivery_text(self, order_id: str, payloads: List[str], guide_messages: List[str]) -> Tuple[str, str]:
        marker = f"Delivery-ID: G2G-{order_id}"
        stock_message = "\n".join([
            marker,
            f"Order: {order_id}",
            "",
            "Hello! Thank you for your purchase.",
            "Here is your account/data:",
            "",
            *payloads,
        ]).strip()

        full_text = stock_message
        if guide_messages:
            full_text += "\n\n--- GUIDE ---\n" + "\n\n".join(guide_messages)

        return marker, full_text

    def prepare_order(self, order_id: str, offer_id: str = "", qty: int = 1, title: str = "", category: str = "", source: str = "manual") -> Dict[str, Any]:
        fields = self._extract_order_fields(order_id, offer_id, qty, title, category, source)
        offer_id = fields["offer_id"]
        qty = int(fields["quantity"])

        self.repo.ensure_order(
            order_id=order_id,
            offer_id=offer_id,
            title=fields["title"],
            category=fields["category"],
            quantity=qty,
            source=source,
            raw=fields.get("api_payload") or {},
        )

        row = self.repo.get_order(order_id)
        status = (row["status"] if row else "new").lower()

        if status in {"delivered", "sent"}:
            return {"ok": True, "already_done": True, "order": row_to_dict(row)}

        reserved_lines = self.repo.get_reserved_stock_payloads(order_id)
        snapshot = self.repo.get_reserved_payload_snapshot(order_id)

        if not reserved_lines and snapshot:
            reserved_lines = snapshot

        if not reserved_lines:
            reserved_lines = self.repo.reserve_stock_rows(order_id, offer_id, qty)
            self.repo.update_order_fields(
                order_id,
                status="reserved",
                reserved_payload=json.dumps(reserved_lines, ensure_ascii=False),
                error_text="",
            )
            p(f"🔒 reserved {len(reserved_lines)} line(s) for order={order_id}")

        guide_lines = self.read_guide_lines(offer_id)
        guide_messages = chunk_message_lines(guide_lines, CONFIG["MAX_MESSAGE_CHARS"])
        marker, full_text = self.build_delivery_text(order_id, reserved_lines, guide_messages)

        self.repo.update_order_fields(
            order_id,
            delivery_marker=marker,
            sent_payload=full_text,
            error_text="",
        )

        return {
            "ok": True,
            "order_id": order_id,
            "offer_id": offer_id,
            "qty": qty,
            "title": fields["title"],
            "marker": marker,
            "stock_lines": reserved_lines,
            "guide_messages": guide_messages,
            "full_text": full_text,
        }

    def print_prepared_message(self, prepared: Dict[str, Any]) -> None:
        p("=" * 80)
        p(f"ORDER: {prepared.get('order_id')}")
        p(f"OFFER: {prepared.get('offer_id')}")
        p(f"QTY: {prepared.get('qty')}")
        p("=" * 80)
        p(prepared.get("full_text", ""))
        p("=" * 80)

    def process_order(self, order_id: str, offer_id: str = "", qty: int = 1, title: str = "", category: str = "", source: str = "manual") -> None:
        try:
            order_id = str(order_id).strip()
            if not order_id:
                return

            existing = self.repo.get_order(order_id)
            if existing and (existing["status"] or "").lower() in {"delivered", "sent", "manual_review", "failed"}:
                p(f"ℹ️ skip order={order_id}, local status={existing['status']}")
                return

            prepared = self.prepare_order(order_id, offer_id or "", qty, title, category, source)
            if prepared.get("already_done"):
                p(f"ℹ️ order={order_id} already done")
                return

            if not CONFIG["BROWSER_SEND_ENABLED"]:
                self.repo.update_order_fields(order_id, status="manual_ready")
                p("🟡 BROWSER_SEND_ENABLED=0. Message prepared, stock is reserved, nothing sent.")
                self.print_prepared_message(prepared)
                self.notify.send(
                    f"🟡 <b>G2G manual delivery ready</b>\n"
                    f"Order: <code>{order_id}</code>\n"
                    f"Offer: <code>{prepared.get('offer_id')}</code>\n"
                    f"Qty: <code>{prepared.get('qty')}</code>\n"
                    f"Status: <code>manual_ready</code>"
                )
                return

            self.repo.update_order_fields(order_id, status="sending", error_text="")
            proof_path = os.path.join(CONFIG["PROOFS_DIR"], f"{safe_filename(order_id)}.png")

            result = self.browser.deliver(
                order_id=order_id,
                stock_message="\n\n".join(prepared["full_text"].split("\n\n--- GUIDE ---\n")[:1]).strip(),
                guide_messages=prepared["guide_messages"],
                marker=prepared["marker"],
                proof_path=proof_path,
            )

            if not result.get("stock_visible"):
                err = result.get("error") or "Stock marker not visible"
                if result.get("stock_send_attempted") or result.get("already_sent"):
                    self.repo.update_order_fields(order_id, status="manual_review", error_text=err)
                    p(f"⚠️ stock may have been sent -> manual_review: {err}")
                    self.notify.send(
                        f"⚠️ <b>G2G manual review</b>\n"
                        f"Order: <code>{order_id}</code>\n"
                        f"Reason: stock may already be in chat\n"
                        f"Error: <code>{escape_for_log(err, 300)}</code>"
                    )
                    return

                released = self.repo.release_reserved_stock(order_id)
                self.repo.update_order_fields(order_id, status="failed", error_text=err, reserved_payload="")
                p(f"❌ delivery failed before confirmed send. Released={released}. Error={err}")
                self.notify.send(
                    f"❌ <b>G2G delivery failed</b>\n"
                    f"Order: <code>{order_id}</code>\n"
                    f"Released: <code>{released}</code>\n"
                    f"Error: <code>{escape_for_log(err, 300)}</code>"
                )
                return

            sold_count = self.repo.finalize_reserved_stock(order_id)
            proof_sha = sha256_file(result.get("proof_path") or "")

            if CONFIG["CLICK_DELIVERED_AFTER_SEND"]:
                if result.get("delivered_clicked"):
                    final_status = "delivered"
                else:
                    final_status = "manual_review"
            else:
                final_status = "sent"

            self.repo.update_order_fields(
                order_id,
                status=final_status,
                proof_path=result.get("proof_path") or "",
                proof_sha256=proof_sha,
                error_text=result.get("error") or "",
            )

            p(f"✅ G2G order processed. order={order_id} status={final_status} sold={sold_count}")
            self.notify.send(
                f"✅ <b>G2G order processed</b>\n"
                f"Order: <code>{order_id}</code>\n"
                f"Status: <code>{final_status}</code>\n"
                f"Sold rows: <code>{sold_count}</code>"
            )
            self.generate_catalog()

        except Exception as e:
            err = str(e)
            p(f"❌ process_order failed for {order_id}: {err}")
            traceback.print_exc()
            try:
                self.repo.ensure_order(order_id, offer_id or self._resolve_offer_id(""), title or "Unknown", category or "g2g", qty or 1, source=source)
                self.repo.update_order_fields(order_id, status="failed", error_text=err)
            except Exception:
                pass
            self.notify.send(
                f"❌ <b>G2G critical order error</b>\n"
                f"Order: <code>{order_id}</code>\n"
                f"Error: <code>{escape_for_log(err, 300)}</code>"
            )
        finally:
            if CONFIG["AUTO_CLOSE_BROWSER_AFTER_ORDER"]:
                try:
                    self.browser.quit()
                except Exception:
                    pass

    def release_order(self, order_id: str) -> None:
        released = self.repo.release_reserved_stock(order_id)
        self.repo.update_order_fields(order_id, status="failed", error_text=f"Released manually at {utc_ts()}")
        p(f"↩️ released reserved stock for order={order_id}: {released}")

    # --------------------------------------------------------------------------
    # Browser helpers
    # --------------------------------------------------------------------------

    def open_browser(self) -> None:
        if not self.proxy_wrapper.start():
            p("🔴 Browser proxy wrapper failed to start")
            return
        self.browser.open_login_check()
        p(f"🌐 Browser opened with profile: {CONFIG['BROWSER_PROFILE_DIR']}")
        p("Log in to G2G manually in this browser profile, then close or keep it open.")

    def test_message(self, order_id: str) -> None:
        if not self.proxy_wrapper.start():
            p("🔴 Browser proxy wrapper failed to start")
            return

        if not CONFIG["BROWSER_SEND_ENABLED"]:
            p("BROWSER_SEND_ENABLED=0. Set BROWSER_SEND_ENABLED=1 in .env to send a test message.")
            p("The browser will not send anything.")
            self.browser.open_order(order_id, force=True)
            return

        marker = f"G2G-BOT-TEST {utc_ts()}"
        result = self.browser.send_test_message(order_id, marker)
        p(json.dumps(result, indent=2, ensure_ascii=False))

    # --------------------------------------------------------------------------
    # Webhook
    # --------------------------------------------------------------------------

    def verify_webhook_signature(self, request_obj) -> bool:
        if not CONFIG["WEBHOOK_VERIFY_SIGNATURE"]:
            return True

        secret = CONFIG["WEBHOOK_SECRET_TOKEN"]
        if not secret:
            p("❌ WEBHOOK_SECRET_TOKEN missing while WEBHOOK_VERIFY_SIGNATURE=1")
            return False

        signature = request_obj.headers.get("g2g-signature", "")
        timestamp = request_obj.headers.get("g2g-timestamp", "")
        if not signature or not timestamp:
            return False

        webhook_url = CONFIG["WEBHOOK_PUBLIC_URL"] or request_obj.base_url
        canonical_string = webhook_url + CONFIG["G2G_USER_ID"] + str(timestamp)
        expected = hmac.new(
            key=secret.encode("utf-8"),
            msg=canonical_string.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected, signature)

    def extract_webhook_order(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        event_type = str(data.get("event_type") or "").strip()

        order_id = (
            str(payload.get("order_id") or "").strip()
            or str(payload.get("order_item_id") or "").strip()
            or str(data.get("order_id") or "").strip()
        )
        if not order_id:
            return None

        offer_id = str(payload.get("offer_id") or "").strip()
        qty = 1
        for candidate in [payload.get("purchased_qty"), payload.get("quantity"), payload.get("qty"), 1]:
            try:
                n = int(candidate)
                if n > 0:
                    qty = n
                    break
            except Exception:
                continue

        return {
            "order_id": order_id,
            "offer_id": offer_id,
            "qty": qty,
            "title": "",
            "category": str(payload.get("offer_service_type") or "g2g").strip(),
            "source": f"webhook:{event_type}",
        }

    def start_webhook_server_in_thread(self) -> None:
        if not CONFIG["WEBHOOK_ENABLED"]:
            return

        try:
            from flask import Flask, jsonify, request
        except ImportError as exc:
            raise RuntimeError("Flask is not installed. Install it with: pip install flask") from exc

        app = Flask(__name__)
        bot_ref = self

        @app.route(CONFIG["WEBHOOK_PATH"], methods=["POST"])
        def g2g_webhook():
            try:
                if not bot_ref.verify_webhook_signature(request):
                    return jsonify({"status": "error", "message": "signature mismatch"}), 403

                data = request.get_json(silent=True) or {}
                event_type = str(data.get("event_type") or "").strip()
                p(f"🔔 webhook received: {event_type}")

                if CONFIG["WEBHOOK_ALLOWED_EVENTS"] and event_type not in CONFIG["WEBHOOK_ALLOWED_EVENTS"]:
                    return jsonify({"status": "ignored", "event_type": event_type}), 200

                item = bot_ref.extract_webhook_order(data)
                if not item:
                    return jsonify({"status": "ignored", "message": "no order id"}), 200

                bot_ref.enqueue_order(**item)
                return jsonify({"status": "queued", "order_id": item["order_id"]}), 200

            except Exception as e:
                p(f"❌ webhook error: {e}")
                traceback.print_exc()
                return jsonify({"status": "error", "message": str(e)}), 500

        def run_app():
            p(f"🌐 Webhook server started on {CONFIG['WEBHOOK_HOST']}:{CONFIG['WEBHOOK_PORT']}{CONFIG['WEBHOOK_PATH']}")
            app.run(
                host=CONFIG["WEBHOOK_HOST"],
                port=CONFIG["WEBHOOK_PORT"],
                debug=False,
                use_reloader=False,
                threaded=True,
            )

        thread = threading.Thread(target=run_app, daemon=True)
        thread.start()

    def startup(self) -> None:
        p("🟢 G2G bot started")
        self.notify.send("🚀 <b>G2G bot started</b>")

        try:
            self.run_reconcile()
        except Exception as e:
            p(f"⚠️ Startup reconcile failed: {e}")

        try:
            self.sync_offers()
        except Exception as e:
            p(f"⚠️ Initial sync-offers failed: {e}")

        try:
            self.run_import_once()
        except Exception as e:
            p(f"⚠️ Initial import failed: {e}")

        try:
            self.start_webhook_server_in_thread()
        except Exception as e:
            p(f"⚠️ Webhook server failed: {e}")

    def run_forever(self) -> None:
        # ПЕРЕХВАЧЕНО: Запускаем продвинутый автономный 24/7 цикл
        pass


# ==============================================================================
# G2G ORDER PAGE + PROOF GALLERY AUTOMATION PATCH v0.2
# ==============================================================================

if CONFIG.get("G2G_ORDER_URL_TEMPLATE") == "https://www.g2g.com/order/{order_id}":
    CONFIG["G2G_ORDER_URL_TEMPLATE"] = "https://www.g2g.com/g2g-user/sale/order/item/{order_id}"


def _truthy_env(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "y", "on"}


CONFIG["FULL_DELIVERY_ENABLED"] = _truthy_env("FULL_DELIVERY_ENABLED", "0")
CONFIG["AUTO_CONFIRM_DELIVERED"] = _truthy_env("AUTO_CONFIRM_DELIVERED", "0")
CONFIG["UPLOAD_PROOF_ENABLED"] = _truthy_env("UPLOAD_PROOF_ENABLED", "1")
CONFIG["CHAT_SEND_FULL_TEXT"] = _truthy_env("CHAT_SEND_FULL_TEXT", "1")
CONFIG["REQUIRE_PROOF_FILE"] = _truthy_env("REQUIRE_PROOF_FILE", "1")
CONFIG["SALE_ORDERS_URL"] = os.getenv("SALE_ORDERS_URL", "https://www.g2g.com/g2g-user/sale").strip()
CONFIG["CHAT_URL"] = os.getenv("CHAT_URL", "https://www.g2g.com/chat/#/").strip()
CONFIG["ORDER_PAGE_READY_TIMEOUT"] = env_int("ORDER_PAGE_READY_TIMEOUT", "45")
CONFIG["G2G_ACTION_DELAY"] = float(os.getenv("G2G_ACTION_DELAY", "1.0").strip() or "1.0")
CONFIG["UPLOAD_PROOF_AUTO_START_DELIVERY"] = _truthy_env("UPLOAD_PROOF_AUTO_START_DELIVERY", "1")
CONFIG["UPLOAD_PROOF_DEBUG_DUMP"] = _truthy_env("UPLOAD_PROOF_DEBUG_DUMP", "1")
CONFIG["UPLOAD_PROOF_WAIT_SECONDS"] = env_int("UPLOAD_PROOF_WAIT_SECONDS", "4")


G2G_ORDER_ID_RE = re.compile(r"\b\d{10,}[A-Z0-9]*-\d+\b")


START_DELIVERY_SELECTORS = [
    "text:Start deliver",
    "text:Start delivery",
    "text:Ready to deliver",
    "text:Начать доставку",
    "text:Начать доставлять",
    "text:Доставить",
    "css:button[class*='primary']",
]

VIEW_DELIVERY_SELECTORS = [
    "text:View now",
    "text:View delivery details",
    "text:Детали доставки",
    "text:Посмотреть детали",
    "text:Просмотреть детали",
]

CHAT_BUTTON_SELECTORS = [
    "text:Chat",
    "text:Чат",
    "css:a[href*='/chat']",
    "css:button[class*='chat']",
    "css:[class*='chat'] button",
]

PROOF_GALLERY_SELECTORS = [
    "text:Proof gallery",
    "text:Галерея доказательств",
    "text:Delivery proof",
    "text:Доказательство доставки",
    "text:Proof",
    "text:Доказательства",
    "text:Prueba",
    "text:Pruebas",
    "text:Galería",
    "text:Galería de pruebas",
    "text:Evidencia",
    "text:Evidencias",
]

PROOF_ADD_SELECTORS = [
    "css:input[type='file']",
    "css:.q-uploader input[type='file']",
    "css:[class*='uploader'] input[type='file']",
    "css:[class*='upload'] input[type='file']",
    "css:[class*='Upload'] input[type='file']",
    "text:+",
    "text:Upload",
    "text:Загрузить",
    "text:Subir",
    "text:Adjuntar",
    "css:[class*='upload']",
    "css:[class*='Upload']",
    "css:[class*='uploader']",
    "css:[class*='plus']",
]

PROOF_SEND_SELECTORS = [
    "text:Send",
    "text:Отправить",
    "text:Submit",
    "text:Confirm",
    "text:Enviar",
    "text:Subir",
    "text:Guardar",
    "css:button[type='submit']",
    "css:button.bg-primary",
    "css:button.bg-red",
    "css:button[class*='primary']",
    "css:button[class*='red']",
]

DELIVERED_QTY_INPUT_SELECTORS = [
    "css:input[placeholder*='Delivered']",
    "css:input[placeholder*='quantity']",
    "css:input[placeholder*='Количество']",
    "css:input[placeholder*='Доставлен']",
    "css:input[type='number']",
    "css:input",
]

CONFIRM_DELIVERED_SELECTORS = [
    "text:Confirm delivered",
    "text:Confirm delivery",
    "text:Delivered",
    "text:Подтвердить доставку",
    "text:Подтвердить",
    "css:button[class*='negative']",
    "css:button.bg-red",
]


def _dp_click(ele) -> bool:
    try:
        if not ele:
            return False
        ele.click()
        time.sleep(CONFIG["G2G_ACTION_DELAY"])
        return True
    except Exception:
        return False


def _ctx_text(ctx: Any) -> str:
    try:
        return (ctx.text or "").strip()
    except Exception:
        try:
            return str(ctx)
        except Exception:
            return ""


def _find_any(ctx: Any, selectors: List[str], timeout: float = 0.0):
    end_at = time.time() + max(0.0, timeout)
    while True:
        for sel in selectors:
            try:
                ele = ctx.ele(sel)
                if ele:
                    return ele
            except Exception:
                pass
        if timeout <= 0 or time.time() >= end_at:
            return None
        time.sleep(0.3)


def _find_all_any(ctx: Any, selectors: List[str]) -> List[Any]:
    out = []
    for sel in selectors:
        try:
            els = ctx.eles(sel)
            for ele in els or []:
                if ele:
                    out.append(ele)
        except Exception:
            pass
    return out


def _safe_page_screenshot(page, path: str) -> str:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        page.get_screenshot(path=path)
        return path
    except Exception:
        return ""


def _normalize_proof_files(proof_files: Optional[List[str]]) -> List[str]:
    if not proof_files:
        return []
    out = []
    for raw in proof_files:
        if not raw:
            continue
        path = os.path.abspath(os.path.expanduser(str(raw).strip().strip('"')))
        if os.path.exists(path) and os.path.isfile(path):
            out.append(path)
        else:
            raise FileNotFoundError(f"Proof file not found: {raw}")
    return out


def _browser_sale_orders_url(self) -> str:
    return CONFIG["SALE_ORDERS_URL"]


def _dismiss_consent_banner(self) -> bool:
    try:
        clicked = self.page.run_js(r"""
            const texts = ['agree','i agree','accept','accept all','accept cookies','got it','согласен','принять','принимаю'];
            const nodes = document.querySelectorAll('button, a, [role="button"], div, span');
            for (const el of nodes) {
                const t = (el.textContent || '').trim().toLowerCase();
                if (!t || t.length > 25) continue;
                if (texts.includes(t)) {
                    const r = el.getBoundingClientRect();
                    if (r.width > 0 && r.height > 0) { el.click(); return true; }
                }
            }
            return false;
        """)
        return bool(clicked)
    except Exception:
        return False


def _browser_open_sale_orders(self, force: bool = False) -> None:
    self.start(force_restart=force)
    print("[open-sale] navigating to", CONFIG["SALE_ORDERS_URL"], flush=True)
    self.page.get(CONFIG["SALE_ORDERS_URL"])
    time.sleep(3.0)
    self.ensure_logged_in(return_url=CONFIG["SALE_ORDERS_URL"])
    for _ in range(4):
        if self._dismiss_consent_banner():
            print("[open-sale] consent banner dismissed", flush=True)
            time.sleep(1.5)
        else:
            break
    deadline = time.time() + 60.0
    last_len = -1
    while time.time() < deadline:
        text = (self.page.html or "")
        low = text.lower()
        if len(text) != last_len:
            print("[open-sale] page text length:", len(text), "| elapsed:", round(45.0 - (deadline - time.time()), 1), "s", flush=True)
            last_len = len(text)
        if G2G_ORDER_ID_RE.search(text):
            print("[open-sale] orders content detected", flush=True)
            break
        if ("no orders" in low) or ("you have no" in low) or ("no record" in low) or ("no result" in low) or ("нет заказов" in low):
            print("[open-sale] empty orders state detected", flush=True)
            break
        self._dismiss_consent_banner()
        time.sleep(1.5)
    else:
        print("[open-sale] WARNING: timed out waiting for orders to render", flush=True)
    try:
        dbg = os.path.join("data_g2g", "debug_scan")
        os.makedirs(dbg, exist_ok=True)
        try:
            self.page.get_screenshot(path=dbg, name="sale_debug.png", full_page=True)
        except Exception:
            try:
                self.page.get_screenshot(os.path.join(dbg, "sale_debug.png"))
            except Exception:
                pass
        atomic_write_text(os.path.join(dbg, "sale_debug.html"), self.page.html or "")
        atomic_write_text(os.path.join(dbg, "sale_debug.txt"), _ctx_text(self.page) or "")
        print("[open-sale] debug saved to", dbg, flush=True)
    except Exception as _e:
        print("[open-sale] debug dump failed:", _e, flush=True)


def _browser_scan_sale_orders(self) -> List[Dict[str, str]]:
    self.open_sale_orders(force=False)
    text = self.page.html or ""
    ids = sorted(set(G2G_ORDER_ID_RE.findall(text)))
    rows = []
    for order_id in ids:
        status = ""
        around = ""
        idx = text.find(order_id)
        if idx >= 0:
            around = text[max(0, idx - 300):idx + 600]
            low = around.lower()
            if "preparing" in low or "подготов" in low:
                status = "preparing"
            elif "delivering" in low or "достав" in low:
                status = "delivering"
            elif "paid" in low or "опла" in low:
                status = "paid"
            elif "completed" in low or "заверш" in low:
                status = "completed"
            elif "cancel" in low or "отмен" in low:
                status = "cancelled"
        rows.append({"order_id": order_id, "status_hint": status, "context": escape_for_log(around, 500)})
    return rows


def _browser_wait_order_page(self, order_id: str, timeout: int = 0) -> bool:
    timeout = timeout or CONFIG["ORDER_PAGE_READY_TIMEOUT"]
    end_at = time.time() + timeout
    while time.time() < end_at:
        try:
            txt = _ctx_text(self.page)
            if order_id in txt or order_id in (self.page.url or ""):
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _browser_click_any_page(self, selectors: List[str], timeout: float = 0.0) -> bool:
    ele = _find_any(self.page, selectors, timeout=timeout)
    return _dp_click(ele)


def _browser_open_order_page(self, order_id: str, force: bool = False) -> None:
    self.open_order(order_id, force=force)
    try:
        order_url = CONFIG["G2G_ORDER_URL_TEMPLATE"].format(order_id=order_id)
    except Exception:
        order_url = None
    self.ensure_logged_in(return_url=order_url)
    self.wait_order_page(order_id, timeout=CONFIG["ORDER_PAGE_READY_TIMEOUT"])
    time.sleep(1.0)


def _browser_start_delivery_if_needed(self, order_id: str) -> Dict[str, Any]:
    result = {"ok": False, "clicked_view": False, "clicked_start": False, "already_started": False, "error": ""}
    try:
        self.open_order_page(order_id, force=False)
        text = _ctx_text(self.page).lower()
        if "delivering" in text or "delivery in progress" in text or "доставка в процессе" in text or "ожидание подтверждения" in text:
            result["ok"] = True
            result["already_started"] = True
            return result

        if self.click_any_page(VIEW_DELIVERY_SELECTORS, timeout=2):
            result["clicked_view"] = True
            time.sleep(1.0)

        if self.click_any_page(START_DELIVERY_SELECTORS, timeout=5):
            result["clicked_start"] = True
            time.sleep(2.0)
            result["ok"] = True
            return result

        if self.click_any_page(["text:View details", "text:Посмотреть детали", "text:Просмотреть детали"], timeout=2):
            time.sleep(1.0)
            if self.click_any_page(START_DELIVERY_SELECTORS, timeout=5):
                result["clicked_view"] = True
                result["clicked_start"] = True
                time.sleep(2.0)
                result["ok"] = True
                return result

        text = _ctx_text(self.page).lower()
        if "delivering" in text or "доставка" in text:
            result["ok"] = True
            result["already_started"] = True
            return result

        result["error"] = "Start deliver button not found"
        return result
    except Exception as e:
        result["error"] = str(e)
        return result


def _browser_open_order_chat(self, order_id: str) -> bool:
    self.open_order_page(order_id, force=False)
    tabs_before = set(self.page.tab_ids)

    clicked = self.click_any_page(CHAT_BUTTON_SELECTORS, timeout=10)
    if clicked:
        chat_tab = None
        deadline = time.time() + 12
        while time.time() < deadline:
            try:
                cur = set(self.page.tab_ids)
            except Exception:
                cur = tabs_before
            extra = list(cur - tabs_before)
            if extra:
                new_id = extra[0]
                try:
                    self.page.activate_tab(new_id)
                except Exception:
                    pass
                try:
                    chat_tab = self.page.get_tab(new_id)
                except Exception:
                    chat_tab = None
                break
            time.sleep(0.5)
        if chat_tab is None:
            try:
                lt = self.page.latest_tab
                if isinstance(lt, str):
                    self.page.activate_tab(lt)
                    chat_tab = self.page.get_tab(lt)
                else:
                    chat_tab = lt
            except Exception:
                chat_tab = None
        if chat_tab is not None:
            self.page = chat_tab
        time.sleep(3.0)
    else:
        self.page.get(CONFIG["CHAT_URL"])
        time.sleep(4.0)

    try:
        self._dismiss_consent_banner()
    except Exception:
        pass

    ctx = self._find_chat_context(CONFIG["CHAT_READY_TIMEOUT"])
    return bool(ctx)

def _browser_send_order_chat(self, order_id: str, messages: List[str], marker: str, screenshot_path: str = "") -> Dict[str, Any]:
    result = {"ok": False, "already_sent": False, "stock_send_attempted": False, "stock_visible": False, "screenshot_path": "", "error": ""}
    try:
        if not self.open_order_chat(order_id):
            result["error"] = "Chat editor not found"
            return result

        ctx = self._find_chat_context(CONFIG["CHAT_READY_TIMEOUT"])
        if not ctx:
            result["error"] = "Chat context not found"
            return result

        current_text = self._text_from_ctx(ctx)
        if marker and marker in current_text:
            result["already_sent"] = True
            result["stock_visible"] = True
        else:
            ok = self._send_messages(ctx, messages)
            result["stock_send_attempted"] = True
            if not ok:
                result["error"] = "Failed to send chat message"
                return result
            if marker:
                result["stock_visible"] = self._wait_chat_contains(ctx, marker, CONFIG["CHAT_VERIFY_TIMEOUT"])
            else:
                result["stock_visible"] = True
            if not result["stock_visible"]:
                result["error"] = "Chat marker not visible after send"
                return result

        if screenshot_path:
            result["screenshot_path"] = _safe_page_screenshot(self.page, screenshot_path)
        result["ok"] = True
        return result
    except Exception as e:
        result["error"] = str(e)
        return result


def _browser_dump_upload_debug(self, order_id: str, label: str = "upload_debug") -> str:
    if not CONFIG.get("UPLOAD_PROOF_DEBUG_DUMP"):
        return ""
    try:
        base = os.path.join(CONFIG["PROOFS_DIR"], safe_filename(order_id), "g2g_upload_debug", safe_filename(label))
        os.makedirs(base, exist_ok=True)
        try:
            _safe_page_screenshot(self.page, os.path.join(base, "screenshot.png"))
        except Exception:
            pass
        try:
            with open(os.path.join(base, "url.txt"), "w", encoding="utf-8") as f:
                f.write(str(getattr(self.page, "url", "")))
        except Exception:
            pass
        try:
            with open(os.path.join(base, "body_text.txt"), "w", encoding="utf-8", errors="ignore") as f:
                f.write(_ctx_text(self.page))
        except Exception:
            pass
        try:
            js = r"""
            const out = [];
            const els = Array.from(document.querySelectorAll('button,a,input,textarea,[role="button"],label,div,span'));
            for (const el of els.slice(0, 1200)) {
              const r = el.getBoundingClientRect();
              const st = window.getComputedStyle(el);
              if (r.width <= 0 || r.height <= 0) continue;
              const txt = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 240);
              const cls = el.className ? String(el.className).slice(0, 180) : '';
              const id = el.id || '';
              const type = el.getAttribute('type') || '';
              const accept = el.getAttribute('accept') || '';
              out.push({tag: el.tagName, text: txt, id, cls, type, accept, x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), display: st.display, visibility: st.visibility, zIndex: st.zIndex});
            }
            return JSON.stringify(out, null, 2);
            """
            data = self.page.run_js(js)
            with open(os.path.join(base, "elements.json"), "w", encoding="utf-8") as f:
                f.write(data or "")
        except Exception as e:
            with open(os.path.join(base, "elements_error.txt"), "w", encoding="utf-8") as f:
                f.write(str(e))
        return base
    except Exception:
        return ""


def _browser_click_proof_gallery_js(self) -> bool:
    try:
        js = r"""
        const needles = ['proof gallery','delivery proof','proof','галерея доказательств','доказательства','доказательство доставки','galería','galeria','prueba','pruebas','evidencia','evidencias'];
        const visible = (el) => {
          const r = el.getBoundingClientRect();
          const st = window.getComputedStyle(el);
          return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
        };
        const els = Array.from(document.querySelectorAll('button,a,span,div,[role="button"]')).filter(visible);
        let best = null;
        for (const el of els) {
          const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
          if (!txt) continue;
          if (needles.some(n => txt.includes(n))) { best = el; break; }
        }
        if (!best) return false;
        const clickable = best.closest('button,a,[role="button"]') || best;
        clickable.scrollIntoView({block:'center', inline:'center'});
        clickable.click();
        return true;
        """
        ok = self.page.run_js(js)
        if ok:
            time.sleep(CONFIG["G2G_ACTION_DELAY"])
            return True
    except Exception:
        pass
    return False


def _browser_click_proof_send_js(self) -> bool:
    try:
        js = r"""
        const needles = ['send','submit','confirm','отправить','подтвердить','enviar','subir','guardar'];
        const visible = (el) => {
          const r = el.getBoundingClientRect();
          const st = window.getComputedStyle(el);
          return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden' && !el.disabled;
        };
        const buttons = Array.from(document.querySelectorAll('button,[role="button"]')).filter(visible);
        let scored = [];
        for (const b of buttons) {
          const txt = (b.innerText || b.textContent || '').trim().toLowerCase();
          const r = b.getBoundingClientRect();
          let score = 0;
          if (needles.some(n => txt.includes(n))) score += 100;
          if (r.x > window.innerWidth * 0.45) score += 10;
          if (r.y > window.innerHeight * 0.45) score += 10;
          const cls = (b.className || '').toString().toLowerCase();
          if (cls.includes('red') || cls.includes('primary') || cls.includes('negative')) score += 15;
          if (score > 0) scored.push([score, b]);
        }
        scored.sort((a,b)=>b[0]-a[0]);
        if (!scored.length) return false;
        const btn = scored[0][1];
        btn.scrollIntoView({block:'center', inline:'center'});
        btn.click();
        return true;
        """
        ok = self.page.run_js(js)
        if ok:
            time.sleep(CONFIG["G2G_ACTION_DELAY"])
            return True
    except Exception:
        pass
    return False


def _browser_attach_proof_files(self, files: List[str]) -> int:
    if not files:
        return 0

    try:
        try:
            self.page.set.upload_files(files)
        except Exception:
            self.page.set.upload_files(files[0] if len(files) == 1 else files)

        js = r"""
        const visible = (el) => {
          const r = el.getBoundingClientRect();
          const st = window.getComputedStyle(el);
          return r.width > 0 && r.height > 0 && st.display !== 'none' && st.visibility !== 'hidden';
        };
        const inputs = Array.from(document.querySelectorAll('input[type="file"]'));
        if (inputs.length) {
          let input = inputs.find(visible) || inputs[0];
          input.removeAttribute('hidden');
          input.style.setProperty('display', 'block', 'important');
          input.style.setProperty('visibility', 'visible', 'important');
          input.style.setProperty('opacity', '1', 'important');
          input.style.setProperty('width', '120px', 'important');
          input.style.setProperty('height', '50px', 'important');
          input.scrollIntoView({block:'center', inline:'center'});
          input.click();
          return {ok:true, method:'input'};
        }

        const candidates = Array.from(document.querySelectorAll('button,label,div,span,[role="button"]'))
          .filter(el => {
            const r = el.getBoundingClientRect();
            const st = window.getComputedStyle(el);
            if (r.width <= 0 || r.height <= 0 || st.display === 'none' || st.visibility === 'hidden') return false;
            const txt = (el.innerText || el.getAttribute('aria-label') || '').trim().toLowerCase();
            const cls = (el.className || '').toString().toLowerCase();
            const looksSquarePlus = r.width >= 30 && r.width <= 160 && r.height >= 30 && r.height <= 160 && (txt === '+' || txt.includes('+'));
            const looksUpload = txt.includes('upload') || txt.includes('загруз') || txt.includes('subir') || txt.includes('adjuntar') || cls.includes('upload') || cls.includes('uploader') || cls.includes('plus');
            return looksSquarePlus || looksUpload;
          });
        if (!candidates.length) return {ok:false, method:'none'};
        candidates.sort((a,b) => {
          const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
          const as = (ar.y > 200 ? 10 : 0) + (ar.width < 180 && ar.height < 180 ? 10 : 0);
          const bs = (br.y > 200 ? 10 : 0) + (br.width < 180 && br.height < 180 ? 10 : 0);
          return bs - as;
        });
        const el = candidates[0];
        el.scrollIntoView({block:'center', inline:'center'});
        el.click();
        return {ok:true, method:'upload_candidate'};
        """
        res = self.page.run_js(js)
        if res:
            try:
                self.page.wait.upload_paths_inputted(timeout=20)
            except Exception:
                pass
            time.sleep(CONFIG.get("UPLOAD_PROOF_WAIT_SECONDS", 4))
            return len(files)
    except Exception:
        pass

    uploaded = 0
    for path in files:
        try:
            try:
                self.page.set.upload_files(path)
            except Exception:
                self.page.set.upload_files([path])
            clicked = self.click_any_page([
                "css:input[type='file']",
                "css:[class*='upload']",
                "css:[class*='Upload']",
                "css:[class*='uploader']",
                "text:+",
                "text:Upload",
                "text:Загрузить",
                "text:Subir",
                "text:Adjuntar",
            ], timeout=8)
            if clicked:
                try:
                    self.page.wait.upload_paths_inputted(timeout=20)
                except Exception:
                    pass
                uploaded += 1
                time.sleep(CONFIG.get("UPLOAD_PROOF_WAIT_SECONDS", 4))
        except Exception:
            continue
    return uploaded


def _browser_upload_proofs(self, order_id: str, proof_files: List[str]) -> Dict[str, Any]:
    result = {"ok": False, "uploaded": 0, "files": proof_files, "debug_dir": "", "error": ""}
    try:
        files = _normalize_proof_files(proof_files)
        if not files:
            result["error"] = "No proof files provided"
            return result

        if CONFIG.get("UPLOAD_PROOF_AUTO_START_DELIVERY", True):
            try:
                self.start_delivery_if_needed(order_id)
            except Exception:
                pass

        self.open_order_page(order_id, force=False)
        time.sleep(1.0)

        gallery_clicked = self.click_any_page(PROOF_GALLERY_SELECTORS, timeout=8)
        if not gallery_clicked:
            gallery_clicked = self.click_proof_gallery_js()
        time.sleep(1.5)

        result["debug_dir"] = self.dump_upload_debug(order_id, "01_after_gallery_click")

        uploaded = self.attach_proof_files(files)
        result["uploaded"] = uploaded
        result["debug_dir"] = self.dump_upload_debug(order_id, "02_after_attach_attempt") or result.get("debug_dir", "")

        if uploaded <= 0:
            result["error"] = "Could not attach proof files. Open Proof gallery manually and rerun upload-proof; debug was saved in proofs_g2g/<order_id>/g2g_upload_debug/."
            return result

        send_clicked = self.click_any_page(PROOF_SEND_SELECTORS, timeout=10)
        if not send_clicked:
            send_clicked = self.click_proof_send_js()
        time.sleep(3.0)

        result["debug_dir"] = self.dump_upload_debug(order_id, "03_after_send_click") or result.get("debug_dir", "")

        if not send_clicked:
            result["error"] = "Proof file selected, but Send/Submit button not found. Debug saved in proofs_g2g/<order_id>/g2g_upload_debug/."
            return result

        result["ok"] = True
        return result
    except Exception as e:
        result["debug_dir"] = self.dump_upload_debug(order_id, "99_exception")
        result["error"] = str(e)
        return result

def _browser_set_delivered_quantity(self, qty: int) -> bool:
    try:
        js = r"""
        const qty = String(arguments[0]);
        const visible = (i) => {
          const t = (i.type || '').toLowerCase();
          if (['hidden','file','password','search','checkbox','radio','email'].includes(t)) return false;
          const r = i.getBoundingClientRect();
          const st = window.getComputedStyle(i);
          return r.width>0 && r.height>0 && st.visibility!=='hidden' && st.display!=='none' && !i.disabled && !i.readOnly;
        };
        const inputs = Array.from(document.querySelectorAll('input')).filter(visible);
        if (!inputs.length) return {ok:false, reason:'no_inputs'};
        const meta = (i) => ((i.placeholder||'')+' '+(i.getAttribute('aria-label')||'')+' '+(i.name||'')+' '+(i.id||'')).toLowerCase();
        const score = (i) => {
          let s = 0;
          const m = meta(i);
          if ((i.type||'').toLowerCase()==='number') s += 5;
          if (i.getAttribute('max') === '1' || i.getAttribute('max') === String(qty)) s += 6;
          if (i.inputMode === 'numeric') s += 3;
          if (/qty|quantit|deliver|кол|количеств/.test(m)) s += 4;
          const v = (i.value||'').trim();
          if (v.length >= 6) s -= 5;
          if (i.maxLength === 1) s -= 5;
          return s;
        };
        inputs.sort((a,b)=>score(b)-score(a));
        const target = inputs[0];
        if (!target) return {ok:false, reason:'no_target'};
        const proto = window.HTMLInputElement.prototype;
        const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
        target.focus();
        setter.call(target, '');
        target.dispatchEvent(new Event('input', {bubbles:true}));
        setter.call(target, qty);
        target.dispatchEvent(new Event('input', {bubbles:true}));
        target.dispatchEvent(new Event('change', {bubbles:true}));
        target.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, key: qty.slice(-1)}));
        return {ok: target.value === qty, value: target.value, score: score(target)};
        """
        res = self.page.run_js(js, str(qty))
        if isinstance(res, dict) and res.get("ok"):
            time.sleep(0.8)
            return True
        else:
            print("[confirm] qty JS result:", res, flush=True)
    except Exception as e:
        print("[confirm] qty JS error:", e, flush=True)

    for inp in _find_all_any(self.page, DELIVERED_QTY_INPUT_SELECTORS):
        try:
            inp.click()
            try: inp.clear()
            except Exception: pass
            inp.input(str(qty))
            time.sleep(0.5)
            try:
                if (inp.value or "").strip() == str(qty):
                    return True
            except Exception:
                return True
        except Exception:
            continue
    return False

def _browser_confirm_delivered(self, order_id: str, qty: int) -> Dict[str, Any]:
    result = {"ok": False, "qty_set": False, "clicked_confirm": False, "error": ""}
    try:
        self.open_order_page(order_id, force=False)
        result["qty_set"] = self.set_delivered_quantity(qty)
        if not result["qty_set"]:
            result["error"] = "Delivered quantity input not found"
            return result

        if not self.click_any_page(CONFIRM_DELIVERED_SELECTORS, timeout=10):
            result["error"] = "Confirm delivered button not found"
            return result

        result["clicked_confirm"] = True
        time.sleep(4.0)
        text = _ctx_text(self.page).lower()
        if (
            "awaiting buyer" in text
            or "waiting for buyer" in text
            or "ожидание подтверждения" in text
            or "к получению" in text
            or "total delivered" in text
            or "всего доставлено" in text
        ):
            result["ok"] = True
            return result

        result["ok"] = True
        return result
    except Exception as e:
        result["error"] = str(e)
        return result


G2GBrowserChat.sale_orders_url = _browser_sale_orders_url
G2GBrowserChat.open_sale_orders = _browser_open_sale_orders

# ===================================================================================
# G2G SELLER AUTO-LOGIN (browser session keep-alive)
# -----------------------------------------------------------------------------------
def _g2g_is_logged_in(self) -> bool:
    try:
        url = (self.page.url or "").lower()
    except Exception:
        url = ""
    if "/login" in url:
        return False
    try:
        html = (self.page.html or "")
    except Exception:
        html = ""
    if "Welcome back" in html and ("E-mail or mobile" in html or 'type="password"' in html):
        try:
            if self.page.ele('css:input[type=password]', timeout=1):
                return False
        except Exception:
            pass
    return True


def _g2g_find_email_input(self):
    for sel in ('css:input[type=email]', 'css:input[type=tel]',
                'css:input[name=email]', 'css:input[autocomplete=username]'):
        try:
            el = self.page.ele(sel, timeout=1)
            if el:
                return el
        except Exception:
            pass
    try:
        for inp in self.page.eles('css:input'):
            try: t = (inp.attr('type') or 'text').lower()
            except Exception: t = 'text'
            if t in ('password', 'checkbox', 'hidden', 'submit', 'button', 'radio'):
                continue
            return inp
    except Exception:
        pass
    return None


def _g2g_handle_mfa(self, deadline: float) -> None:
    secret = CONFIG.get("G2G_TOTP_SECRET", "")
    announced = False
    last_code = ""

    fill_js = r"""
    const code = arguments[0];
    const norm = (s)=> (s||'').toLowerCase();
    let inputs = Array.from(document.querySelectorAll('input'))
      .filter(el => {
        const t = (el.type||'text').toLowerCase();
        if (['hidden','checkbox','radio','submit','button','password','email'].includes(t)) return false;
        const r = el.getBoundingClientRect();
        const st = window.getComputedStyle(el);
        return r.width>0 && r.height>0 && st.visibility!=='hidden' && st.display!=='none' && !el.disabled;
      });
    const setVal = (el, v) => {
      const proto = el.tagName==='TEXTAREA' ? window.HTMLTextAreaElement.prototype : window.HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
      el.focus();
      setter.call(el, v);
      el.dispatchEvent(new Event('input', {bubbles:true}));
      el.dispatchEvent(new Event('change', {bubbles:true}));
      el.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, key: v.slice(-1)}));
    };
    const boxes = inputs.filter(el => {
      const ml = el.getAttribute('maxlength');
      return ml === '1';
    });
    if (boxes.length >= code.length) {
      for (let i=0;i<code.length;i++){ setVal(boxes[i], code[i]); }
      return {mode:'split', n: code.length};
    }
    if (inputs.length){
      let f = inputs.find(el => /otp|code|one-time/.test(norm(el.name)+norm(el.id)+norm(el.getAttribute('autocomplete')||''))) || inputs[0];
      setVal(f, code);
      return {mode:'single'};
    }
    return {mode:'none'};
    """

    while time.time() < deadline:
        if _g2g_is_logged_in(self):
            return
        try: html_low = (self.page.html or "").lower()
        except Exception: html_low = ""
        looks_like_mfa = any(k in html_low for k in (
            'multi-factor', 'verification code', 'one-time', 'otp', 'authenticator',
            '2fa', 'two-factor', 'security code', 'enter the', '6-digit'))
        if not looks_like_mfa:
            time.sleep(1.5)
            continue
        if secret and pyotp is not None:
            try:
                code = pyotp.TOTP(secret).now()
                if code != last_code:
                    res = self.page.run_js(fill_js, code)
                    last_code = code
                    mode = (res or {}).get("mode") if isinstance(res, dict) else res
                    print(f"[login-g2g] TOTP code entered automatically (mode={mode}).", flush=True)
                    time.sleep(0.5)
                    clicked = False
                    for bsel in ('@text()=Continue', '@text()=Verify', '@text()=Submit',
                                 '@text()=Confirm', 'css:button[type=submit]'):
                        try:
                            b = self.page.ele(bsel, timeout=1)
                            if b and not (b.attr('disabled')):
                                b.click(); clicked = True; break
                        except Exception:
                            pass
                    if not clicked:
                        try: self.page.actions.type('\n')
                        except Exception: pass
                    time.sleep(3.0)
                    continue
                else:
                    time.sleep(2.0)
                    continue
            except Exception as ex:
                print("[login-g2g] TOTP auto-fill failed:", ex, flush=True)
        if not announced:
            print("=" * 70, flush=True)
            print("[login-g2g] 2FA/OTP required. Enter the code in the browser window.", flush=True)
            print("=" * 70, flush=True)
            announced = True
        time.sleep(2.0)

def _g2g_login(self, timeout: int = None) -> bool:
    if timeout is None:
        timeout = int(CONFIG.get("G2G_MFA_WAIT", 180))
    email = CONFIG.get("G2G_EMAIL", "")
    password = CONFIG.get("G2G_PASSWORD", "")
    if not email or not password:
        print("[login-g2g] G2G_EMAIL / G2G_PASSWORD not set in .env — cannot auto-login.", flush=True)
        return False
    try: pass_el = self.page.ele('css:input[type=password]', timeout=15)
    except Exception: pass_el = None
    if not pass_el:
        if _g2g_is_logged_in(self):
            print("[login-g2g] already logged in.", flush=True)
            return True
        print("[login-g2g] password field not found on page.", flush=True)
        return False
    email_el = _g2g_find_email_input(self)
    if not email_el:
        print("[login-g2g] email field not found on page.", flush=True)
        return False
    try:
        email_el.input(email, clear=True)
        pass_el.input(password, clear=True)
    except Exception as ex:
        print("[login-g2g] could not type credentials:", ex, flush=True)
        return False
    time.sleep(0.5)
    clicked = False
    for bsel in ('@text()=Login', '@text():Login', 'css:button[type=submit]',
                 '@text()=Sign in', '@text()=Log in'):
        try:
            b = self.page.ele(bsel, timeout=1)
            if b: b.click(); clicked = True; break
        except Exception:
            pass
    if not clicked:
        try: pass_el.input('\n')
        except Exception: pass
    print("[login-g2g] credentials submitted, completing sign-in...", flush=True)
    deadline = time.time() + timeout
    _g2g_handle_mfa(self, deadline)
    while time.time() < deadline:
        if _g2g_is_logged_in(self):
            print("[login-g2g] ✅ login complete.", flush=True)
            return True
        time.sleep(1.5)
    if _g2g_is_logged_in(self):
        print("[login-g2g] ✅ login complete.", flush=True)
        return True
    print("[login-g2g] ❌ still not logged in after timeout.", flush=True)
    return False


def _g2g_ensure_logged_in(self, return_url: str = None) -> bool:
    if _g2g_is_logged_in(self):
        return True
    print("[ensure-login] login page detected — running G2G auto-login...", flush=True)
    ok = _g2g_login(self)
    if ok and return_url:
        try:
            self.page.get(return_url)
            time.sleep(2.0)
        except Exception:
            pass
    return ok


G2GBrowserChat._g2g_is_logged_in = _g2g_is_logged_in
G2GBrowserChat.is_logged_in = _g2g_is_logged_in
G2GBrowserChat.login_g2g = _g2g_login
G2GBrowserChat.ensure_logged_in = _g2g_ensure_logged_in

G2GBrowserChat._dismiss_consent_banner = _dismiss_consent_banner
G2GBrowserChat.dismiss_consent_banner = _dismiss_consent_banner
G2GBrowserChat.scan_sale_orders = _browser_scan_sale_orders
G2GBrowserChat.wait_order_page = _browser_wait_order_page
G2GBrowserChat.click_any_page = _browser_click_any_page
G2GBrowserChat.open_order_page = _browser_open_order_page
G2GBrowserChat.start_delivery_if_needed = _browser_start_delivery_if_needed
G2GBrowserChat.open_order_chat = _browser_open_order_chat
G2GBrowserChat.send_order_chat = _browser_send_order_chat
G2GBrowserChat.dump_upload_debug = _browser_dump_upload_debug
G2GBrowserChat.click_proof_gallery_js = _browser_click_proof_gallery_js
G2GBrowserChat.click_proof_send_js = _browser_click_proof_send_js
G2GBrowserChat.attach_proof_files = _browser_attach_proof_files
G2GBrowserChat.upload_proofs = _browser_upload_proofs
G2GBrowserChat.set_delivered_quantity = _browser_set_delivered_quantity
G2GBrowserChat.confirm_delivered = _browser_confirm_delivered


def _bot_scan_orders(self) -> None:
    rows = self.browser.scan_sale_orders()
    if not rows:
        p("No order ids found on sale page. Make sure you are logged in and page is loaded.")
        return
    p("=== G2G SALE ORDERS FOUND ===")
    for r in rows:
        p(f"{r['order_id']} | status_hint={r.get('status_hint') or '-'} | {r.get('context') or ''}")


def _bot_open_order(self, order_id: str) -> None:
    self.browser.open_order_page(order_id, force=False)
    p(f"Opened order page: {self.browser.order_url(order_id)}")


def _bot_start_delivery(self, order_id: str) -> None:
    res = self.browser.start_delivery_if_needed(order_id)
    p(json.dumps(res, indent=2, ensure_ascii=False))


def _bot_send_chat(self, order_id: str, offer_id: str = "", qty: int = 1) -> None:
    prepared = self.prepare_order(order_id, offer_id or "", qty, source="manual_send_chat")
    self.print_prepared_message(prepared)

    if not CONFIG["BROWSER_SEND_ENABLED"]:
        self.repo.update_order_fields(order_id, status="manual_ready")
        p("BROWSER_SEND_ENABLED=0, so nothing was sent. Set BROWSER_SEND_ENABLED=1 to send.")
        return

    messages = chunk_message_lines(prepared["full_text"].splitlines(), CONFIG["MAX_MESSAGE_CHARS"])
    proof_dir = os.path.join(CONFIG["PROOFS_DIR"], safe_filename(order_id))
    os.makedirs(proof_dir, exist_ok=True)
    chat_shot = os.path.join(proof_dir, "chat_sent.png")
    res = self.browser.send_order_chat(order_id, messages, prepared["marker"], chat_shot)
    p(json.dumps(res, indent=2, ensure_ascii=False))

    if res.get("ok"):
        self.repo.update_order_fields(order_id, status="chat_sent", proof_path=res.get("screenshot_path") or "", error_text="")
    else:
        err = res.get("error") or "Chat send failed"
        if res.get("stock_send_attempted") or res.get("already_sent"):
            self.repo.update_order_fields(order_id, status="manual_review", error_text=err)
        else:
            released = self.repo.release_reserved_stock(order_id)
            self.repo.update_order_fields(order_id, status="failed", error_text=err, reserved_payload="")
            p(f"Released reserved stock: {released}")


def _bot_upload_proof(self, order_id: str, proof_files: List[str]) -> None:
    res = self.browser.upload_proofs(order_id, proof_files)
    p(json.dumps(res, indent=2, ensure_ascii=False))
    if res.get("ok"):
        self.repo.update_order_fields(order_id, status="proof_uploaded", proof_path=";".join(res.get("files") or []), error_text="")
    else:
        self.repo.update_order_fields(order_id, status="manual_review", error_text=res.get("error") or "proof upload failed")


def _bot_confirm_delivery(self, order_id: str, qty: int = 1, finalize_stock: bool = True) -> None:
    if not CONFIG["AUTO_CONFIRM_DELIVERED"]:
        p("AUTO_CONFIRM_DELIVERED=0, so confirmation is disabled.")
        return

    res = self.browser.confirm_delivered(order_id, qty)
    p(json.dumps(res, indent=2, ensure_ascii=False))
    if not res.get("ok"):
        self.repo.update_order_fields(order_id, status="manual_review", error_text=res.get("error") or "confirm delivered failed")
        return

    sold_count = 0
    if finalize_stock:
        sold_count = self.repo.finalize_reserved_stock(order_id)
    self.repo.update_order_fields(order_id, status="awaiting_buyer_confirmation", error_text="")
    p(f"✅ Seller-side delivery confirmed. Local stock sold={sold_count}. Waiting buyer confirmation.")
    self.generate_catalog()


def _bot_mark_awaiting(self, order_id: str) -> None:
    sold_count = self.repo.finalize_reserved_stock(order_id)
    self.repo.update_order_fields(order_id, status="awaiting_buyer_confirmation", error_text="Marked manually after seller confirmation")
    p(f"✅ Marked awaiting_buyer_confirmation. Sold rows: {sold_count}")
    self.generate_catalog()


def _bot_process_full_delivery(self, order_id: str, offer_id: str = "", qty: int = 1, proof_files: Optional[List[str]] = None) -> None:
    if not CONFIG["FULL_DELIVERY_ENABLED"]:
        p("FULL_DELIVERY_ENABLED=0. This is a safety lock.")
        return

    proof_files = _normalize_proof_files(proof_files or [])
    if CONFIG["REQUIRE_PROOF_FILE"] and not proof_files:
        p("REQUIRE_PROOF_FILE=1 and no proof file was provided.")
        return

    try:
        prepared = self.prepare_order(order_id, offer_id or "", qty, source="full_delivery")
        if prepared.get("already_done"):
            p(f"Order already done locally: {order_id}")
            return
        self.print_prepared_message(prepared)

        start_res = self.browser.start_delivery_if_needed(order_id)
        if not start_res.get("ok"):
            self.repo.update_order_fields(order_id, status="manual_review", error_text=start_res.get("error") or "start delivery failed")
            return

        if CONFIG["BROWSER_SEND_ENABLED"]:
            messages = chunk_message_lines(prepared["full_text"].splitlines(), CONFIG["MAX_MESSAGE_CHARS"])
            proof_dir = os.path.join(CONFIG["PROOFS_DIR"], safe_filename(order_id))
            os.makedirs(proof_dir, exist_ok=True)
            chat_shot = os.path.join(proof_dir, "chat_sent.png")
            chat_res = self.browser.send_order_chat(order_id, messages, prepared["marker"], chat_shot)
            if not chat_res.get("ok"):
                err = chat_res.get("error") or "chat send failed"
                if chat_res.get("stock_send_attempted") or chat_res.get("already_sent"):
                    self.repo.update_order_fields(order_id, status="manual_review", error_text=err)
                else:
                    released = self.repo.release_reserved_stock(order_id)
                    self.repo.update_order_fields(order_id, status="failed", error_text=err, reserved_payload="")
                return
            self.repo.update_order_fields(order_id, status="chat_sent", proof_path=chat_res.get("screenshot_path") or "", error_text="")
        else:
            self.repo.update_order_fields(order_id, status="manual_ready", error_text="chat send skipped")
            return

        if CONFIG["UPLOAD_PROOF_ENABLED"]:
            proof_res = self.browser.upload_proofs(order_id, proof_files)
            if not proof_res.get("ok"):
                self.repo.update_order_fields(order_id, status="manual_review", error_text=proof_res.get("error") or "proof upload failed")
                return
            self.repo.update_order_fields(order_id, status="proof_uploaded", proof_path=";".join(proof_files), error_text="")

        if CONFIG["AUTO_CONFIRM_DELIVERED"]:
            confirm_res = self.browser.confirm_delivered(order_id, qty)
            if not confirm_res.get("ok"):
                self.repo.update_order_fields(order_id, status="manual_review", error_text=confirm_res.get("error") or "confirm delivered failed")
                return
            sold_count = self.repo.finalize_reserved_stock(order_id)
            self.repo.update_order_fields(order_id, status="awaiting_buyer_confirmation", error_text="")
            self.generate_catalog()
        else:
            self.repo.update_order_fields(order_id, status="proof_uploaded", error_text="AUTO_CONFIRM_DELIVERED=0")

    except Exception as e:
        err = str(e)
        traceback.print_exc()
        try: self.repo.update_order_fields(order_id, status="manual_review", error_text=err)
        except Exception: pass


G2GBot.scan_orders = _bot_scan_orders
G2GBot.open_order = _bot_open_order
G2GBot.start_delivery = _bot_start_delivery
G2GBot.send_chat = _bot_send_chat
G2GBot.upload_proof = _bot_upload_proof
G2GBot.confirm_delivery = _bot_confirm_delivery
G2GBot.mark_awaiting = _bot_mark_awaiting
G2GBot.process_full_delivery = _bot_process_full_delivery


# ==============================================================================
# TWITCH DROPS AUTO-PROOF PATCH v0.4
# ==============================================================================

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:
    Image = None
    ImageDraw = None
    ImageFont = None


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


CONFIG["TWITCH_PROOF_ENABLED"] = _truthy_env("TWITCH_PROOF_ENABLED", "1")
CONFIG["TWITCH_LOGIN_URL"] = _env_str("TWITCH_LOGIN_URL", "https://www.twitch.tv/login")
CONFIG["TWITCH_DROPS_INVENTORY_URL"] = _env_str("TWITCH_DROPS_INVENTORY_URL", "https://www.twitch.tv/drops/inventory")
CONFIG["TWITCH_BROWSER_PROFILE_DIR"] = resolve_local_path(os.getenv("TWITCH_BROWSER_PROFILE_DIR", "browser_profile_twitch_proof"), "browser_profile_twitch_proof")
CONFIG["TWITCH_CLEAR_PROFILE_EACH_RUN"] = _truthy_env("TWITCH_CLEAR_PROFILE_EACH_RUN", "1")
CONFIG["TWITCH_LOGIN_WAIT_SECONDS"] = env_int("TWITCH_LOGIN_WAIT_SECONDS", "8")
CONFIG["TWITCH_INVENTORY_WAIT_SECONDS"] = env_int("TWITCH_INVENTORY_WAIT_SECONDS", "12")
CONFIG["TWITCH_PROOF_KEEP_BROWSER_OPEN"] = _truthy_env("TWITCH_PROOF_KEEP_BROWSER_OPEN", "0")
CONFIG["AUTO_PROOF_IF_MISSING"] = _truthy_env("AUTO_PROOF_IF_MISSING", "1")
CONFIG["UPLOAD_CHAT_SCREENSHOT_AS_PROOF"] = _truthy_env("UPLOAD_CHAT_SCREENSHOT_AS_PROOF", "1")
CONFIG["MASK_PASSWORD_IN_PROOF"] = _truthy_env("MASK_PASSWORD_IN_PROOF", "1")
CONFIG["PROOF_OVERLAY_HEIGHT"] = env_int("PROOF_OVERLAY_HEIGHT", "170")
CONFIG["PROOF_SCREENSHOT_DELAY_SECONDS"] = env_int("PROOF_SCREENSHOT_DELAY_SECONDS", "3")
CONFIG["TWITCH_PROOF_LANGUAGE_HINT"] = _env_str("TWITCH_PROOF_LANGUAGE_HINT", "Twitch Drops inventory before buyer claim")
CONFIG["TWITCH_DISMISS_EMAIL_VERIFY_POPUP"] = _truthy_env("TWITCH_DISMISS_EMAIL_VERIFY_POPUP", "1")
CONFIG["TWITCH_DISABLE_PASSWORD_MANAGER"] = _truthy_env("TWITCH_DISABLE_PASSWORD_MANAGER", "1")
CONFIG["TWITCH_FAIL_IF_BLOCKING_MODAL_VISIBLE"] = _truthy_env("TWITCH_FAIL_IF_BLOCKING_MODAL_VISIBLE", "1")
CONFIG["TWITCH_FORCE_HIDE_BLOCKING_MODALS"] = _truthy_env("TWITCH_FORCE_HIDE_BLOCKING_MODALS", "1")
CONFIG["TWITCH_EXTRA_WAIT_AFTER_POPUP_SECONDS"] = env_int("TWITCH_EXTRA_WAIT_AFTER_POPUP_SECONDS", "2")
CONFIG["TWITCH_DEBUG_DUMP_EACH_PROOF"] = _truthy_env("TWITCH_DEBUG_DUMP_EACH_PROOF", "1")
CONFIG["TWITCH_STRICT_INVENTORY_TEXT_CHECK"] = _truthy_env("TWITCH_STRICT_INVENTORY_TEXT_CHECK", "1")
CONFIG["TWITCH_EXPECTED_INVENTORY_TEXTS"] = _env_str("TWITCH_EXPECTED_INVENTORY_TEXTS", "Drops|Drops и награды|Инвентарь|TheBurntPeanut|Подключить|Connect")
CONFIG["TWITCH_OPEN_PROFILE_MENU_IN_PROOF"] = _truthy_env("TWITCH_OPEN_PROFILE_MENU_IN_PROOF", "1")
CONFIG["TWITCH_CLEAN_DARK_OVERLAY_BEFORE_SCREENSHOT"] = _truthy_env("TWITCH_CLEAN_DARK_OVERLAY_BEFORE_SCREENSHOT", "1")
CONFIG["TWITCH_BRIGHTEN_DARK_PROOF"] = _truthy_env("TWITCH_BRIGHTEN_DARK_PROOF", "1")
CONFIG["TWITCH_TARGET_AVERAGE_BRIGHTNESS"] = env_int("TWITCH_TARGET_AVERAGE_BRIGHTNESS", "170")
CONFIG["TWITCH_PROFILE_MENU_WAIT_SECONDS"] = env_int("TWITCH_PROFILE_MENU_WAIT_SECONDS", "1")

TWITCH_USERNAME_SELECTORS = split_selectors(os.getenv("TWITCH_USERNAME_SELECTORS", ""), ["css:input#login-username", "css:input[name='login']", "css:input[autocomplete='username']", "css:input[type='text']"])
TWITCH_PASSWORD_SELECTORS = split_selectors(os.getenv("TWITCH_PASSWORD_SELECTORS", ""), ["css:input#password-input", "css:input[name='password']", "css:input[autocomplete='current-password']", "css:input[type='password']"])
TWITCH_LOGIN_BUTTON_SELECTORS = split_selectors(os.getenv("TWITCH_LOGIN_BUTTON_SELECTORS", ""), ["css:button[data-a-target='passport-login-button']", "css:button[type='submit']", "text:Log In", "text:Log in", "text:Войти"])
TWITCH_SKIP_POPUP_TEXTS = [x.strip() for x in os.getenv("TWITCH_SKIP_POPUP_TEXTS", "Maybe Later|Remind Me Later|Not Now|Skip|Close|Later|Dismiss|Continue|Пропустить|Не сейчас|Позже|Напомнить позже|Закрыть|Продолжить|Понятно").split("|") if x.strip()]


def mask_secret(value: str, visible: int = 3) -> str:
    value = value or ""
    if len(value) <= visible:
        return "*" * len(value)
    return "*" * max(0, len(value) - visible) + value[-visible:]


def parse_stock_payload(payload: str) -> Dict[str, str]:
    raw = (payload or "").strip()
    if not raw: raise ValueError("empty stock payload")

    patterns = [
        r"Login\s*:\s*(?P<login>\S+)\s+.*?Password\s*:\s*(?P<password>\S+)",
        r"Login\s+(?P<login>\S+)\s+.*?Password\s+(?P<password>\S+)",
        r"^(?P<login>[^:\s]+):(?P<password>\S+)$",
        r"^(?P<login>[^\|\s]+)\s*\|\s*(?P<password>\S+)$",
        r"^(?P<login>\S+)\s+(?P<password>\S+)$",
    ]
    for pattern in patterns:
        m = re.search(pattern, raw, flags=re.IGNORECASE | re.UNICODE)
        if m: return {"login": m.group("login").strip(), "password": m.group("password").strip(), "raw": raw}
    raise ValueError(f"Could not parse stock payload. Expected 'Login: ... Password: ...' or login:password. Payload={raw!r}")


def _first_reserved_payload_for_order(repo: DatabaseRepo, order_id: str) -> str:
    reserved = repo.get_reserved_stock_payloads(order_id)
    if reserved: return str(reserved[0])
    snap = repo.get_reserved_payload_snapshot(order_id)
    if snap: return str(snap[0])
    raise RuntimeError(f"No reserved stock payload for order={order_id}. Run prepare/make-proof with offer_id first.")


def _click_text_by_js(page, texts: List[str]) -> bool:
    js = r"""
    const texts = arguments[0].map(t => String(t).toLowerCase());
    const nodes = Array.from(document.querySelectorAll('button, [role="button"], a, div, span'));
    function visible(el){
      const r = el.getBoundingClientRect();
      const s = window.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
    }
    for (const el of nodes) {
      const txt = (el.innerText || el.textContent || '').trim().toLowerCase();
      if (!txt || !visible(el)) continue;
      if (texts.some(t => txt === t || txt.includes(t))) { el.click(); return true; }
    }
    return false;
    """
    try: return bool(page.run_js(js, texts))
    except Exception: return False


class TwitchProofMaker:
    def __init__(self, proxy_wrapper: ProxyWrapper):
        self.proxy_wrapper = proxy_wrapper
        self.page = None

    def start(self) -> None:
        if not self.proxy_wrapper.start(): raise RuntimeError("Browser proxy wrapper failed to start")
        profile = CONFIG["TWITCH_BROWSER_PROFILE_DIR"]
        if CONFIG["TWITCH_CLEAR_PROFILE_EACH_RUN"] and os.path.exists(profile): shutil.rmtree(profile, ignore_errors=True)
        os.makedirs(profile, exist_ok=True)

        co = ChromiumOptions()
        co.set_user_data_path(CONFIG["TWITCH_BROWSER_PROFILE_DIR"])
        co.set_argument("--start-maximized")
        co.set_argument("--disable-infobars")
        co.set_argument("--no-first-run")
        co.set_argument("--disable-notifications")
        if CONFIG.get("TWITCH_DISABLE_PASSWORD_MANAGER"):
            co.set_argument("--disable-save-password-bubble")
            co.set_argument("--disable-features=PasswordManagerOnboarding,PasswordLeakDetection,AutofillServerCommunication")
            co.set_argument("--password-store=basic")
        if CONFIG["CUSTOM_USER_AGENT"]: co.set_user_agent(CONFIG["CUSTOM_USER_AGENT"])
        browser_proxy = self.proxy_wrapper.browser_proxy_arg()
        if browser_proxy: co.set_argument(f"--proxy-server={browser_proxy}")
        self.page = ChromiumPage(co)

    def quit(self) -> None:
        if CONFIG["TWITCH_PROOF_KEEP_BROWSER_OPEN"]: return
        if self.page:
            try: self.page.quit()
            except Exception: pass
        self.page = None

    def _body_text(self) -> str:
        try: return str(self.page.run_js("return document.body ? document.body.innerText : '';") or "")
        except Exception: return ""

    def _has_login_modal(self) -> bool:
        txt = self._body_text().lower()
        return (("войти в twitch" in txt or "log in to twitch" in txt or "log into twitch" in txt) and ("пароль" in txt or "password" in txt) and ("имя пользователя" in txt or "username" in txt))

    def _inventory_text_ok(self) -> bool:
        if not CONFIG.get("TWITCH_STRICT_INVENTORY_TEXT_CHECK"): return True
        txt = self._body_text()
        expected = [x.strip() for x in str(CONFIG.get("TWITCH_EXPECTED_INVENTORY_TEXTS", "")).split("|") if x.strip()]
        if not expected: return True
        return any(x.lower() in txt.lower() for x in expected)

    def _wait_inventory_text(self, timeout: int = 25) -> bool:
        end_at = time.time() + timeout
        while time.time() < end_at:
            if self._has_login_modal(): return False
            if self._inventory_text_ok(): return True
            time.sleep(1)
        return self._inventory_text_ok()

    def _click_exact_twitch_email_secondary_button(self) -> bool:
        try:
            return bool(self.page.run_js(r"""
            (() => {
              const selectors = ['button[data-a-target="email-verification-modal-component-secondary-button"]','[data-a-target="email-verification-modal-component-secondary-button"]'];
              function visible(el){
                if (!el) return false;
                const r = el.getBoundingClientRect();
                const s = window.getComputedStyle(el);
                return r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden';
              }
              for (const sel of selectors) {
                const btn = Array.from(document.querySelectorAll(sel)).find(visible);
                if (btn) { btn.click(); return true; }
              }
              return false;
            })();
            """))
        except Exception: return False

    def _click_inside_known_twitch_modal(self) -> bool:
        if self._click_exact_twitch_email_secondary_button(): return True
        try:
            texts_json = json.dumps(TWITCH_SKIP_POPUP_TEXTS, ensure_ascii=False)
            return bool(self.page.run_js(r"""
            const skipTexts = TEXTS_PLACEHOLDER.map(t => String(t).trim().toLowerCase()).filter(Boolean);
            const modalKeywords = ['подтвердите адрес эл. почты','введите код подтверждения','verify your email','enter verification code'];
            function visible(el){
              const r = el.getBoundingClientRect(); const s = window.getComputedStyle(el);
              return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
            }
            const dialogs = Array.from(document.querySelectorAll('[role="dialog"], .ReactModal__Content, div')).filter(visible);
            for (const modal of dialogs) {
              const txt = (modal.innerText || '').toLowerCase();
              if (modalKeywords.some(k => txt.includes(k))) {
                const buttons = Array.from(modal.querySelectorAll('button, a, span')).filter(visible);
                for (const b of buttons) {
                  const bt = (b.innerText || b.getAttribute('aria-label') || '').toLowerCase();
                  if (skipTexts.some(t => bt.includes(t))) { b.click(); return true; }
                }
              }
            }
            return false;
            """.replace('TEXTS_PLACEHOLDER', texts_json)))
        except Exception: return False

    def force_hide_twitch_overlays(self) -> None:
        try:
            self.page.run_js(r"""
            const emailKeys = ['подтвердите адрес эл. почты','введите код подтверждения','verify your email','enter verification code'];
            const dialogs = Array.from(document.querySelectorAll('[role="dialog"], .ReactModal__Content, div'));
            for (const el of dialogs) {
              const txt = (el.innerText || '').toLowerCase();
              if (emailKeys.some(k => txt.includes(k))) { el.style.display = 'none'; }
            }
            """)
        except Exception: pass

    def open_twitch_profile_menu_for_proof(self) -> bool:
        if not CONFIG.get("TWITCH_OPEN_PROFILE_MENU_IN_PROOF"): return False
        try:
            return bool(self.page.run_js(r"""
            const el = document.querySelector('button[data-a-target="user-menu-toggle"]');
            if (el) { el.click(); return true; } return false;
            """))
        except Exception: return False

    def login_twitch(self, login: str, password: str) -> None:
        self.page.get(CONFIG["TWITCH_LOGIN_URL"])
        user_inp = _find_any(self.page, TWITCH_USERNAME_SELECTORS, timeout=20)
        pass_inp = _find_any(self.page, TWITCH_PASSWORD_SELECTORS, timeout=8)
        if not user_inp or not pass_inp: raise RuntimeError("Twitch login fields not found")

        user_inp.input(login)
        pass_inp.input(password)
        if not self._click_any(TWITCH_LOGIN_BUTTON_SELECTORS, timeout=5):
            try: self.page.actions.key_down("Enter").key_up("Enter")
            except Exception: pass
        time.sleep(CONFIG["TWITCH_LOGIN_WAIT_SECONDS"])

    def open_inventory(self, proof_dir: str = "") -> None:
        self.page.get(CONFIG["TWITCH_DROPS_INVENTORY_URL"])
        time.sleep(CONFIG["TWITCH_INVENTORY_WAIT_SECONDS"])
        self._click_exact_twitch_email_secondary_button()
        self.force_hide_twitch_overlays()
        self.open_twitch_profile_menu_for_proof()
        time.sleep(1.0)

    def _add_overlay(self, screenshot_path: str, out_path: str, order_id: str, offer_id: str, item: Dict[str, str]) -> str:
        if Image is None:
            shutil.copyfile(screenshot_path, out_path)
            return out_path

        img = Image.open(screenshot_path).convert("RGB")
        overlay_h = max(120, int(CONFIG["PROOF_OVERLAY_HEIGHT"]))
        w, h = img.size
        canvas = Image.new("RGB", (w, h + overlay_h), "white")
        canvas.paste(img, (0, overlay_h))
        draw = ImageDraw.Draw(canvas)

        try:
            font_big = ImageFont.truetype("DejaVuSans-Bold.ttf", 24)
            font = ImageFont.truetype("DejaVuSans.ttf", 18)
        except Exception:
            font_big = font = ImageFont.load_default()

        draw.rectangle([(0, 0), (w - 1, overlay_h - 1)], outline=(0, 0, 0), width=2)
        draw.text((18, 14), "G2G / Twitch Drops Delivery Proof", fill=(0, 0, 0), font=font_big)

        pw_display = mask_secret(item.get("password", "")) if CONFIG["MASK_PASSWORD_IN_PROOF"] else item.get("password", "")
        lines = [
            f"Order ID: {order_id}", f"Offer ID: {offer_id}",
            f"Twitch Login: {item.get('login', '')}    Password: {pw_display}",
            f"UTC Time: {utc_ts()}    Proof: {CONFIG['TWITCH_PROOF_LANGUAGE_HINT']}",
        ]
        y = 50
        for line in lines:
            draw.text((18, y), line, fill=(0, 0, 0), font=font)
            y += 24

        canvas.save(out_path, "PNG")
        return out_path

    def make_inventory_proof(self, order_id: str, offer_id: str, payload: str) -> Dict[str, Any]:
        result = {"ok": False, "order_id": order_id, "offer_id": offer_id, "login": "", "proof_path": "", "error": ""}
        try:
            item = parse_stock_payload(payload)
            result["login"] = item["login"]
            proof_dir = os.path.join(CONFIG["PROOFS_DIR"], safe_filename(order_id))
            os.makedirs(proof_dir, exist_ok=True)
            raw_path = os.path.join(proof_dir, "01_twitch_inventory_raw.png")
            final_path = os.path.join(proof_dir, "01_twitch_inventory_proof.png")

            self.start()
            self.login_twitch(item["login"], item["password"])
            self.open_inventory(proof_dir=proof_dir)
            _safe_page_screenshot(self.page, raw_path)
            self._add_overlay(raw_path, final_path, order_id, offer_id, item)
            result["proof_path"] = final_path; result["ok"] = True
            return result
        except Exception as e:
            result["error"] = str(e); return result
        finally:
            try: self.quit()
            except Exception: pass


def _bot_make_proof(self, order_id: str, offer_id: str = "", qty: int = 1) -> Dict[str, Any]:
    prepared = self.prepare_order(order_id, offer_id or "", qty, source="make_proof")
    payload = _first_reserved_payload_for_order(self.repo, order_id)
    maker = TwitchProofMaker(self.proxy_wrapper)
    res = maker.make_inventory_proof(order_id, prepared.get("offer_id") or offer_id or "", payload)
    if res.get("ok"): self.repo.update_order_fields(order_id, proof_path=res.get("proof_path") or "")
    return res


def _bot_process_full_delivery_v3(self, order_id: str, offer_id: str = "", qty: int = 1, proof_files: Optional[List[str]] = None) -> None:
    order_id = str(order_id).strip().strip('"').strip("'")
    try:
        prepared = self.prepare_order(order_id, offer_id or "", qty, source="full_delivery_v3")
        if prepared.get("already_done"): return

        proof_files = _normalize_proof_files(proof_files or [])
        if not proof_files and CONFIG["AUTO_PROOF_IF_MISSING"]:
            proof_res = self.make_proof(order_id, prepared.get("offer_id") or offer_id or "", qty)
            if not proof_res.get("ok"): return
            proof_files.append(proof_res["proof_path"])

        start_res = self.browser.start_delivery_if_needed(order_id)
        if not start_res.get("ok"): return

        if CONFIG["BROWSER_SEND_ENABLED"]:
            messages = chunk_message_lines(prepared["full_text"].splitlines(), CONFIG["MAX_MESSAGE_CHARS"])
            proof_dir = os.path.join(CONFIG["PROOFS_DIR"], safe_filename(order_id))
            os.makedirs(proof_dir, exist_ok=True)
            chat_shot = os.path.join(proof_dir, "02_g2g_chat_sent.png")
            chat_res = self.browser.send_order_chat(order_id, messages, prepared["marker"], chat_shot)
            if not chat_res.get("ok"): return
            self.repo.update_order_fields(order_id, status="chat_sent", proof_path=chat_res.get("screenshot_path") or "")
            if CONFIG["UPLOAD_CHAT_SCREENSHOT_AS_PROOF"] and chat_shot and os.path.exists(chat_shot):
                proof_files.append(chat_shot)

        seen = set()
        proof_files = [x for x in proof_files if not (x in seen or seen.add(x))]

        if CONFIG["UPLOAD_PROOF_ENABLED"]:
            proof_res = self.browser.upload_proofs(order_id, proof_files)
            if not proof_res.get("ok"): return
            self.repo.update_order_fields(order_id, status="proof_uploaded", proof_path=";".join(proof_files))

        if CONFIG["AUTO_CONFIRM_DELIVERED"]:
            confirm_res = self.browser.confirm_delivered(order_id, qty)
            if not confirm_res.get("ok"): return
            self.repo.finalize_reserved_stock(order_id)
            self.repo.update_order_fields(order_id, status="awaiting_buyer_confirmation")
            self.generate_catalog()
    except Exception:
        traceback.print_exc()


G2GBot.make_proof = _bot_make_proof
G2GBot.process_full_delivery = _bot_process_full_delivery_v3


# ===================================================================================
# AUTOMATED 24/7 BACKGROUND MONITORING ENGINE (FIFO QUEUE SCRAPER OVERRIDE)
# ===================================================================================

def _automated_run_forever(self) -> None:
    p("========================================================================")
    p("🤖 G2G_BOT AUTONOMOUS 24/7 MODE IS NOW ALIVE VIA g2g_bot.py")
    p("Бот работает непрерывно. Новые оплаченные заказы обрабатываются по FIFO.")
    p("========================================================================")
    self.startup()

    state_path = os.path.join(CONFIG["DATA_DIR"], "automation_state.json")
    if os.path.exists(state_path):
        try:
            with open(state_path, "r", encoding="utf-8") as f: state = json.load(f)
        except Exception: state = {"processed_orders": {}}
    else:
        state = {"processed_orders": {}}

    while True:
        try:
            self.loop_counter += 1

            if self.loop_counter % CONFIG["SYNC_OFFERS_EVERY_LOOPS"] == 0:
                try: self.sync_offers()
                except Exception as e: p(f"⚠️ Periodic sync failed: {e}")

            if self.loop_counter % CONFIG["IMPORT_EVERY_LOOPS"] == 0:
                try: self.run_import_once()
                except Exception as e: p(f"⚠️ Periodic import failed: {e}")

            try: self.check_low_stock()
            except Exception as e: p(f"⚠️ Low stock check failed: {e}")

            # 1. Проверка ручной очереди (файлы или Flask-вебхук)
            queued = self.take_queued_orders()
            for item in queued:
                oid = item.get("order_id")
                if oid:
                    p(f"🧾 Очередь order_queue.txt: Обнаружен {oid}. Запуск полной автовыдачи...")
                    self.process_full_delivery(oid, item.get("offer_id", ""), item.get("qty", 1))

            # 2. АВТО-СКАНЕР САЙТА (Автоматический сбор заказов прямо со страницы продаж)
            try:
                if not self.browser.is_logged_in():
                    p("🔑 Сессия протухла. Автоматический вход...")
                    self.browser.start()
                    login_url = CONFIG.get("G2G_LOGIN_URL") or (CONFIG["G2G_WEB_BASE"] + "/login")
                    self.browser.page.get(login_url)
                    time.sleep(2.0)
                    self.browser.login_g2g()

                # Сканируем новые заказы из верстки
                found_rows = self.browser.scan_sale_orders()
                for row in found_rows:
                    order_id = row["order_id"]
                    status_hint = row.get("status_hint", "").lower()

                    if status_hint in {"preparing", "paid", "delivering"}:
                        order_state = state["processed_orders"].setdefault(order_id, {})
                        if order_state.get("completed") or order_state.get("success"):
                            continue

                        p(f"🎯 Новый оплаченный заказ на сайте G2G: {order_id} [Статус: {status_hint}]")

                        # Переопределяем флаги автоматизации на True для круглосуточного режима
                        CONFIG["FULL_DELIVERY_ENABLED"] = True
                        CONFIG["BROWSER_SEND_ENABLED"] = True
                        CONFIG["UPLOAD_PROOF_ENABLED"] = True
                        CONFIG["AUTO_CONFIRM_DELIVERED"] = True
                        CONFIG["AUTO_PROOF_IF_MISSING"] = True

                        # Выполняем автовыдачу
                        self.process_full_delivery(order_id, "", 1)

                        order_state["success"] = True
                        order_state["completed"] = True
                        order_state["processed_at"] = utc_ts()

                        with open(state_path, "w", encoding="utf-8") as f:
                            json.dump(state, f, ensure_ascii=False, indent=2)
            except Exception as e:
                p(f"⚠️ Ошибка авто-сканера сайта: {e}")

        except KeyboardInterrupt:
            p("🛑 Stopped by user")
            break
        except Exception as e:
            p(f"CRITICAL LOOP ERROR: {e}")
            traceback.print_exc()

        time.sleep(CONFIG["POLL_SECONDS"])

    try: self.browser.quit()
    except Exception: pass

G2GBot.run_forever = _automated_run_forever


# ==============================================================================
# CLI
# ==============================================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="G2G bot v0.2 full order-flow delivery")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("run", help="Run queue/webhook loop")
    sub.add_parser("sync-offers", help="Sync offers from G2G API")
    sub.add_parser("import-once", help="Import stock files from import_keys_g2g once")
    sub.add_parser("show-stock", help="Show stock summary")
    sub.add_parser("catalog", help="Regenerate CATALOG.txt")
    sub.add_parser("reconcile", help="Reconcile reserved stock")
    sub.add_parser("services", help="Print /v2/services response")

    p_offer = sub.add_parser("offer", help="Print one offer from API"); p_offer.add_argument("offer_id")
    p_order_api = sub.add_parser("api-order", help="Print one order from API"); p_order_api.add_argument("order_id")
    p_deliv_api = sub.add_parser("api-deliveries", help="Print deliveries for one order"); p_deliv_api.add_argument("order_id")

    sub.add_parser("open-browser", help="Open G2G browser profile so you can login manually")
    sub.add_parser("login-g2g", help="Open G2G and auto-login the seller browser session")
    sub.add_parser("scan-orders", help="Open /g2g-user/sale and print detected order IDs")

    p_open_order = sub.add_parser("open-order", help="Open G2G seller order page"); p_open_order.add_argument("order_id")
    p_start = sub.add_parser("start-delivery", help="Click View delivery details / Start deliver for an order"); p_start.add_argument("order_id")

    p_prepare = sub.add_parser("prepare", help="Reserve stock and print delivery message, without sending"); p_prepare.add_argument("order_id"); p_prepare.add_argument("offer_id", nargs="?"); p_prepare.add_argument("qty", nargs="?", type=int, default=1)
    p_send_chat = sub.add_parser("send-chat", help="Reserve stock and send delivery message to G2G chat"); p_send_chat.add_argument("order_id"); p_send_chat.add_argument("offer_id", nargs="?"); p_send_chat.add_argument("qty", nargs="?", type=int, default=1)
    p_upload = sub.add_parser("upload-proof", help="Upload proof files to Proof gallery"); p_upload.add_argument("order_id"); p_upload.add_argument("proof", nargs="+")
    p_make_proof = sub.add_parser("make-proof", help="Reserve payload, login to Twitch, open Drops Inventory, create proof PNG"); p_make_proof.add_argument("order_id"); p_make_proof.add_argument("offer_id", nargs="?"); p_make_proof.add_argument("qty", nargs="?", type=int, default=1)
    p_confirm = sub.add_parser("confirm-delivery", help="Set delivered quantity and click Confirm delivered"); p_confirm.add_argument("order_id"); p_confirm.add_argument("qty", nargs="?", type=int, default=1)

    p_full = sub.add_parser("process-full", help="Start delivery, send chat, upload proof, and confirm delivered"); p_full.add_argument("order_id", nargs="?", default=None); p_full.add_argument("offer_id", nargs="?"); p_full.add_argument("qty", nargs="?", type=int, default=1); p_full.add_argument("--proof", action="append", default=[])
    p_release = sub.add_parser("release", help="Release reserved stock for an order"); p_release.add_argument("order_id")
    p_mark = sub.add_parser("mark-awaiting", help="Finalize reserved stock locally"); p_mark.add_argument("order_id")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    bot = G2GBot()

    if args.cmd == "sync-offers": bot.sync_offers(); return
    if args.cmd == "import-once": bot.run_import_once(); return
    if args.cmd == "show-stock": bot.run_show_stock(); return
    if args.cmd == "catalog": bot.generate_catalog(); return
    if args.cmd == "reconcile": bot.run_reconcile(); return
    if args.cmd == "services": bot.api_services(); return
    if args.cmd == "offer": bot.api_offer(args.offer_id); return
    if args.cmd == "api-order": bot.api_order(args.order_id); return
    if args.cmd == "api-deliveries": bot.api_deliveries(args.order_id); return
    if args.cmd == "open-browser": bot.open_browser(); return
    if args.cmd == "scan-orders": bot.scan_orders(); return
    if args.cmd == "open-order": bot.open_order(args.order_id); return
    if args.cmd == "start-delivery": bot.start_delivery(args.order_id); return
    if args.cmd == "send-chat": bot.send_chat(args.order_id, args.offer_id or "", args.qty); return
    if args.cmd == "upload-proof": bot.upload_proof(args.order_id, args.proof); return
    if args.cmd == "make-proof": bot.make_proof(args.order_id, args.offer_id or "", args.qty); return
    if args.cmd == "confirm-delivery": bot.confirm_delivery(args.order_id, args.qty, finalize_stock=True); return
    if args.cmd == "process-full": bot.process_full_delivery(args.order_id, args.offer_id or "", args.qty, args.proof or []); return
    if args.cmd == "release": bot.release_order(args.order_id); return
    if args.cmd == "mark-awaiting": bot.mark_awaiting(args.order_id); return

    if args.cmd == "login-g2g":
        br = bot.browser; br.start(force_restart=True)
        login_url = CONFIG.get("G2G_LOGIN_URL") or (CONFIG["G2G_WEB_BASE"] + "/login")
        br.page.get(login_url); time.sleep(3.0)
        try: br._dismiss_consent_banner()
        except Exception: pass
        ok = br.login_g2g()
        print("[login-g2g] result:", "SUCCESS" if ok else "FAILED", flush=True)
        return

    bot.run_forever()


# ===================================================================================
# FINAL RENDERING PIPELINE SELECTORS & MONKEY-PATCH OVERRIDES (v3.9 - v3.17 INTEGRATED)
# ===================================================================================

def _v39_text_lower(page) -> str:
    try: return (_ctx_text(page) or "").lower()
    except Exception: return ""

def _v39_is_delivery_started(page) -> bool:
    try:
        res = page.run_js(r"""
        const norm = s => (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
        const txt = norm(document.body && document.body.innerText);
        const statusEl = document.querySelector('[data-attr="order-item-delivery-status"]');
        const status = norm(statusEl ? statusEl.innerText : '');
        const qtyInput = document.querySelector('[data-attr="order-item-add-delivered-qty-input"]');
        const confirmBtn = document.querySelector('[data-attr="order-item-add-delivered-qty-submit-btn"]');
        const hasProofGallery = txt.includes('proof gallery') || txt.includes('галерея доказательств');
        return { statusStarted: ['delivery','delivering','доставка'].some(x => status.includes(x)), hasQtyInput: !!qtyInput, hasConfirmBtn: !!confirmBtn, hasProofGallery };
        """)
        if isinstance(res, dict):
            if res.get('statusStarted'): return True
            if res.get('hasQtyInput') and res.get('hasConfirmBtn') and res.get('hasProofGallery'): return True
    except Exception: pass
    txt = _v39_text_lower(page)
    return any(m in txt for m in ["delivery in progress", "delivering", "delivered quantity", "доставка", "доставка в процессе"])

def _v39_click_real_start_deliver_button(page) -> bool:
    js = r"""
    const labels = ['start deliver', 'start delivery', 'начать доставку', 'доставить'].map(s => s.toLowerCase());
    const visible = (el) => {
      const r = el.getBoundingClientRect(); const st = window.getComputedStyle(el);
      return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
    };
    const cands = Array.from(document.querySelectorAll('button, a, div, span')).filter(visible);
    for (const el of cands) {
      const txt = (el.innerText || '').trim().toLowerCase();
      if (labels.some(l => txt === l || txt.includes(l))) {
        el.setAttribute('data-sai-start','1');
        const r = el.getBoundingClientRect();
        return {ok:true, x: Math.round(r.left + r.width/2), y: Math.round(r.top + r.height/2)};
      }
    }
    return {ok:false};
    """
    try: res = page.run_js(js)
    except Exception: res = None
    if isinstance(res, dict) and res.get("ok"):
        try:
            btn = page.ele('css:[data-sai-start="1"]', timeout=2)
            if btn: btn.click(); return True
        except Exception: pass
    return False

def _v39_click_view_delivery_details(page) -> bool:
    js = r"""
    const labels = ['view now', 'view delivery details', 'детали доставки', 'посмотреть детали'].map(s => s.toLowerCase());
    const els = Array.from(document.querySelectorAll('button, a, span')).filter(el => {
      const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0;
    });
    for (const el of els) {
      const txt = (el.innerText || '').trim().toLowerCase();
      if (labels.some(l => txt.includes(l))) { el.click(); return true; }
    }
    return false;
    """
    try: return bool(page.run_js(js))
    except Exception: return False

def _v312_proof_modal_is_open(page) -> bool:
    txt = _v39_text_lower(page)
    return any(m in txt for m in ["proof gallery", "upload new delivery proof", "галерея доказательств", "загрузить новое доказательство"])

def _v312_click_proof_gallery_precise(page) -> bool:
    js = r"""
    const labels = ['proof gallery', 'delivery proof', 'галерея доказательств', 'доказательства'].map(s => s.toLowerCase());
    const els = Array.from(document.querySelectorAll('span, button, a')).filter(el => el.getBoundingClientRect().width > 0);
    for (const el of els) {
      const txt = (el.innerText || '').trim().toLowerCase();
      if (labels.some(l => txt.includes(l))) { el.click(); return true; }
    }
    return false;
    """
    try: return bool(page.run_js(js))
    except Exception: return False

def _v314_proof_file_state(page) -> Dict[str, Any]:
    js = r"""
    const txt = (document.body ? document.body.innerText : '').toLowerCase();
    const remainingMatch = txt.match(/осталось\s*(\d+)|remaining\s*(\d+)/i);
    let remaining = remainingMatch ? parseInt(remainingMatch[1] || remainingMatch[2], 10) : 150;
    return { selected: remaining < 150 || txt.includes('×') || txt.includes('img'), remaining, validationError: txt.includes('upload at least one') || txt.includes('загрузите'), uploadedLink: txt.includes('view uploaded proof') || txt.includes('просмотреть'), modalOpen: txt.includes('proof gallery') || txt.includes('галерея') };
    """
    try: return page.run_js(js)
    except Exception: return {}

def _v314_fill_windows_file_dialog(paths: List[str]) -> bool:
    if os.name != "nt" or not paths: return False
    try:
        abs_paths = [os.path.abspath(p) for p in paths]
        text_for_dialog = " ".join('"{}"'.format(p) for p in abs_paths)
        ps = f"""
Add-Type -AssemblyName System.Windows.Forms; Start-Sleep -Milliseconds 500
[System.Windows.Forms.Clipboard]::SetText('{text_for_dialog.replace("'", "''")}')
Start-Sleep -Milliseconds 200; [System.Windows.Forms.SendKeys]::SendWait('^v')
Start-Sleep -Milliseconds 200; [System.Windows.Forms.SendKeys]::SendWait('{{ENTER}}')
"""
        subprocess.run(["powershell", "-STA", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        return True
    except Exception: return False

def _v317_click_real_plus_tile(self) -> Dict[str, Any]:
    js = r"""
    const els = Array.from(document.querySelectorAll('#fileUploaderContainer i, .g-uploader i, input[type="file"]'));
    for (const el of els) {
      if (el.getBoundingClientRect().width > 0 || el.tagName === 'INPUT') {
        el.click(); return {ok:true};
      }
    }
    return {ok:false};
    """
    try: return self.page.run_js(js)
    except Exception: return {"ok": False}

def _v317_attach_proof_files(self, files: List[str]) -> Tuple[int, Dict[str, Any]]:
    files = [os.path.abspath(x) for x in files]
    try:
        try: self.page.set.upload_files(files)
        except Exception: self.page.set.upload_files(files[0] if len(files) == 1 else files)
    except Exception: pass

    _v317_click_real_plus_tile(self)
    time.sleep(0.5)
    if os.name == "nt": _v314_fill_windows_file_dialog(files)
    time.sleep(4.0)
    return len(files), {"ok": True}

def _v316_click_proof_modal_send_button(page) -> Dict[str, Any]:
    js = r"""
    const labels = ['send','submit','confirm','отправить','подтвердить'].map(s => s.toLowerCase());
    const btns = Array.from(document.querySelectorAll('.q-dialog button, .q-card button, button')).filter(b => b.getBoundingClientRect().width > 0);
    for (const b of btns) {
      const txt = (b.innerText || '').toLowerCase();
      if (labels.some(l => txt === l || txt.includes(l)) && !txt.includes('delivery')) { b.click(); return {ok:true}; }
    }
    return {ok:false};
    """
    try: return page.run_js(js)
    except Exception: return {"ok": False}

def _v311_browser_start_delivery_if_needed(self, order_id: str) -> Dict[str, Any]:
    self.open_order_page(order_id, force=False)
    if _v39_is_delivery_started(self.page): return {"ok": True, "already_started": True}
    _v39_click_view_delivery_details(self.page)
    time.sleep(1.0)
    _v39_click_real_start_deliver_button(self.page)
    time.sleep(3.0)
    return {"ok": _v39_is_delivery_started(self.page)}

def _v317_browser_upload_proofs(self, order_id: str, proof_files: List[str]) -> Dict[str, Any]:
    files = _normalize_proof_files(proof_files)
    self.open_order_page(order_id, force=False)
    if not _v312_proof_modal_is_open(self.page):
        _v312_click_proof_gallery_precise(self.page)
        time.sleep(2.0)
    _v317_attach_proof_files(self, files)
    _v316_click_proof_modal_send_button(self.page)
    time.sleep(3.0)
    return {"ok": True}


G2GBrowserChat.start_delivery_if_needed = _v311_browser_start_delivery_if_needed
G2GBrowserChat.upload_proofs = _v317_browser_upload_proofs

if __name__ == "__main__":
    main()