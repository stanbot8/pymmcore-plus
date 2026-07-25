from __future__ import annotations

import io
import logging
import multiprocessing
import os
import sys
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from pathlib import Path


def _write_shared_log(
    log_file: str,
    hold_open: bool,
    ready: Any,
    release: Any,
    result: Any,
) -> None:
    sys.stderr = io.StringIO()
    from pymmcore_plus._logger import configure_logging, logger

    configure_logging(
        file=log_file,
        log_to_stderr=False,
        file_rotation=0.001,
        file_retention=10,
    )
    if hold_open:
        logger.warning("held-0-%s", "x" * 4000)
        ready.set()
        release.wait(timeout=30)
        logger.warning("held-1-%s", "x" * 4000)
    else:
        ready.wait(timeout=30)
        for index in range(2):
            logger.warning("rotator-%d-%s", index, "x" * 4000)
        release.set()
    result.put(sys.stderr.getvalue())


def _abandon_rotation(log_file: str) -> None:
    from pymmcore_plus._logger_windows import WindowsRotatingFileHandler

    handler = WindowsRotatingFileHandler(log_file)
    handler._wait_for_mutex()
    os.replace(log_file, f"{log_file}.1")
    os._exit(0)


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
    errors = [result.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(timeout=30)

    assert [process.exitcode for process in processes] == [0, 0]
    assert not any("--- Logging error ---" in error for error in errors)
    log_files = list(tmp_path.glob("shared.log*"))
    assert sum(path.read_text().count("\n") for path in log_files) == 4
    assert "held-1" in (tmp_path / "shared.log").read_text()


@pytest.mark.skipif(sys.platform != "win32", reason="The issue affects Windows.")
def test_file_rotation_recovers_after_process_exit(tmp_path: Path) -> None:
    from pymmcore_plus._logger_windows import WindowsRotatingFileHandler

    context = multiprocessing.get_context("spawn")
    log_file = str(tmp_path / "shared.log")
    handler = WindowsRotatingFileHandler(log_file)
    test_logger = logging.Logger("test")
    test_logger.addHandler(handler)
    try:
        test_logger.warning("before")
        interrupted = context.Process(target=_abandon_rotation, args=(log_file,))
        interrupted.start()
        interrupted.join(timeout=30)
        assert interrupted.exitcode == 0
        test_logger.warning("after")
    finally:
        handler.close()

    assert "after" in (tmp_path / "shared.log").read_text()
    assert "before" in (tmp_path / "shared.log.1").read_text()
