# g2g_send_chat_strict_v5.py
# Standalone strict G2G chat sender with:
# - automatic proxy wrapper starter
# - order-page buyer parsing
# - G2G chat window switching
# - chat search by buyer/order if editor is not opened automatically
#
# Put this file next to:
#   g2g_send_chat_strict_v1.py
#   g2g_send_chat_strict_v2.py
#   .env
#
# Usage:
#   python .\g2g_send_chat_strict_v5.py "1776749386781X4CM-1" "G1776665621789GY" "1" --login "ml8qy8f5zrx5" --password "bi3iehazmu_"
#
# If chat search does not find the buyer automatically:
#   python .\g2g_send_chat_strict_v5.py "1776749386781X4CM-1" "G1776665621789GY" "1" --login "ml8qy8f5zrx5" --password "bi3iehazmu_" --buyer "KiraDrops"
#
# Dependencies:
#   pip install selenium pyperclip pyautogui pproxy

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

try:
    import pyperclip
except Exception:
    pyperclip = None


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

    raise RuntimeError(f"Proxy wrapper did not start on {listen}. Try: pip install pproxy")


DEFAULT_GUIDE = """Hello!
Thank you for your purchase.
You received Twitch account credentials for claiming the purchased Drops.
CAREFULLY COPY THE USERNAME AND PASSWORD, WITHOUT SPACES!!!!
https://saydis.pro/rust/en / <-- THIS IS AN INSTRUCTION! DON'T IGNORE HER, OTHERWISE YOU WON'T GET THE SKINS!!!!!!!!! YOU NEED TO CONNECT STEAM FIRST, AND THEN ONLY TWITCH!!!! THEN CLICK THE "CHECK FOR MISSED DROPS" BUTTON!!!! THEN RE-ENTER THE GAME!!!
Important:
- Rewards can usually be claimed only once per Rust account.
- Please record a continuous video from payment to checking inventory in case you need support."""


def build_message(order_id: str, login: str, password: str) -> str:
    return f"""Delivery-ID: G2G-{order_id}
Order: {order_id}

Hello! Thank you for your purchase.
Here is your account/data:

🌐 Login: {login}   🔒 Password: {password}

--- GUIDE ---
{DEFAULT_GUIDE}"""


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def detect_buyer_from_order_body(body: str) -> str:
    # Known RU body: "... Просмотреть детали\nКуплено KiraDrops\nЧат ..."
    patterns = [
        r"Куплено\s+([^\n\r]+)",
        r"Bought\s+by\s+([^\n\r]+)",
        r"Comprado\s+por\s+([^\n\r]+)",
        r" куплено\s+([A-Za-z0-9_.\-]+)\s+чат",
    ]
    for pat in patterns:
        m = re.search(pat, body, flags=re.I)
        if m:
            val = m.group(1).strip()
            val = re.split(r"\s+(?:Чат|Chat|Итог|Total)\b", val, flags=re.I)[0].strip()
            if val and len(val) <= 80:
                return val
    return ""


def wait_for_order_page(driver, env: dict[str, str], order_id: str, debug_dir: Path) -> dict[str, Any]:
    import g2g_send_chat_strict_v1 as base

    order_base = order_id.split("-", 1)[0]
    template = env.get("G2G_ORDER_URL_TEMPLATE", "https://www.g2g.com/g2g-user/sale/order/item/{order_id}")
    if "{order_id}" not in template:
        template = "https://www.g2g.com/g2g-user/sale/order/item/{order_id}"
    url = template.replace("{order_id}", order_id)

    driver.get(url)

    script = f"""
        const txt = (document.body && document.body.innerText || '');
        const low = txt.toLowerCase();
        return {{
          ok: low.includes({json.dumps(order_base.lower())}),
          hasChat: low.includes('чат') || low.includes('chat'),
          body: txt.slice(0, 3000),
          url: location.href
        }};
    """
    state = None
    end = time.time() + 75
    while time.time() < end:
        try:
            state = driver.execute_script(script)
            if state and state.get("ok") and state.get("hasChat"):
                break
        except Exception as e:
            state = {"error": str(e)}
        time.sleep(0.5)

    base.dump_debug(driver, debug_dir, "01_order_page_v5")

    if not state or not state.get("ok"):
        return {
            "ok": False,
            "target_url": url,
            "current_url": driver.current_url,
            "error": "Order page did not load or order_id was not found.",
            "state": state,
        }

    buyer = detect_buyer_from_order_body(state.get("body", ""))

    return {
        "ok": True,
        "target_url": url,
        "current_url": driver.current_url,
        "buyer_detected": buyer,
        "state": {k: v for k, v in state.items() if k != "body"},
        "bodySample": state.get("body", "")[:1200],
    }


def click_order_chat_button(driver, debug_dir: Path) -> dict[str, Any]:
    import g2g_send_chat_strict_v1 as base

    # Pick the Chat button in the order item/card, not the header chat icon.
    script = r"""
        const visible = e => {
          if (!e) return false;
          const r = e.getBoundingClientRect();
          const s = getComputedStyle(e);
          return r.width > 5 && r.height > 5 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        };
        const text = e => (e.innerText || e.textContent || '').trim();
        const low = e => text(e).toLowerCase();
        const ancestorText = (e) => {
          let n = e;
          let out = '';
          for (let i=0; i<7 && n; i++, n=n.parentElement) out += '\n' + (n.innerText || '');
          return out.toLowerCase();
        };

        const nodes = Array.from(document.querySelectorAll('button,a,[role="button"],.q-btn,span,div'));
        const scored = [];
        for (const e of nodes) {
          if (!visible(e)) continue;
          const r = e.getBoundingClientRect();
          const t = low(e);
          const href = (e.getAttribute('href') || '').toLowerCase();
          const cls = (e.className || '').toString().toLowerCase();
          const anc = ancestorText(e);

          let score = 0;
          if (t === 'чат' || t === 'chat') score += 250;
          else if (t.includes('чат') || t.includes('chat')) score += 120;

          // Order card area normally has order item / buyer / price around it.
          if (anc.includes('куплено') || anc.includes('bought') || anc.includes('kiradrops')) score += 120;
          if (anc.includes('rust drops') || anc.includes('#177674')) score += 80;

          // Avoid header chat icon.
          if (r.y < 120) score -= 180;
          if (href.includes('/chat/#/') && !t) score -= 120;
          if (cls.includes('q-btn')) score += 5;

          if (score > 0) {
            scored.push({e, score, text:text(e), href, cls, rect:{x:r.x,y:r.y,w:r.width,h:r.height}, anc:anc.slice(0,500)});
          }
        }
        scored.sort((a,b)=>b.score-a.score);
        if (!scored.length) return null;
        return scored[0];
    """
    info = driver.execute_script(script)
    if not info or not info.get("e"):
        base.dump_debug(driver, debug_dir, "02_chat_button_not_found_v5")
        return {"ok": False, "error": "Order Chat button not found", "info": info}

    el = info.pop("e")
    old_handles = set(driver.window_handles)

    base.click_js_element(driver, el)
    time.sleep(1.5)

    # Switch to new chat window if opened.
    new_handle = ""
    end = time.time() + 15
    while time.time() < end:
        new_handles = [h for h in driver.window_handles if h not in old_handles]
        if new_handles:
            new_handle = new_handles[-1]
            driver.switch_to.window(new_handle)
            break
        if "/chat" in (driver.current_url or ""):
            break
        time.sleep(0.3)

    # Fallback: if the click did not open chat, open chat directly.
    if "/chat" not in (driver.current_url or "") and not new_handle:
        driver.get("https://www.g2g.com/chat/#/")

    base.dump_debug(driver, debug_dir, "03_after_chat_click_v5")
    return {"ok": True, "click": info, "new_handle": new_handle, "current_url": driver.current_url, "window_handles": len(driver.window_handles)}


def find_editor_state(driver) -> dict[str, Any]:
    script = r"""
        const visible = e => {
          if (!e) return false;
          const r = e.getBoundingClientRect();
          const s = getComputedStyle(e);
          return r.width > 5 && r.height > 5 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        };
        const selectors = [
          '[id^="editor-g2g_dm_"] .toastui-editor-ww-container p',
          '[id^="editor-g2g_dm_"] .toastui-editor-contents[contenteditable="true"]',
          '[id^="editor-g2g_dm_"] [contenteditable="true"]',
          '.toastui-editor-ww-container p',
          '.toastui-editor-contents[contenteditable="true"]',
          '[contenteditable="true"]',
          'textarea'
        ];
        for (const sel of selectors) {
          for (const e of Array.from(document.querySelectorAll(sel))) {
            if (visible(e)) {
              return {ok:true, selector:sel, element:e, url:location.href, body:(document.body.innerText||'').slice(0,2500)};
            }
          }
        }
        return {ok:false, url:location.href, body:(document.body.innerText||'').slice(0,2500)};
    """
    return driver.execute_script(script)


def wait_editor(driver, timeout: int = 45) -> dict[str, Any]:
    end = time.time() + timeout
    state = None
    while time.time() < end:
        state = find_editor_state(driver)
        if state and state.get("ok"):
            return state
        time.sleep(0.5)
    return state or {"ok": False}


def search_and_open_dialog(driver, order_id: str, buyer: str, debug_dir: Path) -> dict[str, Any]:
    import g2g_send_chat_strict_v1 as base

    if pyperclip is None:
        return {"ok": False, "error": "pyperclip is not installed. Run: pip install pyperclip"}

    order_base = order_id.split("-", 1)[0]
    terms = []
    if buyer:
        terms.append(buyer)
    terms.extend([order_id, order_base])

    search_input_script = r"""
        const visible = e => {
          if (!e) return false;
          const r = e.getBoundingClientRect();
          const s = getComputedStyle(e);
          return r.width > 20 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        };
        const inputs = Array.from(document.querySelectorAll('input, textarea, [contenteditable="true"]'));
        const scored = [];
        for (const e of inputs) {
          if (!visible(e)) continue;
          const ph = (e.getAttribute('placeholder') || '').toLowerCase();
          const aria = (e.getAttribute('aria-label') || '').toLowerCase();
          const cls = (e.className || '').toString().toLowerCase();
          const r = e.getBoundingClientRect();
          let score = 0;
          if (ph.includes('search') || ph.includes('поиск')) score += 200;
          if (aria.includes('search') || aria.includes('поиск')) score += 150;
          if (cls.includes('search')) score += 80;
          // Search is usually near top/left, editor is lower.
          if (r.y < 250) score += 40;
          if (r.x < 700) score += 20;
          if (score > 0) scored.push({e, score, ph, aria, cls, rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
        }
        scored.sort((a,b)=>b.score-a.score);
        if (!scored.length) return null;
        return scored[0];
    """

    results = []
    for term in terms:
        info = driver.execute_script(search_input_script)
        if not info or not info.get("e"):
            results.append({"term": term, "ok": False, "error": "search input not found", "info": info})
            continue

        el = info.pop("e")
        try:
            pyperclip.copy(term)
            base.click_js_element(driver, el)
            time.sleep(0.2)
            from selenium.webdriver import ActionChains
            from selenium.webdriver.common.keys import Keys
            ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).send_keys(Keys.BACKSPACE).perform()
            ActionChains(driver).key_down(Keys.CONTROL).send_keys("v").key_up(Keys.CONTROL).perform()
            time.sleep(2.5)
        except Exception as e:
            results.append({"term": term, "ok": False, "error": f"search type failed: {e}"})
            continue

        # Click a visible search result / conversation containing buyer or order.
        click_script = """
            const term = arguments[0].toLowerCase();
            const visible = e => {
              if (!e) return false;
              const r = e.getBoundingClientRect();
              const s = getComputedStyle(e);
              return r.width > 20 && r.height > 10 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            };
            const text = e => (e.innerText || e.textContent || '').trim();
            const candidates = Array.from(document.querySelectorAll('div,li,a,button,[role="button"]'));
            const scored = [];
            for (const e of candidates) {
              if (!visible(e)) continue;
              const t = text(e).toLowerCase();
              if (!t || !t.includes(term)) continue;
              const r = e.getBoundingClientRect();
              let score = 0;
              score += 100;
              // Prefer list area, not hidden full body.
              if (r.x < 900) score += 40;
              if (r.y > 80) score += 10;
              if (r.width < 900 && r.height < 250) score += 50;
              scored.push({e, score, text:text(e).slice(0,500), rect:{x:r.x,y:r.y,w:r.width,h:r.height}});
            }
            scored.sort((a,b)=>b.score-a.score);
            if (!scored.length) return null;
            return scored[0];
        """
        candidate = driver.execute_script(click_script, term)
        if not candidate or not candidate.get("e"):
            base.dump_debug(driver, debug_dir, f"04_search_no_result_{re.sub(r'[^a-zA-Z0-9]+','_',term)[:30]}")
            results.append({"term": term, "ok": False, "error": "conversation result not found"})
            continue

        cand_info = {k: v for k, v in candidate.items() if k != "e"}
        base.click_js_element(driver, candidate["e"])
        time.sleep(3)
        base.dump_debug(driver, debug_dir, f"05_after_open_dialog_{re.sub(r'[^a-zA-Z0-9]+','_',term)[:30]}")

        ed = wait_editor(driver, timeout=20)
        results.append({"term": term, "ok": bool(ed.get("ok")), "candidate": cand_info, "editor": {k:v for k,v in ed.items() if k != "element"}})
        if ed.get("ok"):
            return {"ok": True, "term": term, "results": results, "editor": ed}

    return {"ok": False, "results": results}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("order_id")
    ap.add_argument("offer_id")
    ap.add_argument("qty")
    ap.add_argument("--login", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--buyer", default="")
    ap.add_argument("--keep-open", action="store_true")
    args = ap.parse_args()

    env = read_env()
    try:
        proxy_state = ensure_proxy_wrapper(env)
        print("PROXY_WRAPPER:", json.dumps(proxy_state, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Could not start proxy wrapper: {e}", "hint": "Run: pip install pproxy"}, ensure_ascii=False, indent=2))
        return

    try:
        import g2g_send_chat_strict_v1 as base
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"Cannot import g2g_send_chat_strict_v1.py: {e}"}, ensure_ascii=False, indent=2))
        return

    order_id = args.order_id.strip()
    debug_dir = base.safe_mkdir(Path(env.get("PROOFS_DIR", "proofs_g2g")) / order_id / "g2g_chat_debug_strict_v5")

    result: dict[str, Any] = {
        "ok": False,
        "order_id": order_id,
        "offer_id": args.offer_id,
        "qty": args.qty,
        "login": args.login,
        "debug_dir": str(debug_dir.resolve()),
        "error": "",
    }

    message = build_message(order_id, args.login.strip(), args.password.strip())
    result["message_preview"] = message[:500]

    driver: Optional[Any] = None
    try:
        driver = base.make_driver(env, debug_dir)

        order_state = wait_for_order_page(driver, env, order_id, debug_dir)
        result["order_page"] = order_state
        if not order_state.get("ok"):
            result["error"] = order_state.get("error", "Order page failed")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        buyer = args.buyer.strip() or order_state.get("buyer_detected", "")
        result["buyer"] = buyer

        click_state = click_order_chat_button(driver, debug_dir)
        result["chat_click"] = click_state
        if not click_state.get("ok"):
            result["error"] = click_state.get("error", "Chat click failed")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        # Wait editor directly.
        editor = wait_editor(driver, timeout=45)
        result["editor_initial"] = {k: v for k, v in editor.items() if k != "element"}

        # If generic chat list opened, search/open buyer/order dialog.
        if not editor.get("ok"):
            search_state = search_and_open_dialog(driver, order_id, buyer, debug_dir)
            result["search_dialog"] = {k: v for k, v in search_state.items() if k != "editor"}
            if search_state.get("ok") and search_state.get("editor"):
                editor = search_state["editor"]

        if not editor.get("ok") or not editor.get("element"):
            base.dump_debug(driver, debug_dir, "06_editor_not_found_v5")
            result["error"] = "Chat editor not found after click/search."
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        paste_state = base.paste_message(driver, editor["element"], message, debug_dir)
        result["paste"] = paste_state
        if not paste_state.get("ok"):
            result["error"] = "Message was not pasted into chat editor"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        send_state = base.click_send(driver, debug_dir)
        result["send_click"] = send_state
        if not send_state.get("clicked"):
            result["error"] = send_state.get("error", "Send button click failed")
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        verify = base.verify_sent(driver, order_id, timeout=int(env.get("CHAT_VERIFY_TIMEOUT", "35") or "35"))
        result["send_verify"] = verify
        result["ok"] = bool(verify.get("ok"))
        if not result["ok"]:
            result["error"] = "Send clicked, but Delivery-ID marker was not verified in chat."
        print(json.dumps(result, ensure_ascii=False, indent=2))

    except Exception as e:
        result["error"] = f"Exception: {e}"
        if driver:
            base.dump_debug(driver, debug_dir, "99_exception_v5")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        if driver and not args.keep_open:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
