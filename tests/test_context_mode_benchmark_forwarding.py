"""Context-mode provenance forwarding across isolated benchmark processes."""

import json

import devtools.benchmarks.terminal_bench.harbor_installed_agent as tb_agent


def test_terminal_bench_forwards_false_tombstone_and_keeps_authority_pair_atomic(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("OUROBOROS_CONTEXT_MODE", raising=False)
    monkeypatch.delenv("OUROBOROS_CONTEXT_MODE_AUTO_LOW", raising=False)
    settings = tmp_path / "settings.json"
    agent = lambda: tb_agent.OuroborosTerminalBenchAgent(  # noqa: E731
        logs_dir=tmp_path, host_settings_path=str(settings),
    )._container_env()

    settings.write_text(json.dumps({
        "OUROBOROS_CONTEXT_MODE": "low",
        "OUROBOROS_CONTEXT_MODE_AUTO_LOW": False,
    }), encoding="utf-8")
    env = agent()
    assert env["OUROBOROS_CONTEXT_MODE"] == "low"
    assert env["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "false"

    settings.write_text(json.dumps({
        "OUROBOROS_CONTEXT_MODE": "low",
        "OUROBOROS_CONTEXT_MODE_AUTO_LOW": "true",
    }), encoding="utf-8")
    legacy_env = agent()
    assert legacy_env["OUROBOROS_CONTEXT_MODE"] == "max"
    assert legacy_env["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "false"

    settings.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "low")
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE_AUTO_LOW", "true")
    inherited = agent()
    assert inherited["OUROBOROS_CONTEXT_MODE"] == "low"
    assert "OUROBOROS_CONTEXT_MODE_AUTO_LOW" not in inherited

    settings.write_text(json.dumps({
        "OUROBOROS_CONTEXT_MODE": "max",
        "OUROBOROS_CONTEXT_MODE_AUTO_LOW": "false",
    }), encoding="utf-8")
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE", "low")
    monkeypatch.delenv("OUROBOROS_CONTEXT_MODE_AUTO_LOW", raising=False)
    mixed_authority = agent()
    assert mixed_authority["OUROBOROS_CONTEXT_MODE"] == "low"
    assert "OUROBOROS_CONTEXT_MODE_AUTO_LOW" not in mixed_authority

    monkeypatch.delenv("OUROBOROS_CONTEXT_MODE", raising=False)
    monkeypatch.setenv("OUROBOROS_CONTEXT_MODE_AUTO_LOW", "true")
    settings.write_text(json.dumps({
        "OUROBOROS_CONTEXT_MODE": "low",
        "OUROBOROS_CONTEXT_MODE_AUTO_LOW": "false",
    }), encoding="utf-8")
    reverse_mixed = agent()
    assert reverse_mixed["OUROBOROS_CONTEXT_MODE"] == "low"
    assert reverse_mixed["OUROBOROS_CONTEXT_MODE_AUTO_LOW"] == "false"
