from __future__ import annotations

import asyncio
import os
import re
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Tuple
from urllib.parse import quote, unquote, urlparse, urlunparse

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
    affiliate_app_key: str | None
    affiliate_app_secret: str | None
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
        affiliate_app_key = _optional_str("ALIEXPRESS_API_APP_KEY")
        affiliate_app_secret = _optional_str("ALIEXPRESS_API_APP_SECRET")

        # Allow running if at least one method is present
        if not (affiliate_api_endpoint or affiliate_portal_template or affiliate_prefix):
            pass

        return cls(
            tg_api_id=int(_require_env("TG_API_ID")),
            tg_api_hash=_require_env("TG_API_HASH"),
            tg_session=_require_env("TG_SESSION"),
            tg_source_channels=tg_source_channels,
            tg_target_channel=_require_env("TG_TARGET_CHANNEL"),
            affiliate_api_endpoint=affiliate_api_endpoint,
            affiliate_app_key=affiliate_app_key,
            affiliate_app_secret=affiliate_app_secret,
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
            message_cooldown_seconds=_float_env("MESSAGE_COOLDOWN_SECONDS", 5.0, allow_zero=True, min_value=0.0),
            max_message_age_minutes=_int_env("MAX_MESSAGE_AGE_MINUTES", 240, min_value=1),
            keyword_allowlist=_list_env("KEYWORD_ALLOWLIST"),
            keyword_blocklist=_list_env("KEYWORD_BLOCKLIST"),
            resolve_redirects=_bool_env("RESOLVE_REDIRECTS", True),
            resolve_redirect_timeout=_float_env("RESOLVE_REDIRECT_TIMEOUT", 4.0, min_value=0.1),
        )

    def describe_affiliate_mode(self) -> str:
        if self.affiliate_api_endpoint:
            return "portal API endpoint (Signed)"
        if self.affiliate_portal_template:
            return "portal template"
        return "prefix-based affiliate link"


# =======================
# Affiliate link pipeline
# =======================


def _canonical_url(url: str) -> str:
    return url.strip().strip("[]()<>.,")


def clean_product_url(url: str) -> str:
    """
    Cleans an AliExpress URL to its bare minimum (item ID) to avoid tracking conflicts.
    Converts: https://www.aliexpress.com/item/12345.html?spm=...
    To: https://www.aliexpress.com/item/12345.html
    """
    try:
        parsed = urlparse(url)
        # Keep only scheme, netloc, and path
        # Remove query params completely to ensure clean redirection
        clean = urlunparse((parsed.scheme, parsed.netloc, parsed.path, '', '', ''))
        
        # Double check if it's an item link
        if "/item/" in clean:
            return clean
        
        # If it's a short link that resolved to something else, return as is but without query
        return clean
    except:
        return url


def resolve_final_url(url: str, *, enabled: bool, timeout_seconds: float) -> str:
    if not enabled:
        return url

    normalized = _canonical_url(url)
    if "s.click.aliexpress.com" not in normalized.lower() and "bit.ly" not in normalized.lower():
        return normalized

    try:
        timeout = httpx.Timeout(timeout_seconds, connect=timeout_seconds, read=timeout_seconds, write=timeout_seconds)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(normalized)
        if response.history and response.url:
            final_url = str(response.url)
            log_info(f"Resolved short link to final URL")
            return final_url
    except Exception as exc:
        log_info(f"Redirect resolution failed; using original URL: {exc}")

    return normalized


def ensure_affiliate_link(content: str, affiliate_url: str) -> Tuple[str, bool]:
    normalized = _canonical_url(affiliate_url)
    
    # Check if the link is already there
    if normalized in content:
        return content, False

    # Check for common "Buy Now" phrases to avoid duplication
    phrases = ["👇 לקנייה באליאקספרס:", "לקנייה:", "לינק:", "קישור:", "Link:", "Buy:"]
    
    lines = content.split('\n')
    cleaned_lines = []
    found_phrase = False
    
    for line in lines:
        # If we find a line that looks like a buy header, we skip it (we will add our own)
        if any(p in line for p in phrases):
            continue
        cleaned_lines.append(line)
    
    clean_content = '\n'.join(cleaned_lines).strip()
    
    enforced_block = f"👇 לקנייה באליאקספרס:\n{normalized}"
    return f"{clean_content}\n\n{enforced_block}", True


class AffiliateLinkBuilder:
    def __init__(self, config: Config):
        self.config = config

    def _sign_params(self, params: Dict[str, str]) -> str:
        if not self.config.affiliate_app_secret:
            return ""
        sorted_keys = sorted(params.keys())
        param_str = ""
        for key in sorted_keys:
            param_str += f"{key}{params[key]}"
        sign_source = f"{self.config.affiliate_app_secret}{param_str}{self.config.affiliate_app_secret}"
        return hashlib.md5(sign_source.encode("utf-8")).hexdigest().upper()

    def _from_api(self, original_url: str) -> str | None:
        if not self.config.affiliate_api_endpoint or not self.config.affiliate_app_key:
            return None

        timestamp = str(int(time.time() * 1000))
        params = {
            "app_key": self.config.affiliate_app_key,
            "timestamp": timestamp,
            "sign_method": "md5",
            "urls": original_url,
            "promotion_link_type": "0",
            "tracking_id": "default",
            "format": "json",
            "v": "2.0"
        }
        params["sign"] = self._sign_params(params)
        headers = {"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
        
        try:
            with httpx.Client(timeout=self.config.affiliate_api_timeout) as http_client:
                response = http_client.post(self.config.affiliate_api_endpoint, data=params, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            if "aliexpress_affiliate_link_generate_response" in data:
                resp_body = data["aliexpress_affiliate_link_generate_response"].get("resp_result", {}).get("result", {})
                promotions = resp_body.get("promotion_links", {}).get("promotion_link", [])
                if promotions:
                    return _canonical_url(promotions[0].get("promotion_link"))
            
            candidates = [
                data.get("result", {}).get("promotion_links", [{}])[0].get("promotion_link") if data.get("result") else None,
                data.get("promotion_link")
            ]
            for cand in candidates:
                if cand: return _canonical_url(cand)
                
        except Exception as exc:
            log_info(f"API call failed: {exc}")
            return None
        return None

    def _from_portal_template(self, clean_url: str) -> str | None:
        if not self.config.affiliate_portal_template:
            return None
        
        # We encode the CLEAN URL only
        encoded = quote(clean_url, safe="")
        template = self.config.affiliate_portal_template
        
        if "{url}" in template:
            return template.replace("{url}", encoded).strip()
        return template.strip()

    def build(self, original_url: str) -> str:
        # 1. Resolve Redirects (e.g. bit.ly or s.click)
        resolved = resolve_final_url(original_url, enabled=self.config.resolve_redirects, timeout_seconds=self.config.resolve_redirect_timeout)
        
        # 2. Clean the URL (Remove all tracking params from the source!)
        cleaned = clean_product_url(resolved)
        
        # 3. Try API
        api_link = self._from_api(cleaned)
        if api_link:
            log_info(f"Using affiliate link from API")
            return api_link

        # 4. Try Portal Template (This is what you are using now)
        portal_link = self._from_portal_template(cleaned)
        if portal_link:
            log_info(f"Using affiliate link from portal template")
            return portal_link

        return cleaned


# ===============
# Caption creator
# ===============


def extract_fact_hints(text: str) -> Dict[str, str]:
    hints: Dict[str, str] = {}
    price_match = re.search(r"(₪|\$)\s?\d+[\d.,]*", text)
    if price_match: hints["price"] = price_match.group(0)
    rating_match = re.search(r"(?:⭐|rating[:\s]*)(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if rating_match: hints["rating"] = rating_match.group(1)
    orders_match = re.search(r"(\d[\d.,]*\+?)\s*(?:orders|הזמנות|sold)", text, re.IGNORECASE)
    if orders_match: hints["orders"] = orders_match.group(1)
    coupon_matches = re.findall(r"(?:קופון|coupon|code)[:\s]*([A-Za-z0-9-]+)", text, re.IGNORECASE)
    if coupon_matches: hints["coupons"] = ", ".join(dict.fromkeys(coupon_matches))
    return hints


def _fallback_caption(orig_text: str, affiliate_url: str) -> str:
    cleaned = orig_text.strip().splitlines()
    headline = cleaned[0] if cleaned else "מצאתי דיל ששווה להציץ בו"
    return f"{headline}\n\n👇 לקנייה באליאקספרס:\n{affiliate_url}"


class CaptionWriter:
    def __init__(self, openai_client: OpenAI, config: Config):
        self.client = openai_client
        self.model = config.openai_model

    def write(self, orig_text: str, affiliate_url: str) -> str:
        hints = extract_fact_hints(orig_text)
        hints_str = "\n".join([f"- {k}: {v}" for k, v in hints.items()])
        
        # Note: We instruct OpenAI NOT to include the link, we will add it safely later
        prompt = f"""
תכתוב פוסט דיל קצר וקולע לטלגרם בעברית על בסיס הטקסט למטה.
סגנון: חברי, קצר, אימוג'י אחד או שניים.
מבנה:
1. כותרת מושכת.
2. 2-3 בולטים על המוצר.
3. מחיר וקופונים אם יש.
אל תכתוב "לקנייה" ואל תוסיף קישור - אני אוסיף את זה לבד.

מידע:
{orig_text}
{hints_str}
"""
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt.strip()}],
                temperature=0.6,
                max_tokens=400,
            )
            return response.choices[0].message.content.strip()
        except:
            return _fallback_caption(orig_text, "")


# ===================
# Deal bot main logic
# ===================


ali_regex = re.compile(r"https?://[^\s]*aliexpress\.com[^\s]*", re.IGNORECASE)


def extract_aliexpress_links(text: str) -> List[str]:
    if not text: return []
    return ali_regex.findall(text)


def normalize_aliexpress_id(url: str) -> str:
    clean = _canonical_url(url)
    match = re.search(r"/item/(\d+)\.html", clean)
    return match.group(1) if match else "unknown"


def format_message(content: str, product_id: str) -> str:
    # Clean invisible chars usually added by telegram
    return content.replace(f"(id:{product_id})", "").strip() + f"\n\n(id:{product_id})"


def log_info(message: str) -> None:
    print(message, flush=True)


class DealBot:
    def __init__(self, client: TelegramClient, caption_writer: CaptionWriter, affiliate_builder: AffiliateLinkBuilder, config: Config):
        self.client = client
        self.caption_writer = caption_writer
        self.affiliate_builder = affiliate_builder
        self.config = config
        self.processed_ids = set()

    async def run(self):
        log_info("Starting bot...")
        for channel in self.config.tg_source_channels:
            async for msg in self.client.iter_messages(channel, limit=self.config.max_messages_per_channel):
                if not msg.message: continue
                
                links = extract_aliexpress_links(msg.message)
                if not links: continue
                
                # Simple logic: Take first link, build affiliate, write caption, post
                original_url = links[0]
                
                # Resolve & Build Affiliate Link
                affiliate_url = self.affiliate_builder.build(original_url)
                
                # Extract ID for dup check
                prod_id = normalize_aliexpress_id(original_url)
                if prod_id in self.processed_ids: continue
                
                # Write Caption (Without Link)
                caption = self.caption_writer.write(msg.message, affiliate_url)
                
                # Combine Caption + Link Safely
                final_text, _ = ensure_affiliate_link(caption, affiliate_url)
                final_text = format_message(final_text, prod_id)
                
                # Post
                try:
                    if msg.media:
                        await self.client.send_file(self.config.tg_target_channel, msg.media, caption=final_text)
                    else:
                        await self.client.send_message(self.config.tg_target_channel, final_text)
                    
                    log_info(f"Posted {prod_id}")
                    self.processed_ids.add(prod_id)
                    
                    if len(self.processed_ids) >= self.config.max_posts_per_run:
                        return
                        
                except Exception as e:
                    log_info(f"Error posting: {e}")


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
