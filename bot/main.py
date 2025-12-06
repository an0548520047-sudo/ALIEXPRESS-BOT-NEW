from __future__ import annotations

import asyncio
import os
import re
import time
import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional
from datetime import datetime

import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl

# ==================
# Config & Helpers
# ==================

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        alt = name.replace("API_", "")
        value = os.getenv(alt) or os.getenv(name.replace("ALIEXPRESS_", "AFFILIATE_"))
        if not value or not value.strip():
            if "APP_KEY" in name or "SESSION" in name or "HASH" in name:
                raise RuntimeError(f"Missing required env: {name}")
            return ""
    return value.strip()

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except:
        return default

@dataclass
class Config:
    tg_api_id: int
    tg_api_hash: str
    tg_session: str
    tg_source_channels: List[str]
    tg_target_channel: str
    affiliate_app_key: str
    affiliate_app_secret: str
    openai_api_key: str
    openai_model: str
    max_messages_per_channel: int
    max_posts_per_run: int
    min_rating: float = 4.6
    min_orders: int = 500

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            tg_api_id=int(_require_env("TG_API_ID")),
            tg_api_hash=_require_env("TG_API_HASH"),
            tg_session=_require_env("TG_SESSION"),
            tg_source_channels=[c.strip() for c in _require_env("TG_SOURCE_CHANNELS").split(",") if c.strip()],
            tg_target_channel=_require_env("TG_TARGET_CHANNEL"),
            affiliate_app_key=_require_env("ALIEXPRESS_APP_KEY"),
            affiliate_app_secret=_require_env("ALIEXPRESS_APP_SECRET"),
            openai_api_key=_require_env("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            max_messages_per_channel=_int_env("MAX_MESSAGES_PER_CHANNEL", 250),
            max_posts_per_run=_int_env("MAX_POSTS_PER_RUN", 10)
        )

# =======================
# Logic Utils
# =======================

def resolve_url_smart(url: str) -> str:
    if "aliexpress" in url and "s.click" not in url:
        return url
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.head(url, follow_redirects=True)
            return str(resp.url).replace("m.aliexpress", "www.aliexpress")
    except:
        return url

def extract_item_id(url: str) -> str | None:
    match = re.search(r"/item/(\d+)\.html", url)
    if match: return match.group(1)
    match = re.search(r"(\d{10,})", url)
    return match.group(1) if match else None

# =======================
# AliExpress API Class
# =======================

class AliExpressAPI:
    def __init__(self, config: Config):
        self.config = config
        self.base_url = "https://api-sg.aliexpress.com/router/rest"

    def _sign(self, params: Dict[str, str]) -> str:
        s = "".join([f"{k}{params[k]}" for k in sorted(params.keys())])
        s = f"{self.config.affiliate_app_secret}{s}{self.config.affiliate_app_secret}"
        return hashlib.md5(s.encode("utf-8")).hexdigest().upper()

    def get_product_details(self, item_id: str) -> Dict | None:
        print(f"🔍 Checking quality for item: {item_id}")
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        params = {
            "app_key": self.config.affiliate_app_key,
            "timestamp": current_time,
            "sign_method": "md5",
            "method": "aliexpress.affiliate.product.detail.get",
            "product_ids": item_id,
            "target_currency": "ILS",
            "target_language": "HE",
            "tracking_id": "bot_check",
            "format": "json",
            "v": "2.0"
        }
        params["sign"] = self._sign(params)

        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(
                    self.base_url, 
                    data=params, 
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
                )
                
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    print(f"⚠️ Critical: API returned non-JSON. Status: {resp.status_code}")
                    return None

                if "error_response" in data:
                    err = data["error_response"]
                    print(f"🛑 API ERROR for {item_id}: {err.get('msg')} | {err.get('sub_msg')}")
                    return None

                response_root = data.get("aliexpress_affiliate_product_detail_get_response")
                if not response_root:
                    print(f"⚠️ Unexpected JSON structure")
                    return None

                resp_result = response_root.get("resp_result", {})
                if resp_result.get("resp_code") != 200:
                    print(f"⚠️ Logic Error (Item {item_id}): {resp_result.get('resp_msg')}")
                    return None

                result_data = resp_result.get("result")
                if not result_data:
                    print(f"⚠️ Item {item_id} valid but no data returned.")
                    return None

                products = result_data.get("products", {}).get("product")
                if products:
                    return products[0]
                else:
                    print(f"⚠️ Item {item_id} product list is empty.")
                    return None

        except Exception as e:
            print(f"⚠️ Connection/HTTP Exception: {e}")
        return None

    def generate_link(self, url: str) -> str | None:
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        params = {
            "app_key": self.config.affiliate_app_key,
            "timestamp": current_time,
            "sign_method": "md5",
            "method": "aliexpress.affiliate.link.generate",
            "urls": url,
            "promotion_link_type": "2",
            "tracking_id": f"tg_bot_{datetime.now().strftime('%m%d')}",
            "format": "json",
            "v": "2.0"
        }
        params["sign"] = self._sign(params)

        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(self.base_url, data=params, headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
                data = resp.json()
                
                if "error_response" in data:
                    print(f"🛑 Link Gen Error: {data['error_response'].get('sub_msg')}")
                    return None
