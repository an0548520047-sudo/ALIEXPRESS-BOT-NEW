# -*- coding: utf-8 -*-
import os
import sys
import time
import json
import hashlib
import hmac
import asyncio
import re
import logging
from datetime import datetime
from urllib.parse import quote_plus

# צד שלישי
import httpx
from telethon import TelegramClient, events, sync
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl
from openai import OpenAI

# ==========================================
# הגדרות לוגים (כדי שנבין מה קורה)
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# הגדרות קונפיגורציה (טעינה מה-Secrets)
# ==========================================
class Config:
    # Telegram
    API_ID = int(os.environ.get("TG_API_ID", 0))
    API_HASH = os.environ.get("TG_API_HASH")
    SESSION_STR = os.environ.get("TG_SESSION")
    SOURCE_CHANNELS = [x.strip() for x in os.environ.get("TG_SOURCE_CHANNELS", "").split(",") if x.strip()]
    TARGET_CHANNEL = os.environ.get("TG_TARGET_CHANNEL")

    # AliExpress
    APP_KEY = os.environ.get("ALIEXPRESS_APP_KEY")
    APP_SECRET = os.environ.get("ALIEXPRESS_APP_SECRET")
    
    # OpenAI
    OPENAI_KEY = os.environ.get("OPENAI_API_KEY")
    
    # הגדרות סינון
    MIN_PRICE = 0.1
    MAX_MESSAGES = 30  # כמה הודעות אחרונות לסרוק

# בדיקת תקינות משתנים
if not Config.APP_KEY or not Config.APP_SECRET:
    logger.critical("❌ Missing ALIEXPRESS_APP_KEY or ALIEXPRESS_APP_SECRET")
    sys.exit(1)

# ==========================================
# מחלקת עליאקספרס (בנייה מחדש)
# ==========================================
class AliExpressClient:
    def __init__(self, app_key, app_secret):
        self.app_key = app_key
        self.app_secret = app_secret
        self.gateway = "https://api-sg.aliexpress.com/sync"  # שרת גלובלי ראשי

    def _generate_sign(self, params):
        """
        יצירת חתימה לפי התקן המדויק של עליאקספרס:
        1. מיון מפתחות אלפביתי
        2. שרשור Secret + Key + Value + ... + Secret
        3. המרה ל-MD5 Uppercase
        """
        # מיון הפרמטרים
        keys = sorted(params.keys())
        
        # יצירת המחרוזת לחתימה
        sign_str = self.app_secret
        for key in keys:
            val = str(params[key])
            sign_str += f"{key}{val}"
        sign_str += self.app_secret

        # הצפנה
        m = hashlib.md5()
        m.update(sign_str.encode("utf-8"))
        return m.hexdigest().upper()

    def execute(self, method, api_params):
        """שליחת בקשה לשרת"""
        # פרמטרים מערכתיים
        sys_params = {
            "app_key": self.app_key,
            "timestamp": datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), # UTC נקי
            "format": "json",
            "method": method,
            "sign_method": "md5",
            "v": "2.0"
        }

        # איחוד פרמטרים
        all_params = {**sys_params, **api_params}
        
        # חתימה
        all_params["sign"] = self._generate_sign(all_params)

        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Cache-Control": "no-cache",
            "Connection": "Keep-Alive",
        }

        try:
            # שליחה ב-POST
            with httpx.Client(timeout=15.0) as client:
                response = client.post(self.gateway, data=all_params, headers=headers)
                response.raise_for_status()
                return response.json()
        except Exception as e:
            logger.error(f"Network Error: {e}")
            return None

    def get_details(self, product_id):
        """קבלת פרטי מוצר"""
        params = {
            "product_ids": product_id,
            "target_currency": "ILS",
            "target_language": "HE"
        }
        res = self.execute("aliexpress.affiliate.product.detail.get", params)
        
        # בדיקת תקינות תשובה
        if not res or "error_response" in res:
            err = res.get("error_response", {}) if res else "No Response"
            logger.error(f"API Error (Details): {err}")
            return None
            
        try:
            result = res["aliexpress_affiliate_product_detail_get_response"]["resp_result"]["result"]
            return result["products"]["product"][0]
        except (KeyError, IndexError):
            return None

    def generate_link(self, original_url):
        """יצירת לינק שותפים"""
        params = {
            "promotion_link_type": "0",  # 0 = רגיל, 2 = Hot Link
            "source_values": original_url,
            "tracking_id": "telegram_bot"
        }
        res = self.execute("aliexpress.affiliate.link.generate", params)

        if not res or "error_response" in res:
            err = res.get("error_response", {}) if res else "No Response"
            logger.error(f"API Error (Link): {err}")
            return None

        try:
            result = res["aliexpress_affiliate_link_generate_response"]["resp_result"]["result"]
            return result["promotion_links"]["promotion_link"][0]["promotion_link"]
        except (KeyError, IndexError):
            return None

# ==========================================
# כלי עזר (Utils)
# ==========================================
def extract_id(url):
    """חילוץ ID מהלינק"""
    # ניסיון 1: לפי סיומת html
    match = re.search(r'/item/(\d+)\.html', url)
    if match: return match.group(1)
    
    # ניסיון 2: סתם מספר ארוך
    match = re.search(r'(\d{11,})', url)
    if match: return match.group(1)
    
    return None

def resolve_url(url):
    """פתיחת קיצורים (bit.ly, s.click וכו')"""
    try:
        with httpx.Client(follow_redirects=True, timeout=10) as client:
            resp = client.head(url)
            return str(resp.url).split('?')[0] # מנקה זבל מה-URL
    except:
        return url

# ==========================================
# קופירייטר (OpenAI)
# ==========================================
class AIWriter:
    def __init__(self):
        self.client = OpenAI(api_key=Config.OPENAI_KEY) if Config.OPENAI_KEY else None

    def generate(self, text, price):
        if not self.client:
            return "מציאה מאליאקספרס! 👇"

        prompt = f"""
        כתוב פוסט מכירה קצר וקולע לטלגרם בעברית (סלנג קליל).
        המוצר: {text[:200]}
        מחיר: {price}
        
        מבנה:
        כותרת אש 🔥
        משפט התלהבות
        הנעה לפעולה
        בלי האשטאגים.
        """
        
        try:
            resp = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=150
            )
            return resp.choices[0].message.content.strip()
        except:
            return "דיל שווה בטירוף! אל תפספסו 👇"

# ==========================================
# הבוט הראשי
# ==========================================
async def main():
    logger.info("🚀 Starting Bot (Clean Version)...")
    
    # 1. התחברות לטלגרם
    client = TelegramClient(StringSession(Config.SESSION_STR), Config.API_ID, Config.API_HASH)
    await client.start()
    
    # 2. אתחול מחלקות
    ali = AliExpressClient(Config.APP_KEY, Config.APP_SECRET)
    ai = AIWriter()
    
    # 3. מעבר על ערוצים
    processed_count = 0
    
    for source in Config.SOURCE_CHANNELS:
        logger.info(f"👀 Scanning: {source}")
        
        try:
            # שליפת הודעות אחרונות
            messages = await client.get_messages(source, limit=Config.MAX_MESSAGES)
            
            for msg in messages:
                if not msg.text: continue

                # חיפוש לינקים
                links = re.findall(r'(https?://[^\s]+)', msg.text)
                for link in links:
                    if "aliexpress" not in link and "s.click" not in link:
                        continue
                        
                    # פתיחת הלינק וזיהוי מוצר
                    real_url = resolve_url(link)
                    pid = extract_id(real_url)
                    
                    if not pid:
                        continue
                        
                    logger.info(f"🔎 Found Product ID: {pid}")
                    
                    # בדיקת פרטים מול עליאקספרס
                    details = ali.get_details(pid)
                    if not details:
                        logger.warning(f"❌ Failed to get details for {pid}")
                        continue
                        
                    # יצירת לינק שותפים
                    aff_link = ali.generate_link(real_url)
                    if not aff_link:
                        logger.warning(f"❌ Failed to generate link for {pid}")
                        continue
                        
                    # הכנת פוסט
                    price = details.get("target_sale_price", "") + " " + details.get("target_sale_price_currency", "ILS")
                    title = details.get("product_title", "")
                    caption = ai.generate(title, price)
                    
                    final_msg = f"{caption}\n\n👇 לפרטים ורכישה:\n{aff_link}"
                    
                    # שליחה
                    img_url = details.get("product_main_image_url")
                    try:
                        if img_url:
                            await client.send_file(Config.TARGET_CHANNEL, img_url, caption=final_msg)
                        else:
                            await client.send_message(Config.TARGET_CHANNEL, final_msg)
                        
                        logger.info(f"✅ Posted: {pid}")
                        processed_count += 1
                        time.sleep(2) # השהייה קטנה
                        
                    except Exception as e:
                        logger.error(f"Failed to send telegram msg: {e}")

        except Exception as e:
            logger.error(f"Error scanning channel {source}: {e}")

    logger.info(f"🏁 Done. Processed {processed_count} items.")
    await client.disconnect()

if __name__ == '__main__':
    asyncio.run(main())
