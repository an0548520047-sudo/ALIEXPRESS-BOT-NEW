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
# 2. מנהל עליאקספרס (מנגנון המרה משופר)
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
        """מנקה זבל מהלינק ומחלץ ID"""
        try:
            # פתיחת קיצורים נפוצים
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
        """מנסה לייצר לינק שותף, עם לוגיקה חכמה לטיפול בשגיאות"""
        clean_link, _ = self.clean_url(url)
        logger.info(f"🔌 Converting link: {clean_link}")

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
                    
                    # 1. ניסיון לחלץ מהמבנה הסטנדרטי
                    if "aliexpress_affiliate_link_generate_response" in data:
                        root = data["aliexpress_affiliate_link_generate_response"]
                        if "resp_result" in root and "result" in root["resp_result"]:
                            promos = root["resp_result"]["result"]["promotion_links"]["promotion_link"]
                            return promos[0]["promotion_link"]
                    
                    # 2. ניסיון לחלץ ממבנה שטוח (לפעמים קורה)
                    if "result" in data and "promotion_links" in data["result"]:
                        promos = data["result"]["promotion_links"]["promotion_link"]
                        return promos[0]["promotion_link"]

                    # 3. טיפול בשגיאות
                    if "error_response" in data:
                        msg = data["error_response"].get("msg", "Unknown")
                        # אם המוצר לא נמצא או הלינק לא חוקי, אין טעם לנסות שוב
                        if "Invalid" in msg or "found" in msg:
                            logger.warning(f"⚠️ API Rejected: {msg}")
                            break
                        logger.warning(f"⚠️ API Error: {msg}")
                    else:
                        # אם המבנה לא מוכר, נדפיס אותו כדי שנוכל לתקן
                        logger.info(f"🔍 Unknown JSON: {json.dumps(data)}")

                    time.sleep(1)

            except Exception as e:
                logger.warning(f"API Attempt {attempt+1} network error: {e}")
                time.sleep(2)
        
        logger.error("⚠️ Failed to generate link. Using fallback (Clean URL).")
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
        self.client = TelegramClient(StringSession(Config.
