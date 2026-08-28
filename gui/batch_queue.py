"""Persistent multi-case TOPAS queue with per-job process control."""

from __future__ import annotations

import csv
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import threading
import time
from typing import Any, Callable, Optional
import uuid

from gui.runtime_monitor import clamp_threads, collect_process_status


Command = tuple[str, list[str], Path, Optional[Path]]
BuildAction = Callable[[str, dict[str, Any]], tuple[str, list[Command]]]
InitializeCase = Callable[[Path], None]
CanStart = Callable[[], bool]


class QueueCancelled(RuntimeError):
    pass


# A queued case runs the same sequence the Workflow tab does by hand. Everything
# after the transport only reads the dose binary, so a failure there costs
# nothing but the analysis itself -- the expensive result is already on disk.
DEFAULT_PHASES = ("pipeline", "preflight", "run_topas", "analyze", "gamma")
POST_TRANSPORT_ACTIONS = frozenset({"analyze", "gamma"})


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_job_id(value: str) -> str:
    value = str(value).strip()
    if not re.fullmatch(r"[a-f0-9]{12}", value):
        raise RuntimeError("Invalid queue job id")
    return value


# "04_generate_topas_plan.py exited with status 1" is true and useless: the one
# line that says what to do about it is buried in the per-case run log. Pull it
# up into the job record so the queue table shows it.
_FAILURE_LINE = re.compile(r"^[\w.]*(?:Error|Exception)\s*:\s*\S")
_FAILURE_MARKERS = ("BLOCK", "ERROR:", "FAILED", "Traceback (most recent call last)")


def _failure_reason(captured: list[str], limit: int = 400) -> str:
    lines = [
        stripped
        for chunk in captured
        for line in str(chunk).splitlines()
        if (stripped := line.strip())
    ]
    if not lines:
        return ""
    for line in reversed(lines):
        if _FAILURE_LINE.match(line) or line.startswith(_FAILURE_MARKERS):
            return f" — {line[:limit]}"
    return f" — {lines[-1][:limit]}"


def _allocation(root: Path) -> list[int]:
    path = root / "plan_parsed" / "spot_history_allocation.csv"
    if not path.is_file():
        return []
    result: list[int] = []
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream):
            try:
                result.append(max(0, int(row.get("AllocatedHistories", 0))))
            except (TypeError, ValueError):
                result.append(0)
    return result


class JobRuntime:
    def __init__(self, manager: "BatchQueueManager", job_id: str):
        self.manager = manager
        self.job_id = job_id
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.process: Optional[subprocess.Popen[str]] = None
        self.process_group_id: Optional[int] = None
        self.pause_requested = False
        self.cancel_requested = False
        self.transport_started_monotonic: Optional[float] = None
        self.transport_fraction = 0.0
        self.pause_started_monotonic: Optional[float] = None
        self.paused_accumulated_seconds = 0.0

    def append(self, text: str) -> None:
        self.manager.append_log(self.job_id, text)

    def check_cancelled(self) -> None:
        with self.lock:
            if self.cancel_requested:
                raise QueueCancelled("Cancelled by user")

    def wait_if_paused(self) -> None:
        with self.condition:
            while self.pause_requested and not self.cancel_requested:
                self.condition.wait(timeout=1.0)
            if self.cancel_requested:
                raise QueueCancelled("Cancelled by user")

    def pause(self) -> str:
        with self.condition:
            if self.pause_requested:
                return "Job is already paused"
            self.pause_requested = True
            self.pause_started_monotonic = time.monotonic()
            group_id = self.process_group_id
            if group_id is not None and hasattr(signal, "SIGSTOP"):
                try:
                    os.killpg(group_id, signal.SIGSTOP)
                except ProcessLookupError:
                    pass
            self.manager.update_job(
                self.job_id,
                status="paused",
                detail="Paused; process memory and output files are preserved",
                force=True,
            )
            return "Job paused"

    def resume(self) -> str:
        with self.condition:
            if not self.pause_requested:
                return "Job is already running"
            if self.pause_started_monotonic is not None:
                self.paused_accumulated_seconds += max(
                    0.0, time.monotonic() - self.pause_started_monotonic
                )
            self.pause_started_monotonic = None
            group_id = self.process_group_id
            if group_id is not None and hasattr(signal, "SIGCONT"):
                try:
                    os.killpg(group_id, signal.SIGCONT)
                except ProcessLookupError:
                    pass
            self.pause_requested = False
            self.pause_started_monotonic = None
            self.condition.notify_all()
            self.manager.update_job(
                self.job_id,
                status="running",
                detail="Resumed",
                force=True,
            )
            return "Job resumed"

    def cancel(self) -> str:
        with self.condition:
            self.cancel_requested = True
            group_id = self.process_group_id
            if group_id is not None:
                try:
                    if self.pause_requested and hasattr(signal, "SIGCONT"):
                        os.killpg(group_id, signal.SIGCONT)
                    os.killpg(group_id, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            self.pause_requested = False
            self.condition.notify_all()
            self.manager.update_job(
                self.job_id,
                status="cancelling",
                detail="Termination requested",
                force=True,
            )
            return "Job cancellation requested"


class BatchQueueManager:
    """Schedule complete case workflows with a hard local concurrency cap of two."""

    schema_version = 1

    def __init__(
        self,
        storage_path: Path,
        *,
        initialize_case: InitializeCase,
        build_action: BuildAction,
        can_start: CanStart,
        phases: tuple[str, ...] = DEFAULT_PHASES,
    ):
        self.storage_path = storage_path.expanduser().resolve()
        self.initialize_case = initialize_case
        self.build_action = build_action
        self.can_start = can_start
        self.phases = tuple(phases)
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.runtimes: dict[str, JobRuntime] = {}
        self.enabled = False
        self.max_parallel = 1
        self._last_persist = 0.0
        self._load()

    def _load(self) -> None:
        if not self.storage_path.is_file():
            return
        try:
            payload = json.loads(self.storage_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if int(payload.get("schema_version", 0)) != self.schema_version:
            return
        self.max_parallel = min(2, max(1, int(payload.get("max_parallel", 1))))
        records = payload.get("jobs", [])
        if not isinstance(records, list):
            return
        for record in records:
            if not isinstance(record, dict):
                continue
            try:
                job_id = _safe_job_id(str(record.get("id", "")))
            except RuntimeError:
                continue
            job = dict(record)
            if job.get("status") in {"running", "paused", "cancelling"}:
                job["status"] = "interrupted"
                job["error"] = (
                    "The GUI process ended while this job was active. Verify that no orphan TOPAS "
                    "process remains, then use Retry."
                )
                job["detail"] = "Interrupted by GUI shutdown; manual retry required"
                job["finished_at"] = _now_iso()
            job["process_group_id"] = None
            self.jobs[job_id] = job
            self.order.append(job_id)
        self._persist_locked(force=True)

    def _payload_locked(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "updated_at": _now_iso(),
            "enabled": self.enabled,
            "max_parallel": self.max_parallel,
            "jobs": [self.jobs[job_id] for job_id in self.order if job_id in self.jobs],
        }

    def _persist_locked(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_persist < 2.0:
            return
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.storage_path.with_suffix(self.storage_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self._payload_locked(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.storage_path)
        self._last_persist = now

    def update_job(self, job_id: str, force: bool = False, **updates: Any) -> None:
        with self.lock:
            if job_id not in self.jobs:
                return
            self.jobs[job_id].update(updates)
            self.jobs[job_id]["updated_at"] = _now_iso()
            self._persist_locked(force=force)

    def append_log(self, job_id: str, text: str) -> None:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                return
            path = Path(str(job["log_path"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(text)

    def enqueue(
        self,
        root: Path,
        payload: dict[str, Any],
        *,
        label: str,
        estimate: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        root = root.expanduser().resolve()
        output_tag = str(payload.get("output_tag", "")).strip()
        with self.lock:
            duplicate = next(
                (
                    job
                    for job in self.jobs.values()
                    if job.get("case_root") == str(root)
                    and job.get("output_tag") == output_tag
                    and job.get("status") in {"queued", "running", "paused", "cancelling"}
                ),
                None,
            )
            if duplicate:
                raise RuntimeError(
                    f"This case/tag is already in the active queue: {duplicate['id']}"
                )
            job_id = uuid.uuid4().hex[:12]
            log_path = root / "analysis" / "_batch_queue" / f"job-{job_id}" / "run.log"
            job = {
                "id": job_id,
                "case_root": str(root),
                "label": label or root.name,
                "output_tag": output_tag,
                "histories": int(payload.get("histories", 0)),
                "threads": int(payload.get("threads", 0)),
                # The value actually written to Ts/NumberOfThreads is "threads";
                # "requested_threads" keeps the operator's original input visible
                # when the local core count capped it.
                "requested_threads": int(
                    payload.get("requested_threads", payload.get("threads", 0))
                ),
                "beam_model_mode": str(payload.get("beam_model_mode", "baseline")),
                "status": "queued",
                "detail": "Waiting for an available local slot",
                "stage": "Waiting",
                "progress": 0.0,
                "transport_progress": 0.0,
                "created_at": _now_iso(),
                "updated_at": _now_iso(),
                "started_at": "",
                "finished_at": "",
                "attempts": 0,
                "error": "",
                "process_group_id": None,
                "payload": dict(payload),
                "estimate": dict(estimate or {}),
                "log_path": str(log_path),
            }
            self.jobs[job_id] = job
            self.order.append(job_id)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                f"Batch queue job {job_id}\nCase: {root}\nCreated: {_now_iso()}\n",
                encoding="utf-8",
            )
            self._persist_locked(force=True)
        self.kick()
        return dict(job)

    def set_enabled(self, enabled: bool) -> None:
        with self.lock:
            self.enabled = bool(enabled)
            self._persist_locked(force=True)
        if enabled:
            self.kick()

    def set_max_parallel(self, value: int) -> None:
        value = int(value)
        if value not in {1, 2}:
            raise RuntimeError("Local parallel jobs must be 1 or 2")
        with self.lock:
            self.max_parallel = value
            self._persist_locked(force=True)
        self.kick()

    def active_count(self) -> int:
        with self.lock:
            return sum(
                job.get("status") in {"running", "paused", "cancelling"}
                for job in self.jobs.values()
            )

    def case_is_active(self, root: Path) -> bool:
        resolved = str(root.expanduser().resolve())
        with self.lock:
            return any(
                job.get("case_root") == resolved
                and job.get("status") in {"running", "paused", "cancelling"}
                for job in self.jobs.values()
            )

    def kick(self) -> None:
        threading.Thread(target=self._schedule, daemon=True).start()

    def _schedule(self) -> None:
        with self.lock:
            if not self.enabled or not self.can_start():
                return
            active = {
                job_id
                for job_id, job in self.jobs.items()
                if job.get("status") in {"running", "paused", "cancelling"}
            }
            active_roots = {str(self.jobs[job_id].get("case_root")) for job_id in active}
            capacity = self.max_parallel - len(active)
            if capacity <= 0:
                return
            selected: list[str] = []
            for job_id in self.order:
                job = self.jobs.get(job_id, {})
                root = str(job.get("case_root", ""))
                if job.get("status") != "queued" or root in active_roots:
                    continue
                selected.append(job_id)
                active_roots.add(root)
                if len(selected) >= capacity:
                    break
            for job_id in selected:
                job = self.jobs[job_id]
                job.update(
                    {
                        "status": "running",
                        "detail": "Starting full workflow",
                        "stage": "Initialize case",
                        "progress": 0.0,
                        "transport_progress": 0.0,
                        "started_at": _now_iso(),
                        "finished_at": "",
                        "error": "",
                        "attempts": int(job.get("attempts", 0)) + 1,
                    }
                )
                runtime = JobRuntime(self, job_id)
                self.runtimes[job_id] = runtime
                threading.Thread(
                    target=self._run_job,
                    args=(job_id, runtime),
                    daemon=True,
                ).start()
            if selected:
                self._persist_locked(force=True)

    def _run_job(self, job_id: str, runtime: JobRuntime) -> None:
        with self.lock:
            job = dict(self.jobs[job_id])
        root = Path(str(job["case_root"])).expanduser().resolve()
        payload = dict(job["payload"])
        payload["root"] = str(root)
        # A snapshot taken before the thread cap existed can still carry an
        # oversubscribed count; converge it here so the run and the progress
        # model both use the value TOPAS will really see.
        effective_threads, thread_note = clamp_threads(payload.get("threads", 1))
        payload["threads"] = effective_threads
        # The snapshot was taken from the Workflow form, which points at whatever
        # case the operator had open. Those paths and the TPS dose UID belong to
        # that case, not this one. Clear them so the post-transport stages
        # rediscover this job's own dose grid instead of silently analysing a
        # different patient's binary.
        foreign = [
            key
            for key in ("mc_binary", "tps_dose_uid")
            if str(payload.get(key, "")).strip()
            and not str(payload[key]).startswith(str(root))
        ]
        for key in foreign:
            payload[key] = ""
        analysis_warnings: list[str] = []
        ok = False
        error = ""
        try:
            runtime.append(f"\n=== Attempt {job['attempts']} started {_now_iso()} ===\n")
            if foreign:
                runtime.append(
                    "NOTE: cleared queue-snapshot "
                    + ", ".join(foreign)
                    + " that belonged to another case; this job uses its own outputs.\n"
                )
            if thread_note:
                runtime.append(f"WARNING: {thread_note}\n")
                self.update_job(job_id, force=True, threads=effective_threads)
            budget_note = str(payload.get("history_budget_note", "")).strip()
            if budget_note:
                runtime.append(f"WARNING: {budget_note}\n")
            runtime.check_cancelled()
            self.initialize_case(root)
            for phase_index, action in enumerate(self.phases):
                runtime.wait_if_paused()
                runtime.check_cancelled()
                if action in POST_TRANSPORT_ACTIONS:
                    # These read the dose binary the transport just wrote, so the
                    # path can only be resolved now. A failure here leaves a
                    # valid, expensive dose on disk; the job is marked
                    # "completed_with_warnings" rather than "failed" so an
                    # analysis problem is never mistaken for a lost transport.
                    payload["mc_binary"] = ""
                try:
                    title, commands = self.build_action(action, payload)
                except Exception as exc:
                    if action not in POST_TRANSPORT_ACTIONS:
                        raise
                    analysis_warnings.append(f"{action}: {exc}")
                    runtime.append(f"\nWARNING: skipped {action}: {exc}\n")
                    continue
                try:
                    self._run_commands(
                        runtime,
                        root,
                        title,
                        commands,
                        phase_index=phase_index,
                        phase_total=len(self.phases),
                        requested_threads=max(1, int(payload.get("threads", 1))),
                    )
                except QueueCancelled:
                    raise
                except Exception as exc:
                    if action not in POST_TRANSPORT_ACTIONS:
                        raise
                    analysis_warnings.append(f"{action}: {exc}")
                    runtime.append(f"\nWARNING: {action} failed: {exc}\n")
            ok = True
        except QueueCancelled as exc:
            error = str(exc)
        except Exception as exc:
            error = str(exc)
        with self.lock:
            current = self.jobs.get(job_id)
            if current is not None:
                if ok:
                    current.update(
                        {
                            "status": "completed_with_warnings" if analysis_warnings else "completed",
                            "detail": (
                                "Transport completed; "
                                f"{len(analysis_warnings)} post-transport stage(s) skipped"
                                if analysis_warnings
                                else "Preparation, TOPAS, profiles and Gamma completed"
                            ),
                            "stage": "Complete",
                            "progress": 1.0,
                            "transport_progress": 1.0,
                            "error": "; ".join(analysis_warnings),
                        }
                    )
                    if analysis_warnings:
                        runtime.append(
                            "\nTransport completed. The dose binary is valid and on disk.\n"
                            "These post-transport stages did not finish:\n"
                            + "".join(f"  - {note}\n" for note in analysis_warnings)
                            + "Rerun them from the Workflow tab against this case.\n"
                        )
                    else:
                        runtime.append("\nCompleted successfully.\n")
                elif runtime.cancel_requested:
                    current.update(
                        {
                            "status": "cancelled",
                            "detail": "Cancelled; partial files remain available for audit",
                            "error": error or "Cancelled by user",
                        }
                    )
                    runtime.append(f"\nCancelled: {error}\n")
                else:
                    current.update(
                        {
                            "status": "failed",
                            "detail": "Failed; inspect the job log and use Retry",
                            "error": error,
                        }
                    )
                    runtime.append(f"\nFailed: {error}\n")
                current["finished_at"] = _now_iso()
                current["process_group_id"] = None
                current["updated_at"] = _now_iso()
            self.runtimes.pop(job_id, None)
            self._persist_locked(force=True)
        self.kick()

    def _run_commands(
        self,
        runtime: JobRuntime,
        root: Path,
        title: str,
        commands: list[Command],
        *,
        phase_index: int,
        phase_total: int,
        requested_threads: int,
    ) -> None:
        runtime.append(f"\n=== {title} ===\n")
        for command_index, (label, argv, cwd, command_log_path) in enumerate(commands):
            runtime.wait_if_paused()
            runtime.check_cancelled()
            allocations = _allocation(root) if label == "TOPAS full-plan run" else []
            total_spots = len(allocations)
            total_histories = sum(allocations)
            history_offsets: list[int] = []
            work_offsets: list[float] = []
            running_histories = 0
            running_work = 0.0
            for allocated in allocations:
                history_offsets.append(running_histories)
                running_histories += allocated
                work_offsets.append(running_work)
                running_work += 1.0 + math.ceil(allocated / requested_threads)
            command_fraction = command_index / max(1, len(commands))
            overall = (phase_index + command_fraction) / max(1, phase_total)
            self.update_job(
                runtime.job_id,
                stage=label,
                detail=title,
                progress=overall,
                force=True,
            )
            runtime.append("\n$ " + " ".join(argv) + "\n")
            captured: list[str] = []
            process = subprocess.Popen(
                argv,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                start_new_session=True,
            )
            with runtime.lock:
                runtime.process = process
                runtime.process_group_id = process.pid
                if label == "TOPAS full-plan run":
                    runtime.transport_started_monotonic = time.monotonic()
                    runtime.transport_fraction = 0.0
                if runtime.pause_requested and hasattr(signal, "SIGSTOP"):
                    try:
                        os.killpg(process.pid, signal.SIGSTOP)
                    except ProcessLookupError:
                        pass
            self.update_job(
                runtime.job_id,
                process_group_id=process.pid,
                force=True,
            )
            assert process.stdout is not None
            for line in process.stdout:
                captured.append(line)
                runtime.append(line)
                if label == "TOPAS full-plan run":
                    match = re.search(
                        r"Begin processing for Run:\s*(\d+),\s*History:\s*(\d+)",
                        line,
                    )
                    if match and running_work > 0.0:
                        spot_index = int(match.group(1))
                        history_index = int(match.group(2))
                        if 0 <= spot_index < total_spots:
                            completed_histories = history_offsets[spot_index] + min(
                                history_index + 1, allocations[spot_index]
                            )
                            spot_fraction = min(
                                1.0,
                                (history_index + 1) / max(1, allocations[spot_index]),
                            )
                            spot_work = 1.0 + math.ceil(
                                allocations[spot_index] / requested_threads
                            )
                            completed_work = work_offsets[spot_index] + spot_fraction * spot_work
                            transport_fraction = min(1.0, completed_work / running_work)
                            runtime.transport_fraction = transport_fraction
                            overall = (phase_index + transport_fraction) / max(1, phase_total)
                            self.update_job(
                                runtime.job_id,
                                stage="TOPAS transport",
                                detail=(
                                    f"Spot {spot_index + 1:,}/{total_spots:,}; "
                                    f"history {completed_histories:,}/{total_histories:,}"
                                ),
                                progress=overall,
                                transport_progress=transport_fraction,
                                completed_spots=min(total_spots, spot_index + 1),
                                total_spots=total_spots,
                                completed_histories=completed_histories,
                                total_histories=total_histories,
                            )
            process.stdout.close()
            code = process.wait()
            with runtime.lock:
                runtime.process = None
                runtime.process_group_id = None
            self.update_job(runtime.job_id, process_group_id=None, force=True)
            if command_log_path:
                command_log_path.parent.mkdir(parents=True, exist_ok=True)
                command_log_path.write_text("".join(captured), encoding="utf-8")
            if runtime.cancel_requested:
                raise QueueCancelled("Cancelled by user")
            if code != 0:
                raise RuntimeError(
                    f"{label} exited with status {code}{_failure_reason(captured)}"
                )
            command_fraction = (command_index + 1) / max(1, len(commands))
            self.update_job(
                runtime.job_id,
                progress=(phase_index + command_fraction) / max(1, phase_total),
            )

    def control(self, job_id: str, action: str) -> str:
        job_id = _safe_job_id(job_id)
        action = str(action).strip().lower()
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise RuntimeError("Queue job not found")
            runtime = self.runtimes.get(job_id)
            status = str(job.get("status", ""))
            if action == "pause":
                if runtime is None or status not in {"running"}:
                    raise RuntimeError("Only a running job can be paused")
                return runtime.pause()
            if action == "resume":
                if runtime is None or status != "paused":
                    raise RuntimeError("Only a paused job can be resumed")
                return runtime.resume()
            if action == "cancel":
                if status == "queued":
                    job.update(
                        status="cancelled",
                        detail="Removed from the waiting queue; case data were not changed",
                        finished_at=_now_iso(),
                    )
                    self._persist_locked(force=True)
                    return "Queued job cancelled"
                if runtime is None or status not in {"running", "paused", "cancelling"}:
                    raise RuntimeError("Only a queued or active job can be cancelled")
                return runtime.cancel()
            if action == "retry":
                if status not in {"failed", "cancelled", "interrupted", "completed_with_warnings"}:
                    raise RuntimeError(
                        "Only a failed, cancelled, interrupted or "
                        "completed-with-warnings job can be retried"
                    )
                job.update(
                    status="queued",
                    detail="Waiting to retry",
                    stage="Waiting",
                    progress=0.0,
                    transport_progress=0.0,
                    error="",
                    process_group_id=None,
                    finished_at="",
                )
                self._persist_locked(force=True)
                message = "Job queued for retry"
            elif action == "remove":
                if status in {"running", "paused", "cancelling"}:
                    raise RuntimeError("Stop an active job before removing it from the queue")
                self.jobs.pop(job_id, None)
                self.order = [value for value in self.order if value != job_id]
                self._persist_locked(force=True)
                return "Queue record removed; case data and results were preserved"
            else:
                raise RuntimeError("Unknown queue action")
        self.kick()
        return message

    def read_log(self, job_id: str, after: int) -> dict[str, Any]:
        job_id = _safe_job_id(job_id)
        with self.lock:
            job = self.jobs.get(job_id)
            if not job:
                raise RuntimeError("Queue job not found")
            path = Path(str(job["log_path"]))
        text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
        after = max(0, min(int(after), len(text)))
        return {"job_id": job_id, "cursor": len(text), "text": text[after:]}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            records = [dict(self.jobs[job_id]) for job_id in self.order if job_id in self.jobs]
            enabled = self.enabled
            max_parallel = self.max_parallel
            runtime_refs = dict(self.runtimes)
        now_epoch = time.time()
        for record in records:
            runtime = runtime_refs.get(str(record["id"]))
            started = str(record.get("started_at", ""))
            finished = str(record.get("finished_at", ""))
            elapsed = 0.0
            if started:
                try:
                    end_epoch = (
                        datetime.fromisoformat(finished).timestamp()
                        if finished
                        else now_epoch
                    )
                    elapsed = max(
                        0.0,
                        end_epoch - datetime.fromisoformat(started).timestamp(),
                    )
                except ValueError:
                    elapsed = 0.0
            record["elapsed_seconds"] = elapsed
            estimate_seconds = float(record.get("estimate", {}).get("seconds", 0.0) or 0.0)
            eta: Optional[float] = None
            eta_basis = ""
            if runtime is not None and runtime.transport_started_monotonic is not None:
                paused_now = (
                    max(0.0, time.monotonic() - runtime.pause_started_monotonic)
                    if runtime.pause_started_monotonic is not None
                    else 0.0
                )
                active = max(
                    0.0,
                    time.monotonic()
                    - runtime.transport_started_monotonic
                    - runtime.paused_accumulated_seconds
                    - paused_now,
                )
                fraction = max(0.0, min(1.0, runtime.transport_fraction))
                if active >= 20.0 and fraction >= 0.002:
                    eta = active * (1.0 - fraction) / fraction
                    eta_basis = "observed sequential-spot progress"
                elif estimate_seconds > 0.0:
                    eta = max(0.0, estimate_seconds - active)
                    eta_basis = "case runtime estimate (warming up)"
            elif record.get("status") == "queued" and estimate_seconds > 0.0:
                eta = estimate_seconds
                eta_basis = "planned runtime after start"
            record["eta_seconds"] = eta
            record["eta_basis"] = eta_basis
            record["estimated_finish_epoch"] = now_epoch + eta if eta is not None else None
            process_group_id = (
                runtime.process_group_id if runtime is not None else None
            )
            if process_group_id is not None:
                monitor = collect_process_status(process_group_id)
                record["compute"] = {
                    key: monitor.get(key)
                    for key in (
                        "task_cpu_percent",
                        "task_cpu_cores",
                        "task_rss_bytes",
                        "task_os_threads",
                        "processes",
                    )
                }
            else:
                record["compute"] = {}
            record.pop("payload", None)
        return {
            "enabled": enabled,
            "max_parallel": max_parallel,
            "active": sum(
                record.get("status") in {"running", "paused", "cancelling"}
                for record in records
            ),
            "queued": sum(record.get("status") == "queued" for record in records),
            "jobs": records,
            "storage": str(self.storage_path),
        }
