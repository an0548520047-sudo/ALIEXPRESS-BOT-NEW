from __future__ import annotations

import asyncio
import os
import re
import time
import hashlib
import json
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from datetime import datetime

import httpx
from openai import OpenAI
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityTextUrl

# ==================
# Config & Helpers
# ==================

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        alt = name.replace("API_", "")
        value = os.getenv(alt) or os.getenv(name.replace("ALIEXPRESS_", "AFFILIATE_"))
        if not value or not value.strip():
            if "APP_KEY" in name or "SESSION" in name or "HASH" in name:
                raise RuntimeError(f"Missing required env: {name}")
            return ""
    return value.strip()

def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except:
        return default

@dataclass
class Config:
    tg_api_id: int
    tg_api_hash: str
    tg_session: str
    tg_source_channels: List[str]
    tg_target_channel: str
    affiliate_app_key: str
    affiliate_app_secret: str
    openai_api_key: str
    openai_model: str
    max_messages_per_channel: int
    max_posts_per_run: int
    min_rating: float = 4.6
    min_orders: int = 500

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            tg_api_id=int(_require_env("TG_API_ID")),
            tg_api_hash=_require_env("TG_API_HASH"),
            tg_session=_require_env("TG_SESSION"),
            tg_source_channels=[c.strip() for c in _require_env("TG_SOURCE_CHANNELS").split(",") if c.strip()],
            tg_target_channel=_require_env("TG_TARGET_CHANNEL"),
            affiliate_app_key=_require_env("ALIEXPRESS_APP_KEY"),
            affiliate_app_secret=_require_env("ALIEXPRESS_APP_SECRET"),
            openai_api_key=_require_env("OPENAI_API_KEY"),
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            max_messages_per_channel=_int_env("MAX_MESSAGES_PER_CHANNEL", 250),
            max_posts_per_run=_int_env("MAX_POSTS_PER_RUN", 10)
        )

# =======================
# Logic Utils
# =======================

def resolve_url_smart(url: str) -> str:
    """פותר הפניות (bit.ly וכד') כדי להגיע ללינק האמיתי"""
    if "aliexpress" in url and "s.click" not in url:
        return url
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.head(url, follow_redirects=True)
            return str(resp.url).replace("m.aliexpress", "www.aliexpress")
    except:
        return url

def extract_item_id(url: str) -> str | None:
    """מחלץ את ה-ID של המוצר מהלינק"""
    match = re.search(r"/item/(\d+)\.html", url)
    if match: return match.group(1)
    match = re.search(r"(\d{10,})", url)
    return match.group(1) if match else None

# =======================
# AliExpress API Class (Taobao Router Fix)
# =======================

class AliExpressAPI:
    def __init__(self, config: Config):
        self.config = config
        # שינוי קריטי: מעבר לשרת Taobao שהוא היציב ביותר
        self.base_url = "https://api.taobao.com/router/rest"

    def _sign(self, params: Dict[str, str]) -> str:
        # החתימה נשארת זהה: Secret + Params + Secret
        s = "".join([f"{k}{params[k]}" for k in sorted(params.keys())])
        s = f"{self.config.affiliate_app_secret}{s}{self.config.affiliate_app_secret}"
        return hashlib.md5(s.encode("utf-8")).hexdigest().upper()

    def get_product_details(self, item_id: str) -> Dict | None:
        print(f"🔍 Checking quality for item: {item_id}")
        # שינוי קריטי 2: שינוי פורמט הזמן ל-YYYY-MM-DD HH:MM:SS
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        params = {
            "app_key": self.config.affiliate_app_key,
            "timestamp": current_time,
            "sign_method": "md5",
            "method": "aliexpress.affiliate.product.detail.get",
            "product_ids": item_id,
            "target_currency": "ILS",
            "target_language": "HE",
            "tracking_id": "bot_quality_check",
            "format": "json",
            "v": "2.0"
        }
        params["sign"] = self._sign(params)

        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(
                    self.base_url, 
                    data=params, 
                    headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"}
                )
                
                try:
                    data = resp.json()
                except json.JSONDecodeError:
                    print(f"⚠️ Critical: API returned non-JSON. Status: {resp.status_code}. Body: {resp.text[:100]}...")
                    return None

                # בדיקת שגיאות תגובה
                if "error_response" in data:
                    err = data["error_response"]
                    print(f"🛑 API ERROR for {item_id}: {err.get('msg')} (Code: {err.get('code')}) | {err.get('sub_msg')}")
                    return None

                response_root = data.get("aliexpress_affiliate_product_detail_get_response")
                if not response_root:
                    print(f"⚠️ Unexpected JSON structure: {list(data.keys())}")
                    return None

                resp_result = response_root.get("resp_result", {})
                if resp_result.get("resp_code") != 200:
                    print(f"⚠️ Logic Error (Item {item_id}): {resp_result.get('resp_msg')} (Code: {resp_result.get('resp_code')})")
                    return None

                result_data = resp_result.get("result")
                if not result_data:
                    print(f"⚠️ Item {item_id} exists but no data returned (Restricted? Sold out?).")
                    return None

                products = result_data.get("products", {}).get("product")
                if products:
                    return products[0]
                else:
                    print(f"⚠️ Item {item_id} product list is empty.")
                    return None

        except Exception as e:
            print(f"⚠️ Connection/HTTP Exception: {e}")
        return None

    def generate_link(self, url: str) -> str | None:
        # שינוי קריטי 2: שינוי פורמט הזמן גם כאן
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        params = {
            "app_key": self.config.affiliate_app_key,
            "timestamp": current_time,
            "sign_method": "md5",
            "method": "aliexpress.affiliate.link.generate",
            "urls": url,
            "promotion_link_type": "2",
            "tracking_id": f"tg_bot_{datetime.now().strftime('%m%d')}",
            "format": "json",
            "v": "2.0"
        }
        params["sign"] = self._sign(params)

        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(self.base_url, data=params, headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"})
                data = resp.json()
                
                if "error_response" in data:
                     print(f"🛑 Link Gen Error: {data['error_response'].get('sub_msg')}")
                     return None

                if "aliexpress_affiliate_link_generate_response" in data:
                    res = data["aliexpress_affiliate_link_generate_response"]["resp_result"]["result"]
                    if "promotion_links" in res and res["promotion_links"]["promotion_link"]:
                        return res["promotion_links"]["promotion_link"][0]["promotion_link"]
                else:
                     print(f"⚠️ Link Gen Structure Mismatch: {data}")
                     
        except Exception as e:
            print(f"⚠️ API Link Gen Exception: {e}")
        return None

# =======================
# Content Generation
# =======================

class Copywriter:
    def __init__(self, client: OpenAI, model: str):
        self.client = client
        self.model = model

    def write_post(self, original_text: str, price: str = "") -> str:
        prompt = f"""
אתה קופירייטר ישראלי מומחה לשיווק בטלגרם.
המטרה: לכתוב פוסט קצר, דחוף ואנרגטי שגורם לאנשים להקליק ולקנות מיד.
המוצר מתואר בטקסט המקור: "{original_text[:300]}"
מחיר (אם ידוע): {price}

הנחיות:
1. כותרת חזקה עם אימוג'י (למשל: הלם 😱, מחיר הזיה 📉, חוטפים את זה 🔥).
2. גוף הטקסט: 2-3 משפטים קצרים בסלנג ישראלי טבעי ("תקשיבו זה מטורף", "אל תפספסו").
3. בלי האשטאגים. בלי "שלום לכולם". ישר ולעניין.
4. תדגיש את המחיר אם הוא זול.
"""
        try:
            res = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,
                max_tokens=250
            )
            return res.choices[0].message.content.strip()
        except:
            return "דיל מטורף מאליאקספרס! אל תפספסו את המחיר הזה 🔥👇"

# =======================
# Main Bot Logic
# =======================

class DealBot:
    def __init__(self, client: TelegramClient, ali_api: AliExpressAPI, copywriter: Copywriter, config: Config):
        self.client = client
        self.ali = ali_api
        self.writer = copywriter
        self.config = config
        self.processed_ids = set()

    async def load_history(self):
        print(f"📚 Scanning last {self.config.max_messages_per_channel} messages for history...")
        try:
            async for msg in self.client.iter_messages(self.config.tg_target_channel, limit=self.config.max_messages_per_channel):
                if not msg.message: continue
                
                if msg.entities:
                    for ent in msg.entities:
                        if isinstance(ent, MessageEntityTextUrl) and "bot-id" in ent.url:
                             match = re.search(r"bot-id/(\d+)", ent.url)
                             if match: self.processed_ids.add(match.group(1))
                
                match_old = re.search(r"id[:\-](\\d+)", msg.message)
                if match_old: self.processed_ids.add(match_old.group(1))
                
        except Exception as e:
            print(f"History load warning: {e}")
        print(f"✅ Loaded {len(self.processed_ids)} items to ignore.")

    async def run(self):
        await self.load_history()
        print("🚀 Bot started...")
        
        posts_count = 0
        
        for channel in self.config.tg_source_channels:
            print(f"👀 Scanning source: {channel}...")
            try:
                async for msg in self.client.iter_messages(channel, limit=50):
                    if posts_count >= self.config.max_posts_per_run:
                        print("✋ Max posts reached for this run.")
                        return
                    
                    if not msg.message: continue

                    urls = re.findall(r"https?://[^\s]+", msg.message)
                    ali_url = next((u for u in urls if "aliexpress" in u or "bit.ly" in u or "s.click" in u), None)
                    if not ali_url: continue

                    real_url = resolve_url_smart(ali_url)
                    item_id = extract_item_id(real_url)
                    
                    if not item_id: continue
                    if item_id in self.processed_ids:
                        print(f"⏭️ Duplicate found: {item_id}")
                        continue

                    details = self.ali.get_product_details(item_id)
                    
                    if not details:
                        print(f"⚠️ Skipping {item_id} due to missing details.")
                        continue

                    rating = float(details.get("evaluate_rate", "0").replace("%", "")) 
                    if rating > 5: rating = rating / 20 
                    
                    orders = int(details.get("last_volume", 0))
                    price = details.get("target_sale_price", "")

                    print(f"📊 Product {item_id}: {rating}⭐ | {orders} Orders")

                    if rating < self.config.min_rating:
                        print(f"❌ Low Rating ({rating} < {self.config.min_rating}). Skip.")
                        continue
                    
                    if orders < self.config.min_orders:
                        print(f"❌ Low Orders ({orders} < {self.config.min_orders}). Skip.")
                        continue

                    aff_link = self.ali.generate_link(real_url)
                    if not aff_link:
                        print("❌ Failed to generate affiliate link. Skip.")
                        continue

                    caption = self.writer.write_post(msg.message, price)
                    
                    hidden_id = f"[‎](http://bot-id/{item_id})"
                    final_text = f"{hidden_id}{caption}\n\n👇 דיל בלעדי:\n{aff_link}"

                    clean_image = details.get("product_main_image_url")
                    
                    try:
                        if clean_image:
                            await self.client.send_file(self.config.tg_target_channel, clean_image, caption=final_text)
                        elif msg.media:
                            await self.client.send_file(self.config.tg_target_channel, msg.media, caption=final_text)
                        else:
                            await self.client.send_message(self.config.tg_target_channel, final_text, link_preview=True)
                        
                        print(f"✅ Published Item: {item_id}")
                        self.processed_ids.add(item_id)
                        posts_count += 1
                        
                    except Exception as e:
                        print(f"💥 Send Error: {e}")

            except Exception as e:
                print(f"Error scanning channel {channel}: {e}")

async def main():
    config = Config.from_env()
    client = TelegramClient(StringSession(config.tg_session), config.tg_api_id, config.tg_api_hash)
    oa_client = OpenAI(api_key=config.openai_api_key)
    
    ali_api = AliExpressAPI(config)
    copywriter = Copywriter(oa_client, config.openai_model)
    
    bot = DealBot(client, ali_api, copywriter, config)
    
    await client.start()
    async with client:
        await bot.run()

if __name__ == "__main__":
    asyncio.run(main())
