from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ensure_subprocess_host_deps.py"
SPEC = importlib.util.spec_from_file_location("ensure_subprocess_host_deps", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_load_extra_reqs_reads_file(tmp_path: Path, monkeypatch) -> None:
    sandbox_dir = tmp_path / "sandbox"
    sandbox_dir.mkdir()
    (sandbox_dir / "extras.txt").write_text(
        "# comment\ninline_snapshot\n\ndirty_equals\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(MODULE, "_SANDBOX_DIR", sandbox_dir)

    assert MODULE.load_extra_reqs() == ("inline_snapshot", "dirty_equals")


def test_build_install_plan_splits_wheel_online_and_installed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    wheels = tmp_path / "wheels"
    wheels.mkdir()
    (wheels / "pytest-8.3.4-py3-none-any.whl").write_text("", encoding="utf-8")
    (wheels / "attrs-24.0.0-py3-none-any.whl").write_text("", encoding="utf-8")

    monkeypatch.setattr(
        MODULE,
        "dist_installed",
        lambda req: MODULE.dist_name(req) == "dirty_equals",
    )

    wheel_reqs, online_reqs, installed_reqs, skipped_reqs = MODULE.build_install_plan(
        wheels=wheels,
        reqs=["pytest==8.3.4", "attrs", "inline_snapshot", "dirty_equals", "pluggy"],
        extra_reqs=("inline_snapshot", "dirty_equals", "attrs"),
    )

    assert wheel_reqs == ["pytest==8.3.4", "attrs"]
    assert online_reqs == ["inline_snapshot", "pluggy"]
    assert installed_reqs == ["dirty_equals"]
    assert skipped_reqs == []
