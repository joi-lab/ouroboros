"""Settings and config loading for Viktor-Friday orchestrator."""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import yaml


_DEFAULT_MODELS = {
    "solver_model": "o3",
    "tutor_model": "gpt-5-mini",
    "ocr_model": "gpt-4.1",
    "fallbacks": {
        "solver": ["gpt-5-mini"],
        "tutor": ["gpt-4.1"],
        "ocr": ["gpt-4.1-mini"],
    },
}

_DEFAULT_POLICY = {
    "no_direct_answer_before_attempt": True,
    "max_hint_depth": 2,
    "goodhart_thresholds": {
        "max_leakage_penalty": 0.2,
        "min_hidden_score": 0.45,
    },
    "setpoints": {
        "competency": 0.50,
        "transfer": 0.45,
        "horizon": 0.40,
        "error_signature": 0.45,
        "safety_agency": 0.80,
    },
    "setpoint_update": {
        "ewma_alpha": 0.15,
        "max_daily_drift": 0.05,
    },
    "stress_weights_ai": {
        "verifier_disagreement_rate": 0.25,
        "repeated_confusion_after_hints": 0.20,
        "direct_answer_pressure_incidents": 0.20,
        "latency_over_sla": 0.20,
        "non_transfer_recurrence": 0.15,
    },
    "stress_weights_viktor": {
        "idle_blocks_over_threshold": 0.35,
        "hint_to_progress_lag": 0.35,
        "repeated_error_signature": 0.30,
    },
}

_DEFAULT_BUDGET = {
    "monthly_cap_usd": 150.0,
    "per_session_soft_cap_usd": 8.0,
    "reserve_buckets": {
        "benchmark": 30.0,
        "interactive": 100.0,
        "safety_margin": 20.0,
    },
}


@dataclass(frozen=True)
class VFridaySettings:
    """Resolved runtime settings."""

    repo_root: Path
    config_dir: Path
    data_dir: Path
    db_path: Path
    audit_jsonl_path: Path
    models: Dict[str, Any]
    policy: Dict[str, Any]
    budget: Dict[str, Any]
    api_host: str
    api_port: int
    orchestrator_url: str
    telegram_bot_token: str


def _deep_update(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_update(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: Path, default: Dict[str, Any]) -> Dict[str, Any]:
    if not path.exists():
        return copy.deepcopy(default)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return copy.deepcopy(default)
        return _deep_update(default, raw)
    except Exception:
        return copy.deepcopy(default)


def load_settings(repo_root: Path | None = None) -> VFridaySettings:
    """Load settings from config files and environment."""
    root = (repo_root or Path(__file__).resolve().parent.parent).resolve()
    config_dir = root / "configs"
    data_dir = Path(os.environ.get("VFRIDAY_DATA_DIR", str(root / "data" / "vfriday"))).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    models = _load_yaml(config_dir / "vfriday_models.yaml", _DEFAULT_MODELS)
    policy = _load_yaml(config_dir / "vfriday_policy.yaml", _DEFAULT_POLICY)
    budget = _load_yaml(config_dir / "vfriday_budget.yaml", _DEFAULT_BUDGET)

    api_host = os.environ.get("VFRIDAY_API_HOST", "127.0.0.1")
    api_port = int(os.environ.get("VFRIDAY_API_PORT", "8080"))
    orchestrator_url = os.environ.get("VFRIDAY_ORCHESTRATOR_URL", f"http://{api_host}:{api_port}")
    telegram_bot_token = os.environ.get("VFRIDAY_TELEGRAM_BOT_TOKEN", "")

    return VFridaySettings(
        repo_root=root,
        config_dir=config_dir,
        data_dir=data_dir,
        db_path=data_dir / "vfriday.sqlite3",
        audit_jsonl_path=data_dir / "audit.jsonl",
        models=models,
        policy=policy,
        budget=budget,
        api_host=api_host,
        api_port=api_port,
        orchestrator_url=orchestrator_url,
        telegram_bot_token=telegram_bot_token,
    )
