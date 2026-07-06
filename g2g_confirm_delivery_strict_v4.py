# g2g_confirm_delivery_strict_v4.py
# Wrapper over g2g_confirm_delivery_strict_v1.py.
#
# Fixes:
# - v3 refused because the quantity input existed but was not "visible" in Selenium/DOM probe.
# - v2 clicked a wrong giant DIV.
#
# v4 strategy:
# - Does NOT click proof gallery.
# - Finds delivered quantity INPUT even if hidden/offscreen.
# - Forces only that INPUT visible long enough to set value.
# - Clicks only a real BUTTON / .q-btn / [role=button], never a plain DIV.
#
# Put next to:
#   g2g_confirm_delivery_strict_v1.py
#   .env
#
# Dry-run:
#   python .\g2g_confirm_delivery_strict_v4.py "1776749386781X4CM-1" --qty "1" --dry-run --keep-open
#
# Real:
#   python .\g2g_confirm_delivery_strict_v4.py "1776749386781X4CM-1" --qty "1" --do-it --keep-open

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

try:
    import g2g_confirm_delivery_strict_v1 as base
except Exception as e:
    print(json.dumps({
        "ok": False,
        "error": f"Cannot import g2g_confirm_delivery_strict_v1.py. Put v1 and v4 in the same folder. Import error: {e}"
    }, ensure_ascii=False, indent=2))
    raise SystemExit(1)

from selenium.webdriver import ActionChains
from selenium.webdriver.common.keys import Keys


def dump_json(debug_dir: Path, name: str, data: Any) -> None:
    try:
        Path(debug_dir).mkdir(parents=True, exist_ok=True)
        (Path(debug_dir) / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def check_proof_gallery_v4(driver, debug_dir: Path) -> dict[str, Any]:
    # Do not reopen proof gallery at confirm step. We already uploaded proof earlier.
    state = driver.execute_script(r"""
        const txt = (document.body && document.body.innerText || '');
        const low = txt.toLowerCase();
        return {
          ok: low.includes('доставка') || low.includes('delivery'),
          hasDeliveryProgress: low.includes('доставка в процессе') || low.includes('delivering'),
          hasGalleryText: low.includes('галерея доказательств') || low.includes('proof gallery'),
          bodySample: txt.slice(0, 2500)
        };
    """)
    base.dump_debug(driver, debug_dir, "02_precheck_no_gallery_click_v4")
    return {
        "ok": True,
        "skipped_click": True,
        "note": "v4 does not reopen proof gallery before confirm.",
        "state": state
    }


def find_qty_input_any_state(driver) -> dict[str, Any]:
    script = r"""
        const allInputs = Array.from(document.querySelectorAll('input, textarea'));
        const infoAll = [];
        const scored = [];

        function rectOf(e) {
          const r = e.getBoundingClientRect();
          return {x:r.x, y:r.y, w:r.width, h:r.height, top:r.top, left:r.left, bottom:r.bottom, right:r.right};
        }
        function isVisibleEnough(e) {
          const r = e.getBoundingClientRect();
          const s = getComputedStyle(e);
          return r.width > 2 && r.height > 2 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        }
        function ancestorText(e, depth=8) {
          let n = e, out = '';
          for (let i=0; i<depth && n; i++, n=n.parentElement) out += '\n' + (n.innerText || '');
          return out.toLowerCase();
        }

        for (const e of allInputs) {
          const ph = (e.getAttribute('placeholder') || '').toLowerCase();
          const aria = (e.getAttribute('aria-label') || '').toLowerCase();
          const name = (e.getAttribute('name') || '').toLowerCase();
          const id = (e.id || '').toLowerCase();
          const cls = (e.className || '').toString().toLowerCase();
          const type = (e.getAttribute('type') || '').toLowerCase();
          const value = e.value || '';
          const anc = ancestorText(e);
          const all = [ph, aria, name, id, cls, type, anc].join(' ');
          const styles = getComputedStyle(e);

          const info = {
            tag:e.tagName,
            ph,
            aria,
            name,
            id,
            cls:cls.slice(0,200),
            type,
            value,
            visible:isVisibleEnough(e),
            disabled:!!e.disabled || e.getAttribute('aria-disabled') === 'true',
            display:styles.display,
            visibility:styles.visibility,
            opacity:styles.opacity,
            rect:rectOf(e),
            ancestor:anc.slice(0,500)
          };
          infoAll.push(info);

          let score = 0;
          if (ph.includes('доставленное количество')) score += 800;
          if (ph.includes('delivered quantity') || ph.includes('delivery quantity')) score += 800;
          if (ph.includes('достав') || ph.includes('deliver') || ph.includes('quantity')) score += 250;
          if (aria.includes('достав') || aria.includes('deliver') || aria.includes('quantity')) score += 200;
          if (anc.includes('доставленное количество') || anc.includes('delivered quantity') || anc.includes('delivery quantity')) score += 350;
          if (cls.includes('q-field__native') || cls.includes('q-placeholder')) score += 80;
          if (type === 'text' || type === 'number') score += 30;
          if (value === 'false' || type === 'hidden') score -= 300;
          if (ph.includes('поиск') || ph.includes('search') || ph.includes('сообщение') || ph.includes('message')) score -= 600;

          if (score > 0) scored.push({e, score, info});
        }

        scored.sort((a,b)=>b.score-a.score);

        // If placeholder was absent but there are only 2 inputs, prefer q-field__native over hidden false field.
        if (!scored.length && allInputs.length) {
          for (const e of allInputs) {
            const cls = (e.className || '').toString().toLowerCase();
            const ph = (e.getAttribute('placeholder') || '').toLowerCase();
            const type = (e.getAttribute('type') || '').toLowerCase();
            const value = e.value || '';
            let score = 0;
            if (cls.includes('q-field__native')) score += 150;
            if (ph) score += 80;
            if (value === 'false' || type === 'hidden') score -= 300;
            if (ph.includes('поиск') || ph.includes('search')) score -= 300;
            if (score > 0) {
              scored.push({e, score, info: {
                tag:e.tagName, ph, cls:cls.slice(0,200), type, value,
                visible:isVisibleEnough(e), rect:rectOf(e)
              }});
            }
          }
          scored.sort((a,b)=>b.score-a.score);
        }

        if (!scored.length) {
          return {ok:false, inputs:infoAll, body:(document.body.innerText||'').slice(0,3000)};
        }

        return {ok:true, candidate:scored[0], all:scored.slice(0,15).map(x => ({score:x.score, info:x.info})), inputs:infoAll};
    """
    return driver.execute_script(script)


def set_delivered_qty_v4(driver, qty: str, debug_dir: Path) -> dict[str, Any]:
    qty = str(qty).strip()

    # Make page predictable.
    try:
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.5)
    except Exception:
        pass

    base.dump_debug(driver, debug_dir, "04_before_qty_v4")

    probe = None
    end = time.time() + 20
    while time.time() < end:
        probe = find_qty_input_any_state(driver)
        if probe.get("ok"):
            break
        time.sleep(0.5)

    dump_json(debug_dir, "04_qty_probe_v4.json", {k:v for k,v in (probe or {}).items() if k != "candidate"})

    if not probe or not probe.get("ok") or not probe.get("candidate") or not probe["candidate"].get("e"):
        base.dump_debug(driver, debug_dir, "04_qty_not_found_v4")
        return {"ok": False, "method": "any_state_dom", "probe": probe, "error": "Delivered quantity input not found even among hidden/offscreen inputs."}

    el = probe["candidate"].pop("e")

    # Force ONLY this input visible/focusable if it was offscreen/hidden.
    set_script = r"""
        const input = arguments[0];
        const val = String(arguments[1]);

        const before = {
          tag: input.tagName,
          ph: input.getAttribute('placeholder') || '',
          id: input.id || '',
          cls: (input.className || '').toString(),
          type: input.getAttribute('type') || '',
          value: input.value || '',
          rect: (() => { const r=input.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; })(),
          display: getComputedStyle(input).display,
          visibility: getComputedStyle(input).visibility,
          opacity: getComputedStyle(input).opacity
        };

        // Save old style to attributes for debug. Do not restore before confirm; we need it active.
        input.dataset.g2gOldStyle = input.getAttribute('style') || '';

        input.style.display = 'block';
        input.style.visibility = 'visible';
        input.style.opacity = '1';
        input.style.pointerEvents = 'auto';
        input.style.position = 'fixed';
        input.style.left = '900px';
        input.style.top = '170px';
        input.style.width = '180px';
        input.style.height = '48px';
        input.style.zIndex = '2147483647';
        input.removeAttribute('disabled');
        input.removeAttribute('readonly');
        input.setAttribute('tabindex', '0');

        function setNativeValue(el, value) {
          const proto =
            el.tagName === 'TEXTAREA' ? window.HTMLTextAreaElement.prototype :
            el.tagName === 'INPUT' ? window.HTMLInputElement.prototype :
            null;
          const setter = proto && Object.getOwnPropertyDescriptor(proto, 'value')?.set;
          if (setter) setter.call(el, value);
          else el.value = value;
        }
        function fire(el, type) {
          el.dispatchEvent(new Event(type, {bubbles:true, cancelable:true}));
        }
        function fireInput(el) {
          try {
            el.dispatchEvent(new InputEvent('input', {bubbles:true, cancelable:true, inputType:'insertText', data:el.value}));
          } catch (_) {
            fire(el, 'input');
          }
        }

        input.scrollIntoView({block:'center', inline:'center'});
        input.focus();
        setNativeValue(input, '');
        fireInput(input);
        fire(input, 'change');

        setNativeValue(input, val);
        fireInput(input);
        fire(input, 'change');

        input.dispatchEvent(new KeyboardEvent('keydown', {bubbles:true, cancelable:true, key:'Enter'}));
        input.dispatchEvent(new KeyboardEvent('keyup', {bubbles:true, cancelable:true, key:'Enter'}));
        input.blur();

        const after = {
          value: input.value || '',
          rect: (() => { const r=input.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; })(),
          display: getComputedStyle(input).display,
          visibility: getComputedStyle(input).visibility,
          opacity: getComputedStyle(input).opacity
        };
        return {before, after};
    """
    js_state = driver.execute_script(set_script, el, qty)
    time.sleep(0.5)

    # Keyboard pass for Vue/Quasar sync.
    keyboard_error = ""
    try:
        el.click()
        time.sleep(0.2)
        ActionChains(driver).key_down(Keys.CONTROL).send_keys("a").key_up(Keys.CONTROL).send_keys(Keys.BACKSPACE).send_keys(qty).perform()
        time.sleep(0.3)
        driver.execute_script("""
            arguments[0].dispatchEvent(new Event('input', {bubbles:true, cancelable:true}));
            arguments[0].dispatchEvent(new Event('change', {bubbles:true, cancelable:true}));
            arguments[0].blur();
        """, el)
    except Exception as e:
        keyboard_error = str(e)

    time.sleep(0.5)
    base.dump_debug(driver, debug_dir, "05_after_qty_set_v4")

    verify = driver.execute_script(r"""
        const arr = Array.from(document.querySelectorAll('input, textarea')).map(e => ({
          tag:e.tagName,
          ph:e.getAttribute('placeholder') || '',
          id:e.id || '',
          cls:(e.className || '').toString(),
          type:e.getAttribute('type') || '',
          value:e.value || '',
          rect:(() => { const r=e.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; })(),
          display:getComputedStyle(e).display,
          visibility:getComputedStyle(e).visibility,
          opacity:getComputedStyle(e).opacity
        }));
        const body = (document.body.innerText || '').slice(0,3000);
        return {inputs:arr, body};
    """)

    ok = any(
        str(x.get("value", "")).strip() == qty
        and (
            "достав" in str(x.get("ph","")).lower()
            or "deliver" in str(x.get("ph","")).lower()
            or "quantity" in str(x.get("ph","")).lower()
            or "q-field__native" in str(x.get("cls","")).lower()
        )
        for x in verify.get("inputs", [])
    )

    return {
        "ok": bool(ok),
        "method": "forced_dom_input",
        "target": {k:v for k,v in probe.items() if k not in {"candidate"}},
        "js_state": js_state,
        "keyboard_error": keyboard_error,
        "verify": verify,
        "error": "" if ok else "Input was found but value was not verified."
    }


def find_real_confirm_control(driver) -> dict[str, Any]:
    script = r"""
        function rectOf(e) {
          const r = e.getBoundingClientRect();
          return {x:r.x,y:r.y,w:r.width,h:r.height,top:r.top,left:r.left,bottom:r.bottom,right:r.right};
        }
        function visible(e) {
          if (!e) return false;
          const r = e.getBoundingClientRect();
          const s = getComputedStyle(e);
          return r.width > 5 && r.height > 5 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        }
        function textOf(e) { return (e.innerText || e.textContent || '').trim(); }

        const controls = new Map();

        // 1) Direct real clickable controls.
        for (const e of Array.from(document.querySelectorAll('button,.q-btn,[role="button"]'))) {
          const t = textOf(e).toLowerCase();
          if (t.includes('подтвердить доставку') || t.includes('confirm delivery')) {
            controls.set(e, {source:'direct_control'});
          }
        }

        // 2) Text node wrappers -> closest real clickable only.
        for (const e of Array.from(document.querySelectorAll('span,div,i'))) {
          const t = textOf(e).toLowerCase();
          if (!(t.includes('подтвердить доставку') || t.includes('confirm delivery'))) continue;
          const c = e.closest('button,.q-btn,[role="button"]');
          if (c) controls.set(c, {source:'closest_from_text', textNodeTag:e.tagName});
        }

        const qtyInput = Array.from(document.querySelectorAll('input,textarea')).find(e => {
          const ph = (e.getAttribute('placeholder') || '').toLowerCase();
          const val = (e.value || '').trim();
          return val && (ph.includes('достав') || ph.includes('deliver') || ph.includes('quantity') || (e.className || '').toString().toLowerCase().includes('q-field__native'));
        });
        const qr = qtyInput ? qtyInput.getBoundingClientRect() : null;

        const scored = [];
        for (const [e, meta] of controls.entries()) {
          const tag = e.tagName;
          const cls = (e.className || '').toString().toLowerCase();
          const t = textOf(e);
          const r = e.getBoundingClientRect();
          const disabled = !!e.disabled || e.getAttribute('aria-disabled') === 'true' || cls.includes('disabled');

          // Reject generic non-button DIV. Accept DIV only if it really is .q-btn or role=button.
          if (tag === 'DIV' && !cls.includes('q-btn') && e.getAttribute('role') !== 'button') continue;

          let score = 0;
          if (tag === 'BUTTON') score += 300;
          if (cls.includes('q-btn')) score += 250;
          if (e.getAttribute('role') === 'button') score += 160;
          if (t.toLowerCase().includes('подтвердить доставку')) score += 250;
          if (t.toLowerCase().includes('confirm delivery')) score += 250;
          if (cls.includes('bg-primary') || cls.includes('text-white') || cls.includes('g-btn')) score += 80;
          if (disabled) score -= 1000;

          // Avoid absurd page containers.
          if (r.width > 700 || r.height > 250) score -= 500;
          if (r.width < 20 || r.height < 10) score -= 100;

          if (qr) {
            const sameRow = Math.abs((r.y + r.height/2) - (qr.y + qr.height/2)) < 160;
            const rightOfQty = r.x > qr.x;
            if (sameRow) score += 100;
            if (rightOfQty) score += 60;
          }

          scored.push({
            e,
            score,
            tag,
            text:t.slice(0,300),
            cls:cls.slice(0,250),
            disabled,
            rect:rectOf(e),
            visible:visible(e),
            source:meta.source
          });
        }

        scored.sort((a,b)=>b.score-a.score);

        return {
          ok: scored.length > 0 && scored[0].score > 0,
          candidate: scored[0] || null,
          all: scored.slice(0,20),
          qtyInput: qtyInput ? {ph:qtyInput.getAttribute('placeholder')||'', value:qtyInput.value||'', cls:(qtyInput.className||'').toString(), rect:rectOf(qtyInput)} : null,
          body:(document.body.innerText||'').slice(0,2500)
        };
    """
    return driver.execute_script(script)


def click_confirm_delivery_v4(driver, debug_dir: Path) -> dict[str, Any]:
    # Verify qty before clicking.
    qty_state = driver.execute_script(r"""
        const inputs = Array.from(document.querySelectorAll('input,textarea')).map(e => ({
          ph:e.getAttribute('placeholder') || '',
          cls:(e.className || '').toString(),
          value:e.value || ''
        }));
        const good = inputs.find(x =>
          String(x.value).trim() &&
          (
            x.ph.toLowerCase().includes('достав') ||
            x.ph.toLowerCase().includes('deliver') ||
            x.ph.toLowerCase().includes('quantity') ||
            x.cls.toLowerCase().includes('q-field__native')
          )
        );
        return {ok:!!good, good:good || null, inputs};
    """)
    if not qty_state.get("ok"):
        base.dump_debug(driver, debug_dir, "06_no_qty_before_confirm_v4")
        return {"ok": False, "pre_qty": qty_state, "error": "Refusing confirm: quantity is not set."}

    probe = find_real_confirm_control(driver)
    dump_json(debug_dir, "06_confirm_probe_v4.json", {k:v for k,v in probe.items() if k != "candidate"})
    if not probe.get("ok") or not probe.get("candidate") or not probe["candidate"].get("e"):
        base.dump_debug(driver, debug_dir, "06_confirm_button_not_found_v4")
        return {"ok": False, "pre_qty": qty_state, "probe": probe, "error": "Real confirm button not found."}

    el = probe["candidate"].pop("e")
    cand = probe["candidate"]

    # If button is offscreen/hidden due responsive sticky bar, force just the BUTTON visible.
    click_state = driver.execute_script(r"""
        const btn = arguments[0];

        const before = {
          tag:btn.tagName,
          text:(btn.innerText||btn.textContent||'').trim(),
          cls:(btn.className||'').toString(),
          disabled:!!btn.disabled || btn.getAttribute('aria-disabled') === 'true',
          rect:(() => { const r=btn.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; })(),
          display:getComputedStyle(btn).display,
          visibility:getComputedStyle(btn).visibility,
          opacity:getComputedStyle(btn).opacity
        };

        if (before.disabled) return {ok:false, before, error:'button disabled'};

        btn.dataset.g2gOldStyle = btn.getAttribute('style') || '';
        btn.style.display = 'inline-flex';
        btn.style.visibility = 'visible';
        btn.style.opacity = '1';
        btn.style.pointerEvents = 'auto';
        btn.style.position = 'fixed';
        btn.style.left = '1100px';
        btn.style.top = '170px';
        btn.style.width = '180px';
        btn.style.height = '48px';
        btn.style.zIndex = '2147483647';

        btn.scrollIntoView({block:'center', inline:'center'});
        btn.click();

        const after = {
          rect:(() => { const r=btn.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; })()
        };
        return {ok:true, before, after};
    """, el)

    time.sleep(3)
    base.dump_debug(driver, debug_dir, "07_after_confirm_click_v4")

    # Confirmation modal if any: click real positive button only.
    modal_state = driver.execute_script(r"""
        function visible(e) {
          if (!e) return false;
          const r = e.getBoundingClientRect();
          const s = getComputedStyle(e);
          return r.width > 5 && r.height > 5 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
        }
        function textOf(e) { return (e.innerText || e.textContent || '').trim(); }
        const dialogs = Array.from(document.querySelectorAll('.q-dialog,[role="dialog"]')).filter(visible);
        const d = dialogs[dialogs.length - 1];
        if (!d) return {hasModal:false};

        const controls = Array.from(d.querySelectorAll('button,.q-btn,[role="button"]')).filter(e => {
          const t = textOf(e).toLowerCase();
          const cls = (e.className || '').toString().toLowerCase();
          const r = e.getBoundingClientRect();
          if (e.tagName === 'DIV' && !cls.includes('q-btn') && e.getAttribute('role') !== 'button') return false;
          if (r.width > 700 || r.height > 250) return false;
          return t.includes('подтверд') || t.includes('confirm') || t.includes('да') || t.includes('yes') || t === 'ok';
        }).map(e => {
          const t = textOf(e).toLowerCase();
          const cls = (e.className || '').toString().toLowerCase();
          let score = 200;
          if (e.tagName === 'BUTTON') score += 100;
          if (cls.includes('q-btn')) score += 80;
          if (t.includes('отмена') || t.includes('cancel')) score -= 1000;
          return {e, score, tag:e.tagName, text:textOf(e), cls:cls.slice(0,180), disabled:!!e.disabled || e.getAttribute('aria-disabled')==='true'};
        }).filter(x => x.score > 0 && !x.disabled);

        controls.sort((a,b)=>b.score-a.score);
        return {hasModal:true, text:(d.innerText||'').slice(0,1500), candidate:controls[0] || null, all:controls.slice(0,10)};
    """)
    modal_clicked = False
    if modal_state.get("hasModal") and modal_state.get("candidate") and modal_state["candidate"].get("e"):
        mel = modal_state["candidate"].pop("e")
        modal_clicked = driver.execute_script("arguments[0].click(); return true;", mel)
        time.sleep(4)
        base.dump_debug(driver, debug_dir, "08_after_modal_confirm_v4")

    return {
        "ok": bool(click_state.get("ok")),
        "pre_qty": qty_state,
        "button": cand,
        "click_state": click_state,
        "modal": modal_state,
        "modal_clicked": modal_clicked,
        "error": "" if click_state.get("ok") else click_state.get("error", "Confirm click failed.")
    }


def verify_delivered_v4(driver, order_id: str, debug_dir: Path, timeout: int = 90) -> dict[str, Any]:
    order_base = order_id.split("-", 1)[0].lower()
    script = f"""
        const txt = (document.body && document.body.innerText || '');
        const low = txt.toLowerCase();
        return {{
          url: location.href,
          hasOrder: low.includes({json.dumps(order_base)}),
          deliveredOneOfOne:
            low.includes('всего доставлено 1 / 1') ||
            low.includes('delivered 1 / 1') ||
            /всего\\s+доставлено\\s+1\\s*\\/\\s*1/.test(low) ||
            /delivered\\s+1\\s*\\/\\s*1/.test(low),
          awaitingBuyer:
            low.includes('ожидает подтверждения покупателя') ||
            low.includes('ожидание подтверждения покупателя') ||
            low.includes('awaiting buyer confirmation'),
          completed:
            low.includes('доставлено') ||
            low.includes('delivered'),
          stillConfirm:
            low.includes('подтвердить доставку') ||
            low.includes('confirm delivery'),
          bodySample: txt.slice(0,5000)
        }};
    """

    state = None
    end = time.time() + timeout
    while time.time() < end:
        try:
            state = driver.execute_script(script)
            if state.get("hasOrder") and (state.get("deliveredOneOfOne") or state.get("awaitingBuyer")):
                base.dump_debug(driver, debug_dir, "09_verified_delivered_v4")
                return {"ok": True, "state": state}
        except Exception as e:
            state = {"error": str(e)}
        time.sleep(1)

    try:
        driver.refresh()
        time.sleep(6)
        state = driver.execute_script(script)
        base.dump_debug(driver, debug_dir, "10_verify_after_reload_v4")
        if state.get("hasOrder") and (state.get("deliveredOneOfOne") or state.get("awaitingBuyer")):
            return {"ok": True, "after_reload": True, "state": state}
    except Exception as e:
        state = {"error": str(e)}

    return {"ok": False, "state": state}


base.check_proof_gallery = check_proof_gallery_v4
base.set_delivered_qty = set_delivered_qty_v4
base.click_confirm_delivery = click_confirm_delivery_v4
base.verify_delivered = verify_delivered_v4

if __name__ == "__main__":
    base.main()
