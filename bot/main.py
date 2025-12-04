from __future__ import annotations

import asyncio
import os
import re
import time
import hashlib
from dataclasses import dataclass
from typing import Dict, List
from datetime import datetime, timedelta
import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession

HISTORY_FILE = "history.txt"
MAX_HISTORY_SIZE = 200

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        alt = name.replace("ALIEXPRESS_", "AFFILIATE_")
        value = os.getenv(alt)
    return value.strip() if value else ""

def _optional_str(name: str) -> str | None:
    val = os.getenv(name)
    return val.strip() if val else None

@dataclass
class Config:
    tg_api_id: int
    tg_api_hash: str
    tg_session: str
    tg_source_channels: List[str]
    tg_target_channel: str
    affiliate_app_key: str | None
    affiliate_app_secret: str | None
    openai_api_key: str
    openai_model: str

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            tg_api_id=int(_require_env("TG_API_ID")),
            tg_api_hash=_require_env("TG_API_HASH"),
            tg_session=_require_env("TG_SESSION"),
            tg_source_channels=[c.strip() for c in _require_env("TG_SOURCE_CHANNELS").split(",")],
            tg_target_channel=_require_env("TG_TARGET_CHANNEL"),
            affiliate_app_key=_optional_str("ALIEXPRESS_APP_KEY") or _optional_str("ALIEXPRESS_API_APP_KEY"),
            affiliate_app_secret=_optional_str("ALIEXPRESS_APP_SECRET") or _optional_str("ALIEXPRESS_API_APP_SECRET"),
            openai_api_key=_require_env("OPENAI_API_KEY"),
            openai_model="gpt-4o-mini"
        )

def resolve_short_link(url: str) -> str:
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            return str(client.head(url, follow_redirects=True).url).replace("m.aliexpress", "www.aliexpress")
    except:
        return url

def extract_item_id(url: str) -> str | None:
    match = re.search(r"/item/(\d+)\.html", url)
    if match: return match.group(1)
    match = re.search(r"(\d{10,})", url)
    return match.group(1) if match else None

class AffiliateLinkBuilder:
    def __init__(self, config: Config):
        self.config = config

    def _sign(self, params):
        s = "".join([f"{k}{params[k]}" for k in sorted(params.keys())])
        s = f"{self.config.affiliate_app_secret}{s}{self.config.affiliate_app_secret}"
        return hashlib.md5(s.encode()).hexdigest().upper()

    def get_link(self, original_url: str) -> str:
        # 1. Resolve & Clean
        resolved = resolve_short_link(original_url) if "s.click" in original_url or "bit.ly" in original_url else original_url
        item_id = extract_item_id(resolved)
        if not item_id: return resolved
        
        clean_url = f"https://www.aliexpress.com/item/{item_id}.html"
        
        # 2. Try API (כמו שעבד לך קודם)
        if self.config.affiliate_app_key and self.config.affiliate_app_secret:
            try:
                params = {
                    "app_key": self.config.affiliate_app_key,
                    "timestamp": str(int(time.time() * 1000)),
                    "sign_method": "md5",
                    "urls": clean_url,
                    "promotion_link_type": "2",
                    "tracking_id": "default",
                    "format": "json",
                    "v": "2.0",
                    "method": "aliexpress.affiliate.link.generate"
                }
                params["sign"] = self._sign(params)
                with httpx.Client(timeout=10) as client:
                    resp = client.post("https://api-sg.aliexpress.com/sync", data=params, headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
                    data = resp.json()
                    # חילוץ הלינק
                    if "aliexpress_affiliate_link_generate_response" in data:
                        res = data["aliexpress_affiliate_link_generate_response"]["resp_result"]["result"]
                        if "promotion_links" in res and res["promotion_links"]["promotion_link"]:
                            link = res["promotion_links"]["promotion_link"][0]["promotion_link"]
                            if "s.click" in link: return link
            except:
                pass
        
        # אם נכשל - מחזיר נקי (כמו בגרסה שעבדה)
        return clean_url

class CaptionWriter:
    def __init__(self, openai_client: OpenAI, model: str):
        self.client = openai_client
        self.model = model

    def write(self, orig_text: str) -> str:
        try:
            res = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": f"תכתוב פוסט מכירה קצרצר ומושך בעברית למוצר הזה (בלי האשטאג): {orig_text[:200]}"}],
                temperature=0.7, max_tokens=150
            )
            return res.choices[0].message.content.strip()
        except:
            return "דיל מעולה! 👇"

async def main():
    config = Config.from_env()
    client = TelegramClient(StringSession(config.tg_session), config.tg_api_id, config.tg_api_hash)
    oa_client = OpenAI(api_key=config.openai_api_key)
    builder = AffiliateLinkBuilder(config)
    writer = CaptionWriter(oa_client, config.openai_model)
    
    # טעינת היסטוריה
    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            history = [line.strip() for line in f if line.strip()][-MAX_HISTORY_SIZE:]

    print("Bot started...")
    await client.start()
    
    count = 0
    for channel in config.tg_source_channels:
        print(f"Scanning {channel}...")
        async for msg in client.iter_messages(channel, limit=30):
            if not msg.message or count >= 10: break
            
            # בדיקת זמן (רק 24 שעות אחרונות) - כדי לא להעלות עתיקות
            if msg.date and (datetime.now(msg.date.tzinfo) - msg.date).days > 1:
                continue

            # חילוץ לינק
            urls = re.findall(r"https?://[^\s]+", msg.message)
            ali_url = next((u for u in urls if "aliexpress" in u or "bit.ly" in u), None)
            if not ali_url: continue

            # בדיקת כפילות
            resolved = resolve_short_link(ali_url) if "s.click" in ali_url else ali_url
            item_id = extract_item_id(resolved)
            if not item_id or item_id in history: continue

            # יצירת תוכן
            aff_link = builder.get_link(ali_url)
            caption = writer.write(msg.message)
            text = f"{caption}\n\n👇 {aff_link}"

            # שליחה (עם המדיה המקורית!)
            try:
                if msg.media:
                    await client.send_file(config.tg_target_channel, msg.media, caption=text)
                else:
                    await client.send_message(config.tg_target_channel, text, link_preview=True)
                
                print(f"✅ Posted: {item_id}")
                history.append(item_id)
                count += 1
            except Exception as e:
                print(f"Error: {e}")

    # שמירה
    with open(HISTORY_FILE, "w") as f:
        f.write("\n".join(history[-MAX_HISTORY_SIZE:]))

if __name__ == "__main__":
    asyncio.run(main())
