# g2g_delivery_production.py
# Self-contained multi-account G2G auto-delivery pipeline. Fixes Windows cp1251/emoji prepare crash, recovers reserved stock, supports TwitchProofMaker(proxy_wrapper), auto-detects proof method names, fixes make_inventory_proof(order_id, offer_id, payload), runs every Twitch proof in a fresh subprocess, fixes nested JSON parsing, starts G2G delivery directly, uploads proofs directly, resumes unfinished orders, fixes proof-modal close detection, checks already-uploaded proof count, adds --assume-proofs-uploaded, improves gallery opening, and fixes missing _extract_remaining_from_text, clicks the correct selected-file upload dialog, uploads remaining proofs in one session with fresh '+' slots, accepts G2G's 'clear removed / remaining 148' success state, enforces one distinct proof per account, can upload extra distinct proofs even when G2G already has enough, retries Twitch proof generation with pauses, Production build: scans orders, prepares stock/accounts, tries one optional proof for the first account, uploads that one proof if available, sends readable chat messages with all account data, and can confirm delivery at the end.
#
# This file does NOT import g2g_auto_deliver_strict_v1.py.
#
# Required files in the same folder:
#   g2g_bot.py
#   g2g_upload_proof_strict_v3_19.py
#   g2g_send_chat_strict_v5.py
#   g2g_confirm_delivery_strict_v4.py
#
# Install:
#   pip install selenium pproxy pyperclip pyautogui pillow requests python-dotenv
#
# Find one processable order:
#   python .\g2g_delivery_production.py find
#
# Auto process one found order WITHOUT final confirm:
#   python .\g2g_delivery_production.py once --no-confirm
#
# Auto process one found order FULL:
#   python .\g2g_delivery_production.py once --full
#
# Watch mode FULL:
#   python .\g2g_delivery_production.py watch --full --poll 30
#
# Known order:
#   python .\g2g_delivery_production.py once --order-id "ORDER_ID" --full
#
# Quantity:
#   --qty auto  (default, parses order page ×N / Количество N)
#   --qty 2     (manual override)

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import logging
import logging
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

try:
    from g2g_fast_client import (
        G2GConfig, scan_orders as fast_scan_orders, inspect_order as fast_inspect_order,
        send_chat as fast_send_chat, upload_proof as fast_upload_proof,
        confirm_delivery as fast_confirm_delivery, read_fast_config, test_connection as fast_test_connection
    )
    FAST_CLIENT_AVAILABLE = True
except ImportError:
    FAST_CLIENT_AVAILABLE = False
    logging.warning("[INIT] g2g_fast_client.py not found, using only browser")


try:
    from g2g_fast_client import (
        G2GConfig, scan_orders as fast_scan_orders, inspect_order as fast_inspect_order,
        send_chat as fast_send_chat, upload_proof as fast_upload_proof,
        confirm_delivery as fast_confirm_delivery, read_fast_config, test_connection as fast_test_connection
    )
    FAST_CLIENT_AVAILABLE = True
except ImportError:
    FAST_CLIENT_AVAILABLE = False
    logging.warning("[INIT] g2g_fast_client.py not found, using only browser")

from selenium import webdriver
from selenium.webdriver.chrome.options import Options


ORDER_RE = re.compile(r"\b(\d{12,}[A-Z0-9]+-\d+)\b", re.I)
LOGIN_LINE_RE = re.compile(r"Login:\s*([^\s]+)\s+.*?Password:\s*([^\s]+)", re.I | re.S)


# =============================================================================
# Common helpers
# =============================================================================

def read_env(path: Path = Path(".env")) -> dict[str, str]:
    env = dict(os.environ)
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def now_ms() -> int:
    return int(time.time() * 1000)


def safe_mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    safe_mkdir(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def order_sort_key(order_id: str) -> int:
    m = re.match(r"(\d+)", order_id)
    return int(m.group(1)) if m else 0


def clean_order_id(value: str) -> str:
    m = ORDER_RE.search(value or "")
    return m.group(1) if m else (value or "").strip()


def parse_last_json(stdout: str) -> Optional[dict[str, Any]]:
    """
    Extract JSON object from mixed stdout.

    Important:
      Some helpers print an outer JSON object containing nested dictionaries.
      The old parser returned the *last nested* dict, for example maker_result,
      instead of the outer result. This parser chooses the largest decoded object.
    """
    decoder = json.JSONDecoder()
    best = None
    best_len = -1
    text = stdout or ""
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, end = decoder.raw_decode(text[i:])
            if isinstance(obj, dict) and end > best_len:
                best = obj
                best_len = end
        except Exception:
            pass
    return best


def run_py(args: list[str], env: dict[str, str] | None = None, timeout: int = 900) -> dict[str, Any]:
    cmd = [sys.executable] + args

    # Critical for Windows RU locale/cp1251:
    # g2g_bot.py prints emoji like 🔒. Without these variables, child Python may crash
    # with UnicodeEncodeError before stdout is captured.
    child_env = {**os.environ, **(env or {})}
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"

    cp = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=child_env,
    )
    merged = (cp.stdout or "") + "\n" + (cp.stderr or "")
    return {
        "cmd": cmd,
        "returncode": cp.returncode,
        "stdout": cp.stdout,
        "stderr": cp.stderr,
        "json": parse_last_json(merged),
    }


def print_step(title: str) -> None:
    print("\n" + "=" * 90)
    print(title)
    print("=" * 90)


def ensure_required_files(files: list[str]) -> list[str]:
    return [f for f in files if not Path(f).exists()]


# =============================================================================
# Proxy / Selenium browser
# =============================================================================

def parse_host_port(value: str) -> tuple[str, int]:
    value = (value or "").strip()
    if ":" not in value:
        return value, 0
    host, port = value.rsplit(":", 1)
    return host.strip(), int(port.strip())


def port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def build_pproxy_upstream(env: dict[str, str]) -> str:
    scheme = env.get("PROXY_SCHEME", "http").strip() or "http"
    host = env.get("PROXY_HOST", "").strip()
    port = env.get("PROXY_PORT", "").strip()
    user = env.get("PROXY_USER", "").strip()
    password = env.get("PROXY_PASS", "").strip()

    if not host or not port:
        raise RuntimeError("PROXY_HOST / PROXY_PORT are empty in .env")

    if user and password:
        return f"{scheme}://{host}:{port}#{user}:{password}"
    return f"{scheme}://{host}:{port}"


def ensure_proxy_wrapper(env: dict[str, str]) -> dict[str, Any]:
    mode = env.get("BROWSER_PROXY_MODE", "").strip().lower()
    if mode != "wrapper":
        return {"ok": True, "mode": mode, "message": "BROWSER_PROXY_MODE is not wrapper; nothing to start."}

    listen = env.get("BROWSER_PROXY_LISTEN", "127.0.0.1:8900").strip()
    host, port = parse_host_port(listen)
    if not host or not port:
        raise RuntimeError(f"Invalid BROWSER_PROXY_LISTEN={listen!r}")

    if port_open(host, port):
        return {"ok": True, "mode": "wrapper", "listen": listen, "already_listening": True}

    upstream = build_pproxy_upstream(env)
    cmd = [sys.executable, "-m", "pproxy", "-l", f"http://{listen}", "-r", upstream]
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)

    for _ in range(50):
        if port_open(host, port):
            return {"ok": True, "mode": "wrapper", "listen": listen, "started": True, "pid": proc.pid}
        time.sleep(0.2)

    raise RuntimeError(f"Proxy wrapper did not start on {listen}. Run: pip install pproxy")


def make_driver(env: dict[str, str], debug_dir: Path | None = None) -> webdriver.Chrome:
    opts = Options()

    profile_dir = Path(env.get("BROWSER_PROFILE_DIR", "browser_profile_g2g")).resolve()
    safe_mkdir(profile_dir)
    opts.add_argument(f"--user-data-dir={profile_dir}")
    opts.add_argument("--profile-directory=Default")
    opts.add_argument("--lang=ru-RU")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-popup-blocking")
    opts.add_argument("--remote-allow-origins=*")
    opts.add_argument("--no-first-run")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--start-maximized")

    proxy_used = ""
    mode = env.get("BROWSER_PROXY_MODE", "").strip().lower()
    if mode == "wrapper":
        listen = env.get("BROWSER_PROXY_LISTEN", "").strip()
        if listen:
            proxy_used = f"http://{listen}" if not listen.startswith(("http://", "https://", "socks5://")) else listen
    elif mode == "direct":
        proxy_used = env.get("BROWSER_PROXY", "").strip()

    if proxy_used:
        opts.add_argument(f"--proxy-server={proxy_used}")

    driver = webdriver.Chrome(options=opts)
    try:
        driver.set_window_size(1450, 950)
    except Exception:
        pass

    if debug_dir:
        safe_mkdir(debug_dir)
        (debug_dir / "driver_info.json").write_text(
            json.dumps({"profile_dir": str(profile_dir), "proxy_used": proxy_used}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return driver


def dump_debug(driver: webdriver.Chrome, debug_dir: Path, name: str) -> None:
    safe_mkdir(debug_dir)
    try:
        driver.save_screenshot(str(debug_dir / f"{name}.png"))
    except Exception:
        pass
    try:
        (debug_dir / f"{name}.html").write_text(driver.page_source, encoding="utf-8", errors="replace")
    except Exception:
        pass
    try:
        info = {
            "url": driver.current_url,
            "title": driver.title,
            "body": driver.execute_script("return (document.body && document.body.innerText || '').slice(0, 8000);"),
        }
        (debug_dir / f"{name}.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def body_text(driver: webdriver.Chrome) -> str:
    try:
        return driver.execute_script("return document.body ? document.body.innerText : '';") or ""
    except Exception:
        return ""


def wait_body_contains(driver: webdriver.Chrome, needle: str, timeout: int = 60) -> bool:
    end = time.time() + timeout
    needle = needle.lower()
    while time.time() < end:
        if needle in body_text(driver).lower():
            return True
        time.sleep(0.5)
    return False


# =============================================================================
# Order finding / inspection
# =============================================================================

@dataclass
class FoundOrder:
    order_id: str
    buyer: str = ""
    current_url: str = ""
    status_sample: str = ""
    processable: bool = False


def order_url(env: dict[str, str], order_id: str) -> str:
    # Always build the seller order URL from the explicit order id.
    # Do not trust a stale browser/login redirect URL.
    oid = clean_order_id(order_id)
    tmpl = env.get("G2G_ORDER_URL_TEMPLATE", "https://www.g2g.com/g2g-user/sale/order/item/{order_id}")
    if "{order_id}" not in tmpl:
        tmpl = "https://www.g2g.com/g2g-user/sale/order/item/{order_id}"
    return tmpl.replace("{order_id}", oid)


def parse_buyer_from_body(body: str) -> str:
    patterns = [
        r"Куплено\s+([^\n\r]+)",
        r"Bought\s+by\s+([^\n\r]+)",
        r"Comprado\s+por\s+([^\n\r]+)",
    ]
    for pat in patterns:
        m = re.search(pat, body, flags=re.I)
        if m:
            val = m.group(1).strip()
            val = re.split(r"\s+(?:Чат|Chat|Итог|Total)\b", val, flags=re.I)[0].strip()
            if val and len(val) <= 80:
                return val
    return ""


def is_finished_or_bad(body: str) -> bool:
    low = body.lower()

    # Do NOT treat the standard warning "Заказ может быть отменен..." as a cancelled order.
    cancelled_markers = [
        "заказ отменен",
        "заказ отменён",
        "отменено",
        "cancelled",
        "canceled",
    ]
    finished_markers = [
        "ожидание подтверждения покупателя",
        "ожидает подтверждения покупателя",
        "доставка завершена",
        "delivery completed",
        "waiting for buyer confirmation",
    ]

    return any(x in low for x in cancelled_markers + finished_markers)


def is_processable_order(body: str) -> bool:
    low = body.lower()
    if is_finished_or_bad(body):
        return False

    positive = [
        "оплачено",
        "paid",
        "доставка",
        "доставка в процессе",
        "заказ обработан",
        "ожидает доставки",
        "ready to deliver",
        "start deliver",
        "подтвердить доставку",
        "галерея доказательств",
    ]
    return any(x in low for x in positive)


def parse_qty_from_order_body(body: str) -> int:
    text = body or ""
    patterns = [
        r"#\s*\d{12,}[A-Z0-9]+-\d+\s*×\s*(\d+)",
        r"×\s*(\d+)\s*(?:\n|$)",
        r"Количество\s+(\d+)",
        r"Quantity\s+(\d+)",
        r"qty\s*[:=]?\s*(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, flags=re.I)
        if m:
            try:
                n = int(m.group(1))
                if 1 <= n <= 100:
                    return n
            except Exception:
                pass
    return 1


def find_order_ids_on_sales_page(driver: webdriver.Chrome, env: dict[str, str], debug_dir: Path) -> list[str]:
    sale_url = env.get("SALE_ORDERS_URL", "https://www.g2g.com/g2g-user/sale").strip() or "https://www.g2g.com/g2g-user/sale"
    driver.get(sale_url)
    time.sleep(8)
    dump_debug(driver, debug_dir, "find_01_sale_page")

    js = r"""
        const ids = new Set();
        const re = /\/g2g-user\/sale\/order\/item\/([^/?#"]+)/i;

        for (const a of Array.from(document.querySelectorAll('a[href]'))) {
          const href = a.href || '';
          const m = href.match(re);
          if (m) ids.add(m[1]);
        }

        const txt = (document.body && document.body.innerText || '');
        const m2 = txt.match(/\b\d{12,}[A-Z0-9]+-\d+\b/gi) || [];
        for (const x of m2) ids.add(x);

        return Array.from(ids);
    """
    try:
        ids = driver.execute_script(js) or []
    except Exception:
        ids = []

    ids = [clean_order_id(x) for x in ids if ORDER_RE.search(x)]
    ids = list(dict.fromkeys(ids))
    ids.sort(key=order_sort_key, reverse=True)
    return ids


def inspect_order(driver: webdriver.Chrome, env: dict[str, str], order_id: str, debug_dir: Path) -> FoundOrder:
    driver.get(order_url(env, order_id))
    wait_body_contains(driver, order_id.split("-", 1)[0], timeout=60)
    time.sleep(2)
    body = body_text(driver)
    dump_debug(driver, debug_dir, f"find_order_{order_id}")

    return FoundOrder(
        order_id=order_id,
        buyer=parse_buyer_from_body(body),
        current_url=driver.current_url,
        status_sample=body[:2500],
        processable=is_processable_order(body),
    )


def _unfinished_state_order_ids(state: dict[str, Any]) -> list[str]:
    """
    Resume partially processed orders even if G2G seller list does not expose them anymore.
    This is important after --no-confirm / after delivery is already started.
    """
    ids = []
    for oid, rec in (state.get("orders") or {}).items():
        if not ORDER_RE.search(str(oid)):
            continue
        if rec.get("confirmed") or rec.get("completed"):
            continue
        # Any of these means the bot has already touched this order and should resume it.
        if (
            rec.get("prepared")
            or rec.get("accounts")
            or rec.get("proofs")
            or rec.get("delivery_started")
            or rec.get("uploaded_proofs")
            or rec.get("chat_messages")
            or rec.get("completed_without_confirm")
            or rec.get("ready_to_confirm")
        ):
            ids.append(oid)
    ids.sort(key=order_sort_key, reverse=True)
    return ids


def find_new_order(env: dict[str, str], state: dict[str, Any], debug_dir: Path, explicit_order_id: str = "") -> Optional[FoundOrder]:
    # --- HYBRID FAST SCAN START ---
    if FAST_CLIENT_AVAILABLE:
        fast_cfg = read_fast_config()
        if fast_cfg.scan_enabled:
            print("[FAST] Checking for new orders via requests...")
            scan_res = fast_scan_orders(fast_cfg)
            if not scan_res.get("needs_browser") and scan_res.get("orders"):
                for o in scan_res["orders"]:
                    oid = o["order_id"]
                    rec = state.get("orders", {}).get(oid, {})
                    if rec.get("confirmed") or rec.get("completed"):
                        continue

                    print(f"[FAST] Found processable order: {oid}")
                    insp_res = fast_inspect_order(fast_cfg, oid)
                    if not insp_res.get("needs_browser"):
                        return FoundOrder(
                            order_id=oid,
                            buyer=insp_res.get("buyer", ""),
                            current_url=order_url(env, oid),
                            status_sample=f"Fast-scanned: {o.get('status')}",
                            processable=True
                        )
    # --- HYBRID FAST SCAN END ---

    # --- HYBRID FAST SCAN START ---
    if FAST_CLIENT_AVAILABLE:
        fast_cfg = read_fast_config()
        if fast_cfg.scan_enabled:
            print("[FAST] Checking for new orders via requests...")
            scan_res = fast_scan_orders(fast_cfg)
            if not scan_res.get("needs_browser") and scan_res.get("orders"):
                # Use the first found order to avoid opening a browser for scanning.
                # In a more advanced version, we could process all of them.
                for o in scan_res["orders"]:
                    oid = o["order_id"]
                    # Skip finished/known orders similarly to browser logic
                    rec = state.get("orders", {}).get(oid, {})
                    if rec.get("confirmed") or rec.get("completed"):
                        continue

                    print(f"[FAST] Found processable order: {oid}")
                    # We still need to 'inspect' it to get full details (buyer name, etc.)
                    # We can try to do this via requests too.
                    insp_res = fast_inspect_order(fast_cfg, oid)
                    if not insp_res.get("needs_browser"):
                        return FoundOrder(
                            order_id=oid,
                            buyer=insp_res.get("buyer", ""),
                            current_url=order_url(env, oid),
                            status_sample=f"Fast-scanned: {o.get('status')}",
                            processable=True
                        )
    # --- HYBRID FAST SCAN END ---

    # ensure_proxy_wrapper(env)
    driver = make_driver(env, debug_dir)
    try:
        if explicit_order_id:
            return inspect_order(driver, env, clean_order_id(explicit_order_id), debug_dir)

        checked: list[str] = []

        # 1) First resume unfinished orders from state.
        # This prevents "No processable order found" after the order has moved from Preparation
        # into Delivery and the seller-list scraper no longer returns it.
        state_ids = _unfinished_state_order_ids(state)
        for oid in state_ids:
            checked.append(oid)
            fo = inspect_order(driver, env, oid, debug_dir)
            if fo.processable:
                (debug_dir / "find_resume_from_state.json").write_text(
                    json.dumps({"ok": True, "order_id": oid, "state_ids": state_ids, "body": fo.status_sample}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                return fo

        # 2) Then scan seller orders page for truly new orders.
        ids = find_order_ids_on_sales_page(driver, env, debug_dir)
        (debug_dir / "find_order_ids.json").write_text(json.dumps({"state_ids": state_ids, "sale_page_ids": ids}, ensure_ascii=False, indent=2), encoding="utf-8")

        for oid in ids:
            if oid in checked:
                continue
            rec = state.get("orders", {}).get(oid, {})
            if rec.get("confirmed") or rec.get("completed"):
                continue

            fo = inspect_order(driver, env, oid, debug_dir)
            if fo.processable:
                return fo

        (debug_dir / "find_none.json").write_text(
            json.dumps({"ok": False, "state_ids": state_ids, "sale_page_ids": ids, "checked": checked}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def qty_for_order(args: argparse.Namespace, fo: FoundOrder) -> int:
    q_arg = str(args.qty).strip().lower()
    if q_arg and q_arg != "auto":
        return max(1, int(q_arg))
    return max(1, parse_qty_from_order_body(fo.status_sample))


def _is_login_page(driver: webdriver.Chrome) -> bool:
    try:
        url = (driver.current_url or "").lower()
        txt = body_text(driver).lower()
        return ("/login" in url) or ("welcome back!" in txt and "password" in txt and "login" in txt)
    except Exception:
        return False


def _click_best_text(driver: webdriver.Chrome, needles: list[str], timeout: int = 20) -> dict[str, Any]:
    """
    Click smallest visible clickable-looking element containing one of needles.
    Works better on G2G than fixed selectors because language/layout changes.
    """
    end = time.time() + timeout
    needles_l = [n.lower() for n in needles]

    js = r"""
        const needles = arguments[0].map(x => String(x).toLowerCase());
        const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],span,div'));
        const out = [];

        function visible(el) {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none' && st.opacity !== '0';
        }

        for (const el of nodes) {
          if (!visible(el)) continue;
          const txt = (el.innerText || el.textContent || '').trim();
          if (!txt) continue;
          const low = txt.toLowerCase();
          if (!needles.some(n => low.includes(n))) continue;

          const r = el.getBoundingClientRect();
          const area = r.width * r.height;

          // Prefer real buttons / links / role=button and smaller controls.
          let score = area;
          const tag = el.tagName.toLowerCase();
          if (tag === 'button') score -= 100000;
          if (tag === 'a') score -= 50000;
          if (el.getAttribute('role') === 'button') score -= 50000;
          if (area > 300000) score += 1000000;

          out.push({
            tag,
            text: txt.slice(0, 120),
            score,
            rect: {x:r.x, y:r.y, w:r.width, h:r.height}
          });
        }

        out.sort((a,b) => a.score - b.score);
        if (!out.length) return {ok:false, candidates:[]};

        const chosen = out[0];

        // Find the corresponding element again and click.
        for (const el of nodes) {
          if (!visible(el)) continue;
          const txt = (el.innerText || el.textContent || '').trim().slice(0, 120);
          if (txt !== chosen.text) continue;
          const low = txt.toLowerCase();
          if (!needles.some(n => low.includes(n))) continue;

          el.scrollIntoView({block:'center', inline:'center'});
          el.click();
          return {ok:true, clicked:chosen, candidates:out.slice(0,10)};
        }
        return {ok:false, candidates:out.slice(0,10), error:'chosen not found on second pass'};
    """

    while time.time() < end:
        try:
            res = driver.execute_script(js, needles_l)
            if isinstance(res, dict) and res.get("ok"):
                return res
        except Exception as e:
            last = {"ok": False, "error": str(e)}
        time.sleep(0.5)

    try:
        body = body_text(driver)[:1500]
    except Exception:
        body = ""
    return {"ok": False, "error": "text control not found", "needles": needles, "bodySample": body}


def verify_delivery_stage(env: dict[str, str], order_id: str, debug_dir: Path) -> dict[str, Any]:
    # ensure_proxy_wrapper(env)
    driver = make_driver(env, debug_dir)
    try:
        driver.get(order_url(env, order_id))
        time.sleep(2)

        if _is_login_page(driver):
            dump_debug(driver, debug_dir, "verify_delivery_login_page")
            return {
                "ok": False,
                "login_required": True,
                "error": "G2G seller session is not logged in in browser_profile_g2g.",
                "url": driver.current_url,
                "bodySample": body_text(driver)[:1500],
            }

        wait_body_contains(driver, order_id.split("-", 1)[0], timeout=60)
        time.sleep(3)
        body = body_text(driver)
        low = body.lower()
        ok = (
            "галерея доказательств" in low
            and (
                "подтвердить доставку" in low
                or "доставленное количество" in low
                or "доставка в процессе" in low
                or "доставка" in low
            )
        )
        dump_debug(driver, debug_dir, "verify_delivery_stage")
        return {"ok": ok, "bodySample": body[:1500], "url": driver.current_url}
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def start_delivery_if_needed(env: dict[str, str], order_id: str, debug_dir: Path) -> dict[str, Any]:
    """
    v9: no longer relies only on g2g_bot.py start-delivery.
    It clicks the current G2G flow directly:
      Просмотреть сейчас / View now -> Start deliver / Начать доставку
    """
    pre = verify_delivery_stage(env, order_id, debug_dir)
    if pre.get("ok"):
        return {"ok": True, "already_started": True, "verify": pre}
    if pre.get("login_required"):
        return {"ok": False, "login_required": True, "verify": pre, "error": pre.get("error")}

    # ensure_proxy_wrapper(env)
    driver = make_driver(env, debug_dir)
    try:
        driver.get(order_url(env, order_id))
        time.sleep(3)

        if _is_login_page(driver):
            dump_debug(driver, debug_dir, "start_delivery_login_page")
            return {
                "ok": False,
                "login_required": True,
                "error": "G2G seller session is not logged in in browser_profile_g2g.",
                "url": driver.current_url,
                "bodySample": body_text(driver)[:1500],
            }

        wait_body_contains(driver, order_id.split("-", 1)[0], timeout=60)
        time.sleep(2)
        dump_debug(driver, debug_dir, "start_00_before_clicks")

        body0 = body_text(driver)
        low0 = body0.lower()

        steps: dict[str, Any] = {}

        # First click: view delivery details / view now.
        if ("просмотреть сейчас" in low0) or ("view now" in low0) or ("view delivery" in low0):
            steps["view_now"] = _click_best_text(driver, [
                "Просмотреть сейчас",
                "View now",
                "View delivery details",
                "Просмотреть детали доставки",
            ], timeout=20)
            time.sleep(3)
            dump_debug(driver, debug_dir, "start_01_after_view_now")
        else:
            steps["view_now"] = {"ok": False, "skipped": True, "reason": "view-now text not present"}

        # Second click: start deliver in modal/card.
        steps["start_deliver"] = _click_best_text(driver, [
            "Start deliver",
            "Ready to deliver",
            "Начать доставку",
            "Начать достав",
            "Начать",
        ], timeout=25)
        time.sleep(5)
        dump_debug(driver, debug_dir, "start_02_after_start_deliver")

        # Sometimes G2G needs a confirmation button after Start deliver.
        body1 = body_text(driver)
        low1 = body1.lower()
        if any(x in low1 for x in ["confirm", "подтверд", "ok", "да"]):
            steps["confirm_modal"] = _click_best_text(driver, [
                "Confirm",
                "Подтвердить",
                "Да",
                "OK",
                "ОК",
            ], timeout=8)
            time.sleep(4)
            dump_debug(driver, debug_dir, "start_03_after_confirm_modal")

        # Verify in the same driver first.
        body2 = body_text(driver)
        low2 = body2.lower()
        same_driver_ok = (
            "галерея доказательств" in low2
            and (
                "подтвердить доставку" in low2
                or "доставленное количество" in low2
                or "доставка в процессе" in low2
                or "доставка" in low2
            )
        )

        if same_driver_ok:
            return {
                "ok": True,
                "already_started": False,
                "method": "direct_view_now_start_deliver",
                "steps": steps,
                "url": driver.current_url,
                "bodySample": body2[:1500],
            }

        # Fallback old command, then re-verify.
        old = run_py(["g2g_bot.py", "start-delivery", order_id], env=env, timeout=300)
        post = verify_delivery_stage(env, order_id, debug_dir)
        return {
            "ok": bool(post.get("ok")),
            "already_started": False,
            "method": "direct_then_old_fallback",
            "steps": steps,
            "old_start_cmd": old,
            "verify": post,
            "bodySample": body2[:1500],
        }

    finally:
        try:
            driver.quit()
        except Exception:
            pass


# =============================================================================
# Direct G2G proof upload
# =============================================================================

def _wait_order_page_loaded(driver: webdriver.Chrome, order_id: str, timeout: int = 90) -> dict[str, Any]:
    """
    Wait until G2G order page has useful body text.
    Handles slow JS blank body by refresh retries.
    """
    end = time.time() + timeout
    short_order = order_id.split("-", 1)[0].lower()
    last = {}

    while time.time() < end:
        try:
            url = driver.current_url
            txt = body_text(driver)
            low = txt.lower()

            last = {"url": url, "bodySample": txt[:1500]}

            if _is_login_page(driver):
                return {"ok": False, "login_required": True, "state": last}

            if short_order in low or order_id.lower() in low:
                return {"ok": True, "state": last}

            # If the body is totally empty, G2G JS sometimes stuck; refresh once in a while.
            if not txt.strip():
                time.sleep(2)
                try:
                    driver.refresh()
                except Exception:
                    pass
                time.sleep(4)
            else:
                time.sleep(1)

        except Exception as e:
            last = {"error": str(e)}
            time.sleep(1)

    return {"ok": False, "error": "order page did not load useful body text", "state": last}


def _open_gallery_modal(driver: webdriver.Chrome, debug_dir: Path, idx: int) -> dict[str, Any]:
    """
    Open G2G proof gallery/upload modal.

    v13 improvements:
      - click exact selector with JS even if Selenium thinks it is not displayed;
      - click parent li/button when the visible text span itself is not enough;
      - try coordinate click on the visible "Галерея доказательств" text;
      - wait longer and probe for upload-modal-only markers.
    """
    # If upload modal is already open, do not click the tab again.
    try:
        if _modal_has_gallery(driver):
            return {"ok": True, "method": "already_open"}
    except Exception:
        pass

    # Make left action block predictable.
    try:
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)
    except Exception:
        pass

    exact_selectors = [
        "#delivery-details-actions-list > li:nth-child(1) > span > span",
        "#delivery-details-actions-list > li:nth-child(1)",
        "#delivery-details-actions-list li:nth-child(1)",
        "#delivery-details-actions-list span",
    ]

    attempts = []

    js_click_selector = r"""
        const sel = arguments[0];
        const els = Array.from(document.querySelectorAll(sel));
        function rectOf(e){ const r=e.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; }
        for (const el of els) {
          const txt = (el.innerText || el.textContent || '').trim();
          const low = txt.toLowerCase();
          if (sel.includes('delivery-details-actions-list') && txt && !low.includes('галерея') && !low.includes('proof') && !low.includes('доказ')) {
            continue;
          }
          const target = el.closest('li,button,[role="button"],span,div') || el;
          target.scrollIntoView({block:'center', inline:'center'});
          try { target.click(); } catch(e) {}
          try {
            target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
            target.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
            target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
          } catch(e) {}
          return {ok:true, selector:sel, text:txt, rect:rectOf(target), tag:target.tagName};
        }
        return {ok:false, selector:sel, count:els.length};
    """

    for sel in exact_selectors:
        try:
            res = driver.execute_script(js_click_selector, sel)
            attempts.append({"method": "js_selector", "result": res})
            time.sleep(3)
            dump_debug(driver, debug_dir, f"upload_{idx:02d}_after_gallery_selector")
            if _modal_has_gallery(driver):
                return {"ok": True, "method": "js_selector", "selector": sel, "attempts": attempts}
        except Exception as e:
            attempts.append({"method": "js_selector", "selector": sel, "error": str(e)})

    # Text click via general helper.
    clicked = _click_best_text(driver, [
        "Галерея доказательств",
        "Proof gallery",
        "Delivery proof gallery",
        "Доказательств",
    ], timeout=15)
    attempts.append({"method": "text_helper", "result": clicked})
    time.sleep(3)
    dump_debug(driver, debug_dir, f"upload_{idx:02d}_after_gallery_text")
    if clicked.get("ok") and _modal_has_gallery(driver):
        return {"ok": True, "method": "text", "click": clicked, "attempts": attempts}

    # Coordinate fallback: find the smallest visible text node and click center using CDP.
    js_find = r"""
        const nodes = Array.from(document.querySelectorAll('span,div,li,button,[role="button"]'));
        function visible(el) {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          return r.width > 2 && r.height > 2 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
        }
        const cands = [];
        for (const el of nodes) {
          if (!visible(el)) continue;
          const txt = (el.innerText || el.textContent || '').trim();
          const low = txt.toLowerCase();
          if (!(low.includes('галерея доказательств') || low.includes('proof gallery'))) continue;
          const r = el.getBoundingClientRect();
          cands.push({text:txt.slice(0,120), x:r.x+r.width/2, y:r.y+r.height/2, rect:{x:r.x,y:r.y,w:r.width,h:r.height}, area:r.width*r.height, tag:el.tagName});
        }
        cands.sort((a,b)=>a.area-b.area);
        return cands[0] || null;
    """
    try:
        cand = driver.execute_script(js_find)
        attempts.append({"method": "coord_find", "candidate": cand})
        if cand:
            x, y = int(cand["x"]), int(cand["y"])
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
            driver.execute_cdp_cmd("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
            time.sleep(3)
            dump_debug(driver, debug_dir, f"upload_{idx:02d}_after_gallery_coord")
            if _modal_has_gallery(driver):
                return {"ok": True, "method": "coordinate", "candidate": cand, "attempts": attempts}
    except Exception as e:
        attempts.append({"method": "coordinate", "error": str(e)})

    return {"ok": False, "error": "proof gallery modal did not open", "attempts": attempts, "modal_state": _probe_upload_modal(driver)}



def _extract_remaining_from_text(text: str) -> int | None:
    """
    G2G proof modal counter examples:
      Загрузить новое доказательство доставки: (осталось 149)
      Upload new delivery proof: (149 remaining)
    """
    t = text or ""
    patterns = [
        r"осталось\s*(\d+)",
        r"\((\d+)\s*remaining\)",
        r"remaining\s*(\d+)",
    ]
    for pat in patterns:
        m = re.search(pat, t, flags=re.I)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                pass
    return None


def _modal_has_gallery(driver: webdriver.Chrome) -> bool:
    try:
        txt = body_text(driver).lower()

        # IMPORTANT:
        # The order page itself always contains the tab text "Галерея доказательств".
        # That does NOT mean the upload modal is open.
        # Require modal-only markers.
        upload_markers = [
            "загрузить новое доказательство",
            "используйте перетаскивание",
            "осталось",
            "upload new",
            "delivery proof",
            "drag",
        ]
        if not any(x in txt for x in upload_markers):
            return False

        dialogs = driver.find_elements("css selector", ".q-dialog, [role='dialog'], .q-dialog__inner")
        if dialogs:
            return True

        return ("отправить" in txt or "send" in txt) and ("close" in txt or "clear" in txt or "add" in txt)
    except Exception:
        return False


def _probe_upload_modal(driver: webdriver.Chrome) -> dict[str, Any]:
    """
    Probe the ACTIVE upload dialog, not the whole page.

    G2G may keep several q-dialog wrappers in DOM. The whole body can contain two
    "Отправить" buttons and stale dialog text, so upload/send must target the dialog
    that contains upload markers and, after attach, preferably "clear".
    """
    try:
        js = r"""
            function visible(el) {
              const r = el.getBoundingClientRect();
              const st = getComputedStyle(el);
              return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
            }

            const rootsRaw = Array.from(document.querySelectorAll('.q-dialog, [role="dialog"], .q-dialog__inner, .q-card, body'));
            const roots = [];
            for (const root of rootsRaw) {
              const txt = (root.innerText || root.textContent || '').trim();
              const low = txt.toLowerCase();
              const uploadLike = low.includes('загрузить новое доказательство') || low.includes('осталось') || low.includes('upload new') || low.includes('delivery proof');
              if (!uploadLike && root.tagName.toLowerCase() !== 'body') continue;

              const inputs = Array.from(root.querySelectorAll('input[type="file"]'));
              const buttons = Array.from(root.querySelectorAll('button')).map(b => (b.innerText || b.textContent || '').trim()).filter(Boolean);
              const imgs = Array.from(root.querySelectorAll('img')).filter(visible).length;
              const hasClear = low.includes('clear') || low.includes('удалить');
              const hasValidation = low.includes('пожалуйста, загрузите хотя бы один файл') || low.includes('upload at least one');

              let score = 0;
              if (uploadLike) score += 100;
              if (hasClear) score += 1000;
              if (inputs.length) score += 50;
              if (buttons.some(b => b.toLowerCase().includes('отправить') || b.toLowerCase().includes('send'))) score += 50;
              if (root.tagName.toLowerCase() === 'body') score -= 200;

              const r = root.getBoundingClientRect ? root.getBoundingClientRect() : {x:0,y:0,width:0,height:0};
              roots.push({
                score,
                tag: root.tagName,
                className: root.className || '',
                text: txt.slice(0, 2000),
                fileInputCount: inputs.length,
                buttons,
                imgCount: imgs,
                hasClear,
                hasValidation,
                rect: {x:r.x||0,y:r.y||0,w:r.width||0,h:r.height||0}
              });
            }

            roots.sort((a,b) => b.score - a.score);
            const best = roots[0] || null;
            const body = document.body ? document.body.innerText : '';
            return {
              bodySample: body.slice(0, 1500),
              dialogCount: document.querySelectorAll('.q-dialog, [role="dialog"], .q-dialog__inner').length,
              fileInputCount: document.querySelectorAll('input[type="file"]').length,
              imgCount: Array.from(document.querySelectorAll('img')).filter(visible).length,
              bestRoot: best,
              rootText: best ? best.text : '',
              buttons: best ? best.buttons.slice(0, 20) : [],
              roots: roots.slice(0, 6)
            };
        """
        return driver.execute_script(js) or {}
    except Exception as e:
        return {"error": str(e)}


def _get_active_upload_root(driver: webdriver.Chrome):
    """
    Return WebElement for the active upload root.
    Prefer root that has upload markers + selected file marker 'clear'.
    """
    js = r"""
        function visible(el) {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
        }

        const rootsRaw = Array.from(document.querySelectorAll('.q-dialog, [role="dialog"], .q-dialog__inner, .q-card, body'));
        const scored = [];

        for (const root of rootsRaw) {
          const txt = (root.innerText || root.textContent || '').trim();
          const low = txt.toLowerCase();
          const uploadLike = low.includes('загрузить новое доказательство') || low.includes('осталось') || low.includes('upload new') || low.includes('delivery proof');
          if (!uploadLike && root.tagName.toLowerCase() !== 'body') continue;

          const inputs = Array.from(root.querySelectorAll('input[type="file"]'));
          const buttons = Array.from(root.querySelectorAll('button')).map(b => (b.innerText || b.textContent || '').trim()).filter(Boolean);
          const hasSend = buttons.some(b => b.toLowerCase().includes('отправить') || b.toLowerCase().includes('send'));
          const hasClear = low.includes('clear') || low.includes('удалить');

          let score = 0;
          if (uploadLike) score += 100;
          if (hasClear) score += 1000;
          if (inputs.length) score += 50;
          if (hasSend) score += 50;
          if (root.tagName.toLowerCase() === 'body') score -= 200;

          scored.push({root, score});
        }

        scored.sort((a,b) => b.score - a.score);
        return scored.length ? scored[0].root : null;
    """
    try:
        return driver.execute_script(js)
    except Exception:
        return None


def _click_plus_if_needed(driver: webdriver.Chrome) -> dict[str, Any]:
    """
    Click '+' in the active G2G uploader to create a fresh input[type=file].

    Important for qty > 1:
    after the first proof, G2G can keep stale hidden inputs/dialogs in DOM.
    We click the real uploader tile before every attach and then use the newest input.
    """
    selectors = [
        "#fileUploaderContainer .g-uploader > div",
        "#fileUploaderContainer .g-uploader",
        "#fileUploaderContainer i",
        "#fileUploaderContainer .q-icon",
        ".g-uploader .q-icon",
        ".g-uploader i",
        "[class*='uploader'] i",
        "[class*='uploader'] .q-icon",
        "div[role='button'] i",
    ]

    # Prefer elements inside the active upload root.
    roots = []
    try:
        root = _get_active_upload_root(driver)
        if root:
            roots.append(root)
    except Exception:
        pass
    roots.append(driver)

    attempts = []

    for root in roots:
        for sel in selectors:
            try:
                els = root.find_elements("css selector", sel)
                for el in els:
                    try:
                        html = (el.get_attribute("outerHTML") or "").lower()
                        txt = (el.text or "").strip().lower()
                        visible = el.is_displayed()
                    except Exception:
                        continue

                    if not visible:
                        # Hidden input/icon may still be clickable through parent; skip only if no parent clue.
                        pass

                    looks_plus = (
                        txt in {"add", "+", ""}
                        or any(x in html for x in ["add", "plus", "mdi-plus", "q-icon", "material-icons"])
                    )
                    if not looks_plus:
                        continue

                    try:
                        target = driver.execute_script(
                            "return arguments[0].closest('button,[role=\"button\"],.g-uploader,div') || arguments[0];",
                            el,
                        )
                    except Exception:
                        target = el

                    try:
                        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", target)
                        time.sleep(0.2)
                        driver.execute_script("arguments[0].click();", target)
                        time.sleep(1.0)
                        return {"ok": True, "selector": sel, "text": txt, "html": html[:300]}
                    except Exception as e:
                        attempts.append({"selector": sel, "error": str(e), "text": txt, "html": html[:160]})
            except Exception as e:
                attempts.append({"selector": sel, "error": str(e)})

    clicked = _click_best_text(driver, ["add", "+"], timeout=3)
    return {"ok": bool(clicked.get("ok")), "click": clicked, "attempts": attempts[-10:]}


def _attach_file_to_modal(driver: webdriver.Chrome, file_path: str, debug_dir: Path, idx: int) -> dict[str, Any]:
    """
    Attach a proof file into the active G2G upload modal.

    v16:
      - always clicks '+' first to create a fresh slot/input;
      - uses the last input[type=file] from the active upload root;
      - verifies selected-file marker inside the active root, not whole body.
    """
    fp = str(Path(file_path).resolve())
    if not Path(fp).exists():
        return {"ok": False, "error": f"proof file not found: {fp}"}

    last_error = ""
    plus_logs = []

    # Key change for 2nd/3rd files.
    plus_logs.append(_click_plus_if_needed(driver))
    time.sleep(1.5)

    for attempt in range(4):
        try:
            root = _get_active_upload_root(driver)
            if root:
                inputs = root.find_elements("css selector", "input[type='file']")
            else:
                inputs = driver.find_elements("css selector", "input[type='file']")

            inputs = list(inputs or [])
            if inputs:
                inp = inputs[-1]
                driver.execute_script("""
                    arguments[0].style.display='block';
                    arguments[0].style.visibility='visible';
                    arguments[0].style.opacity='1';
                    arguments[0].removeAttribute('disabled');
                    arguments[0].style.position='fixed';
                    arguments[0].style.zIndex='99999';
                    arguments[0].style.height='50px';
                    arguments[0].style.width='300px';
                """, inp)
                inp.send_keys(fp)
                time.sleep(5)
                dump_debug(driver, debug_dir, f"upload_{idx:02d}_after_sendkeys_attempt_{attempt}")

                state = _probe_upload_modal(driver)
                root_text = ((state.get("bestRoot") or {}).get("text") or state.get("rootText") or "").lower()
                body_text_sample = (state.get("bodySample") or "").lower()
                file_name_part = Path(fp).name[:10].lower()

                selected = (
                    "clear" in root_text
                    or "очистить" in root_text
                    or (state.get("bestRoot") or {}).get("hasClear")
                    or file_name_part in root_text
                    or file_name_part in body_text_sample
                )

                if selected:
                    return {
                        "ok": True,
                        "method": "send_keys_fresh_plus_last_input",
                        "inputCount": len(inputs),
                        "plus_logs": plus_logs,
                        "state": state,
                    }

        except Exception as e:
            last_error = str(e)

        # If no selected marker, click plus again and retry.
        try:
            plus_logs.append(_click_plus_if_needed(driver))
            time.sleep(1.2)
        except Exception as e:
            plus_logs.append({"ok": False, "error": str(e)})

    return {
        "ok": False,
        "error": f"file input attach failed after retries: {last_error}",
        "plus_logs": plus_logs,
        "state": _probe_upload_modal(driver),
    }


def _click_modal_send(driver: webdriver.Chrome, debug_dir: Path, idx: int) -> dict[str, Any]:
    before_state = _probe_upload_modal(driver)
    before_text = ((before_state.get("bestRoot") or {}).get("text") or before_state.get("rootText") or before_state.get("bodySample") or "")
    before_remaining = _extract_remaining_from_text(before_text)

    js = r"""
        function visible(el) {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
        }

        const rootsRaw = Array.from(document.querySelectorAll('.q-dialog, [role="dialog"], .q-dialog__inner, .q-card, body'));
        const roots = [];
        for (const root of rootsRaw) {
          const txt = (root.innerText || root.textContent || '').trim();
          const low = txt.toLowerCase();
          const uploadLike = low.includes('загрузить новое доказательство') || low.includes('осталось') || low.includes('upload new') || low.includes('delivery proof');
          if (!uploadLike && root.tagName.toLowerCase() !== 'body') continue;

          const buttons = Array.from(root.querySelectorAll('button, [role="button"]')).filter(visible);
          const hasSend = buttons.some(b => ((b.innerText || b.textContent || '').toLowerCase().includes('отправить') || (b.innerText || b.textContent || '').toLowerCase().includes('send')));
          if (!hasSend) continue;

          const hasClear = low.includes('clear') || low.includes('удалить');
          let score = 0;
          if (uploadLike) score += 100;
          if (hasClear) score += 1000;       // THIS is the selected-file dialog
          if (root.querySelectorAll('input[type="file"]').length) score += 50;
          if (root.tagName.toLowerCase() === 'body') score -= 200;

          roots.push({root, score, text: txt.slice(0, 600), hasClear});
        }

        roots.sort((a,b) => b.score - a.score);
        if (!roots.length) return {ok:false, error:'no upload root with send button'};

        const root = roots[0].root;
        const buttons = Array.from(root.querySelectorAll('button, [role="button"]')).filter(visible);
        let candidates = [];

        for (const b of buttons) {
          const txt = (b.innerText || b.textContent || '').trim();
          const low = txt.toLowerCase();
          if (!(low.includes('отправить') || low.includes('send') || low.includes('submit'))) continue;
          const r = b.getBoundingClientRect();
          let score = r.width * r.height;
          if (b.tagName.toLowerCase() === 'button') score -= 100000;
          candidates.push({el:b, text:txt, rect:{x:r.x,y:r.y,w:r.width,h:r.height}, score});
        }

        candidates.sort((a,b) => a.score - b.score);
        if (!candidates.length) return {ok:false, error:'send button not found in selected upload root', rootText: roots[0].text};

        const c = candidates[0];
        c.el.scrollIntoView({block:'center', inline:'center'});
        try { c.el.click(); } catch(e) {}
        try {
          c.el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
          c.el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
          c.el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
        } catch(e) {}

        return {
          ok:true,
          clicked:{text:c.text, rect:c.rect, score:c.score},
          rootHasClear: roots[0].hasClear,
          rootText: roots[0].text,
          candidates:candidates.slice(0,10).map(x=>({text:x.text, rect:x.rect, score:x.score}))
        };
    """

    try:
        res = driver.execute_script(js) or {}
    except Exception as e:
        res = {"ok": False, "error": str(e)}

    final_state = {}
    final_modal = True
    after_remaining = None
    validation = False

    for _ in range(25):
        time.sleep(1)
        final_state = _probe_upload_modal(driver)
        final_text = ((final_state.get("bestRoot") or {}).get("text") or final_state.get("rootText") or final_state.get("bodySample") or "")
        final_low = final_text.lower()
        final_modal = _modal_has_gallery(driver)
        after_remaining = _extract_remaining_from_text(final_text)
        validation = "пожалуйста, загрузите хотя бы один файл" in final_low or "upload at least one" in final_low

        if validation:
            break
        if not final_modal:
            break
        if before_remaining is not None and after_remaining is not None and after_remaining < before_remaining:
            break

    dump_debug(driver, debug_dir, f"upload_{idx:02d}_after_send_click")

    before_root = before_state.get("bestRoot") or {}
    final_root = final_state.get("bestRoot") or {}

    before_had_clear = bool(before_root.get("hasClear")) or ("clear" in (before_state.get("rootText") or "").lower())
    final_has_clear = bool(final_root.get("hasClear")) or ("clear" in (final_state.get("rootText") or "").lower())

    # G2G behavior seen in logs:
    # - after attach, the counter can already become 148 and "clear" is visible;
    # - after clicking Send, the modal may remain open, counter stays 148, but "clear" disappears.
    # That means the selected file was accepted/submitted. v16 falsely failed because it
    # required the modal to close or the counter to decrease AFTER click.
    accepted_because_clear_disappeared = before_had_clear and not final_has_clear and not validation
    accepted_because_counter_decreased = (
        before_remaining is not None and after_remaining is not None and after_remaining < before_remaining
    )
    accepted_because_modal_closed = not final_modal

    ok = bool(res.get("ok")) and not validation and (
        accepted_because_modal_closed
        or accepted_because_counter_decreased
        or accepted_because_clear_disappeared
    )

    return {
        "ok": ok,
        "click": res,
        "modalStillOpen": final_modal,
        "validationError": validation,
        "beforeRemaining": before_remaining,
        "afterRemaining": after_remaining,
        "beforeHadClear": before_had_clear,
        "finalHasClear": final_has_clear,
        "acceptedBecauseClearDisappeared": accepted_because_clear_disappeared,
        "acceptedBecauseCounterDecreased": accepted_because_counter_decreased,
        "acceptedBecauseModalClosed": accepted_because_modal_closed,
        "state": final_state,
    }


def direct_upload_one_proof(env: dict[str, str], order_id: str, file_path: str, idx: int, debug_root: Path) -> dict[str, Any]:
    """
    Direct upload that uses the same browser logic as the auto script.
    This avoids the old g2g_upload_proof_strict_v3_19.py failure where it opened
    the correct URL but saw an empty body.
    """
    debug_dir = safe_mkdir(debug_root / "g2g_upload_direct_v10")
    # ensure_proxy_wrapper(env)
    driver = make_driver(env, debug_dir)
    try:
        target = order_url(env, order_id)
        driver.get(target)
        load = _wait_order_page_loaded(driver, order_id, timeout=90)
        dump_debug(driver, debug_dir, f"upload_{idx:02d}_00_page_loaded")
        if not load.get("ok"):
            return {"ok": False, "uploaded": 0, "error": "order page did not load for direct upload", "load": load, "target_url": target}

        body = body_text(driver).lower()
        if "доставка" not in body and "delivery" not in body:
            return {"ok": False, "uploaded": 0, "error": "order is not in delivery page/state", "bodySample": body[:1500]}

        gallery = _open_gallery_modal(driver, debug_dir, idx)
        if not gallery.get("ok"):
            return {"ok": False, "uploaded": 0, "error": "proof gallery did not open", "gallery": gallery}

        attach = _attach_file_to_modal(driver, file_path, debug_dir, idx)
        if not attach.get("ok"):
            return {"ok": False, "uploaded": 0, "error": "proof file was not attached", "gallery": gallery, "attach": attach}

        send = _click_modal_send(driver, debug_dir, idx)
        if not send.get("ok"):
            return {"ok": False, "uploaded": 0, "error": "send proof button click/verify failed", "gallery": gallery, "attach": attach, "send": send}

        return {
            "ok": True,
            "uploaded": 1,
            "order_id": order_id,
            "file": str(file_path),
            "debug_dir": str(debug_dir),
            "gallery": gallery,
            "attach": attach,
            "send": send,
        }
    finally:
        try:
            driver.quit()
        except Exception:
            pass

def direct_upload_proofs_sequence(env: dict[str, str], order_id: str, file_paths: list[str], debug_root: Path) -> dict[str, Any]:
    """
    Upload all remaining proofs in one browser session.

    This is the important qty>1 fix:
      - open gallery once;
      - for each file click '+' and attach using the newest input;
      - click 'Отправить' once at the end in the selected-file dialog.
    """
    debug_dir = safe_mkdir(debug_root / "g2g_upload_direct_v16_sequence")
    # ensure_proxy_wrapper(env)
    driver = make_driver(env, debug_dir)

    attached_files = []
    attach_results = []

    try:
        target = order_url(env, order_id)
        driver.get(target)
        load = _wait_order_page_loaded(driver, order_id, timeout=90)
        dump_debug(driver, debug_dir, "seq_00_page_loaded")
        if not load.get("ok"):
            return {"ok": False, "uploaded": 0, "error": "order page did not load", "load": load, "debug_dir": str(debug_dir)}

        if not _modal_has_gallery(driver):
            gallery = _open_gallery_modal(driver, debug_dir, 1)
            if not gallery.get("ok"):
                return {"ok": False, "uploaded": 0, "error": "gallery did not open", "gallery": gallery, "debug_dir": str(debug_dir)}
        else:
            gallery = {"ok": True, "method": "already_open"}

        time.sleep(2)
        dump_debug(driver, debug_dir, "seq_01_gallery_open")

        for idx, fp in enumerate(file_paths, 1):
            fp_path = Path(fp)
            if not fp_path.exists():
                return {
                    "ok": False,
                    "uploaded": len(attached_files),
                    "error": f"File not found: {fp_path}",
                    "files": attached_files,
                    "attach_results": attach_results,
                    "debug_dir": str(debug_dir),
                }

            attach = _attach_file_to_modal(driver, str(fp_path), debug_dir, idx)
            attach_results.append(attach)
            dump_debug(driver, debug_dir, f"seq_{idx:02d}_attached")

            if not attach.get("ok"):
                return {
                    "ok": False,
                    "uploaded": len(attached_files),
                    "error": f"attach failed for file {idx}",
                    "gallery": gallery,
                    "attach": attach,
                    "files": attached_files,
                    "attach_results": attach_results,
                    "debug_dir": str(debug_dir),
                }

            attached_files.append(str(fp_path))
            time.sleep(1.5)

        send = _click_modal_send(driver, debug_dir, len(file_paths))
        dump_debug(driver, debug_dir, "seq_after_final_send")

        ok = bool(send.get("ok"))

        # Extra verification. If the modal remains open but the remaining counter is now 148,
        # the second proof is already accepted for a qty=2 order.
        final_probe = _probe_upload_modal(driver)
        final_text = ((final_probe.get("bestRoot") or {}).get("text") or final_probe.get("rootText") or final_probe.get("bodySample") or "")
        final_remaining = _extract_remaining_from_text(final_text)

        if not ok:
            if send.get("validationError"):
                ok = False
            elif not _modal_has_gallery(driver):
                ok = True
            elif send.get("acceptedBecauseClearDisappeared"):
                ok = True
            elif final_remaining is not None and final_remaining <= 148:
                ok = True

        return {
            "ok": ok,
            "uploaded": len(attached_files) if ok else 0,
            "files": attached_files,
            "gallery": gallery,
            "attach_results": attach_results,
            "send": send,
            "finalProbe": final_probe,
            "finalRemaining": final_remaining,
            "debug_dir": str(debug_dir),
            "error": "" if ok else "final Send did not verify",
        }

    finally:
        try:
            driver.quit()
        except Exception:
            pass


def direct_check_uploaded_proof_count(env: dict[str, str], order_id: str, qty: int, debug_root: Path) -> dict[str, Any]:
    """
    Open proof gallery and estimate how many proofs are already uploaded.
    G2G uses 'осталось 150/149/148...' counter.
    For this product flow, starting limit is 150, so uploaded ~= 150 - remaining.
    """
    debug_dir = safe_mkdir(debug_root / "g2g_upload_direct_v12_check")
    # ensure_proxy_wrapper(env)
    driver = make_driver(env, debug_dir)
    try:
        target = order_url(env, order_id)
        driver.get(target)
        load = _wait_order_page_loaded(driver, order_id, timeout=90)
        dump_debug(driver, debug_dir, "check_00_page_loaded")
        if not load.get("ok"):
            return {"ok": False, "error": "order page did not load", "load": load}

        gallery = _open_gallery_modal(driver, debug_dir, 0)
        if not gallery.get("ok"):
            return {"ok": False, "error": "gallery did not open", "gallery": gallery}

        time.sleep(2)
        state = _probe_upload_modal(driver)
        remaining = _extract_remaining_from_text(state.get("bodySample", ""))
        uploaded_estimate = None
        enough = False
        if remaining is not None:
            uploaded_estimate = max(0, 150 - int(remaining))
            enough = uploaded_estimate >= int(qty)

        dump_debug(driver, debug_dir, "check_01_gallery_open")
        return {
            "ok": True,
            "remaining": remaining,
            "uploaded_estimate": uploaded_estimate,
            "enough": enough,
            "qty": qty,
            "gallery": gallery,
            "state": state,
        }
    finally:
        try:
            driver.quit()
        except Exception:
            pass




# =============================================================================
# Direct G2G chat send
# =============================================================================

def build_delivery_message(order_id: str, login: str, password: str) -> str:
    return (
        f"Delivery-ID: G2G-{order_id}\n"
        f"Order: {order_id}\n\n"
        "Hello! Thank you for your purchase.\n"
        "Here is your account/data:\n\n"
        f"🌐 Login: {login}   🔒 Password: {password}\n\n"
        "--- GUIDE ---\n"
        "Hello!\n"
        "Thank you for your purchase.\n"
        "You received Twitch account credentials for claiming the purchased Drops.\n"
        "CAREFULLY COPY THE USERNAME AND PASSWORD, WITHOUT SPACES!!!!\n"
        "https://saydis.pro/rust/en / <-- THIS IS AN INSTRUCTION! DON'T IGNORE HER, OTHERWISE YOU WON'T GET THE SKINS!!!!!!!!! "
        "YOU NEED TO CONNECT STEAM FIRST, AND THEN ONLY TWITCH!!!! THEN CLICK THE \"CHECK FOR MISSED DROPS\" BUTTON!!!! THEN RE-ENTER THE GAME!!!\n"
        "Important:\n"
        "- Rewards can usually be claimed only once per Rust account.\n"
        "- Please record a continuous video from payment to checking inventory in case you need support."
    )


def _chat_state(driver: webdriver.Chrome) -> dict[str, Any]:
    try:
        js = r"""
            function visible(el) {
              const r = el.getBoundingClientRect();
              const st = getComputedStyle(el);
              return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
            }
            const body = document.body ? document.body.innerText : '';
            const editors = Array.from(document.querySelectorAll(
              '[contenteditable="true"], .toastui-editor-contents, .toastui-editor-ww-container div[contenteditable="true"], .ProseMirror, textarea'
            )).filter(visible);
            const inputs = Array.from(document.querySelectorAll('input, textarea')).filter(visible).map(e => ({
              tag:e.tagName, type:e.getAttribute('type')||'', placeholder:e.getAttribute('placeholder')||'', text:e.value||''
            }));
            const sendBtns = Array.from(document.querySelectorAll('button, [role="button"], i')).filter(visible).map(e => {
              const r=e.getBoundingClientRect();
              return {tag:e.tagName, text:(e.innerText||e.textContent||'').trim(), cls:e.className||'', rect:{x:r.x,y:r.y,w:r.width,h:r.height}};
            }).filter(x => (x.text||x.cls||'').toLowerCase().includes('send') || (x.text||x.cls||'').toLowerCase().includes('отправ') || (x.text||x.cls||'').toLowerCase().includes('q-icon'));
            return {url: location.href, body: body.slice(0,2500), editorCount: editors.length, inputs: inputs.slice(0,20), sendBtns: sendBtns.slice(0,20)};
        """
        return driver.execute_script(js) or {}
    except Exception as e:
        return {"error": str(e), "url": getattr(driver, "current_url", "")}


def _find_chat_editor(driver: webdriver.Chrome):
    js = r"""
        function visible(el) {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
        }
        const selectors = [
          '[contenteditable="true"]',
          '.toastui-editor-contents',
          '.toastui-editor-ww-container div[contenteditable="true"]',
          '.ProseMirror',
          'textarea'
        ];
        let candidates = [];
        for (const sel of selectors) {
          for (const el of Array.from(document.querySelectorAll(sel))) {
            if (!visible(el)) continue;
            const r = el.getBoundingClientRect();
            candidates.push({el, area:r.width*r.height, text:(el.innerText||el.textContent||el.value||'').slice(0,200), tag:el.tagName});
          }
        }
        candidates.sort((a,b) => b.area - a.area);
        return candidates.length ? candidates[0].el : null;
    """
    try:
        return driver.execute_script(js)
    except Exception:
        return None


def _insert_chat_message(driver: webdriver.Chrome, message: str) -> dict[str, Any]:
    editor = _find_chat_editor(driver)
    if not editor:
        return {"ok": False, "error": "chat editor not found", "state": _chat_state(driver)}

    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].focus();", editor)
        time.sleep(0.3)
        # Use JS events because ToastUI may ignore plain .send_keys on nested <p>.
        driver.execute_script("""
            const el = arguments[0];
            const text = arguments[1];
            el.focus();
            if (el.tagName.toLowerCase() === 'textarea') {
              el.value = text;
            } else {
              el.innerText = text;
            }
            el.dispatchEvent(new InputEvent('input', {bubbles:true, inputType:'insertText', data:text}));
            el.dispatchEvent(new Event('change', {bubbles:true}));
        """, editor, message)
        time.sleep(1)
        return {"ok": True, "state": _chat_state(driver)}
    except Exception as e:
        return {"ok": False, "error": str(e), "state": _chat_state(driver)}


def _click_chat_send(driver: webdriver.Chrome) -> dict[str, Any]:
    js = r"""
        function visible(el) {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
        }

        const nodes = Array.from(document.querySelectorAll('button, [role="button"], i, .g-send-btn button'));
        const cands = [];
        for (const el of nodes) {
          if (!visible(el)) continue;
          const txt = (el.innerText || el.textContent || '').trim();
          const cls = String(el.className || '');
          const low = (txt + ' ' + cls).toLowerCase();
          const r = el.getBoundingClientRect();
          let score = r.width*r.height;

          if (low.includes('send') || low.includes('отправ')) score -= 100000;
          if (cls.includes('g-send-btn')) score -= 100000;
          if (el.closest('.g-send-btn')) score -= 100000;
          if (r.width > 80 && r.height > 20) score -= 1000;

          // Prefer bottom-right visible controls.
          score -= (r.y / 10);
          score -= (r.x / 20);

          if (low.includes('send') || low.includes('отправ') || el.closest('.g-send-btn')) {
            cands.push({el, text:txt, cls, score, rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
          }
        }
        cands.sort((a,b)=>a.score-b.score);
        if (!cands.length) return {ok:false, error:'send button not found'};
        const c = cands[0];
        c.el.scrollIntoView({block:'center', inline:'center'});
        try { c.el.click(); } catch(e) {}
        try {
          c.el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
          c.el.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
          c.el.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
        } catch(e) {}
        return {ok:true, clicked:{text:c.text, cls:c.cls, rect:c.rect, score:c.score}, candidates:cands.slice(0,8).map(x=>({text:x.text, cls:x.cls, rect:x.rect, score:x.score}))};
    """
    try:
        res = driver.execute_script(js) or {}
    except Exception as e:
        res = {"ok": False, "error": str(e)}
    time.sleep(3)
    return res


def _click_chat_conversation(driver: webdriver.Chrome, order_id: str, buyer: str = "") -> dict[str, Any]:
    terms = [order_id.lower(), order_id.split("-", 1)[0].lower()]
    if buyer:
        terms.append(buyer.lower())

    js = r"""
        const terms = arguments[0];
        function visible(el) {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
        }
        const nodes = Array.from(document.querySelectorAll('div,li,a,span,[role="button"]')).filter(visible);
        const cands = [];
        for (const el of nodes) {
          const txt = (el.innerText || el.textContent || '').trim();
          if (!txt) continue;
          const low = txt.toLowerCase();
          if (!terms.some(t => t && low.includes(t))) continue;
          const r = el.getBoundingClientRect();
          let area = r.width*r.height;
          if (area > 250000) area += 1000000;
          cands.push({el, text:txt.slice(0,400), area, rect:{x:r.x,y:r.y,w:r.width,h:r.height}, tag:el.tagName, cls:el.className||''});
        }
        cands.sort((a,b)=>a.area-b.area);
        if (!cands.length) return {ok:false, terms, error:'conversation candidate not found'};
        const c = cands[0];
        const target = c.el.closest('a,li,[role="button"],div') || c.el;
        target.scrollIntoView({block:'center', inline:'center'});
        try { target.click(); } catch(e) {}
        target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
        return {ok:true, clicked:{text:c.text, rect:c.rect, tag:c.tag, cls:c.cls}, count:cands.length};
    """
    try:
        return driver.execute_script(js, terms) or {}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def direct_send_chat_one(env: dict[str, str], order_id: str, login: str, password: str, buyer: str, debug_root: Path, idx: int) -> dict[str, Any]:
    debug_dir = safe_mkdir(debug_root / "g2g_chat_direct_v15")
    # ensure_proxy_wrapper(env)
    driver = make_driver(env, debug_dir)
    message = build_delivery_message(order_id, login, password)

    try:
        urls = [
            f"https://www.g2g.com/chat/#/order-item/{order_id.lower()}",
            f"https://www.g2g.com/chat/#/order-item/{order_id}",
            "https://www.g2g.com/chat/#/",
        ]

        states = []
        for u in urls:
            driver.get(u)
            time.sleep(8)
            dump_debug(driver, debug_dir, f"chat_{idx:02d}_opened")
            st = _chat_state(driver)
            states.append({"url": u, "state": st})

            if _find_chat_editor(driver):
                break

            # If route did not select conversation, try clicking order/buyer in chat list.
            click_conv = _click_chat_conversation(driver, order_id, buyer)
            states.append({"click_conversation": click_conv})
            time.sleep(5)
            if _find_chat_editor(driver):
                break

        ins = _insert_chat_message(driver, message)
        if not ins.get("ok"):
            return {"ok": False, "error": "Chat editor not found after direct route/click.", "states": states, "insert": ins, "debug_dir": str(debug_dir)}

        send = _click_chat_send(driver)
        dump_debug(driver, debug_dir, f"chat_{idx:02d}_after_send")
        if not send.get("ok"):
            return {"ok": False, "error": "Chat send button not found/clicked.", "states": states, "insert": ins, "send": send, "debug_dir": str(debug_dir), "message_preview": message[:500]}

        # Verify the sent message appears in chat body.
        time.sleep(3)
        body = body_text(driver)
        ok = (login in body) and (password in body)
        return {
            "ok": ok,
            "order_id": order_id,
            "login": login,
            "debug_dir": str(debug_dir),
            "message_preview": message[:500],
            "states": states[-4:],
            "insert": ins,
            "send": send,
            "verify": {"login_in_body": login in body, "password_in_body": password in body, "bodySample": body[-2000:]},
            "error": "" if ok else "Message send was clicked, but login/password not found in chat body after send.",
        }
    finally:
        try:
            driver.quit()
        except Exception:
            pass



# =============================================================================
# Stock / account / proof helpers
# =============================================================================

def parse_accounts_from_prepare_output(text: str) -> list[dict[str, str]]:
    accounts = []
    for m in LOGIN_LINE_RE.finditer(text or ""):
        login = m.group(1).strip()
        password = m.group(2).strip()
        if login and password:
            accounts.append({"login": login, "password": password})

    seen = set()
    out = []
    for a in accounts:
        key = (a["login"], a["password"])
        if key not in seen:
            seen.add(key)
            out.append(a)
    return out


def db_accounts_for_order(env: dict[str, str], order_id: str, limit: int) -> list[dict[str, str]]:
    db_path = Path(env.get("DATA_DIR", "data_g2g")) / "app.db"
    if not db_path.exists():
        return []

    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.execute(
                "SELECT data FROM stock WHERE order_id=? AND status='reserved' ORDER BY id LIMIT ?",
                (order_id, int(limit)),
            )
            rows = [r[0] for r in cur.fetchall()]
    except Exception:
        return []

    accounts = []
    for data in rows:
        if ":" in data:
            login, password = data.split(":", 1)
            accounts.append({"login": login.strip(), "password": password.strip()})
    return [a for a in accounts if a.get("login") and a.get("password")]


def mark_db_sold(env: dict[str, str], order_id: str) -> dict[str, Any]:
    db_path = Path(env.get("DATA_DIR", "data_g2g")) / "app.db"
    if not db_path.exists():
        return {"ok": False, "error": f"DB not found: {db_path}"}

    try:
        with sqlite3.connect(db_path) as conn:
            c = conn.cursor()
            c.execute("UPDATE stock SET status='sold', used_at=? WHERE order_id=? AND status IN ('reserved','available')", (now_ms(), order_id))
            stock_rows = c.rowcount
            try:
                c.execute("UPDATE orders SET status='delivered', delivered=1, updated_at=? WHERE order_id=?", (now_ms(), order_id))
                order_rows = c.rowcount
            except Exception:
                order_rows = 0
            conn.commit()
        return {"ok": True, "stock_rows": stock_rows, "order_rows": order_rows}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def safe_filename_part(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:60] or "account"


def make_twitch_proof_for_account(env: dict[str, str], order_id: str, offer_id: str, idx: int, total: int, account: dict[str, str]) -> dict[str, Any]:
    """
    v8: run every Twitch proof in a fresh subprocess.

    Why:
      In multi-account orders, the first proof can succeed, then the second proof can fail with
      "Twitch login fields not found" because browser/class state is stale. A fresh Python
      process per account avoids that.
    """
    helper = "g2g_make_twitch_proof_one_strict_v1.py"
    if not Path(helper).exists():
        return {"ok": False, "error": f"Missing helper file: {helper}"}

    child_env = {**env}
    child_env["PYTHONIOENCODING"] = "utf-8"
    child_env["PYTHONUTF8"] = "1"
    child_env["TWITCH_CLEAR_PROFILE_EACH_RUN"] = "1"
    child_env["TWITCH_PROOF_KEEP_BROWSER_OPEN"] = "0"

    res = run_py(
        [helper, order_id, offer_id, account["login"], account["password"], str(idx)],
        env=child_env,
        timeout=900,
    )

    out = res.get("json") or {}
    if not out:
        return {
            "ok": False,
            "error": "helper returned no JSON",
            "stdout": res.get("stdout", ""),
            "stderr": res.get("stderr", ""),
            "returncode": res.get("returncode"),
        }

    if res.get("returncode") != 0 or not out.get("ok"):
        return {
            "ok": False,
            "error": out.get("error", "helper failed"),
            "helper_json": out,
            "stdout": res.get("stdout", ""),
            "stderr": res.get("stderr", ""),
            "returncode": res.get("returncode"),
        }

    return out


# =============================================================================
# Proof uniqueness helpers
# =============================================================================

def _proof_file_fingerprint(path: str) -> tuple[int, str]:
    """
    Lightweight fingerprint: file size + first 1MB SHA256.
    Enough to catch duplicated proof paths/content.
    """
    import hashlib
    p = Path(path)
    if not p.exists():
        return (-1, "")
    h = hashlib.sha256()
    with p.open("rb") as f:
        h.update(f.read(1024 * 1024))
    return (p.stat().st_size, h.hexdigest())


def _select_distinct_proofs_for_accounts(rec: dict[str, Any], accounts: list[dict[str, str]], qty: int) -> tuple[list[str], list[str]]:
    """
    Return proof paths in the same order as accounts.
    Detect bad state where multiple accounts point to one proof file.

    Old failed runs can leave:
      rec["proofs"] = [
        {"login": account1, "proof_path": "...01_twitch_inventory_proof.png"},
        {"login": account2, "proof_path": "...01_twitch_inventory_proof.png"}
      ]
    which makes the uploader send the same screenshot twice.
    """
    problems: list[str] = []
    selected: list[str] = []
    by_login: dict[str, dict[str, Any]] = {}

    for pr in rec.get("proofs", []) or []:
        if not pr.get("ok"):
            continue
        login = str(pr.get("login") or "").strip()
        proof_path = str(pr.get("proof_path") or "").strip()
        if login and proof_path:
            by_login[login] = pr

    seen_paths: set[str] = set()
    seen_fps: set[tuple[int, str]] = set()

    for i, acc in enumerate(accounts[:qty], 1):
        login = acc.get("login", "")
        pr = by_login.get(login)
        if not pr:
            problems.append(f"missing proof object for account {i}/{qty}: {login}")
            continue

        pp = str(pr.get("proof_path") or "")
        p = Path(pp)
        if not pp or not p.exists():
            problems.append(f"proof file missing for account {i}/{qty}: {login}: {pp}")
            continue

        norm = str(p.resolve()).lower()
        fp = _proof_file_fingerprint(str(p))

        if norm in seen_paths:
            problems.append(f"duplicate proof path for account {i}/{qty}: {login}: {pp}")
            continue

        if fp in seen_fps:
            problems.append(f"duplicate proof content for account {i}/{qty}: {login}: {pp}")
            continue

        # The helper-created good names contain the login. Generic legacy names are suspicious
        # for multi-qty orders because they are often overwritten/reused.
        if qty > 1 and login.lower() not in p.name.lower():
            problems.append(f"suspicious generic proof filename for account {i}/{qty}: {login}: {p.name}")
            continue

        seen_paths.add(norm)
        seen_fps.add(fp)
        selected.append(str(p))

    return selected, problems


def _regenerate_all_proofs_for_accounts(env: dict[str, str], rec: dict[str, Any], order_id: str, offer_id: str, accounts: list[dict[str, str]], qty: int) -> list[dict[str, Any]]:
    """
    Regenerate proofs for every account to guarantee one distinct proof per login.
    Used when state has duplicate/generic proof paths.
    """
    proofs = []
    for i, acc in enumerate(accounts[:qty], 1):
        print(f"Regenerating proof {i}/{qty}: {acc['login']}")
        pr = make_twitch_proof_for_account(env, order_id, offer_id, i, qty, acc)
        print(json.dumps(pr, ensure_ascii=False, indent=2))
        proofs.append(pr)
        if not pr.get("ok"):
            raise RuntimeError(f"proof regeneration failed for {acc['login']}: {pr.get('error')}")
    rec["proofs"] = proofs
    rec["proof_made"] = True
    rec["proof_made_at"] = now_ms()
    # Important: proof files changed, so old upload/chat state is not trustworthy.
    rec["uploaded_proofs"] = []
    rec["proof_uploaded"] = False
    return proofs


def _parse_extra_upload_indexes(raw: str, qty: int) -> list[int]:
    """
    1-based indexes from --upload-extra-indexes.
    Empty means all 1..qty.
    """
    raw = (raw or "").strip()
    if not raw:
        return list(range(1, qty + 1))
    out = []
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            n = int(part)
            if 1 <= n <= qty and n not in out:
                out.append(n)
        except Exception:
            pass
    return out or list(range(1, qty + 1))



# =============================================================================
# Chat proofs + combined data message
# =============================================================================

def build_delivery_message_parts(order_id: str, accounts: list[dict[str, str]]) -> list[str]:
    # G2G chat often collapses line breaks inside one long message, so we send short messages.
    # Account credentials are formatted in the same pretty style as stock.
    parts: list[str] = []

    parts.append(
        "\n".join([
            f"Delivery-ID: G2G-{order_id}",
            f"Order: {order_id}",
            "",
            "Hello! Thank you for your purchase.",
            "Here is your account/data:",
        ])
    )

    total = len(accounts)
    for i, acc in enumerate(accounts, 1):
        login = str(acc.get("login", "")).strip()
        password = str(acc.get("password", "")).strip()

        parts.append(
            "\n".join([
                f"ACCOUNT {i}/{total}",
                f"🌐 Login: {login}   🔒 Password: {password}",
            ])
        )

    parts.append(
        "\n".join([
            "GUIDE",
            "1. Open: https://saydis.pro/rust/en",
            "2. Connect STEAM first, then TWITCH.",
            "3. Click: CHECK FOR MISSED DROPS.",
            "4. Re-enter the game.",
            "",
            "IMPORTANT",
            "- Rewards can usually be claimed only once per Rust account.",
            "- Please record a continuous video from payment to checking inventory in case you need support.",
        ])
    )

    return parts

def _open_chat_for_order(driver: webdriver.Chrome, order_id: str, buyer: str, debug_dir: Path) -> dict[str, Any]:
    states = []
    urls = [
        f"https://www.g2g.com/chat/#/order-item/{order_id.lower()}",
        f"https://www.g2g.com/chat/#/order-item/{order_id}",
        "https://www.g2g.com/chat/#/",
    ]

    for u in urls:
        driver.get(u)
        time.sleep(8)
        dump_debug(driver, debug_dir, "chat_bundle_opened")
        st = _chat_state(driver)
        states.append({"url": u, "state": st})

        if _find_chat_editor(driver):
            return {"ok": True, "states": states}

        click_conv = _click_chat_conversation(driver, order_id, buyer)
        states.append({"click_conversation": click_conv})
        time.sleep(5)
        if _find_chat_editor(driver):
            return {"ok": True, "states": states}

    return {"ok": bool(_find_chat_editor(driver)), "states": states, "error": "chat editor not found after open route"}


def _click_chat_attach(driver: webdriver.Chrome) -> dict[str, Any]:
    js = r"""
        function visible(el) {
          const r = el.getBoundingClientRect();
          const st = getComputedStyle(el);
          return r.width > 1 && r.height > 1 && st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0';
        }
        const nodes = Array.from(document.querySelectorAll('button,[role="button"],i,span,div'));
        const cands = [];
        for (const el of nodes) {
          if (!visible(el)) continue;
          const txt = (el.innerText || el.textContent || '').trim();
          const cls = String(el.className || '');
          const low = (txt + ' ' + cls).toLowerCase();
          if (!(low.includes('attach') || low.includes('attach_file') || low.includes('paperclip') || low.includes('clip') || low.includes('file'))) continue;
          const r = el.getBoundingClientRect();
          let score = r.width*r.height;
          if (el.tagName.toLowerCase() === 'button') score -= 100000;
          if (el.closest('button,[role="button"]')) score -= 50000;
          cands.push({el, text:txt, cls, score, rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
        }
        cands.sort((a,b)=>a.score-b.score);
        if (!cands.length) return {ok:false, error:'attach control not found'};
        const c = cands[0];
        const target = c.el.closest('button,[role="button"]') || c.el;
        target.scrollIntoView({block:'center', inline:'center'});
        try { target.click(); } catch(e) {}
        try {
          target.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true, view:window}));
          target.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true, view:window}));
          target.dispatchEvent(new MouseEvent('click', {bubbles:true, cancelable:true, view:window}));
        } catch(e) {}
        return {ok:true, clicked:{text:c.text, cls:c.cls, rect:c.rect, score:c.score}};
    """
    try:
        return driver.execute_script(js) or {}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _attach_chat_file(driver: webdriver.Chrome, file_path: str, debug_dir: Path, idx: int) -> dict[str, Any]:
    fp = str(Path(file_path).resolve())
    if not Path(fp).exists():
        return {"ok": False, "error": f"chat proof file not found: {fp}"}

    attach_clicks = []
    last_error = ""
    for attempt in range(4):
        try:
            inputs = driver.find_elements("css selector", "input[type='file']")
            if inputs:
                inp = inputs[-1]
                driver.execute_script("""
                    arguments[0].style.display='block';
                    arguments[0].style.visibility='visible';
                    arguments[0].style.opacity='1';
                    arguments[0].removeAttribute('disabled');
                    arguments[0].style.position='fixed';
                    arguments[0].style.zIndex='99999';
                    arguments[0].style.height='50px';
                    arguments[0].style.width='300px';
                """, inp)
                inp.send_keys(fp)
                time.sleep(4)
                dump_debug(driver, debug_dir, f"chat_attach_{idx:02d}")
                page = (driver.page_source or "").lower()
                body = body_text(driver).lower()
                name = Path(fp).name.lower()
                # Attachment previews may not expose filename, so treat successful send_keys as enough
                if name in page or name in body or True:
                    return {"ok": True, "method": "send_keys", "inputCount": len(inputs), "attachClicks": attach_clicks}
        except Exception as e:
            last_error = str(e)

        click = _click_chat_attach(driver)
        attach_clicks.append(click)
        time.sleep(1.5)

    return {"ok": False, "error": f"could not attach chat file: {last_error}", "attachClicks": attach_clicks, "state": _chat_state(driver)}



def _send_chat_text_message(driver: webdriver.Chrome, message: str, debug_dir: Path, label: str) -> dict[str, Any]:
    ins = _insert_chat_message(driver, message)
    if not ins.get("ok"):
        return {"ok": False, "error": "Could not insert text message", "insert": ins}

    send = _click_chat_send(driver)
    time.sleep(3)
    dump_debug(driver, debug_dir, f"chat_text_{label}")
    if not send.get("ok"):
        return {"ok": False, "error": "Could not click send for text message", "insert": ins, "send": send}

    body = body_text(driver)
    snippet = ""
    for line in message.splitlines():
        line = line.strip()
        if len(line) >= 8:
            snippet = line[:80]
            break
    verified = bool(snippet and snippet in body)
    return {
        "ok": True,
        "insert": ins,
        "send": send,
        "verify": {"snippet": snippet, "snippet_in_body": verified, "bodySample": body[-1600:]},
    }


def direct_send_chat_bundle_with_proofs(env: dict[str, str], order_id: str, buyer: str, proof_paths: list[str], accounts: list[dict[str, str]], debug_root: Path) -> dict[str, Any]:
    debug_dir = safe_mkdir(debug_root / "g2g_chat_bundle_v24")
    # ensure_proxy_wrapper(env)
    driver = make_driver(env, debug_dir)
    try:
        opened = _open_chat_for_order(driver, order_id, buyer, debug_dir)
        if not opened.get("ok"):
            return {"ok": False, "error": "Could not open chat thread/editor", "open": opened, "debug_dir": str(debug_dir)}

        proof_sends = []
        for i, pp in enumerate(proof_paths, 1):
            att = _attach_chat_file(driver, pp, debug_dir, i)
            if not att.get("ok"):
                return {"ok": False, "error": f"Could not attach proof {i}/{len(proof_paths)} in chat", "open": opened, "attach": att, "proofSends": proof_sends, "debug_dir": str(debug_dir)}
            send = _click_chat_send(driver)
            time.sleep(4)
            dump_debug(driver, debug_dir, f"chat_proof_sent_{i:02d}")
            if not send.get("ok"):
                return {"ok": False, "error": f"Could not send proof attachment {i}/{len(proof_paths)} in chat", "open": opened, "attach": att, "send": send, "proofSends": proof_sends, "debug_dir": str(debug_dir)}
            proof_sends.append({"proof_path": pp, "attach": att, "send": send})

        parts = build_delivery_message_parts(order_id, accounts)
        text_sends = []
        for idx, part in enumerate(parts, 1):
            sent = _send_chat_text_message(driver, part, debug_dir, f"{idx:02d}")
            text_sends.append({"index": idx, "text": part, "result": sent})
            if not sent.get("ok"):
                return {
                    "ok": False,
                    "error": f"Could not send text message part {idx}/{len(parts)}",
                    "open": opened,
                    "proofSends": proof_sends,
                    "textSends": text_sends,
                    "debug_dir": str(debug_dir),
                }
            time.sleep(2)

        body = body_text(driver)
        verify = {
            "all_logins_present": all(acc["login"] in body for acc in accounts),
            "all_passwords_present": all(acc["password"] in body for acc in accounts),
            "bodySample": body[-3000:],
        }
        ok = verify["all_logins_present"] and verify["all_passwords_present"]
        return {
            "ok": ok,
            "order_id": order_id,
            "proof_count_in_chat": len(proof_paths),
            "text_message_count": len(parts),
            "debug_dir": str(debug_dir),
            "open": opened,
            "proofSends": proof_sends,
            "textSends": text_sends,
            "verify": verify,
            "error": "" if ok else "Sent proof/text messages, but not all account data was found in final chat body.",
        }
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def _select_available_distinct_proofs(rec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Best-effort proof selection.
    Keep distinct successful proof files only. Missing proofs for some accounts are NOT fatal.
    """
    selected = []
    problems = []
    seen_paths: set[str] = set()
    seen_fps: set[tuple[int, str]] = set()

    for pr in rec.get("proofs", []) or []:
        if not pr.get("ok"):
            if pr.get("login"):
                problems.append(f"proof missing/failed for {pr.get('login')}: {pr.get('error','not ok')}")
            continue

        pp = str(pr.get("proof_path") or "")
        if not pp or not Path(pp).exists():
            problems.append(f"proof file missing for {pr.get('login','?')}: {pp}")
            continue

        p = Path(pp).resolve()
        norm = str(p).lower()
        fp = _proof_file_fingerprint(str(p))
        if norm in seen_paths:
            problems.append(f"duplicate proof path skipped: {p.name}")
            continue
        if fp in seen_fps:
            problems.append(f"duplicate proof content skipped: {p.name}")
            continue

        seen_paths.add(norm)
        seen_fps.add(fp)
        selected.append({
            "login": pr.get("login",""),
            "proof_path": str(p),
            "raw_screenshot_path": pr.get("raw_screenshot_path",""),
            "ok": True,
        })

    return selected, problems



# =============================================================================
# Main processing
# =============================================================================

def process_order(env: dict[str, str], state: dict[str, Any], state_path: Path, fo: FoundOrder, args: argparse.Namespace) -> dict[str, Any]:
    order_id = fo.order_id
    offer_id = args.offer_id or env.get("G2G_TEST_OFFER_ID", "").strip()
    qty = qty_for_order(args, fo)
    buyer = args.buyer or fo.buyer or ""

    debug_dir = safe_mkdir(Path(env.get("PROOFS_DIR", "proofs_g2g")) / order_id / "auto_deliver_multi_debug")

    # --- HYBRID INIT START ---
    fast_cfg = read_fast_config() if FAST_CLIENT_AVAILABLE else None
    # --- HYBRID INIT END ---

    # --- HYBRID INIT START ---
    fast_cfg = read_fast_config() if FAST_CLIENT_AVAILABLE else None
    # --- HYBRID INIT END ---

    result: dict[str, Any] = {
        "ok": False,
        "order_id": order_id,
        "offer_id": offer_id,
        "qty": qty,
        "buyer": buyer,
        "debug_dir": str(debug_dir.resolve()),
        "steps": {},
        "error": "",
    }

    if not offer_id:
        result["error"] = "offer_id is empty. Set G2G_TEST_OFFER_ID in .env or use --offer-id."
        return result

    required = [
        "g2g_bot.py",
        "g2g_send_chat_strict_v5.py",
        "g2g_confirm_delivery_strict_v4.py",
        "g2g_make_twitch_proof_one_strict_v1.py",
    ]
    missing = ensure_required_files(required)
    if missing:
        result["error"] = "Missing required files: " + ", ".join(missing)
        return result

    rec = state.setdefault("orders", {}).setdefault(order_id, {})
    rec.setdefault("created_at", now_ms())
    rec["last_seen_at"] = now_ms()
    rec["buyer"] = buyer
    rec["offer_id"] = offer_id
    rec["qty"] = qty
    save_state(state_path, state)

    if args.import_once:
        print_step("0. import-once")
        imp = run_py(["g2g_bot.py", "import-once"], env=env, timeout=300)
        result["steps"]["import_once"] = imp
        print(imp["stdout"])

    # 1) Prepare / reserve N accounts.
    # First try to recover already-reserved rows. This matters if an earlier prepare crashed
    # only while printing emoji on Windows cp1251, after it already reserved stock.
    if len(rec.get("accounts", [])) < qty and not args.force_prepare:
        already_reserved = db_accounts_for_order(env, order_id, qty)
        if len(already_reserved) >= qty:
            print_step(f"1. prepare skipped: recovered {len(already_reserved)} reserved account(s) from DB")
            rec["accounts"] = already_reserved[:qty]
            rec["prepared"] = True
            rec["prepared_recovered_from_db"] = True
            rec["prepared_at"] = now_ms()
            save_state(state_path, state)

    if len(rec.get("accounts", [])) < qty or args.force_prepare:
        print_step(f"1. prepare / reserve stock qty={qty}")
        prep = run_py(["g2g_bot.py", "prepare", order_id, offer_id, str(qty)], env=env, timeout=300)
        result["steps"]["prepare"] = prep
        print(prep["stdout"])
        if prep.get("stderr"):
            print(prep["stderr"])

        accounts = parse_accounts_from_prepare_output((prep["stdout"] or "") + "\n" + (prep["stderr"] or ""))

        # Even if prepare returned non-zero, recover rows from DB because the reservation may
        # have succeeded before the process crashed on console encoding.
        db_acc = db_accounts_for_order(env, order_id, qty)
        if len(db_acc) > len(accounts):
            accounts = db_acc

        if prep["returncode"] != 0 and len(accounts) < qty:
            result["error"] = "prepare failed"
            rec["error"] = result["error"]
            rec["prepare_stdout"] = prep["stdout"]
            rec["prepare_stderr"] = prep["stderr"]
            save_state(state_path, state)
            return result

        if len(accounts) < qty:
            result["error"] = f"Could not get enough accounts after prepare. Need {qty}, got {len(accounts)}."
            rec["error"] = result["error"]
            rec["prepare_stdout"] = prep["stdout"]
            rec["prepare_stderr"] = prep["stderr"]
            save_state(state_path, state)
            return result

        rec["accounts"] = accounts[:qty]
        rec["prepared"] = True
        rec["prepared_at"] = now_ms()
        if prep["returncode"] != 0:
            rec["prepare_recovered_after_nonzero"] = True
        save_state(state_path, state)
    else:
        print_step("1. prepare skipped: accounts already in state")

    accounts = rec["accounts"][:qty]

    # 2) Make ONLY ONE proof for the first account.
    first_account = accounts[0]
    existing_proofs = rec.get("proofs", []) or []
    first_existing = None
    for pr in existing_proofs:
        if pr.get("ok") and pr.get("login") == first_account["login"] and pr.get("proof_path") and Path(pr.get("proof_path")).exists():
            first_existing = pr
            break

    if first_existing and not args.force_proof:
        print_step("2. single proof skipped: first-account proof already exists")
        rec["proofs"] = [first_existing]
    else:
        print_step("2. make ONLY first-account Twitch proof")
        pr = None
        for attempt in range(1, 4):
            print(f"Making single proof attempt {attempt}/3: {first_account['login']}")
            pr = make_twitch_proof_for_account(env, order_id, offer_id, 1, 1, first_account)
            print(json.dumps(pr, ensure_ascii=False, indent=2))
            if pr.get("ok"):
                break
            if attempt < 3:
                print("  -> failed, waiting 15s before retry...")
                time.sleep(15)

        if not pr or not pr.get("ok"):
            print(f"single proof failed for first account after retries: {first_account['login']}; continuing WITHOUT proof")
            rec["proofs"] = [{
                "ok": False,
                "login": first_account["login"],
                "proof_path": "",
                "raw_screenshot_path": "",
                "error": (pr or {}).get("error", f"single proof failed for {first_account['login']}"),
            }]
            rec["proof_made"] = False
            rec["proof_made_at"] = now_ms()
            rec["proof_optional_failure"] = True
            rec["proof_optional_failure_at"] = now_ms()
            # Since there is no proof, gallery/chat proof upload must be recalculated.
            rec["uploaded_proofs"] = []
            rec["proof_uploaded"] = False
            save_state(state_path, state)
        else:
            rec["proofs"] = [pr]
            rec["proof_made"] = True
            rec["proof_made_at"] = now_ms()
            # New proof invalidates old upload/chat assumptions.
            rec["uploaded_proofs"] = []
            rec["proof_uploaded"] = False
            rec["chat_messages"] = []
            rec["chat_sent"] = False
            save_state(state_path, state)

    proof_paths = []
    for pr in rec.get("proofs", []) or []:
        if pr.get("ok") and pr.get("proof_path") and Path(pr.get("proof_path")).exists():
            proof_paths.append(str(Path(pr["proof_path"]).resolve()))

    # Enforce exactly one proof in this strategy.
    proof_paths = proof_paths[:1]

    result["steps"]["proof_summary"] = {
        "strategy": "single_proof_first_account_only",
        "proof_count": len(proof_paths),
        "proof_login": first_account["login"],
        "proof_paths": proof_paths,
        "all_account_logins": [a["login"] for a in accounts[:qty]],
    }
    print(json.dumps(result["steps"]["proof_summary"], ensure_ascii=False, indent=2))

    if not proof_paths:
        print("No valid single proof file exists; continuing with proofless flow (chat credentials only).")
        result["steps"]["proof_summary"]["proof_optional_failure"] = True



# 3) Start delivery.
    if not rec.get("delivery_started") or args.force_start:
        print_step("3. start delivery / verify")
        start = start_delivery_if_needed(env, order_id, debug_dir)
        result["steps"]["start_delivery"] = start
        print(json.dumps(start, ensure_ascii=False, indent=2))
        if not start.get("ok"):
            result["error"] = "Could not verify delivery mode after start-delivery."
            rec["error"] = result["error"]
            save_state(state_path, state)
            return result

        rec["delivery_started"] = True
        rec["delivery_started_at"] = now_ms()
        save_state(state_path, state)
    else:
        print_step("3. start delivery skipped")

    # 4) Upload only ONE proof to G2G proof gallery, if a proof exists.
    gallery_target_count = 1

    if args.assume_proofs_uploaded:
        print_step("4. gallery proof upload skipped: --assume-proofs-uploaded")
        rec["uploaded_proofs"] = [
            {"proof_path": proof_paths[0], "ok": True, "json": {"ok": True, "uploaded": 1, "skipped": True, "reason": "--assume-proofs-uploaded"}}
        ]
        rec["proof_uploaded"] = True
        rec["proof_uploaded_at"] = now_ms()
        save_state(state_path, state)
    elif len(rec.get("uploaded_proofs", [])) < 1 or args.force_upload:
        if not proof_paths:
            print_step("4. gallery proof upload skipped: no proof available")
            rec["uploaded_proofs"] = []
            rec["proof_uploaded"] = False
            save_state(state_path, state)
        else:
            print_step("4. upload only 1 proof to G2G gallery")

            upload_env = {**env, "G2G_ORDER_URL_TEMPLATE": "https://www.g2g.com/g2g-user/sale/order/item/{order_id}"}
            check = direct_check_uploaded_proof_count(upload_env, order_id, gallery_target_count, debug_dir)
            result["steps"]["upload_precheck"] = {"json": check}
            print("Gallery upload precheck:")
            print(json.dumps(check, ensure_ascii=False, indent=2))

            if check.get("ok") and check.get("enough") and not args.force_upload:
                rec["uploaded_proofs"] = [{"proof_path": proof_paths[0], "ok": True, "json": {"ok": True, "uploaded": 1, "skipped": True, "reason": "gallery already has at least 1 proof", "precheck": check}}]
                rec["proof_uploaded"] = True
                rec["proof_uploaded_at"] = now_ms()
                save_state(state_path, state)
                print("Gallery upload skipped: at least one proof is already in G2G gallery.")
            else:
                first_proof = proof_paths[0]

                # --- HYBRID FAST UPLOAD START ---
                up_json = {"ok": False}
                if fast_cfg and fast_cfg.upload_endpoint:
                    print(f"[FAST] Uploading proof via requests: {first_proof}")
                    res = fast_upload_proof(fast_cfg, order_id, first_proof)
                    if not res.get("needs_browser") and res.get("success"):
                        up_json = {"ok": True, "uploaded": 1, "method": "fast_requests"}
                        print("[FAST] Proof uploaded successfully.")
                # --- HYBRID FAST UPLOAD END ---

                if not up_json.get("ok"):
                    print(f"Uploading only first gallery proof via Selenium: {first_proof}")
                    up_json = direct_upload_proofs_sequence(upload_env, order_id, [first_proof], debug_dir)

                result["steps"]["upload_gallery_one"] = {"json": up_json}
                print(json.dumps(up_json, ensure_ascii=False, indent=2))

                if not up_json.get("ok"):
                    result["error"] = "single gallery proof upload failed"
                    rec["error"] = result["error"]
                    save_state(state_path, state)
                    return result

                rec["uploaded_proofs"] = [{"proof_path": first_proof, "ok": True, "json": up_json}]
                rec["proof_uploaded"] = True
                rec["proof_uploaded_at"] = now_ms()
                save_state(state_path, state)
    else:
        print_step("4. gallery proof upload skipped")


    # 5) Send account data in chat. Proof image is NOT sent to chat anymore.
    # Proof must be uploaded only to G2G Gallery to avoid duplicate proof messages in chat.
    if not rec.get("chat_sent") or args.force_chat:
        print_step(f"5. send account data in chat only; proof stays in gallery")

        # --- HYBRID FAST CHAT START ---
        chat_json = {"ok": False}
        if fast_cfg and fast_cfg.chat_endpoint:
            print("[FAST] Sending credentials via requests...")
            # Combine payloads for fast chat if possible, or just send one message.
            # Building a simple message similar to build_delivery_message_parts
            msg = f"Order: {order_id}\n"
            for i, acc in enumerate(accounts[:qty], 1):
                msg += f"ACC {i}: {acc['login']} / {acc['password']}\n"
            res = fast_send_chat(fast_cfg, order_id, msg)
            if not res.get("needs_browser") and res.get("success"):
                chat_json = {"ok": True, "method": "fast_requests"}
                print("[FAST] Chat message sent successfully.")
        # --- HYBRID FAST CHAT END ---

        if not chat_json.get("ok"):
            print("Sending credentials via Selenium...")

        # --- HYBRID FAST CHAT START ---
        chat_json = {"ok": False}
        if fast_cfg and fast_cfg.chat_endpoint:
            print("[FAST] Sending credentials via requests...")
            msg = f"Order: {order_id}\n"
            for i, acc in enumerate(accounts[:qty], 1):
                msg += f"ACC {i}: {acc['login']} / {acc['password']}\n"
            res = fast_send_chat(fast_cfg, order_id, msg)
            if not res.get("needs_browser") and res.get("success"):
                chat_json = {"ok": True, "method": "fast_requests"}
                print("[FAST] Chat message sent successfully.")
        # --- HYBRID FAST CHAT END ---

        if not chat_json.get("ok"):
            print("Sending credentials via Selenium...")
            chat_json = direct_send_chat_bundle_with_proofs(env, order_id, buyer, [], accounts[:qty], debug_dir)


        result["steps"]["chat_bundle"] = {"json": chat_json}
        print(json.dumps(chat_json, ensure_ascii=False, indent=2))

        if not chat_json.get("ok"):
            result["error"] = "chat proof/data bundle failed"
            rec["error"] = result["error"]
            save_state(state_path, state)
            return result

        rec["chat_messages"] = [{"bundle": True, "strategy": "credentials_only_proof_in_gallery", "ok": True, "json": chat_json}]
        rec["chat_sent"] = True
        rec["chat_sent_at"] = now_ms()
        save_state(state_path, state)
    else:
        print_step("5. chat bundle skipped")


    # 6) Confirm delivery qty=N.
    if args.no_confirm:
        result["ok"] = True
        result["error"] = ""
        rec["completed_without_confirm"] = True
        rec["last_ok_at"] = now_ms()
        save_state(state_path, state)
        return result

    if not args.full and not args.confirm:
        result["ok"] = True
        result["error"] = "Stopped before confirm because --full/--confirm was not set."
        rec["ready_to_confirm"] = True
        rec["last_ok_at"] = now_ms()
        save_state(state_path, state)
        return result

    if not rec.get("confirmed") or args.force_confirm:
        print_step(f"6. confirm delivery qty={qty}")

        # --- HYBRID FAST CONFIRM START ---
        conf_json = {"ok": False}
        if fast_cfg and fast_cfg.confirm_endpoint:
            print(f"[FAST] Confirming delivery via requests (qty={qty})...")
            res = fast_confirm_delivery(fast_cfg, order_id, qty)
            if not res.get("needs_browser") and res.get("success"):
                conf_json = {"ok": True, "method": "fast_requests"}
                print("[FAST] Delivery confirmed successfully.")
        # --- HYBRID FAST CONFIRM END ---

        if not conf_json.get("ok"):
            print("Confirming delivery via Selenium...")

        # --- HYBRID FAST CONFIRM START ---
        conf_json = {"ok": False}
        if fast_cfg and fast_cfg.confirm_endpoint:
            print(f"[FAST] Confirming delivery via requests (qty={qty})...")
            res = fast_confirm_delivery(fast_cfg, order_id, qty)
            if not res.get("needs_browser") and res.get("success"):
                conf_json = {"ok": True, "method": "fast_requests"}
                print("[FAST] Delivery confirmed successfully.")
        # --- HYBRID FAST CONFIRM END ---

        if not conf_json.get("ok"):
            print("Confirming delivery via Selenium...")
            conf = run_py(["g2g_confirm_delivery_strict_v4.py", order_id, "--qty", str(qty), "--do-it"], env=env, timeout=900)
            result["steps"]["confirm_delivery"] = conf
            print(conf["stdout"])
            conf_json = conf["json"]

        if conf["returncode"] != 0 or not conf_json or not conf_json.get("ok"):
            result["error"] = "confirm delivery failed"
            rec["error"] = result["error"]
            rec["confirm_stdout"] = conf["stdout"]
            save_state(state_path, state)
            return result

        rec["confirmed"] = True
        rec["confirmed_at"] = now_ms()
        rec["completed"] = True
        rec["mark_db_sold"] = mark_db_sold(env, order_id)
        result["steps"]["mark_db_sold"] = rec["mark_db_sold"]
        save_state(state_path, state)
    else:
        print_step("6. confirm skipped")

    result["ok"] = True
    result["error"] = ""
    rec["last_ok_at"] = now_ms()
    save_state(state_path, state)
    return result


# =============================================================================
# CLI
# =============================================================================

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Self-contained G2G multi-account auto delivery pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--order-id", default="", help="Process known order. If omitted, bot searches seller orders page.")
        p.add_argument("--offer-id", default="", help="Offer ID. Defaults to G2G_TEST_OFFER_ID from .env.")
        p.add_argument("--qty", default="auto", help="auto or number. Default: auto-detect from order page.")
        p.add_argument("--buyer", default="", help="Buyer nickname. Optional; auto-detected when possible.")
        p.add_argument("--full", action="store_true", help="Run full pipeline including confirm delivery.")
        p.add_argument("--confirm", action="store_true", help="Confirm delivery after proof+chat.")
        p.add_argument("--no-confirm", action="store_true", help="Stop after proof upload and chat.")
        p.add_argument("--assume-proofs-uploaded", action="store_true", help="Skip proof upload step and continue to chat/confirm. Use only if proof gallery already has enough proofs.")
        p.add_argument("--upload-extra-proofs", action="store_true", help="Upload distinct proof files even if G2G gallery counter already says enough. Use when earlier run uploaded duplicate proof and G2G does not allow deletion.")
        p.add_argument("--upload-extra-indexes", default="", help="Optional comma list of 1-based proof indexes to upload as extra evidence, e.g. 2 or 1,2. Empty = upload all distinct proofs.")
        p.add_argument("--import-once", action="store_true")
        p.add_argument("--force-prepare", action="store_true")
        p.add_argument("--force-proof", action="store_true")
        p.add_argument("--force-start", action="store_true")
        p.add_argument("--force-upload", action="store_true")
        p.add_argument("--force-chat", action="store_true")
        p.add_argument("--force-confirm", action="store_true")

    once = sub.add_parser("once")
    add_common(once)

    watch = sub.add_parser("watch")
    add_common(watch)
    watch.add_argument("--poll", type=int, default=30)

    find = sub.add_parser("find")
    find.add_argument("--order-id", default="")

    return ap


def main() -> None:
    args = build_parser().parse_args()
    env = read_env()
    env["G2G_ORDER_URL_TEMPLATE"] = "https://www.g2g.com/g2g-user/sale/order/item/{order_id}"

    state_path = Path(env.get("DATA_DIR", "data_g2g")) / "auto_deliver_state_multi.json"
    state = load_state(state_path)
    state.setdefault("orders", {})

    debug_root = safe_mkdir(Path(env.get("PROOFS_DIR", "proofs_g2g")) / "_auto_find_debug_multi")

    if args.cmd == "find":
        fo = find_new_order(env, state, debug_root, explicit_order_id=args.order_id)
        if not fo:
            print(json.dumps({"ok": False, "error": "No processable order found."}, ensure_ascii=False, indent=2))
            return
        print(json.dumps({"ok": True, "order": asdict(fo), "qty_detected": parse_qty_from_order_body(fo.status_sample)}, ensure_ascii=False, indent=2))
        return

    if args.cmd == "once":
        fo = find_new_order(env, state, debug_root, explicit_order_id=args.order_id)
        if not fo:
            print(json.dumps({"ok": False, "error": "No processable order found."}, ensure_ascii=False, indent=2))
            return

        print(json.dumps({"found_order": asdict(fo), "qty_detected": parse_qty_from_order_body(fo.status_sample)}, ensure_ascii=False, indent=2))
        if not fo.processable:
            print(json.dumps({"ok": False, "error": "Order page is not processable. Not reserving stock.", "current_url": fo.current_url}, ensure_ascii=False, indent=2))
            return
        res = process_order(env, state, state_path, fo, args)
        print("\nFINAL RESULT:")
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return

    if args.cmd == "watch":
        print("Watching for G2G orders with multi-account support...")
        print(f"State: {state_path}")
        print(f"Poll: {args.poll}s")

        while True:
            try:
                state = load_state(state_path)
                state.setdefault("orders", {})
                fo = find_new_order(env, state, debug_root, explicit_order_id=args.order_id)
                if fo:
                    print(json.dumps({"found_order": asdict(fo), "qty_detected": parse_qty_from_order_body(fo.status_sample)}, ensure_ascii=False, indent=2))
                    if not fo.processable:
                        print(json.dumps({"ok": False, "error": "Order page is not processable. Not reserving stock.", "current_url": fo.current_url}, ensure_ascii=False, indent=2))
                        time.sleep(max(5, int(args.poll)))
                        continue
                    res = process_order(env, state, state_path, fo, args)
                    print("\nFINAL RESULT:")
                    print(json.dumps(res, ensure_ascii=False, indent=2))
                else:
                    print(time.strftime("%Y-%m-%d %H:%M:%S"), "No processable order found.")
            except KeyboardInterrupt:
                print("Stopped.")
                return
            except Exception as e:
                print(json.dumps({"ok": False, "error": f"watch exception: {e}"}, ensure_ascii=False, indent=2))
            time.sleep(max(5, int(args.poll)))


if __name__ == "__main__":
    main()
