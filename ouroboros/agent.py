
"""
Ouroboros agent core (modifiable).

This module is intentionally self-contained (minimal dependencies) so that Ouroboros can edit it safely.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import json
import os
import pathlib
import subprocess
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


# -----------------------------
# Utilities
# -----------------------------

def utc_now_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat()


def sha256_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def read_text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: pathlib.Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def append_jsonl(path: pathlib.Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(obj, ensure_ascii=False)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def run(cmd: List[str], cwd: Optional[pathlib.Path] = None) -> str:
    res = subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True)
    if res.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(cmd)}\n\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}"
        )
    return res.stdout.strip()


def safe_relpath(p: str) -> str:
    p = p.replace("\\", "/").lstrip("/")
    if ".." in pathlib.PurePosixPath(p).parts:
        raise ValueError("Path traversal is not allowed.")
    return p


def truncate_for_log(s: str, max_chars: int = 4000) -> str:
    if len(s) <= max_chars:
        return s
    return s[: max_chars // 2] + "\n...\n" + s[-max_chars // 2 :]


def list_dir(root: pathlib.Path, rel: str, max_entries: int = 500) -> Dict[str, Any]:
    base = (root / safe_relpath(rel)).resolve()
    assert base.exists(), f"Path does not exist: {base}"
    assert base.is_dir(), f"Not a directory: {base}"
    out: List[Dict[str, Any]] = []
    for i, p in enumerate(sorted(base.rglob("*"))):
        if i >= max_entries:
            break
        out.append(
            {
                "path": str(p.relative_to(root)),
                "is_dir": p.is_dir(),
                "size": (p.stat().st_size if p.is_file() else None),
            }
        )
    return {"base": str(base), "count": len(out), "items": out, "truncated": (len(out) >= max_entries)}


# -----------------------------
# Environment + Paths
# -----------------------------

@dataclass(frozen=True)
class Env:
    repo_dir: pathlib.Path
    drive_root: pathlib.Path
    branch_dev: str = "ouroboros"
    branch_stable: str = "ouroboros-stable"

    def repo_path(self, rel: str) -> pathlib.Path:
        return (self.repo_dir / safe_relpath(rel)).resolve()

    def drive_path(self, rel: str) -> pathlib.Path:
        return (self.drive_root / safe_relpath(rel)).resolve()


# -----------------------------
# Agent
# -----------------------------

class OuroborosAgent:
    """
    One agent instance per worker process.

    Mostly stateless; long-term state lives on Drive.
    """

    def __init__(self, env: Env):
        self.env = env
        self._pending_events: List[Dict[str, Any]] = []

    def handle_task(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        self._pending_events = []

        drive_logs = self.env.drive_path("logs")
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_received", "task": task})

        base_prompt = read_text(self.env.repo_path("prompts/BASE.md"))
        world_md = read_text(self.env.repo_path("WORLD.md")) if self.env.repo_path("WORLD.md").exists() else ""
        readme_md = read_text(self.env.repo_path("README.md")) if self.env.repo_path("README.md").exists() else ""

        notes_path = self.env.drive_path("NOTES.md")
        notes_md = read_text(notes_path) if notes_path.exists() else ""

        state_path = self.env.drive_path("state/state.json")
        state_json = read_text(state_path) if state_path.exists() else "{}"

        index_summaries_path = self.env.drive_path("index/summaries.json")
        index_summaries = read_text(index_summaries_path) if index_summaries_path.exists() else ""

        chat_log_path = self.env.drive_path("logs/chat.jsonl")
        chat_log = read_text(chat_log_path) if chat_log_path.exists() else ""

        runtime_ctx = {
            "utc_now": utc_now_iso(),
            "repo_dir": str(self.env.repo_dir),
            "drive_root": str(self.env.drive_root),
            "git_head": self._git_head(),
            "git_branch": self._git_branch(),
            "task": {"id": task.get("id"), "type": task.get("type")},
        }

        messages = [
            {"role": "system", "content": base_prompt},
            {"role": "system", "content": "## WORLD.md\n\n" + world_md},
            {"role": "system", "content": "## README.md\n\n" + readme_md},
            {"role": "system", "content": "## Drive state (state/state.json)\n\n" + state_json},
            {"role": "system", "content": "## NOTES.md (Drive)\n\n" + notes_md},
            {"role": "system", "content": "## Index summaries (Drive: index/summaries.json)\n\n" + index_summaries},
            {"role": "system", "content": "## Runtime context (JSON)\n\n" + json.dumps(runtime_ctx, ensure_ascii=False, indent=2)},
            {"role": "system", "content": "## Raw chat log (Drive: logs/chat.jsonl)\n\n" + chat_log},
            {"role": "user", "content": task.get("text", "")},
        ]

        tools = self._tools_schema()
        text, usage = self._llm_with_tools(messages=messages, tools=tools)

        self._pending_events.append({
            "type": "llm_usage",
            "task_id": task.get("id"),
            "provider": "openrouter",
            "usage": usage,
            "ts": utc_now_iso(),
        })

        self._pending_events.append({
            "type": "send_message",
            "chat_id": task["chat_id"],
            "text": text,
            "task_id": task.get("id"),
            "ts": utc_now_iso(),
        })

        self._pending_events.append({"type": "task_done", "task_id": task.get("id"), "ts": utc_now_iso()})
        append_jsonl(drive_logs / "events.jsonl", {"ts": utc_now_iso(), "type": "task_done", "task_id": task.get("id")})
        return list(self._pending_events)

    # ---------- git helpers ----------

    def _git_head(self) -> str:
        return run(["git", "rev-parse", "HEAD"], cwd=self.env.repo_dir)

    def _git_branch(self) -> str:
        return run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.env.repo_dir)

    # ---------- tools + LLM loop ----------

    def _openrouter_client(self):
        from openai import OpenAI
        headers = {"HTTP-Referer": "https://colab.research.google.com/", "X-Title": "Ouroboros"}
        return OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ["OPENROUTER_API_KEY"], default_headers=headers)

    def _llm_with_tools(self, messages: List[Dict[str, Any]], tools: List[Dict[str, Any]]) -> Tuple[str, Dict[str, Any]]:
        model = os.environ.get("OUROBOROS_MODEL", "openai/gpt-5.2")
        client = self._openrouter_client()

        tool_name_to_fn = {
            "repo_read": self._tool_repo_read,
            "repo_list": self._tool_repo_list,
            "drive_read": self._tool_drive_read,
            "drive_list": self._tool_drive_list,
            "drive_write": self._tool_drive_write,
            "repo_write_commit": self._tool_repo_write_commit,
            "git_status": self._tool_git_status,
            "git_diff": self._tool_git_diff,
            "run_shell": self._tool_run_shell,
            "web_search": self._tool_web_search,
            "computer_use_browser": self._tool_computer_use_browser,
            "request_restart": self._tool_request_restart,
            "request_stable_promotion": self._tool_request_stable_promotion,
            "schedule_task": self._tool_schedule_task,
            "cancel_task": self._tool_cancel_task,
            "reindex_request": self._tool_reindex_request,
        }

        max_tool_rounds = int(os.environ.get("OUROBOROS_MAX_TOOL_ROUNDS", "20"))
        last_usage: Dict[str, Any] = {}

        for _ in range(max_tool_rounds):
            resp = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            resp_dict = resp.model_dump()
            last_usage = resp_dict.get("usage", {}) or {}

            choice = (resp_dict.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            tool_calls = msg.get("tool_calls") or []
            content = msg.get("content")

            if tool_calls:
                messages.append({"role": "assistant", "content": content or "", "tool_calls": tool_calls})
                for tc in tool_calls:
                    fn = tc["function"]["name"]
                    args = json.loads(tc["function"]["arguments"] or "{}")
                    result = tool_name_to_fn[fn](**args)
                    append_jsonl(
                        self.env.drive_path("logs") / "tools.jsonl",
                        {"ts": utc_now_iso(), "tool": fn, "args": args, "result_preview": truncate_for_log(result, 2000)},
                    )
                    messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
                continue

            return (content or ""), last_usage

        return "⚠️ Превышен лимит tool rounds. Остановился.", last_usage

    def _tools_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "repo_read",
                    "description": "Read a UTF-8 text file from the GitHub repo (relative path).",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "repo_list",
                    "description": "List files under a repo directory (relative path).",
                    "parameters": {
                        "type": "object",
                        "properties": {"dir": {"type": "string"}, "max_entries": {"type": "integer"}},
                        "required": ["dir"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "drive_read",
                    "description": "Read a UTF-8 text file from Google Drive root (relative path).",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "drive_list",
                    "description": "List files under a Drive directory (relative path).",
                    "parameters": {
                        "type": "object",
                        "properties": {"dir": {"type": "string"}, "max_entries": {"type": "integer"}},
                        "required": ["dir"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "drive_write",
                    "description": "Write a UTF-8 text file in Google Drive root (relative path).",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string"},
                            "content": {"type": "string"},
                            "mode": {"type": "string", "enum": ["overwrite", "append"]},
                        },
                        "required": ["path", "content", "mode"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "repo_write_commit",
                    "description": "Write a UTF-8 text file in repo, then git add/commit/push to ouroboros branch. Canonical self-modification.",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "content": {"type": "string"}, "commit_message": {"type": "string"}},
                        "required": ["path", "content", "commit_message"],
                    },
                },
            },
            {
                "type": "function",
                "function": {"name": "git_status", "description": "Run git status --porcelain in repo.", "parameters": {"type": "object", "properties": {}, "required": []}},
            },
            {
                "type": "function",
                "function": {"name": "git_diff", "description": "Run git diff in repo.", "parameters": {"type": "object", "properties": {}, "required": []}},
            },
            {
                "type": "function",
                "function": {
                    "name": "run_shell",
                    "description": "Run a shell command (list of args) inside the repo (dangerous; use carefully). Returns stdout+stderr.",
                    "parameters": {
                        "type": "object",
                        "properties": {"cmd": {"type": "array", "items": {"type": "string"}}, "cwd": {"type": "string"}},
                        "required": ["cmd"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "OpenAI web search via Responses API tool web_search (fresh web). Returns JSON with answer + sources.",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}, "allowed_domains": {"type": "array", "items": {"type": "string"}}},
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "computer_use_browser",
                    "description": "OpenAI computer use loop (browser) executed via Playwright. Returns JSON with result + artifacts.",
                    "parameters": {"type": "object", "properties": {"goal": {"type": "string"}, "max_steps": {"type": "integer"}}, "required": ["goal"]},
                },
            },
            {
                "type": "function",
                "function": {"name": "request_restart", "description": "Ask supervisor to restart Ouroboros runtime (apply new code).", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
            },
            {
                "type": "function",
                "function": {"name": "request_stable_promotion", "description": "Ask owner approval to promote current ouroboros HEAD to ouroboros-stable.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
            },
            {
                "type": "function",
                "function": {"name": "schedule_task", "description": "Schedule a background task (queued by supervisor).", "parameters": {"type": "object", "properties": {"description": {"type": "string"}}, "required": ["description"]}},
            },
            {
                "type": "function",
                "function": {"name": "cancel_task", "description": "Request supervisor to cancel a task by id.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
            },
            {
                "type": "function",
                "function": {"name": "reindex_request", "description": "Request owner approval to run full reindexing of summaries.", "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}},
            },
        ]

    # ---------- tool implementations ----------

    def _tool_repo_read(self, path: str) -> str:
        return read_text(self.env.repo_path(path))

    def _tool_repo_list(self, dir: str, max_entries: int = 500) -> str:
        return json.dumps(list_dir(self.env.repo_dir, dir, max_entries=max_entries), ensure_ascii=False, indent=2)

    def _tool_drive_read(self, path: str) -> str:
        return read_text(self.env.drive_path(path))

    def _tool_drive_list(self, dir: str, max_entries: int = 500) -> str:
        return json.dumps(list_dir(self.env.drive_root, dir, max_entries=max_entries), ensure_ascii=False, indent=2)

    def _tool_drive_write(self, path: str, content: str, mode: str) -> str:
        p = self.env.drive_path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if mode == "overwrite":
            p.write_text(content, encoding="utf-8")
        else:
            with p.open("a", encoding="utf-8") as f:
                f.write(content)
        return f"OK: wrote {mode} {path} ({len(content)} chars)"

    def _acquire_git_lock(self) -> pathlib.Path:
        lock_dir = self.env.drive_path("locks")
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / "git.lock"
        while lock_path.exists():
            time.sleep(0.5)
        lock_path.write_text(f"locked_at={utc_now_iso()}\n", encoding="utf-8")
        return lock_path

    def _release_git_lock(self, lock_path: pathlib.Path) -> None:
        if lock_path.exists():
            lock_path.unlink()

    def _tool_repo_write_commit(self, path: str, content: str, commit_message: str) -> str:
        assert commit_message.strip(), "commit_message must be non-empty"
        lock = self._acquire_git_lock()
        try:
            run(["git", "checkout", self.env.branch_dev], cwd=self.env.repo_dir)
            write_text(self.env.repo_path(path), content)
            run(["git", "add", safe_relpath(path)], cwd=self.env.repo_dir)
            run(["git", "commit", "-m", commit_message], cwd=self.env.repo_dir)
            run(["git", "push", "origin", self.env.branch_dev], cwd=self.env.repo_dir)
        finally:
            self._release_git_lock(lock)
        return f"OK: committed and pushed to {self.env.branch_dev}: {commit_message}"

    def _tool_git_status(self) -> str:
        return run(["git", "status", "--porcelain"], cwd=self.env.repo_dir)

    def _tool_git_diff(self) -> str:
        return run(["git", "diff"], cwd=self.env.repo_dir)

    def _tool_run_shell(self, cmd: List[str], cwd: str = "") -> str:
        wd = self.env.repo_dir if not cwd else (self.env.repo_dir / safe_relpath(cwd)).resolve()
        res = subprocess.run(cmd, cwd=str(wd), capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Shell failed: {' '.join(cmd)}\n\nSTDOUT:\n{res.stdout}\n\nSTDERR:\n{res.stderr}")
        return (res.stdout + "\n" + res.stderr).strip()

    def _tool_request_restart(self, reason: str) -> str:
        self._pending_events.append({"type": "restart_request", "reason": reason, "ts": utc_now_iso()})
        return f"Restart requested: {reason}"

    def _tool_request_stable_promotion(self, reason: str) -> str:
        self._pending_events.append({"type": "stable_promotion_request", "reason": reason, "ts": utc_now_iso()})
        return f"Stable promotion requested (needs owner approval): {reason}"

    def _tool_schedule_task(self, description: str) -> str:
        self._pending_events.append({"type": "schedule_task", "description": description, "ts": utc_now_iso()})
        return f"Scheduled task request: {description}"

    def _tool_cancel_task(self, task_id: str) -> str:
        self._pending_events.append({"type": "cancel_task", "task_id": task_id, "ts": utc_now_iso()})
        return f"Cancel requested for task_id={task_id}"

    def _tool_reindex_request(self, reason: str) -> str:
        self._pending_events.append({"type": "reindex_request", "reason": reason, "ts": utc_now_iso()})
        return f"Reindex requested (needs owner approval): {reason}"

    def _tool_web_search(self, query: str, allowed_domains: Optional[List[str]] = None) -> str:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return json.dumps({"error": "OPENAI_API_KEY is not set; web_search unavailable."}, ensure_ascii=False)

        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        tool: Dict[str, Any] = {"type": "web_search"}
        if allowed_domains:
            tool["filters"] = {"allowed_domains": allowed_domains}

        resp = client.responses.create(
            model=os.environ.get("OUROBOROS_WEBSEARCH_MODEL", "gpt-5"),
            tools=[tool],
            tool_choice="auto",
            include=["web_search_call.action.sources"],
            input=query,
        )
        d = resp.model_dump()

        sources: List[Dict[str, Any]] = []
        for item in d.get("output", []) or []:
            if item.get("type") == "web_search_call":
                action = item.get("action") or {}
                sources = action.get("sources") or []

        out = {"answer": d.get("output_text", ""), "sources": sources}
        return json.dumps(out, ensure_ascii=False, indent=2)

    def _tool_computer_use_browser(self, goal: str, max_steps: int = 40) -> str:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            return json.dumps({"error": "OPENAI_API_KEY is not set; computer_use unavailable."}, ensure_ascii=False)

        from openai import OpenAI
        client = OpenAI(api_key=api_key)

        from playwright.sync_api import sync_playwright

        display_width = int(os.environ.get("OUROBOROS_BROWSER_W", "1024"))
        display_height = int(os.environ.get("OUROBOROS_BROWSER_H", "768"))

        tool = {"type": "computer_use_preview", "display_width": display_width, "display_height": display_height, "environment": "browser"}

        artifacts_dir = self.env.drive_path("artifacts") / f"computer_use_{uuid.uuid4().hex[:8]}"
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        def _b64_png(png_bytes: bytes) -> str:
            return base64.b64encode(png_bytes).decode("ascii")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = browser.new_page(viewport={"width": display_width, "height": display_height})
            page.goto("about:blank")

            png0 = page.screenshot(full_page=False)
            screenshot0_url = f"data:image/png;base64,{_b64_png(png0)}"

            last = client.responses.create(
                model=os.environ.get("OUROBOROS_COMPUTER_MODEL", "computer-use-preview"),
                tools=[tool],
                reasoning={"summary": "concise"},
                truncation="auto",
                input=[{"role": "user", "content": [{"type": "input_text", "text": goal}, {"type": "input_image", "image_url": screenshot0_url}]}],
            )

            for step in range(max_steps):
                d = last.model_dump()
                computer_calls = [it for it in (d.get("output") or []) if it.get("type") == "computer_call"]
                if not computer_calls:
                    browser.close()
                    return json.dumps({"result": d.get("output_text", ""), "artifacts_dir": str(artifacts_dir), "steps": step}, ensure_ascii=False, indent=2)

                call = computer_calls[0]
                call_id = call.get("call_id")
                action = (call.get("action") or {})
                pending_checks = call.get("pending_safety_checks") or []

                atype = action.get("type")
                if atype == "click":
                    page.mouse.click(action["x"], action["y"], button=action.get("button", "left"))
                elif atype == "double_click":
                    page.mouse.dblclick(action["x"], action["y"], button=action.get("button", "left"))
                elif atype == "scroll":
                    page.mouse.move(action.get("x", 1), action.get("y", 1))
                    page.evaluate(f"window.scrollBy({int(action.get('scrollX', 0))}, {int(action.get('scrollY', 0))})")
                elif atype == "type":
                    page.keyboard.type(action.get("text", ""))
                elif atype == "keypress":
                    page.keyboard.press(action.get("key", "Enter"))
                elif atype == "wait":
                    time.sleep(float(action.get("seconds", 1)))
                else:
                    pass

                png = page.screenshot(full_page=False)
                (artifacts_dir / f"step_{step:03d}.png").write_bytes(png)

                last = client.responses.create(
                    model=os.environ.get("OUROBOROS_COMPUTER_MODEL", "computer-use-preview"),
                    previous_response_id=last.id,
                    tools=[tool],
                    truncation="auto",
                    input=[{
                        "type": "computer_call_output",
                        "call_id": call_id,
                        "acknowledged_safety_checks": pending_checks,
                        "output": {"type": "computer_screenshot", "image_url": f"data:image/png;base64,{_b64_png(png)}"},
                    }],
                )

            browser.close()

        d = last.model_dump()
        return json.dumps({"result": d.get("output_text", ""), "warning": "max_steps reached", "artifacts_dir": str(artifacts_dir)}, ensure_ascii=False, indent=2)


def make_agent(repo_dir: str, drive_root: str) -> OuroborosAgent:
    env = Env(repo_dir=pathlib.Path(repo_dir), drive_root=pathlib.Path(drive_root))
    return OuroborosAgent(env)


def smoke_test() -> str:
    required = ["prompts/BASE.md", "README.md", "WORLD.md"]
    return "OK: " + ", ".join(required)
