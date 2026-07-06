# g2g_make_twitch_proof_one_strict_v1.py
# Makes ONE Twitch inventory proof in a fresh Python process.
#
# Usage:
#   python .\g2g_make_twitch_proof_one_strict_v1.py ORDER_ID OFFER_ID LOGIN PASSWORD INDEX
#
# This is used by g2g_auto_deliver_strict_v8_multi_standalone.py to avoid
# stale TwitchProofMaker/browser state between multiple accounts.

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any


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


def safe_filename_part(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:60] or "account"


def build_proxy_wrapper(gb, env: dict[str, str]):
    logs = []

    for name in ["ProxyWrapper", "BrowserProxyWrapper", "ProxyAuthWrapper", "BrowserProxy", "ProxyServerWrapper"]:
        if hasattr(gb, name):
            cls = getattr(gb, name)
            for label, args in [("no_args", ()), ("env_dict", (env,))]:
                try:
                    obj = cls(*args)
                    logs.append(f"proxy wrapper ok: {name}({label})")
                    return obj, logs
                except Exception as e:
                    logs.append(f"proxy wrapper fail: {name}({label}): {type(e).__name__}: {e}")

    logs.append("proxy wrapper fallback: None")
    return None, logs


def main() -> None:
    if len(sys.argv) < 6:
        print(json.dumps({
            "ok": False,
            "error": "Usage: python g2g_make_twitch_proof_one_strict_v1.py ORDER_ID OFFER_ID LOGIN PASSWORD INDEX"
        }, ensure_ascii=False, indent=2))
        return

    order_id = sys.argv[1]
    offer_id = sys.argv[2]
    login = sys.argv[3]
    password = sys.argv[4]
    idx = int(sys.argv[5])

    # Force clean proof browser behavior for every account.
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    os.environ["TWITCH_CLEAR_PROFILE_EACH_RUN"] = "1"
    os.environ["TWITCH_PROOF_KEEP_BROWSER_OPEN"] = "0"

    env = read_env()
    env["TWITCH_CLEAR_PROFILE_EACH_RUN"] = "1"
    env["TWITCH_PROOF_KEEP_BROWSER_OPEN"] = "0"

    result = {
        "ok": False,
        "order_id": order_id,
        "offer_id": offer_id,
        "login": login,
        "proof_path": "",
        "raw_screenshot_path": "",
        "error": "",
        "logs": [],
    }

    try:
        import g2g_bot as gb
    except Exception as e:
        result["error"] = f"Could not import g2g_bot.py: {type(e).__name__}: {e}"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if not hasattr(gb, "TwitchProofMaker"):
        result["error"] = "g2g_bot.py has no TwitchProofMaker"
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    try:
        proxy_wrapper, p_logs = build_proxy_wrapper(gb, env)
        result["logs"].extend(p_logs)

        try:
            maker = gb.TwitchProofMaker(proxy_wrapper)
            result["logs"].append("TwitchProofMaker(proxy_wrapper) ok")
        except TypeError:
            maker = gb.TwitchProofMaker()
            result["logs"].append("TwitchProofMaker() ok")

        if not hasattr(maker, "make_inventory_proof"):
            result["error"] = "TwitchProofMaker has no make_inventory_proof method"
            result["logs"].append("public methods: " + ", ".join([n for n in dir(maker) if not n.startswith("_")]))
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        payload = f"Login: {login} Password: {password}"
        res = maker.make_inventory_proof(order_id, offer_id, payload)

        if hasattr(maker, "quit"):
            try:
                maker.quit()
            except Exception:
                pass

        result["maker_result"] = res

        if not isinstance(res, dict) or not res.get("ok"):
            result["error"] = (res or {}).get("error", "make_inventory_proof returned not ok") if isinstance(res, dict) else "make_inventory_proof returned non-dict"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        src = Path(res.get("proof_path", ""))
        raw_src = Path(res.get("raw_screenshot_path", "")) if res.get("raw_screenshot_path") else None

        if not src.exists():
            result["error"] = f"maker proof_path does not exist: {src}"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return

        proof_dir = Path(env.get("PROOFS_DIR", "proofs_g2g")) / order_id
        proof_dir.mkdir(parents=True, exist_ok=True)

        unique = proof_dir / f"{idx:02d}_twitch_inventory_proof_{safe_filename_part(login)}.png"
        shutil.copy2(src, unique)

        raw_unique = ""
        if raw_src and raw_src.exists():
            raw_dst = proof_dir / f"{idx:02d}_twitch_inventory_raw_{safe_filename_part(login)}.png"
            shutil.copy2(raw_src, raw_dst)
            raw_unique = str(raw_dst)

        result["ok"] = True
        result["proof_path"] = str(unique)
        result["raw_screenshot_path"] = raw_unique
        result["error"] = ""

        print(json.dumps(result, ensure_ascii=False, indent=2))

    except Exception as e:
        result["error"] = f"Exception: {type(e).__name__}: {e}"
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
