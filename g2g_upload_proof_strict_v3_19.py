# -*- coding: utf-8 -*-
"""
G2G strict proof uploader v3.19
Standalone script. Does NOT modify g2g_bot.py.

Usage:
  python g2g_upload_proof_strict_v3_19.py 1776749386781X4CM-1 ".\proofs_g2g\1776749386781X4CM-1\01_twitch_inventory_proof.png"

Before running:
  pip install selenium pyautogui
  Close old bot Chrome windows that use browser_profile_g2g.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def load_env(path: Path) -> Dict[str, str]:
    env: Dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        env[k] = v
    return env


ROOT = Path.cwd()
ENV = load_env(ROOT / ".env")


def env_get(name: str, default: str = "") -> str:
    return os.environ.get(name) or ENV.get(name, default)


PROOFS_DIR = Path(env_get("PROOFS_DIR", "proofs_g2g"))
BROWSER_PROFILE_DIR = Path(env_get("BROWSER_PROFILE_DIR", "browser_profile_g2g"))
G2G_ORDER_URL_TEMPLATE = env_get(
    "G2G_ORDER_URL_TEMPLATE",
    "https://www.g2g.com/g2g-user/sale/order/item/{order_id}",
)

GALLERY_CSS = "#delivery-details-actions-list > li:nth-child(1) > span > span"
PLUS_CSS = "#fileUploaderContainer > div.g-uploader.row.q-gutter-x-lg.q-gutter-y-md > div > div > i"

WAIT_PAGE_SECONDS = int(env_get("G2G_UPLOAD_WAIT_PAGE_SECONDS", "90"))
WAIT_MODAL_SECONDS = int(env_get("G2G_UPLOAD_WAIT_MODAL_SECONDS", "30"))
WAIT_ATTACH_SECONDS = int(env_get("G2G_UPLOAD_WAIT_ATTACH_SECONDS", "30"))
WAIT_SEND_SECONDS = int(env_get("G2G_UPLOAD_WAIT_SEND_SECONDS", "45"))
KEEP_BROWSER_OPEN = env_get("G2G_UPLOAD_KEEP_BROWSER_OPEN", "0") == "1"


def jprint(obj: Dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def clean_order_id(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def body_text(driver) -> str:
    try:
        return driver.find_element(By.TAG_NAME, "body").text or ""
    except Exception:
        return ""


def safe_screenshot(driver, path: Path) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        driver.save_screenshot(str(path))
        return str(path)
    except Exception:
        return ""


def safe_click(driver, el) -> None:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", el)
        time.sleep(0.25)
    except Exception:
        pass
    try:
        el.click()
        return
    except Exception:
        pass
    try:
        driver.execute_script("arguments[0].click();", el)
        return
    except Exception:
        pass
    ActionChains(driver).move_to_element(el).pause(0.1).click().perform()


def visible(el) -> bool:
    try:
        return el.is_displayed() and el.size.get("height", 0) > 5 and el.size.get("width", 0) > 5
    except Exception:
        return False


def probe_page_state(driver) -> Dict[str, Any]:
    txt = body_text(driver)
    try:
        url = driver.current_url
    except Exception:
        url = ""
    js = """
    const vis = e => {
      const r = e.getBoundingClientRect();
      const s = getComputedStyle(e);
      return r.width > 2 && r.height > 2 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
    };
    const dialogs = [...document.querySelectorAll('.q-dialog,[role="dialog"]')].filter(vis);
    const uploader = document.querySelector('#fileUploaderContainer');
    const sendBtns = [...document.querySelectorAll('.q-dialog button,.q-dialog .q-btn,[role="dialog"] button,[role="dialog"] .q-btn')]
      .filter(vis).map(e => (e.innerText||e.textContent||'').trim()).filter(Boolean);
    return {
      url: location.href,
      dialogCount: dialogs.length,
      dialogTexts: dialogs.map(d => (d.innerText||'').slice(0,500)),
      uploaderFound: !!uploader,
      uploaderText: uploader ? (uploader.innerText||'').slice(0,500) : '',
      fileInputCount: document.querySelectorAll('input[type=file]').length,
      sendButtons: sendBtns.slice(0,20)
    };
    """
    try:
        extra = driver.execute_script(js) or {}
    except Exception as e:
        extra = {"js_error": str(e)}
    return {"url": url, "bodySample": txt[:1200].lower(), **extra}


def copy_to_clipboard_windows(text: str) -> bool:
    try:
        import subprocess
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $args[0]", text],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return True
    except Exception:
        pass
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(text)
        return True
    except Exception:
        return False


def launch_driver():
    try:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options
    except Exception as e:
        raise RuntimeError("Selenium is not installed. Run: pip install selenium") from e

    options = Options()
    profile_abs = (ROOT / BROWSER_PROFILE_DIR).resolve()
    profile_abs.mkdir(parents=True, exist_ok=True)
    options.add_argument(f"--user-data-dir={profile_abs}")
    options.add_argument("--profile-directory=Default")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--start-maximized")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--lang=ru-RU")

    proxy_mode = env_get("BROWSER_PROXY_MODE", "").strip().lower()
    proxy_listen = env_get("BROWSER_PROXY_LISTEN", "").strip()
    proxy_direct = env_get("BROWSER_PROXY", "").strip()

    if proxy_mode == "wrapper" and proxy_listen:
        proxy = proxy_listen if "://" in proxy_listen else f"http://{proxy_listen}"
        options.add_argument(f"--proxy-server={proxy}")
    elif proxy_mode == "direct" and proxy_direct:
        options.add_argument(f"--proxy-server={proxy_direct}")

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(90)
    return driver


def wait_order_page(driver, order_id: str, debug_dir: Path) -> Tuple[bool, Dict[str, Any]]:
    oid = clean_order_id(order_id)
    target_url = G2G_ORDER_URL_TEMPLATE.format(order_id=order_id)
    driver.get(target_url)
    for i in range(WAIT_PAGE_SECONDS):
        txt = body_text(driver)
        clean_body = clean_order_id(txt)
        try:
            url = driver.current_url
        except Exception:
            url = ""
        if oid in clean_body:
            return True, {"target_url": target_url, "current_url": url, "seconds": i}
        low = txt.lower()
        if i > 8 and any(x in low for x in ["welcome back", "login", "e-mail or mobile number", "registrate como vendedor"]):
            safe_screenshot(driver, debug_dir / "00_login_or_wrong_account.png")
            return False, {"target_url": target_url, "current_url": url, "reason": "login_or_wrong_account", "state": probe_page_state(driver)}
        if i in (5, 15, 30, 60):
            safe_screenshot(driver, debug_dir / f"00_wait_order_{i:02d}.png")
        time.sleep(1)
    safe_screenshot(driver, debug_dir / "00_wrong_page_timeout.png")
    return False, {"target_url": target_url, "reason": "order_id_not_found", "state": probe_page_state(driver)}


def is_delivering(driver) -> Tuple[bool, Dict[str, Any]]:
    txt = body_text(driver).lower()
    has_gallery = any(x in txt for x in ["галерея доказательств", "proof gallery"])
    has_qty = any(x in txt for x in ["всего доставлено", "delivered quantity", "total delivered"])
    has_delivery = any(x in txt for x in ["доставка в процессе", "доставка", "delivering", "delivery in progress"])
    return (has_gallery and has_qty and has_delivery), {"has_gallery": has_gallery, "has_qty": has_qty, "has_delivery": has_delivery, "bodySample": txt[:1000]}


def click_gallery(driver, debug_dir: Path) -> Dict[str, Any]:
    try:
        el = driver.find_element(By.CSS_SELECTOR, GALLERY_CSS)
        if visible(el):
            safe_click(driver, el)
            time.sleep(2)
            return {"ok": True, "method": "exact_css", "selector": GALLERY_CSS}
    except Exception as e:
        exact_error = str(e)
    else:
        exact_error = ""

    for xp in [
        "//*[contains(normalize-space(.),'Галерея доказательств')]",
        "//*[contains(normalize-space(.),'Proof gallery')]",
        "//*[contains(normalize-space(.),'Gallery')]",
    ]:
        try:
            for el in driver.find_elements(By.XPATH, xp):
                if visible(el):
                    safe_click(driver, el)
                    time.sleep(2)
                    return {"ok": True, "method": "text_xpath", "xpath": xp}
        except Exception:
            pass

    safe_screenshot(driver, debug_dir / "01_no_gallery_click.png")
    return {"ok": False, "error": "Gallery button not found/clickable", "exact_error": exact_error, "state": probe_page_state(driver)}


def find_proof_modal(driver) -> Optional[Any]:
    try:
        candidates = driver.find_elements(By.CSS_SELECTOR, ".q-dialog, [role='dialog'], .q-card")
    except Exception:
        return None
    best = None
    best_score = -1
    for el in candidates:
        if not visible(el):
            continue
        try:
            txt = ((el.text or el.get_attribute("innerText") or "")[:4000]).lower()
        except Exception:
            txt = ""
        score = 0
        for marker in ["загрузить новое доказательство доставки", "галерея доказательств", "upload new proof", "proof gallery", "доказательств"]:
            if marker in txt:
                score += 10
        try:
            has_uploader = bool(el.find_elements(By.CSS_SELECTOR, "#fileUploaderContainer"))
        except Exception:
            has_uploader = False
        if has_uploader:
            score += 30
        if score > best_score:
            best_score = score
            best = el
    return best if best_score >= 10 else None


def wait_proof_modal(driver, debug_dir: Path) -> Tuple[Optional[Any], Dict[str, Any]]:
    for i in range(WAIT_MODAL_SECONDS):
        modal = find_proof_modal(driver)
        if modal:
            return modal, {"ok": True, "seconds": i}
        if i in (3, 10, 20):
            safe_screenshot(driver, debug_dir / f"01_wait_modal_{i:02d}.png")
        time.sleep(1)
    safe_screenshot(driver, debug_dir / "01_no_modal.png")
    return None, {"ok": False, "state": probe_page_state(driver)}


def modal_state(driver) -> Dict[str, Any]:
    js = """
    const vis = e => {
      const r = e.getBoundingClientRect();
      const s = getComputedStyle(e);
      return r.width > 2 && r.height > 2 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
    };
    const root = document.querySelector('#fileUploaderContainer');
    const rootText = root ? (root.innerText || root.textContent || '') : '';
    const imgs = root ? [...root.querySelectorAll('img,.q-img__image,[style*="background-image"],canvas')].filter(vis).length : 0;
    const removeLike = root ? [...root.querySelectorAll('button,i,span,div')].some(e => vis(e) && /×|close|delete|remove/i.test((e.innerText||e.textContent||e.getAttribute('class')||'').trim())) : false;
    const validationError = /пожалуйста|please/i.test(document.body.innerText || '');
    let remaining = null;
    const body = document.body.innerText || '';
    const m = body.match(/осталось\s*(\d+)/i);
    if (m) remaining = parseInt(m[1], 10);
    const uploadChildren = root ? root.querySelectorAll('*').length : 0;
    return {rootFound: !!root, rootText, imgs, removeLike, validationError, remaining, uploadChildren};
    """
    try:
        return driver.execute_script(js) or {}
    except Exception as e:
        return {"error": str(e)}


def click_exact_plus(driver, debug_dir: Path) -> Dict[str, Any]:
    try:
        plus = driver.find_element(By.CSS_SELECTOR, PLUS_CSS)
        if visible(plus):
            safe_click(driver, plus)
            return {"ok": True, "method": "exact_css", "selector": PLUS_CSS}
    except Exception as e:
        exact_error = str(e)
    else:
        exact_error = "exact plus not visible"

    try:
        elems = driver.find_elements(By.CSS_SELECTOR, "#fileUploaderContainer i, #fileUploaderContainer button, #fileUploaderContainer div[role='button'], #fileUploaderContainer .q-icon")
        for el in elems:
            if not visible(el):
                continue
            text = (el.text or el.get_attribute("innerText") or el.get_attribute("class") or "").lower()
            rect = el.rect
            if "+" in text or "add" in text or (10 <= rect.get("width", 0) <= 120 and 10 <= rect.get("height", 0) <= 120):
                safe_click(driver, el)
                return {"ok": True, "method": "uploader_visible_element", "text": text[:120], "rect": rect}
    except Exception as e:
        fallback_error = str(e)
    else:
        fallback_error = "no visible plus-like element"

    safe_screenshot(driver, debug_dir / "02_no_plus.png")
    return {"ok": False, "exact_error": exact_error, "fallback_error": fallback_error, "state": modal_state(driver)}


def try_direct_file_input(driver, file_path: Path) -> Dict[str, Any]:
    try:
        inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
    except Exception:
        inputs = []
    if not inputs:
        return {"ok": False, "reason": "no_input"}
    errors = []
    for inp in reversed(inputs):
        try:
            driver.execute_script("""
                arguments[0].style.display='block';
                arguments[0].style.visibility='visible';
                arguments[0].style.opacity='1';
                arguments[0].style.position='fixed';
                arguments[0].style.left='20px';
                arguments[0].style.top='20px';
                arguments[0].style.zIndex='2147483647';
                arguments[0].style.width='400px';
                arguments[0].style.height='80px';
            """, inp)
            inp.send_keys(str(file_path.resolve()))
            return {"ok": True, "method": "send_keys", "inputs": len(inputs)}
        except Exception as e:
            errors.append(str(e))
    return {"ok": False, "reason": "send_keys_failed", "inputs": len(inputs), "errors": errors[-3:]}


def attach_file(driver, file_path: Path, debug_dir: Path) -> Dict[str, Any]:
    import pyautogui  # type: ignore
    safe_screenshot(driver, debug_dir / "02_before_attach.png")
    before = modal_state(driver)
    direct = try_direct_file_input(driver, file_path)
    if direct.get("ok"):
        time.sleep(4)
    else:
        plus_click = click_exact_plus(driver, debug_dir)
        if not plus_click.get("ok"):
            return {"ok": False, "method": "plus_click_failed", "before": before, "direct": direct, "plus_click": plus_click}
        time.sleep(1.5)
        path_text = str(file_path.resolve())
        copied = copy_to_clipboard_windows(path_text)
        if copied:
            pyautogui.hotkey("ctrl", "v")
        else:
            pyautogui.write(path_text, interval=0.002)
        time.sleep(0.3)
        pyautogui.press("enter")
        time.sleep(4)
    safe_screenshot(driver, debug_dir / "02_after_attach_attempt.png")
    logs = []
    for _ in range(WAIT_ATTACH_SECONDS):
        st = modal_state(driver)
        logs.append(st)
        strong = False
        remaining = st.get("remaining")
        if isinstance(remaining, int) and remaining < 150:
            strong = True
        if int(st.get("imgs") or 0) > 0:
            strong = True
        if st.get("removeLike"):
            strong = True
        if int(st.get("uploadChildren") or 0) > int(before.get("uploadChildren") or 0) + 4:
            strong = True
        if strong and not st.get("validationError"):
            return {"ok": True, "direct": direct, "after": st, "logs": logs[-5:]}
        time.sleep(1)
    return {"ok": False, "direct": direct, "before": before, "after": logs[-1] if logs else {}, "logs": logs[-8:]}


def click_send_inside_modal(driver, debug_dir: Path) -> Dict[str, Any]:
    js = """
    const vis = e => {
      const r = e.getBoundingClientRect();
      const s = getComputedStyle(e);
      return r.width > 2 && r.height > 2 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
    };
    const dialogs = [...document.querySelectorAll('.q-dialog,[role="dialog"]')].filter(vis);
    for (const d of dialogs) {
      const t = (d.innerText || '').toLowerCase();
      if (!(/галерея доказательств|загрузить новое доказательство|proof gallery|upload new proof/.test(t))) continue;
      const buttons = [...d.querySelectorAll('button,.q-btn')].filter(vis);
      for (const b of buttons) {
        const bt = (b.innerText || b.textContent || '').trim().toLowerCase();
        if (/^(отправить|send)$/.test(bt) || bt.includes('отправить') || bt.includes('send')) {
          b.scrollIntoView({block:'center', inline:'center'});
          b.click();
          return {ok:true, text:bt, tag:b.tagName, className:b.className, dialogText:t.slice(0,500)};
        }
      }
      return {ok:false, reason:'send_not_found_in_matching_dialog', buttons: buttons.map(b => (b.innerText||b.textContent||'').trim()).filter(Boolean)};
    }
    return {ok:false, reason:'matching_dialog_not_found', dialogCount:dialogs.length};
    """
    try:
        res = driver.execute_script(js) or {}
    except Exception as e:
        res = {"ok": False, "reason": "js_exception", "error": str(e)}
    if not res.get("ok"):
        safe_screenshot(driver, debug_dir / "03_send_not_found.png")
    else:
        time.sleep(3)
        safe_screenshot(driver, debug_dir / "03_after_send_click.png")
    return res


def verify_send(driver) -> Dict[str, Any]:
    for i in range(WAIT_SEND_SECONDS):
        st = modal_state(driver)
        page = body_text(driver).lower()
        modal = find_proof_modal(driver)
        modal_open = modal is not None and visible(modal)
        if not modal_open:
            return {"ok": True, "reason": "modal_closed", "seconds": i, "state": st}
        if any(x in page for x in ["просмотреть загруженное доказательство", "view uploaded proof", "uploaded proof"]):
            return {"ok": True, "reason": "uploaded_link", "seconds": i, "state": st}
        if any(x in page for x in ["пожалуйста, загрузите хотя бы один файл", "please upload at least one file"]):
            return {"ok": False, "reason": "validation_error", "seconds": i, "state": st}
        time.sleep(1)
    return {"ok": False, "reason": "timeout_wait_send", "state": modal_state(driver)}


def upload_proof_strict(order_id: str, files: List[Path]) -> Dict[str, Any]:
    debug_dir = ROOT / PROOFS_DIR / order_id / "g2g_upload_debug_strict"
    debug_dir.mkdir(parents=True, exist_ok=True)
    driver = None
    result: Dict[str, Any] = {"ok": False, "uploaded": 0, "order_id": order_id, "files": [str(p) for p in files], "debug_dir": str(debug_dir), "error": ""}
    try:
        for p in files:
            if not p.exists():
                result["error"] = f"Proof file not found: {p}"
                return result
        driver = launch_driver()
        page_ok, page_info = wait_order_page(driver, order_id, debug_dir)
        result["page"] = page_info
        if not page_ok:
            result["error"] = "Order page did not open correctly. Check seller login/proxy/order_id."
            return result
        delivering, deliv_info = is_delivering(driver)
        result["delivering"] = deliv_info
        if not delivering:
            safe_screenshot(driver, debug_dir / "00_not_delivering.png")
            result["error"] = "Order is not in delivery state. Run start-delivery first or open order manually."
            return result
        gallery = click_gallery(driver, debug_dir)
        result["gallery_click"] = gallery
        if not gallery.get("ok"):
            result["error"] = "Could not click Proof gallery."
            return result
        modal, modal_info = wait_proof_modal(driver, debug_dir)
        result["modal_ready"] = modal_info
        if not modal:
            result["error"] = "Proof upload modal did not open."
            return result
        attached_files = []
        attach_logs = []
        for p in files:
            attach = attach_file(driver, p, debug_dir)
            attach_logs.append(attach)
            if not attach.get("ok"):
                result["attach_logs"] = attach_logs
                result["error"] = f"Proof file was not selected/queued in G2G modal: {p.name}"
                return result
            attached_files.append(str(p))
        result["attach_logs"] = attach_logs
        result["uploaded"] = len(attached_files)
        send_click = click_send_inside_modal(driver, debug_dir)
        result["send_click"] = send_click
        if not send_click.get("ok"):
            result["error"] = "Send/Отправить button not found inside proof modal."
            return result
        send_verify = verify_send(driver)
        result["send_verify"] = send_verify
        if not send_verify.get("ok"):
            result["error"] = "Proof send was not verified."
            return result
        result["ok"] = True
        result["error"] = ""
        return result
    except Exception as e:
        if driver is not None:
            safe_screenshot(driver, debug_dir / "99_exception.png")
        result["error"] = str(e)
        result["traceback"] = traceback.format_exc()[-4000:]
        return result
    finally:
        if driver is not None and not KEEP_BROWSER_OPEN:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.action_chains import ActionChains
    except Exception as e:
        jprint({"ok": False, "error": "Selenium is not installed. Run: pip install selenium", "details": str(e)})
        sys.exit(1)
    if len(sys.argv) < 3:
        jprint({"ok": False, "error": "Usage: python g2g_upload_proof_strict_v3_19.py ORDER_ID FILE_PATH [FILE_PATH2 ...]"})
        sys.exit(1)
    oid = sys.argv[1].strip()
    fpaths = [Path(x) for x in sys.argv[2:]]
    res = upload_proof_strict(oid, fpaths)
    jprint(res)
    sys.exit(0 if res.get("ok") else 2)
