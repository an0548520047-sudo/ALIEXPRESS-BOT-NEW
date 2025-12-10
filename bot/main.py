# -*- coding: utf-8 -*-
import asyncio
import os
import re
import time
import hashlib
import logging
import random
import json
from urllib.parse import urlparse, urlunparse

import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl

# ==========================================
# 1. הגדרות וקונפיגורציה
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class Config:
    API_ID = int(os.environ.get("TG_API_ID", 0))
    API_HASH = os.environ.get("TG_API_HASH")
    SESSION = os.environ.get("TG_SESSION")
    SOURCE_CHANNELS = [x.strip() for x in os.environ.get("TG_SOURCE_CHANNELS", "").split(",") if x.strip()]
    TARGET_CHANNEL = os.environ.get("TG_TARGET_CHANNEL")
    APP_KEY = os.environ.get("ALIEXPRESS_APP_KEY")
    APP_SECRET = os.environ.get("ALIEXPRESS_APP_SECRET")
    API_ENDPOINT = "https://api-sg.aliexpress.com/sync"
    OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
    OPENAI_MODEL = "gpt-4o-mini"
    MAX_MESSAGES = 50       
    MAX_POSTS_PER_RUN = 8
    MIN_DELAY = 6
    MAX_RUNTIME_MINUTES = 25 

    @staticmethod
    def validate():
        if not Config.APP_KEY or not Config.APP_SECRET:
            logger.critical("❌ Critical: AliExpress Keys missing!")
            return False
        if not Config.SESSION:
            logger.critical("❌ Critical: Telegram Session missing!")
            return False
        return True

# ==========================================
# 2. מנהל עליאקספרס (תיקון JSON)
# ==========================================
class AliExpressHandler:
    def __init__(self):
        self.key = Config.APP_KEY
        self.secret = Config.APP_SECRET
        self.gateway = Config.API_ENDPOINT

    def _sign(self, params):
        keys = sorted(params.keys())
        sign_str = self.secret + "".join(f"{k}{params[k]}" for k in keys) + self.secret
        return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

    def clean_url(self, url):
        try:
            if any(x in url for x in ['bit.ly', 't.me', 'tinyurl', 's.click', 'a.aliexpress']):
                with httpx.Client(follow_redirects=True, timeout=10) as client:
                    resp = client.head(url)
                    url = str(resp.url)

            match = re.search(r'/item/(\d+)\.html', url)
            if match:
                clean_id = match.group(1)
                return f"https://www.aliexpress.com/item/{clean_id}.html", clean_id
            
            parsed = urlparse(url)
            clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
            return clean, None
        except Exception:
            return url, None

    def generate_affiliate_link(self, url, retries=3):
        clean_link, _ = self.clean_url(url)
        
        params = {
            "app_key": self.key,
            "timestamp": str(int(time.time() * 1000)),
            "format": "json",
            "method": "aliexpress.affiliate.link.generate",
            "sign_method": "md5",
            "v": "2.0",
            "promotion_link_type": "0",
            "source_values": clean_link,
            "tracking_id": "telegram_bot"
        }
        params["sign"] = self._sign(params)

        for attempt in range(retries):
            try:
                with httpx.Client(timeout=15) as client:
                    resp = client.post(self.gateway, data=params)
                    data = resp.json()
                    
                    # === התיקון הקריטי כאן ===
                    # בדיקת מבנה התשובה בצורה בטוחה יותר
                    if "aliexpress_affiliate_link_generate_response" in data:
                        resp_root = data["aliexpress_affiliate_link_generate_response"]
                        
                        # לפעמים התשובה היא ישירה ב-resp_result ולפעמים בתוך result
                        if "resp_result" in resp_root:
                            resp_result = resp_root["resp_result"]
                            
                            # ניסיון לחלץ מ-result (המבנה הנפוץ)
                            if "result" in resp_result:
                                promos = resp_result["result"]["promotion_links"]["promotion_link"]
                                return promos[0]["promotion_link"]
                            
                            # ניסיון לחלץ ישירות (אם המבנה שונה)
                            elif "promotion_links" in resp_result:
                                promos = resp_result["promotion_links"]["promotion_link"]
                                return promos[0]["promotion_link"]
                    
                    # אם הגענו לפה, המבנה לא תואם או שיש שגיאה
                    if "error_response" in data:
                        logger.warning(f"API Error: {data['error_response'].get('msg')}")
                    else:
                        # הדפסת ה-JSON המלא ללוג כדי שנבין מה קורה
                        logger.warning(f"Unexpected JSON Structure: {json.dumps(data)}")

                    time.sleep(1)
                    
            except Exception as e:
                logger.warning(f"API Attempt {attempt+1} error: {e}")
                time.sleep(2)
        
        logger.error("⚠️ Failed to generate affiliate link. Using fallback.")
        return clean_link

# ==========================================
# 3. יוצר תוכן אנושי
# ==========================================
class ContentGenerator:
    def __init__(self):
        self.client = OpenAI(api_key=Config.OPENAI_KEY) if Config.OPENAI_KEY else None

    def _sanitize_input(self, text):
        text = re.sub(r'https?://\S+', '', text)
        bad_words = ["הצטרפו", "ערוץ", "join", "channel", "t.me", "@", "👇", "בלינק"]
        lines = [line for line in text.split('\n') if not any(bw in line.lower() for bw in bad_words)]
        return "\n".join(lines).strip()

    def create_caption(self, original_text, price_hint=""):
        if not self.client:
            return "מציאה חדשה! 👇"

        clean_text = self._sanitize_input(original_text)
        
        if len(clean_text) < 15:
            prompt = f"כתוב המלצה קצרה ומתלהבת בעברית על מוצר מאליאקספרס. מחיר: {price_hint}."
        else:
            prompt = f"""
            אתה מנהל קהילת קניות בטלגרם.
            כתוב פוסט המלצה קצר (2-3 משפטים) על בסיס הטקסט:
            "{clean_text[:500]}"
            מחיר: {price_hint}
            הנחיות: טון אישי ("מצאתי לכם"), בלי מילים שיווקיות זולות, בלי האשטאגים.
            """

        try:
            resp = self.client.chat.completions.create(
                model=Config.OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=300
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return "מצאתי דיל מעניין! שווה בדיקה 👇"

# ==========================================
# 4. הבוט הראשי
# ==========================================
class AffiliateBot:
    def __init__(self):
        self.client = TelegramClient(StringSession(Config.SESSION), Config.API_ID, Config.API_HASH)
        self.ali = AliExpressHandler()
        self.writer = ContentGenerator()
        self.history = set()
        self.start_time = time.time()

    async def load_history(self):
        logger.info("📚 Syncing history...")
        try:
            async for msg in self.client.iter_messages(Config.TARGET_CHANNEL, limit=150):
                if msg.entities:
                    for ent in msg.entities:
                        if isinstance(ent, MessageEntityTextUrl) and "bot-id" in ent.url:
                            match = re.search(r"bot-id/(\d+)", ent.url)
                            if match: self.history.add(match.group(1))
                if msg.text:
                    links = re.findall(r'/item/(\d+)\.html', msg.text)
                    for pid in links: self.history.add(pid)
        except Exception as e:
            logger.warning(f"History warning: {e}")
        logger.info(f"✅ Loaded {len(self.history)} past items.")

    def should_stop(self):
        elapsed = (time.time() - self.start_time) / 60
        if elapsed >= Config.MAX_RUNTIME_MINUTES:
            logger.info("⏱️ Time limit reached. Stopping safely.")
            return True
        return False

    async def run(self):
        if not Config.validate(): return
        
        await self.client.start()
        await self.load_history()
        
        processed_count = 0
        logger.info("🚀 Bot is running...")

        for source in Config.SOURCE_CHANNELS:
            logger.info(f"👀 Checking: {source}")
            try:
                async for msg in self.client.iter_messages(source, limit=Config.MAX_MESSAGES):
                    if processed_count >= Config.MAX_POSTS_PER_RUN:
                        logger.info("🛑 Daily limit reached. Done.")
                        return
                    if self.should_stop(): return
                    if not msg.text: continue
                    
                    urls = re.findall(r'(https?://[^\s]+)', msg.text)
                    valid_urls = [u for u in urls if any(x in u for x in ["aliexpress", "s.click", "bit.ly"])]
                    if not valid_urls: continue
                    
                    original_link = valid_urls[0]
                    _, pid = self.ali.clean_url(original_link)
                    if pid and pid in self.history: continue 

                    logger.info(f"💡 Found candidate: {pid}")
                    
                    final_link = self.ali.generate_affiliate_link(original_link)
                    
                    _, final_pid = self.ali.clean_url(final_link)
                    current_id = final_pid if final_pid else str(hash(final_link))
                    
                    if current_id in self.history:
                        logger.info(f"⏩ Skipping duplicate: {current_id}")
                        continue

                    price_match = re.search(r"(₪|\$)\s?\d+(\.\d+)?", msg.text)
                    price = price_match.group(0) if price_match else ""
                    caption = self.writer.create_caption(msg.text, price)

                    hidden_id = f"[‎](http://bot-id/{current_id})"
                    final_msg = f"{hidden_id}{caption}\n\n👇 לרכישה:\n{final_link}"

                    try:
                        if msg.media:
                            await self.client.send_file(Config.TARGET_CHANNEL, msg.media, caption=final_msg)
                        else:
                            await self.client.send_message(Config.TARGET_CHANNEL, final_msg, link_preview=True)
                        
                        logger.info(f"✅ Posted Successfully: {current_id}")
                        self.history.add(current_id)
                        processed_count += 1
                        time.sleep(random.randint(Config.MIN_DELAY, Config.MIN_DELAY + 5))
                    except Exception as e:
                        logger.error(f"❌ Send failed: {e}")

            except Exception as e:
                logger.error(f"Error reading channel: {e}")

        logger.info(f"🏁 Session finished. New posts: {processed_count}")

if __name__ == "__main__":
    bot = AffiliateBot()
    asyncio.run(bot.run())
