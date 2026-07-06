"""
G2G Fast HTTP Client v2.1
Гибридный клиент: requests для scan/inspect, fallback на браузер для delivery-шагов.
"""

import os
import re
import json
import time
import logging
from pathlib import Path
from typing import Optional, Dict, List, Any
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

ENV_PATH = Path(".env")

@dataclass
class G2GConfig:
    orders_url: Optional[str] = None
    chat_endpoint: Optional[str] = None
    upload_endpoint: Optional[str] = None
    confirm_endpoint: Optional[str] = None
    start_delivery_endpoint: Optional[str] = None
    user_agent: str = ""
    cookie_header: str = ""
    scan_enabled: bool = False

    def has_delivery_endpoints(self) -> bool:
        return all([self.chat_endpoint, self.upload_endpoint, self.confirm_endpoint, self.start_delivery_endpoint])


def read_fast_config() -> G2GConfig:
    cfg = G2GConfig()
    if ENV_PATH.exists():
        env_text = ENV_PATH.read_text(encoding="utf-8")
    else:
        env_text = ""
        logger.warning(".env не найден, используем os.environ")

    def _get(key: str, default: str = "") -> str:
        pattern = re.compile(rf"^{key}=(.+)$", re.MULTILINE)
        m = pattern.search(env_text)
        if m:
            return m.group(1).strip().strip('"').strip("'")
        return os.environ.get(key, default)

    cfg.orders_url = _get("G2G_REQUESTS_ORDERS_URL") or None
    cfg.chat_endpoint = _get("G2G_CHAT_API_ENDPOINT") or None
    cfg.upload_endpoint = _get("G2G_UPLOAD_API_ENDPOINT") or None
    cfg.confirm_endpoint = _get("G2G_CONFIRM_API_ENDPOINT") or None
    cfg.start_delivery_endpoint = _get("G2G_START_DELIVERY_ENDPOINT") or None
    cfg.user_agent = _get(
        "G2G_USER_AGENT",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:149.0) Gecko/20100101 Firefox/149.0",
    )
    cfg.cookie_header = _get("G2G_COOKIE_HEADER", "")
    cfg.scan_enabled = _get("G2G_REQUESTS_SCAN_ENABLED", "0") == "1"
    return cfg


def _session(cfg: G2GConfig) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": cfg.user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cookie": cfg.cookie_header,
    })
    return s


def scan_orders(cfg: G2GConfig) -> Dict[str, Any]:
    if not cfg.scan_enabled or not cfg.orders_url:
        return {"orders": [], "needs_browser": True, "error": "scan not configured"}
    try:
        s = _session(cfg)
        resp = s.get(cfg.orders_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        orders = _parse_orders(soup)
        logger.info(f"[FAST] Найдено заказов: {len(orders)}")
        return {"orders": orders, "needs_browser": False, "error": None}
    except Exception as e:
        logger.error(f"[FAST] Scan failed: {e}")
        return {"orders": [], "needs_browser": True, "error": str(e)}


def _parse_orders(soup: BeautifulSoup) -> List[Dict[str, Any]]:
    orders = []
    rows = soup.select("table.order-table tbody tr") or soup.select(".order-item")
    for row in rows:
        try:
            order_id = row.get("data-order-id") or ""
            if not order_id:
                link = row.select_one("a[href*='/order/']")
                if link:
                    href = link.get("href", "")
                    order_id = href.split("/order/")[-1].split("/")[0]
            status_elem = row.select_one(".order-status, .status")
            status = status_elem.get_text(strip=True) if status_elem else "unknown"
            if any(k in status.lower() for k in ["pending", "processing", "awaiting", "deliver"]):
                orders.append({"order_id": order_id, "status": status, "raw_html": str(row)[:500]})
        except Exception:
            continue
    return orders


def inspect_order(cfg: G2GConfig, order_id: str) -> Dict[str, Any]:
    if not cfg.scan_enabled or not cfg.orders_url:
        return {"needs_browser": True, "error": "inspect not configured"}
    try:
        s = _session(cfg)
        detail_url = cfg.orders_url.rstrip("/").rsplit("/", 1)[0] + f"/order/{order_id}"
        resp = s.get(detail_url, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        return {
            "order_id": order_id,
            "buyer": _safe_text(soup, ".buyer-name, .customer-name"),
            "game": _safe_text(soup, ".game-title, .product-name"),
            "amount": _safe_text(soup, ".order-amount, .quantity"),
            "chat_url": _safe_attr(soup, "a[href*='/chat/']", "href"),
            "needs_browser": False,
        }
    except Exception as e:
        logger.error(f"[FAST] Inspect failed: {e}")
        return {"needs_browser": True, "error": str(e)}


def _safe_text(soup: BeautifulSoup, selector: str) -> str:
    el = soup.select_one(selector)
    return el.get_text(strip=True) if el else ""


def _safe_attr(soup: BeautifulSoup, selector: str, attr: str) -> str:
    el = soup.select_one(selector)
    return el.get(attr, "") if el else ""


def send_chat(cfg: G2GConfig, order_id: str, message: str) -> Dict[str, Any]:
    if not cfg.chat_endpoint:
        return {"needs_browser": True, "reason": "chat_endpoint not configured"}
    try:
        s = _session(cfg)
        payload = {"order_id": order_id, "message": message}
        resp = s.post(cfg.chat_endpoint, json=payload, timeout=10)
        return {"success": resp.status_code == 200, "status_code": resp.status_code}
    except Exception as e:
        return {"needs_browser": True, "error": str(e)}


def upload_proof(cfg: G2GConfig, order_id: str, image_path: str) -> Dict[str, Any]:
    if not cfg.upload_endpoint:
        return {"needs_browser": True, "reason": "upload_endpoint not configured"}
    try:
        s = _session(cfg)
        with open(image_path, "rb") as f:
            files = {"file": (Path(image_path).name, f, "image/png")}
            data = {"order_id": order_id}
            resp = s.post(cfg.upload_endpoint, data=data, files=files, timeout=30)
        return {"success": resp.status_code == 200, "status_code": resp.status_code}
    except Exception as e:
        return {"needs_browser": True, "error": str(e)}


def confirm_delivery(cfg: G2GConfig, order_id: str, qty: int = 1) -> Dict[str, Any]:
    if not cfg.confirm_endpoint:
        return {"needs_browser": True, "reason": "confirm_endpoint not configured"}
    try:
        s = _session(cfg)
        payload = {"order_id": order_id, "quantity": qty}
        resp = s.post(cfg.confirm_endpoint, json=payload, timeout=10)
        return {"success": resp.status_code == 200, "status_code": resp.status_code}
    except Exception as e:
        return {"needs_browser": True, "error": str(e)}


def test_connection(cfg: G2GConfig) -> bool:
    try:
        s = _session(cfg)
        resp = s.get("https://www.g2g.com/", timeout=10, allow_redirects=True)
        if "login" in resp.url.lower() or resp.status_code != 200:
            logger.warning("[TEST] Возможно, cookies протухли — редирект на логин")
            return False
        return True
    except Exception as e:
        logger.error(f"[TEST] Connection failed: {e}")
        return False
