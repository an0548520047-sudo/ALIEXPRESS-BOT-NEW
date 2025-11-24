import asyncio
import os
import re
from datetime import datetime, timezone
from typing import Iterable
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


def _get_list_env(name: str) -> list[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return []
    return [value.strip().lower() for value in raw.split(",") if value.strip()]


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
max_posts_per_run = int(os.getenv("MAX_POSTS_PER_RUN", "5"))
message_cooldown_seconds = int(os.getenv("MESSAGE_COOLDOWN_SECONDS", "5"))
max_message_age_minutes = int(os.getenv("MAX_MESSAGE_AGE_MINUTES", "240"))
keyword_allowlist = _get_list_env("KEYWORD_ALLOWLIST")
keyword_blocklist = _get_list_env("KEYWORD_BLOCKLIST")

client = TelegramClient(StringSession(tg_session), tg_api_id, tg_api_hash)
oa_client = OpenAI(api_key=openai_api_key)
processed_product_ids: set[str] = set()

# ============
# UTILITIES
# ============

ali_regex = re.compile(r"https?://[^\s]*aliexpress\.com[^\s]*", re.IGNORECASE)
url_regex = re.compile(r"https?://\S+")


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


def strip_non_affiliate_links(content: str, affiliate_url: str) -> str:
    """Remove original/extra URLs so only the personal affiliate link remains."""

    def _replacer(match: re.Match[str]) -> str:
        url = match.group(0)
        if affiliate_url in url:
            return affiliate_url
        return ""

    cleaned = url_regex.sub(_replacer, content)
    occurrences = cleaned.count(affiliate_url)
    if occurrences > 1:
        cleaned = cleaned.replace(affiliate_url, "", occurrences - 1)
    return cleaned.strip()


def ensure_affiliate_link(content: str, affiliate_url: str) -> tuple[str, bool]:
    """Guarantee the personal affiliate link appears exactly once.

    Returns the possibly-updated content and whether an append was needed.
    """
    if affiliate_url in content:
        return content, False

    enforced_block = f"👇 לקנייה באליאקספרס:\n{affiliate_url}"

    # If the prompt added the header without the link, attach it to the same block.
    if "👇 לקנייה באליאקספרס" in content:
        return content.rstrip() + f"\n{affiliate_url}", True

    return content.rstrip() + f"\n\n{enforced_block}", True


def _fallback_caption(orig_text: str, affiliate_url: str) -> str:
    """Create a minimal, deterministic caption if the model yields nothing."""
    cleaned = orig_text.strip().splitlines()
    headline = cleaned[0] if cleaned else "מצאתי דיל ששווה להציץ בו"
    bullets = [line for line in cleaned[1:6] if line.strip()][:4]
    bullet_block = (
        "\n".join(f"• {b.strip()}" for b in bullets)
        if bullets
        else "• לפרטים נוספים בקישור"
    )

    base = "\n".join(
        [
            headline,
            "הנה הפרטים בקצרה:",
            bullet_block,
            "👇 לקנייה באליאקספרס:",
            affiliate_url,
        ]
    )
    return base.strip()


def rewrite_caption(orig_text: str, affiliate_url: str) -> str:
    prompt = f"""
אתה כותב פוסט דיל לקבוצת דילים ישראלית (וואטסאפ / טלגרם).
המטרה: פוסט מוכן אחד-לאחד להעתקה.

חוקי סגנון:
- כתיבה רק בעברית, טון יומיומי, ישראלי, קצר, עם טיפה יותר חיים והומור עדין.
- 1–3 אימוג'ים בסך הכול (לא יותר) שמרגישים טבעיים למוצר.
- משפטים קצרים, בלי הפרזות ובלי "הדיל הכי מטורף בעולם".
- לא להמציא מידע שלא קיים במקור.
- אם נתון (מחיר/דירוג/הזמנות/קופונים) לא מופיע במידע – מדלגים עליו.

מבנה מחייב של הפוסט (תמיד לשמור עליו):
1) שורת פתיחה – שאלה יומיומית שמתאימה למוצר (שורה אחת).
2) משפט אחד קצר שמציג את המוצר כפתרון ברור לשאלה.
3) בולטים תכל'ס – 3–6 נקודות, קצרות (עד ~7–9 מילים):
   - סוג/דגם/שימושים/יתרונות/פרטים טכניים חשובים.
4) נתוני מחיר/דירוג/הזמנות – רק אם קיימים במידע:
   - "💰 מחיר אחרי הנחות: <מחיר>" (אפשר גם מחיר $ בסוגריים אם הופיע).
   - "⭐ דירוג: X.X" אם יש.
   - "📦 מס' הזמנות: XXXX+" אם יש.
   - אם כתוב שהמיסים לא כלולים – לציין במשפט קצר.
5) קופונים – רק אם קיימים במידע:
   - שורה: "🎁 קופונים:" ואז רשימה מסודרת; אם צריך סדר שימוש – לציין "קודם X ואז Y".
6) קישור קנייה:
   - שורה: "👇 לקנייה באליאקספרס:"
   - בשורה הבאה: הלינק {affiliate_url}

דגשים:
- להשתמש רק במידע שמופיע בטקסט המקורי של הפוסט (או בלינק אם מוזכר). לא להמציא.
- אל תזכיר שאתה מעתיק או מקבוצה אחרת. לא לציין "אליאקספרס" פרט לשורת הקנייה.
- הומור עדין וקצר, בלי צעקות.
- אל תוסיף קישורים אחרים מלבד הלינק שסיפקתי בשורת הקנייה, ולא לחזור עליו פעמיים.

הנה המידע הגולמי שעליו מסתמך הפוסט (תיאור/מחיר/דירוג/קופונים/קישור וכו'):
---
{orig_text}
---
"""

    response = oa_client.chat.completions.create(
        model=openai_model,
        messages=[
            {
                "role": "system",
                "content": (
                    "אתה כותב קופי בעברית לקבוצת דילים בטלגרם. שמור על מבנה קבוע, לא "
                    "ממציא פרטים, ומשתמש ב-1–3 אימוג'ים בלבד."
                ),
            },
            {"role": "user", "content": prompt.strip()},
        ],
        temperature=0.6,
        max_tokens=500,
    )
    content = response.choices[0].message.content.strip()
    if not content:
        log_info("OpenAI returned empty content; using fallback caption")
        return _fallback_caption(orig_text, affiliate_url)

    return content


def evaluate_post_quality(msg: Message) -> tuple[bool, str | None]:
    if not msg.message:
        return False, "empty message"

    text = msg.message.lower()
    keywords = ["₪", "$", "discount", "coupon", "קופון", "דיל", "מבצע", "%", "קוד"]

    if keyword_blocklist and any(blocked in text for blocked in keyword_blocklist):
        return False, "blocked keyword"

    allow_sources: Iterable[str] = keyword_allowlist if keyword_allowlist else keywords
    if not any(keyword in text for keyword in allow_sources):
        return False, "missing keywords"

    if msg.views is not None and msg.views < min_views:
        return False, "below min views"

    if msg.date:
        message_dt = msg.date
        if message_dt.tzinfo is None:
            message_dt = message_dt.replace(tzinfo=timezone.utc)

        age_minutes = (datetime.now(timezone.utc) - message_dt).total_seconds() / 60
        if age_minutes > max_message_age_minutes:
            return False, "too old"

    return True, None


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


async def process_channel(channel: str) -> tuple[int, dict[str, int]]:
    log_info(f"Scanning source channel: {channel}")

    posted_count = 0
    skip_reasons: dict[str, int] = {}

    def _mark_skip(reason: str) -> None:
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    async for msg in client.iter_messages(channel, limit=max_messages_per_channel):
        if not isinstance(msg, Message) or not msg.message:
            continue

        links = extract_aliexpress_links(msg.message)
        if not links:
            _mark_skip("no aliexpress link")
            continue

        is_good, reason = evaluate_post_quality(msg)
        if not is_good:
            _mark_skip(reason or "unknown")
            log_info(f"Skip message in {channel}: {reason}")
            continue

        original_url = links[0]
        product_id = normalize_aliexpress_id(original_url)

        if product_id in processed_product_ids:
            log_info(f"Already handled product_id={product_id} earlier this run; skipping")
            _mark_skip("duplicate this run")
            continue

        if await already_posted(product_id):
            log_info(f"Already posted product_id={product_id}, skipping")
            processed_product_ids.add(product_id)
            _mark_skip("duplicate previously posted")
            continue

        affiliate_url = make_affiliate_link(original_url)

        source_without_links = strip_non_affiliate_links(msg.message, affiliate_url)
        if source_without_links != msg.message:
            log_info("Stripped original links from source message to enforce personal URL")

        try:
            new_caption = rewrite_caption(source_without_links or msg.message, affiliate_url)
        except Exception as exc:  # noqa: BLE001
            log_info(f"OpenAI rewrite error: {exc}")
            _mark_skip("rewrite error fallback")
            new_caption = f"{source_without_links or msg.message}\n\n🔗 לינק: {affiliate_url}"

        cleaned_caption = strip_non_affiliate_links(new_caption, affiliate_url)
        secured_caption, appended_link = ensure_affiliate_link(cleaned_caption, affiliate_url)
        if appended_link:
            log_info(
                "Affiliate link was missing from the rewritten text; appended personal link"
            )

        final_text = format_message(secured_caption, product_id)

        if dry_run:
            log_info(
                "DRY_RUN is enabled; skipping send. Would have posted "
                f"product_id={product_id} to {tg_target_channel}"
            )
            posted_count += 1
            processed_product_ids.add(product_id)
            if posted_count >= max_posts_per_run:
                log_info(
                    "Reached MAX_POSTS_PER_RUN in DRY_RUN mode; stopping further processing"
                )
                break
            continue

        try:
            await client.send_message(tg_target_channel, final_text)
            log_info(f"Posted product_id={product_id} to {tg_target_channel}")
        except Exception as exc:  # noqa: BLE001
            log_info(f"Error sending message to target channel: {exc}")

        posted_count += 1
        processed_product_ids.add(product_id)
        if posted_count >= max_posts_per_run:
            log_info("Reached MAX_POSTS_PER_RUN; stopping further processing")
            break

        if message_cooldown_seconds > 0:
            await asyncio.sleep(message_cooldown_seconds)

    if skip_reasons:
        details = ", ".join(f"{reason}: {count}" for reason, count in skip_reasons.items())
        log_info(f"Channel {channel} skip summary -> {details}")

    return posted_count, skip_reasons


async def main() -> None:
    total_posted = 0
    total_skip_reasons: dict[str, int] = {}

    for channel in tg_source_channels:
        channel_posted, channel_skips = await process_channel(channel)
        total_posted += channel_posted

        for reason, count in channel_skips.items():
            total_skip_reasons[reason] = total_skip_reasons.get(reason, 0) + count

    if total_skip_reasons:
        summary = ", ".join(
            f"{reason}: {count}" for reason, count in total_skip_reasons.items()
        )
        log_info(f"Overall skip summary -> {summary}")

    log_info(
        "Run completed. Posts sent (including DRY_RUN counts): "
        f"{total_posted}"
    )


if __name__ == "__main__":
    asyncio.run(client.start())
    with client:
        client.loop.run_until_complete(main())
