"""Build a CANONICAL archived usage-ledger chain for the F1 (#1195) workload.

Shared by `tests/test_startup_historical_audit_server.py` (the real-server
readiness-versus-history case) and runnable as a script for measurements.

The chain is produced by the shipped compactor (`compact_usage_ledger_locked`)
under the real monetary lock, so every segment, header, epoch step and
`source_sha256` pin is exactly what production writes — this module never
hand-authors an archive segment.  The first author's fixture reported
`archive_materialized: false`; `archived_attempt_ids()` therefore returned the
empty set immediately and the "967 segments" workload was never executed.

Everything here writes to a caller-supplied synthetic root.  It never reads or
touches the owner's data root.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time

if __package__ is None and str(Path(__file__).resolve().parents[1]) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _old_ts(age_days: float) -> str:
    moment = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=age_days)
    return moment.isoformat().replace("+00:00", "Z")


def _attempt_row(seq: int, attempt_id: str, task_id: str, ts: str, state: str) -> dict:
    """One valid attempt row (the ledger requires every chain to begin reserved)."""
    row = {
        "seq": seq,
        "ts": ts,
        "attempt_id": attempt_id,
        "kind": "attempt",
        "state": state,
        "task_id": task_id,
        "root_task_id": task_id,
        "model": "fixture::model",
        "provider": "fixture",
        "category": "agent",
        "source": "fixture",
    }
    if state in ("reserved", "dispatched"):
        row["reservation_upper_bound_usd"] = 0.002
    elif state == "released":
        row["reason"] = "fixture_release"
    else:
        row["cost_usd"] = 0.000123
        row["cost_final"] = True
        row["pricing_known"] = True
        row["prompt_tokens"] = 11
        row["completion_tokens"] = 7
    return row


# The ledger's state machine: reserved -> dispatched -> settled (3 rows), or the
# shorter reserved -> released (2 rows).  Both finals are foldable, so a chain
# of either shape is archivable.  `_chain_shapes` hits an EXACT row count with a
# mix of the two, which is what "N valid attempt rows per generation" means.
def _chain_shapes(rows: int) -> list[tuple[str, ...]]:
    long_chain = ("reserved", "dispatched", "settled")
    short_chain = ("reserved", "released")
    remainder = rows % 3
    shorts = 0 if remainder == 0 else (2 if remainder == 1 else 1)
    longs = (rows - 2 * shorts) // 3
    if longs < 0:
        longs, shorts = 0, rows // 2
    return [long_chain] * longs + [short_chain] * shorts


def _last_seq(ledger: Path) -> int:
    if not ledger.exists():
        return 0
    last = 0
    with ledger.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                last = int(json.loads(line).get("seq") or last)
            except ValueError:
                continue
    return last


def build_chain(root: Path, *, generations: int, rows_per_generation: int,
                progress_every: int = 0) -> dict:
    """Append + compact `generations` times, leaving a real archived chain.

    Returns bounded counting facts.  Raises if any pass fails to commit, so a
    fixture can never silently degrade into "no archive" the way the first
    author's run did.
    """
    from ouroboros.usage_compaction import compact_usage_ledger_locked
    from ouroboros.usage_ledger import ARCHIVE_SEGMENT_DIR_REL, LEDGER_REL, _locked

    ledger = root / LEDGER_REL
    ledger.parent.mkdir(parents=True, exist_ok=True)
    archived_ids: list[str] = []
    started = time.monotonic()
    for generation in range(1, generations + 1):
        seq = _last_seq(ledger)
        ts = _old_ts(30 + generations - generation)  # every row is far past the fold horizon
        lines = []
        for index, shape in enumerate(_chain_shapes(rows_per_generation)):
            attempt_id = f"a{generation:04d}-{index:06d}"
            archived_ids.append(attempt_id)
            for state in shape:
                seq += 1
                lines.append(json.dumps(
                    _attempt_row(seq, attempt_id, f"task-{index % 17:03d}", ts, state),
                    separators=(",", ":"),
                ))
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write("".join(line + "\n" for line in lines))
        with _locked(root) as heartbeat:
            receipt = compact_usage_ledger_locked(root, heartbeat=heartbeat)
        if receipt is None:
            raise RuntimeError(f"compaction pass {generation} did not commit; chain would be incomplete")
        if progress_every and generation % progress_every == 0:
            print(f"  generation {generation}/{generations} "
                  f"({time.monotonic() - started:.1f}s)", file=sys.stderr, flush=True)
    segments = sorted((root / ARCHIVE_SEGMENT_DIR_REL).glob("*.jsonl"))
    return {
        "generations": generations,
        "rows_per_generation": rows_per_generation,
        "attempt_chains_per_generation": len(_chain_shapes(rows_per_generation)),
        "rows_written_per_generation": sum(len(shape) for shape in _chain_shapes(rows_per_generation)),
        "archived_attempt_ids_expected": len(archived_ids),
        "segments_on_disk": len(segments),
        "archive_bytes": sum(path.stat().st_size for path in segments),
        "live_ledger_bytes": ledger.stat().st_size,
        "build_seconds": time.monotonic() - started,
        "first_archived_attempt_id": archived_ids[0] if archived_ids else "",
        "last_archived_attempt_id": archived_ids[-1] if archived_ids else "",
    }


def add_live_tail(root: Path, *, rows: int) -> list[str]:
    """Append recent (unfoldable) attempts so the live replay is non-empty too."""
    from ouroboros.usage_ledger import LEDGER_REL

    ledger = root / LEDGER_REL
    seq = _last_seq(ledger)
    ts = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    ids = []
    with ledger.open("a", encoding="utf-8") as handle:
        for index, shape in enumerate(_chain_shapes(rows)):
            attempt_id = f"live-{index:06d}"
            ids.append(attempt_id)
            for state in shape:
                seq += 1
                handle.write(json.dumps(
                    _attempt_row(seq, attempt_id, f"task-{index % 17:03d}", ts, state),
                    separators=(",", ":"),
                ) + "\n")
    return ids


def write_seal_manifests(root: Path, *, archived: list[str], live: list[str],
                         missing: int) -> dict:
    """Seal manifests over archived / live / absent attempt identities.

    The archived identities are the ones that force `archived_attempt_ids()` to
    run: they are absent from the live replay by construction.
    """
    counts = {"archived": 0, "live": 0, "missing": 0}
    calls = root / "observability" / "calls"

    def _write(attempt_id: str, index: int) -> None:
        directory = calls / f"task-{index % 17:03d}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{attempt_id}.json").write_text(json.dumps({
            "call_id": attempt_id,
            "task_id": f"task-{index % 17:03d}",
            "model_send_seal": {
                "attempt_id": attempt_id,
                "canonical_basis": "model_send_candidate_v1",
                "pre_redaction_sha256": hashlib.sha256(attempt_id.encode()).hexdigest(),
                "size_bytes": len(attempt_id),
            },
        }, separators=(",", ":")), encoding="utf-8")

    index = 0
    for attempt_id in archived:
        _write(attempt_id, index); index += 1; counts["archived"] += 1
    for attempt_id in live:
        _write(attempt_id, index); index += 1; counts["live"] += 1
    for offset in range(missing):
        _write(f"missing-{offset:06d}", index); index += 1; counts["missing"] += 1
    return counts


def prepare_root(root: Path, *, generations: int, rows_per_generation: int,
                 archived_seals: int, live_rows: int, live_seals: int,
                 missing_seals: int, progress_every: int = 0) -> dict:
    """Full F1 fixture: archived chain + live tail + the three seal identities."""
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "logs").mkdir(parents=True, exist_ok=True)
    facts = build_chain(root, generations=generations,
                        rows_per_generation=rows_per_generation,
                        progress_every=progress_every)
    live_ids = add_live_tail(root, rows=live_rows)
    # Archived identities are sampled across the whole chain, not just its head,
    # so the union really has to be materialised.
    per_generation = len(_chain_shapes(rows_per_generation))
    step = max(1, facts["archived_attempt_ids_expected"] // max(1, archived_seals))
    archived_sample = [
        f"a{(1 + (offset * step) // per_generation):04d}-{(offset * step) % per_generation:06d}"
        for offset in range(archived_seals)
    ]
    counts = write_seal_manifests(
        root, archived=archived_sample, live=live_ids[:live_seals], missing=missing_seals,
    )
    facts["seal_manifests"] = counts
    facts["live_tail_rows"] = live_rows
    return facts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--generations", type=int, required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--archived-seals", type=int, default=8)
    parser.add_argument("--live-rows", type=int, default=16)
    parser.add_argument("--live-seals", type=int, default=8)
    parser.add_argument("--missing-seals", type=int, default=2)
    parser.add_argument("--progress-every", type=int, default=0)
    args = parser.parse_args()
    root = Path(args.root)
    os.environ.setdefault("OUROBOROS_DATA_DIR", str(root))
    facts = prepare_root(
        root, generations=args.generations, rows_per_generation=args.rows,
        archived_seals=args.archived_seals, live_rows=args.live_rows,
        live_seals=args.live_seals, missing_seals=args.missing_seals,
        progress_every=args.progress_every,
    )
    print(json.dumps(facts, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
