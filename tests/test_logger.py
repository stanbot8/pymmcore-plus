from __future__ import annotations

import io
import multiprocessing
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
        ready.set()
        release.wait(timeout=30)
    else:
        ready.wait(timeout=30)
    for _ in range(2):
        logger.warning("x" * 4000)
    if not hold_open:
        release.set()
    result.put(sys.stderr.getvalue())


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
