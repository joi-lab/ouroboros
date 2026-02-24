# Atlas Prompt: Viktor-Friday Colab Deploy

Copy this prompt into Atlas in browser.

```text
Ты DevOps-ассистент. Нужен рабочий Colab для проекта Viktor-Friday (репозиторий: ouroboros-base).

Сделай пошагово:
1) Сгенерируй готовый Google Colab notebook с ячейками запуска.
2) Используй Python 3.
3) Данные сессий сохраняй в Google Drive: /content/drive/MyDrive/vfriday-data
4) Запуск:
   - FastAPI оркестратор в фоне (порт 8080)
   - Telegram Tutor Bot в foreground
5) В конце дай checklist проверки.

Требования к notebook:

Ячейка 1 (mount + clone + install):
- from google.colab import drive; drive.mount('/content/drive')
- git clone https://github.com/<MY_GITHUB_USER>/ouroboros-base.git /content/ouroboros-base
- cd /content/ouroboros-base
- pip install -r requirements.txt

Ячейка 2 (env):
- экспортируй:
  VFRIDAY_TELEGRAM_BOT_TOKEN=<PUT_TOKEN>
  OPENROUTER_API_KEY=<PUT_KEY>
  VFRIDAY_API_HOST=0.0.0.0
  VFRIDAY_API_PORT=8080
  VFRIDAY_ORCHESTRATOR_URL=http://127.0.0.1:8080
  VFRIDAY_DATA_DIR=/content/drive/MyDrive/vfriday-data
- создай директорию VFRIDAY_DATA_DIR

Ячейка 3 (инициализация skills-state, опционально):
- python3 scripts/vfriday_skill_init.py
- python3 scripts/vfriday_skill_state.py

Ячейка 4 (start API in background):
- nohup python3 -m vfriday.app > /content/vfriday_api.log 2>&1 &
- sleep 3
- curl -s http://127.0.0.1:8080/healthz

Ячейка 5 (smoke):
- python3 scripts/vfriday_smoke_run.py

Ячейка 6 (start tutor bot):
- python3 -m vfriday.integrations.telegram_tutor_bot

Ячейка 7 (debug helpers):
- tail -n 120 /content/vfriday_api.log
- пример команд для Telegram:
  /new_session
  /help Я застрял на шаге с проекцией вектора

После генерации notebook:
- Проверь, что /healthz возвращает status=ok.
- Проверь, что smoke выводит "SMOKE OK".
- Дай мне краткий runbook: как перезапустить API и bot после disconnect Colab.
```

