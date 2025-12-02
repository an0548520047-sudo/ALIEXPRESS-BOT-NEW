name: Telegram Affiliate Bot

on:
  schedule:
    - cron: "*/30 * * * *"  # מריץ כל 30 דקות
  workflow_dispatch:        # מאפשר הרצה ידנית

jobs:
  run-bot:
    runs-on: ubuntu-latest

    env:
      # משתני טלגרם (אל תיגע, זה מושך מהסודות)
      TG_API_ID: ${{ secrets.TG_API_ID }}
      TG_API_HASH: ${{ secrets.TG_API_HASH }}
      TG_SESSION: ${{ secrets.TG_SESSION }}
      TG_SOURCE_CHANNELS: ${{ secrets.TG_SOURCE_CHANNELS }}
      TG_TARGET_CHANNEL: ${{ secrets.TG_TARGET_CHANNEL }}
      
      # משתני אליאקספרס - התיקון החשוב!
      # אנחנו מקשרים כאן בין השמות שיש לך בסודות (מימין) למשתנים שהקוד צריך (משמאל)
      ALIEXPRESS_APP_KEY: ${{ secrets.ALIEXPRESS_APP_KEY }}
      ALIEXPRESS_APP_SECRET: ${{ secrets.ALIEXPRESS_APP_SECRET }}
      
      # הגדרות API קבועות
      AFFILIATE_API_ENDPOINT: "https://api-sg.aliexpress.com/sync"
      AFFILIATE_API_TIMEOUT: "15"

      # משתני OpenAI
      OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
      OPENAI_MODEL: "gpt-4o-mini"
      
      # הגדרות כלליות לבוט
      MAX_POSTS_PER_RUN: "10"
      MAX_MESSAGES_PER_CHANNEL: "50"
      RESOLVE_REDIRECTS: "true"

    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: |
          python -m pip install --upgrade pip
          pip install telethon openai python-dotenv httpx

      - name: Run bot
        run: |
          python -m bot.main
