import asyncio
import os
import re
from urllib.parse import quote

from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message

# ============
# ENV / SECRETS
# ============

def _must_get_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _get_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default

    normalized = raw.strip().lower()
    return normalized in {"1", "true", "yes", "y", "on"}


tg_api_id = int(_must_get_env("TG_API_ID"))
tg_api_hash = _must_get_env("TG_API_HASH")
tg_session = _must_get_env("TG_SESSION")

tg_source_channels = [
    c.strip() for c in _must_get_env("TG_SOURCE_CHANNELS").split(",") if c.strip()
]

tg_target_channel = _must_get_env("TG_TARGET_CHANNEL")
affiliate_prefix = _must_get_env("AFFILIATE_PREFIX")
openai_api_key = _must_get_env("OPENAI_API_KEY")

openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
min_views = int(os.getenv("MIN_VIEWS", "1500"))
max_messages_per_channel = int(os.getenv("MAX_MESSAGES_PER_CHANNEL", "80"))
dry_run = _get_bool_env("DRY_RUN", False)

client = TelegramClient(StringSession(tg_session), tg_api_id, tg_api_hash)
oa_client = OpenAI(api_key=openai_api_key)

# ============
# UTILITIES
# ============

ali_regex = re.compile(r"https?://[^\s]*aliexpress\.com[^\s]*", re.IGNORECASE)


def extract_aliexpress_links(text: str) -> list[str]:
    if not text:
        return []
    return ali_regex.findall(text)


def normalize_aliexpress_id(url: str) -> str:
    """Extract a stable identifier to detect duplicates."""
    match = re.search(r"/item/(\d+)\.html", url)
    if match:
        return match.group(1)

    match = re.search(r"/(\d+)\.html", url)
    if match:
        return match.group(1)

    return url.split("?")[0]


def make_affiliate_link(original_url: str) -> str:
    encoded = quote(original_url, safe="")
    return f"{affiliate_prefix}{encoded}"


def rewrite_caption(orig_text: str, affiliate_url: str) -> str:
    prompt = (
        "אתה כותב פוסטים לקבוצת טלגרם של דילים ומבצעים בעברית.\n\n"
        "קבל טקסט מקורי של פוסט (להשראה בלבד) ולינק מוצר. תכתוב פוסט חדש, מקורי וקצר (3–6 שורות) בעברית,\n"
        "בסגנון קליל ומוכר עם קצת אימוג'ים, שמסביר למה המוצר שימושי או מגניב. אל תעתיק משפטים כפי שהם ואל תזכיר\n"
        "קבוצות אחרות או את המילה \"אליאקספרס\".\n\n"
        f"בסוף הפוסט הוסף שורה אחת בדיוק:\n🔗 לינק: {affiliate_url}\n\n"
        "הטקסט המקורי (רק להשראה):\n"
        f"{orig_text}\n"
    )

    response = oa_client.chat.completions.create(
        model=openai_model,
        messages=[
            {"role": "system", "content": "אתה כותב קופי בעברית לקבוצת דילים בטלגרם."},
            {"role": "user", "content": prompt.strip()},
        ],
        temperature=0.7,
        max_tokens=400,
    )

    return response.choices[0].message.content.strip()


def is_potentially_good_post(msg: Message) -> bool:
    if not msg.message:
        return False

    text = msg.message.lower()
    keywords = ["₪", "$", "discount", "coupon", "קופון", "דיל", "מבצע", "%", "קוד"]
    if not any(keyword in text for keyword in keywords):
        return False

    if msg.views is not None and msg.views < min_views:
        return False

    return True


def format_message(content: str, product_id: str) -> str:
    return f"{content}\n\n(id:{product_id})"


def log_info(message: str) -> None:
    print(message, flush=True)


async def already_posted(product_id: str) -> bool:
    async for msg in client.iter_messages(tg_target_channel, limit=300):
        if not isinstance(msg, Message) or not msg.message:
            continue
        if f"(id:{product_id})" in msg.message:
            return True
    return False


# ============
# MAIN FLOW
# ============


async def process_channel(channel: str) -> None:
    log_info(f"Scanning source channel: {channel}")

    async for msg in client.iter_messages(channel, limit=max_messages_per_channel):
        if not isinstance(msg, Message) or not msg.message:
            continue

        links = extract_aliexpress_links(msg.message)
        if not links:
            continue

        if not is_potentially_good_post(msg):
            continue

        original_url = links[0]
        product_id = normalize_aliexpress_id(original_url)

        if await already_posted(product_id):
            log_info(f"Already posted product_id={product_id}, skipping")
            continue

        affiliate_url = make_affiliate_link(original_url)

        try:
            new_caption = rewrite_caption(msg.message, affiliate_url)
        except Exception as exc:  # noqa: BLE001
            log_info(f"OpenAI rewrite error: {exc}")
            new_caption = f"{msg.message}\n\n🔗 לינק: {affiliate_url}"

        final_text = format_message(new_caption, product_id)

        if dry_run:
            log_info(
                "DRY_RUN is enabled; skipping send. Would have posted "
                f"product_id={product_id} to {tg_target_channel}"
            )
            continue

        try:
            await client.send_message(tg_target_channel, final_text)
            log_info(f"Posted product_id={product_id} to {tg_target_channel}")
        except Exception as exc:  # noqa: BLE001
            log_info(f"Error sending message to target channel: {exc}")


async def main() -> None:
    for channel in tg_source_channels:
        await process_channel(channel)


if __name__ == "__main__":
    asyncio.run(client.start())
    with client:
        client.loop.run_until_complete(main())
import telethon
print("Telethon version:", telethon.__version__)
