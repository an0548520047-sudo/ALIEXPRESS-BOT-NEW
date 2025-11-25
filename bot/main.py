import telethon
import os
import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote
from io import BytesIO
import requests
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message
import hashlib
import hmac
import time

### הגדרות סביבה ###
def _must_get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

tg_api_id = int(_must_get_env("TG_API_ID"))
tg_api_hash = _must_get_env("TG_API_HASH")
tg_session = _must_get_env("TG_SESSION")
tg_source_channels = [c.strip() for c in _must_get_env("TG_SOURCE_CHANNELS").split(",") if c.strip()]
tg_target_channel = _must_get_env("TG_TARGET_CHANNEL")
openai_api_key = _must_get_env("OPENAI_API_KEY")
openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
min_views = int(os.getenv("MIN_VIEWS", "1500"))
max_messages_per_channel = int(os.getenv("MAX_MESSAGES_PER_CHANNEL", "80"))
app_key = _must_get_env("ALIEXPRESS_API_APP_KEY")
app_secret = _must_get_env("ALIEXPRESS_API_APP_SECRET")
REPEAT_COOLDOWN_DAYS = int(os.getenv("REPEAT_COOLDOWN_DAYS", "3"))

client = TelegramClient(StringSession(tg_session), tg_api_id, tg_api_hash)
oa_client = OpenAI(api_key=openai_api_key)

### פונקציות עזר ###
def extract_aliexpress_links(text):
    aliex_regex = re.compile(r"https?://[^\s]*aliexpress\.com[^\s]*", re.IGNORECASE)
    return aliex_regex.findall(text) if text else []

def get_product_id(url):
    match = re.search(r"/item/(\d+)\.html", url)
    if match:
        return match.group(1)
    match = re.search(r"/(\d+)\.html", url)
    if match:
        return match.group(1)
    return url.split("?")[0]

def extract_price_from_text(text):
    price_match = re.search(r'(\d+[\.,]?\d*)\s*[₪$]|(\d+[\.,]?\d*)\s*ש"ח', text)
    if price_match:
        return price_match.group(1) or price_match.group(2)
    return None

def extract_coupons_from_text(text):
    coupon_matches = re.findall(r'[A-Z0-9]{4,15}', text)
    return coupon_matches[:3] if coupon_matches else []

def format_message(content, product_id):
    return f"{content}\n\n(id:{product_id})"

async def already_posted_recently(product_id: str) -> bool:
    async for msg in client.iter_messages(tg_target_channel, limit=400):
        if not msg.message or f"(id:{product_id})" not in msg.message:
            continue
        if msg.date:
            days_since = (datetime.now(timezone.utc) - msg.date).days
            if days_since < REPEAT_COOLDOWN_DAYS:
                return True
    return False

### === יצירת חתימה ל-API של עליאקספרס === ###
def sign_params(params, app_secret):
    param_str = ""
    for key in sorted(params.keys()):
        param_str += f"{key}{params[key]}"
    sign_str = app_secret + param_str + app_secret
    sign = hashlib.md5(sign_str.encode('utf-8')).hexdigest().upper()
    return sign

### === יצירת לינק שותף === ###
def make_affiliate_link_aliexpress(product_url, app_key, app_secret):
    api_method = "portals.open/api.getPromotionLinks"
    timestamp = str(int(time.time() * 1000))
    params = {
        "app_key": app_key,
        "timestamp": timestamp,
        "sign_method": "md5",
        "promotion_link_type": "1",
        "urls": product_url,
        "format": "json",
        "v": "2.0"
    }
    params["sign"] = sign_params(params, app_secret)
    api_url = f'https://gw-api.aliexpress.com/openapi/param2/2/portals.open/api.getPromotionLinks/{app_key}'
    resp = requests.get(api_url, params=params)
    print("== API raw response (affiliate link) ==", resp.text)
    try:
        data = resp.json()
    except Exception as e:
        print("שגיאה בפיענוח JSON מה-API:", e)
        print("תוכן תגובה:", resp.text)
        return None
    try:
        return data["result"]["promotion_links"][0]['promotion_link']
    except Exception:
        print("ה־API לא החזיר קישור נכון:", data)
        return None

### === משיכת פרטי מוצר + תמונה מאליאקספרס === ###
def get_product_details_from_aliexpress(product_id, app_key, app_secret):
    api_method = "aliexpress.open/api.getProducts"
    timestamp = str(int(time.time() * 1000))
    params = {
        "app_key": app_key,
        "timestamp": timestamp,
        "sign_method": "md5",
        "product_ids": product_id,
        "target_currency": "ILS",
        "target_language": "HE",
        "format": "json",
        "v": "2.0"
    }
    params["sign"] = sign_params(params, app_secret)
    api_url = f'https://gw-api.aliexpress.com/openapi/param2/2/aliexpress.open/api.getProducts/{app_key}'
    resp = requests.get(api_url, params=params)
    print("== API raw response (product details) ==", resp.text)
    try:
        data = resp.json()
    except Exception as e:
        print("שגיאה בפיענוח JSON מה-API:", e)
        print("תוכן תגובה:", resp.text)
        return None
    try:
        product = data["result"]["products"][0]
        return {
            "title": product.get("product_title", ""),
            "price": product.get("target_sale_price", ""),
            "original_price": product.get("target_original_price", ""),
            "rating": product.get("evaluate_rate", ""),
            "orders": product.get("lastest_volume", ""),
            "image_url": product.get("product_main_image_url", ""),
        }
    except Exception as e:
        print("לא הצליח למשוך פרטי מוצר:", e, data)
        return None

def download_image(image_url):
    try:
        resp = requests.get(image_url, timeout=10)
        return BytesIO(resp.content)
    except Exception as e:
        print(f"שגיאה בהורדת תמונה: {e}")
        return None

### בניית פוסט חדש עם GPT ###
def create_post_from_product_data(product_data, affiliate_url, extracted_coupons):
    coupon_text = ""
    if extracted_coupons:
        coupon_text = f"\n🎁 קודי קופון: {', '.join(extracted_coupons)}"
    prompt = (
        f"כתוב פוסט דיל בעברית, קצר ומזמין, כאילו חבר ממליץ בקבוצה.\n"
        f"פרטי המוצר:\n"
        f"- שם: {product_data['title']}\n"
        f"- מחיר: {product_data['price']} ₪\n"
        f"- דירוג: {product_data['rating']}\n"
        f"- הזמנות: {product_data['orders']}\n"
        f"{coupon_text}\n"
        f"תן משפט פתיחה, 1-2 אימוג׳ים, נקודות עיקריות, ובסוף כתוב: 'לקנייה, ראו לינק למטה.'"
    )
    response = oa_client.chat.completions.create(
        model=openai_model,
        messages=[
            {"role": "system", "content": "כתוב פוסטים בעברית, קצרים, טבעיים וידידותיים."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.7,
        max_tokens=400
    )
    caption = response.choices[0].message.content.strip()
    return caption + f"\n\n👇 לקנייה באליאקספרס:\n{affiliate_url}"

def log_info(msg): print(msg, flush=True)

def is_good_post(msg: Message):
    if not msg.message:
        return False
    text = msg.message.lower()
    keywords = ["₪", "$", "discount", "coupon", "קופון", "דיל", "מבצע", "%", "קוד"]
    if not any(kw in text for kw in keywords):
        return False
    if msg.views is not None and msg.views < min_views:
        return False
    return True

async def process_channel(channel):
    log_info(f"== סורק ערוץ: {channel} ==")
    async for msg in client.iter_messages(channel, limit=max_messages_per_channel):
        now = datetime.now()
        if now.hour < 7 or now.hour >= 24:
            log_info("לא שולח פוסט - מחוץ לשעות 7:00–00:00")
            continue
        if not is_good_post(msg):
            continue
        links = extract_aliexpress_links(msg.message)
        if not links:
            continue
        original_url = links[0]
        product_id = get_product_id(original_url)
        if await already_posted_recently(product_id):
            log_info(f"דיל {product_id} פורסם ב–{REPEAT_COOLDOWN_DAYS} ימים האחרונים, דילוג.")
            continue
        # חילוץ מחיר וקופונים מהטקסט המקורי
        extracted_price = extract_price_from_text(msg.message)
        extracted_coupons = extract_coupons_from_text(msg.message)
        # יצירת לינק שותף
        affiliate_url = make_affiliate_link_aliexpress(original_url, app_key, app_secret)
        if not affiliate_url:
            log_info(f"לא הצליח ליצור קישור שותף ל־{product_id}")
            continue
        # משיכת פרטי מוצר אמיתיים מאליאקספרס
        product_data = get_product_details_from_aliexpress(product_id, app_key, app_secret)
        if not product_data or not product_data.get("image_url"):
            log_info(f"לא הצליח למשוך פרטי מוצר ל־{product_id}")
            continue
        # בניית פוסט חדש
        try:
            new_caption = create_post_from_product_data(product_data, affiliate_url, extracted_coupons)
        except Exception as exc:
            log_info(f"שגיאת OpenAI: {exc}")
            new_caption = f"{product_data['title']}\n\n👇 לקנייה באליאקספרס:\n{affiliate_url}"
        final_text = format_message(new_caption, product_id)
        # הורדת תמונה מאליאקספרס
        image_file = download_image(product_data['image_url'])
        if not image_file:
            log_info(f"לא הצליח להוריד תמונה ל־{product_id}")
            continue
        # פרסום
        try:
            await client.send_file(
                tg_target_channel,
                image_file,
                caption=final_text,
                force_document=False
            )
            log_info(f"*** פורסם (תמונה מקורית+טקסט) product_id={product_id} ***")
        except Exception as exc:
            log_info(f"שגיאת שליחה: {exc}")

async def main():
    for channel in tg_source_channels:
        await process_channel(channel)

if __name__ == "__main__":
    async def runner():
        await client.start()
        await main()
    asyncio.run(runner())
