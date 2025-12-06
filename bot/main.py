from __future__ import annotations

import asyncio
import os
import re
import time
import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Optional, Any
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
        # Fallback to alternative names just in case
        alt = name.replace("API_", "")
        value = os.getenv(alt) or os.getenv(name.replace("ALIEXPRESS_", "AFFILIATE_"))
        
        if not value or not value.strip():
            # Critical variables that must exist
            if any(x in name for x in ["APP_KEY", "SESSION", "HASH", "API_ID"]):
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
    """Follows redirects to get the real product URL."""
    if "aliexpress" in url and "s.click" not in url and "/item/" in url:
        return url
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.head(url, follow_redirects=True)
            final_url = str(resp.url)
            # Normalize mobile links to desktop
            return final_url.replace("m.aliexpress", "www.aliexpress")
    except:
        return url

def extract_item_id(url: str) -> str | None:
    """Extracts the numeric item ID from the URL."""
    # Pattern 1: Standard .html
    match = re.search(r"/item/(\d+)\.html", url)
    if match: return match.group(1)
    
    # Pattern 2: Just a long number sequence
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
        # Sort parameters alphabetically
        sorted_keys = sorted(params.keys())
        # Create string: secret + key1value1key2value2... + secret
        s = "".join([f"{k}{params[k]}" for k in sorted_keys])
        s = f"{self.config.affiliate_app_secret}{s}{self.config.affiliate_app_secret}"
        # MD5 Hash and Upper case
        return hashlib.md5(s.encode("utf-8")).hexdigest().upper()

    def _send_request(self, method: str, api_params: Dict[str, str]) -> Optional[Dict]:
        """Centralized method to handle signing and sending requests."""
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        system_params = {
            "app_key": self.config.affiliate_app_key,
            "timestamp": current_time,
            "sign_method": "md5",
            "method": method,
            "format": "json",
            "v": "2.0"
        }
        
        # Merge system params with specific API params
        full_params = {**system_params, **api_params}
        full_params["sign"] = self._sign(full_params)

        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(
                    self.base_url, 
                    data=full_params, 
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
                )
                
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    print(f"⚠️ Critical: API returned non-JSON. Status: {resp.status_code}")
                    return None

                # Check for top-level errors
                if "error_response" in data:
                    err = data["error_response"]
                    print(f"🛑 API ERROR ({method}): {err.get('msg')} | {err.get('sub_msg')}")
                    return None
                
                return data

        except Exception as e:
            print(f"⚠️ Connection Exception: {e}")
            return None

    def get_product_details(self, item_id: str) -> Dict | None:
        print(f"🔍 Checking quality for item: {item_id}")
        
        params = {
            "product_ids": item_id,
            "target_currency": "ILS",
            "target_language": "HE",
            "tracking_id": "bot_check"
        }

        data = self._send_request("aliexpress.affiliate.product.detail.get", params)
        if not data:
            return None

        response_root = data.get("aliexpress_affiliate_product_detail_get_response")
        if not response_root:
            print(f"⚠️ Unexpected JSON structure for item {item_id}")
            return None

        resp_result = response_root.get("resp_result", {})
        if resp_result.get("resp_code") != 200:
            print(f"⚠️ Logic Error (Item {item_id}): {resp_result.get('resp_msg')}")
            return None

        result_data = resp_result.get("result")
        if not result_data:
            print(f"⚠️ Item {item_id} valid but no data returned (Sold out/Restricted).")
            return None

        products = result_data.get("products", {}).get("product")
        if products:
            return products[0]
        else:
            print(f"⚠️ Item {item_id} product list is empty.")
            return None

    def generate_link(self, url: str) -> str | None:
        params = {
            "urls": url,
            "promotion_link_type": "2", # 2 = Hot Link
            "tracking_id": f"tg_bot_{datetime.now().strftime('%m%d')}"
        }

        data = self._send_request("aliexpress.
