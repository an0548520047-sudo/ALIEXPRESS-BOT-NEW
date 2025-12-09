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
# 1. הגדרות לוגים
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

if not Config.APP_KEY or not Config.APP_SECRET:
    logger.critical("❌ Missing Keys!")
    sys.exit(1)

# ==========================================
# 3. מחלקת עליאקספרס
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
            with httpx.Client(timeout=20.0) as client:
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
        params = {"product_ids": product_id, "target_currency": "USD", "target_language": "EN"}
        res = self.execute("aliexpress.affiliate.product.detail.get", params)
        if not res: return None
        try:
            return res["aliexpress_affiliate_product_detail_get_response"]["resp_result"]["result"]["products"]["product"][0]
        except:
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
# 4. מנוע AI
# ==========================================
class AIWriter:
    def __init__(self):
        self.client = OpenAI(api_key=Config.OPENAI_KEY) if Config.OPENAI_KEY else None

    def generate(self, title, price):
        if not self.client: return "מציאה חדשה! 👇"
        try:
            prompt = f"פוסט טלגרם קצר בעברית. מוצר: {title}. מחיר: {price}. בלי האשטאגים."
            resp = self.client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": prompt}]
            )
            return resp.choices[0].message.content.strip()
        except:
            return "דיל מטורף מאליאקספרס! 🔥"

# ==========================================
# 5. כלי עזר
# ==========================================
def resolve_url(url):
    try:
        cookies = {"xman_us_f": "x_l=0&x_locale=en_US", "aep_usuc_f": "region=IL&site=glo&b_locale=en_US&c_tp=USD"}
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/110.0.0.0 Safari/537.36"}
        with httpx.Client(follow_redirects=True, timeout=15, headers=headers, cookies=cookies) as client:
            resp = client.head(url)
            final = str(resp.url)
            if "aliexpress.us" in final: return final.replace("aliexpress.us", "aliexpress.com")
            return final.split('?')[0]
    except:
        return url

def extract_id(url):
    match = re.search(r'/item/(1005\d{10,})\.html', url)
    if match: return match.group(1)
    match = re.search(r'/item/(\d+)\.html', url)
    if match: return match.group(1)
    return None

# ==========================================
# 6. הריצה הראשית
# ==========================================
async def main():
    logger.info("🚀 Bot Starting (Aggressive Mode)...")
    
    # טעינת היסטוריה
    processed_ids = set()
    if os.path.exists(Config.HISTORY_FILE):
        with open(Config.HISTORY_FILE, "r") as f:
            processed_ids = set(f.read().splitlines())
    
    # התחברות
    client = TelegramClient(StringSession(Config.SESSION_STR), Config.API_ID, Config.API_HASH)
    await client.start()

    ali = AliExpressClient(Config.APP_KEY, Config.APP_SECRET)
    ai = AIWriter()
    
    for channel in Config.SOURCE_CHANNELS:
        logger.info(f"👀 Scanning: {channel}")
        try:
            messages = await client.get_messages(channel, limit=Config.MAX_MESSAGES)
            for msg in messages:
                if not msg.text: continue
                links = re.findall(r'(https?://[^\s]+)', msg.text)
                
                for link in links:
                    if "aliexpress" not in link and "s.click" not in link: continue
                    
                    real_url = resolve_url(link)
                    pid = extract_id(real_url)
                    
                    if not pid or pid in processed_ids: continue
                    
                    logger.info(f"⚡ Processing: {pid}")
                    
                    # ניסיון ליצור לינק אפיליאייט (הכי חשוב!)
                    aff_link = ali.generate_link(real_url)
                    if not aff_link:
                        logger.warning(f"⏩ Failed to generate link for {pid}")
                        continue

                    # ניסיון למשוך פרטים (אופציונלי - לא עוצר אם נכשל)
                    details = ali.get_details(pid)
                    
                    if details:
                        title = details.get('product_title', 'מוצר מומלץ')
                        price = f"{details.get('target_sale_price', '')} USD"
                        img = details.get("product_main_image_url")
                    else:
                        # אם אין פרטים, ננסה לקחת מהודעת המקור או נשים ברירת מחדל
                        title = "מציאה מאליאקספרס"
                        price = "מחיר מעולה"
                        img = None # נשלח הודעת טקסט בלבד
                    
                    text = ai.generate(title, price)
                    final_msg = f"{text}\n\n👇 לרכישה:\n{aff_link}"

                    try:
                        if img:
                            await client.send_file(Config.TARGET_CHANNEL, img, caption=final_msg)
                        elif msg.media: # ננסה להשתמש בתמונה מההודעה המקורית
                            await client.send_file(Config.TARGET_CHANNEL, msg.media, caption=final_msg)
                        else:
                            await client.send_message(Config.TARGET_CHANNEL, final_msg, link_preview=True)
                        
                        logger.info(f"✅ POSTED: {pid}")
                        processed_ids.add(pid)
                        with open(Config.HISTORY_FILE, "a") as f: f.write(f"{pid}\n")
                        time.sleep(3)
                    except Exception as e:
                        logger.error(f"❌ Send Error: {e}")

        except Exception as e:
            logger.error(f"Channel Error: {e}")

    # שמירה וסיום
    logger.info("🏁 Done.")

if __name__ == '__main__':
    asyncio.run(main())
