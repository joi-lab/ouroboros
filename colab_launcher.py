# ============================
# Ouroboros — Runtime launcher (entry point, executed from repository)
# ============================
# Thin orchestrator: secrets, bootstrap, main loop.
# Heavy logic lives in supervisor/ package.

import os, sys, json, time, uuid, pathlib, subprocess, datetime, threading
from typing import Any, Dict, List, Optional, Set, Tuple

# ----------------------------
# 0) Install launcher deps
# ----------------------------
def install_launcher_deps() -> None:
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-q", "openai>=1.0.0", "requests"],
        check=True,
    )

install_launcher_deps()

def ensure_claude_code_cli() -> bool:
    """Best-effort install of Claude Code CLI for Anthropic-powered code edits."""
    local_bin = str(pathlib.Path.home() / ".local" / "bin")
    if local_bin not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{local_bin}:{os.environ.get('PATH', '')}"

    has_cli = subprocess.run(["bash", "-lc", "command -v claude >/dev/null 2>&1"], check=False).returncode == 0
    if has_cli:
        return True

    subprocess.run(["bash", "-lc", "curl -fsSL https://claude.ai/install.sh | bash"], check=False)
    has_cli = subprocess.run(["bash", "-lc", "command -v claude >/dev/null 2>&1"], check=False).returncode == 0
    if has_cli:
        return True

    subprocess.run(["bash", "-lc", "command -v npm >/dev/null 2>&1 && npm install -g @anthropic-ai/claude-code"], check=False)
    has_cli = subprocess.run(["bash", "-lc", "command -v claude >/dev/null 2>&1"], check=False).returncode == 0
    return has_cli

# ----------------------------
# 0.1) provide apply_patch shim
# ----------------------------
APPLY_PATCH_PATH = pathlib.Path("/usr/local/bin/apply_patch")
APPLY_PATCH_CODE = r"""#!/usr/bin/env python3
import sys
import pathlib

def _norm_line(l: str) -> str:
    if l.startswith(" "):
        return l[1:]
    return l

def _find_subseq(hay, needle):
    if not needle:
        return 0
    n = len(needle)
    for i in range(0, len(hay) - n + 1):
        ok = True
        for j in range(n):
            if hay[i + j] != needle[j]:
                ok = False
                break
        if ok:
            return i
    return -1

def _find_subseq_rstrip(hay, needle):
    if not needle:
        return 0
    hay2 = [x.rstrip() for x in hay]
    needle2 = [x.rstrip() for x in needle]
    return _find_subseq(hay2, needle2)

def apply_update_file(path: str, hunks: list[list[str]]):
    p = pathlib.Path(path)
    if not p.exists():
        sys.stderr.write(f"apply_patch: file not found: {path}\n")
        sys.exit(2)

    text = p.read_text(encoding="utf-8")
    src = text.splitlines()

    for hunk in hunks:
        old_seq = []
        new_seq = []
        for line in hunk:
            if line.startswith("+"):
                new_seq.append(line[1:])
            elif line.startswith("-"):
                old_seq.append(line[1:])
            else:
                c = _norm_line(line)
                old_seq.append(c)
                new_seq.append(c)

        idx = _find_subseq(src, old_seq)
        if idx < 0:
            idx = _find_subseq_rstrip(src, old_seq)
        if idx < 0:
            sys.stderr.write("apply_patch: failed to match hunk in file: " + path + "\n")
            sys.stderr.write("HUNK (old_seq):\n" + "\n".join(old_seq) + "\n")
            sys.exit(3)

        src = src[:idx] + new_seq + src[idx + len(old_seq):]

    p.write_text("\n".join(src) + "\n", encoding="utf-8")

def main():
    lines = sys.stdin.read().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith("*** Begin Patch"):
            i += 1
            continue

        if line.startswith("*** Update File:"):
            path = line.split(":", 1)[1].strip()
            i += 1

            hunks = []
            cur = []
            while i < len(lines) and not lines[i].startswith("*** "):
                if lines[i].startswith("@@"):
                    if cur:
                        hunks.append(cur)
                        cur = []
                    i += 1
                    continue
                cur.append(lines[i])
                i += 1
            if cur:
                hunks.append(cur)

            apply_update_file(path, hunks)
            continue

        if line.startswith("*** End Patch"):
            i += 1
            continue

        i += 1

if __name__ == "__main__":
    main()
"""
APPLY_PATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
APPLY_PATCH_PATH.write_text(APPLY_PATCH_CODE, encoding="utf-8")
APPLY_PATCH_PATH.chmod(0o755)

# ----------------------------
# 1) Secrets + runtime config
# ----------------------------
from google.colab import userdata  # type: ignore
from google.colab import drive  # type: ignore

_LEGACY_CFG_WARNED: Set[str] = set()

def _userdata_get(name: str) -> Optional[str]:
    try:
        return userdata.get(name)
    except Exception:
        return None

def get_secret(name: str, default: Optional[str] = None, required: bool = False) -> Optional[str]:
    v = _userdata_get(name)
    if v is None or str(v).strip() == "":
        v = os.environ.get(name, default)
    if required:
        assert v is not None and str(v).strip() != "", f"Missing required secret: {name}"
    return v

def get_cfg(name: str, default: Optional[str] = None, allow_legacy_secret: bool = False) -> Optional[str]:
    v = os.environ.get(name)
    if v is not None and str(v).strip() != "":
        return v
    if allow_legacy_secret:
        legacy = _userdata_get(name)
        if legacy is not None and str(legacy).strip() != "":
            if name not in _LEGACY_CFG_WARNED:
                print(f"[cfg] DEPRECATED: move {name} from Colab Secrets to config cell/env.")
                _LEGACY_CFG_WARNED.add(name)
            return legacy
    return default

OPENROUTER_API_KEY = get_secret("OPENROUTER_API_KEY", required=True)
TELEGRAM_BOT_TOKEN = get_secret("TELEGRAM_BOT_TOKEN", required=True)
TOTAL_BUDGET_DEFAULT = get_secret("TOTAL_BUDGET", required=True)
GITHUB_TOKEN = get_secret("GITHUB_TOKEN", required=True)

try:
    TOTAL_BUDGET_LIMIT = float(TOTAL_BUDGET_DEFAULT)
except Exception:
    TOTAL_BUDGET_LIMIT = 0.0

OPENAI_API_KEY = get_secret("OPENAI_API_KEY", default="")
ANTHROPIC_API_KEY = get_secret("ANTHROPIC_API_KEY", default="")
GITHUB_USER = get_cfg("GITHUB_USER", default="razzant", allow_legacy_secret=True)
GITHUB_REPO = get_cfg("GITHUB_REPO", default="ouroboros", allow_legacy_secret=True)
MAX_WORKERS = int(get_cfg("OUROBOROS_MAX_WORKERS", default="5", allow_legacy_secret=True) or "5")
MODEL_MAIN = get_cfg("OUROBOROS_MODEL", default="openai/gpt-5.2", allow_legacy_secret=True)
MODEL_CODE = get_cfg("OUROBOROS_MODEL_CODE", default="openai/gpt-5.2-codex", allow_legacy_secret=True)

BUDGET_REPORT_EVERY_MESSAGES = 10
SOFT_TIMEOUT_SEC = max(60, int(get_cfg("OUROBOROS_SOFT_TIMEOUT_SEC", default="600", allow_legacy_secret=True) or "600"))
HARD_TIMEOUT_SEC = max(120, int(get_cfg("OUROBOROS_HARD_TIMEOUT_SEC", default="1800", allow_legacy_secret=True) or "1800"))

os.environ["OPENROUTER_API_KEY"] = str(OPENROUTER_API_KEY)
os.environ["OPENAI_API_KEY"] = str(OPENAI_API_KEY or "")
os.environ["ANTHROPIC_API_KEY"] = str(ANTHROPIC_API_KEY or "")
os.environ["GITHUB_USER"] = str(GITHUB_USER or "razzant")
os.environ["GITHUB_REPO"] = str(GITHUB_REPO or "ouroboros")
os.environ["OUROBOROS_MODEL"] = str(MODEL_MAIN or "openai/gpt-5.2")
os.environ["OUROBOROS_MODEL_CODE"] = str(MODEL_CODE or "openai/gpt-5.2-codex")
os.environ["TELEGRAM_BOT_TOKEN"] = str(TELEGRAM_BOT_TOKEN)

if str(ANTHROPIC_API_KEY or "").strip():
    ensure_claude_code_cli()

# ----------------------------
# 2) Mount Drive
# ----------------------------
if not pathlib.Path("/content/drive/MyDrive").exists():
    drive.mount("/content/drive")

DRIVE_ROOT = pathlib.Path("/content/drive/MyDrive/Ouroboros").resolve()
REPO_DIR = pathlib.Path("/content/ouroboros_repo").resolve()

for sub in ["state", "logs", "memory", "index", "locks", "archive"]:
    (DRIVE_ROOT / sub).mkdir(parents=True, exist_ok=True)
REPO_DIR.mkdir(parents=True, exist_ok=True)

CHAT_LOG_PATH = DRIVE_ROOT / "logs" / "chat.jsonl"
if not CHAT_LOG_PATH.exists():
    CHAT_LOG_PATH.write_text("", encoding="utf-8")

# ----------------------------
# 3) Git constants
# ----------------------------
BRANCH_DEV = "ouroboros"
BRANCH_STABLE = "ouroboros-stable"
REMOTE_URL = f"https://{GITHUB_TOKEN}:x-oauth-basic@github.com/{GITHUB_USER}/{GITHUB_REPO}.git"

# ----------------------------
# 4) Initialize supervisor modules
# ----------------------------
from supervisor.state import init as state_init, load_state, save_state, append_jsonl
state_init(DRIVE_ROOT)

from supervisor.telegram import (
    init as telegram_init, TelegramClient, send_with_budget, log_chat,
)
TG = TelegramClient(str(TELEGRAM_BOT_TOKEN))
telegram_init(
    drive_root=DRIVE_ROOT,
    total_budget_limit=TOTAL_BUDGET_LIMIT,
    budget_report_every=BUDGET_REPORT_EVERY_MESSAGES,
    tg_client=TG,
)

from supervisor.git_ops import (
    init as git_ops_init, ensure_repo_present, checkout_and_reset,
    sync_runtime_dependencies, import_test,
)
git_ops_init(
    repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, remote_url=REMOTE_URL,
    branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
)

from supervisor.workers import (
    init as workers_init, EVENT_Q, WORKERS, PENDING, RUNNING,
    spawn_workers, kill_workers, assign_tasks, ensure_workers_healthy,
    enforce_task_timeouts, enqueue_task, enqueue_evolution_task_if_needed,
    persist_queue_snapshot, restore_pending_from_snapshot,
    update_budget_from_usage, status_text, cancel_task_by_id,
    queue_review_task, rotate_chat_log_if_needed,
    handle_chat_direct, reset_chat_agent, _get_chat_agent,
    _sort_pending,
)
workers_init(
    repo_dir=REPO_DIR, drive_root=DRIVE_ROOT, max_workers=MAX_WORKERS,
    soft_timeout=SOFT_TIMEOUT_SEC, hard_timeout=HARD_TIMEOUT_SEC,
    total_budget_limit=TOTAL_BUDGET_LIMIT,
    branch_dev=BRANCH_DEV, branch_stable=BRANCH_STABLE,
)

# ----------------------------
# 5) Bootstrap repo
# ----------------------------
ensure_repo_present()
ok_dev, err_dev = checkout_and_reset(BRANCH_DEV, reason="bootstrap_dev", unsynced_policy="rescue_and_reset")
assert ok_dev, f"Failed to prepare {BRANCH_DEV}: {err_dev}"
deps_ok, deps_msg = sync_runtime_dependencies(reason="bootstrap_dev")
assert deps_ok, f"Failed to install runtime dependencies for {BRANCH_DEV}: {deps_msg}"
t = import_test()
if not t["ok"]:
    append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "type": "import_fail_dev",
        "stdout": t["stdout"], "stderr": t["stderr"],
    })
    ok_stable, err_stable = checkout_and_reset(BRANCH_STABLE, reason="bootstrap_fallback_stable",
                                                unsynced_policy="rescue_and_reset")
    assert ok_stable, f"Failed to prepare {BRANCH_STABLE}: {err_stable}"
    deps_ok_stable, deps_msg_stable = sync_runtime_dependencies(reason="bootstrap_fallback_stable")
    assert deps_ok_stable, f"Failed to install runtime dependencies for {BRANCH_STABLE}: {deps_msg_stable}"
    t2 = import_test()
    assert t2["ok"], f"Stable branch also failed import.\n\nSTDOUT:\n{t2['stdout']}\n\nSTDERR:\n{t2['stderr']}"

# ----------------------------
# 6) Start workers
# ----------------------------
kill_workers()
spawn_workers(MAX_WORKERS)
restored_pending = restore_pending_from_snapshot()
persist_queue_snapshot(reason="startup")
if restored_pending > 0:
    st_boot = load_state()
    if st_boot.get("owner_chat_id"):
        send_with_budget(int(st_boot["owner_chat_id"]),
                         f"♻️ Восстановил pending queue из snapshot: {restored_pending} задач.")

append_jsonl(DRIVE_ROOT / "logs" / "supervisor.jsonl", {
    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "type": "launcher_start",
    "branch": load_state().get("current_branch"),
    "sha": load_state().get("current_sha"),
    "max_workers": MAX_WORKERS,
    "model_default": MODEL_MAIN, "model_code": MODEL_CODE,
    "soft_timeout_sec": SOFT_TIMEOUT_SEC, "hard_timeout_sec": HARD_TIMEOUT_SEC,
})

# ----------------------------
# 7) Main loop
# ----------------------------
offset = int(load_state().get("tg_offset") or 0)

while True:
    rotate_chat_log_if_needed()
    ensure_workers_healthy()

    # Drain worker events
    while EVENT_Q.qsize() > 0:
        evt = EVENT_Q.get()
        et = evt.get("type")

        if et == "llm_usage":
            update_budget_from_usage(evt.get("usage") or {})
            continue

        if et == "task_heartbeat":
            task_id = str(evt.get("task_id") or "")
            if task_id and task_id in RUNNING:
                meta = RUNNING.get(task_id) or {}
                meta["last_heartbeat_at"] = time.time()
                phase = str(evt.get("phase") or "")
                if phase:
                    meta["heartbeat_phase"] = phase
                RUNNING[task_id] = meta
            continue

        if et == "typing_start":
            try:
                _chat_id = int(evt.get("chat_id") or 0)
                if _chat_id:
                    # Send typing action — supervisor handles it directly
                    TG.send_chat_action(_chat_id, "typing")
            except Exception:
                pass
            continue

        if et == "send_message":
            try:
                _log_text = evt.get("log_text")
                _fmt = str(evt.get("format") or "")
                send_with_budget(
                    int(evt["chat_id"]),
                    str(evt.get("text") or ""),
                    log_text=(str(_log_text) if isinstance(_log_text, str) else None),
                    fmt=_fmt,
                )
            except Exception as e:
                append_jsonl(
                    DRIVE_ROOT / "logs" / "supervisor.jsonl",
                    {
                        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                        "type": "send_message_event_error", "error": repr(e),
                    },
                )
            continue

        if et == "task_done":
            task_id = evt.get("task_id")
            wid = evt.get("worker_id")
            if task_id:
                RUNNING.pop(str(task_id), None)
            if wid in WORKERS and WORKERS[wid].busy_task_id == task_id:
                WORKERS[wid].busy_task_id = None
            persist_queue_snapshot(reason="task_done")
            continue

        if et == "task_metrics":
            append_jsonl(
                DRIVE_ROOT / "logs" / "supervisor.jsonl",
                {
                    "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "type": "task_metrics_event",
                    "task_id": str(evt.get("task_id") or ""),
                    "task_type": str(evt.get("task_type") or ""),
                    "duration_sec": round(float(evt.get("duration_sec") or 0.0), 3),
                    "tool_calls": int(evt.get("tool_calls") or 0),
                    "tool_errors": int(evt.get("tool_errors") or 0),
                },
            )
            continue

        if et == "review_request":
            queue_review_task(reason=str(evt.get("reason") or "agent_review_request"), force=False)
            continue

        if et == "restart_request":
            st = load_state()
            if st.get("owner_chat_id"):
                send_with_budget(int(st["owner_chat_id"]),
                                 f"♻️ Restart requested by agent: {evt.get('reason')}")
            ok_reset, msg_reset = checkout_and_reset(
                BRANCH_DEV, reason="agent_restart_request",
                unsynced_policy="rescue_and_block",
            )
            if not ok_reset:
                if st.get("owner_chat_id"):
                    send_with_budget(int(st["owner_chat_id"]),
                                     f"⚠️ Restart пропущен: {msg_reset}")
                continue
            deps_ok, deps_msg = sync_runtime_dependencies(reason="agent_restart_request")
            if not deps_ok:
                if st.get("owner_chat_id"):
                    send_with_budget(int(st["owner_chat_id"]),
                                     f"⚠️ Restart отменен: не удалось установить зависимости ({deps_msg}).")
                continue
            it = import_test()
            if not it["ok"]:
                ok_stable, msg_stable = checkout_and_reset(
                    BRANCH_STABLE, reason="agent_restart_import_fail",
                    unsynced_policy="rescue_and_reset",
                )
                if not ok_stable:
                    if st.get("owner_chat_id"):
                        send_with_budget(int(st["owner_chat_id"]),
                                         f"⚠️ Не удалось переключиться на {BRANCH_STABLE}: {msg_stable}")
                    continue
                deps_ok_stable, deps_msg_stable = sync_runtime_dependencies(
                    reason="agent_restart_import_fail_stable")
                if not deps_ok_stable:
                    if st.get("owner_chat_id"):
                        send_with_budget(int(st["owner_chat_id"]),
                                         f"⚠️ Не удалось установить зависимости в {BRANCH_STABLE}: {deps_msg_stable}")
                    continue
            kill_workers()
            reset_chat_agent()
            spawn_workers(MAX_WORKERS)
            continue

        if et == "promote_to_stable":
            try:
                import subprocess as sp
                sp.run(["git", "fetch", "origin"], cwd=str(REPO_DIR), check=True)
                sp.run(["git", "push", "origin", f"{BRANCH_DEV}:{BRANCH_STABLE}"],
                       cwd=str(REPO_DIR), check=True)
                new_sha = sp.run(["git", "rev-parse", f"origin/{BRANCH_STABLE}"],
                                  cwd=str(REPO_DIR), capture_output=True, text=True,
                                  check=True).stdout.strip()
                st = load_state()
                if st.get("owner_chat_id"):
                    send_with_budget(int(st["owner_chat_id"]),
                                     f"✅ Промоут: {BRANCH_DEV} → {BRANCH_STABLE} ({new_sha[:8]})")
            except Exception as e:
                st = load_state()
                if st.get("owner_chat_id"):
                    send_with_budget(int(st["owner_chat_id"]),
                                     f"❌ Ошибка промоута в stable: {e}")
            continue

        if et == "schedule_task":
            st = load_state()
            owner_chat_id = st.get("owner_chat_id")
            desc = str(evt.get("description") or "").strip()
            if owner_chat_id and desc:
                tid = uuid.uuid4().hex[:8]
                enqueue_task({"id": tid, "type": "task", "chat_id": int(owner_chat_id), "text": desc})
                send_with_budget(int(owner_chat_id), f"🗓️ Запланировал задачу {tid}: {desc}")
                persist_queue_snapshot(reason="schedule_task_event")
            continue

        if et == "cancel_task":
            task_id = str(evt.get("task_id") or "").strip()
            st = load_state()
            owner_chat_id = st.get("owner_chat_id")
            ok = cancel_task_by_id(task_id) if task_id else False
            if owner_chat_id:
                send_with_budget(int(owner_chat_id),
                                 f"{'✅' if ok else '❌'} cancel {task_id or '?'} (event)")
            continue

    enforce_task_timeouts()
    enqueue_evolution_task_if_needed()
    assign_tasks()
    persist_queue_snapshot(reason="main_loop")

    # Poll Telegram
    try:
        updates = TG.get_updates(offset=offset, timeout=10)
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
        msg = upd.get("message") or upd.get("edited_message") or {}
        if not msg:
            continue

        chat_id = int(msg["chat"]["id"])
        from_user = msg.get("from") or {}
        user_id = int(from_user.get("id") or 0)
        text = str(msg.get("text") or "")
        now_iso = datetime.datetime.now(datetime.timezone.utc).isoformat()

        st = load_state()
        if st.get("owner_id") is None:
            st["owner_id"] = user_id
            st["owner_chat_id"] = chat_id
            st["last_owner_message_at"] = now_iso
            save_state(st)
            log_chat("in", chat_id, user_id, text)
            send_with_budget(chat_id, "✅ Owner registered. Ouroboros online.")
            continue

        if user_id != int(st.get("owner_id")):
            continue

        log_chat("in", chat_id, user_id, text)
        st["last_owner_message_at"] = now_iso
        save_state(st)

        # Supervisor commands
        if text.strip().lower().startswith("/panic"):
            send_with_budget(chat_id, "🛑 PANIC: stopping everything now.")
            kill_workers()
            st2 = load_state()
            st2["tg_offset"] = offset
            save_state(st2)
            raise SystemExit("PANIC")

        if text.strip().lower().startswith("/restart"):
            st2 = load_state()
            st2["session_id"] = uuid.uuid4().hex
            save_state(st2)
            send_with_budget(chat_id, "♻️ Restarting (soft).")
            ok_reset, msg_reset = checkout_and_reset(
                BRANCH_DEV, reason="owner_restart",
                unsynced_policy="rescue_and_block",
            )
            if not ok_reset:
                send_with_budget(chat_id, f"⚠️ Restart отменен: {msg_reset}")
                continue
            deps_ok, deps_msg = sync_runtime_dependencies(reason="owner_restart")
            if not deps_ok:
                send_with_budget(chat_id, f"⚠️ Restart отменен: зависимости ({deps_msg}).")
                continue
            it = import_test()
            if not it["ok"]:
                ok_stable, msg_stable = checkout_and_reset(
                    BRANCH_STABLE, reason="owner_restart_import_fail",
                    unsynced_policy="rescue_and_reset",
                )
                if not ok_stable:
                    send_with_budget(chat_id, f"⚠️ Не удалось переключиться на {BRANCH_STABLE}: {msg_stable}")
                    continue
                deps_ok_s, deps_msg_s = sync_runtime_dependencies(
                    reason="owner_restart_import_fail_stable")
                if not deps_ok_s:
                    send_with_budget(chat_id, f"⚠️ Зависимости в {BRANCH_STABLE}: {deps_msg_s}")
                    continue
            kill_workers()
            reset_chat_agent()
            spawn_workers(MAX_WORKERS)
            continue

        if text.strip().lower().startswith("/status"):
            send_with_budget(chat_id, status_text(), force_budget=True)
            continue

        if text.strip().lower().startswith("/review"):
            queue_review_task(reason="owner:/review", force=True)
            continue

        lowered = text.strip().lower()
        if lowered.startswith("/evolve"):
            parts = lowered.split()
            action = parts[1] if len(parts) > 1 else "on"
            turn_on = action not in ("off", "stop", "0")
            st2 = load_state()
            st2["evolution_mode_enabled"] = bool(turn_on)
            save_state(st2)
            if not turn_on:
                PENDING[:] = [t for t in PENDING if str(t.get("type")) != "evolution"]
                _sort_pending()
                persist_queue_snapshot(reason="evolve_off")
            if turn_on:
                send_with_budget(chat_id, "🧬 Эволюция: ON. Отключить: /evolve stop")
            else:
                send_with_budget(chat_id, "🛑 Эволюция: OFF.")
            continue

        # All other messages → direct chat with Ouroboros
        agent = _get_chat_agent()
        if agent._busy:
            agent.inject_message(text)
        else:
            threading.Thread(
                target=handle_chat_direct,
                args=(chat_id, text),
                daemon=True,
            ).start()

    st = load_state()
    st["tg_offset"] = offset
    save_state(st)

    time.sleep(0.2)
