# ALIEXPRESS-BOT-NEW

A Telegram automation that scans source deal channels for AliExpress links, rewrites the copy in Hebrew, swaps in your affiliate link, and posts to your target channel. The workflow is designed to run on GitHub Actions and relies on repository secrets for all credentials.

## How it works
1. Iterates through configured source channels and inspects recent messages.
2. Filters for posts that look like deals (keywords + optional view threshold) and contain an AliExpress URL.
3. Builds an affiliate link using your deep-link prefix.
4. Uses OpenAI to generate fresh Hebrew copy (not a direct copy of the source) and appends a product identifier to avoid duplicates.
5. Posts the rewritten message to your target channel.

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
- `AFFILIATE_PREFIX` (affiliate deep-link prefix)
- `OPENAI_API_KEY`

Optional overrides:
- `OPENAI_MODEL` (default: `gpt-4.1-mini`)
- `MIN_VIEWS` (default: `1500`)
- `MAX_MESSAGES_PER_CHANNEL` (default: `80`)
- `DRY_RUN` (default: `false`) – when `true`, the bot logs what it would post without sending messages.

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
export AFFILIATE_PREFIX="https://example.com/deeplink?url="
export OPENAI_API_KEY=...
export DRY_RUN=true  # optional safety switch while testing locally
python bot/main.py
```

## Notes
- The bot only posts a product once per target channel by tagging each message with `(id:<product_id>)`.
- Adjust the cron schedule in `.github/workflows/telegram_affiliate_bot.yml` if you want a different posting cadence.
- Keep secrets out of version control; the workflow reads everything from GitHub Secrets.

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
