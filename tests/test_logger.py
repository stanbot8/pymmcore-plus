from __future__ import annotations

import io
import multiprocessing
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest


def test_native_log_rotation_is_optional() -> None:
    from pymmcore_plus.core._mmcore_plus import _set_primary_log_file_rotation

    _set_primary_log_file_rotation(SimpleNamespace(), 10, 2)
    rotation = Mock()
    _set_primary_log_file_rotation(
        SimpleNamespace(setPrimaryLogFileRotation=rotation), 10, 2
    )
    rotation.assert_called_once_with(10, 2)


def _write_shared_log(
    log_file: str,
    hold_open: bool,
    ready: Any,
    release: Any,
    result: Any,
) -> None:
    sys.stderr = io.StringIO()
    from pymmcore_plus import CMMCorePlus
    from pymmcore_plus._logger import configure_logging, logger

    configure_logging(
        file=log_file,
        log_to_stderr=False,
        file_rotation=0.001,
        file_retention=10,
    )
    core = CMMCorePlus()
    if hold_open:
        ready.set()
        release.wait(timeout=30)
    else:
        ready.wait(timeout=30)
    for index in range(2):
        logger.warning("shared-record-%d-%d-%s", os.getpid(), index, "x" * 4000)
    if not hold_open:
        release.set()
    core_log = core.getPrimaryLogFile()
    core.setPrimaryLogFile("")
    result.put((sys.stderr.getvalue(), core_log))


@pytest.mark.skipif(sys.platform != "win32", reason="The issue affects Windows.")
def test_file_rotation_is_process_safe(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    result = context.Queue()
    processes = [
        context.Process(
            target=_write_shared_log,
            args=(
                str(tmp_path / "shared.log"),
                hold_open,
                ready,
                release,
                result,
            ),
        )
        for hold_open in (True, False)
    ]

    for process in processes:
        process.start()
    results = [result.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)

    assert [process.exitcode for process in processes] == [0, 0]
    assert not any("--- Logging error ---" in error for error, _ in results)
    assert len({core_log for _, core_log in results}) == 2
    log_files = list(tmp_path.glob("shared.log*"))
    assert len(log_files) > 1
    assert sum(path.read_text().count("shared-record-") for path in log_files) == 4


@pytest.mark.skipif(sys.platform != "win32", reason="The issue affects Windows.")
def test_cmmcore_uses_separate_log(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    from pymmcore_plus import CMMCorePlus
    from pymmcore_plus._logger import configure_logging

    shared_log = tmp_path / "shared.log"
    configure_logging(
        file=shared_log,
        log_to_stderr=False,
        file_rotation=0.01,
        file_retention=10,
    )
    core = CMMCorePlus()
    other_core = CMMCorePlus()
    core_log = Path(core.getPrimaryLogFile())
    native_rotation = callable(getattr(core, "setPrimaryLogFileRotation", None))
    try:
        assert core_log != Path(other_core.getPrimaryLogFile())
        for index in range(4):
            core.logMessage(f"native-record-{index}-{'x' * 4000}")
    finally:
        core.setPrimaryLogFile("")
        other_core.setPrimaryLogFile("")

    assert core_log.match(f"shared-cmmcore-{os.getpid()}-*.log")
    core_logs = list(tmp_path.glob(f"{core_log.stem}*{core_log.suffix}"))
    assert (len(core_logs) > 1) is native_rotation
    assert sum(path.read_text().count("native-record-") for path in core_logs) == 4
    assert "cannot rotate" not in capfd.readouterr().err.lower()
