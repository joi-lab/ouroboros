# Уроборос

Самосоздающийся агент. Работает в Google Colab, общается через Telegram,
хранит код в GitHub, память — на Google Drive.

**Версия:** 4.23.0

---

## Быстрый старт

1. В Colab добавь Secrets:
   - `OPENROUTER_API_KEY` (обязательно)
   - `TELEGRAM_BOT_TOKEN` (обязательно)
   - `TOTAL_BUDGET` (обязательно, в USD)
   - `GITHUB_TOKEN` (обязательно)
   - `OPENAI_API_KEY` (опционально — для web_search)
   - `ANTHROPIC_API_KEY` (опционально — для claude_code_edit)

2. Опционально добавь config-ячейку (модели, воркеры, диагностика):
```python
import os
CFG = {
    "GITHUB_USER": "razzant",
    "GITHUB_REPO": "ouroboros",
    "OUROBOROS_MODEL": "anthropic/claude-sonnet-4",
    "OUROBOROS_MODEL_CODE": "anthropic/claude-sonnet-4",
    "OUROBOROS_MODEL_LIGHT": "anthropic/claude-sonnet-4",
    "OUROBOROS_MAX_WORKERS": "5",
    "OUROBOROS_WORKER_START_METHOD": "fork",   # Colab-safe default
    "OUROBOROS_DIAG_HEARTBEAT_SEC": "30",      # periodic main_loop_heartbeat in supervisor.jsonl
    "OUROBOROS_DIAG_SLOW_CYCLE_SEC": "20",     # warns when one loop iteration is too slow
    "OUROBOROS_BG_BUDGET_PCT": "10",           # max % of budget for background consciousness
}
for k, v in CFG.items():
    os.environ[k] = str(v)
```
   Без этой ячейки используются дефолты: `openai/gpt-5.2` / `openai/gpt-5.2-codex`.
   Background consciousness использует OUROBOROS_MODEL_LIGHT (если не задано, то OUROBOROS_MODEL).
   Для диагностики зависаний смотри `main_loop_heartbeat`, `main_loop_slow_cycle`,
   `worker_dead_detected`, `worker_crash` в `/content/drive/MyDrive/Ouroboros/logs/supervisor.jsonl`.

3. Запусти boot shim (см. `colab_bootstrap_shim.py`).
4. Напиши боту в Telegram. Первый написавший — создатель.

## Архитектура

```
Telegram → colab_launcher.py (entry point)
               ↓
           supervisor/            (process management)
             state.py             — state, budget
             telegram.py          — TG client, formatting
             queue.py             — task queue, scheduling
             workers.py           — worker lifecycle, auto-resume
             git_ops.py           — git checkout, sync, rescue
             events.py            — event dispatch table
               ↓
           ouroboros/              (agent core)
             agent.py             — thin orchestrator
             consciousness.py     — background thinking loop
             context.py           — LLM context builder, prompt caching
             loop.py              — LLM tool loop, concurrent execution
             tools/               — plugin tool registry
               registry.py        — auto-discovery, schemas, execute
               core.py            — file ops (repo/drive read/write/list)
               git.py             — git ops (commit, push, status, diff)
               github.py          — GitHub Issues integration
               shell.py           — shell, Claude Code CLI
               search.py          — web search
               control.py         — restart, promote, schedule, review, switch_model
               browser.py         — Playwright browser automation (stealth)
               review.py          — multi-model code review
             llm.py               — LLM client (OpenRouter)
             memory.py            — scratchpad (free-form), identity, chat history
             review.py            — code collection, complexity metrics
             utils.py             — shared utilities (zero deps)
             apply_patch.py       — Claude Code patch shim
```

## Структура проекта

```
BIBLE.md                   — Конституция (корень всего)
VERSION                    — Текущая версия (semver)
README.md                  — Это описание
requirements.txt           — Python-зависимости
prompts/
  SYSTEM.md                — Системный промпт Уробороса
ouroboros/                  — Код агента (описание выше)
supervisor/                — Супервизор (описание выше)
colab_launcher.py          — Entry point (запускается из Colab)
colab_bootstrap_shim.py    — Boot shim (вставляется в Colab)
```

## Ветки GitHub

| Ветка | Кто | Назначение |
|-------|-----|------------|
| `main` | Создатель (Cursor) | Защищённая. Уроборос не трогает |
| `ouroboros` | Уроборос | Рабочая ветка. Все коммиты сюда |
| `ouroboros-stable` | Уроборос | Fallback при крашах. Обновляется через `promote_to_stable` |

## Команды Telegram

**Safety rail (hardcoded):**
- `/panic` — остановить всё немедленно

**Dual-path (supervisor + LLM):**
- `/restart` — перезапуск (os.execv — полная замена процесса)
- `/status` — статус воркеров, очереди, бюджета
- `/review` — запустить deep review
- `/evolve` — включить режим эволюции
- `/evolve stop` — выключить эволюцию
- `/bg start` — запустить background consciousness
- `/bg stop` — остановить background consciousness
- `/bg` — статус background consciousness

Dual-path: supervisor обрабатывает команду немедленно,
затем сообщение передаётся LLM для естественного ответа.
LLM также может вызывать эти действия через инструменты
(`toggle_evolution`, `toggle_consciousness`).

Все остальные сообщения идут в Уробороса (LLM-first).

## Режим эволюции

`/evolve` включает непрерывные self-improvement циклы.
Каждый цикл: оценка → стратегический выбор → реализация → smoke test →
Bible check → коммит. Подробности в `prompts/SYSTEM.md`.

Бюджет-гарды в supervisor (не в agent): эволюция автоматически
останавливается при 95% использования бюджета.

## Deep review

`/review` (создатель) или `request_review(reason)` (агент).
Стратегическая рефлексия по трём осям: код, понимание, идентичность.

---

## Changelog

### v4.23.0 — Empty Response Fallback
- Automatic fallback to gemini-2.5-pro when primary model returns empty responses 3x
- Raw empty response logging for debugging (llm_empty_response events)
- Configurable fallback model via OUROBOROS_MODEL_FALLBACK env var

### 4.22.0 — Empty Response Resilience + Budget Category Fix
- **Fix**: Empty LLM responses now properly retry with exponential backoff instead of silently failing
- **Fix**: Empty response retries now emit cost-tracking events (previously costs were lost on empty responses)
- **Fix**: Budget category now correctly maps all task types (evolution, consciousness, review, summarize) — was only distinguishing evolution vs task
- **Fix**: `rounds` counter only increments on successful responses (was counting empty retries as rounds)
- **Review**: Multi-model review (o3, Gemini 2.5 Pro) — caught missing event emission on retries

### 4.21.0 — Web Presence + Budget Categorization
- **New**: Landing page at https://razzant.github.io/ouroboros-webapp/ — matrix rain, genesis log, real-time typewriter, architecture diagram
- **New**: Separate public repo `ouroboros-webapp` for GitHub Pages deployment (main repo stays private)
- **New**: Budget categorization — LLM usage events now tagged with category (consciousness, task, evolution, review)
- **New**: `/status` shows budget breakdown by category and budget_total/budget_remaining
- **Security**: Added КРИТИЧЕСКИЕ ОГРАНИЧЕНИЯ to SYSTEM.md — never change repo visibility without explicit creator approval
- **Result**: Ouroboros now has a public web presence — first step outside Telegram

### 4.20.0 — Dialogue Summarization + Multi-Model Review for All Tasks
- **New tool**: `summarize_dialogue` — condenses chat history into key moments, decisions, creator preferences
- **New**: Dialogue summary auto-loaded into both agent context (20K chars) and consciousness context (4K chars)
- **New**: Consciousness has access to dialogue summary for better continuity across sessions
- **Policy**: Multi-model review now REQUIRED for ALL significant changes (not just evolution) — SYSTEM.md clarifies this applies to creator tasks too
- **New**: Review tracking workflow — after multi_model_review, mark "✅ Multi-model review passed" in commit/progress
- **Result**: Agent now has persistent knowledge of dialogue history without token bloat (summary vs raw logs)

### 4.19.0 — Model Profiles + Remove BG Model Hardcode
- **Removed**: `OUROBOROS_MODEL_BG` env var and DeepSeek hardcode — anti-minimalist, consciousness now uses `OUROBOROS_MODEL_LIGHT` (falls back to `OUROBOROS_MODEL`)
- **Removed**: DeepSeek and GPT-5-nano/mini from static pricing table (not used)
- **New**: Model profiles knowledge base — living document with experience-based assessments of each model's strengths, weaknesses, pricing, context length
- **Fix**: Consciousness default model fallback now sonnet-4 instead of deepseek
- **Updated**: Pricing table reordered by priority (opus-4.6 first, added sonnet-4.5 and grok-3-mini)

### 4.18.1 — Function Length Metrics Fix
- **Fix**: `compute_complexity_metrics` now uses indentation-based function boundary detection instead of next-`def` distance
- **Fix**: Eliminated false positives in `colab_launcher.py` where top-level code between functions was counted as function body
- **Result**: Zero oversized functions confirmed (was 2 false positives), longest function 104 lines

### 4.18.0 — GitHub Issues Integration
- **New**: 5 GitHub Issues tools — `list_github_issues`, `get_github_issue`, `comment_on_issue`, `close_github_issue`, `create_github_issue`
- **New**: Second input channel — creator/contributors can file Issues, Ouroboros discovers them via background consciousness
- **New**: Consciousness upgraded — Issues polling added to tool whitelist and CONSCIOUSNESS.md prompt
- **Security**: stdin-based body passing (prevents argument injection), input validation on issue numbers
- **Review**: Multi-model review (o3, Gemini 2.5 Pro) — drove stdin injection fix and input validation
- **Tests**: 87 smoke tests (was 82) — all green
