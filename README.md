# Ouroboros

Самоэволюционирующий AI-агент с Telegram-интерфейсом. Выполняет задачи, сохраняет цепочку рассуждений (reasoning), и автоматически улучшает результаты через эволюционный цикл.

Форк [joi-lab/ouroboros](https://github.com/joi-lab/ouroboros) с поддержкой **DeepSeek API** и **локального запуска**.

---

## Что умеет

- **Выполняет задачи** -- отправляешь запрос в Telegram, получаешь результат
- **Reasoning Capture** -- цепочки рассуждений модели (chain-of-thought) сохраняются в `reasoning.jsonl`, а не выбрасываются. Видно, как агент думает
- **Эволюция по задаче** -- после выполнения задачи агент автоматически развивает результат: углубляет анализ, добавляет примеры, исправляет недостатки
- **Приоритет пользователя** -- новое сообщение мгновенно прерывает эволюцию. Пользователь всегда первый
- **Бюджет и защита** -- лимит расходов, circuit breaker (3 неудачи → пауза), автоотключение при низком балансе

---

## Требования

- Python 3.10+
- [DeepSeek API ключ](https://platform.deepseek.com/api_keys)
- [Telegram Bot Token](https://t.me/BotFather) (создай бота через `/newbot`)
- [GitHub Token](https://github.com/settings/tokens) (classic token с `repo` scope)

---

## Установка

### 1. Клонируй репозиторий

```bash
git clone https://github.com/YOUR_USERNAME/ouroboros.git
cd ouroboros
```

### 2. Установи зависимости

```bash
pip install -r requirements.txt
```

Содержимое `requirements.txt`:
```
openai>=1.0.0
requests
playwright
playwright-stealth
```

Для браузерных задач (опционально):
```bash
playwright install chromium
```

### 3. Создай файл `.env`

В корне проекта создай файл `.env` со своими ключами:

```bash
# === ОБЯЗАТЕЛЬНЫЕ ===

# DeepSeek API ключ (https://platform.deepseek.com/api_keys)
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Telegram Bot Token (получи у @BotFather)
TELEGRAM_BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrSTUvwxYZ

# Лимит расходов в долларах (агент остановится при превышении)
TOTAL_BUDGET=100

# GitHub Token (https://github.com/settings/tokens → classic → repo scope)
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Твой GitHub username и имя форка
GITHUB_USER=your_username
GITHUB_REPO=ouroboros

# === МОДЕЛИ ===

# Основная модель (deepseek-reasoner = с reasoning, deepseek-chat = без)
OUROBOROS_MODEL=deepseek-reasoner

# Модель для кода
OUROBOROS_MODEL_CODE=deepseek-reasoner

# Лёгкая модель (для дедупликации, компактизации контекста)
OUROBOROS_MODEL_LIGHT=deepseek-chat

# === ОПЦИОНАЛЬНО ===

# Количество воркер-процессов (по умолчанию 3)
OUROBOROS_MAX_WORKERS=3

# Максимум раундов LLM на одну задачу (по умолчанию 200)
OUROBOROS_MAX_ROUNDS=200

# Таймауты в секундах
OUROBOROS_SOFT_TIMEOUT_SEC=600
OUROBOROS_HARD_TIMEOUT_SEC=1800
```

### 4. Запусти

```bash
python local_launcher.py --data-dir ./local_data --repo-dir .
```

Ожидаемый вывод:
```
21:15:37 [ouroboros.local_launcher] INFO: TelegramClient patched to use curl
21:15:38 [ouroboros.local_launcher] INFO: Local mode: skipping safe_restart
21:15:38 [ouroboros.local_launcher] INFO: Ouroboros started (local, DeepSeek backend)
21:15:38 [ouroboros.local_launcher] INFO:   Model: deepseek-reasoner | Code: deepseek-reasoner | Light: deepseek-chat
21:15:38 [ouroboros.local_launcher] INFO:   Workers: 3 | Budget: $100.00
21:15:38 [ouroboros.local_launcher] INFO: Evolution mode enabled by default
21:15:38 [ouroboros.local_launcher] INFO: Entering main loop — send a message to your Telegram bot to begin.
```

### 5. Отправь сообщение в Telegram

Открой своего бота в Telegram и напиши любое сообщение. Первый написавший становится **владельцем** — все остальные игнорируются.

---

## Как это работает

```
Пользователь (Telegram)
    │
    ▼
local_launcher.py          ← главный цикл, принимает сообщения
    │
    ├── handle_chat_direct() ← обрабатывает задачу пользователя
    │       │
    │       ▼
    │   ouroboros/agent.py   ← оркестратор
    │       │
    │       ▼
    │   ouroboros/loop.py    ← LLM цикл (запрос → инструменты → запрос → ...)
    │       │                  сохраняет reasoning_content каждого раунда
    │       ▼
    │   ouroboros/llm.py     ← вызовы DeepSeek API
    │
    ├── Результат → state.json (last_user_task_text + last_user_task_result)
    │
    ▼
enqueue_evolution_task_if_needed()
    │
    ▼
Воркер-процесс выполняет эволюцию по последней задаче
    │
    ▼
Новое сообщение от пользователя → эволюция отменяется
```

---

## Структура файлов

```
ouroboros/
├── local_launcher.py        # Точка входа (DeepSeek backend)
├── .env                     # API ключи (не коммитится)
├── requirements.txt         # Зависимости
│
├── ouroboros/                # Ядро агента
│   ├── agent.py             # Оркестратор задач
│   ├── loop.py              # LLM цикл + reasoning capture
│   ├── llm.py               # Клиент OpenRouter/DeepSeek
│   ├── context.py           # Формирование контекста для LLM
│   ├── consciousness.py     # Фоновое сознание (отключено в local)
│   ├── memory.py            # Память: scratchpad, identity
│   └── tools/               # Инструменты агента
│       ├── core.py          # Файловые операции
│       ├── git.py           # Git операции
│       ├── github.py        # GitHub Issues
│       ├── shell.py         # Shell, Claude Code
│       ├── search.py        # Веб-поиск
│       ├── browser.py       # Playwright браузер
│       └── control.py       # Управление: restart, evolve, review
│
├── supervisor/              # Управление процессами
│   ├── state.py             # Состояние, бюджет
│   ├── telegram.py          # Telegram клиент
│   ├── queue.py             # Очередь задач, эволюция
│   ├── workers.py           # Воркеры
│   ├── events.py            # Обработка событий
│   └── git_ops.py           # Git операции
│
└── local_data/              # Данные (создаётся автоматически)
    ├── state/
    │   ├── state.json       # Состояние агента
    │   └── queue_snapshot.json
    ├── logs/
    │   ├── events.jsonl     # Все события (раунды, ошибки, метрики)
    │   ├── reasoning.jsonl  # Цепочки рассуждений LLM
    │   ├── tools.jsonl      # Вызовы инструментов
    │   └── supervisor.jsonl # Логи супервизора
    ├── memory/
    │   ├── identity.md      # Идентичность агента
    │   ├── scratchpad.md    # Рабочие заметки
    │   └── *.md             # Документы, созданные агентом
    └── task_results/
        └── *.json           # Результаты выполненных задач
```

---

## Мониторинг и логи

### Reasoning — как думает агент

```bash
# В реальном времени
tail -f local_data/logs/reasoning.jsonl | python3 -m json.tool

# Красиво вывести все цепочки
cat local_data/logs/reasoning.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    r = json.loads(line)
    final = ' [FINAL]' if r.get('is_final') else ''
    print(f'Round {r[\"round\"]}{final}:')
    print(r['reasoning_content'][:500])
    print('---')
"
```

### События — что происходит

```bash
# Все события в реальном времени
tail -f local_data/logs/events.jsonl | python3 -m json.tool

# Только раунды LLM (с reasoning, если есть)
grep '"llm_round"' local_data/logs/events.jsonl | python3 -m json.tool

# Только эволюция
grep -i 'evolution' local_data/logs/events.jsonl | python3 -m json.tool

# Ошибки
grep -i 'error\|timeout\|failure' local_data/logs/events.jsonl | python3 -m json.tool
```

### Состояние агента

```bash
# Полное состояние
cat local_data/state/state.json | python3 -m json.tool

# Быстрая сводка
cat local_data/state/state.json | python3 -c "
import sys, json; s = json.load(sys.stdin)
print(f'Budget:     \${s.get(\"spent_usd\", 0):.4f} spent')
print(f'Evolution:  {\"ON\" if s.get(\"evolution_mode_enabled\") else \"OFF\"} (cycle {s.get(\"evolution_cycle\", 0)})')
print(f'Failures:   {s.get(\"evolution_consecutive_failures\", 0)}')
print(f'Last task:  {(s.get(\"last_user_task_text\") or \"-\")[:80]}')
"
```

### Результаты задач

```bash
# Список выполненных задач
ls -lt local_data/task_results/

# Посмотреть конкретный результат
cat local_data/task_results/<task_id>.json | python3 -m json.tool
```

### Инструменты — что вызывал агент

```bash
# Все вызовы инструментов
tail -f local_data/logs/tools.jsonl | python3 -m json.tool

# Только определённый инструмент
grep '"drive_write"' local_data/logs/tools.jsonl | python3 -m json.tool
```

### Память агента

```bash
# Какие файлы агент создал в памяти
ls -la local_data/memory/

# Прочитать конкретный файл
cat local_data/memory/llm-agent-maturity-methodology.md
```

---

## Команды Telegram

| Команда | Описание |
|---------|----------|
| `/status` | Статус: воркеры, очередь, бюджет |
| `/evolve` | Включить эволюцию |
| `/evolve off` | Выключить эволюцию |
| `/review` | Запустить глубокий обзор кода |
| `/bg start` | Включить фоновое сознание |
| `/bg stop` | Выключить фоновое сознание |
| `/restart` | Мягкий перезапуск |
| `/panic` | Аварийная остановка |

Любое другое сообщение — задача для агента.

---

## Эволюция

### Как работает

1. Пользователь отправляет задачу → агент выполняет
2. Текст задачи и результат сохраняются в `state.json`
3. Когда очередь пуста → запускается эволюция
4. Агент читает последнюю задачу и улучшает результат
5. Новое сообщение от пользователя → эволюция мгновенно отменяется

### Защиты

| Защита | Описание |
|--------|----------|
| **Приоритет пользователя** | Новое сообщение отменяет running/pending эволюцию |
| **Circuit breaker** | 3 неудачи подряд → эволюция ставится на паузу |
| **Budget guard** | Остаток < $50 → эволюция отключается |
| **Busy check** | Эволюция не стартует, пока агент занят задачей пользователя |

### Проверить что эволюция работает

```bash
# 1. Есть ли последняя задача для эволюции?
cat local_data/state/state.json | python3 -c "
import sys, json; s = json.load(sys.stdin)
print('Evolution:', 'ON' if s.get('evolution_mode_enabled') else 'OFF')
print('Cycle:', s.get('evolution_cycle', 0))
print('Task:', (s.get('last_user_task_text') or 'нет')[:100])
"

# 2. Reasoning эволюции (агент думает о задаче пользователя?)
tail -20 local_data/logs/reasoning.jsonl | python3 -c "
import sys, json
for line in sys.stdin:
    r = json.loads(line)
    print(f'Round {r[\"round\"]}:', r['reasoning_content'][:200])
    print()
"

# 3. Результат эволюции
ls -lt local_data/task_results/ | head -5
```

---

## Стоимость

DeepSeek API — одно из самых дешёвых:

| Модель | Input (за 1M токенов) | Output | Cache hit |
|--------|----------------------|--------|-----------|
| `deepseek-reasoner` | $0.55 | $2.19 | $0.14 |
| `deepseek-chat` | $0.27 | $1.10 | $0.07 |

Типичная задача: 4-8 раундов, $0.01-0.05. Эволюция: ~$0.02-0.03 за цикл.

---

## Troubleshooting

### Бот не отвечает в Telegram
```bash
# Проверь что процесс запущен
ps aux | grep local_launcher

# Проверь логи на ошибки
tail -50 local_data/logs/supervisor.jsonl | grep -i error
```

### SSL ошибки (conda)
`local_launcher.py` автоматически патчит Telegram клиент на `curl`. Если `curl` не работает:
```bash
# Проверь curl
curl -s https://api.telegram.org/bot<TOKEN>/getMe
```

### Worker SHA mismatch
```bash
# Подтяни код и перезапусти
git pull
python local_launcher.py --data-dir ./local_data --repo-dir .
```

### Эволюция не запускается
```bash
# Проверь условия
cat local_data/state/state.json | python3 -c "
import sys, json; s = json.load(sys.stdin)
print('Enabled:', s.get('evolution_mode_enabled'))
print('Failures:', s.get('evolution_consecutive_failures'))
print('Last task:', bool(s.get('last_user_task_text')))
print('Budget spent:', s.get('spent_usd'))
"
```
- `Enabled: False` → отправь `/evolve` в Telegram
- `Failures: 3` → circuit breaker. `/evolve` для сброса
- `Last task: False` → отправь хотя бы одну задачу
- Бюджет исчерпан → увеличь `TOTAL_BUDGET` в `.env`

### Сбросить всё состояние
```bash
rm -rf local_data/
python local_launcher.py --data-dir ./local_data --repo-dir .
```

---

## Лицензия

[MIT License](LICENSE)
