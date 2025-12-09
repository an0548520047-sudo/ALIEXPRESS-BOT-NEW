# -*- coding: utf-8 -*-
import asyncio
import os
import re
import time
import hashlib
import logging
import random
from datetime import datetime, time as dt_time
from urllib.parse import urlparse, urlunparse
import pytz

import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl

# ==========================================
# 1. הגדרות וקונפיגורציה
# ==========================================
logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

class Config:
    # טלגרם
    API_ID = int(os.environ.get("TG_API_ID", 0))
    API_HASH = os.environ.get("TG_API_HASH")
    SESSION = os.environ.get("TG_SESSION")
    SOURCE_CHANNELS = [x.strip() for x in os.environ.get("TG_SOURCE_CHANNELS", "").split(",") if x.strip()]
    TARGET_CHANNEL = os.environ.get("TG_TARGET_CHANNEL")

    # עליאקספרס
    APP_KEY = os.environ.get("ALIEXPRESS_APP_KEY")
    APP_SECRET = os.environ.get("ALIEXPRESS_APP_SECRET")
    API_ENDPOINT = "https://api-sg.aliexpress.com/sync"

    # OpenAI
    OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
    OPENAI_MODEL = "gpt-4o-mini"

    # הגדרות ריצה
    MAX_MESSAGES = 50       
    MAX_POSTS_PER_RUN = 8   # הורדתי קצת כדי לשמור על איכות
    MIN_DELAY = 5           # מינימום שניות בין פוסטים
    
    # שעות פעילות (שעון ישראל) - אופציונלי, כרגע 24/7 אבל מוכן לשימוש
    QUIET_HOURS_START = 2   # 02:00 בלילה
    QUIET_HOURS_END = 6     # 06:00 בבוקר

    @staticmethod
    def validate():
        if not Config.APP_KEY or not Config.APP_SECRET:
            logger.critical("❌ Missing AliExpress Keys!")
            return False
        if not Config.SESSION:
            logger.critical("❌ Missing Telegram Session!")
            return False
        return True

# ==========================================
# 2. מחלקת לינקים ועליאקספרס
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
        """מנקה פרמטרים ומחלץ ID"""
        try:
            # פתיחת קיצורים חכמה - רק אם צריך
            if any(x in url for x in ['bit.ly', 't.me', 'tinyurl', 's.click']):
                with httpx.Client(follow_redirects=True, timeout=10) as client:
                    resp = client.head(url)
                    url = str(resp.url)

            # חילוץ ID - השיטה הכי אמינה
            match = re.search(r'/item/(\d+)\.html', url)
            if match:
                return f"https://www.aliexpress.com/item/{match.group(1)}.html", match.group(1)
            
            parsed = urlparse(url)
            clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
            return clean, None
        except Exception:
            return url, None

    def generate_affiliate_link(self, url):
        """יוצר לינק אפיליאייט דרך ה-API עם Fallback"""
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

        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(self.gateway, data=params)
                data = resp.json()
                
                if "aliexpress_affiliate_link_generate_response" in data:
                    result = data["aliexpress_affiliate_link_generate_response"]["resp_result"]["result"]
                    return result["promotion_links"]["promotion_link"][0]["promotion_link"]
                    
        except Exception as e:
            logger.error(f"API Error: {e}")
        
        # אם נכשל, מחזירים את הלינק הנקי כדי לא לאבד את הפוסט
        return clean_link

# ==========================================
# 3. מחלקת תוכן (AI) - משופרת
# ==========================================
class ContentGenerator:
    def __init__(self):
        self.client = OpenAI(api_key=Config.OPENAI_KEY) if Config.OPENAI_KEY else None

    def _sanitize_input(self, text):
        """מנקה זבל מהודעות מקור לפני שליחה ל-AI"""
        # מחיקת לינקים מהטקסט (הם סתם מבלבלים את ה-GPT)
        text = re.sub(r'https?://\S+', '', text)
        # מחיקת שורות "הצטרפו לערוץ" נפוצות
        bad_words = ["הצטרפו", "ערוץ", "join", "channel", "t.me", "@"]
        lines = [line for line in text.split('\n') if not any(bw in line.lower() for bw in bad_words)]
        return "\n".join(lines).strip()

    def create_caption(self, original_text, price_hint=""):
        if not self.client:
            return "מציאה חדשה מאליאקספרס! 👇"

        clean_text = self._sanitize_input(original_text)
        
        # אם נשאר מעט מדי טקסט, נבקש גנרי
        if len(clean_text) < 10:
            prompt = f"כתוב משפט שיווקי קצר על 'גאדג'ט מאליאקספרס'. מחיר: {price_hint}."
        else:
            prompt = f"""
            תפקידך: מנהל ערוץ טלגרם מומחה.
            משימה: שכתב את הטקסט הבא לפוסט מכירתי קצר (מקסימום 3 שורות).
            טקסט מקור: {clean_text[:400]}
            מחיר: {price_hint}
            דרישות: טון מתלהב אבל אמין, השתמש ב-2 אימוג'ים רלוונטיים. בלי האשטאגים.
            """

        try:
            resp = self.client.chat.completions.create(
                model=Config.OPENAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=250
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return "דיל שווה בטירוף! אל תפספסו 🔥"

# ==========================================
# 4. הבוט הראשי
# ==========================================
class AffiliateBot:
    def __init__(self):
        self.client = TelegramClient(StringSession(Config.SESSION), Config.API_ID, Config.API_HASH)
        self.ali = AliExpressHandler()
        self.writer = ContentGenerator()
        self.history = set()

    async def load_history(self):
        """טעינת היסטוריה חכמה"""
        logger.info("📚 Loading history...")
        try:
            async for msg in self.client.iter_messages(Config.TARGET_CHANNEL, limit=200):
                # זיהוי ID נסתר
                if msg.entities:
                    for ent in msg.entities:
                        if isinstance(ent, MessageEntityTextUrl) and "bot-id" in ent.url:
                            match = re.search(r"bot-id/(\d+)", ent.url)
                            if match: self.history.add(match.group(1))
                
                # תמיכה לאחור
                if msg.text:
                    links = re.findall(r'/item/(\d+)\.html', msg.text)
                    for pid in links: self.history.add(pid)
                    
        except Exception as e:
            logger.warning(f"History load warning: {e}")
        
        logger.info(f"✅ History loaded: {len(self.history)} items.")

    async def run(self):
        if not Config.validate(): return
        
        await self.client.start()
        await self.load_history()
        
        processed_count = 0
        logger.info("🚀 Bot started scanning...")

        for source in Config.SOURCE_CHANNELS:
            logger.info(f"👀 Scanning: {source}")
            try:
                async for msg in self.client.iter_messages(source, limit=Config.MAX_MESSAGES):
                    # הגנה מפני הצפה
                    if processed_count >= Config.MAX_POSTS_PER_RUN:
                        logger.info("🛑 Reached limits. Bye.")
                        return

                    if not msg.text: continue
                    
                    # זיהוי לינקים
                    urls = re.findall(r'(https?://[^\s]+)', msg.text)
                    valid_urls = [u for u in urls if "aliexpress" in u or "s.click" in u or "bit.ly" in u]
                    
                    if not valid_urls: continue
                    
                    original_link = valid_urls[0]
                    _, pid = self.ali.clean_url(original_link)
                    
                    # בדיקת כפילות ראשונית
                    if pid and pid in self.history: continue 

                    logger.info(f"🔎 Found deal: {pid or 'Unknown'}")
                    
                    # המרה ללינק אפיליאייט
                    final_link = self.ali.generate_affiliate_link(original_link)
                    
                    # בדיקה סופית של ה-ID אחרי המרה
                    _, final_pid = self.ali.clean_url(final_link)
                    current_id = final_pid if final_pid else str(hash(final_link))
                    
                    if current_id in self.history:
                        logger.info(f"⏩ Duplicate after resolve: {current_id}")
                        continue

                    # יצירת טקסט
                    price_match = re.search(r"(₪|\$)\s?\d+(\.\d+)?", msg.text)
                    price = price_match.group(0) if price_match else ""
                    caption = self.writer.create_caption(msg.text, price)

                    # ID נסתר למעקב
                    hidden_id = f"[‎](http://bot-id/{current_id})"
                    final_msg = f"{hidden_id}{caption}\n\n👇 לרכישה:\n{final_link}"

                    try:
                        # תמיכה חכמה במדיה (כולל וידאו!)
                        if msg.media:
                            await self.client.send_file(Config.TARGET_CHANNEL, msg.media, caption=final_msg)
                        else:
                            await self.client.send_message(Config.TARGET_CHANNEL, final_msg, link_preview=True)
                        
                        logger.info(f"✅ Posted: {current_id}")
                        self.history.add(current_id)
                        processed_count += 1
                        
                        # המתנה רנדומלית (נראה אנושי יותר)
                        wait_time = random.randint(Config.MIN_DELAY, Config.MIN_DELAY + 5)
                        time.sleep(wait_time)
                        
                    except Exception as e:
                        logger.error(f"❌ Send Error: {e}")

            except Exception as e:
                logger.error(f"Channel Error: {e}")

        logger.info(f"🏁 Done. Total: {processed_count}")

if __name__ == "__main__":
    bot = AffiliateBot()
    asyncio.run(bot.run())
