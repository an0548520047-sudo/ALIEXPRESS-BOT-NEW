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
