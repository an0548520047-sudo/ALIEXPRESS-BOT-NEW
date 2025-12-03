from __future__ import annotations

import asyncio
import os
import re
import time
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Tuple
from datetime import datetime, timedelta, timezone

import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession

HISTORY_FILE = "history.txt"
MAX_HISTORY_SIZE = 200

# מילים שאם הן מופיעות בפוסט - מדלגים עליו
BLOCKLIST_KEYWORDS = [
    "black friday", "בלאק פריידי", "blackfriday",
    "11.11", "singles day", "יום הרווקים",
    "נגמר", "אזל", "לא רלוונטי", "פג תוקף",
    "cyber monday", "סייבר מאנדיי"
]

# התעלמות מפוסטים ישנים מ-X שעות
MAX_POST_AGE_HOURS = 24

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        # Fallback for common name mismatches
        alt = name.replace("ALIEXPRESS_", "AFFILIATE_")
        value = os.getenv(alt)
        if not value or not value.strip():
            print(f"Warning: Missing {name}")
            return ""
    return value.strip()

def _optional_str(name: str) -> str | None:
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else None

def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

@dataclass
class Config:
    tg_api_id: int
    tg_api_hash: str
    tg_session: str
    tg_source_channels: List[str]
    tg_target_channel: str
    affiliate_api_endpoint: str | None
    affiliate_app_key: str | None
    affiliate_app_secret: str | None
    affiliate_api_timeout: float
    openai_api_key: str
    openai_model: str
    max_messages_per_channel: int
    max_posts_per_run: int

    @classmethod
    def from_env(cls) -> "Config":
        channels = [c.strip() for c in _require_env("TG_SOURCE_CHANNELS").split(",") if c.strip()]
        
        # Support multiple naming conventions for secrets
        app_key = _optional_str("ALIEXPRESS_APP_KEY") or _optional_str("ALIEXPRESS_API_APP_KEY")
        app_secret = _optional_str("ALIEXPRESS_APP_SECRET") or _optional_str("ALIEXPRESS_API_APP_SECRET")
        
        return cls(
            tg_api_id=int(_require_env("TG_API_ID")),
            tg_api_hash=_require_env("TG_API_HASH"),
            tg_session=_require_env("TG_SESSION"),
            tg_source_channels=channels,
            tg_target_channel=_require_env("TG_TARGET_CHANNEL"),
            affiliate_api_endpoint="https://api-sg.aliexpress.com/sync",
            affiliate_app_key=app_key,
            affiliate_app_secret=app_secret,
            affiliate_api_timeout=_float_env("AFFILIATE_API_TIMEOUT", 15.0),
            openai_api_key=_require_env("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            max_messages_per_channel=_int_env("MAX_MESSAGES_PER_CHANNEL", 30), # Less messages to scan for speed
            max_posts_per_run=_int_env("MAX_POSTS_PER_RUN", 10),
        )

def resolve_short_link(url: str, timeout: float = 8.0) -> str:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.head(url, follow_redirects=True)
            return str(resp.url).replace("m.aliexpress", "www.aliexpress")
    except Exception:
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

    def get_affiliate_info(self, clean_url: str) -> Tuple[str | None, str | None]:
        """Returns (affiliate_link, image_url)"""
        if not self.config.affiliate_app_key or not self.config.affiliate_app_secret:
            print("⚠️ API Creds missing")
            return None, None

        timestamp = str(int(time.time() * 1000))
        # Try Hot Link (2) then General (0)
        for l_type in ["2", "0"]:
            params = {
                "app_key": self.config.affiliate_app_key,
                "timestamp": timestamp,
                "sign_method": "md5",
                "urls": clean_url,
                "promotion_link_type": l_type,
                "tracking_id": "default",
                "format": "json",
                "v": "2.0",
                "method": "aliexpress.affiliate.link.generate"
            }
            params["sign"] = self._sign_params(params)

            try:
                with httpx.Client(timeout=self.config.affiliate_api_timeout) as client:
                    resp = client.post(
                        self.config.affiliate_api_endpoint, 
                        data=params,
                        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
                    )
                    data = resp.json()

                    if "aliexpress_affiliate_link_generate_response" in data:
                        result = data["aliexpress_affiliate_link_generate_response"].get("resp_result", {}).get("result", {})
                        promos = result.get("promotion_links", {}).get("promotion_link", [])
                        if promos:
                            item = promos[0]
                            aff_link = item.get("promotion_link")
                            # The API doesn't always return the image in this call, 
                            # but if we succeed we return the link. 
                            # We will use a different trick for the image if missing.
                            if aff_link and "s.click" in aff_link:
                                print(f"✅ API Generated: {aff_link}")
                                return aff_link, None 
            except Exception as e:
                print(f"API Error ({l_type}): {e}")
                
        return None, None

    def process(self, original_url: str) -> Tuple[str, str | None]:
        # 1. Resolve
        if "s.click" in original_url or "bit.ly" in original_url:
            resolved = resolve_short_link(original_url)
        else:
            resolved = original_url

        # 2. API Call
        aff_link, img_url = self.get_affiliate_info(resolved)
        
        if aff_link:
            return aff_link, img_url # Success via API

        # 3. Fallback
        item_id = extract_item_id(resolved)
        if item_id:
            clean = f"https://www.aliexpress.com/item/{item_id}.html"
            return clean, None
            
        return resolved, None

class CaptionWriter:
    def __init__(self, openai_client: OpenAI, model: str):
        self.client = openai_client
        self.model = model

    def write(self, orig_text: str) -> str:
        prompt = f"""כתוב פוסט קצר בעברית עם אימוג'י: {orig_text[:150]}"""
        try:
            res = self.client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": prompt}], temperature=0.7, max_tokens=200
            )
            if res.choices and res.choices[0].message.content:
                return res.choices[0].message.content.strip()
        except:
            pass
        return "דיל מעולה מאליאקספרס! שווה להציץ 👇"

class DealBot:
    def __init__(self, client: TelegramClient, writer: CaptionWriter, builder: AffiliateLinkBuilder, config: Config):
        self.client = client
        self.writer = writer
        self.builder = builder
        self.config = config
        self.processed_ids: List[str] = []

    def load_history(self):
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r") as f:
                self.processed_ids = [line.strip() for line in f if line.strip()][-MAX_HISTORY_SIZE:]

    def save_history(self):
        with open(HISTORY_FILE, "w") as f:
            f.write("\n".join(self.processed_ids[-MAX_HISTORY_SIZE:]))

    def is_old(self, msg_date: datetime) -> bool:
        if not msg_date: return False
        now = datetime.now(timezone.utc)
        diff = now - msg_date
        return diff > timedelta(hours=MAX_POST_AGE_HOURS)

    def contains_blocked_keywords(self, text: str) -> bool:
        text_lower = text.lower()
        return any(kw in text_lower for kw in BLOCKLIST_KEYWORDS)

    async def run(self):
        self.load_history()
        print("Bot started...")

        for channel in self.config.tg_source_channels:
            print(f"Scanning {channel}...")
            try:
                count = 0
                # Limit scan to recent messages only to avoid super old stuff
                async for msg in self.client.iter_messages(channel, limit=self.config.max_messages_per_channel):
                    if not msg.message: continue

                    # 1. Time Filter
                    if self.is_old(msg.date):
                        # print("Skipping old message")
                        continue

                    # 2. Keyword Filter
                    if self.contains_blocked_keywords(msg.message):
                        print(f"Skipping blocked keyword in: {msg.message[:30]}")
                        continue

                    urls = re.findall(r"https?://[^\s]+", msg.message)
                    ali_urls = [u for u in urls if "aliexpress" in u.lower() or "bit.ly" in u.lower() or "s.click" in u.lower()]
                    if not ali_urls: continue

                    link = ali_urls[0]
                    
                    # Resolve ID specifically for duplication check
                    if "s.click" in link or "bit.ly" in link:
                        resolved_check = resolve_short_link(link)
                    else:
                        resolved_check = link
                        
                    item_id = extract_item_id(resolved_check)
                    if not item_id: continue

                    # 3. Duplication Filter
                    if item_id in self.processed_ids:
                        print(f"⏭️ Duplicate: {item_id}")
                        continue

                    # 4. Get Link & Image
                    final_link, api_image_url = self.builder.process(link)
                    
                    caption = self.writer.write(msg.message)
                    text = f"{caption}\n\n👇 {final_link}"

                    # 5. Send Logic (Clean Image)
                    try:
                        # במקום לשלוח את המדיה המקורית (עם הלוגו), אנחנו:
                        # א. אם קיבלנו תמונה מה-API - נשתמש בה (אופציה עתידית, ה-API לא תמיד מחזיר)
                        # ב. הכי בטוח: שולחים הודעה *בלי תמונה* אבל עם Link Preview פעיל.
                        # טלגרם ימשוך אוטומטית את התמונה הנקייה מאליאקספרס.
                        
                        # אם אתה ממש רוצה לנסות להוריד תמונה נקייה, זה דורש קוד מורכב יותר.
                        # הפתרון הטוב ביותר כרגע כדי להימנע מלוגואים של אחרים:
                        
                        await self.client.send_message(
                            self.config.tg_target_channel, 
                            text,
                            link_preview=True # זה יראה את התמונה המקורית של המוצר
                        )

                        print(f"✅ Posted: {item_id}")
                        self.processed_ids.append(item_id)
                        count += 1
                        if count >= self.config.max_posts_per_run:
                            print("Max posts reached")
                            self.save_history()
                            return

                    except Exception as e:
                        print(f"Send error: {e}")

            except Exception as e:
                print(f"Channel error: {e}")

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
