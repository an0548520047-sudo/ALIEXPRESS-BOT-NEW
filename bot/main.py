# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import hashlib
import asyncio
import re
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession
from openai import OpenAI

# ==========================================
# הגדרות לוגים
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# קונפיגורציה
# ==========================================
class Config:
    API_ID = int(os.environ.get("TG_API_ID", 0))
    API_HASH = os.environ.get("TG_API_HASH")
    SESSION_STR = os.environ.get("TG_SESSION")
    SOURCE_CHANNELS = [x.strip() for x in os.environ.get("TG_SOURCE_CHANNELS", "").split(",") if x.strip()]
    TARGET_CHANNEL = os.environ.get("TG_TARGET_CHANNEL")
    APP_KEY = os.environ.get("ALIEXPRESS_APP_KEY")
    APP_SECRET = os.environ.get("ALIEXPRESS_APP_SECRET")
    OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
    MAX_MESSAGES = 40
    HISTORY_FILE = "history.txt"

# בדיקת מפתחות קריטית
if not Config.APP_KEY or not Config.APP_SECRET:
    logger.critical("❌ Missing Keys! Check GitHub Secrets.")
    sys.exit(1)

# ==========================================
# לקוח עליאקספרס
# ==========================================
class AliExpressClient:
    def __init__(self, app_key, app_secret):
        self.app_key = app_key
        self.app_secret = app_secret
        self.gateway = "https://api-sg.aliexpress.com/router/rest"

    def _sign(self, params):
        keys = sorted(params.keys())
        sign_str = self.app_secret
        for key in keys:
            sign_str += f"{key}{params[key]}"
        sign_str += self.app_secret
        return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

    def execute(self, method, api_params):
        sys_params = {
            "app_key": self.app_key,
            "timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
            "format": "json",
            "method": method,
            "sign_method": "md5",
            "v": "2.0"
        }
        all_params = {**sys_params, **api_params}
        all_params["sign"] = self._sign(all_params)

        try:
            with httpx.Client(timeout=15.0) as client:
                resp = client.post(
                    self.gateway, 
                    data=all_params, 
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
                )
                return resp.json()
        except Exception as e:
            logger.error(f"Network Error: {e}")
            return None

    def get_details(self, product_id):
        # מבקשים USD כדי להגדיל סיכוי לתשובה חיובית
        params = {"product_ids": product_id, "target_currency": "USD", "target_language": "EN"}
        res = self.execute("aliexpress.affiliate.product.detail.get", params)
        if not res: return None
        try:
            return res["aliexpress_affiliate_product_detail_get_response"]["resp_result"]["result"]["products"]["product"][0]
        except:
            return None

    def generate_link(self, clean_url):
        params = {
            "promotion_link_type": "0",
            "source_values": clean_url, # חשוב: שולחים URL נקי!
            "tracking_id": "telegram_bot"
        }
        res = self.execute("aliexpress.affiliate.link.generate", params)
        if not res: return None
        try:
            return res["aliexpress_affiliate_link_generate_response"]["resp_result"]["result"]["promotion_links"]["promotion_link"][0]["promotion_link"]
        except:
            # אם נכשלנו, נחזיר None כדי שהקוד הראשי ידע להשתמש בלינק המקורי
            if "error_response" in res:
                logger.warning(f"⚠️ API Error: {res['error_response'].get('msg')}")
            return None

# ==========================================
# כותב תוכן AI
# ==========================================
class AIWriter:
    def __init__(self):
        self.client = OpenAI(api_key=Config.OPENAI_KEY) if Config.OPENAI_KEY else None

    def generate(self, title, price):
        if not self.client: return "מציאה חדשה! 👇"
        try:
            prompt = f"כתוב פוסט טלגרם קצר, שיווקי וקליט בעברית. המוצר: {title}. המחיר: {price}. בלי האשטאגים. השתמש באימוג'י."
            resp = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}]
            )
            return resp.choices[0].message.content.strip()
        except:
            return "דיל מטורף מאליאקספרס! 🔥"

# ==========================================
# פונקציות עזר קריטיות (Cleaning)
# ==========================================
def clean_url(url):
    """מנקה פרמטרים של שותפים אחרים מהלינק"""
    try:
        parsed = urlparse(url)
        # בנייה מחדש של ה-URL ללא Query Parameters שמפריעים ל-API
        cleaned = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
        return cleaned
    except:
        return url

def resolve_and_extract(url):
    """עוקב אחרי הפניות ומחלץ ID נקי"""
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/110.0.0.0 Safari/537.36"}
        cookies = {"aep_usuc_f": "region=IL&site=glo&b_locale=en_US&c_tp=USD"} # כפיית אתר גלובלי
        
        with httpx.Client(follow_redirects=True, timeout=10, headers=headers, cookies=cookies) as client:
            resp = client.head(url)
            final_url = str(resp.url).split('?')[0]
            
            # תיקון הפניה לארה"ב
            if "aliexpress.us" in final_url:
                final_url = final_url.replace("aliexpress.us", "aliexpress.com")

            # חילוץ ID
            match = re.search(r'/item/(\d+)\.html', final_url)
            if match:
                return match.group(1), final_url
            
            return None, final_url
    except:
        return None, url

# ==========================================
# הבוט הראשי
# ==========================================
async def main():
    logger.info("🚀 Bot Starting (Smart Fallback Mode)...")
    
    # 1. טעינת היסטוריה
    processed_ids = set()
    if os.path.exists(Config.HISTORY_FILE):
        with open(Config.HISTORY_FILE, "r") as f:
            processed_ids = set(f.read().splitlines())
    logger.info(f"📚 History: {len(processed_ids)} items.")

    # 2. התחברות לטלגרם
    try:
        client = TelegramClient(StringSession(Config.SESSION_STR), Config.API_ID, Config.API_HASH)
        await client.start()
    except Exception as e:
        logger.critical(f"❌ Telegram Login Failed: {e}")
        sys.exit(1)

    ali = AliExpressClient(Config.APP_KEY, Config.APP_SECRET)
    ai = AIWriter()
    
    new_posts = 0

    # 3. סריקה
    for channel in Config.SOURCE_CHANNELS:
        logger.info(f"👀 Scanning: {channel}")
        try:
            messages = await client.get_messages(channel, limit=Config.MAX_MESSAGES)
            for msg in messages:
                if not msg.text: continue
                links = re.findall(r'(https?://[^\s]+)', msg.text)
                
                for link in links:
                    if "aliexpress" not in link and "s.click" not in link: continue
                    
                    # פענוח וחילוץ ID
                    pid, real_url = resolve_and_extract(link)
                    
                    if not pid: continue
                    if pid in processed_ids: continue
                    
                    logger.info(f"⚡ Processing ID: {pid}")

                    # 4. ניסיון ליצור לינק אפיליאייט
                    cleaned_url = clean_url(real_url) # ניקוי קריטי!
                    final_link = ali.generate_link(cleaned_url)
                    
                    if not final_link:
                        logger.warning(f"⚠️ API failed to convert link. Using fallback.")
                        final_link = cleaned_url # משתמשים בלינק הנקי הרגיל
                    
                    # 5. ניסיון למשוך פרטים (תמונה/מחיר)
                    details = ali.get_details(pid)
                    
                    if details:
                        title = details.get('product_title', 'מוצר מומלץ')
                        price = f"{details.get('target_sale_price', '')} USD"
                        img = details.get("product_main_image_url")
                    else:
                        # אם ה-API נכשל בפרטים, ננסה לקחת תמונה מההודעה המקורית
                        title = "מציאה מאליאקספרס"
                        price = "מחיר מעולה"
                        img = msg.media if msg.media else None
                    
                    # 6. יצירת תוכן ושליחה
                    text = ai.generate(title, price)
                    caption = f"{text}\n\n👇 לרכישה:\n{final_link}"

                    try:
                        if img:
                            await client.send_file(Config.TARGET_CHANNEL, img, caption=caption)
                        else:
                            await client.send_message(Config.TARGET_CHANNEL, caption, link_preview=True)
                        
                        logger.info(f"✅ POSTED: {pid}")
                        processed_ids.add(pid)
                        new_posts += 1
                        
                        # שמירה מיידית
                        with open(Config.HISTORY_FILE, "a") as f: f.write(f"{pid}\n")
                        
                        time.sleep(5) # המתנה כדי לא להציף
                        
                    except Exception as e:
                        logger.error(f"❌ Telegram Send Error: {e}")

        except Exception as e:
            logger.error(f"Error in channel loop: {e}")

    logger.info(f"🏁 Run finished. Posted {new_posts} new items.")

if __name__ == '__main__':
    asyncio.run(main())
