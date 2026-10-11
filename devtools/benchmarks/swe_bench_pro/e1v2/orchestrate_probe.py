#!/usr/bin/env python3
"""Parallel orchestrator for the fixed-version SWE-bench Pro probe.

Drives N workers over a run CSV. Each task is one isolated `run_pro.py --limit 1
--reset-state --volume-suffix=-wN` invocation, so:
  * per-task hard-reset: --reset-state recreates the worker's obo-repo/obo-data
    volumes => every task starts pristine and independent (matches v25 per-instance
    independence; no cross-task contamination, incl. /obo-repo);
  * worker isolation: --volume-suffix=-wN gives each worker its own
    obo-repo-wN/obo-data-wN volumes + container names (no cross-worker races).

Disk-bounded by design: images are pulled just-in-time by run_pro, each task is
graded inline (official eval) right after it solves, then its task image is removed
(`docker rmi`) unless --keep-images. Peak disk stays ~workers*image instead of
40*image. The orchestrator owns the run-level $ cap (run_pro sees a high
per-invocation budget; the real ceiling is enforced here on accumulated spend),
merges per-task predictions/timeline, and writes a run manifest with per-task
new-vs-v25 verdicts.

  OPENROUTER_API_KEY=... OUROBOROS_BENCH_ALLOW_CONTAINER_SECRETS=1 \
  python orchestrate_probe.py --run-csv runs/probe/run_csv.csv --out-dir runs/probe \
    --workers 3 --solve-model anthropic/claude-sonnet-4.6 \
    --settings .../settings_sonnet46_probe.json --eval-repo .../SWE-bench_Pro-os
"""
from __future__ import annotations
import argparse, csv, hashlib, json, os, subprocess, sys, threading, time, pathlib

PRO = pathlib.Path(__file__).resolve().parent
SRC = pathlib.Path(__file__).resolve().parents[4]
if str(SRC) not in sys.path:  # run standalone — put the repo root on the path for the shared helpers
    sys.path.insert(0, str(SRC))
from devtools.benchmarks.common.run_roots import ensure_outside_repo
from ouroboros.platform_layer import kill_process_tree, subprocess_new_group_kwargs  # cross-platform process group/tree
RUN_PRO = PRO / "run_pro.py"
GRADE_PRO = PRO.parent / "grade_pro.py"
IMG_REPO = "jefzda/sweap-images"
DEFAULT_DOCKER_HOST = os.environ.get("DOCKER_HOST", "")  # portable: respect the operator's DOCKER_HOST; never hardcode a host-specific socket


def reap_timed_out_runpro(proc, worker: int, env: dict) -> None:
    """On a host timeout, kill the run_pro process TREE (cross-platform via platform_layer —
    not a raw POSIX os.killpg) AND reap any leaked solving container for THIS worker (run_pro
    names them ``obopro-w{worker}-*``). Factored out so it is unit-testable (review round-4)."""
    try:
        kill_process_tree(proc)
    except Exception:
        pass
    try:
        proc.wait(timeout=30)
    except Exception:
        pass
    try:
        ids = subprocess.run(["docker", "ps", "-q", "--filter", f"name=obopro-w{worker}-"],
                             capture_output=True, text=True, env=env, timeout=60).stdout.split()
        for cid in ids:
            subprocess.run(["docker", "rm", "-f", cid], capture_output=True, env=env, timeout=60)
    except Exception:
        pass


def read_graded_verdict(eval_dir: pathlib.Path, instance_id: str) -> tuple[str, str]:
    """Read one instance's verdict from ``grade_pro.py``'s TYPED artefact.

    ``grade_pro`` writes ``grade_summary.json`` (``verdicts[]`` with ``verdict`` in
    ``pass|fail|ungraded`` plus ``reason``/``tests``); this reads that structure instead of
    scraping the grader's console output. The previous log string-matching (``DIAGNOSTIC_PASS``
    and friends) silently returned ``ERR`` for EVERY task once the grader's printing changed,
    which reports a healthy run as a broken harness.
    Returns ``(verdict_in_probe_vocabulary, tests_column)``.
    """
    summary_path = pathlib.Path(eval_dir) / "grade_summary.json"
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return "ERR", ""
    rows = [r for r in (summary.get("verdicts") or []) if isinstance(r, dict)]
    row = next((r for r in rows if str(r.get("instance_id")) == str(instance_id)), None)
    if row is None and len(rows) == 1:
        row = rows[0]          # single-instance grading: accept the only row (prefixed vs bare id)
    if row is None:
        return "ERR", ""
    verdict = str(row.get("verdict") or "")
    reason = str(row.get("reason") or "")
    tests = str(row.get("tests") or "")
    if verdict == "pass":
        return "PASS", tests
    if verdict == "fail":
        return "FAIL", tests
    if verdict == "ungraded":
        return ("NO_OUTPUT" if reason == "no_official_output" else f"UNGRADED:{reason}" if reason else "UNGRADED"), tests
    return "ERR", tests


def grade_one(tdir: pathlib.Path, pred: pathlib.Path, iid: str, *,
              host_python: str, eval_repo: str, run_csv: pathlib.Path) -> tuple[str, str]:
    """Grade ONE task and return its verdict, or ``("ERR", "")`` — never a stale one.

    A task directory can already hold ``pro_eval/grade_summary.json`` from an earlier
    attempt (this orchestrator is rerun over partial runs, and ``--out-dir`` is reused).
    The summary is therefore REMOVED before the grader runs, and the verdict is accepted
    only when the grader both exited 0 AND produced that artefact in THIS attempt.
    Without those two conditions a grader that timed out or crashed left the previous
    PASS/FAIL in place and it was silently attributed to the new attempt — a wrong
    headline number with no error anywhere.
    """
    ev = tdir / "pro_eval"
    summary = ev / "grade_summary.json"
    try:
        summary.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        # Cannot guarantee freshness => refuse to read the file at all.
        (tdir / "grade.log").write_text(f"GRADE_STALE_SUMMARY_UNREMOVABLE {exc!r}\n", encoding="utf-8")
        return "ERR", ""
    cmd = [host_python, str(GRADE_PRO), "--predictions", str(pred),
           "--out-dir", str(ev), "--eval-repo", eval_repo,
           "--workers", "1", "--csv", str(run_csv)]
    rc = None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=2400, env=dict(os.environ))
        rc = r.returncode
        out = (r.stdout or "") + "\n" + (r.stderr or "")
    except subprocess.TimeoutExpired:
        out = "GRADE_TIMEOUT"
    except Exception as exc:
        out = f"GRADE_INVOCATION_FAILED {exc!r}"
    (tdir / "grade.log").write_text(out, encoding="utf-8")
    # The log stays for humans; the VERDICT comes from the typed artefact only, and only
    # when this invocation succeeded and actually wrote it.
    if rc != 0 or not summary.is_file():
        return "ERR", ""
    return read_graded_verdict(ev, iid)


def sha256(p: pathlib.Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def read_run_csv(path: pathlib.Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return sorted(csv.DictReader(f), key=lambda r: int(r["idx"]))


def resolve_tags(run_csv: pathlib.Path, host_py: str) -> dict:
    """instance_id (both prefixed + normalized) -> dockerhub_tag, from the HF dataset."""
    code = (
        "import csv,sys,json; from datasets import load_dataset\n"
        "def n(i): return i[len('instance_'):] if i.startswith('instance_') else i\n"
        "ds=load_dataset('ScaleAI/SWE-bench_Pro',split='test'); idx={}\n"
        "for r in ds:\n"
        "    idx[r['instance_id']]=r['dockerhub_tag']; idx[n(r['instance_id'])]=r['dockerhub_tag']\n"
        "want=[r['instance_id'] for r in csv.DictReader(open(sys.argv[1]))]\n"
        "print(json.dumps({w:(idx.get(w) or idx.get(n(w)) or idx.get('instance_'+n(w))) for w in want}))\n"
    )
    env = dict(os.environ); env["PYTHONPATH"] = str(SRC)
    r = subprocess.run([host_py, "-c", code, str(run_csv)], capture_output=True, text=True, timeout=1800, env=env)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        print(f"[orch] tag resolve failed: {e!r}\n{r.stderr[-500:]}", file=sys.stderr)
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-csv", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--solve-model", default="anthropic/claude-sonnet-4.6")
    ap.add_argument("--settings", default=str(PRO / "settings_sonnet46_probe.json"))
    ap.add_argument("--eval-repo", default=os.environ.get("OBO_SWEPRO_EVAL_REPO", ""),
                    help="SWE-bench_Pro-os grader clone for inline grading (or set OBO_SWEPRO_EVAL_REPO)")
    ap.add_argument("--per-task-cost", type=float, default=15.0)
    ap.add_argument("--total-budget", type=float, default=300.0)
    ap.add_argument("--solve-timeout", type=int, default=4500)
    ap.add_argument("--review-slots", type=int, default=1)
    ap.add_argument("--mem-limit", default="8g")
    ap.add_argument("--model-name", default="ouroboros-v6501-sonnet46")
    ap.add_argument("--disable-tools",
                    default="web_search,browse_page,browser_action,analyze_screenshot,vlm_query,view_image,youtube_transcript,claude_code_edit,switch_model")
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--limit", type=int, default=10_000)
    ap.add_argument("--host-python", default=sys.executable)
    ap.add_argument("--docker-host", default=DEFAULT_DOCKER_HOST)
    ap.add_argument("--keep-images", action="store_true", help="do NOT docker rmi the task image after grading")
    ap.add_argument("--no-grade", action="store_true", help="solve only; skip inline official grading")
    args = ap.parse_args()

    if not os.environ.get("OPENROUTER_API_KEY", "").strip() and not os.environ.get("OPENAI_API_KEY", "").strip():
        print("error: neither OPENROUTER_API_KEY nor OPENAI_API_KEY set", file=sys.stderr); return 2
    # Injecting the provider key into UNTRUSTED Pro task containers requires an audited, EXPLICIT
    # operator opt-in — the same contract run_pro.py enforces. NEVER silently default it on (review
    # round-2 CRITICAL): a silent setdefault here bypassed run_pro's refusal guard.
    if os.environ.get("OUROBOROS_BENCH_ALLOW_CONTAINER_SECRETS", "").lower() not in {"1", "true", "yes"}:
        print("error: refusing to inject the provider key into untrusted Pro task containers; "
              "set OUROBOROS_BENCH_ALLOW_CONTAINER_SECRETS=1 explicitly (audited local opt-in)", file=sys.stderr)
        return 2
    if args.docker_host:
        os.environ["DOCKER_HOST"] = args.docker_host  # docker SDK (grade) + CLI (solve)

    run_csv = pathlib.Path(args.run_csv).expanduser().resolve()
    out_dir = ensure_outside_repo(pathlib.Path(args.out_dir).expanduser(), SRC)  # never dirty repo/ (DEVELOPMENT)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = read_run_csv(run_csv)[args.start - 1: args.start - 1 + args.limit]
    tags = resolve_tags(run_csv, args.host_python)
    print(f"[orch] {len(rows)} tasks, {args.workers} workers, budget=${args.total_budget}, "
          f"grade={'off' if args.no_grade else 'inline'}, rmi={'off' if args.keep_images else 'on'}", file=sys.stderr)

    parts: list[list[dict]] = [[] for _ in range(args.workers)]
    for i, r in enumerate(rows):
        parts[i % args.workers].append(r)

    lock = threading.Lock()
    stop = threading.Event()
    state = {"cum_spent": 0.0, "results": []}

    def run_task(w: int, r: dict) -> None:
        idx = int(r["idx"]); iid = r["instance_id"]
        tag = tags.get(iid) or tags.get(iid[len("instance_"):] if iid.startswith("instance_") else "instance_" + iid)
        tdir = out_dir / f"w{w}" / f"t{idx:02d}"
        tdir.mkdir(parents=True, exist_ok=True)
        cmd = [args.host_python, str(RUN_PRO),
               "--csv", str(run_csv), "--start", str(idx), "--limit", "1",
               "--reset-state", "--baseline", "--cadence", "off", f"--volume-suffix=-w{w}",
               "--solve-model", args.solve_model, "--review-slots", str(args.review_slots),
               "--runtime-mode", "pro", "--settings", str(args.settings),
               "--per-task-cost", str(args.per_task_cost), "--total-budget", "999999",
               "--solve-timeout", str(args.solve_timeout), "--absorb-max", "60",
               "--model-name", args.model_name, "--disable-tools", args.disable_tools,
               "--mem-limit", args.mem_limit, "--pause-on-api-err", "-1", "--out-dir", str(tdir)]
        t0 = time.time()
        # Launch run_pro in its OWN session so a host timeout can kill the whole group (the
        # run_pro process + its `docker run` child), then reap any leaked solving container for
        # THIS worker — run_pro names them obopro-w{w}-* (review round-3: was a print-only leak).
        with open(tdir / "orchestrator_run.log", "w") as lg:
            proc = subprocess.Popen(cmd, stdout=lg, stderr=subprocess.STDOUT,
                                    env=dict(os.environ), **subprocess_new_group_kwargs())
            try:
                proc.wait(timeout=args.solve_timeout + 3600)
            except subprocess.TimeoutExpired:
                print(f"[orch] w{w} t{idx} {iid[:36]}: HOST TIMEOUT — killing run_pro + reaping obopro-w{w}-* containers", file=sys.stderr)
                reap_timed_out_runpro(proc, w, dict(os.environ))
        spent = 0.0; patch_bytes = 0; infra = ""; pred = None
        tl = tdir / "timeline.jsonl"
        if tl.exists():
            try:
                row = [json.loads(l) for l in tl.read_text().splitlines() if l.strip()][-1]
                spent = float(row.get("spent_after_usd") or 0.0)
                patch_bytes = int(row.get("patch_bytes") or 0)
                infra = row.get("infra_reason") or ("secret" if row.get("secret_opt_in_required") else "") or row.get("libc_skip") or ""
            except Exception:
                pass
        pf = tdir / "predictions.jsonl"
        if pf.exists() and pf.read_text().strip():
            try:
                pred = [json.loads(l) for l in pf.read_text().splitlines() if l.strip()][-1]
            except Exception:
                pred = None
        verdict_new, tests = "", ""
        if pred and not args.no_grade:
            verdict_new, tests = grade_one(tdir, pf, iid, host_python=args.host_python,
                                           eval_repo=args.eval_repo, run_csv=run_csv)
        if tag and not args.keep_images:
            subprocess.run(["docker", "rmi", "-f", f"{IMG_REPO}:{tag}"], capture_output=True, env=dict(os.environ))
        with lock:
            state["cum_spent"] += spent
            state["results"].append({"idx": idx, "instance_id": iid, "repo": r.get("repo") or "",
                                     "verdict_v25": r.get("verdict"), "bucket": r.get("bucket"), "worker": w,
                                     "spent_usd": round(spent, 4), "patch_bytes": patch_bytes, "infra": infra,
                                     "verdict_new": verdict_new, "tests": tests, "has_pred": bool(pred),
                                     "secs": int(time.time() - t0), "pred": pred})
            cum = state["cum_spent"]
            print(f"[orch] w{w} t{idx:02d} {iid[len('instance_'):][:34]:34} v25={r.get('verdict'):4} -> new={verdict_new or '-':9} "
                  f"patch={patch_bytes}B ${spent:.2f} cum=${cum:.2f} {infra or ''} ({int(time.time()-t0)}s)", file=sys.stderr)
            if cum >= args.total_budget:
                stop.set(); print(f"[orch] !! TOTAL BUDGET ${args.total_budget} reached — stop scheduling", file=sys.stderr)

    def worker(w: int) -> None:
        for r in parts[w]:
            if stop.is_set():
                print(f"[orch] w{w} stopping (budget)", file=sys.stderr); break
            run_task(w, r)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(args.workers)]
    for t in threads: t.start()
    for t in threads: t.join()

    results = sorted(state["results"], key=lambda x: x["idx"])
    preds = [r["pred"] for r in results if r["pred"]]
    (out_dir / "predictions.jsonl").write_text(
        "\n".join(json.dumps(p, ensure_ascii=False) for p in preds) + ("\n" if preds else ""), encoding="utf-8")
    (out_dir / "timeline.jsonl").write_text(
        "\n".join(json.dumps({k: v for k, v in r.items() if k != "pred"}, ensure_ascii=False) for r in results) + "\n",
        encoding="utf-8")
    try:
        repo_sha = subprocess.run(["git", "-C", str(SRC), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        repo_sha = ""
    npass = sum(1 for r in results if r["verdict_new"] == "PASS")
    manifest = {
        "run_csv": str(run_csv), "out_dir": str(out_dir), "workers": args.workers,
        "solve_model": args.solve_model, "review_slots": args.review_slots,
        "per_task_cost": args.per_task_cost, "total_budget": args.total_budget,
        "repo_sha": repo_sha, "src": str(SRC), "settings": str(args.settings),
        "settings_sha": sha256(pathlib.Path(args.settings)), "prompt_sha": sha256(PRO / "prompt_baseline.txt"),
        "disable_tools": args.disable_tools, "eval_repo": args.eval_repo,
        "n_tasks": len(results), "n_predictions": len(preds),
        "n_infra_skip": sum(1 for r in results if r["infra"]),
        "n_pass_new": npass, "cum_spent_usd": round(state["cum_spent"], 4),
        "stopped_on_budget": stop.is_set(),
        "results": [{k: v for k, v in r.items() if k != "pred"} for r in results],
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n[orch] DONE: tasks={len(results)} preds={len(preds)} new_PASS={npass} "
          f"infra_skip={manifest['n_infra_skip']} spent=${state['cum_spent']:.2f} -> {out_dir}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
