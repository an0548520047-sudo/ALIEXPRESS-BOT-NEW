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

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession
from openai import OpenAI

# ==========================================
# 1. הגדרות לוגים (שנבין מה קורה)
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# 2. הגדרות קונפיגורציה
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
    
    # הגדרות כלליות
    MAX_MESSAGES = 40  # כמה הודעות אחרונות לסרוק מכל ערוץ
    HISTORY_FILE = "history.txt"

# בדיקת חובה
if not Config.APP_KEY or not Config.APP_SECRET:
    logger.critical("❌ Missing ALIEXPRESS_APP_KEY or ALIEXPRESS_APP_SECRET in Secrets!")
    sys.exit(1)

# ==========================================
# 3. מחלקת עליאקספרס (הלב של הבוט)
# ==========================================
class AliExpressClient:
    def __init__(self, app_key, app_secret):
        self.app_key = app_key
        self.app_secret = app_secret
        self.gateway = "https://api-sg.aliexpress.com/router/rest"

    def _sign(self, params):
        """יצירת חתימה דיגיטלית לפי דרישות עליאקספרס"""
        keys = sorted(params.keys())
        sign_str = self.app_secret
        for key in keys:
            sign_str += f"{key}{params[key]}"
        sign_str += self.app_secret
        return hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

    def execute(self, method, api_params):
        """שליחת בקשה לשרת"""
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
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(
                    self.gateway, 
                    data=all_params, 
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
                )
                data = resp.json()
                
                if "error_response" in data:
                    err = data["error_response"]
                    logger.error(f"⚠️ API Error: {err.get('msg')} (Code: {err.get('code')})")
                    return None
                return data
        except Exception as e:
            logger.error(f"Network Error: {e}")
            return None

    def get_details(self, product_id):
        """משיכת פרטי מוצר"""
        # אנו מבקשים דולרים כדי להימנע מבעיות "לא נשלח לישראל" שחוסמות את ה-API
        params = {
            "product_ids": product_id,
            "target_currency": "USD",
            "target_language": "EN"
        }
        res = self.execute("aliexpress.affiliate.product.detail.get", params)
        if not res: return None

        try:
            result = res["aliexpress_affiliate_product_detail_get_response"]["resp_result"]["result"]
            products = result.get("products", {}).get("product")
            if products:
                return products[0]
            logger.warning(f"⚠️ Item {product_id} exists but returned no data (Maybe sold out).")
            return None
        except Exception:
            return None

    def generate_link(self, original_url):
        """יצירת קישור שותפים"""
        params = {
            "promotion_link_type": "0",
            "source_values": original_url,
            "tracking_id": "telegram_bot"
        }
        res = self.execute("aliexpress.affiliate.link.generate", params)
        if not res: return None

        try:
            return res["aliexpress_affiliate_link_generate_response"]["resp_result"]["result"]["promotion_links"]["promotion_link"][0]["promotion_link"]
        except Exception:
            return None

# ==========================================
# 4. מנוע AI (כתיבת פוסטים)
# ==========================================
class AIWriter:
    def __init__(self):
        self.client = OpenAI(api_key=Config.OPENAI_KEY) if Config.OPENAI_KEY else None

    def generate(self, title, price):
        if not self.client:
            return "מציאה חדשה מאליאקספרס! 👇"
        
        prompt = (
            f"כתוב פוסט טלגרם קצר, שיווקי וקליט בעברית (סלנג קליל).\n"
            f"המוצר: {title}\n"
            f"המחיר: {price}\n"
            f"הנחיות: כותרת עם אימוג'י, משפט התלהבות, והנעה לפעולה. בלי האשטאגים."
        )
        try:
            resp = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=200
            )
            return resp.choices[0].message.content.strip()
        except Exception:
            return "דיל מטורף מאליאקספרס! אל תפספסו 🔥"

# ==========================================
# 5. כלי עזר (קישורים ו-ID)
# ==========================================
def resolve_url(url):
    """
    פותח קיצורים וממיר קישורי US לקישורים גלובליים
    """
    try:
        # קוקיז שמכריחים את האתר להיות גלובלי ולא אמריקאי
        cookies = {
            "xman_us_f": "x_l=0&x_locale=en_US", 
            "int_locale": "en_US",
            "aep_usuc_f": "region=IL&site=glo&b_locale=en_US&c_tp=USD"
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/110.0.0.0 Safari/537.36"
        }
        
        with httpx.Client(follow_redirects=True, timeout=15, headers=headers, cookies=cookies) as client:
            resp = client.head(url)
            final_url = str(resp.url)
            
            # תיקון קריטי: אם הגענו ל-aliexpress.us, נחליף ל-.com
            if "aliexpress.us" in final_url:
                final_url = final_url.replace("aliexpress.us", "aliexpress.com")
            
            return final_url.split('?')[0]
    except Exception:
        return url

def extract_id(url):
    """מוציא את המספר המזהה מהקישור"""
    # עדיפות לפורמט גלובלי (1005...)
    match = re.search(r'/item/(1005\d{10,})\.html', url)
    if match: return match.group(1)
    
    # פורמט כללי
    match = re.search(r'/item/(\d+)\.html', url)
    if match: return match.group(1)
    return None

# ==========================================
# 6. הריצה הראשית (Main)
# ==========================================
async def main():
    logger.info("🚀 Bot Starting...")
    
    # טעינת היסטוריה
    processed_ids = set()
    if os.path.exists(Config.HISTORY_FILE):
        with open(Config.HISTORY_FILE, "r") as f:
            processed_ids = set(f.read().splitlines())
    logger.info(f"📚 History loaded: {len(processed_ids)} items.")

    # התחברות לטלגרם
    try:
        client = TelegramClient(StringSession(Config.SESSION_STR), Config.API_ID, Config.API_HASH)
        await client.start()
    except Exception as e:
        logger.critical(f"❌ Telegram Login Failed: {e}")
        sys.exit(1)

    ali = AliExpressClient(Config.APP_KEY, Config.APP_SECRET)
    ai = AIWriter()
    
    new_posts_count = 0
    
    # סריקת ערוצים
    for channel in Config.SOURCE_CHANNELS:
        logger.info(f"👀 Scanning source: {channel}")
        try:
            messages = await client.get_messages(channel, limit=Config.MAX_MESSAGES)
            
            for msg in messages:
                if not msg.text: continue
                
                # חיפוש כל הלינקים בהודעה
                links = re.findall(r'(https?://[^\s]+)', msg.text)
                for link in links:
                    if "aliexpress" not in link and "s.click" not in link: continue
                    
                    # פענוח הלינק
                    real_url = resolve_url(link)
                    pid = extract_id(real_url)
                    
                    if not pid: continue
                    if pid in processed_ids: continue # דלג אם כבר פורסם
                    
                    logger.info(f"🔎 Processing ID: {pid}")
                    
                    # 1. משיכת פרטים
                    details = ali.get_details(pid)
                    if not details:
                        # אם נכשל, נשמור בהיסטוריה כדי לא לנסות שוב סתם
                        processed_ids.add(pid) 
                        continue

                    # 2. יצירת לינק שותפים
                    aff_link = ali.generate_link(real_url)
                    if not aff_link: continue
                    
                    # 3. יצירת תוכן
                    price = f"{details.get('target_sale_price', 'Unknown')} {details.get('target_sale_price_currency', 'USD')}"
                    title = details.get('product_title', 'מוצר מומלץ')
                    text = ai.generate(title, price)
                    
                    final_msg = f"{text}\n\n👇 לרכישה:\n{aff_link}"
                    
                    # 4. שליחה
                    try:
                        img = details.get("product_main_image_url")
                        if img:
                            await client.send_file(Config.TARGET_CHANNEL, img, caption=final_msg)
                        else:
                            await client.send_message(Config.TARGET_CHANNEL, final_msg)
                            
                        logger.info(f"✅ Posted successfully: {pid}")
                        
                        # עדכון היסטוריה
                        processed_ids.add(pid)
                        new_posts_count += 1
                        with open(Config.HISTORY_FILE, "a") as f:
                            f.write(f"{pid}\n")
                        
                        time.sleep(3) # מנוחה קצרה
                        
                    except Exception as e:
                        logger.error(f"❌ Send Error: {e}")

        except Exception as e:
            logger.error(f"Error reading channel {channel}: {e}")

    logger.info(f"🏁 Run finished. Posted {new_posts_count} new items.")

if __name__ == '__main__':
    asyncio.run(main())
