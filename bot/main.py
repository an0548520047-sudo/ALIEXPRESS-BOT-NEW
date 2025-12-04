from __future__ import annotations

import asyncio
import os
import re
import time
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Tuple
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession

HISTORY_FILE = "history.txt"
MAX_HISTORY_SIZE = 200
MAX_POST_AGE_HOURS = 24  # רק פוסטים מהיממה האחרונה

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        # Fallback
        alt = name.replace("ALIEXPRESS_", "AFFILIATE_")
        value = os.getenv(alt)
        if not value or not value.strip():
            return ""
    return value.strip()

def _optional_str(name: str) -> str | None:
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else None

@dataclass
class Config:
    tg_api_id: int
    tg_api_hash: str
    tg_session: str
    tg_source_channels: List[str]
    tg_target_channel: str
    affiliate_app_key: str | None
    affiliate_app_secret: str | None
    affiliate_portal_link: str | None
    openai_api_key: str
    openai_model: str
    max_messages_per_channel: int
    max_posts_per_run: int

    @classmethod
    def from_env(cls) -> "Config":
        channels = [c.strip() for c in _require_env("TG_SOURCE_CHANNELS").split(",") if c.strip()]
        app_key = _optional_str("ALIEXPRESS_APP_KEY") or _optional_str("ALIEXPRESS_API_APP_KEY")
        app_secret = _optional_str("ALIEXPRESS_APP_SECRET") or _optional_str("ALIEXPRESS_API_APP_SECRET")
        portal_link = _optional_str("AFFILIATE_PORTAL_LINK") # חובה להגדיר בסודות אם רוצים גיבוי ל-API

        return cls(
            tg_api_id=int(_require_env("TG_API_ID")),
            tg_api_hash=_require_env("TG_API_HASH"),
            tg_session=_require_env("TG_SESSION"),
            tg_source_channels=channels,
            tg_target_channel=_require_env("TG_TARGET_CHANNEL"),
            affiliate_app_key=app_key,
            affiliate_app_secret=app_secret,
            affiliate_portal_link=portal_link,
            openai_api_key=_require_env("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            max_messages_per_channel=50,
            max_posts_per_run=10,
        )

def resolve_short_link(url: str) -> str:
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
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

    def _sign_params(self, params: Dict[str, str]) -> str:
        if not self.config.affiliate_app_secret: return ""
        sorted_keys = sorted(params.keys())
        param_str = "".join([f"{key}{params[key]}" for key in sorted_keys])
        sign_source = f"{self.config.affiliate_app_secret}{param_str}{self.config.affiliate_app_secret}"
        return hashlib.md5(sign_source.encode()).hexdigest().upper()

    def get_link(self, original_url: str) -> str:
        # 1. Resolve
        if "s.click" in original_url or "bit.ly" in original_url:
            resolved = resolve_short_link(original_url)
        else:
            resolved = original_url

        # 2. Clean URL
        item_id = extract_item_id(resolved)
        if not item_id: return resolved
        clean_url = f"https://www.aliexpress.com/item/{item_id}.html"

        # 3. Try API
        if self.config.affiliate_app_key and self.config.affiliate_app_secret:
            print(f"Testing API for {item_id}...")
            try:
                timestamp = str(int(time.time() * 1000))
                params = {
                    "app_key": self.config.affiliate_app_key,
                    "timestamp": timestamp,
                    "sign_method": "md5",
                    "urls": clean_url,
                    "promotion_link_type": "2", # Hot Link
                    "tracking_id": "default",
                    "format": "json",
                    "v": "2.0",
                    "method": "aliexpress.affiliate.link.generate"
                }
                params["sign"] = self._sign_params(params)
                
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post("https://api-sg.aliexpress.com/sync", data=params, headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
                    data = resp.json()
                    if "aliexpress_affiliate_link_generate_response" in data:
                        result = data["aliexpress_affiliate_link_generate_response"].get("resp_result", {}).get("result", {})
                        promos = result.get("promotion_links", {}).get("promotion_link", [])
                        if promos and "s.click" in promos[0].get("promotion_link", ""):
                            return promos[0].get("promotion_link")
            except Exception as e:
                print(f"API Failed: {e}")

        # 4. Try Portal Template (Backup)
        if self.config.affiliate_portal_link and "{url}" in self.config.affiliate_portal_link:
            print("Using Portal Backup")
            return self.config.affiliate_portal_link.replace("{url}", quote(clean_url, safe=""))

        # 5. Fail
        print("No affiliate link generated, returning clean link")
        return clean_url

class CaptionWriter:
    def __init__(self, openai_client: OpenAI, model: str):
        self.client = openai_client
        self.model = model

    def write(self, orig_text: str) -> str:
        prompt = f"תכתוב תיאור מכירתי קצר בעברית (2 משפטים + אימוג'י) למוצר הזה: {orig_text[:200]}"
        try:
            res = self.client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": prompt}], temperature=0.7, max_tokens=200
            )
            return res.choices[0].message.content.strip()
        except:
            return "מצאתי דיל שווה! 👇"

class DealBot:
    def __init__(self, client: TelegramClient, writer: CaptionWriter, builder: AffiliateLinkBuilder, config: Config):
        self.client = client
        self.writer = writer
        self.builder = builder
        self.config = config
        self.processed_ids = []

    def load_history(self):
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                self.processed_ids = [line.strip() for line in f if line.strip()][-MAX_HISTORY_SIZE:]

    def save_history(self):
        with open(HISTORY_FILE, "w") as f:
            f.write("\n".join(self.processed_ids[-MAX_HISTORY_SIZE:]))

    async def run(self):
        self.load_history()
        print("Bot started...")
        count = 0

        for channel in self.config.tg_source_channels:
            print(f"Scanning {channel}...")
            async for msg in self.client.iter_messages(channel, limit=self.config.max_messages_per_channel):
                if not msg.message or count >= self.config.max_posts_per_run: break
                
                # 1. Time Filter (Only last 24h)
                if msg.date:
                    age = datetime.now(timezone.utc) - msg.date
                    if age > timedelta(hours=MAX_POST_AGE_HOURS):
                        continue # Skip old messages

                # 2. Find Link
                urls = re.findall(r"https?://[^\s]+", msg.message)
                ali_url = next((u for u in urls if "aliexpress" in u or "bit.ly" in u), None)
                if not ali_url: continue

                # 3. Check Duplication
                # Quick resolve for checking ID
                resolved_check = resolve_short_link(ali_url) if "s.click" in ali_url else ali_url
                item_id = extract_item_id(resolved_check)
                
                if not item_id or item_id in self.processed_ids:
                    continue

                # 4. Process
                aff_link = self.builder.get_link(ali_url)
                caption = self.writer.write(msg.message)
                text = f"{caption}\n\n👇 {aff_link}"

                # 5. Send (With Original Image!)
                try:
                    if msg.media:
                        # Download and re-upload the media (ensures image exists)
                        await self.client.send_file(self.config.tg_target_channel, msg.media, caption=text)
                    else:
                        # No image in original post, send text only
                        await self.client.send_message(self.config.tg_target_channel, text, link_preview=True)

                    print(f"✅ Posted: {item_id}")
                    self.processed_ids.append(item_id)
                    count += 1
                except Exception as e:
                    print(f"Error sending: {e}")

        self.save_history()

async def main():
    config = Config.from_env()
    client = TelegramClient(StringSession(config.tg_session), config.tg_api_id, config.tg_api_hash)
    oa_client = OpenAI(api_key=config.openai_api_key)
    bot = DealBot(client, CaptionWriter(oa_client, config.openai_model), AffiliateLinkBuilder(config), config)
    await client.start()
    async with client:
        await bot.run()

if __name__ == "__main__":
    asyncio.run(main())
