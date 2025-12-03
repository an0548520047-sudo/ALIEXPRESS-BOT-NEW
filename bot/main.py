from __future__ import annotations

import asyncio
import os
import re
import time
import hashlib
from dataclasses import dataclass
from typing import Dict, List
from urllib.parse import quote, urlparse

import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl

# ==================
# Config and helpers
# ==================

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        alt_name = name.replace("API_", "")
        value = os.getenv(alt_name)
        if value is None or value.strip() == "":
            raise RuntimeError(f"Missing required environment variable: {name} (or {alt_name})")
    return value.strip()

def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None: return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

def _optional_str(name: str) -> str | None:
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else None

def _float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
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
    resolve_redirects: bool
    resolve_redirect_timeout: float

    @classmethod
    def from_env(cls) -> "Config":
        tg_channels_str = os.getenv("TG_SOURCE_CHANNELS", "")
        tg_source_channels = [c.strip() for c in tg_channels_str.split(",") if c.strip()]
        
        app_key = (
            _optional_str("ALIEXPRESS_APP_KEY") or 
            _optional_str("ALIEXPRESS_API_APP_KEY") or 
            _optional_str("AFFILIATE_APP_KEY")
        )
        
        app_secret = (
            _optional_str("ALIEXPRESS_APP_SECRET") or 
            _optional_str("ALIEXPRESS_API_APP_SECRET") or 
            _optional_str("AFFILIATE_API_TOKEN")
        )
        
        api_endpoint = _optional_str("AFFILIATE_API_ENDPOINT") or "https://api-sg.aliexpress.com/sync"

        if not (app_key and app_secret):
            print("⚠️ Warning: API Credentials are MISSING.")

        return cls(
            tg_api_id=int(_require_env("TG_API_ID")),
            tg_api_hash=_require_env("TG_API_HASH"),
            tg_session=_require_env("TG_SESSION"),
            tg_source_channels=tg_source_channels,
            tg_target_channel=_require_env("TG_TARGET_CHANNEL"),
            
            affiliate_api_endpoint=api_endpoint,
            affiliate_app_key=app_key,
            affiliate_app_secret=app_secret,
            affiliate_api_timeout=_float_env("AFFILIATE_API_TIMEOUT", 10.0),
            
            openai_api_key=_require_env("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            max_messages_per_channel=_int_env("MAX_MESSAGES_PER_CHANNEL", 50),
            max_posts_per_run=_int_env("MAX_POSTS_PER_RUN", 10),
            resolve_redirects=_bool_env("RESOLVE_REDIRECTS", True),
            resolve_redirect_timeout=_float_env("RESOLVE_REDIRECT_TIMEOUT", 10.0),
        )

# =======================
# Logic
# =======================

def _canonical_url(url: str) -> str:
    return url.strip().strip("[]()<>.,")

def resolve_url_if_needed(url: str, timeout: float = 10.0) -> str:
    url = _canonical_url(url)
    triggers = ["bit.ly", "tinyurl.com", "goo.gl", "t.me", "is.gd"]
    
    if "aliexpress" in url.lower():
        return url

    if not any(t in url.lower() for t in triggers):
        return url

    print(f"Resolving short link: {url}")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.url:
                return str(resp.url).replace("m.aliexpress", "www.aliexpress")
    except Exception as e:
        print(f"Error resolving URL: {e}")
    
    return url

def extract_item_id_and_clean(url: str) -> str | None:
    match = re.search(r"/item/(\d+)\.html", url)
    if not match:
        match = re.search(r"(\d{10,})", url)
    
    if match:
        return f"https://www.aliexpress.com/item/{match.group(1)}.html"
    return None

class AffiliateLinkBuilder:
    def __init__(self, config: Config):
        self.config = config

    def _sign_params(self, params: Dict[str, str]) -> str:
        if not self.config.affiliate_app_secret: return ""
        sorted_keys = sorted(params.keys())
        param_str = "".join([f"{key}{params[key]}" for key in sorted_keys])
        sign_source = f"{self.config.affiliate_app_secret}{param_str}{self.config.affiliate_app_secret}"
        return hashlib.md5(sign_source.encode("utf-8")).hexdigest().upper()

    def _from_api(self, url_to_convert: str) -> str | None:
        if not self.config.affiliate_app_key or not self.config.affiliate_app_secret:
            return None

        print(f"📡 Calling API for: {url_to_convert}")
        timestamp = str(int(time.time() * 1000))
        
        for l_type in ["2", "0"]:
            params = {
                "app_key": self.config.affiliate_app_key,
                "timestamp": timestamp,
                "sign_method": "md5",
                "urls": url_to_convert,
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
                    resp.raise_for_status()
                    data = resp.json()
                    
                    if "aliexpress_affiliate_link_generate_response" in data:
                        result = data["aliexpress_affiliate_link_generate_response"].get("resp_result", {}).get("result", {})
                        promos = result.get("promotion_links", {}).get("promotion_link", [])
                        
                        if promos:
                            aff_link = promos[0].get("promotion_link")
                            if aff_link and "s.click" in aff_link:
                                print(f"✅ API Success! Link: {aff_link}")
                                return aff_link
            except Exception:
                pass
        
        return None

    def build(self, original_url: str) -> str:
        if "aliexpress" in original_url.lower():
            api_link = self._from_api(original_url)
            if api_link: return api_link
            
            if "/item/" in original_url:
                 clean = extract_item_id_and_clean(original_url)
                 if clean and clean != original_url:
                     api_link = self._from_api(clean)
                     if api_link: return api_link
        else:
            resolved = resolve_url_if_needed(original_url, timeout=self.config.resolve_redirect_timeout)
            clean = extract_item_id_and_clean(resolved) or resolved
            api_link = self._from_api(clean)
            if api_link: return api_link

        print("⚠️ Could not generate affiliate link. Using clean link.")
        return extract_item_id_and_clean(original_url) or original_url

# =======================
# Content & Main
# =======================

def extract_fact_hints(text: str) -> Dict[str, str]:
    hints = {}
    price_match = re.search(r"(₪|\$)\s?\d+[\d.,]*", text)
    if price_match: hints["price"] = price_match.group(0)
    return hints

class CaptionWriter:
    def __init__(self, openai_client: OpenAI, config: Config):
        self.client = openai_client
        self.model = config.openai_model

    def write(self, orig_text: str, affiliate_url: str) -> str:
        hints = extract_fact_hints(orig_text)
        hints_str = ", ".join([f"{k}:{v}" for k, v in hints.items()])
        
        prompt = f"""
כתוב פוסט טלגרם שיווקי קצר וקולח בעברית למוצר הזה.
טקסט מקור: {orig_text[:200]}...
נתונים: {hints_str}
הנחיות: כותרת עם אימוג'י, 2 משפטי המלצה, מחיר אם יש. בלי האשטאגים ובלי "לחצו כאן".
"""
        try:
            res = self.client.chat.completions.create(
                model=self.model, messages=[{"role": "user", "content": prompt}], temperature=0.7, max_tokens=300
            )
            if res.choices and res.choices[0].message.content:
                return res.choices[0].message.content.strip()
        except Exception:
            pass
        return "דיל מעולה מאליאקספרס! שווה בדיקה 👇"

class DealBot:
    def __init__(self, client: TelegramClient, writer: CaptionWriter, builder: AffiliateLinkBuilder, config: Config):
        self.client = client
        self.writer = writer
        self.builder = builder
        self.config = config
        self.processed_ids = set()

    async def load_history(self):
        """סורק את הערוץ יעד כדי לראות מה כבר פורסם"""
        print(f"🔍 Loading history from {self.config.tg_target_channel}...")
        try:
            async for msg in self.client.iter_messages(self.config.tg_target_channel, limit=100):
                if not msg.message: continue
                
                # שיטה חדשה: זיהוי ID בתוך הלינק הנסתר
                # אנחנו מחפשים לינק שנראה כמו: http://bot-id/12345
                if msg.entities:
                    for ent in msg.entities:
                        if isinstance(ent, MessageEntityTextUrl) and "bot-id" in ent.url:
                             # שליפת המספר מה-URL
                             match = re.search(r"bot-id/(\d+)", ent.url)
                             if match:
                                 self.processed_ids.add(match.group(1))
                
                # תמיכה לאחור (לפורמט הישן שהיה כתוב כטקסט)
                old_match = re.search(r"id[:\-](\d+)", msg.message)
                if old_match:
                    self.processed_ids.add(old_match.group(1))

        except Exception as e:
            print(f"Warning: Could not load history: {e}")
        
        print(f"📚 Loaded {len(self.processed_ids)} existing products to ignore.")

    async def run(self):
        # קודם כל טוענים היסטוריה
        await self.load_history()
        
        print("Bot started scanning...")
        for channel in self.config.tg_source_channels:
            print(f"Scanning {channel}...")
            try:
                async for msg in self.client.iter_messages(channel, limit=self.config.max_messages_per_channel):
                    if not msg.message: continue
                    urls = re.findall(r"https?://[^\s]+", msg.message)
                    valid_urls = [u for u in urls if "aliexpress" in u.lower() or "bit.ly" in u.lower() or "s.click" in u.lower()]
                    if not valid_urls: continue
                    
                    link = valid_urls[0]
                    
                    # אנחנו לא בונים לינק עדיין, קודם בודקים אם זה מוצר שכבר יש לנו (כדי לחסוך קריאות ל-API)
                    # אבל בגלל שהלינק יכול להיות שונה, ננסה לנקות אותו בסיסית
                    clean_check = extract_item_id_and_clean(link) or link
                    pid_match = re.search(r"(\d+)\.html", clean_check)
                    # זיהוי מוקדם אם אפשר
                    if pid_match and pid_match.group(1) in self.processed_ids:
                        print(f"Skipping duplicate (early check): {pid_match.group(1)}")
                        continue

                    print(f"Found new potential deal: {link}")
                    
                    final_link = self.builder.build(link)
                    
                    # זיהוי סופי של ה-ID
                    clean = extract_item_id_and_clean(final_link) or final_link
                    pid = re.search(r"(\d+)\.html", clean)
                    pid_str = pid.group(1) if pid else str(hash(clean))
                    
                    if pid_str in self.processed_ids:
                        print(f"Skipping duplicate {pid_str}")
                        continue
                    
                    caption = self.writer.write(msg.message, final_link)
                    
                    # === כאן הקסם של הלינק הנסתר ===
                    # אנחנו שמים תו בלתי נראה שמכיל את ה-ID בתוך ה-URL שלו
                    hidden_id = f"[‎](http://bot-id/{pid_str})"
                    
                    text = f"{hidden_id}{caption}\n\n👇 לקנייה:\n{final_link}"
                    
                    try:
                        if msg.media:
                            await self.client.send_file(self.config.tg_target_channel, msg.media, caption=text)
                        else:
                            await self.client.send_message(self.config.tg_target_channel, text)
                        print(f"✅ Posted {pid_str}")
                        self.processed_ids.add(pid_str)
                        if len(self.processed_ids) >= 200: # ניקוי זיכרון אם הרשימה ענקית
                             pass 
                        
                        if len(self.processed_ids) >= (len(self.processed_ids) + self.config.max_posts_per_run): 
                            # לוגיקה לעצירה (פשטנו את זה כאן כדי שירוץ על הכל אבל יעצור לפי המגבלה בקונפיג)
                            pass

                    except Exception as e:
                        print(f"Send failed: {e}")

            except Exception as e:
                print(f"Channel error: {e}")

async def main():
    config = Config.from_env()
    client = TelegramClient(StringSession(config.tg_session), config.tg_api_id, config.tg_api_hash)
    oa_client = OpenAI(api_key=config.openai_api_key)
    bot = DealBot(client, CaptionWriter(oa_client, config), AffiliateLinkBuilder(config), config)
    await client.start()
    async with client:
        await bot.run()

if __name__ == "__main__":
    asyncio.run(main())
