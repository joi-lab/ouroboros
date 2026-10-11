"""The host-temp admission exception recognizes the shared lock implementation."""

import ast

import pytest

from devtools.benchmarks.common import launcher_audit


def _lock_unit():
    module = launcher_audit._PRE_ADMISSION_LOCK_MODULE
    path = launcher_audit.REPO_ROOT.joinpath(*module.split(".")).with_suffix(".py")
    return launcher_audit._Unit(ast.parse(path.read_text(encoding="utf-8")), module)


def test_shared_platform_lock_is_recognized_before_admission():
    unit = _lock_unit()
    function = unit.functions["acquire_campaign_execution_lock"]
    assert launcher_audit._safe_pre_admission_lock_helper(function, unit)


@pytest.mark.parametrize("defect", ["wrong_import", "missing_nonblocking_lock", "extra_open"])
def test_lock_exception_still_refuses_unrecognized_or_extra_io(defect):
    unit = _lock_unit()
    function = unit.functions["acquire_campaign_execution_lock"]
    if defect == "wrong_import":
        unit.imports["file_lock_exclusive"] = "unrelated_module"
    elif defect == "missing_nonblocking_lock":
        call = next(node for node in ast.walk(function)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "file_lock_exclusive_nb")
        call.func.id = "not_a_lock"
    else:
        function.body.append(ast.parse("open('campaign-output', 'w')").body[0])
    assert not launcher_audit._safe_pre_admission_lock_helper(function, unit)
