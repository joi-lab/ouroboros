# Уроборос

Самомодифицирующийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 2.22.0

---

## Быстрый старт

1. В Colab добавь Secrets:
   - `OPENROUTER_API_KEY` (обязательно)
   - `TELEGRAM_BOT_TOKEN` (обязательно)
   - `TOTAL_BUDGET` (обязательно, в USD)
   - `GITHUB_TOKEN` (обязательно)
   - `OPENAI_API_KEY` (опционально — для web_search)
   - `ANTHROPIC_API_KEY` (опционально — для claude_code_edit)

2. Опционально добавь config-ячейку:
```python
import os
CFG = {
    "GITHUB_USER": "razzant",
    "GITHUB_REPO": "ouroboros",
    "OUROBOROS_MODEL": "openai/gpt-5.2",
    "OUROBOROS_MODEL_CODE": "openai/gpt-5.2-codex",
    "OUROBOROS_MODEL_LIGHT": "anthropic/claude-sonnet-4",  # optional: lighter model for user chat
    "OUROBOROS_MAX_WORKERS": "5",
}
for k, v in CFG.items():
    os.environ[k] = str(v)
```

3. Запусти boot shim (см. `colab_bootstrap_shim.py`).
4. Напиши боту в Telegram. Первый написавший — владелец.

## Архитектура

```
Telegram → colab_launcher.py (thin entry point)
               ↓
           supervisor/           (package)
            ├── state.py         — persistent state + budget + status
            ├── telegram.py      — TG client + formatting + typing
            ├── git_ops.py       — checkout, sync, rescue, safe_restart
            ├── queue.py         — task queue, priority, timeouts, scheduling
            ├── workers.py       — worker lifecycle, health, direct chat
            └── events.py        — event dispatch table (extracted from main loop)
               ↓
           ouroboros/             (agent package)
            ├── agent.py         — thin orchestrator (task handling + events)
            ├── loop.py          — LLM tool loop (concurrent tools, retry, cost)
            ├── context.py       — context builder + prompt caching + compaction
            ├── apply_patch.py   — Claude Code CLI apply_patch shim
            ├── tools/           — pluggable tools
            ├── llm.py           — LLM client + cached token tracking
            ├── memory.py        — scratchpad, identity
            └── review.py        — code review utilities
```

`colab_launcher.py` — тонкий entry point: секреты, bootstrap, main loop.
Вся логика супервизора декомпозирована в `supervisor/` пакет.

`agent.py` — тонкий оркестратор. Принимает задачу, собирает контекст,
вызывает LLM loop, эмитит результаты. Не содержит LLM-логики напрямую.

`loop.py` — ядро: LLM-вызов с инструментами в цикле. **Concurrent tool
execution** (ThreadPoolExecutor), retry, effort escalation, per-round cost
logging. Единственное место где происходит взаимодействие LLM ↔ tools.

`context.py` — сборка LLM-контекста из промптов, памяти, логов и состояния.
**Prompt caching** для Anthropic моделей через `cache_control` на статическом
контенте (~10K tokens). `compact_tool_history()` для сжатия старых tool results.

`tools/` — плагинная архитектура инструментов. Каждый модуль экспортирует
`get_tools()`, новые инструменты добавляются как отдельные файлы.
Включает `codebase_digest` — полный обзор кодовой базы за один вызов,
`browser.py` — browser automation (Playwright).

## Структура проекта

```
BIBLE.md                   — Философия и принципы (корень всего)
VERSION                    — Текущая версия (semver)
README.md                  — Это описание
requirements.txt           — Python-зависимости
prompts/
  SYSTEM.md                — Единый системный промпт Уробороса
supervisor/                — Пакет супервизора (декомпозированный launcher):
  __init__.py               — Экспорты
  state.py                  — State: load/save, budget tracking, status text, log rotation
  telegram.py               — TG client, markdown→HTML, send_with_budget, typing
  git_ops.py                — Git: checkout, reset, rescue, deps sync, safe_restart
  queue.py                  — Task queue: priority, enqueue, persist, timeouts, scheduling
  workers.py                — Worker lifecycle: spawn, kill, respawn, health, direct chat
  events.py                 — Event dispatch table: maps worker events to handlers
ouroboros/
  __init__.py              — Экспорт make_agent
  utils.py                 — Общие утилиты (нулевой уровень зависимостей)
  apply_patch.py           — Claude Code CLI apply_patch shim
  agent.py                 — Тонкий оркестратор: handle_task, event emission
  loop.py                  — LLM tool loop: concurrent execution, retry, cost tracking
  context.py               — Сборка контекста + prompt caching + compact_tool_history
  tools/                   — Пакет инструментов (плагинная архитектура):
    __init__.py             — Реэкспорт ToolRegistry, ToolContext
    registry.py             — Реестр: schemas, execute, auto-discovery
    core.py                 — Файловые операции + codebase_digest
    git.py                  — Git операции (commit, push, status, diff) + untracked warning
    shell.py                — Shell и Claude Code CLI
    search.py               — Web search
    control.py              — restart, promote, schedule, cancel, review, chat_history
    browser.py              — Browser: browse_page, browser_action (Playwright)
  llm.py                   — LLM-клиент: API вызовы, cached token tracking
  memory.py                — Память: scratchpad, identity, chat_history
  review.py                — Deep review: стратегическая рефлексия
colab_launcher.py          — Тонкий entry point: секреты → init → bootstrap → main loop
colab_bootstrap_shim.py    — Boot shim (вставляется в Colab, не меняется)
```

Структура не фиксирована — Уроборос может менять её по принципу самомодификации.

## Ветки GitHub

| Ветка | Кто | Назначение |
|-------|-----|------------|
| `main` | Владелец (Cursor) | Защищённая. Уроборос не трогает |
| `ouroboros` | Уроборос | Рабочая ветка. Все коммиты сюда |
| `ouroboros-stable` | Уроборос | Fallback при крашах. Обновляется через `promote_to_stable` |

## Команды Telegram

Обрабатываются супервизором (код):
- `/panic` — остановить всё немедленно
- `/restart` — мягкий перезапуск
- `/status` — статус воркеров, очереди, бюджета
- `/review` — запустить deep review
- `/evolve` — включить режим эволюции
- `/evolve stop` — выключить эволюцию

Все остальные сообщения идут в Уробороса (LLM-first, без роутера).

## Режим эволюции

`/evolve` включает непрерывные self-improvement циклы.
Каждый цикл: оценка → стратегический выбор → реализация → smoke test → Bible check → коммит.
Подробности в `prompts/SYSTEM.md`.

## Deep review

`/review` (владелец) или `request_review(reason)` (агент).
Стратегическая рефлексия: тренд сложности, направление эволюции,
соответствие Библии, метрики кода. Scope — на усмотрение Уробороса.

---

## Changelog

### 2.22.0 — Budget Guard: Round & Cost Limits

Prevents runaway tasks from burning the budget. Tasks that hit limits get a clean forced-finish instead of infinite loops.

- `ouroboros/loop.py`: `max_rounds` (default=50) + `budget_remaining_usd` (40% threshold) parameters
- When limit hit: LLM gets one final call WITHOUT tools to produce a summary
- `ouroboros/agent.py`: Reads budget from state.json, passes to loop
- Critical for budget conservation: previous tasks ran up to 125+ rounds unchecked

### 2.21.0 — True Process Restart via os.execv

Fixed the fundamental restart problem: `safe_restart` only updated code on disk but the supervisor kept old modules in memory. Workers spawned after restart still used stale code.

- `supervisor/events.py`: Agent `request_restart` now calls `os.execv()` to replace the entire process with fresh Python
- `colab_launcher.py`: Owner `/restart` also uses `os.execv()` for clean restart
- No boot shim changes needed — `os.execv()` keeps the same PID, `subprocess.run` in shim continues waiting
- Saves `tg_offset` and queue state before restart to prevent data loss
- This finally ensures ALL code changes take effect after restart — no more stale modules

### 2.20.0 — Lazy Spawn Context: True Hot-Reload

Fixed the REAL root cause of stale code in workers: `CTX = mp.get_context("spawn")` was cached at module import time. After `safe_restart`, supervisor reused the old context object, so workers were still spawned with stale state.

- `supervisor/workers.py`: `CTX` → lazy `_get_ctx()` + `get_event_q()`. Fresh spawn context created on every `spawn_workers()` call
- `colab_launcher.py`: Uses `get_event_q()` function instead of direct `EVENT_Q` import
- Verified: spawn workers now produce `cost_usd` and `cached_tokens` in events
- Prompt caching confirmed: 12.6K/21.5K tokens cached (59%) in smoke test
- This is the DEFINITIVE fix for the 6-cycle stale code saga

### 2.19.2 — Budget-Aware Context + Restart for Spawn

Added budget remaining info to LLM runtime context so agent can make cost-aware decisions.

- `ouroboros/context.py`: Budget injection (total/spent/remaining USD) into runtime context
- Restart to activate spawn workers (v2.19.0), prompt caching, tool argument compaction (v2.19.1)
- All improvements from v2.14.0-v2.19.1 now live in production
