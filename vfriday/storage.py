"""SQLite + JSONL storage for Viktor-Friday runtime."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


class Storage:
    """Persistence layer for sessions, pipeline runs, governance snapshots, and budget."""

    def __init__(self, db_path: Path, audit_jsonl_path: Path):
        self.db_path = Path(db_path).resolve()
        self.audit_jsonl_path = Path(audit_jsonl_path).resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.audit_jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterable[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        ddl = """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            student_alias TEXT NOT NULL,
            topic TEXT,
            grade_level TEXT,
            goal TEXT,
            active_setpoints_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_sessions (
            chat_id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS solver_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            latency_ms INTEGER NOT NULL,
            usage_json TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS verifier_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            checked_claims INTEGER NOT NULL,
            passed_claims INTEGER NOT NULL,
            failed_claims INTEGER NOT NULL,
            disagreement_rate REAL NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tutor_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            model TEXT NOT NULL,
            tutor_message TEXT NOT NULL,
            confidence REAL NOT NULL,
            requires_attempt INTEGER NOT NULL,
            flags_json TEXT NOT NULL,
            hidden_score REAL NOT NULL,
            leakage_penalty REAL NOT NULL,
            usage_json TEXT NOT NULL,
            latency_ms INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS setpoint_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            snapshot_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS stress_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            stress_ai REAL NOT NULL,
            stress_viktor REAL NOT NULL,
            factors_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS benchmark_runs (
            report_id TEXT PRIMARY KEY,
            candidate_models_json TEXT NOT NULL,
            sample_size INTEGER NOT NULL,
            summary_json TEXT NOT NULL,
            recommendation TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS budget_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL,
            session_id TEXT,
            category TEXT NOT NULL,
            model TEXT,
            amount_usd REAL NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        """
        with self._conn() as conn:
            conn.executescript(ddl)

    @staticmethod
    def _json(data: Dict[str, Any]) -> str:
        return json.dumps(data, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _as_dict(row: sqlite3.Row | None) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}

    def append_audit(
        self,
        *,
        trace_id: str,
        session_id: str,
        event_type: str,
        payload: Dict[str, Any],
    ) -> None:
        line = {
            "ts": _utc_now_iso(),
            "trace_id": trace_id,
            "session_id": session_id,
            "event_type": event_type,
            "payload": payload,
        }
        with self._lock:
            with self.audit_jsonl_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")

    def create_session(
        self,
        *,
        student_alias: str,
        topic: str | None,
        grade_level: str | None,
        goal: str | None,
        active_setpoints: Dict[str, float],
    ) -> Dict[str, Any]:
        session_id = uuid.uuid4().hex[:12]
        now = _utc_now_iso()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO sessions (
                    session_id, student_alias, topic, grade_level, goal,
                    active_setpoints_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    student_alias,
                    topic,
                    grade_level,
                    goal,
                    self._json(active_setpoints),
                    now,
                    now,
                ),
            )
        return {
            "session_id": session_id,
            "created_at": now,
            "active_setpoints": active_setpoints,
        }

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        out = self._as_dict(row)
        if not out:
            return None
        out["active_setpoints"] = json.loads(out.pop("active_setpoints_json"))
        return out

    def update_session_setpoints(self, session_id: str, setpoints: Dict[str, float]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                UPDATE sessions
                SET active_setpoints_json = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (self._json(setpoints), _utc_now_iso(), session_id),
            )

    def bind_chat_session(self, chat_id: int, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO chat_sessions (chat_id, session_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                """,
                (int(chat_id), session_id, _utc_now_iso()),
            )

    def get_chat_session(self, chat_id: int) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT session_id FROM chat_sessions WHERE chat_id = ?",
                (int(chat_id),),
            ).fetchone()
        return str(row["session_id"]) if row else None

    def save_event(self, trace_id: str, session_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        payload_json = self._json(payload)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO events (trace_id, session_id, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (trace_id, session_id, event_type, payload_json, _utc_now_iso()),
            )
        self.append_audit(
            trace_id=trace_id,
            session_id=session_id,
            event_type=event_type,
            payload=payload,
        )

    def save_solver_run(
        self,
        trace_id: str,
        session_id: str,
        model: str,
        status: str,
        latency_ms: int,
        usage: Dict[str, Any],
        response: Dict[str, Any],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO solver_runs (
                    trace_id, session_id, model, status, latency_ms,
                    usage_json, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    session_id,
                    model,
                    status,
                    int(latency_ms),
                    self._json(usage),
                    self._json(response),
                    _utc_now_iso(),
                ),
            )

    def save_verifier_run(
        self,
        trace_id: str,
        session_id: str,
        checked_claims: int,
        passed_claims: int,
        failed_claims: int,
        disagreement_rate: float,
        response: Dict[str, Any],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO verifier_runs (
                    trace_id, session_id, checked_claims, passed_claims,
                    failed_claims, disagreement_rate, response_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    session_id,
                    int(checked_claims),
                    int(passed_claims),
                    int(failed_claims),
                    float(disagreement_rate),
                    self._json(response),
                    _utc_now_iso(),
                ),
            )

    def save_tutor_turn(
        self,
        trace_id: str,
        session_id: str,
        model: str,
        tutor_message: str,
        confidence: float,
        requires_attempt: bool,
        flags: List[str],
        hidden_score: float,
        leakage_penalty: float,
        usage: Dict[str, Any],
        latency_ms: int,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tutor_turns (
                    trace_id, session_id, model, tutor_message, confidence,
                    requires_attempt, flags_json, hidden_score, leakage_penalty,
                    usage_json, latency_ms, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    session_id,
                    model,
                    tutor_message,
                    float(confidence),
                    1 if requires_attempt else 0,
                    self._json({"flags": flags}),
                    float(hidden_score),
                    float(leakage_penalty),
                    self._json(usage),
                    int(latency_ms),
                    _utc_now_iso(),
                ),
            )

    def save_setpoint_snapshot(self, session_id: str, snapshot: Dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO setpoint_snapshots (session_id, snapshot_json, created_at)
                VALUES (?, ?, ?)
                """,
                (session_id, self._json(snapshot), _utc_now_iso()),
            )

    def save_stress_snapshot(
        self,
        session_id: str,
        stress_ai: float,
        stress_viktor: float,
        factors: Dict[str, Any],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO stress_snapshots (
                    session_id, stress_ai, stress_viktor, factors_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    float(stress_ai),
                    float(stress_viktor),
                    self._json(factors),
                    _utc_now_iso(),
                ),
            )

    def get_latest_setpoints(self, session_id: str, fallback: Dict[str, float]) -> Dict[str, float]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT snapshot_json
                FROM setpoint_snapshots
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if row:
            snap = json.loads(row["snapshot_json"])
            if isinstance(snap, dict) and isinstance(snap.get("setpoints"), dict):
                return {k: float(v) for k, v in snap["setpoints"].items()}
        session = self.get_session(session_id)
        if session and isinstance(session.get("active_setpoints"), dict):
            return {k: float(v) for k, v in session["active_setpoints"].items()}
        return {k: float(v) for k, v in fallback.items()}

    def get_latest_stress(self, session_id: str) -> Dict[str, float]:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT stress_ai, stress_viktor
                FROM stress_snapshots
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        if not row:
            return {"stress_ai": 0.0, "stress_viktor": 0.0}
        return {
            "stress_ai": float(row["stress_ai"]),
            "stress_viktor": float(row["stress_viktor"]),
        }

    def get_recent_events(self, session_id: str, limit: int = 10) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT event_type, payload_json, created_at
                FROM events
                WHERE session_id = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (session_id, int(limit)),
            ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            out.append(
                {
                    "event_type": row["event_type"],
                    "created_at": row["created_at"],
                    "payload": payload,
                }
            )
        return out

    def add_budget_entry(
        self,
        trace_id: str,
        session_id: Optional[str],
        category: str,
        amount_usd: float,
        model: Optional[str],
        metadata: Dict[str, Any],
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO budget_ledger (
                    trace_id, session_id, category, model, amount_usd,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    trace_id,
                    session_id,
                    category,
                    model,
                    float(amount_usd),
                    self._json(metadata),
                    _utc_now_iso(),
                ),
            )

    def monthly_spent(self, now: Optional[datetime] = None) -> float:
        now_dt = now or _utc_now()
        month_start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount_usd), 0.0) AS total
                FROM budget_ledger
                WHERE created_at >= ?
                """,
                (month_start,),
            ).fetchone()
        return float(row["total"] if row else 0.0)

    def budget_snapshot(self, monthly_cap_usd: float, per_session_soft_cap_usd: float, session_id: str) -> Dict[str, Any]:
        month_spent = self.monthly_spent()
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT COALESCE(SUM(amount_usd), 0.0) AS total
                FROM budget_ledger
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        session_spent = float(row["total"] if row else 0.0)
        return {
            "monthly_cap_usd": float(monthly_cap_usd),
            "monthly_spent_usd": month_spent,
            "monthly_remaining_usd": max(0.0, float(monthly_cap_usd) - month_spent),
            "per_session_soft_cap_usd": float(per_session_soft_cap_usd),
            "session_spent_usd": session_spent,
        }

    def save_benchmark_run(
        self,
        report_id: str,
        candidate_models: List[str],
        sample_size: int,
        summary: Dict[str, Any],
        recommendation: str,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO benchmark_runs (
                    report_id, candidate_models_json, sample_size,
                    summary_json, recommendation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    report_id,
                    self._json({"models": candidate_models}),
                    int(sample_size),
                    self._json(summary),
                    recommendation,
                    _utc_now_iso(),
                ),
            )

    def run_retention(self, retention_days: int = 30) -> Dict[str, Any]:
        """Strip raw ingest payload fields older than retention window."""
        cutoff = (_utc_now() - timedelta(days=max(1, int(retention_days)))).isoformat()
        sanitized = 0
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT id, payload_json
                FROM events
                WHERE created_at < ?
                """,
                (cutoff,),
            ).fetchall()
            for row in rows:
                payload = json.loads(row["payload_json"])
                changed = False
                for raw_key in ("image_base64", "ocr_text", "latex_text", "problem_text"):
                    if raw_key in payload:
                        payload.pop(raw_key, None)
                        changed = True
                if changed:
                    conn.execute(
                        "UPDATE events SET payload_json = ? WHERE id = ?",
                        (self._json(payload), int(row["id"])),
                    )
                    sanitized += 1
        return {"cutoff": cutoff, "sanitized_rows": sanitized}

