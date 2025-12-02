from __future__ import annotations

import asyncio
import os
import re
import time
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Tuple
from urllib.parse import quote, urlparse, urlunparse

import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession

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
    if raw is None: return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}

def _list_env(name: str) -> List[str]:
    raw = os.getenv(name)
    if raw is None or not raw.strip(): return []
    return [item.strip().lower() for item in raw.split(",") if item.strip()]

def _optional_str(name: str) -> str | None:
    raw = os.getenv(name)
    return raw.strip() if raw and raw.strip() else None

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
            f"Warning: {name} must be a float; got {raw!r}. "
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

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default

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
    affiliate_prefix_encode: bool
    
    openai_api_key: str
    openai_model: str
    max_messages_per_channel: int
    max_posts_per_run: int
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
        affiliate_prefix_encode = _bool_env("AFFILIATE_PREFIX_ENCODE", True)
        affiliate_app_key = _optional_str("ALIEXPRESS_API_APP_KEY")
        affiliate_app_secret = _optional_str("ALIEXPRESS_API_APP_SECRET")

        # Warn if no affiliate method is configured (but don't crash)
        if not (affiliate_api_endpoint or affiliate_portal_template or affiliate_prefix):
            print("Warning: No affiliate method configured (API, Portal, or Prefix).")

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
            affiliate_prefix_encode=affiliate_prefix_encode,
            openai_api_key=_require_env("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            max_messages_per_channel=_int_env("MAX_MESSAGES_PER_CHANNEL", 50),
            max_posts_per_run=_int_env("MAX_POSTS_PER_RUN", 10),
            resolve_redirects=_bool_env("RESOLVE_REDIRECTS", True),
            resolve_redirect_timeout=_float_env("RESOLVE_REDIRECT_TIMEOUT", 10.0),
        )

# =======================
# Core Logic: URL Cleaning
# =======================

def _canonical_url(url: str) -> str:
    return url.strip().strip("[]()<>.,")

def resolve_url_if_needed(url: str, timeout: float = 10.0) -> str:
    """
    Expands shortened links (bit.ly, s.click) to get the real URL.
    """
    url = _canonical_url(url)
    # Domains that MUST be resolved
    triggers = ["s.click", "bit.ly", "a.aliexpress", "/share/", "short"]
    
    if not any(t in url.lower() for t in triggers):
        return url

    print(f"Resolving redirect for: {url}")
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url)
            if resp.url:
                final = str(resp.url)
                # Fix mobile links to desktop
                return final.replace("m.aliexpress", "www.aliexpress")
    except Exception as e:
        print(f"Error resolving URL: {e}")
    
    return url

def extract_item_id_and_clean(url: str) -> str | None:
    """
    Aggressive cleaner: Finds the ID and rebuilds a fresh URL.
    Fixes the 'Homepage Redirect' issue.
    """
    # 1. Try to find ID in the pattern /item/12345.html
    match = re.search(r"/item/(\d+)\.html", url)
    
    # 2. If not found, look for simply numbers (fallback for some formats)
    if not match:
        match = re.search(r"(\d{10,})", url)
        
    if match:
        item_id = match.group(1)
        # Return a sterile, perfect URL
        return f"https://www.aliexpress.com/item/{item_id}.html"
    
    return None

# =======================
# Affiliate Logic
# =======================

class AffiliateLinkBuilder:
    def __init__(self, config: Config):
        self.config = config

    def _is_product_specific(self, url: str) -> bool:
        parsed = urlparse(url)
        path = parsed.path.lower()

        if not parsed.netloc:
            return False

        product_markers = ["/item/", "/i/", "/share/"]
        if any(marker in path for marker in product_markers):
            return True

        return "star.aliexpress" in parsed.netloc.lower()

    def _sign_params(self, params: Dict[str, str]) -> str:
        if not self.config.affiliate_app_secret: return ""
        sorted_keys = sorted(params.keys())
        param_str = "".join([f"{key}{params[key]}" for key in sorted_keys])
        sign_source = f"{self.config.affiliate_app_secret}{param_str}{self.config.affiliate_app_secret}"
        return hashlib.md5(sign_source.encode("utf-8")).hexdigest().upper()

    def _from_api(self, clean_url: str) -> str | None:
        if not self.config.affiliate_api_endpoint or not self.config.affiliate_app_key:
            return None

        print(f"Requesting API link for: {clean_url}")
        timestamp = str(int(time.time() * 1000))
        params = {
            "app_key": self.config.affiliate_app_key,
            "timestamp": timestamp,
            "sign_method": "md5",
            "urls": clean_url,
            "promotion_link_type": "0", # 0 = Regular link, 2 = Hot link
            "tracking_id": "default",   # You can change this if you have a specific tracking ID
            "format": "json",
            "v": "2.0"
        }
        params["sign"] = self._sign_params(params)
        
        try:
            with httpx.Client(timeout=self.config.affiliate_api_timeout) as client:
                resp = client.post(
                    self.config.affiliate_api_endpoint, 
                    data=params, 
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
                )
                resp.raise_for_status()
                data = resp.json()
                
                # Check deep nested response
                if "aliexpress_affiliate_link_generate_response" in data:
                    result = data["aliexpress_affiliate_link_generate_response"].get("resp_result", {}).get("result", {})
                    promos = result.get("promotion_links", {}).get("promotion_link", [])
                    if promos:
                        return promos[0].get("promotion_link") # This should be a s.click short link
                else:
                    print(f"API returned unexpected structure: {data}")

        except Exception as e:
            print(f"API Error: {e}")
        
        return None

    def _from_portal_template(self, clean_url: str) -> str | None:
        # Fallback: Creates a long link, but at least it works
        if not self.config.affiliate_portal_template:
            return None

        encoded = quote(clean_url, safe="")
        template = self.config.affiliate_portal_template

        if "{url}" in template:
            return template.replace("{url}", encoded).strip()

        # If no placeholder is present, the portal template cannot point to the specific product
        print("Affiliate portal template missing {url} placeholder; falling back")
        return None

    def _from_prefix(self, clean_url: str) -> str | None:
        if not self.config.affiliate_prefix:
            return None

        if self.config.affiliate_prefix_encode:
            return f"{self.config.affiliate_prefix}{quote(clean_url, safe='')}".strip()

        return f"{self.config.affiliate_prefix}{clean_url}".strip()

    def build(self, original_url: str) -> str:
        # Step 1: Resolve redirect if configured
        if self.config.resolve_redirects:
            current_url = resolve_url_if_needed(original_url, timeout=self.config.resolve_redirect_timeout)
        else:
            current_url = _canonical_url(original_url)

        # Step 2: Clean and extract ID
        cleaned = extract_item_id_and_clean(current_url)

        # If cleaning failed (no ID found), use the resolved URL as best effort
        if not cleaned:
            cleaned = current_url

        candidates = [
            ("API", self._from_api(cleaned)),
            ("portal template", self._from_portal_template(cleaned)),
            ("prefix", self._from_prefix(cleaned)),
        ]

        for source, link in candidates:
            if not link:
                continue

            if self._is_product_specific(link):
                print(f"Using affiliate link from {source}")
                return link

            print(f"Ignoring {source} affiliate candidate that does not point to a specific product")

        # Fallback: return the cleaned URL (no affiliate)
        return cleaned


# ===============
# Caption creator
# ===============

def extract_fact_hints(text: str) -> Dict[str, str]:
    hints = {}
    
    price_match = re.search(r"(₪|\$)\s?\d+[\d.,]*", text)
    if price_match: hints["price"] = price_match.group(0)
    
    rating_match = re.search(r"(?:⭐|rating[:\s]*)(\d+(?:\.\d+)?)", text, re.IGNORECASE)
    if rating_match: hints["rating"] = rating_match.group(1)
    
    orders_match = re.search(r"(\d[\d.,]*\+?)\s*(?:orders|הזמנות|sold)", text, re.IGNORECASE)
    if orders_match: hints["orders"] = orders_match.group(1)
    
    return hints

class CaptionWriter:
    def __init__(self, openai_client: OpenAI, config: Config):
        self.client = openai_client
        self.model = config.openai_model

    def write(self, orig_text: str, affiliate_url: str) -> str:
        hints = extract_fact_hints(orig_text)
        hints_str = "\n".join([f"- {k}: {v}" for k, v in hints.items()])
        
        prompt = f"""
תכתוב פוסט מכירה קצר לטלגרם.
מוצר: {orig_text[:100]}...
פרטים: {hints_str}

הוראות:
- כותרת קצרה עם אימוג'י.
- 2 משפטים על למה זה שווה.
- מחיר אם ידוע.
- בלי האשטאגים.
- אל תכתוב "קישור" או "לקנייה" - אני מוסיף לבד.
"""
        try:
            res = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=300
            )
            if res.choices and res.choices[0].message.content:
                return res.choices[0].message.content.strip()
            return "דיל מעולה מאליאקספרס! שווה בדיקה 👇"
        except Exception as e:
            print(f"OpenAI Error: {e}")
            return "דיל מעולה מאליאקספרס! שווה בדיקה 👇"

# =======================
# Main Bot Loop
# =======================

class DealBot:
    def __init__(self, client: TelegramClient, writer: CaptionWriter, builder: AffiliateLinkBuilder, config: Config):
        self.client = client
        self.writer = writer
        self.builder = builder
        self.config = config
        self.processed_ids = set()

    async def run(self):
        print("Bot started...")
        for channel in self.config.tg_source_channels:
            print(f"Scanning {channel}...")
            # Iterate over history
            try:
                async for msg in self.client.iter_messages(channel, limit=self.config.max_messages_per_channel):
                    if not msg.message: continue
                    
                    # Find links
                    urls = re.findall(r"https?://[^\s]+", msg.message)
                    ali_urls = [u for u in urls if "aliexpress" in u.lower() or "bit.ly" in u.lower() or "s.click" in u.lower()]
                    
                    if not ali_urls: continue
                    
                    # Process first link found
                    original_link = ali_urls[0]
                    print(f"Found link: {original_link}")
                    
                    # GENERATE LINK
                    affiliate_link = self.builder.build(original_link)
                    
                    # Extract ID for deduplication
                    # We use the affiliate link (which should be clean) to check ID, 
                    # or fallback to hash if ID extraction fails.
                    clean_check = extract_item_id_and_clean(affiliate_link) or affiliate_link
                    prod_id_match = re.search(r"(\d+)\.html", clean_check)
                    
                    if prod_id_match:
                        pid = prod_id_match.group(1)
                    else:
                        # Fallback: hash the URL to avoid reposting same link in this run
                        pid = str(hash(clean_check))
                    
                    if pid in self.processed_ids:
                        print(f"Skipping duplicate ID: {pid}")
                        continue
                    
                    # Generate Text
                    new_caption = self.writer.write(msg.message, affiliate_link)
                    final_msg = f"{new_caption}\n\n👇 לקנייה:\n{affiliate_link}\n\n(id:{pid})"
                    
                    # Send
                    try:
                        if msg.media:
                            await self.client.send_file(self.config.tg_target_channel, msg.media, caption=final_msg)
                        else:
                            await self.client.send_message(self.config.tg_target_channel, final_msg)
                        
                        print(f"Posted: {pid}")
                        self.processed_ids.add(pid)
                        
                        if len(self.processed_ids) >= self.config.max_posts_per_run:
                            print("Max posts reached. Stopping.")
                            return
                            
                    except Exception as e:
                        print(f"Failed to send message: {e}")

            except Exception as e:
                print(f"Error scanning channel {channel}: {e}")

async def main():
    try:
        config = Config.from_env()
        client = TelegramClient(StringSession(config.tg_session), config.tg_api_id, config.tg_api_hash)
        oa_client = OpenAI(api_key=config.openai_api_key)
        
        bot = DealBot(
            client, 
            CaptionWriter(oa_client, config), 
            AffiliateLinkBuilder(config), 
            config
        )
        
        await client.start()
        async with client:
            await bot.run()
            
    except Exception as e:
        print(f"Critical Error in main: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
