# ============================
# Ouroboros — Local Launcher (DeepSeek API backend)
# ============================
# Drop-in replacement for colab_launcher.py that runs locally:
#   - No Google Colab / Google Drive dependencies
#   - Uses local filesystem for persistent state
#   - Uses DeepSeek API (OpenAI-compatible) as LLM backend
#
# Usage:
#   python local_launcher.py --data-dir ./local_data --repo-dir .
#   (or via deploy_local.sh which handles everything)

import argparse
import datetime
import json
import logging
import os
import pathlib
import re
import socket
import subprocess
import sys
import threading
import time
import types
import uuid
import queue as _queue_mod
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Conda's OpenSSL can't connect to Telegram (SSL handshake timeout).
# System curl works fine. Monkey-patch TelegramClient to use curl.
# ---------------------------------------------------------------------------
def _patch_telegram_client_with_curl():
    """Replace requests-based TelegramClient methods with curl-based ones."""
    from supervisor.telegram import TelegramClient as _TC

    def _curl_get_updates(self, offset: int, timeout: int = 10):
        url = f"{self.base}/getUpdates?offset={offset}&timeout={timeout}&allowed_updates=message&allowed_updates=edited_message"
        max_time = max(timeout + 5, 15)
        cmd = ["curl", "-s", "-f", "--max-time", str(max_time), url]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=max_time + 10)
        except subprocess.TimeoutExpired:
            raise RuntimeError("curl getUpdates subprocess timed out")
        if r.returncode != 0:
            raise RuntimeError(f"curl getUpdates failed (rc={r.returncode}): stderr={r.stderr!r}, stdout={r.stdout[:200]!r}")
        if not r.stdout.strip():
            raise RuntimeError("curl getUpdates returned empty response")
        data = json.loads(r.stdout)
        if data.get("ok") is not True:
            raise RuntimeError(f"Telegram getUpdates failed: {data}")
        return data.get("result") or []

    def _curl_send_message(self, chat_id: int, text: str, parse_mode: str = ""):
        cmd = ["curl", "-s", "-f", "--max-time", "30", "-X", "POST",
               "--data-urlencode", f"chat_id={chat_id}",
               "--data-urlencode", f"text={text}",
               "--data-urlencode", "disable_web_page_preview=true"]
        if parse_mode:
            cmd += ["--data-urlencode", f"parse_mode={parse_mode}"]
        cmd.append(f"{self.base}/sendMessage")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
        except subprocess.TimeoutExpired:
            return False, "curl sendMessage timed out"
        if r.returncode != 0:
            return False, f"curl failed (rc={r.returncode}): {r.stderr!r}"
        data = json.loads(r.stdout)
        if data.get("ok") is True:
            return True, "ok"
        return False, f"telegram_api_error: {data}"

    def _curl_send_chat_action(self, chat_id: int, action: str = "typing"):
        cmd = ["curl", "-s", "--max-time", "5", "-X", "POST",
               "-d", f"chat_id={chat_id}", "-d", f"action={action}",
               f"{self.base}/sendChatAction"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            return r.returncode == 0
        except Exception:
            return False

    _TC.get_updates = _curl_get_updates
    _TC.send_message = _curl_send_message
    _TC.send_chat_action = _curl_send_chat_action

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("ouroboros.local_launcher")

# ---------------------------------------------------------------------------
# 0) Parse CLI arguments
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Ouroboros — Local Launcher (DeepSeek)")
parser.add_argument("--data-dir", type=str, default="./local_data",
                    help="Path to persistent data directory (replaces Google Drive)")
parser.add_argument("--repo-dir", type=str, default=".",
                    help="Path to the Ouroboros repository")
args = parser.parse_args()

DRIVE_ROOT = pathlib.Path(args.data_dir).resolve()
REPO_DIR = pathlib.Path(args.repo_dir).resolve()

# ---------------------------------------------------------------------------
# 0.1) Load .env file
# ---------------------------------------------------------------------------
def load_dotenv(env_path: pathlib.Path) -> None:
    """Minimal .env loader — no external dependency needed."""
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Remove surrounding quotes if present
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(key, value)

env_file = REPO_DIR / ".env"
load_dotenv(env_file)

# ---------------------------------------------------------------------------
# 0.2) Install launcher deps (if not in venv already)
# ---------------------------------------------------------------------------
def install_launcher_deps() -> None:
    try:
        import openai  # noqa: F401
        import requests  # noqa: F401
    except ImportError:
        log.info("Installing missing dependencies...")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "openai>=1.0.0", "requests"],
            check=True,
        )

install_launcher_deps()

# ---------------------------------------------------------------------------
# 0.3) Patch the LLM client to use DeepSeek
# ---------------------------------------------------------------------------
# The LLM client (ouroboros/llm.py) uses OpenAI SDK with configurable base_url.
# DeepSeek API is fully OpenAI-compatible, so we just need to override the
# base_url and API key before the client is initialized.

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")

# Map DEEPSEEK_API_KEY → OPENROUTER_API_KEY (the LLM client reads this)
if DEEPSEEK_API_KEY:
    os.environ["OPENROUTER_API_KEY"] = DEEPSEEK_API_KEY

# Store base URL for monkey-patching the LLM client later
os.environ["OUROBOROS_LLM_BASE_URL"] = DEEPSEEK_BASE_URL

# ---------------------------------------------------------------------------
# 0.4) Provide apply_patch shim
# ---------------------------------------------------------------------------
sys.path.insert(0, str(REPO_DIR))

from ouroboros.apply_patch import install as install_apply_patch
from ouroboros.llm import LLMClient, DEFAULT_LIGHT_MODEL
install_apply_patch()

# ---------------------------------------------------------------------------
# Monkey-patch LLMClient to use DeepSeek base URL and strip OpenRouter-specific
# headers / extra_body parameters that DeepSeek doesn't understand.
# ---------------------------------------------------------------------------
_original_init = LLMClient.__init__

def _patched_init(self, api_key=None, base_url="https://openrouter.ai/api/v1"):
    override_url = os.environ.get("OUROBOROS_LLM_BASE_URL", "")
    if override_url:
        base_url = override_url
    _original_init(self, api_key=api_key, base_url=base_url)

LLMClient.__init__ = _patched_init

# Patch _get_client to remove OpenRouter-specific headers
_original_get_client = LLMClient._get_client

def _patched_get_client(self):
    if self._client is None:
        from openai import OpenAI
        self._client = OpenAI(
            base_url=self._base_url,
            api_key=self._api_key,
        )
    return self._client

LLMClient._get_client = _patched_get_client

# Patch chat() to strip OpenRouter-specific extra_body fields
_original_chat = LLMClient.chat

def _patched_chat(self, messages, model, tools=None, reasoning_effort="medium",
                  max_tokens=16384, tool_choice="auto"):
    """
    DeepSeek-compatible chat wrapper.
    Strips OpenRouter-specific fields (reasoning, provider, cache_control).
    Clamps max_tokens to DeepSeek limit (8192).
    Ensures reasoning_content field for deepseek-reasoner multi-turn.
    """
    from ouroboros.llm import normalize_reasoning_effort
    client = self._get_client()

    # DeepSeek max_tokens limit is 8192
    max_tokens = min(max_tokens, 8192)

    # For deepseek-reasoner: assistant messages MUST have reasoning_content field
    # See https://api-docs.deepseek.com/guides/thinking_mode#tool-calls
    is_reasoner = "reasoner" in model
    clean_messages = []
    for m in messages:
        m_copy = dict(m)
        if is_reasoner and m_copy.get("role") == "assistant":
            # Preserve actual reasoning_content from previous rounds if present;
            # otherwise insert empty string for API compatibility
            if "reasoning_content" not in m_copy:
                m_copy["reasoning_content"] = ""
        clean_messages.append(m_copy)

    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": clean_messages,
        "max_tokens": max_tokens,
    }

    if tools:
        # Strip cache_control from tools (OpenRouter/Anthropic-specific)
        clean_tools = []
        for t in tools:
            t_copy = {k: v for k, v in t.items() if k != "cache_control"}
            clean_tools.append(t_copy)
        kwargs["tools"] = clean_tools
        kwargs["tool_choice"] = tool_choice

    import openai as _openai
    try:
        resp = client.chat.completions.create(**kwargs)
    except _openai.BadRequestError as e:
        log.warning("DeepSeek 400 Bad Request: %s | model=%s tools=%d max_tokens=%d",
                    e.message, model, len(tools or []), max_tokens)
        raise
    resp_dict = resp.model_dump()
    usage = resp_dict.get("usage") or {}
    choices = resp_dict.get("choices") or [{}]
    msg = (choices[0] if choices else {}).get("message") or {}

    # DeepSeek pricing (per 1M tokens, cache-miss / cache-hit):
    # deepseek-chat:     $0.27 input / $1.10 output  (cached: $0.07)
    # deepseek-reasoner: $0.55 input / $2.19 output  (cached: $0.14)
    if not usage.get("cost"):
        _pricing = {
            "deepseek-chat":     (0.27, 0.07, 1.10),
            "deepseek-reasoner": (0.55, 0.14, 2.19),
        }
        prices = _pricing.get(model)
        if prices:
            input_price, cached_price, output_price = prices
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            cached_tokens = int(usage.get("prompt_cache_hit_tokens") or
                               usage.get("cached_tokens") or 0)
            non_cached = max(0, prompt_tokens - cached_tokens)
            cost = (non_cached * input_price / 1_000_000
                    + cached_tokens * cached_price / 1_000_000
                    + completion_tokens * output_price / 1_000_000)
            usage["cost"] = round(cost, 6)

    # Map DeepSeek cache fields to the format Ouroboros expects
    if usage.get("prompt_cache_hit_tokens") and not usage.get("cached_tokens"):
        usage["cached_tokens"] = int(usage["prompt_cache_hit_tokens"])

    return msg, usage

LLMClient.chat = _patched_chat

# Patch _fetch_generation_cost to no-op (OpenRouter-specific)
LLMClient._fetch_generation_cost = lambda self, gen_id: None

# Patch fetch_openrouter_pricing to return DeepSeek pricing
import ouroboros.llm as _llm_module
def _deepseek_pricing():
    return {
        "deepseek-chat":     (0.27, 0.07, 1.10),
        "deepseek-reasoner": (0.55, 0.14, 2.19),
    }
_llm_module.fetch_openrouter_pricing = _deepseek_pricing

log.info("LLM client patched for DeepSeek API (%s)", DEEPSEEK_BASE_URL)

# ---------------------------------------------------------------------------
# 1) Secrets + runtime config (no Colab — all from environment)
# ---------------------------------------------------------------------------
def get_secret(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    v = os.environ.get(name, default)
    if required:
        assert v is not None and str(v).strip() != "", f"Missing required env var: {name}"
    return v

def get_cfg(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    if v is not None and str(v).strip() != "":
        return v
    return default

def _parse_int_cfg(raw: Optional[str], default: int, minimum: int = 0) -> int:
    try:
        val = int(str(raw))
    except Exception:
        val = default
    return max(minimum, val)

OPENROUTER_API_KEY = get_secret("OPENROUTER_API_KEY", required=True)
TELEGRAM_BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN", required=True)
TOTAL_BUDGET_DEFAULT = get_secret("TOTAL_BUDGET", required=True)
GITHUB_TOKEN = get_secret("GITHUB_TOKEN", required=True)

# Parse budget
try:
    _raw_budget = str(TOTAL_BUDGET_DEFAULT or "")
    _clean_budget = re.sub(r'[^0-9.\-]', '', _raw_budget)
    TOTAL_BUDGET_LIMIT = float(_clean_budget) if _clean_budget else 0.0
except Exception as e:
    log.warning("Failed to parse TOTAL_BUDGET (%r): %s", TOTAL_BUDGET_DEFAULT, e)
    TOTAL_BUDGET_LIMIT = 0.0

OPENAI_API_KEY = get_secret("OPENAI_API_KEY", default="")
ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY", default="")
GITHUB_USER = get_cfg("GITHUB_USER")
GITHUB_REPO = get_cfg("GITHUB_REPO", default="ouroboros")
assert GITHUB_USER and str(GITHUB_USER).strip(), "GITHUB_USER not set in .env"
assert GITHUB_REPO and str(GITHUB_REPO).strip(), "GITHUB_REPO not set in .env"

MAX_WORKERS = int(get_cfg("OUROBOROS_MAX_WORKERS", default="3") or "3")
MODEL_MAIN = get_cfg("OUROBOROS_MODEL", default="deepseek-chat")
MODEL_CODE = get_cfg("OUROBOROS_MODEL_CODE", default="deepseek-chat")
MODEL_LIGHT = get_cfg("OUROBOROS_MODEL_LIGHT", default="deepseek-chat")

BUDGET_REPORT_EVERY_MESSAGES = 10
SOFT_TIMEOUT_SEC = max(60, int(get_cfg("OUROBOROS_SOFT_TIMEOUT_SEC", default="600") or "600"))
HARD_TIMEOUT_SEC = max(120, int(get_cfg("OUROBOROS_HARD_TIMEOUT_SEC", default="1800") or "1800"))
DIAG_HEARTBEAT_SEC = _parse_int_cfg(
    get_cfg("OUROBOROS_DIAG_HEARTBEAT_SEC", default="30"), default=30, minimum=0)
DIAG_SLOW_CYCLE_SEC = _parse_int_cfg(
    get_cfg("OUROBOROS_DIAG_SLOW_CYCLE_SEC", default="20"), default=20, minimum=0)

# Export to env for subprocesses (workers read these)
os.environ["OPENROUTER_API_KEY"] = str(OPENROUTER_API_KEY)
os.environ["OPENAI_API_KEY"] = str(OPENAI_API_KEY or "")
os.environ["ANTHROPIC_API_KEY"] = str(ANTHROPIC_API_KEY or "")
os.environ["GITHUB_USER"] = str(GITHUB_USER)
os.environ["GITHUB_REPO"] = str(GITHUB_REPO)
os.environ["OUROBOROS_MODEL"] = str(MODEL_MAIN)
os.environ["OUROBOROS_MODEL_CODE"] = str(MODEL_CODE)
if MODEL_LIGHT:
    os.environ["OUROBOROS_MODEL_LIGHT"] = str(MODEL_LIGHT)
# Override fallback list for DeepSeek API — OpenRouter models won't work here
if not os.environ.get("OUROBOROS_MODEL_FALLBACK_LIST"):
    os.environ["OUROBOROS_MODEL_FALLBACK_LIST"] = "deepseek-chat,deepseek-reasoner"
os.environ["OUROBOROS_DIAG_HEARTBEAT_SEC"] = str(DIAG_HEARTBEAT_SEC)
os.environ["OUROBOROS_DIAG_SLOW_CYCLE_SEC"] = str(DIAG_SLOW_CYCLE_SEC)
os.environ["TELEGRAM_BOT_TOKEN"] = str(TELEGRAM_BOT_TOKEN)
os.environ["OUROBOROS_REPO_DIR"] = str(REPO_DIR)

# ---------------------------------------------------------------------------
# 2) Create local data directories (replaces Google Drive mount)
# ---------------------------------------------------------------------------
for sub in ["state", "logs", "memory", "memory/owner_mailbox", "index", "locks", "archive"]:
    (DRIVE_ROOT / sub).mkdir(parents=True, exist_ok=True)
REPO_DIR.mkdir(parents=True, exist_ok=True)

# Clear stale owner mailbox files
try:
    from ouroboros.owner_inject import get_pending_path
    _stale_inject = get_pending_path(DRIVE_ROOT)
    if _stale_inject.exists():
        _stale_inject.unlink(missing_ok=True)
    _mailbox_dir = DRIVE_ROOT / "memory" / "owner_mailbox"
    if _mailbox_dir.exists():
        for _f in _mailbox_dir.iterdir():
            _f.unlink(missing_ok=True)
except Exception:
    pass

CHAT_LOG_PATH = DRIVE_ROOT / "logs" / "chat.jsonl"
if not CHAT_LOG_PATH.exists():
    CHAT_LOG_PATH.write_text("", encoding="utf-8")

# ---------------------------------------------------------------------------
# 3) Git constants
# ---------------------------------------------------------------------------
BRANCH_DEV = os.environ.get("OUROBOROS_BRANCH_DEV", "claude/analyze-agent-reasoning-oMphu")
BRANCH_STABLE = os.environ.get("OUROBOROS_BRANCH_STABLE", "main")
REMOTE_URL = f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"

# ---------------------------------------------------------------------------
# 4) Initialize supervisor modules
# ---------------------------------------------------------------------------
from supervisor.state import (
    init as state_init, load_state, save_state, append_jsonl,
    update_budget_from_usage, status_text, rotate_chat_log_if_needed,
    init_state,
)
state_init(DRIVE_ROOT, TOTAL_BUDGET_LIMIT)
init_state()

# Enable evolution mode by default for local launcher
_st = load_state()
if not _st.get("evolution_mode_enabled"):
    _st["evolution_mode_enabled"] = True
    save_state(_st)
    log.info("Evolution mode enabled by default")

from supervisor.telegram import (
    init as telegram_init, TelegramClient, send_with_budget, log_chat,
)
_patch_telegram_client_with_curl()
log.info("TelegramClient patched to use curl (workaround for conda SSL)")
TG = TelegramClient(str(TELEGRAM_BOT_TOKEN))
telegram_init(
    drive_root=DRIVE_ROOT,
    total_budget_limit=TOTAL_BUDGET_LIMIT,
    budget_report_every=BUDGET_REPORT_EVERY_MESSAGES,
    tg_client=TG,
)

from supervisor.git_ops import (
    init as git_ops_init, ensure_repo_present, checkout_and_reset,
    sync_runtime_dependencies, import_test, safe_restart,
)
git_ops_init(
    repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, remote_url=REMOTE_URL,
    branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
)

from supervisor.queue import (
    enqueue_task, enforce_task_timeouts, enqueue_evolution_task_if_needed,
    persist_queue_snapshot, restore_pending_from_snapshot,
    cancel_task_by_id, queue_review_task, sort_pending,
)

from supervisor.workers import (
    init as workers_init, get_event_q, WORKERS, PENDING, RUNNING,
    spawn_workers, kill_workers, assign_tasks, ensure_workers_healthy,
    handle_chat_direct, _get_chat_agent, auto_resume_after_restart,
)
workers_init(
    repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, max_workers=MAX_WORKERS,
    soft_timeout=SOFT_TIMEOUT_SEC, hard_timeout=HARD_TIMEOUT_SEC,
    total_budget_limit=TOTAL_BUDGET_LIMIT,
    branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
)

from supervisor.events import dispatch_event

# ---------------------------------------------------------------------------
# 5) Bootstrap repo (local mode — skip safe_restart to avoid branch reset)
# ---------------------------------------------------------------------------
ensure_repo_present()
# safe_restart disabled for local mode: it resets to main even with BRANCH_DEV set
# because import_test() may fail in non-Colab environments.
# Instead, just stay on the current branch.
log.info("Local mode: skipping safe_restart, staying on current branch")

# ---------------------------------------------------------------------------
# 6) Start workers
# ---------------------------------------------------------------------------
kill_workers()
spawn_workers(MAX_WORKERS)
restored_pending = restore_pending_from_snapshot()
persist_queue_snapshot(reason="startup")
if restored_pending > 0:
    st_boot = load_state()
    if st_boot.get("owner_chat_id"):
        send_with_budget(int(st_boot["owner_chat_id"]),
                         f"♻️ Restored pending queue from snapshot: {restored_pending} tasks.")

append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "type": "launcher_start",
    "runtime": "local_deepseek",
    "branch": load_state().get("current_branch"),
    "sha": load_state().get("current_sha"),
    "max_workers": MAX_WORKERS,
    "model_default": MODEL_MAIN, "model_code": MODEL_CODE, "model_light": MODEL_LIGHT,
    "llm_base_url": DEEPSEEK_BASE_URL,
    "soft_timeout_sec": SOFT_TIMEOUT_SEC, "hard_timeout_sec": HARD_TIMEOUT_SEC,
    "worker_start_method": str(os.environ.get("OUROBOROS_WORKER_START_METHOD") or ""),
    "diag_heartbeat_sec": DIAG_HEARTBEAT_SEC,
    "diag_slow_cycle_sec": DIAG_SLOW_CYCLE_SEC,
})

log.info("Ouroboros started (local, DeepSeek backend)")
log.info("  Model: %s | Code: %s | Light: %s", MODEL_MAIN, MODEL_CODE, MODEL_LIGHT)
log.info("  Workers: %d | Budget: $%.2f", MAX_WORKERS, TOTAL_BUDGET_LIMIT)
log.info("  Data: %s", DRIVE_ROOT)

# ---------------------------------------------------------------------------
# 6.1) Auto-resume after restart
# ---------------------------------------------------------------------------
auto_resume_after_restart()

# ---------------------------------------------------------------------------
# 6.2) Direct-mode watchdog
# ---------------------------------------------------------------------------
def _chat_watchdog_loop():
    """Monitor direct-mode chat agent for hangs."""
    soft_warned = False
    while True:
        time.sleep(30)
        try:
            agent = _get_chat_agent()
            if not agent._busy:
                soft_warned = False
                continue

            now = time.time()
            idle_sec = now - agent._last_progress_ts
            total_sec = now - agent._task_started_ts

            if idle_sec >= HARD_TIMEOUT_SEC:
                st = load_state()
                if st.get("owner_chat_id"):
                    send_with_budget(
                        int(st["owner_chat_id"]),
                        f"⚠️ Task stuck ({int(total_sec)}s without progress). Restarting agent.",
                    )
                reset_chat_agent()
                soft_warned = False
                continue

            if idle_sec >= SOFT_TIMEOUT_SEC and not soft_warned:
                soft_warned = True
                st = load_state()
                if st.get("owner_chat_id"):
                    send_with_budget(
                        int(st["owner_chat_id"]),
                        f"⏱️ Task running for {int(total_sec)}s, "
                        f"last progress {int(idle_sec)}s ago. Continuing.",
                    )
        except Exception:
            log.debug("Chat watchdog check failed", exc_info=True)

_watchdog_thread = threading.Thread(target=_chat_watchdog_loop, daemon=True)
_watchdog_thread.start()

# ---------------------------------------------------------------------------
# 6.3) Background consciousness
# ---------------------------------------------------------------------------
from ouroboros.consciousness import BackgroundConsciousness

def _get_owner_chat_id() -> Optional[int]:
    try:
        st = load_state()
        cid = st.get("owner_chat_id")
        return int(cid) if cid else None
    except Exception:
        return None

_consciousness = BackgroundConsciousness(
    drive_root=DRIVE_ROOT,
    repo_dir=REPO_DIR,
    event_queue=get_event_q(),
    owner_chat_id_fn=_get_owner_chat_id,
)

def reset_chat_agent():
    """Reset the direct-mode chat agent (called by watchdog on hangs)."""
    import supervisor.workers as _w
    _w._chat_agent = None

# ---------------------------------------------------------------------------
# 7) Main loop (identical to colab_launcher.py, minus Colab imports)
# ---------------------------------------------------------------------------
_event_ctx = types.SimpleNamespace(
    DRIVE_ROOT=DRIVE_ROOT,
    REPO_DIR=REPO_DIR,
    BRANCH_DEV=BRANCH_DEV,
    BRANCH_STABLE=BRANCH_STABLE,
    TG=TG,
    WORKERS=WORKERS,
    PENDING=PENDING,
    RUNNING=RUNNING,
    MAX_WORKERS=MAX_WORKERS,
    send_with_budget=send_with_budget,
    load_state=load_state,
    save_state=save_state,
    update_budget_from_usage=update_budget_from_usage,
    append_jsonl=append_jsonl,
    enqueue_task=enqueue_task,
    cancel_task_by_id=cancel_task_by_id,
    queue_review_task=queue_review_task,
    persist_queue_snapshot=persist_queue_snapshot,
    safe_restart=safe_restart,
    kill_workers=kill_workers,
    spawn_workers=spawn_workers,
    sort_pending=sort_pending,
    consciousness=_consciousness,
)


def _safe_qsize(q: Any) -> int:
    try:
        return int(q.qsize())
    except Exception:
        return -1


def _handle_supervisor_command(text: str, chat_id: int, tg_offset: int = 0):
    lowered = text.strip().lower()

    if lowered.startswith("/panic"):
        send_with_budget(chat_id, "🛑 PANIC: stopping everything now.")
        kill_workers()
        st2 = load_state()
        st2["tg_offset"] = tg_offset
        save_state(st2)
        raise SystemExit("PANIC")

    if lowered.startswith("/restart"):
        st2 = load_state()
        st2["session_id"] = uuid.uuid4().hex
        st2["tg_offset"] = tg_offset
        save_state(st2)
        send_with_budget(chat_id, "♻️ Restarting (soft).")
        # safe_restart disabled for local mode
        send_with_budget(chat_id, "♻️ Restart skipped in local mode (no branch reset).")
        return True
        # ok_r, msg_r = safe_restart(reason="owner_restart", unsynced_policy="rescue_and_reset")
        # if not ok_r:
        #     send_with_budget(chat_id, f"⚠️ Restart cancelled: {msg_r}")
        #     return True
        kill_workers()
        os.execv(sys.executable, [sys.executable, __file__,
                                  "--data-dir", str(DRIVE_ROOT),
                                  "--repo-dir", str(REPO_DIR)])

    if lowered.startswith("/status"):
        status = status_text(WORKERS, PENDING, RUNNING, SOFT_TIMEOUT_SEC, HARD_TIMEOUT_SEC)
        send_with_budget(chat_id, status, force_budget=True)
        return "[Supervisor handled /status — status text already sent to chat]\n"

    if lowered.startswith("/review"):
        queue_review_task(reason="owner:/review", force=True)
        return "[Supervisor handled /review — review task queued]\n"

    if lowered.startswith("/evolve"):
        parts = lowered.split()
        action = parts[1] if len(parts) > 1 else "on"
        turn_on = action not in ("off", "stop", "0")
        st2 = load_state()
        st2["evolution_mode_enabled"] = bool(turn_on)
        save_state(st2)
        if not turn_on:
            PENDING[:] = [t for t in PENDING if str(t.get("type")) != "evolution"]
            sort_pending()
            persist_queue_snapshot(reason="evolve_off")
        state_str = "ON" if turn_on else "OFF"
        send_with_budget(chat_id, f"🧬 Evolution: {state_str}")
        return f"[Supervisor handled /evolve — evolution toggled {state_str}]\n"

    if lowered.startswith("/bg"):
        parts = lowered.split()
        action = parts[1] if len(parts) > 1 else "status"
        if action in ("start", "on", "1"):
            result = _consciousness.start()
            send_with_budget(chat_id, f"🧠 {result}")
        elif action in ("stop", "off", "0"):
            result = _consciousness.stop()
            send_with_budget(chat_id, f"🧠 {result}")
        else:
            bg_status = "running" if _consciousness.is_running else "stopped"
            send_with_budget(chat_id, f"🧠 Background consciousness: {bg_status}")
        return f"[Supervisor handled /bg {action}]\n"

    return ""


# ---------------------------------------------------------------------------
# Main event loop
# ---------------------------------------------------------------------------
offset = int(load_state().get("tg_offset") or 0)
_last_diag_heartbeat_ts = 0.0
_last_message_ts: float = time.time()
_ACTIVE_MODE_SEC: int = 300

# Auto-start background consciousness — DISABLED for local testing
# try:
#     _consciousness.start()
#     log.info("🧠 Background consciousness auto-started")
# except Exception as e:
#     log.warning("consciousness auto-start failed: %s", e)
log.info("🧠 Background consciousness disabled for local mode")

log.info("Entering main loop — send a message to your Telegram bot to begin.")

while True:
    loop_started_ts = time.time()
    rotate_chat_log_if_needed(DRIVE_ROOT)
    ensure_workers_healthy()

    # Drain worker events
    event_q = get_event_q()
    while True:
        try:
            evt = event_q.get_nowait()
        except _queue_mod.Empty:
            break
        dispatch_event(evt, _event_ctx)

    enforce_task_timeouts()
    enqueue_evolution_task_if_needed()
    assign_tasks()
    persist_queue_snapshot(reason="main_loop")

    _now = time.time()
    _active = (_now - _last_message_ts) < _ACTIVE_MODE_SEC
    _poll_timeout = 0 if _active else 10
    try:
        updates = TG.get_updates(offset=offset, timeout=_poll_timeout)
    except Exception as e:
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "telegram_poll_error", "offset": offset, "error": repr(e),
            },
        )
        time.sleep(1.5)
        continue

    for upd in updates:
        offset = int(upd["update_id"]) + 1
        msg_upd = upd.get("message") or upd.get("edited_message") or {}
        if not msg_upd:
            continue

        chat_id = int(msg_upd["chat"]["id"])
        from_user = msg_upd.get("from") or {}
        user_id = int(from_user.get("id") or 0)
        text = str(msg_upd.get("text") or "")
        caption = str(msg_upd.get("caption") or "")
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        # Extract image if present
        image_data = None
        if msg_upd.get("photo"):
            best_photo = msg_upd["photo"][-1]
            file_id = best_photo.get("file_id")
            if file_id:
                b64, mime = TG.download_file_base64(file_id)
                if b64:
                    image_data = (b64, mime, caption)
        elif msg_upd.get("document"):
            doc = msg_upd["document"]
            mime_type = str(doc.get("mime_type") or "")
            if mime_type.startswith("image/"):
                file_id = doc.get("file_id")
                if file_id:
                    b64, mime = TG.download_file_base64(file_id)
                    if b64:
                        image_data = (b64, mime, caption)

        st = load_state()
        if st.get("owner_id") is None:
            st["owner_id"] = user_id
            st["owner_chat_id"] = chat_id
            st["last_owner_message_at"] = now_iso
            save_state(st)
            log_chat("in", chat_id, user_id, text)
            send_with_budget(chat_id, "✅ Owner registered. Ouroboros online. (DeepSeek backend)")
            continue

        if user_id != int(st.get("owner_id")):
            continue

        log_chat("in", chat_id, user_id, text)
        st["last_owner_message_at"] = now_iso
        _last_message_ts = time.time()
        save_state(st)

        # Supervisor commands
        if text.strip().lower().startswith("/"):
            try:
                result = _handle_supervisor_command(text, chat_id, tg_offset=offset)
                if result is True:
                    continue
                elif result:
                    text = result + text
            except SystemExit:
                raise
            except Exception:
                log.warning("Supervisor command handler error", exc_info=True)

        if not text and not image_data:
            continue

        _consciousness.inject_observation(f"Owner message: {text[:100]}")

        # Save real user message as last task for evolution context
        _clean_text = text.strip()
        if (_clean_text
            and len(_clean_text) >= 10
            and not _clean_text.startswith("/")
            and not _clean_text.startswith("[")):
            _st_task = load_state()
            _st_task["last_user_task_text"] = _clean_text[:500]
            save_state(_st_task)

        # Cancel running evolution tasks — user's message takes priority
        _evo_cancelled = []
        for _rtid, _rinfo in list(RUNNING.items()):
            if str(_rinfo.get("task", {}).get("type") or "") == "evolution":
                if cancel_task_by_id(_rtid):
                    _evo_cancelled.append(_rtid)
        # Also drop pending evolution tasks
        _evo_pending_before = len(PENDING)
        PENDING[:] = [t for t in PENDING if str(t.get("type") or "") != "evolution"]
        if _evo_cancelled or len(PENDING) < _evo_pending_before:
            persist_queue_snapshot(reason="evolution_preempted_by_user")
            log.info("Evolution preempted by user message: cancelled=%s", _evo_cancelled)

        agent = _get_chat_agent()

        if agent._busy:
            if image_data:
                if text:
                    agent.inject_message(text)
                send_with_budget(chat_id, "📎 Photo received, but a task is in progress. Send again when I'm free.")
            elif text:
                agent.inject_message(text)
        else:
            # Batch-collect burst messages
            _BATCH_WINDOW_SEC = 1.5
            _EARLY_EXIT_SEC = 0.15
            _batch_start = time.time()
            _batch_deadline = _batch_start + _BATCH_WINDOW_SEC
            _batched_texts = [text] if text else []
            _batched_image = image_data

            _batch_state = load_state()
            _batch_state_dirty = False
            while time.time() < _batch_deadline:
                time.sleep(0.1)
                try:
                    _extra_updates = TG.get_updates(offset=offset, timeout=0) or []
                except Exception:
                    _extra_updates = []
                if not _extra_updates and (time.time() - _batch_start) < _EARLY_EXIT_SEC:
                    break
                for _upd in _extra_updates:
                    offset = max(offset, int(_upd.get("update_id", offset - 1)) + 1)
                    _msg2 = _upd.get("message") or _upd.get("edited_message") or {}
                    _uid2 = (_msg2.get("from") or {}).get("id")
                    _cid2 = (_msg2.get("chat") or {}).get("id")
                    _txt2 = _msg2.get("text") or _msg2.get("caption") or ""
                    if _uid2 and _batch_state.get("owner_id") and _uid2 == int(_batch_state["owner_id"]):
                        log_chat("in", _cid2, _uid2, _txt2)
                        _batch_state["last_owner_message_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
                        _batch_state_dirty = True
                        if _txt2.strip().lower().startswith("/"):
                            try:
                                _cmd_result = _handle_supervisor_command(_txt2, _cid2, tg_offset=offset)
                                if _cmd_result is True:
                                    continue
                                elif _cmd_result:
                                    _txt2 = _cmd_result + _txt2
                            except SystemExit:
                                raise
                            except Exception:
                                log.warning("Supervisor command in batch failed", exc_info=True)
                        if _txt2:
                            _batched_texts.append(_txt2)
                            _batch_deadline = max(_batch_deadline, time.time() + 0.3)
                        if not _batched_image:
                            _doc2 = _msg2.get("document") or {}
                            _photo2 = (_msg2.get("photo") or [None])[-1] or {}
                            _fid2 = _photo2.get("file_id") or _doc2.get("file_id")
                            if _fid2:
                                _b642, _mime2 = TG.download_file_base64(_fid2)
                                if _b642:
                                    _batched_image = (_b642, _mime2, _txt2)

            if _batch_state_dirty:
                save_state(_batch_state)

            if len(_batched_texts) > 1:
                final_text = "\n\n".join(_batched_texts)
                log.info("Message batch: %d messages merged", len(_batched_texts))
            elif _batched_texts:
                final_text = _batched_texts[0]
            else:
                final_text = text

            if agent._busy:
                if final_text:
                    agent.inject_message(final_text)
                if _batched_image:
                    send_with_budget(chat_id, "📎 Photo received, but a task is in progress. Send again when I'm free.")
            else:
                _consciousness.pause()
                def _run_task_and_resume(cid, txt, img):
                    try:
                        handle_chat_direct(cid, txt, img)
                    finally:
                        _consciousness.resume()
                _t = threading.Thread(
                    target=_run_task_and_resume,
                    args=(chat_id, final_text, _batched_image),
                    daemon=True,
                )
                try:
                    _t.start()
                except Exception as _te:
                    log.error("Failed to start chat thread: %s", _te)
                    _consciousness.resume()

    st = load_state()
    st["tg_offset"] = offset
    save_state(st)

    now_epoch = time.time()
    loop_duration_sec = now_epoch - loop_started_ts

    if DIAG_SLOW_CYCLE_SEC > 0 and loop_duration_sec >= float(DIAG_SLOW_CYCLE_SEC):
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "main_loop_slow_cycle",
                "duration_sec": round(loop_duration_sec, 3),
                "pending_count": len(PENDING),
                "running_count": len(RUNNING),
            },
        )

    if DIAG_HEARTBEAT_SEC > 0 and (now_epoch - _last_diag_heartbeat_ts) >= float(DIAG_HEARTBEAT_SEC):
        workers_total = len(WORKERS)
        workers_alive = sum(1 for w in WORKERS.values() if w.proc.is_alive())
        append_jsonl(
            DRIVE_ROOT / "logs" / "supervisor.jsonl",
            {
                "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "type": "main_loop_heartbeat",
                "offset": offset,
                "workers_total": workers_total,
                "workers_alive": workers_alive,
                "pending_count": len(PENDING),
                "running_count": len(RUNNING),
                "event_q_size": _safe_qsize(event_q),
                "running_task_ids": list(RUNNING.keys())[:5],
                "spent_usd": st.get("spent_usd"),
            },
        )
        _last_diag_heartbeat_ts = now_epoch

    _loop_sleep = 0.1 if (_now - _last_message_ts) < _ACTIVE_MODE_SEC else 0.5
    time.sleep(_loop_sleep)
