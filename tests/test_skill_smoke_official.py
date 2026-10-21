# tests/test_skill_smoke_official.py — official OuroborosHub skill install smoke.
#
# The `skill_smoke` lane: real-network install of the pinned official skills
# through the hub's git source (one live fetch of the hub repository) +
# preflight/deps/command probes + the Tier 6 review→grant→
# enable-persistence flow (CI review step only). Canonical lane description (purpose,
# triggers, red-means-investigate posture): docs/DEVELOPMENT.md "Pytest marker
# lanes". No fallback-skip on network failure (owner directive); bounded
# transient retries are the sole flake mitigation. Never add this file to
# _SERIAL_TEST_FILES or mark it `serial`: the lane runs only in its dedicated
# serial CI job, and the `and not skill_smoke` markexprs in quick/full-test
# are the barrier that keeps it out of those passes — the no-serial rule keeps
# the lane assignment single and unambiguous (defense-in-depth).
#
# Command smoke deliberately uses direct plugin.py registration against a fake
# PluginAPI (the tests/test_unix_computer_use_skill.py pattern) instead of the
# full extension_loader/enable chain (owner decision). weather/backlog_manager
# are the no-dependency class production also imports in-process; duckduckgo
# carries isolated deps (production dispatches it out-of-process) — the
# in-process call here is a smoke-level deviation accepted for CI simplicity
# on disposable runners over sha256-verified official payloads.

from __future__ import annotations

import hashlib
import http.client
import importlib.util
import json
import os
import pathlib
import subprocess
import time
from typing import Any, Dict

import pytest

from ouroboros import config
from ouroboros.config import DATA_DIR
from ouroboros.contracts.skill_manifest import parse_skill_manifest_text
from ouroboros.extension_isolated_deps import isolated_site_dirs_scope
from ouroboros.marketplace.install import _run_skill_review
from ouroboros.marketplace.install_specs import install_specs_hash
from ouroboros.marketplace.isolated_deps import (
    install_isolated_dependencies,
    python_runtime_binary,
    read_deps_state,
)
from ouroboros.review_records import review_slot_awaiting, review_slot_unresolved
from ouroboros.settings_defaults import SETTINGS_DEFAULTS
from ouroboros.skill_dependencies import auto_install_specs_for_skill
from ouroboros.skill_loader import (
    _sanitize_skill_name,
    find_skill,
    grant_status_for_skill,
    load_enabled,
    save_enabled,
)
from ouroboros.skill_readiness import skill_readiness_for_execution
from ouroboros.skill_review_cycles import SUBSTANTIVE_SKILL_REVIEW_STATUSES
from ouroboros.skill_review_history import load_history
from ouroboros.skill_review_status import normalize_skill_review_status
from ouroboros.tools.registry import ToolContext
from ouroboros.tools.skill_preflight import _handle_skill_preflight
from tests.conftest import _THREAD_LEAK_GRACE_SEC
from tests.review_pool_rosters import pool_roster, pool_seat, set_review_pool

pytestmark = pytest.mark.skill_smoke

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

OFFICIAL_SKILLS = [
    "a2a",
    "duckduckgo",
    "perplexity",
    "nanobanana",
    "weather",
    "backlog_manager",
    "music_gen",
    "video_gen",
]
# The only skills in the set that declare auto-installable dependency specs.
DEPS_SKILLS = ("a2a", "duckduckgo")
# Import proof per deps skill, run inside the isolated venv's own python.
DEPS_IMPORT_CHECK = {
    "duckduckgo": "import ddgs",
    "a2a": "import google.protobuf, a2a",
}

_TRANSIENT_RETRY_SLEEP_SEC = 10

# Deterministic install refusals (SkillInstallError codes) — never retried: a digest
# mismatch is hub corruption, an unknown or withdrawn entry is a catalog fact.
_DETERMINISTIC_INSTALL_CODES = frozenset({
    "digest_mismatch", "not_found", "withdrawn", "no_release", "payload_refused", "payload_unreadable",
    "name_taken", "update_instead",
})

# urllib transport errors are OSError subclasses; http.client raises its own
# transient shapes (IncompleteRead/BadStatusLine) outside that hierarchy.
_TRANSIENT_EXC = (OSError, http.client.HTTPException)


def _retry_transient(fn, *, describe: str, attempts: int = 2):
    """Retry transport-class failures only. Deterministic failures
    (OuroborosHubError over bad catalog data is a RuntimeError) re-raise
    immediately — red is the honest outcome.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except _TRANSIENT_EXC as exc:
            last_exc = exc
            if attempt < attempts:
                print(f"{describe}: attempt {attempt} failed ({exc!r}); retrying once")
                time.sleep(_TRANSIENT_RETRY_SLEEP_SEC)
    raise AssertionError(f"{describe} failed after {attempts} attempts: {last_exc!r}")


# ---------------------------------------------------------------------------
# Session fixtures: one live catalog fetch, memoized installs/deps.
# ---------------------------------------------------------------------------


def _hub_rows():
    from ouroboros.skill_sources import load_sources

    return [row for row in load_sources() if row["kind"] == "hub"]


@pytest.fixture(scope="session")
def hub_catalog():
    """Fetch the LIVE hub repository exactly once through its git source.

    The fetch exercises the real transport (shallow clone, snapshot). The v1
    ``catalog.json`` the hub keeps for older clients carries per-file hashes,
    which Tier 1 re-verifies on disk; its digest line makes a red run
    reconstructible against the exact hub commit that was tested.
    """
    from ouroboros.skill_source_catalog import refresh_source
    from ouroboros.skill_source_git import clone_dir

    rows = _hub_rows()
    assert rows, "no hub source is connected"
    meta = {}
    for attempt in (1, 2):
        meta = refresh_source(DATA_DIR, rows[0])
        if not meta.get("error"):
            break
        print(f"hub fetch attempt {attempt} failed ({meta.get('error')}); retrying once")
        time.sleep(_TRANSIENT_RETRY_SLEEP_SEC)
    assert not meta.get("error"), f"hub fetch failed: {meta.get('error')}"
    catalog = json.loads((clone_dir(DATA_DIR, rows[0]) / "catalog.json").read_text(encoding="utf-8"))
    slugs = {
        str(item.get("slug") or ""): str(item.get("version") or "")
        for item in catalog.get("skills") or []
        if isinstance(item, dict)
    }
    print(f"OuroborosHub commit={meta.get('commit')}")
    print("catalog versions: " + ", ".join(f"{s}={slugs.get(s, '?')}" for s in OFFICIAL_SKILLS))
    missing = [slug for slug in OFFICIAL_SKILLS if slug not in slugs]
    assert not missing, f"hub catalog is missing expected skills: {missing}"
    yield catalog


@pytest.fixture(scope="session")
def install_skill(hub_catalog):
    """Memoizing installer through the shared source lifecycle: each slug at most once per session.

    The item comes from whichever hub row offers it (Official or Community). A transient fetch failure
    gets one retry; deterministic refusals (a digest mismatch first) are never retried.
    """
    from ouroboros.skill_source_install import SkillInstallError, install_item

    results: Dict[str, Dict[str, Any]] = {}

    def _install(slug: str) -> Dict[str, Any]:
        last: Exception | None = None
        for row in _hub_rows():
            try:
                return install_item(DATA_DIR, f"{row['id']}/{slug}", turn_on_when_ready=False)
            except SkillInstallError as exc:
                if exc.code != "not_found":
                    raise
                last = exc
        raise AssertionError(f"{slug} is offered by no hub row: {last}")

    def _ensure(slug: str) -> Dict[str, Any]:
        if slug in results:
            return results[slug]
        try:
            result = _install(slug)
        except SkillInstallError as exc:
            if exc.code in _DETERMINISTIC_INSTALL_CODES:
                raise AssertionError(f"install({slug}) refused: {exc.code}: {exc}") from exc
            print(f"install({slug}): transient failure ({exc}); retrying once")
            time.sleep(_TRANSIENT_RETRY_SLEEP_SEC)
            result = _install(slug)
        assert result["ok"], f"install({slug}) failed: {result}"
        results[slug] = result
        return result

    return _ensure


@pytest.fixture(scope="session")
def ensure_deps(install_skill):
    """Memoized real isolated-deps install (venv + pip) per deps skill."""
    fingerprints: Dict[str, Dict[str, Any]] = {}

    def _ensure(slug: str) -> Dict[str, Any]:
        if slug in fingerprints:
            return fingerprints[slug]
        install_skill(slug)
        sanitized = _sanitize_skill_name(slug)
        loaded = find_skill(DATA_DIR, sanitized)
        assert loaded is not None, f"installed skill {sanitized!r} not discoverable"
        auto = auto_install_specs_for_skill(DATA_DIR, loaded)
        assert auto, f"{slug}: expected declared auto install specs"
        try:
            fingerprint = install_isolated_dependencies(DATA_DIR, sanitized, loaded.skill_dir, auto)
        except Exception as exc:  # noqa: BLE001 — pip failure text is opaque
            # PyPI/pip flakes are indistinguishable from real resolver errors
            # by text; one bounded retry is safe (venv reuse is idempotent and
            # the failed deps.json state is overwritten on success).
            print(f"{slug}: isolated deps install failed ({exc!r}); retrying once")
            time.sleep(_TRANSIENT_RETRY_SLEEP_SEC)
            fingerprint = install_isolated_dependencies(DATA_DIR, sanitized, loaded.skill_dir, auto)
        assert fingerprint.get("status") == "installed", (
            f"{slug}: isolated deps install failed: {json.dumps(fingerprint, ensure_ascii=False)[:4000]}"
        )
        fingerprints[slug] = fingerprint
        return fingerprint

    return _ensure


def _catalog_entry(hub_catalog: Dict[str, Any], slug: str) -> Dict[str, Any]:
    for item in hub_catalog.get("skills") or []:
        if isinstance(item, dict) and item.get("slug") == slug:
            return item
    raise AssertionError(f"slug {slug} not in catalog")


def _skill_dir(slug: str) -> pathlib.Path:
    return DATA_DIR / "skills" / "ouroboroshub" / _sanitize_skill_name(slug)


def _load_manifest(slug: str):
    text = (_skill_dir(slug) / "SKILL.md").read_text(encoding="utf-8")
    return parse_skill_manifest_text(text)


# ---------------------------------------------------------------------------
# Tier 1 — install: payload landed, sha256 re-verified, provenance sidecar.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(300)
@pytest.mark.parametrize("slug", OFFICIAL_SKILLS)
def test_install_lands_payload_with_verified_provenance(slug, hub_catalog, install_skill):
    from ouroboros.skill_identity import is_release_digest
    from ouroboros.skill_install_record import read_install_record

    result = install_skill(slug)
    sanitized = _sanitize_skill_name(slug)
    target = _skill_dir(slug)
    assert result["name"] == sanitized and target.is_dir()
    assert (target / "SKILL.md").is_file()

    entry = _catalog_entry(hub_catalog, slug)
    for item in entry.get("files") or []:
        rel = str(item.get("path") or "")
        expected_sha = str(item.get("sha256") or "").lower()
        on_disk = target / pathlib.Path(*pathlib.PurePosixPath(rel).parts)
        assert on_disk.is_file(), f"{slug}: catalog file missing on disk: {rel}"
        actual_sha = hashlib.sha256(on_disk.read_bytes()).hexdigest()
        assert actual_sha == expected_sha, f"{slug}: sha256 mismatch for {rel}"

    # The install record replaces the payload sidecar: nothing per-install lands in the payload.
    assert not (target / ".ouroboroshub.json").exists()
    record = read_install_record(DATA_DIR, sanitized)
    assert record is not None and record["entry"] == slug
    assert record["release"]["version"] == str(entry.get("version") or "")
    assert is_release_digest(record["release"]["digest"]) and record["release"]["commit"]
    assert record["basis"] in {"trusted", "review"}
    loaded = find_skill(DATA_DIR, sanitized)
    declared = auto_install_specs_for_skill(DATA_DIR, loaded)
    if slug in DEPS_SKILLS:
        assert declared, f"{slug}: expected auto install specs declared by its manifest"
    else:
        assert not declared, (
            f"{slug}: unexpected auto install specs — the smoke lane's deps coverage "
            f"(DEPS_SKILLS) no longer matches the hub"
        )

    staging = DATA_DIR / "skills" / "ouroboroshub" / ".staging"
    leftovers = [p.name for p in staging.iterdir()] if staging.is_dir() else []
    assert not leftovers, f"install left staging directories behind: {leftovers}"


# ---------------------------------------------------------------------------
# Tier 2 — manifest parses and matches the per-skill contract.
# ---------------------------------------------------------------------------


@pytest.mark.timeout(60)
@pytest.mark.parametrize("slug", OFFICIAL_SKILLS)
def test_manifest_parses_and_matches_contract(slug, install_skill):
    install_skill(slug)
    manifest = _load_manifest(slug)
    target = _skill_dir(slug)

    if slug == "video_gen":
        assert manifest.is_script()
        script_names = {
            str(s.get("name") if isinstance(s, dict) else s) for s in manifest.scripts
        }
        assert "generate.py" in script_names
        assert not manifest.entry
        assert "OPENROUTER_API_KEY" in manifest.env_from_settings
        return

    assert manifest.is_extension(), f"{slug}: expected type extension"
    assert manifest.entry == "plugin.py"
    assert (target / "plugin.py").is_file()

    keyed = {"perplexity", "nanobanana", "music_gen"}
    if slug in keyed:
        assert "OPENROUTER_API_KEY" in manifest.env_from_settings


# ---------------------------------------------------------------------------
# Tier 3 — offline skill_preflight is clean (compile + widget + permissions).
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
@pytest.mark.parametrize("slug", OFFICIAL_SKILLS)
def test_skill_preflight_is_clean(slug, install_skill):
    install_skill(slug)
    ctx = ToolContext(repo_dir=REPO_ROOT, drive_root=DATA_DIR)
    out = _handle_skill_preflight(ctx, skill=_sanitize_skill_name(slug))
    assert not out.startswith("⚠️"), out
    payload = json.loads(out)
    assert payload["ok"] is True, out
    assert payload["files_failed"] == 0, out
    assert all(f.get("ok") for f in payload["manifest"]), out


# ---------------------------------------------------------------------------
# Tier 4 — real pip into the per-skill isolated venv (a2a, duckduckgo).
# ---------------------------------------------------------------------------


@pytest.mark.timeout(900)
@pytest.mark.parametrize("slug", DEPS_SKILLS)
def test_isolated_deps_install_real_pip(slug, ensure_deps):
    ensure_deps(slug)
    sanitized = _sanitize_skill_name(slug)
    loaded = find_skill(DATA_DIR, sanitized)
    assert loaded is not None
    auto = auto_install_specs_for_skill(DATA_DIR, loaded)
    state = read_deps_state(DATA_DIR, sanitized, loaded.skill_dir)
    assert state.get("status") == "installed", state
    assert state.get("specs_hash") == install_specs_hash(auto), state

    venv_python = python_runtime_binary(loaded.skill_dir)
    assert venv_python is not None and venv_python.is_file(), (
        f"{slug}: isolated venv python missing under {loaded.skill_dir}"
    )
    check = DEPS_IMPORT_CHECK[slug]
    proof = subprocess.run(
        [str(venv_python), "-c", check],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert proof.returncode == 0, (
        f"{slug}: venv import check {check!r} failed:\n{proof.stderr[-4000:]}"
    )
    # Provenance: log the resolved package set so a red run caused by PyPI
    # drift is reconstructible from CI output alone.
    freeze = subprocess.run(
        [str(venv_python), "-m", "pip", "freeze"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    print(f"{slug} resolved deps:\n{freeze.stdout.strip()}")


@pytest.mark.timeout(300)
def test_non_deps_skills_declare_no_auto_specs(install_skill):
    """Guard: DEPS_SKILLS stays in sync with what the catalog actually declares."""
    for slug in OFFICIAL_SKILLS:
        if slug in DEPS_SKILLS:
            continue
        install_skill(slug)
        loaded = find_skill(DATA_DIR, _sanitize_skill_name(slug))
        assert loaded is not None
        assert not auto_install_specs_for_skill(DATA_DIR, loaded), (
            f"{slug} now declares auto install specs — add it to DEPS_SKILLS coverage"
        )


# ---------------------------------------------------------------------------
# Tier 5 — command smoke for the keyless skills (direct plugin registration).
# ---------------------------------------------------------------------------


class _FakePluginAPI:
    """Minimal recording PluginAPI stand-in (tests/test_unix_computer_use_skill.py
    pattern). Only the surfaces these three plugins actually use exist —
    an unexpected call fails loudly with AttributeError instead of silently
    no-opping."""

    def __init__(self, state_dir: pathlib.Path, data_dir: pathlib.Path) -> None:
        self.state_dir = state_dir
        self.data_dir = data_dir
        self.tools: Dict[str, Dict[str, Any]] = {}
        self.routes: Dict[str, Dict[str, Any]] = {}
        self.ui_tabs: Dict[str, Dict[str, Any]] = {}
        self.logs: list[tuple[str, str]] = []

    def get_state_dir(self) -> str:
        return str(self.state_dir)

    def get_runtime_info(self) -> Dict[str, Any]:
        return {"data_dir": str(self.data_dir)}

    def register_tool(self, name: str, handler: Any = None, **metadata: Any) -> None:
        self.tools[name] = {"handler": handler, "metadata": metadata}

    def register_route(self, path: str, handler: Any = None, **metadata: Any) -> None:
        self.routes[path] = {"handler": handler, "metadata": metadata}

    def register_ui_tab(self, tab_id: str, title: str = "", **metadata: Any) -> None:
        self.ui_tabs[tab_id] = {"title": title, "metadata": metadata}

    def log(self, level: str, message: str) -> None:
        self.logs.append((level, message))


def _register_plugin(slug: str, tmp_path: pathlib.Path) -> _FakePluginAPI:
    plugin_path = _skill_dir(slug) / "plugin.py"
    # Unique module name per slug: a bare "plugin" name would collide in
    # sys.modules across skills and silently reuse the first loaded module.
    spec = importlib.util.spec_from_file_location(f"skill_smoke_plugin_{slug}", plugin_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    api = _FakePluginAPI(state_dir=tmp_path / "state", data_dir=tmp_path / "data")
    api.state_dir.mkdir(parents=True, exist_ok=True)
    api.data_dir.mkdir(parents=True, exist_ok=True)
    module.register(api)
    return api


def _tool_handler(api: _FakePluginAPI, slug: str, name: str):
    assert name in api.tools, (
        f"{slug}: expected tool {name!r}; registered tools: {sorted(api.tools)}"
    )
    return api.tools[name]["handler"]


@pytest.mark.timeout(120)
def test_duckduckgo_search_returns_results(ensure_deps, tmp_path):
    ensure_deps("duckduckgo")
    skill_dir = _skill_dir("duckduckgo")
    # isolated_site_dirs_scope is the production sys.path bridge over the
    # Tier-4 venv (ddgs is also a core dependency, so this doubles as a check
    # that the bridge itself does not break plugin imports). On exit the scope
    # purges venv-resident modules from sys.modules and removes the injected
    # sys.path entries, so nothing leaks past this test.
    with isolated_site_dirs_scope(skill_dir, enabled=True):
        api = _register_plugin("duckduckgo", tmp_path)
        handler = _tool_handler(api, "duckduckgo", "search")
        payload = json.loads(handler(query="python programming language", max_results=5))
        if "error" in payload:
            # DDG rate-limits shared CI runner IPs; one retry, then honest red.
            print(f"duckduckgo first attempt error: {payload['error']}; retrying once")
            time.sleep(_TRANSIENT_RETRY_SLEEP_SEC)
            payload = json.loads(handler(query="python programming language", max_results=5))
    assert "error" not in payload, f"duckduckgo search failed: {payload}"
    assert payload["count"] >= 1, payload
    assert all(str(r.get("url", "")).startswith("http") for r in payload["results"]), payload


@pytest.mark.timeout(120)
def test_weather_fetch_returns_conditions(install_skill, tmp_path):
    install_skill("weather")
    api = _register_plugin("weather", tmp_path)
    handler = _tool_handler(api, "weather", "fetch")
    payload = json.loads(handler(city="London"))
    if "error" in payload:
        print(f"weather first attempt error: {payload['error']}; retrying once")
        time.sleep(_TRANSIENT_RETRY_SLEEP_SEC)
        payload = json.loads(handler(city="London"))
    assert "error" not in payload, f"weather fetch failed: {payload}"
    assert isinstance(payload.get("temp_c"), int), payload
    assert str(payload.get("condition") or "").strip(), payload
    assert payload.get("forecast_rows"), payload
    assert payload.get("weather_summary"), payload


@pytest.mark.timeout(60)
def test_backlog_manager_summary_offline(install_skill, tmp_path):
    install_skill("backlog_manager")
    api = _register_plugin("backlog_manager", tmp_path)
    handler = _tool_handler(api, "backlog_manager", "summary")
    summary = handler()
    assert summary.startswith("Backlog Manager: 0 visible items"), summary
    assert {"list", "add", "update", "move"} <= set(api.routes), sorted(api.routes)
    assert "backlog" in api.ui_tabs, sorted(api.ui_tabs)


# a2a command smoke is deliberately absent: its tools need a live A2A peer and
# its companion daemon, which this lane must not start (owner decision). Its
# coverage is Tier 2 static companion contract + Tier 3 daemon compile +
# Tier 4 real dependency install.


# ---------------------------------------------------------------------------
# Tier 6 — production review→grant flow + enable-persistence prerequisites
# (real LLM skill review).
#
# Owner directive: CI must exercise the production flow "agent installs a
# skill → runs Ouroboros's OWN skill review → only if the review is OK the
# skill gets enabled" for a representative subset of official skills, with
# the review running on a cheap single reviewer slot (production users keep
# the strong default reviewer models — untouched here).
#
# Runs ONLY in the CI job's dedicated review step (ubuntu shard, secret in
# env, fresh pytest process): downloaded plugin code (Tier 5) must never
# share a process with OPENROUTER_API_KEY, so the job runs Tiers 1-5 and
# Tier 6 as two separate pytest invocations and only the Tier 6 step carries
# the secret. Missing key = hard red, not a skip (owner directive).
#
# Review call follows the production ordering (review first, dependency
# install only after an executable review; auto-grant runs inside
# review_skill). Enforcement is pinned to `blocking` so auto-grant can only
# happen on a clean/warnings verdict — under the production default
# `advisory` even a `blockers` verdict is executable and would auto-grant
# before this test rejects it. Enablement here is "enable persistence
# prerequisites" (review gate + grants + deps + enabled.json +
# skill_readiness_for_execution), NOT live extension loading —
# reconcile_extension is server runtime, consistent with the Tier 5 posture.
#
# Deadlines are the PRODUCT's own: the review runs with no
# OUROBOROS_REVIEW_MODEL_TIMEOUT_SEC, so its one native reviewer episode reads
# the skill in several tool rounds inside the slot window the product resolves
# (``_REVIEW_WINDOW_SEC``). A narrower test-side window only cut healthy
# episodes into unsettled, unretryable reds. The test owns the by-design late
# worker a timed-out slot leaves behind (``owned_review_workers``).
#
# Cost (measured 2026-10-17/18, gemini-3.5-flash, one native reviewer at low
# effort): about $11 per review (the median; single reviews reported from
# $0.77 to $12.24), so about $33 per CI run for the three skills; retries (the
# settled one, an outage waited out) add at most two more waves per skill.
# ---------------------------------------------------------------------------

# Owner-picked subset: a2a (mandated) + duckduckgo (core keyless search;
# exercises the review→deps production ordering) + perplexity (keyed skill;
# exercises OPENROUTER_API_KEY auto-grant). The subset carried telegram-bridge
# for the protected-token and chat-permission path until that skill was retired
# from the official catalog; no remaining official skill requests a protected
# token, so that tier is unexercised here.
REVIEW_SKILLS = ("a2a", "duckduckgo", "perplexity")
_REVIEW_MODEL = "google/gemini-3.5-flash"
# The slot window the product resolves for this review: with no explicit
# OUROBOROS_REVIEW_MODEL_TIMEOUT_SEC and no task deadline, an API reviewer
# slot's logical window is the shipped LLM transport bound
# (deadline_utils.review_operation_timeout_sec -> llm_transport_timeout_sec),
# and a native episode bounds every round's send by what is left of it.
# review_env removes any override, so this shipped default is what governs.
_REVIEW_WINDOW_SEC = float(SETTINGS_DEFAULTS["OUROBOROS_LLM_TRANSPORT_READ_TIMEOUT_SEC"])
# Waves one skill's review may send: the first, then the one settled retry or
# provider outages waited out (_review_with_one_settled_retry).
_REVIEW_WAVES = 3
# The test's own outer bound covers the longest legitimate product timeline:
# each wave settling at its window, and the drain of a late worker whose send
# runs to that bound (owned_review_workers), plus the outage waits, the catalog
# install, pack building and the cold a2a dependency install that only follows
# a passing review. The CI job budget sits above it.
_REVIEW_TEST_TIMEOUT_SEC = int((_REVIEW_WAVES + 1) * _REVIEW_WINDOW_SEC + 900)
# Expected auto-granted settings keys per skill (grants are request-keyed,
# not value-keyed; empty values grant fine and only bite at runtime).
_REVIEW_EXPECTED_KEYS = {
    "a2a": set(),
    "duckduckgo": set(),
    "perplexity": {"OPENROUTER_API_KEY"},
}


# Session scope on purpose: session fixtures instantiate before any
# function-scoped fixture, and among session fixtures signature order wins —
# so listing this BEFORE install_skill makes the missing-key red fire before
# the live catalog fetch. Only Tier 6 requests it; Tiers 1-5 stay keyless.
@pytest.fixture(scope="session")
def review_secret():
    assert os.environ.get("OPENROUTER_API_KEY"), (
        "Tier 6 needs OPENROUTER_API_KEY in the environment (CI: the review "
        "step's secret env; locally: export a key). A missing key is a hard "
        "red by owner directive — forks/mirrors without the secret stay red."
    )


@pytest.fixture()
def review_env(monkeypatch, tmp_path):
    # Cheap + fast verdicts: one reviewer (the production-supported
    # single_reviewer_no_diversity degraded mode) at low reasoning effort. Since
    # the review pool (#1639) a reviewer is a marked row of the subagent catalog,
    # and the retired OUROBOROS_REVIEW_MODELS / OUROBOROS_EFFORT_REVIEW are not
    # read from the environment (docs/architecture/11-frozen-contracts-v1.md);
    # native is the delivery the lane-era slot ran.
    set_review_pool(monkeypatch, pool_roster(pool_seat(
        "skill-smoke-reviewer", _REVIEW_MODEL, delivery="native", effort="low")))
    monkeypatch.setenv("OUROBOROS_REVIEW_ENFORCEMENT", "blocking")
    # The product's own deadlines: no test-side review window, and the shipped
    # transport bound the window and this test's budget derive from.
    monkeypatch.delenv("OUROBOROS_REVIEW_MODEL_TIMEOUT_SEC", raising=False)
    monkeypatch.delenv("OUROBOROS_LLM_TRANSPORT_READ_TIMEOUT_SEC", raising=False)
    monkeypatch.setenv("OUROBOROS_AUTO_GRANT_REVIEWED_SKILLS", "true")
    monkeypatch.setenv("OUROBOROS_REVIEW_MAX_CYCLES", str(_REVIEW_WAVES))  # each wave is a paid cycle
    # get_auto_grant_enabled reads the settings FILE first; point the module
    # at a missing path so the env value above is authoritative (the pattern
    # tests/test_skill_review.py uses).
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "missing-settings.json")


def active_review_attempts() -> Dict[str, Dict[str, Any]]:
    """``{attempt key: {"operation_id", "late"}}`` of every reviewer worker review custody holds now.

    ``late``: its caller already took the typed in-flight answer (the slot's window ended, the
    roster was released early or the author detached), the one kind of worker meant to outlive
    the review it served.
    """
    from ouroboros import review_custody

    with review_custody._ACTIVE_LOCK:
        return {key: {"operation_id": entry.operation_id,
                      "late": bool(entry.timed_out or entry.released_early or entry.detached)}
                for key, entry in review_custody._ACTIVE.items()}


def wait_for_review_workers(started_before, bound_sec: float, poll_sec: float = 0.5) -> Dict[str, str]:
    """Wait until every reviewer worker started after ``started_before`` has retired.

    Review custody retires an attempt from ``_ACTIVE`` in the same worker that
    publishes its settlement, right before the thread ends (its model-send
    executor is already shut down), so an empty difference means the workers
    are finishing. Returns ``{key: operation id}`` of what is still active at
    ``bound_sec`` (``{}`` once drained).
    """
    deadline = time.monotonic() + bound_sec
    while True:
        active = {key: row["operation_id"] for key, row in active_review_attempts().items()
                  if key not in started_before}
        if not active or time.monotonic() >= deadline:
            return active
        time.sleep(poll_sec)


# The session thread-leak check's own grace: all an attempt that is not late may take to retire
# after its review returned.
_SETTLED_ATTEMPT_GRACE_SEC = _THREAD_LEAK_GRACE_SEC


def settle_review_workers(started_before) -> str:
    """Why reviewer workers started after ``started_before`` are still running, or ``""``.

    Only a late worker is waited for, bounded by the product's own transport bound, which
    also bounds its send; it exists only when a slot's window ran out mid-call, so its
    review is unsettled and the test is red anyway. Any other attempt still registered is a
    custody leak and gets just the thread-leak check's grace, so a regression that keeps a
    settled attempt alive after a passing review cannot hide behind the late worker's wait.
    """
    late = {key for key, row in active_review_attempts().items() if row["late"] and key not in started_before}
    lingering = wait_for_review_workers(set(started_before) | late, _SETTLED_ATTEMPT_GRACE_SEC, poll_sec=0.1)
    bound = _REVIEW_WINDOW_SEC + 60
    # A late worker is drained beside a leak too: the environment is never restored under it.
    still = wait_for_review_workers(set(started_before) | set(lingering), bound) if late else {}
    return "; ".join(filter(None, (
        lingering and f"reviewer attempts still registered after the test, none of them late: {sorted(lingering.values())}",
        still and f"late reviewer workers still active {bound:.0f}s after the test: {sorted(still.values())}")))


@pytest.fixture()
def owned_review_workers(review_env):
    """The test owns every reviewer worker it starts, the by-design late one included.

    A slot whose logical window ends hands its caller a typed in-flight actor
    while the physical worker keeps running until its send settles
    (review_custody's late-result worker). That worker belongs to this test:
    wait for it here (``settle_review_workers``), before monkeypatch restores the
    environment it runs under (this fixture is torn down before ``review_env``) and
    before the session's thread-leak check, never by widening that check's thread
    names and never by cutting the provider socket.
    """
    before = set(active_review_attempts())
    yield
    problem = settle_review_workers(before)
    assert not problem, problem


def _skill_state_dir(name: str) -> pathlib.Path:
    return DATA_DIR / "state" / "skills" / name


def _run_review_flow(slug: str, name: str) -> tuple[str, str, str, Dict[str, Any]]:
    """Review first, dependencies only after an executable review (the production ordering)."""
    from ouroboros.skill_dependencies import ensure_skill_dependencies
    from ouroboros.skill_loader import review_status_allows_execution

    status, findings, error = _run_skill_review(DATA_DIR, REPO_ROOT, name)
    payload: Dict[str, Any] = {"review_status": status, "review_findings": findings, "review_error": error}
    deps_status = "not_required"
    if review_status_allows_execution(status) and not error:
        deps_status, deps_error = ensure_skill_dependencies(DATA_DIR, find_skill(DATA_DIR, name))
        payload.update({"deps_status": deps_status, "deps_error": deps_error})
    return str(status), str(error or ""), str(deps_status), payload


def _review_diag(name: str, status: str, error: str, payload: Dict[str, Any]) -> str:
    """Full findings + raw actor records — reviewer output is never truncated."""
    parts = [f"status={status!r} error={error!r}"]
    parts.append(f"review_findings={json.dumps(payload.get('review_findings'), ensure_ascii=False)}")
    review_path = _skill_state_dir(name) / "review.json"
    if review_path.exists():
        review = json.loads(review_path.read_text(encoding="utf-8"))
        parts.append(f"reviewer_models={review.get('reviewer_models')}")
        parts.append(
            "raw_actor_records="
            + json.dumps(review.get("raw_actor_records"), ensure_ascii=False)
        )
    # Quorum failures return before review.json is saved. Their actors live in
    # history, including provider errors that look like malformed verdicts at
    # this boundary. Include the latest attempt even if an older review exists.
    history = load_history(DATA_DIR, name, limit=1)
    if history:
        parts.append("latest_review_history=" + json.dumps(history[-1], ensure_ascii=False))
    return "\n".join(parts)


def _actor_reported_valuation_usd(review: Dict[str, Any]) -> float:
    """Non-authoritative reviewer telemetry, kept only for live-smoke diagnostics."""
    return sum(float(rec.get("cost_usd") or 0.0) for rec in review.get("raw_actor_records") or [])


def _smoke_retry_refusal(name: str, status: str) -> str:
    """Retry only settled work, retaining production replay/cycle and custody facts."""
    if status not in ("clean", "warnings") and normalize_skill_review_status(status) in SUBSTANTIVE_SKILL_REVIEW_STATUSES:
        return f"recorded {status!r} verdict is final for identical material"
    skill, history = find_skill(DATA_DIR, name), load_history(DATA_DIR, name, limit=1)
    if skill is None or not history or history[-1].get("content_hash") != skill.content_hash:
        return "no recorded wave for the current content"
    records = history[-1].get("raw_actor_records")
    if not isinstance(records, list) or not records:
        return "the recorded wave has no reviewer records"
    stack = list(records)
    while stack:
        row = stack.pop()
        if not isinstance(row, dict):
            return "reviewer settlement is unknown"
        if review_slot_awaiting(row) or review_slot_unresolved(row) or row.get("late_result_pending"):
            return "reviewer work is still unsettled in the original wave"
        children = row.get("raw_results")
        if isinstance(children, list) and children:
            stack.extend(children)
        elif row.get("operation_state") not in {"settled", "late_settled", "not_dispatched"}:
            return "reviewer settlement is unknown"
    return ""


# A wave the provider refuses at the HTTP layer (a 429 or 5xx on every reviewer's record) is an outage of
# the live route, not a verdict: the model may have read part of the skill, but its review never finished.
# It is waited out on the provider's clock. A wave the model answered, parseable or not, keeps the one
# settled retry, and a retry that records no wave of its own (a crash before any send, a local refusal)
# ends the test red, as does any attempt whose review raised, wave or not: a crash in the review path is
# never waited out. Every wave is a paid review cycle, a refused one included (_REVIEW_WAVES).
_OUTAGE_BACKOFF_SEC = 90.0
# marketplace.install._run_skill_review's error when review_skill itself raised (it answers "pending").
_REVIEW_RAISED = "review_skill raised:"


def _provider_refused(record: Any) -> bool:
    """The provider refused this reviewer at the HTTP layer, a 429 or a 5xx. The transport label alone
    is no proof: a native episode the model answered with malformed calls is labelled the same way."""
    code = record.get("http_status") if isinstance(record, dict) else None
    try:
        code = int(code) if not isinstance(code, bool) else 0
    except (TypeError, ValueError):
        return False
    return (record.get("status") == "error" and record.get("transport_status") == "provider_transport_error"
            and (code == 429 or 500 <= code <= 599))


def _refused_by_provider(wave: Any) -> bool:
    """The provider refused every reviewer of this recorded wave (a 429 or 5xx): no model answered it."""
    records = wave.get("raw_actor_records") if isinstance(wave, dict) else None
    return isinstance(records, list) and bool(records) and all(_provider_refused(record) for record in records)


def _latest_wave(name: str) -> Any:
    history = load_history(DATA_DIR, name, limit=1)
    return history[-1] if history else None


def _review_with_one_settled_retry(slug: str, name: str):
    retried_on = _latest_wave(name)  # an older row (a reused data dir) never stands in for this attempt's
    status, error, deps_status, payload = _run_review_flow(slug, name)
    waves, outages, settled, stop = 1, 0, False, ""
    while (status not in ("clean", "warnings") or error or (slug in DEPS_SKILLS and deps_status != "installed")):
        wave = _latest_wave(name)
        if waves >= _REVIEW_WAVES:
            stop = f"all {_REVIEW_WAVES} waves are spent"
        elif error.startswith(_REVIEW_RAISED):
            stop = "the review itself raised"
        elif wave == retried_on:
            stop = "the attempt recorded no wave of its own"
        else:
            stop = _smoke_retry_refusal(name, status)
            if not stop and not _refused_by_provider(wave) and settled:
                stop = "its one settled retry is spent"
        if stop:
            break
        if _refused_by_provider(wave):
            outages += 1
            print(f"{slug}: the provider refused every reviewer (outage {outages}); original state kept, "
                  f"retrying in {_OUTAGE_BACKOFF_SEC:.0f}s\n" + _review_diag(name, status, error, payload))
            time.sleep(_OUTAGE_BACKOFF_SEC)
        else:
            settled = True
            print(f"{slug}: attempt not OK; original state kept, one settled retry\n"
                  + _review_diag(name, status, error, payload))
        retried_on = wave
        status, error, deps_status, payload = _run_review_flow(slug, name)
        waves += 1
    note = (f" after {outages} provider outage(s)" if outages else "") + (" after one settled retry" if settled else "")
    if stop:
        note += f" (not retried: {stop})"
        print(f"{slug}: attempt not OK{note}\n" + _review_diag(name, status, error, payload))
    return status, error, deps_status, payload, note


# _REVIEW_TEST_TIMEOUT_SEC: up to three review waves under the product's own
# window (the one settled retry, provider outages waited out), the late
# worker's drain and cold a2a venv+pip. The unchanged production path owns
# free verdict replay. Pending work stays red; its state is never erased.
@pytest.mark.timeout(_REVIEW_TEST_TIMEOUT_SEC)
# review_secret (session) is listed before install_skill (session): among
# same-scope fixtures signature order wins, so a fork without the secret reds
# on one crisp message before the live catalog fetch.
@pytest.mark.parametrize("slug", REVIEW_SKILLS)
def test_review_grants_and_enable(slug, review_secret, install_skill, owned_review_workers):
    install_skill(slug)
    name = _sanitize_skill_name(slug)
    started = time.time()

    status, error, deps_status, payload, note = _review_with_one_settled_retry(slug, name)
    assert status in ("clean", "warnings") and not error, (
        f"{slug}: review not OK{note}\n" + _review_diag(name, status, error, payload)
    )

    # Production ordering: dependencies reconcile only AFTER the executable
    # review, inside the same gateway wrapper.
    if slug in DEPS_SKILLS:
        assert deps_status == "installed", f"{slug}: deps_status={deps_status!r}"
    else:
        assert deps_status == "not_required", f"{slug}: deps_status={deps_status!r}"

    # Persisted review binds to the reloaded payload (fresh find_skill, not
    # the pre-review in-memory object).
    loaded = find_skill(DATA_DIR, name)
    assert loaded is not None and not loaded.load_error
    review = json.loads((_skill_state_dir(name) / "review.json").read_text(encoding="utf-8"))
    assert review.get("content_hash") == loaded.content_hash
    models = list(review.get("reviewer_models") or [])
    assert len(models) == 1, models
    # Reviewer identity may carry the stable slot attribution suffix introduced
    # by the shared review substrate, while the model id remains the contract.
    model_id = models[0].split(" [", 1)[0].rsplit("#", 1)[0]
    assert model_id == _REVIEW_MODEL, models
    # The official_hub reduced-noise profile is retired: Official is the trusted
    # source row's install basis now, so an explicit review is an ordinary one.
    assert review.get("review_profile") in (None, ""), review.get("review_profile")

    # Auto-grant ran inside review_skill (blocking enforcement ⇒ only on
    # clean/warnings). Grants are request-keyed: a requested key grants with an
    # empty settings value.
    grants = grant_status_for_skill(DATA_DIR, loaded)
    assert grants.get("all_granted") is True, grants
    expected_keys = _REVIEW_EXPECTED_KEYS[slug]
    granted_keys = set(grants.get("granted_keys") or [])
    assert granted_keys == expected_keys, (
        f"{slug}: granted_keys={sorted(granted_keys)} expected={sorted(expected_keys)}"
    )
    # Enable persistence prerequisites: the production toggle-gate facts plus
    # the top static readiness gate. Nothing runtime starts here (no server,
    # temp DATA_DIR) — enabling a2a cannot poll or spawn.
    save_enabled(DATA_DIR, name, True)
    assert load_enabled(DATA_DIR, name) is True
    reloaded = find_skill(DATA_DIR, name)
    assert reloaded is not None, f"{slug}: skill vanished from discovery after enable"
    readiness = skill_readiness_for_execution(DATA_DIR, reloaded)
    assert readiness.ready, (
        f"{slug}: readiness not ready — blockers={readiness.blockers} "
        f"agent_fixable={readiness.agent_fixable_blockers} "
        f"owner_action={readiness.owner_action_blockers}"
    )

    print(
        f"{slug}: review={status} slots={models} "
        f"actor_reported_valuation=${_actor_reported_valuation_usd(review):.4f} "
        f"elapsed={time.time() - started:.1f}s"
    )
