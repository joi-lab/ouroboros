"""Pip targets the invoked venv even when its binary belongs to the bundle."""

import json
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile

import pytest

from ouroboros.launcher_bootstrap import embedded_python_env
from ouroboros.platform_layer import pip_install_target_args


@pytest.mark.parametrize("layout", ["bin/python3", "Scripts/python.exe"])
@pytest.mark.parametrize("linked", [False, True], ids=["copied", "symlink"])
def test_confirmed_venv_precedes_embedded_binary_resolution(tmp_path, layout, linked):
    bundled = tmp_path / "python-standalone" / "bin" / "python3"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"interpreter fixture")
    # A copied venv under this ancestor also defeats ancestry-only classification.
    venv = tmp_path / "python-standalone" / "work-environment"
    invocation = venv / pathlib.Path(layout)
    invocation.parent.mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = bundled\n", encoding="utf-8")
    if linked:
        try:
            invocation.symlink_to(bundled)
        except OSError as exc:
            pytest.skip(f"symlink creation unavailable: {exc}")
    else:
        shutil.copyfile(bundled, invocation)
    assert pip_install_target_args(str(invocation)) == []


def test_venv_name_and_ambient_env_do_not_override_direct_bundle(tmp_path, monkeypatch):
    bundled = tmp_path / "python-standalone" / ".venv" / "bin" / "python3"
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"interpreter fixture")
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / "unrelated-env"))
    assert pip_install_target_args(str(bundled)) == ["--user"]
    alias = tmp_path / "python-alias"
    try:
        alias.symlink_to(bundled)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")
    assert pip_install_target_args(str(alias)) == ["--user"]


def test_plain_python_and_nonstandard_cfg_neighbor(tmp_path):
    plain = tmp_path / "usr" / "bin" / "python3"
    assert pip_install_target_args(str(plain)) == []
    bundled = tmp_path / "python-standalone" / "python.exe"
    bundled.parent.mkdir()
    bundled.write_bytes(b"interpreter fixture")
    (bundled.parent / "pyvenv.cfg").write_text("home = elsewhere\n", encoding="utf-8")
    assert pip_install_target_args(str(bundled)) == ["--user"]


@pytest.mark.serial
def test_real_venv_installs_and_imports_local_wheel(tmp_path):
    venv = tmp_path / "work-environment"
    env = embedded_python_env(tmp_path / "data")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], env=env, check=True, timeout=90)
    interpreter = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    wheel = tmp_path / "ouroboros_venv_probe-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ouroboros_venv_probe.py", "VALUE = 'local wheel imported'\n")
        archive.writestr("ouroboros_venv_probe-1.0.dist-info/METADATA",
                         "Metadata-Version: 2.1\nName: ouroboros-venv-probe\nVersion: 1.0\n")
        archive.writestr("ouroboros_venv_probe-1.0.dist-info/WHEEL",
                         "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr("ouroboros_venv_probe-1.0.dist-info/RECORD", "")
    subprocess.run(
        [str(interpreter), "-m", "pip", "install", *pip_install_target_args(str(interpreter)),
         "--no-index", "--no-deps", "--no-cache-dir", str(wheel)],
        env=env, check=True, capture_output=True, text=True, timeout=60,
    )
    result = subprocess.run(
        [str(interpreter), "-c", "import json,sys,ouroboros_venv_probe as p; "
         "print(json.dumps([sys.prefix,p.__file__,p.VALUE]))"],
        env=env, check=True, capture_output=True, text=True, timeout=10,
    )
    prefix, module, value = json.loads(result.stdout)
    assert pathlib.Path(prefix).resolve() == venv.resolve()
    assert venv.resolve() in pathlib.Path(module).resolve().parents
    assert value == "local wheel imported"
