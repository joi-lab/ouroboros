"""A ledger lock failure must preserve the response already paid for."""

import asyncio
import errno
import json

import pytest

from ouroboros import platform_layer, usage_accounting, usage_ledger


@pytest.mark.parametrize("asynchronous", [False, True], ids=["sync", "async"])
def test_paid_response_survives_kernel_lock_refusal(
    tmp_path, monkeypatch, caplog, asynchronous,
):
    root = tmp_path / "data"
    (root / "state").mkdir(parents=True)
    monkeypatch.setenv("OUROBOROS_DATA_DIR", str(root))
    monkeypatch.setenv("OUROBOROS_SETTINGS_PATH", str(root / "settings.json"))
    monkeypatch.setenv("TOTAL_BUDGET", "100")
    usage_accounting._reset_task_cache_splits()
    request = usage_accounting.AttemptRequest(
        model="openai/gpt-5.2", provider="openai", reservation_usd=1.0,
        drive_root=root, task_id="child", root_task_id="root", source="test",
    )
    assert platform_layer.kernel_file_locks_enforced(root / usage_ledger.LOCK_REL)
    response = {"content": "useful result", "usage": {
        "prompt_tokens": 3, "completion_tokens": 2,
    }}
    sends = 0
    refused = []
    actual_kernel_lock = platform_layer.file_lock_exclusive_nb

    def refuse_after_response(fd):
        if sends:
            refused.append(fd)
            raise OSError(errno.EIO, "ledger kernel lock unavailable after response")
        return actual_kernel_lock(fd)

    def send():
        nonlocal sends
        sends += 1
        return response

    async def send_async():
        return send()

    with monkeypatch.context() as failure:
        failure.setattr(platform_layer, "file_lock_exclusive_nb", refuse_after_response)
        if asynchronous:
            actual = asyncio.run(usage_accounting.execute_physical_attempt_async(
                request, send_async,
            ))
        else:
            actual = usage_accounting.execute_physical_attempt(request, send)

    assert actual is response
    assert sends == 1
    assert len(refused) >= 2  # Settlement and the fallback unresolved write both failed.
    assert "Failed to mark post-response accounting failure unresolved" in caplog.text
    rows = [json.loads(line) for line in (root / usage_ledger.LEDGER_REL).read_text().splitlines()]
    assert [row["state"] for row in rows] == ["reserved", "dispatched"]
    projection = usage_accounting.usage_projection(root)
    assert projection["unresolved_upper_bound_usd"] == 1.0
    assert projection["cost_final"] is False
    assert not (root / usage_ledger.LOCK_REL).exists()
    assert len({row["attempt_id"] for row in rows}) == 1
    usage_accounting._reset_task_cache_splits()
