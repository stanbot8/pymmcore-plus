from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
import platform
import re
import statistics
import subprocess
import tempfile
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from numpy.typing import DTypeLike

os.environ["PYMM_LOG_FILE"] = "0"


def _events(workload: str, run_id: str) -> list[Any]:
    import useq

    if workload == "high_throughput_mda":
        events = [
            useq.MDAEvent(index={"t": index}, exposure=0.001) for index in range(1_000)
        ]
    elif workload == "timed_mda":
        events = [
            useq.MDAEvent(
                index={"t": index // 2, "c": index % 2},
                exposure=10 if index % 2 == 0 else 50,
            )
            for index in range(100)
        ]
    elif workload == "parallel_mda":
        events = [useq.MDAEvent(index={"t": index}, exposure=10) for index in range(25)]
    else:
        raise ValueError(f"Unknown workload: {workload}")

    return [
        event.model_copy(
            update={
                "metadata": {
                    **event.metadata,
                    "application_benchmark_marker": f"{run_id}-{index:04d}",
                }
            }
        )
        for index, event in enumerate(events)
    ]


def _prepare_application(
    log_file: Path, workload: str, run_id: str
) -> tuple[Any, list[Any]]:
    import numpy as np

    from pymmcore_plus._logger import configure_logging
    from pymmcore_plus.experimental.unicore import SimpleCameraDevice, UniMMCore

    class BenchmarkCamera(SimpleCameraDevice):
        _exposure = 10.0

        def get_exposure(self) -> float:
            return self._exposure

        def set_exposure(self, exposure: float) -> None:
            self._exposure = exposure

        def sensor_shape(self) -> tuple[int, int]:
            return (64, 64)

        def dtype(self) -> DTypeLike:
            return np.uint16

        def snap(self, buffer: np.ndarray) -> dict[str, float]:
            time.sleep(self._exposure / 1000)
            buffer.fill(0)
            return {"exposure_ms": self._exposure}

    configure_logging(
        file=log_file,
        log_to_stderr=False,
        file_rotation=40,
        file_retention=20,
    )
    core = UniMMCore()
    core.mda.engine.use_hardware_sequencing = False
    core.loadPyDevice("Camera", BenchmarkCamera())
    core.initializeDevice("Camera")
    core.setCameraDevice("Camera")
    core.setAutoShutter(False)
    return core, _events(workload, run_id)


def _close_application(core: Any) -> None:
    from pymmcore_plus._logger import configure_logging

    try:
        core.unloadAllDevices()
    finally:
        configure_logging(file=None, log_to_stderr=False)


def _run_application(core: Any, events: list[Any]) -> tuple[float, int]:
    frame_count = 0

    def _count_frame(*_: Any) -> None:
        nonlocal frame_count
        frame_count += 1

    core.mda.events.frameReady.connect(_count_frame)
    start = time.perf_counter()
    core.mda.run(events)
    duration = time.perf_counter() - start
    core.mda.events.frameReady.disconnect(_count_frame)
    return duration, frame_count


def _markers(log_file: Path) -> list[str]:
    text = "".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in log_file.parent.glob(f"{log_file.name}*")
        if path.is_file()
    )
    return re.findall(r"application_benchmark_marker': '([^']+)", text)


def _run_single(workload: str, repetition: int) -> dict[str, float | int]:
    run_id = f"{workload}-r{repetition}"
    with tempfile.TemporaryDirectory() as directory:
        log_file = Path(directory) / "application.log"
        core = None
        try:
            setup_start = time.perf_counter()
            core, events = _prepare_application(log_file, workload, run_id)
            setup_seconds = time.perf_counter() - setup_start
            acquisition_seconds, frame_count = _run_application(core, events)
        finally:
            if core is not None:
                _close_application(core)
                core = None
                gc.collect()
            else:
                from pymmcore_plus._logger import configure_logging

                configure_logging(file=None, log_to_stderr=False)

        markers = _markers(log_file)
        expected = len(events)
        if frame_count != expected:
            raise RuntimeError(f"Expected {expected} frames. Found {frame_count}.")
        unique_markers = len(set(markers))
        return {
            "setup_seconds": setup_seconds,
            "acquisition_seconds": acquisition_seconds,
            "frames": frame_count,
            "expected_log_records": expected,
            "log_records": len(markers),
            "unique_log_records": unique_markers,
            "missing_log_records": expected - unique_markers,
            "duplicate_log_records": len(markers) - unique_markers,
            "log_correctness": len(markers) == expected and unique_markers == expected,
        }


def _parallel_worker(
    log_file: str,
    repetition: int,
    worker: int,
    ready: Any,
    start: Any,
    result: Any,
) -> None:
    core = None
    try:
        run_id = f"parallel_mda-r{repetition}-p{worker}"
        core, events = _prepare_application(Path(log_file), "parallel_mda", run_id)
        ready.put(None)
        if not start.wait(timeout=30):
            raise TimeoutError("The benchmark start event was not set.")
        duration, frame_count = _run_application(core, events)
        result.put(
            {
                "duration": duration,
                "frames": frame_count,
                "records": len(events),
                "error": "",
            }
        )
    except BaseException:
        result.put({"error": traceback.format_exc()})
    finally:
        if core is not None:
            _close_application(core)
            core = None
            gc.collect()


def _run_parallel(repetition: int, process_count: int) -> dict[str, float | int]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    result = context.Queue()
    with tempfile.TemporaryDirectory() as directory:
        log_file = Path(directory) / "application.log"
        processes = [
            context.Process(
                target=_parallel_worker,
                args=(
                    str(log_file),
                    repetition,
                    worker,
                    ready,
                    start,
                    result,
                ),
            )
            for worker in range(process_count)
        ]
        for process in processes:
            process.start()
        for _ in processes:
            ready.get(timeout=60)

        start_time = time.perf_counter()
        start.set()
        for process in processes:
            process.join(timeout=120)
        wall_seconds = time.perf_counter() - start_time
        worker_results = [result.get(timeout=30) for _ in processes]
        errors = [item["error"] for item in worker_results if item["error"]]
        if errors:
            raise RuntimeError("\n".join(errors))
        exit_codes = [process.exitcode for process in processes]
        if exit_codes != [0] * process_count:
            raise RuntimeError(f"Worker exit codes: {exit_codes}")

        markers = _markers(log_file)
        expected = sum(int(item["records"]) for item in worker_results)
        frame_count = sum(int(item["frames"]) for item in worker_results)
        if frame_count != expected:
            raise RuntimeError(f"Expected {expected} frames. Found {frame_count}.")
        unique_markers = len(set(markers))
        return {
            "acquisition_seconds": wall_seconds,
            "slowest_worker_seconds": max(
                float(item["duration"]) for item in worker_results
            ),
            "frames": frame_count,
            "expected_log_records": expected,
            "log_records": len(markers),
            "unique_log_records": unique_markers,
            "missing_log_records": expected - unique_markers,
            "duplicate_log_records": len(markers) - unique_markers,
            "log_correctness": len(markers) == expected and unique_markers == expected,
        }


def _workload_definition(name: str, process_count: int) -> dict[str, Any]:
    if name == "high_throughput_mda":
        return {
            "processes": 1,
            "frames_per_process": 1_000,
            "exposure_ms": [0.001],
        }
    if name == "timed_mda":
        return {
            "processes": 1,
            "frames_per_process": 100,
            "exposure_ms": [10, 50],
        }
    return {
        "processes": process_count,
        "frames_per_process": 25,
        "exposure_ms": [10],
    }


def _result_summary(
    name: str,
    runs: list[dict[str, float | int]],
    process_count: int,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": name,
        "application_path": "UniMMCore MDA with a Python simulated camera",
        "workload": _workload_definition(name, process_count),
        "runs": runs,
        "median_acquisition_seconds": statistics.median(
            float(run["acquisition_seconds"]) for run in runs
        ),
        "all_runs_correct": all(bool(run["log_correctness"]) for run in runs),
    }
    if all("setup_seconds" in run for run in runs):
        result["median_setup_seconds"] = statistics.median(
            float(run["setup_seconds"]) for run in runs
        )
    return result


def main() -> None:
    """Run application logging benchmarks."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--implementation", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--processes", type=int, default=4)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    checkout_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if checkout_commit != args.commit:
        parser.error(
            f"--commit is {args.commit}, but the checkout is {checkout_commit}."
        )

    import psutil

    runs: dict[str, list[dict[str, float | int]]] = {
        "high_throughput_mda": [],
        "timed_mda": [],
        "parallel_mda": [],
    }
    for repetition in range(args.repetitions):
        single_workloads = (
            ("high_throughput_mda", "timed_mda")
            if repetition % 2 == 0
            else ("timed_mda", "high_throughput_mda")
        )
        for workload in single_workloads:
            runs[workload].append(_run_single(workload, repetition))
        runs["parallel_mda"].append(_run_parallel(repetition, args.processes))

    output = json.dumps(
        {
            "schema_version": "1.0",
            "implementation": args.implementation,
            "commit": args.commit,
            "benchmark_source": {
                "path": str(Path(__file__).resolve()),
                "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            },
            "environment": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "processor": platform.processor(),
                "logical_cpu_count": os.cpu_count(),
                "memory_bytes": psutil.virtual_memory().total,
                "gpu": "not used",
                "process_priority": str(psutil.Process().nice()),
                "cpu_affinity": psutil.Process().cpu_affinity(),
                "temporary_directory": tempfile.gettempdir(),
                "pymmcore": importlib.metadata.version("pymmcore"),
                "useq_schema": importlib.metadata.version("useq-schema"),
            },
            "method": {
                "repetitions": args.repetitions,
                "statistic": "median",
                "file_rotation_mb": 40,
                "file_retention": 20,
                "parallel_processes": args.processes,
            },
            "workloads": [
                _result_summary(name, workload_runs, args.processes)
                for name, workload_runs in runs.items()
            ],
        },
        indent=2,
        sort_keys=True,
    )
    if args.output:
        args.output.write_text(f"{output}\n", encoding="utf-8")
    print(output)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
