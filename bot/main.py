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
from datetime import datetime, timezone

import httpx
from telethon import TelegramClient, events, sync
from telethon.sessions import StringSession
from openai import OpenAI

# ==========================================
# הגדרות לוגים
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
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
    MAX_MESSAGES = 30

# בדיקת משתנים קריטיים
if not Config.APP_KEY or not Config.APP_SECRET:
    logger.critical("❌ Missing ALIEXPRESS_APP_KEY or ALIEXPRESS_APP_SECRET")
    sys.exit(1)

# ==========================================
# מחלקת עליאקספרס
# ==========================================
class AliExpressClient:
    def __init__(self, app_key, app_secret):
        self.app_key = app_key
        self.app_secret = app_secret
        self.gateway = "https://api-sg.aliexpress.com/router/rest"

    def _generate_sign(self, params):
        keys = sorted(params.keys())
        sign_str = self.app_secret
        for key in keys:
            val = str(params[key])
            sign_str += f"{key}{val}"
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
        all_params["sign"] = self._generate_sign(all_params)

        headers = {
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        }

        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.post(self.gateway, data=all_params, headers=headers)
                data = response.json()
                
                # --- דיבאג קריטי: הדפסת התשובה המלאה במקרה של שגיאה ---
                if "error_response" in data:
                    logger.error(f"⚠️ API Error Response: {json.dumps(data)}")
                    return None
                # --------------------------------------------------------
                
                return data
        except Exception as e:
            logger.error(f"Network Error: {e}")
            return None

    def get_details(self, product_id):
        params = {
            "product_ids": product_id,
            "target_currency": "ILS",
            "target_language": "HE"
        }
        res = self.execute("aliexpress.affiliate.product.detail.get", params)
        if not res: return None
        
        try:
            # בדיקה אם רשימת המוצרים ריקה (קורה כשהמוצר לא זמין או לא קיים)
            products_list = res.get("aliexpress_affiliate_product_detail_get_response", {}).get("resp_result", {}).get("result", {}).get("products", {}).get("product")
            
            if not products_list:
                logger.warning(f"⚠️ No product details found for ID: {product_id} (Maybe restricted or invalid)")
                return None
                
            return products_list[0]
        except Exception as e:
            logger.error(f"Parsing Error: {e} | Raw Data: {str(res)[:200]}")
            return None

    def generate_link(self, original_url):
        params = {
            "promotion_link_type": "0",
            "source_values": original_url,
            "tracking_id": "telegram_bot"
        }
        res = self.execute("aliexpress.affiliate.link.generate", params)
        if not res: return None

        try:
            return res["aliexpress_affiliate_link_generate_response"]["resp_result"]["result"]["promotion_links"]["promotion_link"][0]["promotion_link"]
        except:
            return None

# ==========================================
# כלי עזר
# ==========================================
def extract_id(url):
    match = re.search(r'/item/(\d+)\.html', url)
    if match: return match.group(1)
    match = re.search(r'(\d{11,})', url)
    if match: return match.group(1)
    return None

def resolve_url(url):
    try:
        # הוספת headers כדי לעקוף חסימות בסיסיות של עליאקספרס
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
        }
        with httpx.Client(follow_redirects=True, timeout=10, headers=headers) as client:
            resp = client.head(url)
            # אם קיבלנו שגיאה 502 (כמו בלוג שלך), נחזיר את הכתובת המקורית וננסה
            if resp.status_code >= 400:
                return url
            return str(resp.url).split('?')[0]
    except:
        return url

# ==========================================
# בוט
# ==========================================
class AIWriter:
    def __init__(self):
        self.client = OpenAI(api_key=Config.OPENAI_KEY) if Config.OPENAI_KEY else None

    def generate(self, text, price):
        if not self.client: return "מציאה מאליאקספרס! 👇"
        try:
            prompt = f"כתוב פוסט מכירה קצר לטלגרם (סלנג עברי). מוצר: {text[:100]}. מחיר: {price}. בלי האשטאגים."
            resp = self.client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
            return resp.choices[0].message.content.strip()
        except:
            return "דיל שווה בטירוף! אל תפספסו 👇"

async def main():
    logger.info("🚀 Starting Bot (Debug Mode)...")
    
    try:
        client = TelegramClient(StringSession(Config.SESSION_STR), Config.API_ID, Config.API_HASH)
        await client.start()
    except Exception as e:
        logger.critical(f"Login Failed: {e}. Please regenerate TG_SESSION!")
        sys.exit(1)

    ali = AliExpressClient(Config.APP_KEY, Config.APP_SECRET)
    ai = AIWriter()
    
    processed_count = 0
    
    # בדיקת חיבור בסיסית - לוודא שאנחנו לא רצים על ריק
    logger.info(f"Target Channel: {Config.TARGET_CHANNEL}")
    
    for source in Config.SOURCE_CHANNELS:
        logger.info(f"👀 Scanning: {source}")
        try:
            messages = await client.get_messages(source, limit=Config.MAX_MESSAGES)
            for msg in messages:
                if not msg.text: continue
                links = re.findall(r'(https?://[^\s]+)', msg.text)
                for link in links:
                    if "aliexpress" not in link and "s.click" not in link: continue
                    
                    real_url = resolve_url(link)
                    pid = extract_id(real_url)
                    if not pid: continue
                    
                    logger.info(f"🔎 Found ID: {pid}")
                    
                    # שלב 1: משיכת פרטים
                    details = ali.get_details(pid)
                    if not details: 
                        logger.warning(f"⏩ Skipping {pid} - No details returned.")
                        continue 
                    
                    # שלב 2: יצירת לינק
                    aff_link = ali.generate_link(real_url)
                    if not aff_link: 
                        logger.warning(f"⏩ Skipping {pid} - Failed to generate affiliate link.")
                        continue
                    
                    # שלב 3: שליחה
                    price = details.get("target_sale_price", "") + " " + details.get("target_sale_price_currency", "ILS")
                    caption = ai.generate(details.get("product_title", ""), price)
                    final_msg = f"{caption}\n\n👇 לרכישה:\n{aff_link}"
                    
                    try:
                        if details.get("product_main_image_url"):
                            await client.send_file(Config.TARGET_CHANNEL, details.get("product_main_image_url"), caption=final_msg)
                        else:
                            await client.send_message(Config.TARGET_CHANNEL, final_msg)
                        
                        logger.info(f"✅ Posted: {pid}")
                        processed_count += 1
                        time.sleep(2)
                    except Exception as e:
                         logger.error(f"❌ Send Error: {e}")
                         
        except Exception as e:
            logger.error(f"Channel Error: {e}")

    logger.info(f"🏁 Done. Total posted: {processed_count}")

if __name__ == '__main__':
    asyncio.run(main())
