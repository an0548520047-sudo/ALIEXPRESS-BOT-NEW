# ALIEXPRESS-BOT-NEW

A Telegram automation that scans source deal channels for AliExpress links, rewrites the copy in Hebrew, swaps in your affiliate link, and posts to your target channel. The workflow is designed to run on GitHub Actions and relies on repository secrets for all credentials.

## How it works
1. Iterates through configured source channels and inspects recent messages.
2. Filters for posts that look like deals (keywords + optional view threshold) and contain an AliExpress URL.
3. Builds an affiliate link using your deep-link prefix, strips any original/encoded URLs (including s.click short links), and guarantees your personal link appears exactly once in every post (with an extra safety append if the model omits it).
4. Uses OpenAI to generate fresh Hebrew copy (not a direct copy of the source) while passing detected price/rating/order/coupon hints to encourage richer posts, then appends a product identifier to avoid duplicates with a deterministic fallback caption if the model returns nothing.
5. Posts the rewritten message to your target channel and logs per-channel skip reasons so you can quickly tune filters.

## Repository layout
- `bot/main.py` – core bot logic.
- `requirements.txt` – Python dependencies.
- `.github/workflows/telegram_affiliate_bot.yml` – scheduled GitHub Actions workflow (runs every 30 minutes by default).

## Required secrets
Set these in **Settings → Secrets and variables → Actions**:
- `TG_API_ID`
- `TG_API_HASH`
- `TG_SESSION` (Telethon StringSession)
- `TG_SOURCE_CHANNELS` (comma-separated list, e.g., `@source1,@source2`)
- `TG_TARGET_CHANNEL` (your channel or chat ID)
- `AFFILIATE_PORTAL_LINK` **or** `AFFILIATE_PREFIX` (at least one is required)
- `OPENAI_API_KEY`

### Where your personal link comes from
- Preferred: set `AFFILIATE_PORTAL_LINK` to the exact deep-link template from your affiliate portal. If it contains `{url}`, the bot replaces that placeholder with the encoded product URL. If it has no placeholder, the value is used verbatim as your personal link.
- Fallback: if `AFFILIATE_PORTAL_LINK` is empty, the bot uses `AFFILIATE_PREFIX` (old-style "prefix + encoded URL") to build the link.
- The bot removes any original URLs from the scraped message and forces the affiliate link to appear exactly once in the final post, with an extra append safeguard if the model ever omits it.

Optional overrides:
- `OPENAI_MODEL` (default: `gpt-4.1-mini`)
- `MIN_VIEWS` (default: `1500`)
- `MAX_MESSAGES_PER_CHANNEL` (default: `80`)
- `DRY_RUN` (default: `false`) – when `true`, the bot logs what it would post without sending messages.
- `MAX_POSTS_PER_RUN` (default: `5`) – hard cap on how many posts are sent per workflow run.
- `MESSAGE_COOLDOWN_SECONDS` (default: `5`) – pause between posts to avoid flooding or hitting Telegram limits.
- `MAX_MESSAGE_AGE_MINUTES` (default: `240`) – skip deals older than this age in minutes.
- `KEYWORD_ALLOWLIST` (optional) – comma-separated keywords that must appear; if empty the built-in defaults are used.
- `KEYWORD_BLOCKLIST` (optional) – comma-separated keywords that will immediately skip a post.

### Deal copy template (Hebrew)
The rewrite prompt now forces a concise Israeli-style template so posts are ready to paste:

1) Opening question that feels relatable to the product.
2) One short line presenting the product as the answer.
3) 3–6 short bullets: model/type, real advantages, key specs/uses.
4) Price/rating/orders lines only when present in the source (💰/⭐/📦).
5) Coupons line only if coupon data exists (🎁, include order if multiple codes).
6) Link block: "👇 לקנייה באליאקספרס:" followed by the affiliate URL on the next line.

Guardrails: Hebrew only, 1–3 emojis total, slightly livelier tone with light humor, no made-up data, and skips sections when details are missing. The prompt receives extracted price/rating/orders/coupon hints (when present) to keep those lines in the output, warns against extra links, and the bot strips non-affiliate URLs plus enforces your link exactly once if the model goes off-script.

If OpenAI ever returns an empty message, the bot switches to a minimal Hebrew fallback caption that still includes your affiliate link.

You can copy `.env.example` to `.env` for local testing and fill in your values.

## Running locally
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export TG_API_ID=...
export TG_API_HASH=...
export TG_SESSION=...
export TG_SOURCE_CHANNELS="@source1,@source2"
export TG_TARGET_CHANNEL=@your_channel
# Use ONE of the following affiliate configs:
export AFFILIATE_PORTAL_LINK="https://portals.aliexpress.com/deeplink?url={url}"
export AFFILIATE_PREFIX=""  # leave empty if using AFFILIATE_PORTAL_LINK
export OPENAI_API_KEY=...
export DRY_RUN=true  # optional safety switch while testing locally
# Optional fine-tuning
export MAX_POSTS_PER_RUN=5
export MESSAGE_COOLDOWN_SECONDS=5
export MAX_MESSAGE_AGE_MINUTES=240
export KEYWORD_ALLOWLIST="מבצע,קופון"
export KEYWORD_BLOCKLIST="adult"
python bot/main.py
```

## Notes
- The bot only posts a product once per target channel by tagging each message with `(id:<product_id>)`.
- Adjust the cron schedule in `.github/workflows/telegram_affiliate_bot.yml` if you want a different posting cadence.
- Keep secrets out of version control; the workflow reads everything from GitHub Secrets.
- If `TG_SOURCE_CHANNELS` parses to an empty list (e.g., just commas), the bot fails fast to avoid silent no-op runs.
- Use the new keyword allow/block lists and age + per-run caps to keep the feed clean and reduce noise.
- Check the per-channel and overall skip summaries in the logs to see why items were filtered out (e.g., missing keywords, old posts, duplicates).
- Each run logs a short preflight summary (dry-run flag, source count, target channel, affiliate mode, max posts) so you can confirm configuration without exposing secrets.

## מה עכשיו? (צ'ק־ליסט מהיר)
1) ודא שכל ה-Secrets קיימים ברפו תחת **Settings → Secrets and variables → Actions** בשמות המדויקים שמופיעים בטבלה למעלה.
2) אם חסר Secret – הוסף ערך חדש בשם הזהה (למשל `TG_SOURCE_CHANNELS`) והדבק את הערך המתאים.
3) בלשונית **Actions** בחר את ה-Workflow "Telegram Affiliate Bot" והפעל **Run workflow** פעם אחת כדי לראות שהכול תקין בלוגים.
4) מרגע שהריצה הראשונה הצליחה, ה-Workflow יפעל אוטומטית כל 30 דקות (לפי ה-cron). אפשר לשנות את התזמון בקובץ ה-YAML אם תרצה.
5) לבדיקת ביצועים או הדגמה מקומית, הרץ את הפקודות שבחלק "Running locally" (עם אותם משתני סביבה).

אם משהו נתקע או אין פוסטים בקבוצת היעד:
- ודא שהקבוצות במשתנה `TG_SOURCE_CHANNELS` פומביות או שהחשבון שמייצר את ה-`TG_SESSION` חבר בהן.
- הגדל זמנית את `MAX_MESSAGES_PER_CHANNEL` או הקטן את `MIN_VIEWS` כדי לתפוס יותר פוסטים בבדיקה.
- בדוק ביומן הריצה ב-GitHub Actions את ההדפסות (log) שמגיעות מהבוט.
