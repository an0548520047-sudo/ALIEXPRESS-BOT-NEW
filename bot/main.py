# -*- coding: utf-8 -*-
import asyncio
import os
import re
import time
import hashlib
import logging
import random
import json
from urllib.parse import urlparse, urlunparse, parse_qs

import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
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
            logger.critical("❌ Keys missing!")
            return False
        return True

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
            # פתיחת קיצורים והפניות
            if any(x in url for x in ['bit.ly', 't.me', 'tinyurl', 's.click', 'a.aliexpress', 'star.aliexpress']):
                with httpx.Client(follow_redirects=True, timeout=10) as client:
                    resp = client.head(url)
                    url = str(resp.url)
            
            # חילוץ ID מקישורים מיוחדים
            parsed = urlparse(url)
            if "redirectUrl" in parse_qs(parsed.query):
                url = parse_qs(parsed.query)["redirectUrl"][0]

            match = re.search(r'/item/(\d+)\.html', url)
            if match:
                return f"https://www.aliexpress.com/item/{match.group(1)}.html", match.group(1)
            
            return url, None
        except:
            return url, None

    def generate_affiliate_link(self, url, retries=3):
        clean_link, _ = self.clean_url(url)
        
        # אם זה לא לינק למוצר, נחזיר אותו כמו שהוא
        if "item" not in clean_link and "aliexpress.com" in clean_link:
             return clean_link

        logger.info(f"🔌 Processing: {clean_link}")

        params = {
            "app_key": self.key,
            "timestamp": str(int(time.time() * 1000)),
            "format": "json",
            "method": "aliexpress.affiliate.link.generate",
            "sign_method": "md5",
            "v": "2.0",
            "promotion_link_type": "0",
            "source_values": clean_link,
            "tracking_id": "default"  # כאן התיקון!
        }
        params["sign"] = self._sign(params)

        for attempt in range(retries):
            try:
                with httpx.Client(timeout=15) as client:
                    resp = client.post(self.gateway, data=params)
                    data = resp.json()
                    
                    # בדיקת תשובה תקינה
                    if "aliexpress_affiliate_link_generate_response" in data:
                        root = data["aliexpress_affiliate_link_generate_response"]
                        if "resp_result" in root and "result" in root["resp_result"]:
                            promos = root["resp_result"]["result"]["promotion_links"]["promotion_link"]
                            return promos[0]["promotion_link"]
                    
                    # בדיקת שגיאות ידועות
                    if "error_response" in data:
                        msg = data["error_response"].get("msg", "")
                        if "tracking" in msg.lower():
                            logger.error("❌ Tracking ID Error! Using fallback.")
                            break
                    
                    time.sleep(1)
            except:
                time.sleep(1)
        
        return clean_link

class ContentGenerator:
    def __init__(self):
        self.client = OpenAI(api_key=Config.OPENAI_KEY) if Config.OPENAI_KEY else None

    def create_caption(self, text, price=""):
        if not self.client: return "מציאה חדשה! 👇"
        
        # ניקוי הטקסט
        clean = re.sub(r'https?://\S+|@\S+|t\.me\S+', '', text)
        lines = [l for l in clean.split('\n') if not any(x in l for x in ["הצטרפו", "ערוץ"])]
        clean = "\n".join(lines).strip()

        try:
            prompt = f"כתוב פוסט טלגרם קצר (2 שורות) שיווקי וכיפי בעברית על המוצר הזה: '{clean[:300]}'. מחיר: {price}. בלי האשטאגים."
            resp = self.client.chat.completions.create(
                model=Config.OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200
            )
            return resp.choices[0].message.content.strip()
        except:
            return "דיל שווה בטירוף! 🔥"

class AffiliateBot:
    def __init__(self):
        self.client = TelegramClient(StringSession(Config.SESSION), Config.API_ID, Config.API_HASH)
        self.ali = AliExpressHandler()
        self.writer = ContentGenerator()
        self.history = set()
        self.start_time = time.time()

    async def run(self):
        if not Config.validate(): return
        await self.client.start()
        
        # טעינת היסטוריה קצרה
        async for msg in self.client.iter_messages(Config.TARGET_CHANNEL, limit=100):
            if msg.text:
                links = re.findall(r'/item/(\d+)\.html', msg.text)
                for pid in links: self.history.add(pid)

        logger.info("🚀 Bot started.")
        processed = 0

        for source in Config.SOURCE_CHANNELS:
            try:
                async for msg in self.client.iter_messages(source, limit=30):
                    if processed >= Config.MAX_POSTS_PER_RUN: return
                    if (time.time() - self.start_time) > (Config.MAX_RUNTIME_MINUTES * 60): return
                    if not msg.text: continue

                    # מציאת לינק
                    urls = re.findall(r'(https?://[^\s]+)', msg.text)
                    valid = [u for u in urls if "aliexpress" in u or "s.click" in u]
                    if not valid: continue

                    # בדיקת כפילות
                    orig_link = valid[0]
                    _, pid = self.ali.clean_url(orig_link)
                    if pid in self.history: continue

                    # המרה ללינק שותף
                    logger.info(f"💡 Found item: {pid}")
                    aff_link = self.ali.generate_affiliate_link(orig_link)
                    
                    # יצירת הודעה
                    price = re.search(r"(₪|\$)\s?\d+", msg.text)
                    price = price.group(0) if price else ""
                    text = self.writer.create_caption(msg.text, price)
                    
                    final_msg = f"{text}\n\n👇 לרכישה:\n{aff_link}"
                    
                    # שליחה
                    if msg.media:
                        await self.client.send_file(Config.TARGET_CHANNEL, msg.media, caption=final_msg)
                    else:
                        await self.client.send_message(Config.TARGET_CHANNEL, final_msg, link_preview=True)
                    
                    self.history.add(pid)
                    processed += 1
                    time.sleep(random.randint(5, 10))

            except Exception as e:
                logger.error(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(AffiliateBot().run())
