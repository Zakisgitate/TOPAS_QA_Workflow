"""Case-aware TOPAS runtime estimates and lightweight macOS process monitoring."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import csv
import math
import os
from pathlib import Path
import platform
import re
import subprocess
import threading
import time
from typing import Any, Optional


FALLBACK_HISTORIES = 150_000
FALLBACK_THREADS = 4
FALLBACK_SECONDS = 6_831.7


@dataclass(frozen=True)
class RuntimeBenchmark:
    log_path: str
    beam_model_mode: str
    histories: int
    threads: int
    spots: int
    real_seconds: float
    user_seconds: float
    system_seconds: float
    average_cpu_cores: float


_BENCHMARK_LOCK = threading.RLock()
_BENCHMARK_CACHE: dict[str, tuple[tuple[tuple[str, int, int], ...], list[RuntimeBenchmark]]] = {}
_MONITOR_LOCK = threading.RLock()
_MONITOR_CACHE: tuple[float, dict[str, Any]] = (0.0, {})
_HARDWARE_CACHE: Optional[dict[str, Any]] = None


def _read_allocation(path: Path) -> tuple[int, int]:
    if not path.is_file():
        return 0, 0
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    try:
        return len(rows), sum(max(0, int(row.get("AllocatedHistories", 0))) for row in rows)
    except (TypeError, ValueError):
        return len(rows), 0


def _log_context(root: Path, path: Path, text: str) -> tuple[str, int]:
    if path.parent.name == "configuration":
        summary = path.parent / "topas_plan_generation_summary.txt"
        allocation = path.parent / "spot_history_allocation.csv"
        summary_text = summary.read_text(encoding="utf-8", errors="replace") if summary.is_file() else ""
        mode_match = re.search(r"^Beam model mode:\s*(\S+)", summary_text, re.MULTILINE)
        mode = mode_match.group(1).strip().lower() if mode_match else (
            "commissioned" if "Particle source PlanCarbonBeamLayer" in text else "baseline"
        )
    else:
        allocation = root / "plan_parsed" / "spot_history_allocation.csv"
        # Production keeps several historical logs while plan_parsed describes
        # only the newest run, so infer old production-log mode from the source
        # names contained in that log rather than the current summary.
        mode = "commissioned" if "Particle source PlanCarbonBeamLayer" in text else "baseline"
    spots, _histories = _read_allocation(allocation)
    if not spots:
        spots = len(set(int(value) for value in re.findall(r"Begin processing for Run:\s*(\d+)", text)))
    return mode, spots


def _parse_benchmark(root: Path, path: Path) -> Optional[RuntimeBenchmark]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    threads_match = re.search(r"setting number of threads to:\s*(\d+)", text)
    total_match = re.search(
        r"Total:\s+User=([\d.]+)s Real=([\d.]+)s Sys=([\d.]+)s", text
    )
    if not threads_match or not total_match:
        return None
    source_histories = [
        int(value) for value in re.findall(r"Total number of histories:\s*(\d+)", text)
    ]
    histories = sum(source_histories)
    if histories <= 0:
        return None
    user_seconds = float(total_match.group(1))
    real_seconds = float(total_match.group(2))
    system_seconds = float(total_match.group(3))
    if real_seconds <= 0:
        return None
    mode, spots = _log_context(root, path, text)
    return RuntimeBenchmark(
        log_path=str(path.resolve()),
        beam_model_mode=mode,
        histories=histories,
        threads=int(threads_match.group(1)),
        spots=spots,
        real_seconds=real_seconds,
        user_seconds=user_seconds,
        system_seconds=system_seconds,
        average_cpu_cores=(user_seconds + system_seconds) / real_seconds,
    )


def discover_runtime_benchmarks(root: Path) -> list[RuntimeBenchmark]:
    root = root.expanduser().resolve()
    paths = sorted(
        set((root / "topas_output").rglob("run_full_plan_qa_*.log"))
        | set((root / "analysis").rglob("run_full_plan_qa_*.log"))
    )
    signature = tuple(
        (str(path), path.stat().st_mtime_ns, path.stat().st_size)
        for path in paths
        if path.is_file()
    )
    key = str(root)
    with _BENCHMARK_LOCK:
        cached = _BENCHMARK_CACHE.get(key)
        if cached and cached[0] == signature:
            return list(cached[1])
    parsed = [item for path in paths if (item := _parse_benchmark(root, path)) is not None]
    unique: dict[tuple[Any, ...], RuntimeBenchmark] = {}
    for item in parsed:
        identity = (
            item.beam_model_mode,
            item.histories,
            item.threads,
            item.spots,
            round(item.real_seconds, 3),
        )
        unique[identity] = item
    result = list(unique.values())
    with _BENCHMARK_LOCK:
        _BENCHMARK_CACHE[key] = (signature, result)
    return result


def logical_cpu_count() -> int:
    """Logical CPUs on this machine: the hard upper bound for Geant4 workers."""

    return max(1, int(_hardware().get("logical_cpus") or os.cpu_count() or 1))


def physical_cpu_count() -> Optional[int]:
    """Physical cores when the platform reports them; None otherwise."""

    value = _hardware().get("physical_cpus")
    return int(value) if isinstance(value, int) and value > 0 else None


def clamp_threads(requested: int) -> tuple[int, str]:
    """Converge a requested worker count onto the local logical-core count.

    Oversubscribing Geant4 MT workers buys no throughput and costs wall time:
    on this 15-core machine the same 43,919-spot / 100,000-history plan spent
    69 s of system time with 4 workers and 1,017-1,211 s with 64, and ran
    1.4-2.1x longer overall. Returns the value that will actually be written to
    Ts/NumberOfThreads plus a human-readable note ("" when nothing was capped).
    """

    limit = logical_cpu_count()
    try:
        value = int(requested)
    except (TypeError, ValueError):
        value = limit
    if value < 1:
        value = 1
    if value <= limit:
        return value, ""
    return limit, (
        f"Requested {value} threads exceeds the {limit} logical CPUs on this machine; "
        f"using {limit}. More Geant4 workers than cores only adds context switching "
        "and makes the run slower and non-reproducible."
    )


def estimate_topas_runtime(
    histories: int,
    threads: int,
    *,
    root: Optional[Path] = None,
    beam_model_mode: str = "baseline",
    spot_count: int = 0,
) -> dict[str, Any]:
    """Estimate wall time without assuming requested threads are all occupied."""

    histories = max(1, int(histories))
    threads = max(1, int(threads))
    mode = str(beam_model_mode or "baseline").strip().lower()
    benchmarks = discover_runtime_benchmarks(root) if root is not None else []
    matches = [item for item in benchmarks if item.beam_model_mode == mode]
    if spot_count > 0:
        same_layout = [item for item in matches if item.spots == spot_count]
        if same_layout:
            matches = same_layout
    if matches:
        reference = min(
            matches,
            key=lambda item: abs(math.log(histories / item.histories))
            + 0.15 * abs(math.log(threads / item.threads)),
        )
        target_spots = max(1, spot_count or reference.spots or 1)
        reference_spots = max(1, reference.spots or target_spots)
        reference_capacity = min(
            reference.threads, max(1.0, reference.histories / reference_spots)
        )
        target_capacity = min(threads, max(1.0, histories / target_spots))
        reference_cores = max(0.5, min(reference_capacity, reference.average_cpu_cores))
        reference_efficiency = reference_cores / reference_capacity
        target_efficiency = min(
            0.82,
            reference_efficiency
            + 0.16 * (1.0 - math.exp(-max(0.0, target_capacity - reference_capacity) / 8.0)),
        )
        target_cores = max(0.5, target_capacity * target_efficiency)
        overhead_fraction = 0.35 if mode == "commissioned" else 0.25
        workload_scale = (
            overhead_fraction * target_spots / reference_spots
            + (1.0 - overhead_fraction)
            * histories
            / reference.histories
            * reference_cores
            / target_cores
        )
        reference_idle_threads = max(0.0, reference.threads - reference_capacity)
        target_idle_threads = max(0.0, threads - target_capacity)
        thread_penalty_ratio = (
            1.0 + 0.003 * target_idle_threads
        ) / (1.0 + 0.003 * reference_idle_threads)
        seconds = max(1.0, reference.real_seconds * workload_scale * thread_penalty_ratio)
        exact = (
            histories == reference.histories
            and threads == reference.threads
            and target_spots == reference_spots
        )
        if exact:
            # An identical completed run is stronger evidence than any scaling
            # model.  Preserve its measured wall time exactly; even a small
            # model penalty here would make the UI disagree with its own
            # benchmark log.
            seconds = reference.real_seconds
        low_factor, high_factor = ((0.9, 1.1) if exact else (0.65, 1.65))
        confidence = "high" if exact else "medium"
        method = "matching completed case run" if exact else "case benchmark extrapolation"
        return {
            "hours": seconds / 3600.0,
            "low_hours": seconds * low_factor / 3600.0,
            "high_hours": seconds * high_factor / 3600.0,
            "seconds": seconds,
            "confidence": confidence,
            "method": method,
            "effective_threads": target_cores,
            "spot_count": target_spots,
            "histories_per_spot": histories / target_spots,
            "benchmark": asdict(reference),
        }

    ratio = threads / FALLBACK_THREADS
    speedup = ratio if ratio <= 1.0 else 1.0 + 0.35 * (ratio - 1.0)
    seconds = FALLBACK_SECONDS * histories / FALLBACK_HISTORIES / speedup
    return {
        "hours": seconds / 3600.0,
        "low_hours": 0.5 * seconds / 3600.0,
        "high_hours": 4.0 * seconds / 3600.0,
        "seconds": seconds,
        "confidence": "low",
        "method": "legacy fallback; no matching completed case run",
        "effective_threads": min(float(threads), max(1.0, histories / max(1, spot_count))),
        "spot_count": max(0, spot_count),
        "histories_per_spot": histories / max(1, spot_count),
        "benchmark": {
            "histories": FALLBACK_HISTORIES,
            "threads": FALLBACK_THREADS,
            "real_seconds": FALLBACK_SECONDS,
            "beam_model_mode": "legacy water phantom",
        },
    }


def _hardware() -> dict[str, Any]:
    global _HARDWARE_CACHE
    if _HARDWARE_CACHE is not None:
        return dict(_HARDWARE_CACHE)
    logical = max(1, os.cpu_count() or 1)
    values: list[str] = []
    try:
        result = subprocess.run(
            ["sysctl", "-n", "hw.physicalcpu", "hw.memsize", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        values = result.stdout.splitlines()
    except (OSError, subprocess.SubprocessError):
        pass
    if len(values) < 3:
        try:
            profiler = subprocess.run(
                ["system_profiler", "SPHardwareDataType", "-detailLevel", "mini"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            ).stdout
            core_match = re.search(r"Total Number of Cores:\s*(\d+)", profiler)
            memory_match = re.search(r"Memory:\s*([\d.]+)\s*GB", profiler)
            chip_match = re.search(r"Chip:\s*(.+)", profiler)
            values = [
                core_match.group(1) if core_match else "",
                str(int(float(memory_match.group(1)) * 1024**3)) if memory_match else "",
                chip_match.group(1).strip() if chip_match else platform.processor(),
            ]
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    _HARDWARE_CACHE = {
        "logical_cpus": logical,
        "physical_cpus": int(values[0]) if len(values) > 0 and values[0].isdigit() else None,
        "memory_total_bytes": int(values[1]) if len(values) > 1 and values[1].isdigit() else None,
        "cpu_model": values[2].strip() if len(values) > 2 else "",
    }
    return dict(_HARDWARE_CACHE)


def _thread_count(pid: int) -> Optional[int]:
    try:
        result = subprocess.run(
            ["ps", "-M", "-p", str(pid)], capture_output=True, text=True, timeout=2, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line for line in result.stdout.splitlines()[1:] if line.strip()]
    return len(lines) or None


def _memory_status(total_bytes: Optional[int]) -> dict[str, Any]:
    if not total_bytes:
        return {}
    try:
        result = subprocess.run(
            ["vm_stat"], capture_output=True, text=True, timeout=2, check=False
        )
        page_match = re.search(r"page size of (\d+) bytes", result.stdout)
        page_size = int(page_match.group(1)) if page_match else 4096
        values = {
            key: int(value)
            for key, value in re.findall(r"^Pages ([^:]+):\s+(\d+)\.", result.stdout, re.MULTILINE)
        }
        available_pages = (
            values.get("free", 0) + values.get("inactive", 0) + values.get("speculative", 0)
        )
        available = min(total_bytes, available_pages * page_size)
        used = max(0, total_bytes - available)
        return {
            "memory_used_bytes": used,
            "memory_available_bytes": available,
            "memory_percent": 100.0 * used / total_bytes,
        }
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


def collect_process_status(process_group_id: Optional[int]) -> dict[str, Any]:
    """Collect task-group and system status, cached to avoid polling ps too often."""

    global _MONITOR_CACHE
    now = time.monotonic()
    with _MONITOR_LOCK:
        if _MONITOR_CACHE[1] and now - _MONITOR_CACHE[0] < 1.8:
            cached = dict(_MONITOR_CACHE[1])
            if process_group_id == cached.get("requested_process_group_id"):
                return cached
    hardware = _hardware()
    rows: list[dict[str, Any]] = []
    error = ""
    try:
        result = subprocess.run(
            [
                "ps", "-ww", "-axo",
                "pid=,ppid=,pgid=,%cpu=,%mem=,rss=,vsz=,etime=,state=,comm=,args=",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 10)
            if len(parts) < 10:
                continue
            try:
                arguments = parts[10] if len(parts) > 10 else parts[9]
                first_argument = arguments.split(None, 1)[0] if arguments.strip() else parts[9]
                rows.append(
                    {
                        "pid": int(parts[0]), "ppid": int(parts[1]), "pgid": int(parts[2]),
                        "cpu_percent": float(parts[3]), "memory_percent": float(parts[4]),
                        "rss_bytes": int(parts[5]) * 1024, "virtual_bytes": int(parts[6]) * 1024,
                        "os_elapsed": parts[7], "state": parts[8],
                        "command": Path(first_argument).name,
                        "arguments": arguments,
                    }
                )
            except ValueError:
                continue
        if result.returncode != 0:
            error = result.stderr.strip() or f"ps exited with status {result.returncode}"
    except (OSError, subprocess.SubprocessError) as exc:
        error = str(exc)
    logical = int(hardware["logical_cpus"])
    task_rows = [row for row in rows if process_group_id is not None and row["pgid"] == process_group_id]
    for row in task_rows:
        row["threads"] = _thread_count(int(row["pid"]))
    group_cpu = sum(float(row["cpu_percent"]) for row in task_rows)
    system_cpu = sum(max(0.0, float(row["cpu_percent"])) for row in rows)
    top_processes = sorted(rows, key=lambda row: float(row["cpu_percent"]), reverse=True)[:5]
    load = os.getloadavg() if hasattr(os, "getloadavg") else (math.nan, math.nan, math.nan)
    payload = {
        **hardware,
        **_memory_status(hardware.get("memory_total_bytes")),
        "requested_process_group_id": process_group_id,
        "processes": task_rows,
        "top_system_processes": top_processes,
        "task_cpu_percent": group_cpu,
        "task_cpu_cores": group_cpu / 100.0,
        "task_cpu_normalized_percent": group_cpu / logical,
        "task_rss_bytes": sum(int(row["rss_bytes"]) for row in task_rows),
        "task_os_threads": sum(int(row.get("threads") or 0) for row in task_rows),
        "system_cpu_percent": min(100.0, system_cpu / logical),
        "load_average": list(load),
        "sampled_at_epoch": time.time(),
        "error": error,
    }
    with _MONITOR_LOCK:
        _MONITOR_CACHE = (now, payload)
    return dict(payload)
