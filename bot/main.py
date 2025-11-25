import broker
import telethon
import sys
import os
import asyncio
import re
from datetime import datetime, timezone
from urllib.parse import quote
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message

# ==== ENV ====
def _must_get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

def _get_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {'1', 'true', 'yes', 'y', 'on'}

tg_api_id = int(_must_get_env("TG_API_ID"))
tg_api_hash = _must_get_env("TG_API_HASH")
tg_session = _must_get_env("TG_SESSION")
tg_source_channels = [c.strip() for c in _must_get_env("TG_SOURCE_CHANNELS").split(",") if c.strip()]
tg_target_channel = _must_get_env("TG_TARGET_CHANNEL")
affiliate_prefix = _must_get_env("AFFILIATE_PREFIX")
openai_api_key = _must_get_env("OPENAI_API_KEY")
openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
min_views = int(os.getenv("MIN_VIEWS", "1500"))
max_messages_per_channel = int(os.getenv("MAX_MESSAGES_PER_CHANNEL", "80"))
dry_run = _get_bool_env("DRY_RUN", False)
REPEAT_COOLDOWN_DAYS = int(os.getenv("REPEAT_COOLDOWN_DAYS", "3"))

client = TelegramClient(StringSession(tg_session), tg_api_id, tg_api_hash)
oa_client = OpenAI(api_key=openai_api_key)

# ==== עזרים ====
ali_regex = re.compile(r"https?://[^\s]+", re.IGNORECASE)

def extract_links(text):
    return ali_regex.findall(text) if text else []

def get_product_id(url):
    match = re.search(r"/item/(\d+)\.html", url)
    if match:
        return match.group(1)
    match = re.search(r"/(\d+)\.html", url)
    if match:
        return match.group(1)
    return url.split("?")[0]

def make_affiliate_link(url):
    return f"{affiliate_prefix}{quote(url, safe='')}"

def format_message(content, product_id):
    return f"{content}\n\n(id:{product_id})"

def clean_orig_text(text):
    # מוחק כל לינק מהטקסט כדי שלא יישארו בכלל קישורים מקוריים
    return re.sub(r'https?://[^\s]+', '', text).strip()

async def already_posted_recently(product_id: str) -> bool:
    async for msg in client.iter_messages(tg_target_channel, limit=400):
        if not msg.message or f"(id:{product_id})" not in msg.message:
            continue
        if msg.date:
            days_since = (datetime.now(timezone.utc) - msg.date).days
            if days_since < REPEAT_COOLDOWN_DAYS:
                return True
    return False

def rewrite_caption(orig_text, affiliate_url):
    clean_text = clean_orig_text(orig_text)
    prompt = (
        "כתוב פוסט דיל בעברית, בגובה העיניים, קצר ושימושי – כאילו אתה ממליץ לחבר קבוצה. "
        "אסור להכניס קישור בשום מקום בפוסט! תן משפט פתיחה, 1–2 אימוג'ים, ונקודות עיקריות כגון מחיר/דירוג/הזמנות רק אם מופיעים בטקסט. "
        "בסיום הפוסט כתוב 'לקנייה, ראו את הלינק כאן למטה.' אין להמציא מידע!"
        f"\nהנה המידע על הדיל:\n{clean_text}"
    )
    response = oa_client.chat.completions.create(
        model=openai_model,
        messages=[
            {"role": "system", "content": "לא להוסיף לינקים בפוסט בכלל! ולא להמציא מידע. הכל בעברית בלבד."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.65,
        max_tokens=380
    )
    # מוסיף תמיד את הלינק שלך, ולעולם לא לינק שונה מהשורה הזאת!
    return response.choices[0].message.content.strip() + f"\n\n👇 לקנייה באליאקספרס:\n{affiliate_url}"

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
        links = extract_links(msg.message)
        if not links:
            continue
        original_url = links[0]
        affiliate_url = make_affiliate_link(original_url)
        product_id = get_product_id(original_url)
        if await already_posted_recently(product_id):
            log_info(f"דיל {product_id} פורסם ב–{REPEAT_COOLDOWN_DAYS} ימים האחרונים, דילוג.")
            continue
        try:
            new_caption = rewrite_caption(msg.message, affiliate_url)
        except Exception as exc:
            log_info(f"שגיאת OpenAI: {exc}")
            new_caption = clean_orig_text(msg.message) + f"\n\n👇 לקנייה באליאקספרס:\n{affiliate_url}"
        final_text = format_message(new_caption, product_id)
        if dry_run:
            log_info(f"(DRY_RUN) היה נשלח {product_id} לערוץ היעד")
            continue
        try:
            if msg.photo:
                await client.send_file(
                    tg_target_channel,
                    msg.photo,
                    caption=final_text,
                    force_document=False
                )
                log_info(f"*** פורסם (תמונה+טקסט) product_id={product_id} ***")
            else:
                await client.send_message(tg_target_channel, final_text)
                log_info(f"*** פורסם (טקסט בלבד) product_id={product_id} ***")
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
