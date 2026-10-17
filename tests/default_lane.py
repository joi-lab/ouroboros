"""Whole-file shards of CI's default suite, preserving its two existing passes.

Weights are the number of collected default nodes per file, before the serial
split. Greedy largest-first packing (path and shard-index tie breaks) needs no
historical timing file or hash seed. Both passes therefore assign a mixed file
to the same runner. Opt-in lanes never contribute weight or join the selection.

An independent, unsharded --default-suite-manifest collection witnesses each
pass. tests/ci_evidence.py reconciles every shard against those manifests. Skips
retain ordinary pytest semantics; this selector neither adds nor removes them.
"""
from collections import Counter
import re

import pytest

from ouroboros.preflight_runner import LANE_EXCLUSION_EXPR
from tests.browser_lane import _COLLECTION_INI, _NARROWING_OPTIONS
from tests.ci_evidence import file_shards


def pytest_addoption(parser):
    parser.addoption("--default-suite-shard", default=None, metavar="K/N",
                     help="Run whole-file shard K/N of the default parallel or serial pass")
    parser.addoption("--default-suite-manifest", action="store_true",
                     help="Export an unsharded collect-only witness of a default pass")


def _enabled(config):
    return config.getoption("--default-suite-shard") or config.getoption("--default-suite-manifest")


def _shard(config):
    value = config.getoption("--default-suite-shard")
    if value is None:
        return None
    match = re.fullmatch(r"([1-9][0-9]*)/([1-9][0-9]*)", value)
    if match is None or int(match[1]) > int(match[2]):
        raise pytest.UsageError("DEFAULT_SUITE_SHARD: require K/N with 1 <= K <= N")
    return int(match[1]), int(match[2])


def pytest_configure(config):
    if not _enabled(config):
        return
    shard = _shard(config)
    manifest = config.getoption("--default-suite-manifest")
    if manifest and (shard or not config.option.collectonly):
        raise pytest.UsageError("DEFAULT_SUITE_MANIFEST: require unsharded --collect-only")
    expressions = {f"{prefix}serial and {LANE_EXCLUSION_EXPR}": name
                   for prefix, name in (("not ", "parallel"), ("", "serial"))}
    if config.option.markexpr not in expressions:
        raise pytest.UsageError("DEFAULT_SUITE_INCOMPLETE: require the unchanged default pass expression")
    config._default_suite_pass = expressions[config.option.markexpr]
    narrowed = any(getattr(config.option, name, None) for name in _NARROWING_OPTIONS)
    overridden = any(entry.split("=", 1)[0].strip() in _COLLECTION_INI
                     for entry in (config.option.override_ini or []))
    if config.args != ["tests/"] or narrowed or overridden:
        raise pytest.UsageError("DEFAULT_SUITE_INCOMPLETE: require tests/ without collection narrowing")


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_collection_modifyitems(config, items):
    collected = list(items) if _enabled(config) else []
    yield  # conftest adds file-level serial markers; pytest applies the normal -m selection.
    if not _enabled(config):
        return
    excluded = [term.removeprefix("not ") for term in LANE_EXCLUSION_EXPR.split(" and ")]
    default = [item for item in collected
               if not any(item.get_closest_marker(marker) for marker in excluded)]
    serial = config._default_suite_pass == "serial"
    expected = sorted(item.nodeid for item in default
                      if bool(item.get_closest_marker("serial")) == serial)
    if not expected or sorted(item.nodeid for item in items) != expected:
        raise pytest.UsageError("DEFAULT_SUITE_INCOMPLETE: collected selection differs from the default pass")
    weights = dict(sorted(Counter(item.nodeid.split("::", 1)[0] for item in default).items()))
    # Paths are values, never object keys: the public redactor treats keys containing
    # "credential" or "token" as secrets, even when their values are harmless counts.
    lane = {"full": expected, "file_weights": [[path, weight] for path, weight in weights.items()],
            "pass": config._default_suite_pass}
    shard = _shard(config)
    if shard:
        index, count = shard
        owners = file_shards(weights, count)
        assigned = [node for node in expected if owners[node.split("::", 1)[0]] == index]
        if not assigned:
            raise pytest.UsageError(f"DEFAULT_SUITE_INCOMPLETE: empty shard {index}/{count}")
        kept = set(assigned)
        deselected = [item for item in items if item.nodeid not in kept]
        items[:] = [item for item in items if item.nodeid in kept]
        config.hook.pytest_deselected(items=deselected)
        lane.update(shard=list(shard), assigned=assigned)
    config._default_suite_lane = lane


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    """xdist's controller does not collect: retain the workers' agreed lane proof."""
    if not _enabled(node.config):
        return
    lane = getattr(node, "workeroutput", {}).get("default_suite_lane")
    if lane is None:
        return  # A dead worker makes pytest red and cannot supply a complete projection.
    prior = getattr(node.config, "_default_suite_lane", lane)
    if prior != lane:
        node.config._default_suite_mismatch = True
    node.config._default_suite_lane = lane


def pytest_sessionfinish(session):
    config = session.config
    lane = getattr(config, "_default_suite_lane", None)
    if hasattr(config, "workeroutput") and lane is not None:
        config.workeroutput["default_suite_lane"] = lane
    if getattr(config, "_default_suite_mismatch", False):
        print("DEFAULT_SUITE_INCOMPLETE: workers disagree about the complete lane")
        session.exitstatus = 1
