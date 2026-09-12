name: News bot

on:
  schedule:
    - cron: '0 * * * *'      # каждый час
  workflow_dispatch: {}       # позволяет запустить вручную кнопкой

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Установить зависимости
        run: pip install -r requirements.txt

      - name: Запустить бота
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}
        run: python news_bot.py

      - name: Сохранить state.json обратно в репозиторий
        run: |
          git config user.name "news-bot"
          git config user.email "news-bot@users.noreply.github.com"
          git add state.json
          git diff --quiet --cached || git commit -m "update state"
          git push
