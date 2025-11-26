from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Tuple
from urllib.parse import quote, unquote

import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.custom.message import Message


# ==================
# Config and helpers
# ==================


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _list_env(name: str) -> List[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return []
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _optional_str(name: str) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    cleaned = raw.strip()
    return cleaned or None


def _int_env(name: str, default: int, *, allow_zero: bool = False, min_value: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default

    try:
        value = int(raw)
    except ValueError:
        print(
            f"Warning: {name} must be an integer; got {raw!r}. "
            f"Falling back to default={default}",
            flush=True,
        )
        return default

    if min_value is not None and value < min_value:
        print(
            f"Warning: {name} must be >= {min_value}; got {value!r}. "
            f"Falling back to default={default}",
            flush=True,
        )
        return default

    if value == 0 and not allow_zero:
        print(
            f"Warning: {name} must be positive; got 0. "
            f"Falling back to default={default}",
            flush=True,
        )
        return default

    return value


def _float_env(
    name: str,
    default: float,
    *,
    allow_zero: bool = False,
    min_value: float | None = None,
) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default

    try:
        value = float(raw)
    except ValueError:
        print(
            f"Warning: {name} must be a number; got {raw!r}. "
            f"Falling back to default={default}",
            flush=True,
        )
        return default

    if min_value is not None and value < min_value:
        print(
            f"Warning: {name} must be >= {min_value}; got {value!r}. "
            f"Falling back to default={default}",
            flush=True,
        )
        return default

    if value == 0 and not allow_zero:
        print(
            f"Warning: {name} must be positive; got 0. "
            f"Falling back to default={default}",
            flush=True,
        )
        return default

    return value


@dataclass
class Config:
    tg_api_id: int
    tg_api_hash: str
    tg_session: str
    tg_source_channels: List[str]
    tg_target_channel: str
    affiliate_api_endpoint: str | None
    affiliate_api_token: str | None
    affiliate_api_timeout: float
    affiliate_portal_template: str | None
    affiliate_prefix: str | None
    openai_api_key: str
    openai_model: str
    min_views: int
    max_messages_per_channel: int
    dry_run: bool
    require_keywords: bool
    max_posts_per_run: int
    message_cooldown_seconds: float
    max_message_age_minutes: int
    keyword_allowlist: List[str]
    keyword_blocklist: List[str]
    resolve_redirects: bool
    resolve_redirect_timeout: float

    @classmethod
    def from_env(cls) -> "Config":
        tg_source_channels = [c.strip() for c in _require_env("TG_SOURCE_CHANNELS").split(",") if c.strip()]
        if not tg_source_channels:
            raise RuntimeError("TG_SOURCE_CHANNELS is set but empty after parsing")

        affiliate_api_endpoint = _optional_str("AFFILIATE_API_ENDPOINT")
        affiliate_portal_template = _optional_str("AFFILIATE_PORTAL_LINK")
        affiliate_prefix = _optional_str("AFFILIATE_PREFIX")

        if not (affiliate_api_endpoint or affiliate_portal_template or affiliate_prefix):
            raise RuntimeError(
                "You must configure an affiliate link source: AFFILIATE_API_ENDPOINT (preferred), "
                "AFFILIATE_PORTAL_LINK, or AFFILIATE_PREFIX"
            )

        return cls(
            tg_api_id=int(_require_env("TG_API_ID")),
            tg_api_hash=_require_env("TG_API_HASH"),
            tg_session=_require_env("TG_SESSION"),
            tg_source_channels=tg_source_channels,
            tg_target_channel=_require_env("TG_TARGET_CHANNEL"),
            affiliate_api_endpoint=affiliate_api_endpoint,
            affiliate_api_token=(os.getenv("AFFILIATE_API_TOKEN") or "").strip() or None,
            affiliate_api_timeout=_float_env("AFFILIATE_API_TIMEOUT", 5.0, min_value=1e-6),
            affiliate_portal_template=affiliate_portal_template,
            affiliate_prefix=affiliate_prefix,
            openai_api_key=_require_env("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini"),
            min_views=_int_env("MIN_VIEWS", 1500, allow_zero=True, min_value=0),
            max_messages_per_channel=_int_env("MAX_MESSAGES_PER_CHANNEL", 80, min_value=1),
            dry_run=_bool_env("DRY_RUN", False),
            require_keywords=_bool_env("REQUIRE_KEYWORDS", False),
            max_posts_per_run=_int_env("MAX_POSTS_PER_RUN", 5, min_value=1),
            message_cooldown_seconds=_float_env(
                "MESSAGE_COOLDOWN_SECONDS", 5.0, allow_zero=True, min_value=0.0
            ),
            max_message_age_minutes=_int_env("MAX_MESSAGE_AGE_MINUTES", 240, min_value=1),
            keyword_allowlist=_list_env("KEYWORD_ALLOWLIST"),
            keyword_blocklist=_list_env("KEYWORD_BLOCKLIST"),
            resolve_redirects=_bool_env("RESOLVE_REDIRECTS", True),
            resolve_redirect_timeout=_float_env("RESOLVE_REDIRECT_TIMEOUT", 4.0, min_value=0.1),
        )

    def describe_affiliate_mode(self) -> str:
        if self.affiliate_api_endpoint:
            return "portal API endpoint"
        if self.affiliate_portal_template:
            if "{url}" in self.affiliate_portal_template:
                return "portal template with {url} placeholder"
            return "portal template (verbatim link)"
        return "prefix-based affiliate link"


# =======================
# Affiliate link pipeline
# =======================


def _canonical_url(url: str) -> str:
    return url.strip().strip("[]()<>.,")


def resolve_final_url(url: str, *, enabled: bool, timeout_seconds: float) -> str:
    """Follow redirects for AliExpress short links to capture the real product URL.

    When disabled or failing, returns the original URL so the run can proceed.
    """
    if not enabled:
        return url

    normalized = _canonical_url(url)
    if "s.click.aliexpress.com" not in normalized.lower():
        return normalized

    try:
        timeout = httpx.Timeout(timeout_seconds, connect=timeout_seconds, read=timeout_seconds, write=timeout_seconds)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(normalized)
        if response.history and response.url:
            final_url = str(response.url)
            log_info(f"Resolved short link to final URL (ending: {final_url[-12:]})")
            return final_url
    except Exception as exc:  # noqa: BLE001
        log_info(f"Redirect resolution failed; using original URL: {exc}")

    return normalized


def _strip_urls_with_affiliate(content: str, affiliate_url: str, append_if_missing: bool) -> Tuple[str, bool]:
    url_regex = re.compile(r"https?://\S+")
    encoded_url_regex = re.compile(r"https?%3A%2F%2F\S+", re.IGNORECASE)

    normalized_aff = _canonical_url(affiliate_url)
    seen_affiliate = False

    def _replacer(match: re.Match[str]) -> str:
        nonlocal seen_affiliate
        decoded = _canonical_url(unquote(match.group(0)))
        if normalized_aff in decoded and not seen_affiliate:
            seen_affiliate = True
            return normalized_aff
        return ""

    cleaned = content
    for pattern in (url_regex, encoded_url_regex):
        cleaned = pattern.sub(_replacer, cleaned)

    if append_if_missing and not seen_affiliate:
        cleaned = f"{cleaned.rstrip()}\n\n👇 לקנייה באליאקספרס:\n{normalized_aff}".strip()
        seen_affiliate = True

    occurrences = cleaned.count(normalized_aff)
    if occurrences > 1:
        cleaned = cleaned.replace(normalized_aff, "", occurrences - 1)

    return cleaned.strip(), seen_affiliate


def strip_non_affiliate_links(content: str, affiliate_url: str) -> str:
    cleaned, _ = _strip_urls_with_affiliate(content, affiliate_url, append_if_missing=False)
    return cleaned


def ensure_affiliate_link(content: str, affiliate_url: str) -> Tuple[str, bool]:
    normalized = _canonical_url(affiliate_url)
    if normalized in content:
        return content, False

    if "👇 לקנייה באליאקספרס" in content:
        return content.rstrip() + f"\n{normalized}", True

    enforced_block = f"👇 לקנייה באליאקספרס:\n{normalized}"
    return content.rstrip() + f"\n\n{enforced_block}", True


def enforce_single_affiliate_link(content: str, affiliate_url: str) -> str:
    cleaned, seen = _strip_urls_with_affiliate(content, affiliate_url, append_if_missing=False)
    if not seen:
        cleaned, _ = _strip_urls_with_affiliate(cleaned, affiliate_url, append_if_missing=True)
    return cleaned


class AffiliateLinkBuilder:
    def __init__(self, config: Config):
        self.config = config

    def _from_api(self, original_url: str) -> str | None:
        if not self.config.affiliate_api_endpoint:
            return None

        headers = {"Content-Type": "application/json"}
        if self.config.affiliate_api_token:
            headers["Authorization"] = f"Bearer {self.config.affiliate_api_token}"

        payload = {"url": original_url}
        try:
            timeout = httpx.Timeout(
                self.config.affiliate_api_timeout,
                connect=self.config.affiliate_api_timeout,
                read=self.config.affiliate_api_timeout,
                write=self.config.affiliate_api_timeout,
            )
            log_info(
                "Calling affiliate API with timeout="
                f"{self.config.affiliate_api_timeout}s"
            )
            with httpx.Client(timeout=timeout) as http_client:
                response = http_client.post(
                    self.config.affiliate_api_endpoint, json=payload, headers=headers
                )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log_info(f"Affiliate API call failed, falling back: {exc}")
            return None

        try:
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            log_info(f"Could not parse affiliate API JSON response: {exc}")
            return None

        candidates = [
            data.get("affiliate_link") if isinstance(data, dict) else None,
            data.get("promotion_link") if isinstance(data, dict) else None,
            data.get("data", {}).get("affiliate_link") if isinstance(data, dict) else None,
            data.get("data", {}).get("promotion_link") if isinstance(data, dict) else None,
            data.get("data", {}).get("link") if isinstance(data, dict) else None,
            data.get("result", {}).get("promotion_link") if isinstance(data, dict) else None,
        ]

        for candidate in candidates:
            if isinstance(candidate, str) and candidate.strip():
                return _canonical_url(candidate)

        log_info("Affiliate API response missing link fields; falling back to template/prefix")
        return None

    def _from_portal_template(self, encoded_url: str) -> str | None:
        if not self.config.affiliate_portal_template:
            return None
        template = self.config.affiliate_portal_template
        if "{url}" in template:
            return template.replace("{url}", encoded_url).strip()
        return template.strip()

    def _from_prefix(self, encoded_url: str) -> str | None:
        if not self.config.affiliate_prefix:
            return None
        return f"{self.config.affiliate_prefix}{encoded_url}"

    def build(self, original_url: str) -> str:
        cleaned = _canonical_url(unquote(original_url))
        encoded = quote(cleaned, safe="")

        api_link = self._from_api(cleaned)
        if api_link:
            log_info(f"Using affiliate link from API (ending: {api_link[-8:]})")
            return api_link

        portal_link = self._from_portal_template(encoded)
        if portal_link:
            log_info(f"Using affiliate link from portal template (ending: {portal_link[-8:]})")
            return portal_link

        prefix_link = self._from_prefix(encoded)
        if prefix_link:
            log_info(f"Using affiliate link from prefix (ending: {prefix_link[-8:]})")
            return prefix_link

        raise RuntimeError("Failed to build affiliate link; check your configuration")


# ===============
# Caption creator
# ===============


def extract_fact_hints(text: str) -> Dict[str, str]:
    hints: Dict[str, str] = {}

    price_match = re.search(r"(₪|\$)\s?\d+[\d.,]*", text)
    if price_match:
        hints["price"] = price_match.group(0)

    rating_match = re.search(r"(?:⭐|rating[:\s]*)(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if rating_match:
        hints["rating"] = rating_match.group(1)

    orders_match = re.search(r"(\d[\d.,]*\+?)\s*(?:orders|הזמנות|sold)", text, re.IGNORECASE)
    if orders_match:
        hints["orders"] = orders_match.group(1)

    coupon_matches = re.findall(r"(?:קופון|coupon|code)[:\s]*([A-Za-z0-9-]+)", text, re.IGNORECASE)
    if coupon_matches:
        hints["coupons"] = ", ".join(dict.fromkeys(coupon_matches))

    return hints


def _fallback_caption(orig_text: str, affiliate_url: str) -> str:
    cleaned = orig_text.strip().splitlines()
    headline = cleaned[0] if cleaned else "מצאתי דיל ששווה להציץ בו"
    bullets = [line for line in cleaned[1:6] if line.strip()][:4]
    bullet_block = "\n".join(f"• {b.strip()}" for b in bullets) if bullets else "• לפרטים נוספים בקישור"

    return "\n".join(
        [
            headline,
            "הנה הפרטים בקצרה:",
            bullet_block,
            "👇 לקנייה באליאקספרס:",
            _canonical_url(affiliate_url),
        ]
    ).strip()


class CaptionWriter:
    def __init__(self, openai_client: OpenAI, config: Config):
        self.client = openai_client
        self.model = config.openai_model

    def _build_prompt(self, orig_text: str, affiliate_url: str) -> str:
        hints = extract_fact_hints(orig_text)
        if hints:
            hints_lines = ["נתונים שזוהו בטקסט:", *(f"- {k}: {v}" for k, v in hints.items())]
            hints_block = "\n".join(hints_lines)
        else:
            hints_block = "לא נמצאו נתוני מחיר/דירוג/הזמנות/קופונים בטקסט."

        return f"""
אתה כותב פוסט דיל לקבוצת דילים ישראלית (וואטסאפ / טלגרם).
הפוסט צריך לצאת מוכן אחד-לאחד להדבקה.

חוקי סגנון:
- עברית בלבד, טון יומיומי וחי, עם הומור עדין וקצר.
- 1–3 אימוג'ים בסך הכול, לא יותר.
- משפטים קצרים, בלי מנופח ובלי "הדיל הכי מטורף בעולם".
- לא להמציא מידע שלא קיים במקור.

מבנה מחייב:
1) שורת פתיחה – שאלה יומיומית שמתאימה למוצר (שורה אחת).
2) משפט אחד קצר שמציג את המוצר כפתרון ברור לשאלה.
3) בולטים תכל'ס – 3–6 נקודות קצרות (עד ~7–9 מילים): סוג/דגם, יתרונות אמיתיים, שימושים, פרטים טכניים חשובים.
4) מחיר/דירוג/הזמנות – רק אם קיימים במידע: 
   • "💰 מחיר אחרי הנחות: <מחיר>" (אפשר ש"ח ו-$ אם הופיע).
   • "⭐ דירוג: X.X" אם יש.
   • "📦 מס' הזמנות: XXXX+" אם יש.
   • אם מצוין שהמיסים לא כלולים – לרשום במשפט קצר.
5) קופונים – רק אם קיימים במידע: שורה "🎁 קופונים:" ואז רשימה מסודרת; אם יש סדר שימוש – לציין "קודם X ואז Y".
6) קישור קנייה: שורה "👇 לקנייה באליאקספרס:" ואז בשורה הבאה הלינק {affiliate_url}.

דגשים:
- להשתמש רק במה שמופיע במידע המקורי. לא להוסיף קישורים אחרים, לא לחזור על הלינק יותר מפעם אחת.
- לא להזכיר שזה הועתק מקבוצה אחרת. לא להגזים, טון טבעי.

{hints_block}

המידע הגולמי (תיאור/מחיר/דירוג/קופונים/קישור וכו'):
---
{orig_text}
---
"""

    def write(self, orig_text: str, affiliate_url: str) -> str:
        prompt = self._build_prompt(orig_text, affiliate_url)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "אתה כותב קופי בעברית לקבוצת דילים בטלגרם. שמור על מבנה קבוע, "
                        "טון חי וענייני, ואל תמציא פרטים או קישורים נוספים."
                    ),
                },
                {"role": "user", "content": prompt.strip()},
            ],
            temperature=0.6,
            max_tokens=500,
        )
        content = response.choices[0].message.content or ""
        if not content.strip():
            log_info("OpenAI returned empty content; using fallback caption")
            return _fallback_caption(orig_text, affiliate_url)

        return content.strip()


# ===================
# Deal bot main logic
# ===================


ali_regex = re.compile(r"https?://[^\s]*aliexpress\.com[^\s]*", re.IGNORECASE)


def extract_aliexpress_links(text: str) -> List[str]:
    if not text:
        return []
    return ali_regex.findall(text)


def normalize_aliexpress_id(url: str) -> str:
    normalized_url = _canonical_url(url)

    click_match = re.search(
        r"s\.click\.aliexpress\.com/(?:e|aw)/(_?[A-Za-z0-9]+)", normalized_url,
        re.IGNORECASE,
    )
    if click_match:
        return click_match.group(1).lstrip("_")

    match = re.search(r"/item/(\d+)\.html", normalized_url)
    if match:
        return match.group(1)

    match = re.search(r"/(\d+)\.html", normalized_url)
    if match:
        return match.group(1)

    return normalized_url.split("?")[0]


def evaluate_post_quality(
    msg: Message,
    *,
    min_views: int,
    keyword_blocklist: Iterable[str],
    keyword_allowlist: Iterable[str],
    require_keywords: bool,
    max_message_age_minutes: int,
) -> Tuple[bool, str | None]:
    if not msg.message:
        return False, "empty message"

    text = msg.message.lower()
    keywords = ["₪", "$", "discount", "coupon", "קופון", "דיל", "מבצע", "%", "קוד"]

    if keyword_blocklist and any(blocked in text for blocked in keyword_blocklist):
        return False, "blocked keyword"

    allow_sources: Iterable[str] = ()
    missing_reason = None

    if keyword_allowlist:
        allow_sources = keyword_allowlist
        missing_reason = "missing allowlist keywords"
    elif require_keywords:
        allow_sources = keywords
        missing_reason = "missing keywords"

    if allow_sources and not any(keyword in text for keyword in allow_sources):
        return False, missing_reason

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


class DealBot:
    def __init__(
        self,
        client: TelegramClient,
        caption_writer: CaptionWriter,
        affiliate_builder: AffiliateLinkBuilder,
        config: Config,
    ) -> None:
        self.client = client
        self.caption_writer = caption_writer
        self.affiliate_builder = affiliate_builder
        self.config = config
        self.processed_product_ids: set[str] = set()

    async def already_posted(self, product_id: str) -> bool:
        async for msg in self.client.iter_messages(self.config.tg_target_channel, limit=300):
            if not isinstance(msg, Message) or not msg.message:
                continue
            if f"(id:{product_id})" in msg.message:
                return True
        return False

    async def process_channel(self, channel: str) -> Tuple[int, Dict[str, int]]:
        log_info(f"Scanning source channel: {channel}")

        scanned_messages = 0
        eligible_candidates = 0
        posted_count = 0
        skip_reasons: Dict[str, int] = {}

        def _mark_skip(reason: str) -> None:
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

        async for msg in self.client.iter_messages(channel, limit=self.config.max_messages_per_channel):
            scanned_messages += 1
            if not isinstance(msg, Message) or not msg.message:
                continue

            links = extract_aliexpress_links(msg.message)
            if not links:
                _mark_skip("no aliexpress link")
                continue

            is_good, reason = evaluate_post_quality(
                msg,
                min_views=self.config.min_views,
                keyword_blocklist=self.config.keyword_blocklist,
                keyword_allowlist=self.config.keyword_allowlist,
                require_keywords=self.config.require_keywords,
                max_message_age_minutes=self.config.max_message_age_minutes,
            )
            if not is_good:
                _mark_skip(reason or "unknown")
                log_info(f"Skip message in {channel}: {reason}")
                continue

            original_url = links[0]
            resolved_url = resolve_final_url(
                original_url,
                enabled=self.config.resolve_redirects,
                timeout_seconds=self.config.resolve_redirect_timeout,
            )
            if resolved_url != _canonical_url(original_url):
                log_info("Expanded short link to capture the real product URL before affiliation")

            product_id = normalize_aliexpress_id(resolved_url)
            eligible_candidates += 1

            if product_id in self.processed_product_ids:
                log_info(f"Already handled product_id={product_id} earlier this run; skipping")
                _mark_skip("duplicate this run")
                continue

            if await self.already_posted(product_id):
                log_info(f"Already posted product_id={product_id}, skipping")
                self.processed_product_ids.add(product_id)
                _mark_skip("duplicate previously posted")
                continue

            affiliate_url = self.affiliate_builder.build(resolved_url)

            source_without_links = strip_non_affiliate_links(msg.message, affiliate_url)
            if source_without_links != msg.message:
                log_info("Stripped original links from source message to enforce personal URL")

            try:
                new_caption = self.caption_writer.write(source_without_links or msg.message, affiliate_url)
            except Exception as exc:  # noqa: BLE001
                log_info(f"OpenAI rewrite error: {exc}")
                _mark_skip("rewrite error fallback")
                new_caption = f"{source_without_links or msg.message}\n\n👇 לקנייה באליאקספרס:\n{affiliate_url}"

            cleaned_caption = strip_non_affiliate_links(new_caption, affiliate_url)
            secured_caption, appended = ensure_affiliate_link(cleaned_caption, affiliate_url)
            if appended:
                log_info("Affiliate link was missing from the rewritten text; appended personal link")

            final_caption = enforce_single_affiliate_link(secured_caption, affiliate_url)
            if final_caption != secured_caption:
                log_info("Cleaned extra URLs to keep only the personal affiliate link once")

            final_text = format_message(final_caption, product_id)

            send_success = False
            media = getattr(msg, "media", None)

            if self.config.dry_run:
                log_info(
                    "DRY_RUN is enabled; skipping send. Would have posted "
                    f"product_id={product_id} to {self.config.tg_target_channel} "
                    f"with media={'yes' if media else 'no'}"
                )
                send_success = True
            else:
                if media is not None:
                    try:
                        await self.client.send_file(
                            self.config.tg_target_channel,
                            media,
                            caption=final_text,
                        )
                        send_success = True
                        log_info(
                            f"Posted product_id={product_id} to {self.config.tg_target_channel} "
                            "with source media attached"
                        )
                    except Exception as exc:  # noqa: BLE001
                        log_info(
                            "Sending with media failed; retrying without attachment: " f"{exc}"
                        )

                if not send_success:
                    try:
                        await self.client.send_message(self.config.tg_target_channel, final_text)
                        send_success = True
                        log_info(
                            f"Posted product_id={product_id} to {self.config.tg_target_channel} "
                            "(no media)"
                        )
                    except Exception as exc:  # noqa: BLE001
                        log_info(
                            "Error sending message to target channel; will not count as sent: "
                            f"{exc}"
                        )

            if send_success:
                posted_count += 1
                self.processed_product_ids.add(product_id)

                if posted_count >= self.config.max_posts_per_run:
                    log_info("Reached MAX_POSTS_PER_RUN; stopping further processing")
                    break

                if self.config.message_cooldown_seconds > 0:
                    await asyncio.sleep(self.config.message_cooldown_seconds)

        details = ", ".join(f"{reason}: {count}" for reason, count in skip_reasons.items()) or "none"
        log_info(
            "Channel {channel} summary -> scanned={scanned}, candidates={candidates}, posted={posted}, skips={skips}".format(
                channel=channel,
                scanned=scanned_messages,
                candidates=eligible_candidates,
                posted=posted_count,
                skips=details,
            )
        )

        return posted_count, skip_reasons

    async def run(self) -> None:
        log_info(
            "Starting run with "
            f"dry_run={self.config.dry_run}, sources={len(self.config.tg_source_channels)}, "
            f"target={self.config.tg_target_channel}, affiliate_mode={self.config.describe_affiliate_mode()}, "
            f"max_posts_per_run={self.config.max_posts_per_run}, require_keywords={self.config.require_keywords}, "
            f"min_views={self.config.min_views}, max_message_age_minutes={self.config.max_message_age_minutes}"
        )

        total_posted = 0
        total_skip_reasons: Dict[str, int] = {}

        for channel in self.config.tg_source_channels:
            channel_posted, channel_skips = await self.process_channel(channel)
            total_posted += channel_posted

            for reason, count in channel_skips.items():
                total_skip_reasons[reason] = total_skip_reasons.get(reason, 0) + count

        if total_skip_reasons:
            summary = ", ".join(f"{reason}: {count}" for reason, count in total_skip_reasons.items())
            log_info(f"Overall skip summary -> {summary}")

        if total_posted == 0:
            log_info(
                "No posts were sent this run. Check skip summary and consider lowering "
                "filters (MIN_VIEWS, REQUIRE_KEYWORDS) or disabling DRY_RUN for real posting."
            )

        log_info(
            "Run completed. Posts sent (including DRY_RUN counts): "
            f"{total_posted}"
        )


async def main() -> None:
    config = Config.from_env()
    client = TelegramClient(StringSession(config.tg_session), config.tg_api_id, config.tg_api_hash)
    oa_client = OpenAI(api_key=config.openai_api_key)

    bot = DealBot(
        client=client,
        caption_writer=CaptionWriter(oa_client, config),
        affiliate_builder=AffiliateLinkBuilder(config),
        config=config,
    )

    await client.start()
    async with client:
        await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
