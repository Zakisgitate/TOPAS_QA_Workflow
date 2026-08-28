#!/usr/bin/env python3
"""Local browser GUI for the reusable TPS-TOPAS QA workflow."""

from __future__ import annotations

import argparse
import base64
import csv
from datetime import datetime
import hashlib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urlparse
import uuid
import webbrowser

import pydicom

from gui.batch_queue import BatchQueueManager
from gui.case_results import (
    analysis_plan_dir,
    analysis_run_dir,
    archive_production_output,
    case_identity,
    discover_cached_runs,
    snapshot_run_settings,
    trash_cached_run,
    update_run_manifest,
)
from gui.line_dose import (
    frame_binary,
    get_line_dose_dataset,
    list_tps_doses,
    meta_header,
    profile_csv,
    resolve_tps_dose,
)
from gui.machine_models import (
    extract_package,
    import_inspected_package,
    inspect_extracted_package,
    list_machine_models,
    profile_registry_status,
    set_model_active,
)
from gui.mc_rtdose import export_mc_rtdose
from gui.runtime_monitor import clamp_threads, collect_process_status, logical_cpu_count
from gui.ssh_server import (
    check_server_environment,
    inspect_host_keys,
    public_server_status,
    save_server_config,
    test_connection as test_ssh_connection,
    trust_host_key,
)
from gui.tps_topas_gui import (
    collect_status,
    discover_mc_binary,
    mc_binary_matches_current_grid,
    estimate_topas_runtime,
    expected_mc_binary,
    prepared_run_matches,
    run_configuration_options,
)


APP_ROOT = Path(__file__).resolve().parents[1]


class WorkflowState:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.lock = threading.RLock()
        self.log: list[str] = []
        self.running = False
        self.paused = False
        self.process: Optional[subprocess.Popen] = None
        self.process_group_id: Optional[int] = None
        self.progress_before_pause: Optional[dict] = None
        self.task = "Idle"
        self.last_ok: Optional[bool] = None
        self.last_error = ""
        self.progress = {"phase": "Idle", "label": "Idle", "fraction": 0.0, "current": 0, "total": 0, "mode": "idle"}
        self.imports: dict[str, dict] = {}
        self.machine_imports: dict[str, dict] = {}
        self.task_started_monotonic: Optional[float] = None
        self.command_started_monotonic: Optional[float] = None
        self.process_started_monotonic: Optional[float] = None
        self.pause_started_monotonic: Optional[float] = None
        self.paused_accumulated_seconds = 0.0
        self.current_command = "Idle"
        self.runtime_request: dict[str, Any] = {}
        self.runtime_estimate: dict[str, Any] = {}
        # Browser form snapshot of the running task. The page is thrown away on
        # every refresh; the task is not. Without this the reloaded form falls
        # back to its HTML defaults while a transport with different settings
        # is still in flight.
        self.active_request: dict[str, str] = {}
        self.last_task_elapsed_seconds = 0.0
        self.last_process_active_seconds = 0.0

    def append(self, text: str) -> None:
        with self.lock:
            self.log.append(text)
            if len(self.log) > 12_000:
                self.log = self.log[-10_000:]

    def snapshot(self, after: int) -> dict:
        now = time.monotonic()
        with self.lock:
            after = max(0, min(after, len(self.log)))
            process_group_id = self.process_group_id
            paused_now = (
                now - self.pause_started_monotonic
                if self.paused and self.pause_started_monotonic is not None
                else 0.0
            )
            task_elapsed = (
                max(0.0, now - self.task_started_monotonic)
                if self.task_started_monotonic is not None
                else self.last_task_elapsed_seconds
            )
            process_elapsed = (
                max(
                    0.0,
                    now
                    - self.process_started_monotonic
                    - self.paused_accumulated_seconds
                    - paused_now,
                )
                if self.process_started_monotonic is not None
                else self.last_process_active_seconds
            )
            progress = dict(self.progress)
            compute = {
                "current_command": self.current_command,
                "task_elapsed_seconds": task_elapsed,
                "process_active_elapsed_seconds": process_elapsed,
                "paused_seconds": self.paused_accumulated_seconds + paused_now,
                "runtime_request": dict(self.runtime_request),
                "planned_estimate": dict(self.runtime_estimate),
            }
            result = {
                "cursor": len(self.log),
                "lines": self.log[after:],
                "running": self.running,
                "paused": self.paused,
                "task": self.task,
                "last_ok": self.last_ok,
                "last_error": self.last_error,
                "progress": progress,
                "form_state": dict(self.active_request),
            }
        fraction = max(0.0, min(1.0, float(progress.get("fraction", 0.0) or 0.0)))
        live_eta: Optional[float] = None
        eta_basis = ""
        eta_confidence = ""
        if progress.get("mode") in {"spot_runs", "paused"} and process_elapsed >= 20.0:
            if fraction >= 0.002:
                live_eta = process_elapsed * (1.0 - fraction) / fraction
                eta_basis = "observed sequential-spot progress"
                eta_confidence = "high" if fraction >= 0.20 else "medium" if fraction >= 0.05 else "low"
            elif compute["planned_estimate"].get("seconds"):
                live_eta = max(
                    0.0, float(compute["planned_estimate"]["seconds"]) - process_elapsed
                )
                eta_basis = "pre-run case benchmark (warming up)"
                eta_confidence = str(compute["planned_estimate"].get("confidence", "low"))
        compute.update(
            {
                "eta_seconds": live_eta,
                "eta_basis": eta_basis,
                "eta_confidence": eta_confidence,
                "estimated_finish_epoch": (
                    time.time() + live_eta if live_eta is not None and not result["paused"] else None
                ),
            }
        )
        compute.update(collect_process_status(process_group_id))
        result["compute"] = compute
        return result


STATE: WorkflowState = WorkflowState(APP_ROOT)
BATCH: Optional[BatchQueueManager] = None

DICOM_MODALITIES = ("CT", "RTPLAN", "RTDOSE", "RTSTRUCT")
MAX_DICOM_BYTES = 2 * 1024**3
MAX_MACHINE_PACKAGE_BYTES = 512 * 1024**2


# Directories the project owns inside its own tree. A case root placed on one of
# these would interleave case data with the application's own files, and putting
# a case root on sys.path is how an empty `gui/` in a case shadowed the real
# `gui` package (see OPTIMIZATION_REPORT item 3).
APP_OWNED_NAMES = frozenset(
    {"gui", "scripts", "topas", "analysis", "config", "tests", "archive", ".venv", ".claude"}
)
# Subdirectories a case owns. Selecting one of these as a new case root nests a
# case inside another case's data, which is how a run tagged for one plan ended
# up filed under a different patient.
CASE_DATA_NAMES = frozenset(
    {"dicom", "analysis", "topas", "topas_output", "plan_parsed", "machine_model", "scripts"}
)


def safe_root(value: str) -> Path:
    root = Path(value).expanduser().resolve()
    if root == Path("/") or len(root.parts) < 3:
        raise RuntimeError(f"Unsafe case folder: {root}")
    return root


def validate_case_root(value: str) -> Path:
    """safe_root plus the structural guards for *choosing* a case folder.

    Deliberately narrower than "must not live under the project's dicom/": a
    well-formed case folder that happens to sit there works correctly, and
    several already do. What is rejected is a folder that cannot be a case root
    without corrupting something: one holding loose DICOM files, one nested in
    another case's data, and one of the application's own directories.
    """

    root = safe_root(value)
    if root == APP_ROOT:
        return root

    if any(root.glob("*.dcm")):
        raise RuntimeError(
            f"{root} contains DICOM files directly. Select the case folder that will hold "
            "the plan, not the folder holding the images — a case root keeps its DICOM in "
            "dicom/CT, dicom/RTPLAN, dicom/RTDOSE and dicom/RTSTRUCT."
        )

    if root.name in APP_OWNED_NAMES and root.parent == APP_ROOT:
        raise RuntimeError(
            f"{root.name}/ belongs to the application itself. Choose a separate case folder "
            "so case data never mixes with the program files."
        )

    # A case root must not sit inside another case's data folders. Only the
    # enclosing case matters, so find the nearest ancestor holding a
    # case_config.json and check where `root` sits relative to it. APP_ROOT is
    # the template every case is created from, so being under it is normal.
    for parent in root.parents:
        if parent == APP_ROOT or len(parent.parts) < 2:
            break
        if not (parent / "case_config.json").is_file():
            continue
        relative = root.relative_to(parent).parts
        if relative and relative[0] in CASE_DATA_NAMES:
            raise RuntimeError(
                f"{root} is inside the {relative[0]}/ data folder of the case at {parent}. "
                "Nesting a case there mixes the two cases' results together. "
                "Choose a folder outside it."
            )
        raise RuntimeError(
            f"{root} is inside the existing case at {parent}. Create the new case folder "
            f"outside it, or select {parent} itself to continue that case."
        )
    return root


def dicom_counts(root: Path) -> dict[str, int]:
    return {
        modality: len([path for path in (root / "dicom" / modality).glob("*.dcm") if path.is_file()])
        for modality in DICOM_MODALITIES
    }


def validate_imported_dicom(path: Path, expected_modality: str) -> dict[str, str]:
    try:
        dataset = pydicom.dcmread(path, stop_before_pixels=True)
    except Exception as exc:
        raise RuntimeError(f"Not a readable DICOM file: {path.name}") from exc
    actual = str(getattr(dataset, "Modality", "")).upper()
    if actual != expected_modality:
        raise RuntimeError(f"Expected {expected_modality}, but {path.name} is {actual or 'unknown'}")
    if expected_modality == "RTPLAN" and not hasattr(dataset, "IonBeamSequence"):
        raise RuntimeError(f"{path.name} is not an RT Ion Plan")
    frame_uid = str(getattr(dataset, "FrameOfReferenceUID", ""))
    if not frame_uid:
        referenced_frames = {
            str(getattr(item, "FrameOfReferenceUID", ""))
            for item in getattr(dataset, "ReferencedFrameOfReferenceSequence", [])
            if getattr(item, "FrameOfReferenceUID", "")
        }
        if len(referenced_frames) == 1:
            frame_uid = next(iter(referenced_frames))
    return {
        "patient_id": str(getattr(dataset, "PatientID", "")),
        "patient_name": str(getattr(dataset, "PatientName", "")),
        "patient_birth_date": str(getattr(dataset, "PatientBirthDate", "")),
        "study_uid": str(getattr(dataset, "StudyInstanceUID", "")),
        "frame_uid": frame_uid,
        "sop_uid": str(getattr(dataset, "SOPInstanceUID", "")),
    }


def validate_import_batch(batch: dict) -> None:
    expected = int(batch["count"])
    received = batch["received"]
    if set(received) != set(range(expected)):
        raise RuntimeError(f"Import incomplete: received {len(received)} of {expected} files")
    records = [received[index] for index in range(expected)]
    studies = {record["study_uid"] for record in records if record["study_uid"]}
    frames = {record["frame_uid"] for record in records if record["frame_uid"]}
    patient_ids = {record["patient_id"] for record in records if record["patient_id"]}
    patient_names = {record["patient_name"] for record in records if record["patient_name"]}
    if len(studies) > 1 or len(frames) > 1 or len(patient_ids) > 1 or len(patient_names) > 1:
        raise RuntimeError(
            f"Selected {batch['modality']} files contain more than one patient, study or Frame of Reference"
        )
    # A confirmed replacement may be the first modality of a new patient.
    # During that four-button transition, the active DICOM tree is expected to
    # be temporarily mixed and the geometry gate remains WAITING until all
    # modalities have been replaced.
    if batch.get("replace"):
        return
    root = Path(batch["root"])
    for other_modality in DICOM_MODALITIES:
        if other_modality == batch["modality"]:
            continue
        for path in (root / "dicom" / other_modality).glob("*.dcm"):
            record = validate_imported_dicom(path, other_modality)
            if patient_ids and record["patient_id"] and record["patient_id"] not in patient_ids:
                raise RuntimeError(
                    f"Selected {batch['modality']} PatientID does not match existing {other_modality}"
                )
            if patient_names and record["patient_name"] and record["patient_name"] not in patient_names:
                raise RuntimeError(
                    f"Selected {batch['modality']} PatientName does not match existing {other_modality}"
                )
            if studies and record["study_uid"] and record["study_uid"] not in studies:
                raise RuntimeError(
                    f"Selected {batch['modality']} does not match the StudyInstanceUID of existing {other_modality}"
                )
            if frames and record["frame_uid"] and record["frame_uid"] not in frames:
                raise RuntimeError(
                    f"Selected {batch['modality']} does not match the FrameOfReferenceUID of existing {other_modality}"
                )


def archive_existing_dicom(root: Path, modality: str) -> Optional[Path]:
    destination = root / "dicom" / modality
    existing = list(destination.glob("*.dcm")) if destination.is_dir() else []
    if not existing:
        return None
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    archive = root / "dicom_archive" / stamp / modality
    archive.mkdir(parents=True, exist_ok=False)
    for path in existing:
        shutil.move(str(path), str(archive / path.name))
    return archive


def discard_import(token: str) -> None:
    with STATE.lock:
        batch = STATE.imports.pop(token, None)
    if batch:
        shutil.rmtree(Path(batch["stage"]), ignore_errors=True)


def discard_machine_import(token: str) -> None:
    with STATE.lock:
        batch = STATE.machine_imports.pop(token, None)
    if batch:
        shutil.rmtree(Path(batch["stage"]), ignore_errors=True)


def commit_import_batch(
    root: Path,
    batch: dict,
) -> tuple[int, Optional[Path], bool, Optional[Path]]:
    validate_import_batch(batch)
    modality = str(batch["modality"])
    destination = root / "dicom" / modality
    destination.mkdir(parents=True, exist_ok=True)
    existing = list(destination.glob("*.dcm"))
    if existing and not batch["replace"]:
        raise RuntimeError(f"{modality} already contains files; confirm replacement before importing")
    records = [batch["received"][index] for index in range(int(batch["count"]))]
    incoming_studies = {record["study_uid"] for record in records if record["study_uid"]}
    incoming_sops = {record["sop_uid"] for record in records if record["sop_uid"]}
    previous_identity = case_identity(root)
    case_changed = bool(
        (incoming_studies and previous_identity.study_uid not in incoming_studies)
        or (
            modality == "RTPLAN"
            and incoming_sops
            and previous_identity.plan_uid not in incoming_sops
        )
    )
    cache_path: Optional[Path] = None
    if case_changed:
        settings = batch.get("settings") if isinstance(batch.get("settings"), dict) else {}
        tag = str(settings.get("output_tag", "full_plan_100000"))
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tag):
            tag = "full_plan_100000"
        cache_path = snapshot_run_settings(root, tag, settings)
        archived_run = archive_production_output(
            root,
            tag,
            reason=f"Automatic archive before importing a different {modality} patient/plan",
        )
        if archived_run:
            cache_path = Path(archived_run["archived_directory"])
    archive = archive_existing_dicom(root, modality) if existing else None
    staged = Path(batch["stage"])
    manifest_path: Optional[Path] = None
    try:
        for index in range(int(batch["count"])):
            shutil.move(
                str(staged / f"{index:05d}.dcm"),
                str(destination / f"{modality}_{index + 1:05d}.dcm"),
            )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        manifest_path = root / "dicom" / "import_history" / f"{stamp}_{modality}.csv"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        fields = (
            "Imported_at",
            "Modality",
            "Active_filename",
            "Original_filename",
            "Size_bytes",
            "SHA256",
            "PatientID",
            "PatientName",
            "SOPInstanceUID",
            "StudyInstanceUID",
            "FrameOfReferenceUID",
            "Previous_archive",
        )
        with manifest_path.open("x", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for index in range(int(batch["count"])):
                record = batch["received"][index]
                writer.writerow(
                    {
                        "Imported_at": datetime.now().isoformat(timespec="seconds"),
                        "Modality": modality,
                        "Active_filename": f"{modality}_{index + 1:05d}.dcm",
                        "Original_filename": record["original_name"],
                        "Size_bytes": record["size_bytes"],
                        "SHA256": record["sha256"],
                        "PatientID": record["patient_id"],
                        "PatientName": record["patient_name"],
                        "SOPInstanceUID": record["sop_uid"],
                        "StudyInstanceUID": record["study_uid"],
                        "FrameOfReferenceUID": record["frame_uid"],
                        "Previous_archive": str(archive) if archive else "",
                    }
                )
        shutil.rmtree(staged)
    except Exception:
        if manifest_path:
            manifest_path.unlink(missing_ok=True)
        for path in destination.glob("*.dcm"):
            path.unlink()
        if archive:
            for path in archive.iterdir():
                shutil.move(str(path), str(destination / path.name))
        raise
    return int(batch["count"]), archive, case_changed, cache_path


def choose_case_folder() -> Optional[Path]:
    script_text = (
        'set selectedFolder to choose folder with prompt "Select or create a TPS-TOPAS case folder"\n'
        "return POSIX path of selectedFolder"
    )
    result = subprocess.run(["osascript", "-e", script_text], capture_output=True, text=True)
    if result.returncode != 0:
        if "User canceled" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or "Could not open the case-folder selector")
    return validate_case_root(result.stdout.strip())


def choose_case_folders() -> list[Path]:
    script_text = (
        'set selectedFolders to choose folder with prompt "Select TPS-TOPAS case folders" '
        "with multiple selections allowed\n"
        'set resultText to ""\n'
        "repeat with selectedFolder in selectedFolders\n"
        "set resultText to resultText & POSIX path of selectedFolder & linefeed\n"
        "end repeat\n"
        "return resultText"
    )
    result = subprocess.run(["osascript", "-e", script_text], capture_output=True, text=True)
    if result.returncode != 0:
        if "User canceled" in result.stderr:
            return []
        raise RuntimeError(result.stderr.strip() or "Could not open the case-folder selector")
    return [
        validate_case_root(line.strip())
        for line in result.stdout.splitlines()
        if line.strip()
    ]


def choose_ssh_identity_file() -> Optional[Path]:
    script_text = (
        'set selectedFile to choose file with prompt "Choose an existing SSH private key"\n'
        "return POSIX path of selectedFile"
    )
    result = subprocess.run(["osascript", "-e", script_text], capture_output=True, text=True)
    if result.returncode != 0:
        if "User canceled" in result.stderr:
            return None
        raise RuntimeError(result.stderr.strip() or "Could not open the SSH key selector")
    selected = Path(result.stdout.strip()).expanduser().resolve()
    if not selected.is_file():
        raise RuntimeError("Selected SSH identity is not a regular file")
    if selected.suffix.lower() == ".pub":
        raise RuntimeError("Choose the private key, not the .pub file")
    return selected


def initialize_case(case: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(APP_ROOT / "scripts" / "10_initialize_case.py"), "--case-root", str(case)],
        cwd=str(APP_ROOT),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stdout + result.stderr)


def batch_manager() -> BatchQueueManager:
    if BATCH is None:
        raise RuntimeError("Batch queue is not initialized")
    return BATCH


def active_case_work(root: Path) -> str:
    """Return a conservative reason why result-cache mutation is unsafe."""
    root = root.expanduser().resolve()
    with STATE.lock:
        if STATE.running and STATE.root == root:
            return f"interactive task is running: {STATE.task}"
    if BATCH is not None and BATCH.case_is_active(root):
        return "this GUI has an active batch task for the case"
    # Multiple GUI windows may exist. Read the shared queue file as well so an
    # older idle server cannot delete data owned by the active server.
    storage = APP_ROOT / "analysis" / "_batch_queue" / "queue.json"
    try:
        payload = json.loads(storage.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    for job in payload.get("jobs", []) if isinstance(payload, dict) else []:
        if not isinstance(job, dict):
            continue
        try:
            job_root = Path(str(job.get("case_root", ""))).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        status = str(job.get("status", ""))
        if job_root == root and status in {"queued", "running", "paused", "cancelling"}:
            return f"batch job {job.get('id', '')} is {status}"
    return ""


def active_machine_model_work() -> str:
    """Protect shared machine assets from changing under a running transport."""
    with STATE.lock:
        if STATE.running:
            return f"interactive task is running: {STATE.task}"
    if BATCH is not None and BATCH.active_count() > 0:
        return "one or more batch calculations are running or paused"
    storage = APP_ROOT / "analysis" / "_batch_queue" / "queue.json"
    try:
        payload = json.loads(storage.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    for job in payload.get("jobs", []) if isinstance(payload, dict) else []:
        if isinstance(job, dict) and str(job.get("status", "")) in {
            "running", "paused", "cancelling"
        }:
            return f"shared batch job {job.get('id', '')} is {job.get('status')}"
    return ""


def enqueue_case(root: Path, payload: dict[str, Any], *, use_all_layers: bool) -> dict[str, Any]:
    root = safe_root(str(root))
    initialize_case(root)
    counts = dicom_counts(root)
    missing = [name for name in DICOM_MODALITIES if counts.get(name, 0) < 1]
    if missing:
        raise RuntimeError(
            f"{root.name}: import the required DICOM first ({', '.join(missing)} missing)"
        )
    prepared_payload = dict(payload)
    prepared_payload["root"] = str(root)
    if use_all_layers and str(prepared_payload.get("beam_input_mode", "rtplan")) == "rtplan":
        # Energy-layer indices are case-specific. A multi-folder enqueue starts
        # each independent RTPLAN with all of its own layers.
        prepared_payload["energy_layer_indices"] = "all"
    histories = integer(prepared_payload, "histories")
    threads, requested_threads, thread_note = resolve_threads(prepared_payload)
    # The job snapshot records the value that will actually be used, so a queued
    # case cannot carry an oversubscribed thread count into a later run.
    prepared_payload["threads"] = threads
    prepared_payload["requested_threads"] = requested_threads
    if thread_note:
        prepared_payload["thread_limit_note"] = thread_note
    integer(prepared_payload, "seed", 0)
    output_tag = str(prepared_payload.get("output_tag", f"full_plan_{histories}")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", output_tag):
        raise RuntimeError("output_tag may contain only letters, digits, underscore and hyphen")
    prepared_payload["output_tag"] = output_tag
    prepared_beam = beam_settings(prepared_payload)
    validate_selected_beam_profile(root, prepared_beam)
    budget_note = history_budget_warning(
        root, histories, prepared_beam, selected_energy_layers(prepared_payload, root)
    )
    if budget_note:
        prepared_payload["history_budget_note"] = budget_note
    if prepared_beam.get("beam_model_profile"):
        prepared_payload["beam_model_profile"] = prepared_beam["beam_model_profile"]
    estimate: dict[str, Any] = {}
    try:
        estimate = dict(runtime_context_from_payload(root, prepared_payload).get("estimate", {}))
    except Exception:
        # Runtime estimation is advisory; preparation will perform the strict
        # plan-specific validation before any particle transport begins.
        estimate = {}
    try:
        identity = case_identity(root)
        label = " · ".join(
            value
            for value in (
                identity.patient_id or identity.patient_key,
                identity.plan_label or identity.plan_key,
            )
            if value
        )
    except Exception:
        label = root.name
    return batch_manager().enqueue(root, prepared_payload, label=label, estimate=estimate)


def integer(payload: dict, key: str, minimum: int = 1) -> int:
    try:
        value = int(payload[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{key} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{key} must be at least {minimum}")
    return value


def resolve_threads(payload: dict) -> tuple[int, int, str]:
    """Return (effective, requested, note) for the requested worker count.

    Every path that reaches TOPAS goes through here, so a stale queue snapshot
    or a hand-edited request can never oversubscribe the machine.
    """

    requested = integer(payload, "threads")
    effective, note = clamp_threads(requested)
    return effective, requested, note


# Every field the browser form owns. Snapshotted when a task starts so a
# refreshed page can reattach to it instead of showing HTML defaults.
GUI_FORM_FIELDS = (
    "root", "histories", "threads", "seed", "profile_depth",
    "gamma_dta_mm", "gamma_dd_percent", "output_tag",
    "topas_executable", "mc_binary", "tps_dose_uid",
    "beam_input_mode", "beam_model_mode", "beam_model_profile",
    "beam_override_enabled",
    "beam_energy_scale_percent", "beam_energy_offset_mevu",
    "beam_spot_scale_percent", "beam_energy_spread_percent",
    "manual_energy_mevu", "manual_energy_spread_percent",
    "manual_spot_x_mm", "manual_spot_y_mm",
    "manual_spot_fwhm_x_mm", "manual_spot_fwhm_y_mm",
    "energy_layer_indices",
)


def gui_form_state(payload: dict) -> dict[str, str]:
    """Snapshot the submitted form as plain strings the page can restore."""

    state: dict[str, str] = {}
    for key in GUI_FORM_FIELDS:
        if key not in payload:
            continue
        value = payload[key]
        if isinstance(value, bool):
            state[key] = "true" if value else "false"
        else:
            state[key] = str(value)
    if "threads" in state:
        # Record what TOPAS actually gets, not what the operator typed.
        try:
            state["threads"] = str(resolve_threads(payload)[0])
        except RuntimeError:
            pass
    return state


def beam_settings(payload: dict) -> dict[str, object]:
    mode = str(payload.get("beam_input_mode", "rtplan")).strip().lower()
    if mode not in {"rtplan", "manual"}:
        raise RuntimeError("Beam input mode must be RTPLAN or manual")
    model_mode = str(payload.get("beam_model_mode", "baseline")).strip().lower()
    if model_mode not in {"baseline", "commissioned"}:
        raise RuntimeError("Beam model mode must be baseline or commissioned")
    enabled = str(payload.get("beam_override_enabled", "false")).lower() in {"1", "true", "yes", "on"}
    profile_value = str(payload.get("beam_model_profile", "")).strip()
    defaults = {
        "beam_input_mode": "rtplan",
        "beam_model_mode": model_mode,
        "beam_model_profile": profile_value if model_mode == "commissioned" else "",
        "energy_scale": 1.0,
        "energy_offset_mevu": 0.0,
        "spot_size_scale": 1.0,
        "energy_spread_percent": 0.0,
        "manual_energy_mevu": None,
        "manual_spot_x_mm": None,
        "manual_spot_y_mm": None,
        "manual_spot_fwhm_x_mm": None,
        "manual_spot_fwhm_y_mm": None,
    }
    if mode == "manual":
        try:
            result = {
                **defaults,
                "beam_input_mode": "manual",
                "manual_energy_mevu": float(payload.get("manual_energy_mevu", "")),
                "manual_spot_x_mm": float(payload.get("manual_spot_x_mm", "")),
                "manual_spot_y_mm": float(payload.get("manual_spot_y_mm", "")),
                "manual_spot_fwhm_x_mm": float(payload.get("manual_spot_fwhm_x_mm", "")),
                "manual_spot_fwhm_y_mm": float(payload.get("manual_spot_fwhm_y_mm", "")),
                "energy_spread_percent": float(payload.get("manual_energy_spread_percent", 0.0)),
            }
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Manual Energy and spot values must be numeric") from exc
        if not all(
            math.isfinite(float(result[key]))
            for key in (
                "manual_energy_mevu", "manual_spot_x_mm", "manual_spot_y_mm",
                "manual_spot_fwhm_x_mm", "manual_spot_fwhm_y_mm", "energy_spread_percent",
            )
        ):
            raise RuntimeError("Manual Energy and spot values must be finite")
        if not 1.0 <= result["manual_energy_mevu"] <= 500.0:
            raise RuntimeError("Manual Energy must be within 1–500 MeV/u")
        for key in ("manual_spot_x_mm", "manual_spot_y_mm"):
            if not -500.0 <= result[key] <= 500.0:
                raise RuntimeError("Manual spot X/Y must be within −500 to +500 mm")
        for key in ("manual_spot_fwhm_x_mm", "manual_spot_fwhm_y_mm"):
            if not 0.01 <= result[key] <= 200.0:
                raise RuntimeError("Manual spot FWHM X/Y must be within 0.01–200 mm")
        if not 0.0 <= result["energy_spread_percent"] <= 20.0:
            raise RuntimeError("Manual Energy spread must be within 0–20%")
        if model_mode == "commissioned" and not math.isclose(result["energy_spread_percent"], 0.0):
            raise RuntimeError("Commissioned beam mode uses its discrete spectrum and requires manual Energy spread = 0")
        return result
    if model_mode == "commissioned" or not enabled:
        return defaults
    try:
        result = {
            **defaults,
            "energy_scale": float(payload.get("beam_energy_scale_percent", 100.0)) / 100.0,
            "energy_offset_mevu": float(payload.get("beam_energy_offset_mevu", 0.0)),
            "spot_size_scale": float(payload.get("beam_spot_scale_percent", 100.0)) / 100.0,
            "energy_spread_percent": float(payload.get("beam_energy_spread_percent", 0.0)),
        }
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Beam override values must be numeric") from exc
    if not all(
        math.isfinite(float(result[key]))
        for key in ("energy_scale", "energy_offset_mevu", "spot_size_scale", "energy_spread_percent")
    ):
        raise RuntimeError("Beam override values must be finite")
    if not 0.8 <= result["energy_scale"] <= 1.2:
        raise RuntimeError("Beam energy scale must be within 80–120%")
    if not -20.0 <= result["energy_offset_mevu"] <= 20.0:
        raise RuntimeError("Beam energy offset must be within −20 to +20 MeV/u")
    if not 0.25 <= result["spot_size_scale"] <= 4.0:
        raise RuntimeError("Spot-size scale must be within 25–400%")
    if not 0.0 <= result["energy_spread_percent"] <= 20.0:
        raise RuntimeError("Energy spread must be within 0–20%")
    return result


def beam_plan_arguments(settings: dict[str, object]) -> list[str]:
    arguments = [
        "--beam-input-mode", str(settings["beam_input_mode"]),
        "--beam-model-mode", str(settings["beam_model_mode"]),
        "--energy-scale", str(settings["energy_scale"]),
        "--energy-offset-mevu", str(settings["energy_offset_mevu"]),
        "--spot-size-scale", str(settings["spot_size_scale"]),
        "--energy-spread-percent", str(settings["energy_spread_percent"]),
    ]
    if settings["beam_model_mode"] == "commissioned" and settings.get("beam_model_profile"):
        arguments.extend(("--beam-model-profile", str(settings["beam_model_profile"])))
    if settings["beam_input_mode"] == "manual":
        arguments.extend(
            [
                "--manual-energy-mevu", str(settings["manual_energy_mevu"]),
                "--manual-spot-x-mm", str(settings["manual_spot_x_mm"]),
                "--manual-spot-y-mm", str(settings["manual_spot_y_mm"]),
                "--manual-spot-fwhm-x-mm", str(settings["manual_spot_fwhm_x_mm"]),
                "--manual-spot-fwhm-y-mm", str(settings["manual_spot_fwhm_y_mm"]),
            ]
        )
    return arguments


def beam_geometry_arguments(settings: dict[str, object]) -> list[str]:
    arguments = ["--beam-model-mode", str(settings["beam_model_mode"])]
    if settings["beam_model_mode"] == "commissioned" and settings.get("beam_model_profile"):
        arguments.extend(("--beam-model-profile", str(settings["beam_model_profile"])))
    return arguments


def validate_selected_beam_profile(root: Path, settings: dict[str, object]) -> None:
    value = str(settings.get("beam_model_profile", "")).strip()
    if str(settings.get("beam_model_mode")) != "commissioned" or not value:
        return
    profile = Path(value).expanduser().resolve()
    allowed = root.resolve() / "machine_model" / "beam_commissioning"
    try:
        profile.relative_to(allowed)
    except ValueError:
        # Queue cases are initialized from the template and receive the same
        # immutable model tree at a different case-root path.
        try:
            model_index = profile.parts.index("machine_model")
            mapped = root.resolve().joinpath(*profile.parts[model_index:])
        except ValueError as exc:
            raise RuntimeError("Selected beam profile is outside this case's machine_model registry") from exc
        if not mapped.is_file():
            raise RuntimeError("Selected beam-model version is not installed in this queued case")
        profile = mapped.resolve()
    if profile.name != "profile.json" or not profile.is_file():
        raise RuntimeError(f"Selected commissioned profile does not exist: {profile}")
    if profile_registry_status(root, profile) is False:
        raise RuntimeError(f"Selected commissioned profile is deactivated: {profile}")
    settings["beam_model_profile"] = str(profile)


def available_energy_layers(root: Path) -> dict:
    path = root / "plan_parsed" / "energy_layers.csv"
    plans = list((root / "dicom" / "RTPLAN").glob("*.dcm"))
    active_studies: set[str] = set()
    for modality in DICOM_MODALITIES:
        candidate = next(iter(sorted((root / "dicom" / modality).glob("*.dcm"))), None)
        if candidate is None:
            continue
        try:
            study_uid = str(
                getattr(pydicom.dcmread(candidate, stop_before_pixels=True), "StudyInstanceUID", "")
            )
        except Exception:
            study_uid = ""
        if study_uid:
            active_studies.add(study_uid)
    if len(active_studies) > 1:
        return {
            "ready": False,
            "layers": [],
            "signature": "",
            "message": "Finish importing all DICOM categories for the new patient, then run stage 3",
        }
    plan_mtime = max((item.stat().st_mtime_ns for item in plans), default=0)
    if not path.is_file() or path.stat().st_mtime_ns < plan_mtime:
        return {"ready": False, "layers": [], "signature": "", "message": "Run stage 3 to load current RTPLAN energies"}
    layers: list[dict] = []
    with path.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            layers.append(
                {
                    "layerIndex": int(row["LayerIndex"]),
                    "energyMeVu": float(row["Energy_MeVu"]),
                    "spots": int(row["NumberOfSpots"]),
                    "weightMu": float(row["TotalMetersetWeight_MU"]),
                }
            )
    if not layers:
        return {"ready": False, "layers": [], "signature": "", "message": "Current energy layer table is empty"}
    signature = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    return {"ready": True, "layers": layers, "signature": signature, "message": ""}


def selected_energy_layers(payload: dict, root: Path) -> Optional[list[int]]:
    if str(payload.get("beam_input_mode", "rtplan")).strip().lower() == "manual":
        return None
    raw = str(payload.get("energy_layer_indices", "all")).strip().lower()
    if raw in {"", "all"}:
        return None
    if raw == "none":
        raise RuntimeError("Select at least one RTPLAN energy layer")
    try:
        selected = [int(item.strip()) for item in raw.split(",") if item.strip()]
    except ValueError as exc:
        raise RuntimeError("Energy-layer selection must contain layer indices") from exc
    if not selected or any(item <= 0 for item in selected) or len(selected) != len(set(selected)):
        raise RuntimeError("Energy-layer selection must contain unique positive layer indices")
    available = available_energy_layers(root)
    if not available["ready"]:
        raise RuntimeError(str(available["message"]))
    valid = {int(item["layerIndex"]) for item in available["layers"]}
    missing = [item for item in selected if item not in valid]
    if missing:
        raise RuntimeError(f"Selected energy layer(s) are not in the current RTPLAN: {missing}")
    if set(selected) == valid:
        return None
    return selected


def energy_layer_arguments(layer_indices: Optional[list[int]]) -> list[str]:
    return ["--layer-indices", ",".join(str(item) for item in layer_indices)] if layer_indices else []


def resolve_topas(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_file():
        return path.resolve()
    resolved = shutil.which(value)
    if not resolved:
        raise RuntimeError(f"TOPAS executable not found: {value}")
    return Path(resolved)


def script(root: Path, name: str, *arguments: str) -> tuple[str, list[str], Path, Optional[Path]]:
    return (
        name,
        [sys.executable, str(APP_ROOT / "scripts" / name), "--root", str(root), *arguments],
        root,
        None,
    )


def planned_history_allocation(root: Path) -> list[int]:
    allocation = root / "plan_parsed" / "spot_history_allocation.csv"
    if not allocation.is_file():
        return []
    with allocation.open(newline="", encoding="utf-8") as stream:
        values: list[int] = []
        for row in csv.DictReader(stream):
            try:
                values.append(max(0, int(row.get("AllocatedHistories", 0))))
            except (TypeError, ValueError):
                values.append(0)
        return values


def planned_spot_count(root: Path) -> int:
    return len(planned_history_allocation(root))


def requested_spot_count(
    root: Path,
    beam: dict[str, object],
    layers: Optional[list[int]],
) -> int:
    if str(beam.get("beam_input_mode", "rtplan")) == "manual":
        return 1
    inventory = available_energy_layers(root)
    if inventory.get("ready"):
        selected = set(layers) if layers else None
        return sum(
            int(item["spots"])
            for item in inventory["layers"]
            if selected is None or int(item["layerIndex"]) in selected
        )
    return planned_spot_count(root)


def runtime_context_from_payload(root: Path, payload: dict) -> dict[str, Any]:
    beam = beam_settings(payload)
    validate_selected_beam_profile(root, beam)
    layers = selected_energy_layers(payload, root)
    histories = integer(payload, "histories")
    threads, requested_threads, thread_note = resolve_threads(payload)
    spots = requested_spot_count(root, beam, layers)
    estimate = estimate_topas_runtime(
        histories,
        threads,
        root=root,
        beam_model_mode=str(beam.get("beam_model_mode", "baseline")),
        spot_count=spots,
    )
    return {
        "histories": histories,
        "threads": threads,
        "requested_threads": requested_threads,
        "thread_limit_note": thread_note,
        "history_budget_note": history_budget_warning(root, histories, beam, layers),
        "logical_cpus": logical_cpu_count(),
        "beam_model_mode": str(beam.get("beam_model_mode", "baseline")),
        "beam_input_mode": str(beam.get("beam_input_mode", "rtplan")),
        "spot_count": spots,
        "energy_layer_indices": layers or "ALL",
        "estimate": estimate,
    }


def validate_run_ready(root: Path, histories: int, threads: int) -> None:
    states = {item["stage"]: item["state"] for item in collect_status(root)}
    required = (
        "Compatibility gate",
        "RTPLAN parsed",
        "TOPAS geometry",
        "TPS dose grid",
        "Full spot plan",
        "TOPAS run entry",
        "TOPAS preflight",
    )
    waiting = [name for name in required if states.get(name) != "READY"]
    if waiting:
        raise RuntimeError("Preparation is stale or incomplete: " + ", ".join(waiting))
    entry = root / "topas" / "run_full_plan_qa.txt"
    if f"i:Ts/NumberOfThreads = {threads}" not in entry.read_text(encoding="utf-8"):
        raise RuntimeError("Prepared run uses a different thread count; rerun stage 7")
    plan_summary = root / "plan_parsed" / "topas_plan_generation_summary.txt"
    expected_line = f"Histories requested / allocated: {histories} / {histories}"
    if expected_line not in plan_summary.read_text(encoding="utf-8"):
        raise RuntimeError("Generated spot plan uses a different history total; rerun stages 6–7")


def history_budget_warning(
    root: Path,
    histories: int,
    beam: dict[str, object],
    layers: Optional[list[int]],
) -> str:
    """Warn when the total cannot give every positive-weight spot one primary.

    This is allowed on purpose: a full plan costs hours, so a deliberately
    under-sampled run is a useful geometry/range/pipeline check. Stage 6 drops
    the zero-history spots from the TOPAS timeline, which is what actually makes
    the test short. The result is not a plan dose, so say so loudly instead of
    blocking. Returns "" when the budget is sufficient.
    """

    try:
        spots = requested_spot_count(root, beam, layers)
    except Exception:
        # Nothing parsed yet; stage 6 stays the authority.
        return ""
    if spots <= 0 or histories >= spots:
        return ""
    return (
        f"SPARSE TEST RUN: {histories:,} histories cannot give each of the {spots:,} "
        f"selected spots one primary. Only the {histories:,} highest-weight spots "
        f"({100.0 * histories / spots:.1f}%) will be simulated; the remaining "
        f"{spots - histories:,} are dropped from the TOPAS timeline (which is what makes "
        "the run short). The result is NOT a plan dose — whole regions receive nothing, so "
        "particle-number calibration, Gamma and TPS profile comparison are not valid for it. "
        f"Use at least {spots:,} histories for a physically meaningful result."
    )


def plan_warnings(root: Path, payload: dict) -> list[str]:
    """Non-blocking warnings shown in the log before any command starts."""

    notes: list[str] = []
    try:
        thread_note = resolve_threads(payload)[2]
    except RuntimeError:
        thread_note = ""
    if thread_note:
        notes.append(thread_note)
    try:
        beam = beam_settings(payload)
        layers = selected_energy_layers(payload, root)
        histories = integer(payload, "histories")
    except RuntimeError:
        return notes
    budget_note = history_budget_warning(root, histories, beam, layers)
    if budget_note:
        notes.append(budget_note)
    return notes


def build_commands(action: str, payload: dict) -> tuple[str, list[tuple[str, list[str], Path, Optional[Path]]]]:
    root = safe_root(str(payload.get("root", STATE.root)))
    histories = integer(payload, "histories")
    threads, _requested_threads, thread_note = resolve_threads(payload)
    seed = integer(payload, "seed", 0)
    output_tag = str(payload.get("output_tag", f"full_plan_{histories}")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", output_tag):
        raise RuntimeError("output_tag may contain only letters, digits, underscore and hyphen")
    beam = beam_settings(payload)
    validate_selected_beam_profile(root, beam)
    beam_args = beam_plan_arguments(beam)
    beam_geometry_args = beam_geometry_arguments(beam)
    layers = selected_energy_layers(payload, root)
    layer_args = energy_layer_arguments(layers)
    common = {
        "geometry": ("DICOM geometry check", [script(root, "02_check_dicom_geometry.py")]),
        "compatibility": ("Compatibility gate", [script(root, "07_validate_case_compatibility.py", "--overwrite")]),
        "parse": ("RTPLAN parsing", [script(root, "01_parse_ion_plan.py", "--overwrite")]),
        "case_geometry": (
            "Case geometry",
            [script(root, "08_generate_case_geometry.py", *beam_geometry_args, "--overwrite")],
        ),
        "scoring": ("TPS dose grid", [script(root, "03_build_topas_dose_scoring.py", "--overwrite")]),
        "full_plan": (
            "Full spot plan",
            [script(root, "04_generate_topas_plan.py", "--total-histories", str(histories), *beam_args, *layer_args, "--overwrite")],
        ),
        "prepare": (
            "TOPAS run preparation",
            [
                script(
                    root,
                    "09_prepare_topas_run.py",
                    "--histories",
                    str(histories),
                    "--threads",
                    str(threads),
                    "--seed",
                    str(seed),
                    "--output-tag",
                    output_tag,
                    "--overwrite",
                )
            ],
        ),
    }
    if action in common:
        return common[action]
    if action == "initialize":
        return (
            "Initialize case",
            [
                (
                    "Initialize case",
                    [sys.executable, str(APP_ROOT / "scripts" / "10_initialize_case.py"), "--case-root", str(root)],
                    APP_ROOT,
                    None,
                )
            ],
        )
    if action == "prepare_remote_bundle":
        return (
            "Prepare SSH transport bundle",
            [
                (
                    "Build audited remote bundle",
                    [
                        sys.executable,
                        str(APP_ROOT / "scripts" / "15_prepare_remote_bundle.py"),
                        "--root",
                        str(root),
                        "--app-root",
                        str(APP_ROOT),
                        "--output-tag",
                        output_tag,
                    ],
                    root,
                    None,
                )
            ],
        )
    if action == "pipeline":
        commands: list[tuple[str, list[str], Path, Optional[Path]]] = []
        for name in ("geometry", "compatibility", "parse", "case_geometry", "scoring", "full_plan", "prepare"):
            commands.extend(common[name][1])
        return "Preparation stages 1–7", commands
    if action == "preflight":
        topas = resolve_topas(str(payload.get("topas_executable", "topas")))
        plan_parse = root / "topas" / "validate_plan_full_parse.txt"
        grid = root / "topas" / "validate_dose_grid.txt"
        for path in (plan_parse, grid):
            if not path.is_file():
                raise RuntimeError(f"Preflight file missing: {path}")
        return (
            "TOPAS zero-history preflight",
            [
                (
                    "TOPAS full plan parse",
                    [str(topas), plan_parse.name],
                    plan_parse.parent,
                    root / "topas_output" / "test" / "validate_plan_full_parse.log",
                ),
                (
                    "TOPAS zero-history grid",
                    [str(topas), grid.name],
                    grid.parent,
                    root / "topas_output" / "test" / "validate_dose_grid.log",
                ),
                script(root, "03_validate_topas_dose_scoring.py", "--overwrite"),
                script(root, "12_validate_topas_preflight.py", "--overwrite"),
            ],
        )
    if action == "run_topas":
        topas = resolve_topas(str(payload.get("topas_executable", "topas")))
        states = {item["stage"]: item["state"] for item in collect_status(root)}
        core_required = ("Compatibility gate", "RTPLAN parsed", "TOPAS geometry", "TPS dose grid")
        waiting = [name for name in core_required if states.get(name) != "READY"]
        if waiting:
            raise RuntimeError("Stages 1–5 are incomplete: " + ", ".join(waiting))
        output = expected_mc_binary(root)
        tag = output_tag
        if output and (output.exists() or Path(str(output) + "header").exists()):
            archive_production_output(
                root,
                tag,
                output_binary=output,
                reason="Automatic archive before starting another TOPAS transport",
            )
        needs_rebuild = not prepared_run_matches(root, histories, threads, seed, beam, layers)
        entry = root / "topas" / "run_full_plan_qa.txt"
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = root / "topas_output" / "production" / f"run_full_plan_qa_{timestamp}.log"
        commands: list[tuple[str, list[str], Path, Optional[Path]]] = []
        if needs_rebuild:
            plan_parse = root / "topas" / "validate_plan_full_parse.txt"
            grid = root / "topas" / "validate_dose_grid.txt"
            commands.extend(
                [
                    script(root, "08_generate_case_geometry.py", *beam_geometry_args, "--overwrite"),
                    script(root, "04_generate_topas_plan.py", "--total-histories", str(histories), *beam_args, *layer_args, "--overwrite"),
                    script(
                        root, "09_prepare_topas_run.py", "--histories", str(histories),
                        "--threads", str(threads), "--seed", str(seed),
                        "--output-tag", tag, "--overwrite",
                    ),
                    (
                        "TOPAS full plan parse", [str(topas), plan_parse.name], plan_parse.parent,
                        root / "topas_output" / "test" / "validate_plan_full_parse.log",
                    ),
                    (
                        "TOPAS zero-history grid", [str(topas), grid.name], grid.parent,
                        root / "topas_output" / "test" / "validate_dose_grid.log",
                    ),
                    script(root, "03_validate_topas_dose_scoring.py", "--overwrite"),
                    script(root, "12_validate_topas_preflight.py", "--overwrite"),
                ]
            )
        commands.append(("TOPAS full-plan run", [str(topas), entry.name], entry.parent, log_path))
        if (
            output is not None
            and beam.get("beam_input_mode") == "rtplan"
            and beam.get("beam_model_mode") == "commissioned"
        ):
            analysis_dir = analysis_run_dir(root, tag, create=True)
            commands.append(
                script(
                    root,
                    "14_calibrate_mc_dose.py",
                    "--mc-binary",
                    str(output),
                    "--analysis-dir",
                    str(analysis_dir),
                    "--output-tag",
                    tag,
                    "--overwrite",
                )
            )
        return ("Prepare and run TOPAS" if needs_rebuild else "TOPAS full-plan run"), commands
    if action == "analyze":
        mc_value = str(payload.get("mc_binary", "")).strip()
        mc = Path(mc_value).expanduser().resolve() if mc_value else discover_mc_binary(root)
        if not mc or not mc_binary_matches_current_grid(root, mc):
            raise RuntimeError("Select a non-empty TOPAS .bin dose file matching the current TPS grid")
        try:
            depth = float(payload.get("profile_depth", 100))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("profile_depth must be numeric") from exc
        tag = str(payload.get("output_tag", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tag):
            raise RuntimeError("output_tag may contain only letters, digits, underscore and hyphen")
        logs = list((root / "topas_output" / "production").glob("run*.log")) + list(
            (root / "topas_output" / "test").glob("run*.log")
        )
        latest_log = max(logs, key=lambda path: path.stat().st_mtime) if logs else None
        analysis_dir = analysis_run_dir(root, tag, create=True)
        selected_tps_uid = str(payload.get("tps_dose_uid", "")).strip() or None
        tps_dose = resolve_tps_dose(root, selected_tps_uid)
        line_dataset = get_line_dose_dataset(root, mc, selected_tps_uid)
        selected_tps_uid = str(pydicom.dcmread(tps_dose, stop_before_pixels=True).SOPInstanceUID)
        update_run_manifest(
            root,
            tag,
            mc_source=mc,
            additions={
                "beam_settings": beam,
                "energy_layer_indices": layers or "ALL",
                "selected_tps_rtdose": str(tps_dose),
                "selected_tps_dose_uid": selected_tps_uid,
            },
        )
        args = [
            "--tps-dose",
            str(tps_dose),
            "--mc-binary",
            str(mc),
            "--analysis-dir",
            str(analysis_dir),
            "--profile-depth-mm",
            str(depth),
            "--output-tag",
            tag,
            "--mc-label",
            line_dataset.mc_label,
            "--full-plan",
            "--overwrite",
        ]
        if latest_log:
            args.extend(("--run-log", str(latest_log)))
        return "Profile export", [script(root, "06_export_three_direction_profiles.py", *args)]
    if action == "gamma":
        mc_value = str(payload.get("mc_binary", "")).strip()
        mc = Path(mc_value).expanduser().resolve() if mc_value else discover_mc_binary(root)
        if not mc or not mc_binary_matches_current_grid(root, mc):
            raise RuntimeError("Select a non-empty TOPAS .bin dose file matching the current TPS grid")
        try:
            dta = float(payload.get("gamma_dta_mm", 3.0))
            dd = float(payload.get("gamma_dd_percent", 3.0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Gamma DTA and DD must be numeric") from exc
        if not 0.0 < dta <= 20.0:
            raise RuntimeError("Gamma DTA must be within (0, 20] mm")
        if not 0.0 < dd <= 100.0:
            raise RuntimeError("Gamma DD must be within (0, 100] percent")
        tag = str(payload.get("output_tag", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tag):
            raise RuntimeError("output_tag may contain only letters, digits, underscore and hyphen")
        analysis_dir = analysis_run_dir(root, tag, create=True)
        selected_tps_uid = str(payload.get("tps_dose_uid", "")).strip() or None
        tps_dose = resolve_tps_dose(root, selected_tps_uid)
        get_line_dose_dataset(root, mc, selected_tps_uid)
        selected_tps_uid = str(pydicom.dcmread(tps_dose, stop_before_pixels=True).SOPInstanceUID)
        update_run_manifest(
            root,
            tag,
            mc_source=mc,
            additions={
                "beam_settings": beam,
                "energy_layer_indices": layers or "ALL",
                "selected_tps_rtdose": str(tps_dose),
                "selected_tps_dose_uid": selected_tps_uid,
            },
        )
        arguments = [
            "--tps-dose",
            str(tps_dose),
            "--mc-binary",
            str(mc),
            "--analysis-dir",
            str(analysis_dir),
            "--dta-mm",
            str(dta),
            "--dd-percent",
            str(dd),
            "--low-dose-threshold-percent",
            "10",
            "--output-tag",
            tag,
            "--overwrite",
        ]
        return "Global 3D Gamma analysis", [script(root, "11_gamma_analysis.py", *arguments)]
    raise RuntimeError(f"Unknown action: {action}")


def start_commands(
    title: str,
    commands: list[tuple[str, list[str], Path, Optional[Path]]],
    root: Path,
    runtime_context: Optional[dict[str, Any]] = None,
    form_state: Optional[dict[str, str]] = None,
    notes: Optional[list[str]] = None,
) -> None:
    started = time.monotonic()
    with STATE.lock:
        if STATE.running:
            raise RuntimeError(f"Another task is running: {STATE.task}")
        STATE.running = True
        STATE.paused = False
        STATE.task = title
        STATE.last_ok = None
        STATE.last_error = ""
        STATE.root = root
        STATE.process_group_id = None
        STATE.progress_before_pause = None
        STATE.task_started_monotonic = started
        STATE.command_started_monotonic = None
        STATE.process_started_monotonic = None
        STATE.pause_started_monotonic = None
        STATE.paused_accumulated_seconds = 0.0
        STATE.current_command = "Starting..."
        STATE.runtime_request = dict(runtime_context or {})
        STATE.runtime_estimate = dict((runtime_context or {}).get("estimate", {}))
        if form_state:
            STATE.active_request = dict(form_state)
        STATE.last_task_elapsed_seconds = 0.0
        STATE.last_process_active_seconds = 0.0
        STATE.progress = {
            "phase": title,
            "label": "Starting...",
            "fraction": 0.0,
            "current": 0,
            "total": len(commands),
            "mode": "commands",
        }
        STATE.log.append(f"\n=== {title} ===\n")
        for note in notes or []:
            STATE.log.append(f"WARNING: {note}\n")

    def worker() -> None:
        ok = True
        error = ""
        try:
            for command_index, (label, argv, cwd, log_path) in enumerate(commands):
                allocations = planned_history_allocation(root) if label == "TOPAS full-plan run" else []
                total_spots = len(allocations)
                total_histories = sum(allocations)
                requested_threads = max(1, int(STATE.runtime_request.get("threads", 1) or 1))
                history_offsets: list[int] = []
                work_offsets: list[float] = []
                running_histories = 0
                running_work = 0.0
                for allocated in allocations:
                    history_offsets.append(running_histories)
                    running_histories += allocated
                    work_offsets.append(running_work)
                    running_work += 1.0 + math.ceil(allocated / requested_threads)
                completed_histories = 0
                completed_work = 0.0
                with STATE.lock:
                    STATE.current_command = label
                    STATE.command_started_monotonic = time.monotonic()
                    STATE.process_started_monotonic = None
                    STATE.last_process_active_seconds = 0.0
                    STATE.pause_started_monotonic = None
                    STATE.paused_accumulated_seconds = 0.0
                    STATE.progress = {
                        "phase": title,
                        "label": label,
                        "fraction": command_index / max(1, len(commands)),
                        "current": command_index,
                        "total": len(commands),
                        "mode": "spot_runs" if label == "TOPAS full-plan run" else "commands",
                    }
                STATE.append("\n$ " + " ".join(argv) + "\n")
                process = subprocess.Popen(
                    argv,
                    cwd=str(cwd),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                with STATE.lock:
                    STATE.process = process
                    # The child leads a separate POSIX process group, so pause
                    # signals freeze TOPAS and its children without freezing
                    # the GUI server.
                    STATE.process_group_id = process.pid
                    STATE.process_started_monotonic = time.monotonic()
                captured: list[str] = []
                assert process.stdout is not None
                for line in process.stdout:
                    captured.append(line)
                    if label == "TOPAS full-plan run":
                        match = re.search(
                            r"Begin processing for Run:\s*(\d+),\s*History:\s*(\d+)", line
                        )
                        if match:
                            spot_index = int(match.group(1))
                            history_index = int(match.group(2))
                            if 0 <= spot_index < total_spots:
                                current = history_offsets[spot_index] + min(
                                    history_index + 1, allocations[spot_index]
                                )
                                completed_histories = max(completed_histories, current)
                                spot_fraction = min(
                                    1.0, (history_index + 1) / max(1, allocations[spot_index])
                                )
                                spot_work = 1.0 + math.ceil(
                                    allocations[spot_index] / requested_threads
                                )
                                completed_work = max(
                                    completed_work,
                                    work_offsets[spot_index] + spot_fraction * spot_work,
                                )
                            if total_histories and running_work:
                                with STATE.lock:
                                    STATE.progress = {
                                        "phase": title,
                                        "label": (
                                            f"TOPAS transport: history {completed_histories:,} / "
                                            f"{total_histories:,} (spot {spot_index + 1:,} / {total_spots:,})"
                                        ),
                                        "fraction": min(1.0, completed_work / running_work),
                                        "current": completed_histories,
                                        "total": total_histories,
                                        "mode": "spot_runs",
                                        "completed_spots": min(total_spots, spot_index + 1),
                                        "total_spots": total_spots,
                                        "completed_histories": completed_histories,
                                        "total_histories": total_histories,
                                    }
                    STATE.append(line)
                code = process.wait()
                with STATE.lock:
                    if STATE.process_started_monotonic is not None:
                        STATE.last_process_active_seconds = max(
                            0.0,
                            time.monotonic()
                            - STATE.process_started_monotonic
                            - STATE.paused_accumulated_seconds,
                        )
                    STATE.process = None
                    STATE.process_group_id = None
                    STATE.process_started_monotonic = None
                    STATE.paused = False
                    STATE.progress_before_pause = None
                    if label == "TOPAS full-plan run" and code == 0:
                        total = total_histories or total_spots
                        STATE.progress = {
                            "phase": title,
                            "label": (
                                f"TOPAS transport complete ({total_histories:,} histories)"
                                if total_histories
                                else f"TOPAS transport complete ({total_spots:,} spots)"
                                if total_spots
                                else "TOPAS transport complete"
                            ),
                            "fraction": 1.0,
                            "current": total,
                            "total": total,
                            "mode": "complete",
                        }
                    elif code == 0:
                        STATE.progress["current"] = command_index + 1
                        STATE.progress["total"] = len(commands)
                        STATE.progress["fraction"] = (command_index + 1) / max(1, len(commands))
                if log_path:
                    log_path.parent.mkdir(parents=True, exist_ok=True)
                    log_path.write_text("".join(captured), encoding="utf-8")
                if code != 0:
                    ok = False
                    error = f"{label} exited with status {code}"
                    break
        except Exception as exc:
            ok = False
            error = str(exc)
        with STATE.lock:
            if STATE.task_started_monotonic is not None:
                STATE.last_task_elapsed_seconds = max(
                    0.0, time.monotonic() - STATE.task_started_monotonic
                )
            STATE.task_started_monotonic = None
            STATE.running = False
            STATE.paused = False
            STATE.process = None
            STATE.process_group_id = None
            STATE.process_started_monotonic = None
            STATE.pause_started_monotonic = None
            STATE.progress_before_pause = None
            STATE.last_ok = ok
            STATE.last_error = error
            STATE.task = "Idle"
            STATE.current_command = "Complete" if ok else f"Stopped: {error}"
            if ok:
                STATE.progress = {"phase": title, "label": "Complete", "fraction": 1.0, "current": 1, "total": 1, "mode": "complete"}
            else:
                STATE.progress = {
                    "phase": title,
                    "label": f"Stopped: {error}",
                    "fraction": STATE.progress.get("fraction", 0.0),
                    "current": STATE.progress.get("current", 0),
                    "total": STATE.progress.get("total", 0),
                    "mode": "error",
                }
            STATE.log.append("\nCompleted successfully.\n" if ok else f"\nStopped: {error}\n")
        if BATCH is not None:
            BATCH.kick()

    threading.Thread(target=worker, daemon=True).start()


def _signal_active_process(sig: int) -> None:
    with STATE.lock:
        process = STATE.process
        group_id = STATE.process_group_id
    if process is None or process.poll() is not None:
        raise RuntimeError("No active process is available")
    try:
        if group_id is not None:
            os.killpg(group_id, sig)
        else:
            process.send_signal(sig)
    except ProcessLookupError as exc:
        raise RuntimeError("The active process finished before the signal was applied") from exc
    except PermissionError as exc:
        raise RuntimeError("The active process cannot be controlled by this GUI") from exc


def pause_active_task() -> str:
    if not hasattr(signal, "SIGSTOP"):
        raise RuntimeError("Pause/resume is not supported on this operating system")
    with STATE.lock:
        if not STATE.running:
            raise RuntimeError("No task is running")
        if STATE.paused:
            return "Task is already paused"
        if STATE.process is None:
            raise RuntimeError("The task is between commands; try Pause again when the next command starts")
    _signal_active_process(signal.SIGSTOP)
    with STATE.lock:
        STATE.paused = True
        STATE.pause_started_monotonic = time.monotonic()
        STATE.progress_before_pause = dict(STATE.progress)
        previous_label = str(STATE.progress.get("label", STATE.task))
        STATE.progress = {
            **STATE.progress,
            "label": f"Paused — {previous_label}",
            "mode": "paused",
        }
        STATE.log.append("\n[GUI] Task paused. Process memory and output files are preserved.\n")
    return "Task paused; click Resume task to continue from the same process state"


def resume_active_task() -> str:
    if not hasattr(signal, "SIGCONT"):
        raise RuntimeError("Pause/resume is not supported on this operating system")
    with STATE.lock:
        if not STATE.running or STATE.process is None:
            raise RuntimeError("No paused task is available")
        if not STATE.paused:
            return "Task is already running"
    _signal_active_process(signal.SIGCONT)
    with STATE.lock:
        if STATE.pause_started_monotonic is not None:
            STATE.paused_accumulated_seconds += max(
                0.0, time.monotonic() - STATE.pause_started_monotonic
            )
        STATE.pause_started_monotonic = None
        if STATE.progress_before_pause is not None:
            STATE.progress = dict(STATE.progress_before_pause)
        STATE.progress_before_pause = None
        STATE.paused = False
        STATE.log.append("[GUI] Task resumed.\n")
    return "Task resumed from the paused process state"


def stop_active_task() -> str:
    with STATE.lock:
        process = STATE.process
        paused = STATE.paused
    if process is None or process.poll() is not None:
        raise RuntimeError("No process is running")
    # A stopped process must be continued before SIGTERM can be handled.
    if paused and hasattr(signal, "SIGCONT"):
        _signal_active_process(signal.SIGCONT)
    _signal_active_process(signal.SIGTERM)
    with STATE.lock:
        if STATE.pause_started_monotonic is not None:
            STATE.paused_accumulated_seconds += max(
                0.0, time.monotonic() - STATE.pause_started_monotonic
            )
        STATE.pause_started_monotonic = None
        STATE.paused = False
        STATE.progress_before_pause = None
    return "Termination requested"


HTML = r'''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>TPS–TOPAS QA Workflow</title>
<style>
:root{--navy:#14213d;--blue:#2563eb;--green:#16835d;--amber:#b66b08;--bg:#f3f6fa;--card:#fff;--line:#dce3ed;--muted:#65758b;--red:#bc2d3d}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#233148;font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
header{background:linear-gradient(120deg,#13213e,#27466f);color:white;padding:25px 34px 22px}h1{margin:0;font-size:27px;letter-spacing:.1px}header p{margin:6px 0 17px;color:#cfd9e8}.casebar{display:flex;gap:10px;max-width:1260px;align-items:stretch}.case-path{flex:1 1 0;min-width:0;background:#ffffff16;border:1px solid #ffffff38;border-radius:9px;padding:8px 12px}.case-path span{display:block;color:#bfcde1;font-size:11px;text-transform:uppercase;letter-spacing:.6px}.case-path strong{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:14px;margin-top:2px}.casebar button{white-space:nowrap}
input,select{border:1px solid #cbd5e1;border-radius:8px;background:#fff;padding:9px 10px;font:inherit;min-width:0}button{border:0;border-radius:8px;background:#e8edf5;color:#22334d;padding:9px 13px;font-weight:650;cursor:pointer}button:hover{filter:brightness(.97)}button.primary{background:var(--blue);color:white}button.danger{background:#fee8e8;color:var(--red)}button.pause{background:#fff2d6;color:#8a5700}button.resume{background:#dcf6e9;color:#116b4d}button:disabled{opacity:.45;cursor:not-allowed}
nav{display:flex;gap:3px;padding:0 30px;background:#fff;border-bottom:1px solid var(--line)}nav button{border-radius:0;background:none;padding:14px 16px;color:var(--muted)}nav button.active{color:var(--blue);box-shadow:inset 0 -3px var(--blue)}
main{max-width:1420px;margin:auto;padding:22px 28px}.tab{display:none}.tab.active{display:block}.grid{display:grid;grid-template-columns:minmax(620px,1.5fr) minmax(390px,1fr);gap:20px}.card{background:var(--card);border:1px solid var(--line);border-radius:13px;padding:18px;box-shadow:0 3px 13px #1732530a}h2{font-size:18px;margin:0 0 14px;color:var(--navy)}
.step{display:grid;grid-template-columns:34px 1fr auto;align-items:center;gap:10px;padding:9px 2px;border-bottom:1px solid #edf1f6}.step:last-child{border:0}.number{width:27px;height:27px;border-radius:50%;display:grid;place-items:center;background:#eaf1ff;color:var(--blue);font-weight:750}.step strong{display:block;color:var(--navy)}.step small{color:var(--muted)}
.params{display:grid;grid-template-columns:140px 1fr;gap:9px;align-items:center}.wide{grid-column:1/-1;display:grid;grid-template-columns:1fr auto;gap:8px}.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:14px}.actions .primary,.actions .log-button{grid-column:1/-1}.progress-panel{margin-top:14px;padding-top:13px;border-top:1px solid var(--line)}.sparse-warning{display:block;margin-top:5px;color:#8a5000;font-weight:600;white-space:normal;line-height:1.45}.run-lock-note{margin-top:14px;padding:10px 12px;border:1px solid var(--amber);border-radius:8px;background:#fff8e8;color:#6b4c00;font-size:12px;line-height:1.55}.progress-head{display:flex;justify-content:space-between;gap:12px;color:var(--muted);font-size:12px;margin-bottom:6px}.progress-track{height:10px;background:#e7edf5;border-radius:999px;overflow:hidden}.progress-fill{height:100%;width:0;background:var(--blue);transition:width .25s ease}.progress-fill.paused{background:var(--amber)}.progress-fill.indeterminate{width:35%;animation:progress-slide 1.2s ease-in-out infinite alternate}@keyframes progress-slide{from{margin-left:0}to{margin-left:65%}}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:8px 6px;border-bottom:1px solid #edf1f6;vertical-align:top}th{color:var(--muted)}td:nth-child(2){font-weight:750}.ready{color:var(--green)}.waiting{color:var(--amber)}
#log{height:520px;overflow:auto;white-space:pre-wrap;background:#101827;color:#d8e3f2;border-radius:10px;padding:16px;font:12px/1.45 Menlo,monospace}.workflow-log{margin-top:18px;scroll-margin-top:16px}.compute-monitor{border:1px solid var(--line);border-radius:10px;background:#f8fafc;margin-bottom:12px;overflow:hidden}.compute-monitor summary{cursor:pointer;padding:10px 12px;color:var(--navy);font-weight:750;display:flex;gap:8px;align-items:center}.compute-monitor summary .spacer{flex:1}.compute-body{border-top:1px solid var(--line);padding:11px}.compute-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}.compute-metric{background:#fff;border:1px solid #e7ecf3;border-radius:8px;padding:8px 9px;min-width:0}.compute-metric small{display:block;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.3px}.compute-metric strong{display:block;color:var(--navy);font-size:13px;margin-top:2px;overflow-wrap:anywhere}.compute-subtitle{margin:10px 0 5px;color:var(--muted);font-size:11px;font-weight:750;text-transform:uppercase}.compute-table{font:11px/1.35 Menlo,monospace;background:#fff;border:1px solid #e7ecf3;border-radius:8px;overflow:hidden}.compute-table table{font-size:11px}.compute-table th,.compute-table td{padding:6px}.compute-note{color:var(--muted);font-size:11px;margin-top:8px}.resultbar{display:flex;gap:8px;align-items:center;margin-bottom:12px}.resultbar .spacer{flex:1}.resultgrid{display:grid;grid-template-columns:minmax(640px,2fr) minmax(320px,1fr);gap:18px}.imagebox{min-height:600px;display:grid;place-items:center;background:#fafbfd;border-radius:9px;overflow:hidden}.imagebox img{max-width:100%;max-height:690px}.summary{height:650px;overflow:auto;white-space:pre-wrap;font:12px/1.5 Menlo,monospace;background:#f8fafc;padding:12px;border-radius:8px}
.guide{max-width:940px}.guide h3{color:var(--navy);margin-top:22px}.notice{padding:12px 14px;border-radius:9px;background:#fff8e7;border:1px solid #f2d797;color:#6f4a08;margin-bottom:14px}.pill{display:inline-block;border-radius:99px;padding:4px 9px;background:#eaf1ff;color:var(--blue);font-weight:700}.gamma-box{margin-top:16px;padding-top:14px;border-top:1px solid var(--line)}.gamma-box h3{margin:0 0 9px;color:var(--navy);font-size:15px}.gamma-rate{font-size:22px;font-weight:800;color:var(--navy);margin:4px 0 10px}.gamma-meta{color:var(--muted);font-size:12px}.tps-dose-picker{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}.tps-dose-picker select{max-width:390px}#toast{position:fixed;right:24px;bottom:22px;background:#15243e;color:white;padding:12px 16px;border-radius:9px;display:none;max-width:460px;box-shadow:0 8px 30px #0004}
.import-card{margin-bottom:16px}.import-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.import-head p{margin:0;color:var(--muted)}.import-grid{display:grid;grid-template-columns:repeat(4,minmax(180px,1fr));gap:10px}.import-tile{border:1px solid var(--line);border-radius:10px;padding:13px;background:#fafcff;display:grid;grid-template-columns:1fr auto;gap:10px;align-items:center}.import-tile strong{display:block;color:var(--navy);font-size:15px}.import-tile small{display:block;color:var(--muted);margin-top:2px}.import-tile button{background:#eaf1ff;color:var(--blue)}.import-tile.busy{border-color:#8eb2f5;background:#f3f7ff}.file-input{display:none}
.modal-backdrop{position:fixed;inset:0;background:#0c1729aa;display:none;align-items:center;justify-content:center;padding:24px;z-index:20}.modal-backdrop.open{display:flex}.modal{width:min(980px,100%);max-height:92vh;overflow:auto;background:white;border-radius:14px;padding:22px;box-shadow:0 22px 80px #0008}.modal h2{margin-bottom:5px}.modal-subtitle{color:var(--muted);margin:0 0 16px}.run-options{display:grid;gap:9px}.run-option{display:grid;grid-template-columns:28px 1.4fr 1fr .65fr 1.4fr;gap:10px;align-items:center;border:1px solid var(--line);border-radius:10px;padding:11px 12px;cursor:pointer}.run-option:hover,.run-option.selected{border-color:#7ea6ee;background:#f4f8ff}.run-option strong{color:var(--navy)}.run-option small{display:block;color:var(--muted)}.run-option .estimate{font-weight:700;color:var(--blue)}.custom-run{display:grid;grid-template-columns:28px 1.4fr 1fr .65fr 1.4fr;gap:10px;align-items:center;border:1px solid var(--line);border-radius:10px;padding:11px 12px}.custom-run input{width:100%}.modal-note{padding:11px 13px;background:#fff8e7;border:1px solid #f2d797;border-radius:9px;color:#6f4a08;margin-top:14px}.modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:17px}
.line-toolbar{display:flex;flex-wrap:wrap;gap:9px;align-items:end;padding:12px;background:#f8fafc;border:1px solid var(--line);border-radius:10px;margin-bottom:12px}.line-control{display:grid;gap:4px}.line-control label{font-size:11px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.35px}.line-control input[type=range]{width:220px;padding:0}.line-dose-grid{display:grid;grid-template-columns:minmax(520px,1.35fr) minmax(420px,1fr);gap:16px}.dose-canvas-wrap{position:relative;height:620px;background:#07111f;border-radius:10px;overflow:hidden;touch-action:none}.dose-canvas-wrap canvas{display:block;width:100%;height:100%;cursor:crosshair}.dose-readout{position:absolute;left:12px;bottom:10px;max-width:calc(100% - 24px);padding:7px 10px;border-radius:7px;background:#07111fd9;color:#e8f1ff;font:12px/1.4 Menlo,monospace;pointer-events:none}.dose-help{position:absolute;left:12px;top:10px;padding:6px 9px;border-radius:7px;background:#07111fc9;color:#d8e5f7;font-size:12px;pointer-events:none}.line-chart-panel{border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}.line-chart-wrap{height:390px;position:relative}.line-chart-wrap canvas{display:block;width:100%;height:100%}.line-hover{min-height:24px;color:var(--muted);font:12px/1.4 Menlo,monospace;margin-top:5px}.line-caption{font-size:12px;color:var(--muted);margin-bottom:7px;word-break:break-word}.line-stats{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}.line-stat{background:#f8fafc;border-radius:8px;padding:9px}.line-stat strong{display:block;color:var(--navy);margin-bottom:4px}.line-stat span{display:block;color:var(--muted);font-size:12px}.line-status{padding:10px 12px;border-radius:9px;background:#eef5ff;color:#244a82;margin-bottom:12px}.line-legend{display:flex;gap:16px;align-items:center;color:var(--muted);font-size:12px}.line-key{display:inline-block;width:18px;height:3px;border-radius:3px;margin-right:5px;vertical-align:middle}.report-controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.beam-panel{grid-column:1/-1;border:1px solid var(--line);border-radius:9px;background:#f8fafc;padding:10px 12px}.beam-panel summary{cursor:pointer;color:var(--navy);font-weight:750}.beam-grid{display:grid;grid-template-columns:1fr 120px 1fr 120px;gap:8px 10px;align-items:center;margin-top:11px}.beam-warning{grid-column:1/-1;color:#7a4c06;background:#fff8e7;border:1px solid #f2d797;border-radius:7px;padding:8px}.cache-select{max-width:300px}.cache-info{color:var(--muted);font-size:12px}.qa-export{display:flex;gap:7px;align-items:end;border-left:1px solid var(--line);padding-left:10px}.iso-button{background:#fff4cf;color:#7b5500}
.beam-source-options{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:11px}.beam-source-choice{display:flex;gap:9px;align-items:flex-start;border:1px solid var(--line);border-radius:8px;background:#fff;padding:10px;cursor:pointer}.beam-source-choice input{margin-top:2px}.beam-source-choice strong{display:block;color:var(--navy)}.beam-source-choice small{display:block;color:var(--muted);margin-top:2px}.manual-beam-grid{display:grid;grid-template-columns:1fr 120px 1fr 120px;gap:8px 10px;align-items:center;margin-top:11px;padding-top:11px;border-top:1px solid var(--line)}.manual-beam-grid[hidden]{display:none}
.energy-toolbar{display:flex;align-items:center;gap:7px;margin:10px 0 8px}.energy-toolbar span{color:var(--muted);font-size:12px;margin-left:auto}.energy-layer-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;max-height:245px;overflow:auto;padding-right:3px}.energy-layer{display:flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:7px;background:#fff;padding:7px 8px;cursor:pointer}.energy-layer:hover{border-color:#8eb2f5;background:#f4f8ff}.energy-layer input{margin:0}.energy-layer span{min-width:0}.energy-layer strong{display:block;color:var(--navy);font-size:12px}.energy-layer small{display:block;color:var(--muted);font-size:10px}
.queue-toolbar{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin-bottom:14px}.queue-toolbar .spacer{flex:1}.queue-toolbar label{display:flex;gap:7px;align-items:center;color:var(--muted)}.queue-summary{display:flex;gap:8px;align-items:center;margin-bottom:13px}.queue-table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px}.queue-table-wrap table{min-width:1050px}.queue-case strong{display:block;color:var(--navy)}.queue-case small,.queue-config small,.queue-time small{display:block;color:var(--muted);font-weight:400}.queue-state{font-weight:800;text-transform:uppercase;font-size:11px;letter-spacing:.3px}.queue-state.running{color:var(--blue)}.queue-state.paused,.queue-state.queued{color:var(--amber)}.queue-state.completed{color:var(--green)}.queue-state.completed_with_warnings{color:var(--amber)}.queue-state.failed,.queue-state.interrupted,.queue-state.cancelled,.queue-state.cancelling{color:var(--red)}.queue-progress{height:8px;background:#e7edf5;border-radius:999px;overflow:hidden;margin:5px 0}.queue-progress span{display:block;height:100%;background:var(--blue)}.queue-actions{display:flex;flex-wrap:wrap;gap:5px}.queue-actions button{padding:6px 8px;font-size:11px}.queue-empty{text-align:center;color:var(--muted);padding:32px}.queue-log-panel{margin-top:16px}.queue-log-panel summary{cursor:pointer;color:var(--navy);font-weight:750}.queue-log-caption{color:var(--muted);font-size:12px;margin:9px 0}.queue-log{height:360px;overflow:auto;white-space:pre-wrap;background:#101827;color:#d8e3f2;border-radius:10px;padding:14px;font:12px/1.45 Menlo,monospace}
.queue-intake{border:1px solid #9cb9ee;background:#f5f8ff;border-radius:11px;padding:15px;margin:0 0 15px}.queue-intake[hidden]{display:none}.queue-intake-head{display:flex;align-items:flex-start;gap:12px;margin-bottom:12px}.queue-intake-head>div{min-width:0;flex:1}.queue-intake-head strong{display:block;color:var(--navy);font-size:15px}.queue-intake-head small{display:block;color:var(--muted);overflow-wrap:anywhere;margin-top:2px}.queue-intake-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:12px}.queue-intake-note{color:var(--muted);font-size:12px;margin-top:10px}.queue-intake .import-tile.ready-import{border-color:#82caae;background:#f2fbf7}.queue-intake .import-tile.ready-import button{background:#dcf6e9;color:#116b4d}
.machine-layout{display:grid;grid-template-columns:minmax(430px,.9fr) minmax(620px,1.4fr);gap:18px}.machine-upload{border:2px dashed #a8b8cf;border-radius:11px;background:#f8fafc;padding:20px;text-align:center}.machine-upload h3{margin:0 0 5px;color:var(--navy)}.machine-upload p{color:var(--muted);margin:0 0 14px}.machine-summary{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}.finding-list{max-height:440px;overflow:auto;border:1px solid var(--line);border-radius:9px}.finding{display:grid;grid-template-columns:64px minmax(140px,.45fr) 1fr;gap:8px;padding:8px 10px;border-bottom:1px solid #edf1f6}.finding:last-child{border:0}.finding-level{font-weight:850;font-size:11px;letter-spacing:.25px}.finding-level.pass{color:var(--green)}.finding-level.warn{color:var(--amber)}.finding-level.block{color:var(--red)}.machine-table-wrap{overflow:auto;border:1px solid var(--line);border-radius:10px}.machine-table-wrap table{min-width:900px}.machine-id strong{display:block;color:var(--navy)}.machine-id small{display:block;color:var(--muted);overflow-wrap:anywhere}.machine-status.active{color:var(--green);font-weight:800}.machine-status.inactive{color:var(--muted);font-weight:800}.asset-note{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-top:14px}.asset-note div{border:1px solid var(--line);border-radius:9px;padding:10px;background:#fafcff}.asset-note strong{display:block;color:var(--navy)}.asset-note small{color:var(--muted)}
.ssh-layout{display:grid;grid-template-columns:minmax(520px,.95fr) minmax(620px,1.05fr);gap:18px}.ssh-form{display:grid;grid-template-columns:180px minmax(0,1fr);gap:10px 12px;align-items:center}.ssh-form label{color:var(--muted);font-size:13px}.ssh-form input,.ssh-form select{width:100%;min-width:0}.ssh-inline{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:8px}.ssh-check{display:flex;align-items:center;gap:8px;color:var(--navy)!important}.ssh-check input{width:auto}.ssh-actions{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0}.ssh-policy{display:grid;gap:7px}.ssh-policy div{border:1px solid var(--line);border-radius:8px;background:#fafcff;padding:9px}.ssh-policy strong{display:block;color:var(--navy);font-size:12px}.ssh-policy small{display:block;color:var(--muted);margin-top:2px}.ssh-host-key{border:1px solid var(--line);border-radius:9px;padding:10px;margin-top:8px;background:#fafcff}.ssh-host-key code{display:block;overflow-wrap:anywhere;margin:5px 0}.ssh-bundle{border:1px solid var(--line);border-radius:10px;background:#fafcff;padding:13px;margin-top:12px}.ssh-bundle strong{display:block;color:var(--navy)}.ssh-bundle small{display:block;color:var(--muted);overflow-wrap:anywhere;margin-top:3px}.ssh-empty{color:var(--muted);padding:24px;text-align:center;border:1px dashed #b9c5d5;border-radius:10px}.ssh-warning{padding:11px 13px;border-radius:9px;background:#fff0f1;border:1px solid #f1b6be;color:#802333;margin-top:12px}
@media(max-width:1100px){.import-grid{grid-template-columns:1fr 1fr}.line-dose-grid,.machine-layout,.ssh-layout{grid-template-columns:1fr}.dose-canvas-wrap{height:560px}.compute-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:980px){.grid,.resultgrid{grid-template-columns:1fr}.casebar{flex-wrap:wrap}.case-path{flex-basis:100%}}@media(max-width:620px){.import-grid,.asset-note{grid-template-columns:1fr}.dose-canvas-wrap{height:430px}.line-control input[type=range]{width:160px}.line-stats{grid-template-columns:1fr}.energy-layer-grid,.beam-source-options{grid-template-columns:1fr}.manual-beam-grid,.beam-grid,.ssh-form{grid-template-columns:1fr}.compute-metrics{grid-template-columns:1fr}.finding{grid-template-columns:55px 1fr}.finding span:last-child{grid-column:1/-1}}
</style></head>
<body><header><h1>TPS–TOPAS Carbon-Ion QA Workflow</h1><p>Reusable physical-dose shape verification • local-only interface at 127.0.0.1</p><div class="casebar"><input id="root" type="hidden"><div class="case-path"><span>Current case folder</span><strong id="casePath">__ROOT__</strong></div><button onclick="selectCaseFolder()">Choose / create case</button><button onclick="refreshAll()">Refresh</button></div></header>
<nav><button class="active" data-tab="workflow">Workflow</button><button data-tab="queue">Batch queue</button><button data-tab="machines">Machine models</button><button data-tab="ssh">SSH server</button><button data-tab="results">Results</button><button data-tab="guide">Scope & guide</button></nav>
<main>
<section id="workflow" class="tab active"><div class="card import-card"><div class="import-head"><div><h2 style="margin-bottom:3px">Import TPS DICOM</h2><p>Click each category to select files. Imported files remain local to this case.</p></div></div><div class="import-grid" style="margin-top:14px">
<div class="import-tile" id="tile_CT"><div><strong>CT</strong><small id="count_CT">0 files</small></div><button data-import onclick="selectDicom('CT')">Choose folder</button><input class="file-input" id="file_CT" type="file" multiple webkitdirectory directory></div>
<div class="import-tile" id="tile_RTPLAN"><div><strong>RTPLAN</strong><small id="count_RTPLAN">0 files</small></div><button data-import onclick="selectDicom('RTPLAN')">Choose file</button><input class="file-input" id="file_RTPLAN" type="file"></div>
<div class="import-tile" id="tile_RTDOSE"><div><strong>RTDOSE</strong><small id="count_RTDOSE">0 files</small></div><button data-import onclick="selectDicom('RTDOSE')">Choose files</button><input class="file-input" id="file_RTDOSE" type="file" multiple></div>
<div class="import-tile" id="tile_RTSTRUCT"><div><strong>RTSTRUCT</strong><small id="count_RTSTRUCT">0 files</small></div><button data-import onclick="selectDicom('RTSTRUCT')">Choose file</button><input class="file-input" id="file_RTSTRUCT" type="file"></div>
</div></div><div class="notice"><b>Supported research scope:</b> one fully stripped Carbon-12 PBS beam, HFS/G90/couch0, axis-aligned regular RPPD, and either a rectangular 0-HU water phantom or axial DICOM CT patient. Patient CT uses a generic uncommissioned Schneider table; compatibility does not mean clinical commissioning.</div><div class="grid">
<div><div class="card"><h2>Preparation and calculation</h2><div id="steps"></div></div><div id="workflowLog" class="card workflow-log"><div class="resultbar"><h2 style="margin:0">Commands and live output</h2><span class="spacer"></span><button onclick="clearLog()">Clear view</button></div><details id="computeMonitor" class="compute-monitor"><summary><span>Compute status</span><span id="computeSummary" class="pill">Idle</span><span class="spacer"></span><small>CPU · processes · threads · ETA</small></summary><div class="compute-body"><div class="compute-metrics"><div class="compute-metric"><small>Active command</small><strong id="computeCommand">Idle</strong></div><div class="compute-metric"><small>Elapsed / active</small><strong id="computeElapsed">—</strong></div><div class="compute-metric"><small>Live ETA</small><strong id="computeEta">—</strong></div><div class="compute-metric"><small>Planned estimate</small><strong id="computePlan">—</strong></div><div class="compute-metric"><small>Task CPU</small><strong id="computeCpu">—</strong></div><div class="compute-metric"><small>Task memory</small><strong id="computeMemory">—</strong></div><div class="compute-metric"><small>Threads</small><strong id="computeThreads">—</strong></div><div class="compute-metric"><small>System</small><strong id="computeSystem">—</strong></div></div><div class="compute-subtitle">Task process group</div><div class="compute-table"><table><thead><tr><th>PID</th><th>Process</th><th>CPU</th><th>Memory</th><th>OS threads</th><th>State</th><th>Elapsed</th></tr></thead><tbody id="computeProcesses"><tr><td colspan="7">No active task process.</td></tr></tbody></table></div><div class="compute-subtitle">Highest CPU processes on this Mac</div><div id="computeTop" class="compute-note">Waiting for a system sample…</div><div id="computeNote" class="compute-note">ETA switches from the case benchmark to observed sequential-spot progress after warm-up.</div></div></details><div id="log"></div></div></div>
<div><div class="card"><div class="resultbar"><h2 style="margin:0">Run parameters</h2><span class="spacer"></span><button onclick="resetDefaults()">Reset defaults</button></div><div class="params">
<label>Histories</label><input id="histories" type="number" value="100000"><label>Threads</label><input id="threads" type="number" min="1" max="__MAX_THREADS__" value="12"><label>Random seed</label><input id="seed" type="number" value="1699"><label>Profile depth (mm)</label><input id="profile_depth" type="number" value="100"><label>Gamma DTA (mm)</label><input id="gamma_dta_mm" type="number" min="0.1" max="20" step="0.1" value="3"><label>Gamma DD (%)</label><input id="gamma_dd_percent" type="number" min="0.1" max="100" step="0.1" value="3"><label>Output tag</label><input id="output_tag" value="full_plan_100000"><label>TOPAS executable</label><input id="topas_executable" value="__TOPAS__"><label>MC dose source</label><div class="wide"><input id="mc_binary"><button onclick="useLatestMC()">Use latest</button></div>
<details class="beam-panel" id="beamSourcePanel" open><summary>Beam Energy + spot source</summary><div class="beam-source-options"><label class="beam-source-choice"><input type="radio" name="beamInputMode" value="rtplan" checked onchange="toggleBeamInputMode()"><span><strong>Use RTPLAN Energy + spots</strong><small>Preserve the selected RTPLAN layers, positions and delivery sequence.</small></span></label><label class="beam-source-choice"><input type="radio" name="beamInputMode" value="manual" onchange="toggleBeamInputMode()"><span><strong>Set Energy + one spot manually</strong><small>Create one research spot using the values below.</small></span></label></div><div class="beam-grid" style="margin-top:12px"><label>TOPAS beam model</label><select id="beamModelMode" onchange="toggleBeamModelMode()"><option value="baseline">RTPLAN baseline (uncommissioned)</option><option value="commissioned">Machine commissioned (IDD + emittance + VSAD)</option></select><label>Commissioned version</label><select id="beamModelProfile" onchange="beamModelProfileChanged()" disabled><option value="">Auto-select exact machine match</option></select><div id="beamModelHint" class="beam-warning">Commissioned mode requires an exact TreatmentMachineName match and compatible DICOM VSAD. It uses measured-IDD discrete spectra, Fermi-Eyges phase space and energy-dependent number-per-MU; baseline overrides are disabled.</div></div><div id="manualBeamFields" class="manual-beam-grid" hidden><label>Energy (MeV/u)</label><input id="manualEnergyMeVu" type="number" min="1" max="500" step="0.01" value="250"><label>Energy spread (%)</label><input id="manualEnergySpread" type="number" min="0" max="20" step="0.01" value="0"><label>Spot IEC X (mm)</label><input id="manualSpotX" type="number" min="-500" max="500" step="0.1" value="0"><label>Spot IEC Y (mm)</label><input id="manualSpotY" type="number" min="-500" max="500" step="0.1" value="0"><label>Spot FWHM X (mm)</label><input id="manualSpotFwhmX" type="number" min="0.01" max="200" step="0.01" value="8"><label>Spot FWHM Y (mm)</label><input id="manualSpotFwhmY" type="number" min="0.01" max="200" step="0.01" value="8"><div class="beam-warning">Manual mode creates one single-energy research spot and assigns all requested histories to it. It uses the current RTPLAN geometry/isocenter, but it is not a reconstruction of the TPS spot plan.</div></div></details>
<details class="beam-panel" id="beamOverridePanel"><summary><input id="beamOverrideEnabled" type="checkbox" onclick="event.stopPropagation();toggleBeamOverrides()"> Advanced RTPLAN beam overrides (research/commissioning only)</summary><div class="beam-grid"><label>Energy scale (%)</label><input id="beamEnergyScale" type="number" min="80" max="120" step="0.01" value="100"><label>Energy offset (MeV/u)</label><input id="beamEnergyOffset" type="number" min="-20" max="20" step="0.01" value="0"><label>Spot-size scale (%)</label><input id="beamSpotScale" type="number" min="25" max="400" step="0.1" value="100"><label>Energy spread (%)</label><input id="beamEnergySpread" type="number" min="0" max="20" step="0.01" value="0"><div class="beam-warning">Overrides apply only to RTPLAN mode, force regeneration/preflight, and are recorded in the plan audit. Use measured commissioning values only.</div></div></details><details class="beam-panel" id="energyLayerPanel"><summary id="energyLayerSummary">Energy layers — loading current RTPLAN…</summary><div class="energy-toolbar"><button onclick="selectAllEnergyLayers(true)">Select all</button><button onclick="selectAllEnergyLayers(false)">Clear</button><span id="energyLayerHint">Run stage 3 first</span></div><div id="energyLayerGrid" class="energy-layer-grid"></div><div class="beam-warning" style="margin-top:9px">Selecting fewer than all layers creates an energy-subset research run, not a reconstruction of the complete TPS plan. The selection is audited and forces stages 6–8 to rebuild.</div></details></div>
<div class="actions"><button id="runPipelineButton" class="primary" onclick="action('pipeline')">Run stages 1–7</button><button class="danger" onclick="stopTask()">Stop current task</button><button id="pauseTaskButton" class="pause" onclick="togglePauseTask()" disabled>Pause task</button><button class="log-button" onclick="focusWorkflowLog()">View live log</button></div><div id="runLockNote" class="run-lock-note" hidden>A calculation is running in the GUI server process. <b>Refreshing or closing this page does not interrupt it</b> — reopen <span id="runLockUrl"></span> at any time to reattach. The transport parameters above are frozen until the task finishes so a reloaded page cannot disagree with the run in flight. Only <b>Stop current task</b>, or quitting the terminal window that started the GUI, ends a calculation.</div><div class="progress-panel" aria-live="polite"><div class="progress-head"><span>Calculation progress</span><span id="progressText">Idle</span></div><div class="progress-track" role="progressbar" aria-label="Calculation progress" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0"><div id="progressFill" class="progress-fill"></div></div></div></div>
<div class="card" style="margin-top:18px"><h2>Case status <span id="busy" class="pill">Idle</span></h2><table><thead><tr><th>Stage</th><th>State</th><th>Detail</th></tr></thead><tbody id="status"></tbody></table></div></div>
</div></section>
<section id="queue" class="tab"><div class="card"><div class="resultbar"><div><h2 style="margin:0 0 3px">Batch calculation queue</h2><span class="cache-info">Each entry runs preparation stages 1–8 and TOPAS transport with an independent case folder, result cache and log.</span></div><span class="spacer"></span><span id="queueMode" class="pill">Stopped</span></div><div class="queue-toolbar"><button class="primary" onclick="addCurrentToQueue()">Add current case</button><button onclick="chooseQueueCaseFolder()">Choose / create case…</button><span class="spacer"></span><label>Local parallel jobs <select id="queueParallel" onchange="setQueueParallel()"><option value="1">1 job</option><option value="2">2 jobs</option></select></label><button class="resume" onclick="startQueue()">Start / auto-run</button><button class="pause" onclick="stopQueueScheduling()">Stop scheduling</button></div><div class="queue-summary"><span id="queueActive" class="pill">0 active</span><span id="queueWaiting" class="pill">0 waiting</span><span class="cache-info">Stopping scheduling never terminates an active calculation.</span></div><div id="queueIntake" class="queue-intake" hidden><div class="queue-intake-head"><div><strong>Import one batch case</strong><small id="queueIntakePath">No case folder selected.</small></div><button onclick="closeQueueIntake()">Close</button></div><div class="import-grid"><div class="import-tile" id="queue_tile_CT"><div><strong>CT</strong><small id="queue_count_CT">0 files</small></div><button data-import onclick="selectQueueDicom('CT')">Choose folder</button><input class="file-input" id="queue_file_CT" type="file" multiple webkitdirectory directory></div><div class="import-tile" id="queue_tile_RTPLAN"><div><strong>RTPLAN</strong><small id="queue_count_RTPLAN">0 files</small></div><button data-import onclick="selectQueueDicom('RTPLAN')">Choose file</button><input class="file-input" id="queue_file_RTPLAN" type="file"></div><div class="import-tile" id="queue_tile_RTDOSE"><div><strong>RTDOSE</strong><small id="queue_count_RTDOSE">0 files</small></div><button data-import onclick="selectQueueDicom('RTDOSE')">Choose files</button><input class="file-input" id="queue_file_RTDOSE" type="file" multiple></div><div class="import-tile" id="queue_tile_RTSTRUCT"><div><strong>RTSTRUCT</strong><small id="queue_count_RTSTRUCT">0 files</small></div><button data-import onclick="selectQueueDicom('RTSTRUCT')">Choose file</button><input class="file-input" id="queue_file_RTSTRUCT" type="file"></div></div><div class="queue-intake-note">Import the four DICOM categories independently. Existing files in the selected category can be replaced and are archived safely.</div><div class="queue-intake-actions"><button onclick="chooseQueueCaseFolder()">Change case folder</button><button id="queueAddCase" class="primary" onclick="addImportedCaseToQueue()" disabled>Add case to queue</button></div></div><div class="queue-table-wrap"><table><thead><tr><th>Case</th><th>Configuration</th><th>State</th><th>Progress</th><th>Elapsed / ETA</th><th>Controls</th></tr></thead><tbody id="queueJobs"><tr><td colspan="6" class="queue-empty">No cases in the queue.</td></tr></tbody></table></div><details id="queueLogPanel" class="queue-log-panel"><summary>Selected case log</summary><div id="queueLogCaption" class="queue-log-caption">Click View log on a queue entry.</div><pre id="queueLog" class="queue-log">No queue log selected.</pre></details></div></section>
<section id="machines" class="tab"><div class="machine-layout"><div class="card"><h2>Inspect a standard machine model package</h2><div class="machine-upload"><h3>ZIP package</h3><p>Inspection validates content in a temporary folder and does not install or activate anything.</p><button class="primary" onclick="el('machinePackageFile').click()">Choose package</button><input id="machinePackageFile" class="file-input" type="file" accept=".zip,application/zip"></div><div id="machineInspectionEmpty" class="cache-info" style="margin-top:12px">Choose a package to check schema, all SHA-256 values, units, provenance, approval, and current RTPLAN compatibility.</div><div id="machineInspection" hidden><div class="machine-summary"><span id="machineInspectPass" class="pill">0 PASS</span><span id="machineInspectWarn" class="pill">0 WARN</span><span id="machineInspectBlock" class="pill">0 BLOCK</span></div><div id="machineInspectTitle" class="cache-info"></div><div id="machineFindings" class="finding-list" style="margin-top:10px"></div><div class="resultbar" style="margin-top:13px;margin-bottom:0"><span id="machineImportPolicy" class="cache-info"></span><span class="spacer"></span><button id="machineImportButton" class="primary" onclick="importInspectedMachineModel()" disabled>Import model</button></div></div></div><div class="card"><div class="resultbar"><div><h2 style="margin:0 0 3px">Installed models and independent assets</h2><span id="machineRtplan" class="cache-info">Loading current RTPLAN machine…</span></div><span class="spacer"></span><button onclick="refreshMachineModels()">Refresh</button></div><div class="machine-table-wrap"><table><thead><tr><th>Model / asset</th><th>Kind</th><th>Version</th><th>Status</th><th>History</th><th>Action</th></tr></thead><tbody id="machineModels"><tr><td colspan="6" class="queue-empty">No registered models.</td></tr></tbody></table></div><div class="asset-note"><div><strong>CT calibration</strong><small>Scanner/protocol HU–material/RSP data are versioned independently from the beam.</small></div><div><strong>Nozzle / MRF</strong><small>Geometry and WET evidence are versioned independently from the incident source model.</small></div><div><strong>Absolute output</strong><small>Output correction and measurement evidence are independent assets. Registration alone never applies a factor.</small></div></div><div class="notice" style="margin:14px 0 0"><b>Safety policy:</b> model content is immutable. Historical references prevent deletion; this interface only deactivates or reactivates registry entries. Import/status changes are blocked while any local calculation is running or paused.</div></div></div></section>
<section id="ssh" class="tab"><div class="ssh-layout"><div><div class="card"><div class="resultbar"><div><h2 style="margin:0 0 3px">SSH calculation server</h2><span class="cache-info">Enter your server details, verify its identity, then test the commissioned runtime.</span></div><span class="spacer"></span><span id="sshConfigured" class="pill">Loading</span></div><div class="ssh-form"><label for="sshEnabled">Remote actions</label><label class="ssh-check"><input id="sshEnabled" type="checkbox"> Enable this server after commissioning</label><label for="sshServerId">Server ID</label><input id="sshServerId" value="topas-server-01" placeholder="topas-server-01"><label for="sshMode">Connection method</label><select id="sshMode" onchange="sshModeChanged()"><option value="direct">Direct hostname / IP</option><option value="alias">OpenSSH config alias</option></select><label id="sshHostLabel" for="sshHost">Hostname / IP</label><input id="sshHost" placeholder="topas.example.org"><label for="sshUser">SSH username</label><input id="sshUser" placeholder="researcher"><label for="sshPort">SSH port</label><input id="sshPort" type="number" min="1" max="65535" value="22"><label for="sshAuthMode">Authentication</label><select id="sshAuthMode" onchange="sshAuthChanged()"><option value="agent">SSH agent / macOS Keychain</option><option value="identity_file">Existing private-key file</option></select><label for="sshIdentity">Identity file</label><div class="ssh-inline"><input id="sshIdentity" readonly placeholder="Not needed for agent / Keychain"><button id="sshChooseIdentity" onclick="chooseSshIdentity()">Choose…</button></div><label for="sshRemoteRoot">Remote job root</label><input id="sshRemoteRoot" value="/srv/plan1699"><label for="sshTopasExecutable">Server TOPAS</label><input id="sshTopasExecutable" value="/opt/topas/bin/topas"><label for="sshGeant4Setup">Geant4 setup script</label><input id="sshGeant4Setup" value="/opt/geant4/bin/geant4.sh"><label for="sshGeant4Data">Geant4 data root</label><input id="sshGeant4Data" value="/opt/geant4/data"><label for="sshMaxParallel">Remote parallel jobs</label><input id="sshMaxParallel" type="number" min="1" max="32" value="1"></div><div class="ssh-actions"><button class="primary" onclick="saveSshServer()">Save settings</button><button onclick="refreshSshServer()">Reload</button><button id="sshInspectButton" onclick="inspectSshHostKey()">Inspect host key</button><button id="sshTestButton" onclick="testSshServer()">Test connection</button><button id="sshEnvironmentButton" onclick="checkSshEnvironment()">Check TOPAS + Geant4</button></div><div id="sshHostKeys"><div class="ssh-empty">Save connection settings, then inspect the server host key.</div></div><div id="sshFindings" class="finding-list"><div class="ssh-empty">Loading server checks…</div></div><div class="ssh-warning"><b>Patient-data boundary:</b> remote transport uploads CT DICOM and generated TOPAS parameters. Obtain institutional approval before transfer. RTPLAN, RTDOSE and RTSTRUCT are not needed by the server transport bundle.</div></div><div class="card" style="margin-top:18px"><h2>Security and runtime policy</h2><div id="sshPolicy" class="ssh-policy"><div><strong>No stored secrets</strong><small>Passwords and private-key contents never enter this project.</small></div><div><strong>Pinned server identity</strong><small>Strict OpenSSH host-key checking is mandatory.</small></div><div><strong>Server runtime only</strong><small>TOPAS and Geant4 are executed from commissioned server paths.</small></div></div></div></div><div class="card"><div class="resultbar"><div><h2 style="margin:0 0 3px">Current-case remote transport</h2><span class="cache-info">Build an immutable, patient/run-separated bundle after stages 1–7.</span></div><span class="spacer"></span><button id="sshBundleButton" class="primary" onclick="prepareRemoteBundle()">Prepare bundle</button></div><div class="notice"><b>Commissioning sequence:</b> Save settings → inspect and independently verify the SHA-256 host fingerprint → trust key → test connection → check server TOPAS and Geant4 → prepare bundle.</div><div id="sshBundles"><div class="ssh-empty">No remote bundle has been prepared for this case.</div></div><div class="ssh-policy" style="margin-top:14px"><div><strong>01 Upload</strong><small>Content-addressed CT cache plus generated TOPAS parameter tree; no executable is copied.</small></div><div><strong>02 Submit</strong><small>Sources the selected server Geant4 setup script and starts the selected server TOPAS executable with nohup.</small></div><div><strong>03 Status</strong><small>Reads persistent server state and the latest TOPAS log without depending on this browser session.</small></div><div><strong>04 Download</strong><small>Returns dose output, server runtime audit and logs to the patient/run bundle directory.</small></div></div><p class="cache-info" style="margin:14px 0 0">Saving settings, checking a key, or opening this page does not upload patient data or start a calculation. Upload and submission remain explicit script operations. Existing bundles retain the settings recorded when they were created.</p></div></div></section>
<section id="results" class="tab"><div class="card"><div class="resultbar"><b>Result view</b><select id="resultMode" onchange="switchResultMode()"><option value="line-dose">Interactive line dose</option><option value="reports">Report images and Gamma</option></select><label class="tps-dose-picker">TPS RTDOSE <select id="tpsDose" onchange="changeTpsDose()"><option value="">Loading current RTDOSE files…</option></select></label><span class="spacer"></span><span id="caseResultIdentity" class="cache-info"></span><select id="cacheRun" class="cache-select" aria-label="Cached result run" onchange="updateCacheDeleteButton()"><option value="">Current settings</option></select><button onclick="loadCachedRun()">Load cached</button><button id="deleteCachedRunButton" class="danger" onclick="deleteCachedRun()" disabled>Delete cached</button><button onclick="action('open_output')">Open output folder</button></div>
<div id="lineDoseResults"><div class="line-toolbar"><div class="line-control"><label>Plane</label><select id="linePlane" onchange="changeLineDosePlane()"><option value="axial">Axial</option><option value="coronal">Coronal</option><option value="sagittal">Sagittal</option></select></div><div class="line-control"><label id="lineSliceLabel">Slice</label><input id="lineSlice" type="range" min="0" max="0" value="0"></div><button class="iso-button" onclick="goToIsocenter()">Go to isocenter</button><div class="line-control"><label>Image layer</label><select id="lineImageLayer" onchange="renderDoseFrame()"><option value="tps">TPS</option><option value="mc">MC</option><option value="difference">MC − TPS difference</option></select></div><div class="line-control"><label>Curve normalization</label><select id="lineNormalization" onchange="sampleLineDose()"><option value="absolute">Particle-calibrated Gy</option><option value="independent">Independent global max (%)</option><option value="peak_scaled">Legacy TPS-peak fit (diagnostic)</option></select></div><div class="line-control"><label>Samples</label><input id="lineSamples" type="number" min="2" max="5000" value="512" style="width:92px"></div><button onclick="initializeLineDose(true)">Reload dose</button><button id="lineExport" class="primary" onclick="exportLineDose()" disabled>Export line CSV</button><button onclick="el('mcDicomFile').click()">Import MC RTDOSE</button><input class="file-input" id="mcDicomFile" type="file" accept=".dcm,application/dicom"><div class="qa-export"><div class="line-control"><label>MC DICOM mode</label><select id="mcDicomMode"><option value="particle_calibrated">Particle-calibrated QA Gy</option><option value="peak_scaled">Legacy TPS-peak fit</option><option value="raw">Raw TOPAS per-run Gy</option></select></div><button id="mcDicomExport" onclick="exportMcDicom()">Export MC RTDOSE</button></div></div><div id="lineDoseStatus" class="line-status">Open Results to load the current TPS and TOPAS dose grids.</div><div class="line-dose-grid"><div class="dose-canvas-wrap" id="doseCanvasWrap"><canvas id="doseCanvas" aria-label="Interactive TPS and MC dose plane with isocenter crosshair"></canvas><div class="dose-help">The gold crosshair intersects at RTPLAN isocenter. Drag A→B to define a line.</div><div id="doseReadout" class="dose-readout">No dose plane loaded.</div></div><div class="line-chart-panel"><div id="lineCaption" class="line-caption">Draw a line to calculate TPS/MC dose.</div><div class="line-legend"><span><i class="line-key" style="background:#2563eb"></i>TPS</span><span><i class="line-key" style="background:#e35d3f"></i>MC</span><span><i class="line-key" style="background:#ffd34d"></i>Isocenter axes</span></div><div class="line-chart-wrap"><canvas id="lineDoseChart" aria-label="TPS and MC line-dose chart"></canvas></div><div id="lineDoseHover" class="line-hover">Move over the chart for exact values.</div><div id="lineStats" class="line-stats"><div class="line-stat"><strong>No line dose yet</strong><span>Drag across the dose plane.</span></div></div></div></div></div>
<div id="reportResults" hidden><div class="report-controls"><select id="direction"><option value="depth_direction">Depth</option><option value="transverse_x">Transverse X</option><option value="transverse_y">Transverse Y</option><option value="gamma_map">Gamma Map</option><option value="gamma_pass_fail">Gamma Pass/Fail</option></select><button onclick="refreshResults()">Refresh report</button></div><div class="resultgrid"><div class="imagebox"><img id="resultImage" alt="No current result image"></div><div><h2>Latest analysis summary</h2><div id="gammaHeadline" class="gamma-rate">No Gamma result</div><div id="gammaProtocol" class="gamma-meta">Run Gamma analysis to show the pass rate.</div><div id="summary" class="summary" style="margin-top:12px">No analysis summary yet.</div></div></div></div></div></section>
<section id="guide" class="tab"><div class="card guide"><h2>Operating guide</h2>
<h3>New TPS plan</h3><ol><li>Click <b>Choose / create case</b> and select an empty case folder.</li><li>Use the four DICOM buttons to import CT, RTPLAN, RTDOSE and RTSTRUCT separately. A confirmed different patient/plan caches the previous settings and production result, archives replaced DICOM, and resets the GUI parameters.</li><li>Run stages 1–7, review every warning (especially HU calibration and range-modulator warnings), then run the zero-history TOPAS preflight.</li><li>Click <b>Run TOPAS</b>, choose a histories/threads plan and review its estimated time range. Changed parameters automatically rebuild stages 6–8 before transport; an existing production dose is archived instead of overwritten.</li><li>Open <b>Results</b>, choose a verified TPS RTDOSE, select a plane/slice and drag A→B to compare TPS/MC and export CSV. Static reports and Gamma remain under <b>Report images and Gamma</b>.</li></ol>
<h3>TPS RTDOSE selection</h3><p>The Results selector lists every GY/CGY RTDOSE that belongs to the active patient, study, frame and RTPLAN, including its PLAN/BEAM summation and PHYSICAL/EFFECTIVE type. PLAN / PHYSICAL is selected by default. Line dose updates immediately; profile and Gamma reports must be regenerated. Comparing TOPAS physical dose with EFFECTIVE or BEAM dose is diagnostic/research use only and is explicitly marked in the confirmation and report.</p>
<h3>Beam model and overrides</h3><p><b>Machine commissioned</b> follows the imported TOPAS_Test method: measured water IDDs provide a discrete incident-energy spectrum, measured spot sigma versus depth provides a Fermi-Eyges BiGaussian emittance, DICOM VSAD defines each spot axis, and number-per-MU corrects relative layer fluence. The profile is accepted only for an exact machine-name match and compatible VSAD, and its provenance/hash are written to the audit. <b>RTPLAN baseline</b> remains available for A/B comparison. Advanced overrides apply only to baseline mode.</p>
<h3>Machine model packages</h3><p>Open <b>Machine models</b>, choose a standard ZIP and review every PASS/WARN/BLOCK finding before importing. Imported versions are immutable. One exact active RTPLAN-machine match can be selected automatically; multiple versions require an explicit <b>Commissioned version</b>. Historical versions are never deleted and may only be deactivated. CT calibration, nozzle/MRF geometry/WET and absolute-output evidence are registered as independent asset kinds and are not applied merely because they were imported.</p>
<h3>Energy-layer selection</h3><p>After stage 3, expand <b>Energy layers</b> and click individual RTPLAN energies or use <b>Select all/Clear</b>. All layers reconstruct the complete plan. A subset is an energy-specific research run, is recorded in the plan audit, and must not be interpreted as complete-plan TPS validation.</p>
<h3>Patient model</h3><p>Uniform artificial 0-HU CT with a rectangular External is reconstructed as a G4_WATER box. Supported axial clinical CT is reconstructed as TsDicomPatient using the project Schneider table. The included table is a generic TOPAS reference and is not an institution/scanner-specific calibration.</p>
<h3>Runtime estimate</h3><p>The selector offers Quick, Balanced, Recommended, Higher-statistics and Custom plans. It prefers completed logs from the current case and matching beam model, and accounts for spot count, histories per spot and observed CPU-core utilization. During TOPAS transport the estimate is replaced by a live ETA derived from sequential-spot progress. A wide range is still shown when no comparable completed run exists.</p>
<h3>Batch queue</h3><p>Use <b>Choose / create case</b> to select one batch-case folder without changing the Workflow case, then import CT, RTPLAN, RTDOSE and RTSTRUCT independently inside the Batch queue. Add the completed case and repeat for any number of plans. Each queue entry snapshots the current histories, threads, seed and beam settings; imported batch cases use all energy layers from their own RTPLAN. Set one or two local jobs, then click <b>Start / auto-run</b>. Completed jobs automatically release a slot for the next waiting case. <b>Stop scheduling</b> leaves active processes untouched. Pause/resume, cancel and retry are independent for every case; queue metadata persists across GUI restarts, while an unexpectedly interrupted process is never auto-resumed without a manual retry.</p>
<h3>User-configured SSH calculation server</h3><p>The <b>SSH server</b> page accepts a direct hostname/IP or an OpenSSH alias plus an SSH username and port. Authentication stays in OpenSSH agent/Keychain or uses the path of an existing private key; passwords and private-key contents are never stored. Before connection, inspect the presented host key, independently verify its SHA-256 fingerprint, and explicitly pin it in the project <code>known_hosts</code>. Remote commands remain application-defined. <b>Prepare bundle</b> creates an immutable bundle under the current patient/plan/run cache. Its scripts upload CT to a content-addressed server cache, rewrite only the staged TOPAS DICOM path, source the selected server Geant4 environment, and invoke the selected server TOPAS executable. Local TOPAS/Geant4 binaries, RTPLAN, RTDOSE and RTSTRUCT are not uploaded. Upload/submission are explicit and require institutional approval for patient CT transfer.</p>
<h3>Line-dose protocol</h3><p>The gold center-line crosshair is the RTPLAN isocenter projected onto the selected plane; <b>Go to isocenter</b> restores the nearest slice. The viewer samples patient XYZ coordinates on the selected regular RTDOSE grid with trilinear interpolation. For a commissioned run, the default Gy curves use the independent particle-number scale N<sub>plan</sub>/N<sub>sim</sub>. Independent-maximum and legacy TPS-peak-fit views remain explicitly labelled diagnostics.</p>
<h3>MC RTDOSE and cached results</h3><p><b>Export MC RTDOSE</b> creates an audited derived DICOM. Particle-calibrated QA Gy is the default; raw per-run and legacy TPS-peak-fit modes are diagnostic. Its scoring grid remains the PLAN / PHYSICAL RTDOSE, independently of the display selector. Patient, Study, Frame and RTPLAN references are validated before download. Results are isolated by patient, study, RTPLAN and output tag, with the exact particle-allocation snapshot cached under <b>topas_runs</b>. <b>Delete cached</b> removes only the selected standardized result from the Results list by moving it to the case's recoverable <code>analysis/_trash</code>; it is blocked while that case is queued or calculating.</p>
<h3>Gamma protocol</h3><p>The selected TPS RTDOSE is the reference and TOPAS DoseToMedium is the evaluation; PLAN / PHYSICAL remains the standard default. Gamma is global 3D, uses a fixed 10% threshold of the selected TPS maximum, trilinear MC interpolation and γ ≤ 1 as passing. MC is scaled only by commissioned N<sub>plan</sub>/N<sub>sim</sub>; TPS dose is not used to fit MC output and no empirical 0.976 correction is applied. Every pass rate must be reported with RTDOSE, DD, DTA, threshold and normalization.</p>
<h3>Interpretation limits</h3><p>Particle-number calibration removes normalization circularity but does not make this a clinical calculation. The result remains a research physical-dose estimate and depends on beam commissioning, material/HU calibration, MRF geometry/WET and Monte Carlo statistical uncertainty. RBE dose and clinical acceptance criteria are outside this workflow.</p>
<h3>Priority improvements</h3><p><b>P0:</b> commission MRF4, the beam model and scanner-specific HU conversion. <b>P1:</b> add measurements, uncertainty and acceptance metrics. <b>P2:</b> implement arbitrary IEC geometry and multi-beam summation.</p><p>Full Chinese documentation: <code>WORKFLOW.md</code></p></div></section>
</main><div id="runModal" class="modal-backdrop"><div class="modal"><h2>Select TOPAS calculation plan</h2><p class="modal-subtitle">Estimates prefer matching completed runs from this case and beam model. They account for sequential spot count and measured CPU utilization; the live ETA is refined after transport starts.</p><div id="runOptions" class="run-options"></div><label class="custom-run"><input id="runCustomRadio" type="radio" name="runPlan" value="custom"><span><strong>Custom</strong><small>Enter histories and threads</small></span><input id="runCustomHistories" type="number" min="1" value="100000"><input id="runCustomThreads" type="number" min="1" max="__MAX_THREADS__" value="12"><span id="runCustomEstimate" class="estimate"></span></label><div class="modal-note">Changing histories, threads or seed automatically rebuilds stages 6–8 before particle transport. Requested threads are not assumed to be fully occupied: plans with only a few histories per sequential spot can leave most threads idle. This Mac has <b>__MAX_THREADS__ logical CPUs</b> and that is the hard ceiling: more Geant4 workers than cores only adds kernel context switching, which measured 1.4–2.1× longer wall time and 15× more system time on this machine.</div><div class="modal-actions"><button onclick="closeRunModal()">Cancel</button><button class="primary" onclick="confirmRunPlan()">Use selected plan</button></div></div></div><div id="toast"></div>
<script>
const MAX_THREADS=Math.max(1,Number('__MAX_THREADS__')||1);
const steps=[['1','DICOM geometry check','References, grid and isocenter','geometry'],['2','Compatibility gate','Select water-box or DICOM CT patient model','compatibility'],['3','Parse RT Ion Plan','Energy layers, spots and weights','parse'],['4','Generate case geometry','Patient model and beam transform','case_geometry'],['5','Build TPS dose grid','Exact RPPD-aligned scorer','scoring'],['6','Generate full spot plan','Allocate Monte Carlo histories','full_plan'],['7','Prepare TOPAS run','Entry point, threads and safe output','prepare'],['8','TOPAS preflight','Zero-history parse and grid test','preflight'],['9','Run TOPAS','Long calculation with confirmation','run_topas'],['10','Export profiles','English plots and CSV','analyze'],['11','Gamma analysis','User-defined DTA and DD; output pass rate','gamma']];
let cursor=0, lastDone=null, taskPaused=false, queueSelectedJob='', queueLogCursor=0, queueRefreshBusy=false, queueIntakeRoot='', machineInspectionToken='';
// Refreshing this page never touches a running calculation: the task lives in
// the server process, not in the browser. These two flags only control whether
// a freshly loaded page reattaches its form to a task that was already running.
let reattached=false, submittedHere=false, pendingLayerSelection='';
const machineModelState={explicitSelectionRequired:false,rtplanMachine:'',models:[]};
const queueIntakeCounts={CT:0,RTPLAN:0,RTDOSE:0,RTSTRUCT:0};
function el(id){return document.getElementById(id)}
const defaultTopasExecutable=el('topas_executable').value;
el('steps').innerHTML=steps.map(s=>`<div class="step"><span class="number">${s[0]}</span><span><strong>${s[1]}</strong><small>${s[2]}</small></span><button onclick="action('${s[3]}')">Run</button></div>`).join('');
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>showTab(b.dataset.tab));el('direction').onchange=refreshResults;
function showTab(id){document.querySelectorAll('.tab').forEach(e=>e.classList.toggle('active',e.id===id));document.querySelectorAll('nav button').forEach(e=>e.classList.toggle('active',e.dataset.tab===id));if(id==='results'&&el('resultMode').value==='line-dose')initializeLineDose(false);if(id==='queue')refreshQueue();if(id==='machines')refreshMachineModels();if(id==='ssh')refreshSshServer()}
function focusWorkflowLog(){showTab('workflow');el('workflowLog').scrollIntoView({behavior:'smooth',block:'start'})}
function beamInputMode(){return document.querySelector('input[name="beamInputMode"]:checked')?.value||'rtplan'}
function beamModelMode(){return el('beamModelMode').value||'baseline'}
function values(){return {root:el('root').value,histories:el('histories').value,threads:el('threads').value,seed:el('seed').value,profile_depth:el('profile_depth').value,gamma_dta_mm:el('gamma_dta_mm').value,gamma_dd_percent:el('gamma_dd_percent').value,output_tag:el('output_tag').value,topas_executable:el('topas_executable').value,mc_binary:el('mc_binary').value,tps_dose_uid:el('tpsDose').value,beam_input_mode:beamInputMode(),beam_model_mode:beamModelMode(),beam_model_profile:el('beamModelProfile').value,beam_override_enabled:el('beamOverrideEnabled').checked,beam_energy_scale_percent:el('beamEnergyScale').value,beam_energy_offset_mevu:el('beamEnergyOffset').value,beam_spot_scale_percent:el('beamSpotScale').value,beam_energy_spread_percent:el('beamEnergySpread').value,manual_energy_mevu:el('manualEnergyMeVu').value,manual_energy_spread_percent:el('manualEnergySpread').value,manual_spot_x_mm:el('manualSpotX').value,manual_spot_y_mm:el('manualSpotY').value,manual_spot_fwhm_x_mm:el('manualSpotFwhmX').value,manual_spot_fwhm_y_mm:el('manualSpotFwhmY').value,energy_layer_indices:selectedEnergyLayerValue()}}
function toggleBeamOverrides(){const enabled=beamInputMode()==='rtplan'&&beamModelMode()==='baseline'&&el('beamOverrideEnabled').checked;for(const id of ['beamEnergyScale','beamEnergyOffset','beamSpotScale','beamEnergySpread'])el(id).disabled=!enabled}
function applyDerivedFieldStates(){const commissioned=beamModelMode()==='commissioned';el('beamModelProfile').disabled=!commissioned;for(const id of ['manualEnergySpread','manualSpotFwhmX','manualSpotFwhmY'])el(id).disabled=commissioned;toggleBeamOverrides()}
function toggleBeamModelMode(){const commissioned=beamModelMode()==='commissioned';if(commissioned){el('beamOverrideEnabled').checked=false;el('manualEnergySpread').value='0'}el('beamOverridePanel').hidden=commissioned||beamInputMode()==='manual';applyDerivedFieldStates();const tag=el('output_tag').value;if(commissioned&&/^full_plan_\d+$/.test(tag))el('output_tag').value=tag+'_commissioned';if(!commissioned&&/^full_plan_\d+_commissioned$/.test(tag))el('output_tag').value=tag.replace('_commissioned','');updateBeamModelHint()}
function beamModelProfileChanged(){updateBeamModelHint()}
function commissionedSelectionError(){if(beamModelMode()!=='commissioned')return'';if(!machineModelState.models.length)return`No active commissioned model matches RTPLAN machine ${machineModelState.rtplanMachine||'<unknown>'}`;if(machineModelState.explicitSelectionRequired&&!el('beamModelProfile').value)return'Multiple active versions match this RTPLAN machine; select one commissioned version explicitly';return''}
function updateBeamModelHint(){const error=commissionedSelectionError(),selected=el('beamModelProfile').selectedOptions[0];if(beamModelMode()==='commissioned')el('beamModelHint').textContent=error||`Selected model: ${selected?.textContent||'automatic exact match'}. Machine name, VSAD, energy coverage, hashes and calibration binding are revalidated during generation.`;else el('beamModelHint').textContent='Baseline mode uses RTPLAN Energy and spot geometry without commissioned IDD spectrum, phase space or number-per-MU.'}
function toggleBeamInputMode(){const manual=beamInputMode()==='manual';el('manualBeamFields').hidden=!manual;el('beamOverridePanel').hidden=manual||beamModelMode()==='commissioned';el('energyLayerPanel').hidden=manual;if(manual)el('beamOverrideEnabled').checked=false;const tag=el('output_tag').value,h=el('histories').value;if(manual&&/^full_plan_\d+$/.test(tag))el('output_tag').value=`manual_spot_${h}`;if(!manual&&/^manual_spot_\d+$/.test(tag))el('output_tag').value=`full_plan_${h}`;toggleBeamModelMode()}
function manualBeamError(){const e=+el('manualEnergyMeVu').value,s=+el('manualEnergySpread').value,x=+el('manualSpotX').value,y=+el('manualSpotY').value,fx=+el('manualSpotFwhmX').value,fy=+el('manualSpotFwhmY').value;if(![e,s,x,y,fx,fy].every(Number.isFinite))return'Manual Energy and spot values must be numeric';if(e<1||e>500)return'Manual Energy must be within 1–500 MeV/u';if(Math.abs(x)>500||Math.abs(y)>500)return'Manual spot X/Y must be within −500 to +500 mm';if(fx<.01||fx>200||fy<.01||fy>200)return'Manual spot FWHM X/Y must be within 0.01–200 mm';if(s<0||s>20)return'Manual Energy spread must be within 0–20%';return''}
toggleBeamInputMode();
const energyLayerState={signature:'',ready:false};
function energyLayerBoxes(){return [...document.querySelectorAll('#energyLayerGrid input[type="checkbox"]')]}
function selectedEnergyLayerValue(){if(beamInputMode()==='manual')return'all';const boxes=energyLayerBoxes();if(!energyLayerState.ready||!boxes.length)return'all';const selected=boxes.filter(box=>box.checked).map(box=>box.value);if(!selected.length)return'none';return selected.length===boxes.length?'all':selected.join(',')}
function updateEnergyLayerSummary(){const boxes=energyLayerBoxes(),selected=boxes.filter(box=>box.checked);if(!energyLayerState.ready||!boxes.length){el('energyLayerSummary').textContent='Energy layers — run stage 3 for the current RTPLAN';return}const energies=selected.map(box=>Number(box.dataset.energy));let detail=selected.length+' / '+boxes.length+' selected';if(energies.length)detail+=' · '+Math.min(...energies).toFixed(2)+'–'+Math.max(...energies).toFixed(2)+' MeV/u';el('energyLayerSummary').textContent='Energy layers — '+detail;el('energyLayerHint').textContent=selected.length===boxes.length?'Complete TPS plan':selected.length?'Subset: '+selected.length+' layers':'No layer selected'}
function selectAllEnergyLayers(checked){for(const box of energyLayerBoxes())box.checked=checked;updateEnergyLayerSummary()}
async function refreshEnergyLayers(force=false){try{const j=await request('/api/energy-layers?root='+encodeURIComponent(el('root').value));if(!j.ready){energyLayerState.ready=false;energyLayerState.signature='';el('energyLayerGrid').innerHTML='';el('energyLayerHint').textContent=j.message||'Run stage 3 first';updateEnergyLayerSummary();return}if(!force&&energyLayerState.ready&&energyLayerState.signature===j.signature){updateEnergyLayerSummary();return}energyLayerState.ready=true;energyLayerState.signature=j.signature;el('energyLayerGrid').innerHTML=j.layers.map(layer=>'<label class="energy-layer"><input type="checkbox" value="'+layer.layerIndex+'" data-energy="'+layer.energyMeVu+'" checked onchange="updateEnergyLayerSummary()"><span><strong>'+Number(layer.energyMeVu).toFixed(2)+' MeV/u</strong><small>Layer '+layer.layerIndex+' · '+Number(layer.spots).toLocaleString()+' spots</small></span></label>').join('');applyLayerSelection(pendingLayerSelection);updateEnergyLayerSummary();if(reattached)setRunLock(true)}catch(e){energyLayerState.ready=false;el('energyLayerHint').textContent=e.message;updateEnergyLayerSummary()}}

function renderMachineInspection(j){machineInspectionToken=j.token||'';el('machineInspectionEmpty').hidden=true;el('machineInspection').hidden=false;el('machineInspectPass').textContent=`${j.summary.pass} PASS`;el('machineInspectWarn').textContent=`${j.summary.warn} WARN`;el('machineInspectBlock').textContent=`${j.summary.block} BLOCK`;el('machineInspectTitle').textContent=`${j.originalName} · ${j.kindLabel} · ${j.identifier||'<missing ID>'} · version ${j.version||'<missing>'} · package SHA-256 ${j.packageFingerprint}`;el('machineFindings').innerHTML=j.report.map(row=>`<div class="finding"><span class="finding-level ${row.level.toLowerCase()}">${esc(row.level)}</span><strong>${esc(row.check)}</strong><span>${esc(row.detail)}</span></div>`).join('');el('machineImportButton').disabled=!j.importAllowed;el('machineImportPolicy').textContent=j.importAllowed?'Inspection passed. Import still requires explicit confirmation and no active calculation.':'Resolve every BLOCK finding and inspect a new package.'}
async function inspectMachinePackage(file){if(!file)return;machineInspectionToken='';el('machineInspectionEmpty').hidden=false;el('machineInspectionEmpty').textContent='Uploading and inspecting package…';el('machineInspection').hidden=true;try{const response=await fetch(`/api/machine-models/inspect?root=${encodeURIComponent(el('root').value)}&name=${encodeURIComponent(file.name)}`,{method:'POST',headers:{'Content-Type':'application/zip'},body:file});const j=await response.json();if(!response.ok)throw new Error(j.error||response.statusText);renderMachineInspection(j);toast(j.importAllowed?'Package inspection passed':'Package inspection found blocking issues',!j.importAllowed)}catch(e){el('machineInspectionEmpty').textContent='Inspection failed: '+e.message;toast(e.message,true)}finally{el('machinePackageFile').value=''}}
el('machinePackageFile').onchange=event=>inspectMachinePackage(event.target.files[0]);
async function importInspectedMachineModel(){if(!machineInspectionToken){toast('Inspect a package first',true);return}if(!confirm('Import this inspected package as an immutable version?\n\nThe application will recheck every hash and RTPLAN compatibility. Existing content is never overwritten. A running or paused calculation blocks this change.'))return;try{const j=await request('/api/machine-models/import',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:machineInspectionToken})});machineInspectionToken='';el('machineImportButton').disabled=true;el('machineImportPolicy').textContent=j.message;await refreshMachineModels();toast(j.message)}catch(e){toast(e.message,true)}}
function machineRow(model){const status=model.active?'Active':'Inactive',history=model.referenceCount?`${model.referenceCount} cached run reference${model.referenceCount===1?'':'s'}`:'No cached reference found',fingerprint=model.modelFingerprint||model.packageFingerprint||'',action=model.active?`<button class="pause" onclick="setMachineActive('${esc(model.id)}',false,${model.referenceCount})">Deactivate</button>`:`<button class="resume" onclick="setMachineActive('${esc(model.id)}',true,${model.referenceCount})">Reactivate</button>`;return `<tr><td class="machine-id"><strong>${esc(model.identifier||'<missing>')}</strong><small>${esc(fingerprint?fingerprint.slice(0,20)+'…':'legacy, no package fingerprint')}</small></td><td>${esc(model.kindLabel)}</td><td>${esc(model.version)}</td><td class="machine-status ${model.active?'active':'inactive'}">${status}${model.legacy?' · legacy':''}</td><td>${esc(history)}</td><td>${action}</td></tr>`}
async function setMachineActive(modelId,active,referenceCount){const verb=active?'reactivate':'deactivate',history=referenceCount?`\n\nThis immutable model is referenced by ${referenceCount} cached run(s). Their files and audit remain unchanged.`:'';if(!confirm(`${verb[0].toUpperCase()+verb.slice(1)} this model version?${history}\n\nNo model files will be deleted. The change is blocked while a calculation is running or paused.`))return;try{const j=await request('/api/machine-models/status',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({root:el('root').value,model_id:modelId,active})});await refreshMachineModels();toast(j.message)}catch(e){toast(e.message,true)}}
async function refreshMachineModels(){const selector=el('beamModelProfile'),current=selector.value;try{const j=await request('/api/machine-models?root='+encodeURIComponent(el('root').value));machineModelState.rtplanMachine=j.rtplan.machineName||'';machineModelState.explicitSelectionRequired=!!j.explicitSelectionRequired;machineModelState.models=j.compatibleBeamProfiles||[];el('machineRtplan').textContent=j.rtplan.available?`Current RTPLAN: ${j.rtplan.machineName} · ${j.rtplan.energiesMeVu.length} energies · VSAD ${j.rtplan.vsadMm.join(' / ')} mm`:'Current RTPLAN unavailable: '+j.rtplan.detail;el('machineModels').innerHTML=j.models.length?j.models.map(machineRow).join(''):'<tr><td colspan="6" class="queue-empty">No registered or valid legacy models.</td></tr>';const autoLabel=j.explicitSelectionRequired?'Select a version — multiple matches':'Auto-select exact machine match';selector.innerHTML=`<option value="">${autoLabel}</option>`+machineModelState.models.map(model=>`<option value="${esc(model.profile)}">${esc(model.machineName)} · ${esc(model.version)}${model.legacy?' · legacy':''} · ${esc((model.modelFingerprint||'').slice(0,12))}</option>`).join('');if(current&&machineModelState.models.some(model=>model.profile===current))selector.value=current;else selector.value='';updateBeamModelHint();return j}catch(e){machineModelState.models=[];machineModelState.explicitSelectionRequired=false;selector.innerHTML='<option value="">Machine model registry unavailable</option>';el('machineRtplan').textContent=e.message;el('machineModels').innerHTML='<tr><td colspan="6" class="queue-empty">'+esc(e.message)+'</td></tr>';updateBeamModelHint();return null}}

function renderSshFindings(rows){el('sshFindings').innerHTML=rows?.length?rows.map(row=>`<div class="finding"><span class="finding-level ${String(row.level).toLowerCase()}">${esc(row.level)}</span><strong>${esc(row.check)}</strong><span>${esc(row.detail)}</span></div>`).join(''):'<div class="ssh-empty">No server checks returned.</div>'}
function renderSshBundles(bundles){el('sshBundles').innerHTML=bundles?.length?bundles.map((bundle,index)=>`<div class="ssh-bundle"><strong>${index===0?'Latest · ':''}${esc(bundle.jobId||'Unknown job')}</strong><small>${esc(bundle.createdUtc||'')} · ${esc(bundle.outputTag||'')} · CT ${formatBytes(bundle.ctBytes)} · TOPAS parameters ${formatBytes(bundle.topasBytes)}</small><small>Server: ${esc(bundle.serverId||'')} · ${esc(bundle.remoteJobDirectory||'')}</small><small>Local audit and scripts: ${esc(bundle.path||'')}</small></div>`).join(''):'<div class="ssh-empty">No remote bundle has been prepared for this case.</div>'}
let sshFormDirty=false;
function markSshDirty(){sshFormDirty=true;el('sshConfigured').textContent='Unsaved';el('sshConfigured').style.color='var(--amber)';for(const id of ['sshInspectButton','sshTestButton','sshEnvironmentButton','sshBundleButton'])el(id).disabled=true;el('sshHostKeys').innerHTML='<div class="ssh-empty">Save these changes before inspecting or connecting.</div>'}
function sshModeChanged(){const alias=el('sshMode').value==='alias';el('sshHostLabel').textContent=alias?'OpenSSH alias':'Hostname / IP';el('sshHost').placeholder=alias?'topas-server':'topas.example.org';el('sshUser').placeholder=alias?'Optional — may come from ~/.ssh/config':'researcher'}
function sshAuthChanged(){const key=el('sshAuthMode').value==='identity_file';el('sshIdentity').disabled=!key;el('sshChooseIdentity').disabled=!key;el('sshIdentity').placeholder=key?'Choose an existing SSH private key':'Not needed for agent / Keychain'}
function sshConfigValues(){return {enabled:el('sshEnabled').checked,server_id:el('sshServerId').value,ssh_mode:el('sshMode').value,ssh_host:el('sshHost').value,ssh_user:el('sshUser').value,ssh_port:Number(el('sshPort').value),auth_mode:el('sshAuthMode').value,identity_file:el('sshIdentity').value,remote_root:el('sshRemoteRoot').value,topas_executable:el('sshTopasExecutable').value,geant4_environment_script:el('sshGeant4Setup').value,geant4_data_root:el('sshGeant4Data').value,max_parallel_jobs:Number(el('sshMaxParallel').value)}}
async function refreshSshServer(){try{const j=await request('/api/ssh-server?root='+encodeURIComponent(el('root').value)),c=j.config;sshFormDirty=false;el('sshConfigured').textContent=j.configured?'Ready':j.enabled?'Blocked':'Not configured';el('sshConfigured').style.color=j.configured?'var(--green)':j.enabled?'var(--red)':'var(--amber)';el('sshEnabled').checked=!!j.enabled;el('sshServerId').value=c.serverId||'';el('sshMode').value=c.sshMode||'direct';el('sshHost').value=c.sshHost||'';el('sshUser').value=c.sshUser||'';el('sshPort').value=c.port||22;el('sshAuthMode').value=c.authMode||'agent';el('sshIdentity').value=c.identityFile||'';el('sshRemoteRoot').value=c.remoteRoot||'';el('sshTopasExecutable').value=c.topasExecutable||'';el('sshGeant4Setup').value=c.geant4EnvironmentScript||'';el('sshGeant4Data').value=c.geant4DataRoot||'';el('sshMaxParallel').value=c.maxParallelJobs||1;sshModeChanged();sshAuthChanged();el('sshHostKeys').innerHTML=c.hostKeySha256?`<div class="ssh-host-key"><strong>Pinned server fingerprint</strong><code>${esc(c.hostKeySha256)}</code><small>${esc(c.knownHosts)}</small></div>`:'<div class="ssh-empty">Save connection settings, then inspect the server host key.</div>';renderSshFindings(j.findings);renderSshBundles(j.bundles);el('sshInspectButton').disabled=!j.canInspectHostKey;el('sshTestButton').disabled=!j.configured;el('sshEnvironmentButton').disabled=!j.configured;el('sshBundleButton').disabled=!j.configured;const labels={authentication:'Authentication',hostKey:'Server identity',runtime:'Runtime',patientData:'Patient data'};el('sshPolicy').innerHTML=Object.entries(j.policy).map(([key,value])=>`<div><strong>${esc(labels[key]||key)}</strong><small>${esc(value)}</small></div>`).join('');return j}catch(e){el('sshConfigured').textContent='Unavailable';el('sshFindings').innerHTML=`<div class="ssh-empty">${esc(e.message)}</div>`;for(const id of ['sshInspectButton','sshTestButton','sshEnvironmentButton','sshBundleButton'])el(id).disabled=true;return null}}
async function saveSshServer(){try{const j=await request('/api/ssh-server/config',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(sshConfigValues())});toast('SSH server settings saved. Inspect and verify the host key before connecting.');await refreshSshServer();return j}catch(e){toast(e.message,true);return null}}
async function chooseSshIdentity(){try{const j=await request('/api/ssh-server/select-identity',{method:'POST'});if(!j.cancelled){el('sshIdentity').value=j.path;markSshDirty()}}catch(e){toast(e.message,true)}}
function renderSshHostKeys(j){el('sshHostKeys').innerHTML=`<div class="notice"><b>${esc(j.hostname)}:${esc(j.port)}</b> · verify one SHA-256 fingerprint through an independent administrator/source before trusting it.</div>`+j.candidates.map((item,index)=>`<div class="ssh-host-key"><strong>${esc(item.keyType)}${item.trusted?' · Trusted':''}</strong><code>${esc(item.fingerprint)}</code><button class="${item.requiresReplacement?'danger':''}" data-key-index="${index}" ${item.trusted?'disabled':''}>${item.trusted?'Already trusted':item.requiresReplacement?'Replace pinned key…':'Trust verified key…'}</button></div>`).join('');el('sshHostKeys').querySelectorAll('[data-key-index]').forEach(button=>button.onclick=()=>trustSshHostKey(j.candidates[Number(button.dataset.keyIndex)]))}
async function inspectSshHostKey(){el('sshInspectButton').disabled=true;try{const j=await request('/api/ssh-server/inspect-host-key',{method:'POST'});renderSshHostKeys(j);toast('Host key inspected. Verify the fingerprint independently before trusting it.')}catch(e){toast(e.message,true)}finally{el('sshInspectButton').disabled=false}}
async function trustSshHostKey(item){const warning=item.requiresReplacement?'WARNING: this replaces the currently pinned server identity. A changed key can indicate a rebuilt server or a man-in-the-middle attack.':'This pins the server identity for strict checking.';if(!confirm(`${warning}\n\nFingerprint:\n${item.fingerprint}\n\nHave you verified this exact fingerprint through an independent trusted channel?`))return;try{const j=await request('/api/ssh-server/trust-host-key',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fingerprint:item.fingerprint,replace:!!item.requiresReplacement})});toast(j.message);await refreshSshServer()}catch(e){toast(e.message,true)}}
async function testSshServer(){el('sshTestButton').disabled=true;try{const j=await request('/api/ssh-server/test',{method:'POST'});toast(`${j.message} · ${j.host}`)}catch(e){toast(e.message,true)}finally{await refreshSshServer()}}
async function checkSshEnvironment(){el('sshEnvironmentButton').disabled=true;try{const j=await request('/api/ssh-server/environment',{method:'POST'});renderSshFindings(j.findings);toast(j.message,!j.ready)}catch(e){toast(e.message,true);await refreshSshServer()}finally{el('sshEnvironmentButton').disabled=false}}
async function prepareRemoteBundle(){if(!confirm(`Prepare an audited remote transport bundle for the current case?\n\nOutput tag: ${el('output_tag').value}\n\nThis local step does not upload data or start a server calculation. The generated upload script will send patient CT and TOPAS parameter files only; it will use the configured server TOPAS and Geant4 installation.`))return;await submitAction('prepare_remote_bundle')}
document.querySelectorAll('.ssh-form input,.ssh-form select').forEach(control=>control.addEventListener('input',markSshDirty));

function selectedTpsDoseInfo(){const option=el('tpsDose').selectedOptions[0];if(!option||!option.value)return null;return {uid:option.value,label:option.textContent,doseType:option.dataset.doseType||'',summationType:option.dataset.summationType||'',doseUnits:option.dataset.doseUnits||'',fileName:option.dataset.fileName||'',isDefault:option.dataset.isDefault==='true'}}
async function refreshTpsDoses(forceDefault=false){const select=el('tpsDose'),current=forceDefault?'':select.value;try{const j=await request('/api/tps-doses?root='+encodeURIComponent(el('root').value));select.innerHTML=j.doses.map(d=>`<option value="${esc(d.doseUID)}" data-dose-type="${esc(d.doseType)}" data-summation-type="${esc(d.summationType)}" data-dose-units="${esc(d.doseUnits)}" data-file-name="${esc(d.fileName)}" data-is-default="${d.isDefault?'true':'false'}">${esc(d.label)}${d.seriesDescription?' — '+esc(d.seriesDescription):''}</option>`).join('');if(current&&[...select.options].some(option=>option.value===current))select.value=current;else select.value=j.defaultDoseUID||select.options[0]?.value||'';select.disabled=!j.doses.length;return j}catch(e){select.innerHTML='<option value="">No compatible TPS RTDOSE</option>';select.disabled=true;return null}}
async function changeTpsDose(){const info=selectedTpsDoseInfo();invalidateLineDose();if(!info)return;const diagnostic=!info.isDefault,notice=diagnostic?' This is not the default PLAN / PHYSICAL dose; comparisons are diagnostic.':'';if(el('results').classList.contains('active')&&el('resultMode').value==='line-dose')await initializeLineDose(true);else{el('resultImage').src='';el('resultImage').style.display='none';el('summary').textContent=`Selected ${info.label}. Run Export profiles or Gamma to regenerate reports with this RTDOSE.`;el('gammaHeadline').textContent='No Gamma result for the new selection';el('gammaProtocol').textContent='Run Gamma to compare the selected TPS RTDOSE.';toast(`Selected ${info.label}.${notice}`)}}

const lineDoseState={initialized:false,sourceKey:'',meta:null,frameMeta:null,tps:null,mc:null,line:null,profile:null,drawing:false,fit:null,chart:null,frameToken:0};
function invalidateLineDose(){lineDoseState.initialized=false;lineDoseState.sourceKey='';lineDoseState.meta=null;lineDoseState.frameMeta=null;lineDoseState.tps=null;lineDoseState.mc=null;lineDoseState.line=null;lineDoseState.profile=null;el('lineExport').disabled=true}
function switchResultMode(){const interactive=el('resultMode').value==='line-dose';el('lineDoseResults').hidden=!interactive;el('reportResults').hidden=interactive;if(interactive)initializeLineDose(false);else refreshResults()}
function lineDoseQuery(){return new URLSearchParams({root:el('root').value,mc_binary:el('mc_binary').value,tps_dose_uid:el('tpsDose').value})}
function lineDosePayload(){return {root:el('root').value,mc_binary:el('mc_binary').value,tps_dose_uid:el('tpsDose').value,output_tag:el('output_tag').value,p1:lineDoseState.line&&lineDoseState.line.p1,p2:lineDoseState.line&&lineDoseState.line.p2,samples:Number(el('lineSamples').value)||512,normalization:el('lineNormalization').value}}
function decodeMeta(text){return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(text),c=>c.charCodeAt(0))))}
function sizeCanvas(canvas){const rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=Math.max(1,rect.width),h=Math.max(1,rect.height);if(canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)){canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr)}const ctx=canvas.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);return {ctx,w,h,dpr}}
function patientPoint(u,v){const m=lineDoseState.frameMeta;return m.origin.map((x,i)=>x+u*m.uVec[i]+v*m.vVec[i])}
function fmtPoint(p){return p.map(v=>Number(v).toFixed(1)).join(', ')}
function fitDoseFrame(w,h){const m=lineDoseState.frameMeta,pw=Math.max(m.pixelSizeU,m.width*m.pixelSizeU),ph=Math.max(m.pixelSizeV,m.height*m.pixelSizeV),scale=Math.min((w-18)/pw,(h-18)/ph),dw=pw*scale,dh=ph*scale;return {x:(w-dw)/2,y:(h-dh)/2,w:dw,h:dh}}
function canvasToFrame(event){const m=lineDoseState.frameMeta,fit=lineDoseState.fit,rect=el('doseCanvas').getBoundingClientRect();if(!m||!fit)return null;const x=event.clientX-rect.left,y=event.clientY-rect.top;if(x<fit.x||x>fit.x+fit.w||y<fit.y||y>fit.y+fit.h)return null;return {u:(x-fit.x)/fit.w*(m.width-1),v:(y-fit.y)/fit.h*(m.height-1),x,y}}
function colorStop(t){const stops=[[0,[4,15,35]],[.16,[33,67,166]],[.35,[23,156,199]],[.55,[39,174,96]],[.74,[244,202,58]],[.9,[239,105,46]],[1,[183,28,43]]];t=Math.max(0,Math.min(1,t));for(let i=1;i<stops.length;i++){if(t<=stops[i][0]){const a=stops[i-1],b=stops[i],q=(t-a[0])/(b[0]-a[0]);return a[1].map((v,j)=>Math.round(v+q*(b[1][j]-v)))}}return stops.at(-1)[1]}
function differenceColor(value){const t=Math.max(-1,Math.min(1,value));return t<0?[Math.round(245+40*t),Math.round(245+115*t),245]:[245,Math.round(245-155*t),Math.round(245-205*t)]}
function renderDoseFrame(){const canvas=el('doseCanvas'),s=sizeCanvas(canvas),ctx=s.ctx;ctx.clearRect(0,0,s.w,s.h);ctx.fillStyle='#07111f';ctx.fillRect(0,0,s.w,s.h);const m=lineDoseState.frameMeta;if(!m||!lineDoseState.tps)return;const fit=fitDoseFrame(s.w,s.h);lineDoseState.fit=fit;const off=document.createElement('canvas');off.width=m.width;off.height=m.height;const octx=off.getContext('2d'),image=octx.createImageData(m.width,m.height),layer=el('lineImageLayer').value,tmax=m.tpsMaxGy||1,mcmax=m.mcAbsoluteCalibrated?tmax:(m.mcMaxRawGy||1);for(let i=0;i<m.width*m.height;i++){const tn=lineDoseState.tps[i]/tmax,mn=lineDoseState.mc?lineDoseState.mc[i]/mcmax:0;let rgb;if(layer==='difference')rgb=differenceColor(mn-tn);else{const n=layer==='mc'?mn:tn;rgb=n<.005?[4,10,20]:colorStop(n)}const j=i*4;image.data[j]=rgb[0];image.data[j+1]=rgb[1];image.data[j+2]=rgb[2];image.data[j+3]=255}octx.putImageData(image,0,0);ctx.imageSmoothingEnabled=true;ctx.drawImage(off,fit.x,fit.y,fit.w,fit.h);ctx.strokeStyle='#dbeafe';ctx.lineWidth=1;ctx.strokeRect(fit.x+.5,fit.y+.5,fit.w-1,fit.h-1);if(m.isocenterInsideFrame){const ix=fit.x+m.isocenterU/(m.width-1)*fit.w,iy=fit.y+m.isocenterV/(m.height-1)*fit.h;ctx.save();ctx.strokeStyle=m.isIsocenterSlice?'#ffd34d':'#d5a800aa';ctx.fillStyle='#ffd34d';ctx.lineWidth=m.isIsocenterSlice?1.8:1.2;ctx.setLineDash([7,5]);ctx.beginPath();ctx.moveTo(fit.x,iy);ctx.lineTo(fit.x+fit.w,iy);ctx.moveTo(ix,fit.y);ctx.lineTo(ix,fit.y+fit.h);ctx.stroke();ctx.setLineDash([]);ctx.beginPath();ctx.arc(ix,iy,5.5,0,Math.PI*2);ctx.fill();ctx.fillStyle='#08101c';ctx.font='bold 9px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText('ISO',ix,iy);ctx.restore()}const L=lineDoseState.line;if(L){const x1=fit.x+L.u1/(m.width-1)*fit.w,y1=fit.y+L.v1/(m.height-1)*fit.h,x2=fit.x+L.u2/(m.width-1)*fit.w,y2=fit.y+L.v2/(m.height-1)*fit.h;ctx.strokeStyle='#ffffff';ctx.lineWidth=2.2;ctx.shadowColor='#000';ctx.shadowBlur=4;ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y2);ctx.stroke();ctx.shadowBlur=0;for(const [x,y,label] of [[x1,y1,'A'],[x2,y2,'B']]){ctx.fillStyle='#fff';ctx.beginPath();ctx.arc(x,y,5,0,Math.PI*2);ctx.fill();ctx.fillStyle='#07111f';ctx.font='bold 10px sans-serif';ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(label,x,y)}}}
function updateDoseReadout(position){const m=lineDoseState.frameMeta;if(!m||!lineDoseState.tps){el('doseReadout').textContent='No dose plane loaded.';return}const u=position?position.u:(m.width-1)/2,v=position?position.v:(m.height-1)/2,i=Math.round(v)*m.width+Math.round(u),p=patientPoint(u,v),t=lineDoseState.tps[i],mc=lineDoseState.mc&&lineDoseState.mc[i],label=lineDoseState.meta?.tpsLabel||'TPS',mcLabel=m.mcAbsoluteCalibrated?'MC calibrated':'MC uncalibrated';el('doseReadout').textContent=`Patient XYZ ${fmtPoint(p)} mm · ${label} ${Number(t).toPrecision(4)} Gy${mc==null?'':` · ${mcLabel} ${Number(mc).toPrecision(4)} Gy`}`}
function defaultLine(){const m=lineDoseState.frameMeta,v=m.isocenterInsideFrame?m.isocenterV:(m.height-1)/2,u1=(m.width-1)*.08,u2=(m.width-1)*.92;lineDoseState.line={u1,v1:v,u2,v2:v,p1:patientPoint(u1,v),p2:patientPoint(u2,v)};renderDoseFrame();updateDoseReadout(null);sampleLineDose()}
async function initializeLineDose(force=false){try{if(!el('tpsDose').value)await refreshTpsDoses();const key=el('root').value+'|'+el('tpsDose').value+'|'+el('mc_binary').value;if(lineDoseState.initialized&&!force&&lineDoseState.sourceKey===key){renderDoseFrame();renderLineDoseChart();return}el('lineDoseStatus').textContent='Loading selected TPS RTDOSE and MC dose grids…';const meta=await request('/api/line-dose/meta?'+lineDoseQuery());lineDoseState.meta=meta;lineDoseState.initialized=true;lineDoseState.sourceKey=key;const absoluteOption=el('lineNormalization').querySelector('option[value="absolute"]');absoluteOption.disabled=meta.hasMC&&!meta.mcAbsoluteCalibrated;if(meta.hasMC&&meta.mcAbsoluteCalibrated)el('lineNormalization').value='absolute';else if(el('lineNormalization').value==='absolute')el('lineNormalization').value='independent';el('mcDicomMode').querySelector('option[value="particle_calibrated"]').disabled=meta.hasMC&&!meta.mcAbsoluteCalibrated;const plane=el('linePlane').value,count=meta.dimensions[plane];el('lineSlice').max=Math.max(0,count-1);el('lineSlice').value=meta.isocenterSlices[plane];el('lineImageLayer').querySelector('option[value="mc"]').disabled=!meta.hasMC;el('lineImageLayer').querySelector('option[value="difference"]').disabled=!meta.hasMC;el('mcDicomExport').disabled=!meta.hasMC;if(!meta.hasMC&&el('lineImageLayer').value!=='tps')el('lineImageLayer').value='tps';let currentNote='';if(meta.tpsDoseType!=='PHYSICAL'||meta.tpsSummationType!=='PLAN')currentNote+=' Selected TPS dose is not PLAN / PHYSICAL; MC comparison is diagnostic only.';if(meta.mcIsCachedExport)currentNote+=' Cached MC RTDOSE loaded for review; see its audit sidecar for normalization.';else if(meta.hasMC&&!meta.mcIsCurrent)currentNote+=' Warning: the selected MC is not current for the latest preparation; treat this view as historical/diagnostic.';if(meta.hasMC&&meta.mcAbsoluteCalibrated)currentNote+=` Independent scale N_plan/N_sim = ${Number(meta.mcCalibrationScale).toPrecision(8)}.`;else if(meta.hasMC)currentNote+=' Particle calibration unavailable: '+meta.mcCalibrationReason;el('lineDoseStatus').textContent=`${meta.tpsLabel} · ${meta.tpsFileName} · ${meta.shapeZYX.join(' × ')} [Z,Y,X], spacing ${meta.spacingXYZmm.map(v=>Number(v).toFixed(2)).join(' × ')} mm. Isocenter ${fmtPoint(meta.isocenterXYZmm)} mm. ${meta.hasMC?meta.mcLabel+' loaded.':'No compatible MC dose found; TPS-only view.'}${currentNote}`;await loadLineDoseFrame(true)}catch(e){invalidateLineDose();el('lineDoseStatus').textContent='Line dose unavailable: '+e.message;toast(e.message,true)}}
async function changeLineDosePlane(){if(!lineDoseState.meta){await initializeLineDose(true);return}const plane=el('linePlane').value,count=lineDoseState.meta.dimensions[plane];el('lineSlice').max=Math.max(0,count-1);el('lineSlice').value=lineDoseState.meta.isocenterSlices[plane];await loadLineDoseFrame(true)}
async function goToIsocenter(){if(!lineDoseState.meta){await initializeLineDose(true);return}const plane=el('linePlane').value;el('lineSlice').value=lineDoseState.meta.isocenterSlices[plane];await loadLineDoseFrame(true)}
async function loadLineDoseFrame(reset=true){if(!lineDoseState.meta)return;const token=++lineDoseState.frameToken,q=lineDoseQuery();q.set('plane',el('linePlane').value);q.set('index',el('lineSlice').value);try{const response=await fetch('/api/line-dose/frame?'+q);if(!response.ok){let msg=response.statusText;try{msg=(await response.json()).error||msg}catch(_e){}throw new Error(msg)}const header=response.headers.get('X-Line-Dose-Meta');if(!header)throw new Error('Dose frame metadata is missing');const meta=decodeMeta(header),buffer=await response.arrayBuffer();if(token!==lineDoseState.frameToken)return;const n=meta.width*meta.height;lineDoseState.frameMeta=meta;lineDoseState.tps=new Float32Array(buffer,0,n);lineDoseState.mc=meta.hasMC?new Float32Array(buffer,n*4,n):null;el('lineSliceLabel').textContent=`Slice ${meta.index+1} / ${lineDoseState.meta.dimensions[meta.plane]} · ΔISO ${meta.isocenterSliceDistanceMm.toFixed(1)} mm`;if(reset||!lineDoseState.line)defaultLine();else{renderDoseFrame();sampleLineDose()}}catch(e){el('lineDoseStatus').textContent='Cannot load dose frame: '+e.message;toast(e.message,true)}}
async function sampleLineDose(){if(!lineDoseState.line)return;try{const j=await request('/api/line-dose/sample',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(lineDosePayload())});lineDoseState.profile=j;el('lineExport').disabled=false;el('lineCaption').textContent=`A (${fmtPoint(j.p1)}) → B (${fmtPoint(j.p2)}) mm · ${j.lengthMm.toFixed(1)} mm · ${j.samples} samples`;renderLineDoseChart();renderLineDoseStats()}catch(e){lineDoseState.profile=null;el('lineExport').disabled=true;toast('Line-dose calculation failed: '+e.message,true)}}
function chartSeries(){const p=lineDoseState.profile;if(!p)return[];const colors={tps:'#2563eb',mc:'#e35d3f'};return p.layers.map(L=>({id:L.id,label:L.label,color:colors[L.id],x:p.distanceMm,y:L.display,inside:p.insideDoseGrid}))}
function renderLineDoseChart(hoverIndex=null){const canvas=el('lineDoseChart'),s=sizeCanvas(canvas),ctx=s.ctx;ctx.clearRect(0,0,s.w,s.h);ctx.fillStyle='#fff';ctx.fillRect(0,0,s.w,s.h);const series=chartSeries();if(!series.length){ctx.fillStyle='#65758b';ctx.font='13px sans-serif';ctx.textAlign='center';ctx.fillText('Draw a line on the dose plane',s.w/2,s.h/2);return}const pad={l:58,r:18,t:20,b:42},xmax=series[0].x.at(-1)||1;let ymax=Math.max(...series.flatMap(a=>a.y.filter(Number.isFinite)));if(el('lineNormalization').value==='independent')ymax=Math.max(105,ymax*1.08);else ymax*=1.1;if(!(ymax>0))ymax=1;const sx=x=>pad.l+x/xmax*(s.w-pad.l-pad.r),sy=y=>s.h-pad.b-y/ymax*(s.h-pad.t-pad.b);lineDoseState.chart={pad,xmax,ymax,w:s.w,h:s.h};ctx.strokeStyle='#dce3ed';ctx.fillStyle='#65758b';ctx.font='11px sans-serif';ctx.lineWidth=1;for(let i=0;i<=5;i++){const y=ymax*i/5,py=sy(y);ctx.beginPath();ctx.moveTo(pad.l,py);ctx.lineTo(s.w-pad.r,py);ctx.stroke();ctx.textAlign='right';ctx.textBaseline='middle';ctx.fillText(y.toFixed(ymax<10?2:0),pad.l-7,py)}for(let i=0;i<=5;i++){const x=xmax*i/5,px=sx(x);ctx.beginPath();ctx.moveTo(px,pad.t);ctx.lineTo(px,s.h-pad.b);ctx.stroke();ctx.textAlign='center';ctx.textBaseline='top';ctx.fillText(x.toFixed(0),px,s.h-pad.b+7)}ctx.strokeStyle='#7b8798';ctx.beginPath();ctx.moveTo(pad.l,pad.t);ctx.lineTo(pad.l,s.h-pad.b);ctx.lineTo(s.w-pad.r,s.h-pad.b);ctx.stroke();ctx.fillStyle='#445268';ctx.textAlign='right';ctx.textBaseline='bottom';ctx.fillText('Distance (mm)',s.w-pad.r,s.h-4);ctx.save();ctx.translate(13,(pad.t+s.h-pad.b)/2);ctx.rotate(-Math.PI/2);ctx.textAlign='center';ctx.textBaseline='middle';ctx.fillText(lineDoseState.profile.displayUnit,0,0);ctx.restore();for(const a of series){ctx.strokeStyle=a.color;ctx.lineWidth=2.4;ctx.lineJoin='round';ctx.lineCap='round';ctx.beginPath();let started=false;for(let i=0;i<a.x.length;i++){if(!a.inside[i]||!Number.isFinite(a.y[i])){started=false;continue}const px=sx(a.x[i]),py=sy(a.y[i]);if(!started){ctx.moveTo(px,py);started=true}else ctx.lineTo(px,py)}ctx.stroke()}if(hoverIndex!=null){const i=Math.max(0,Math.min(series[0].x.length-1,hoverIndex)),px=sx(series[0].x[i]);ctx.strokeStyle='#64748b';ctx.setLineDash([4,3]);ctx.beginPath();ctx.moveTo(px,pad.t);ctx.lineTo(px,s.h-pad.b);ctx.stroke();ctx.setLineDash([]);for(const a of series){if(!a.inside[i])continue;ctx.fillStyle=a.color;ctx.beginPath();ctx.arc(px,sy(a.y[i]),4.5,0,Math.PI*2);ctx.fill()}}}
function renderLineDoseStats(){const p=lineDoseState.profile;if(!p)return;el('lineStats').innerHTML=p.layers.map(L=>{const s=L.stats,f=v=>v==null?'—':Number(v).toFixed(2);return `<div class="line-stat"><strong>${esc(L.label)}</strong><span>Max ${f(s.max)} @ ${f(s.maxAtMm)} mm</span><span>Mean ${f(s.mean)} · FWHM ${f(s.fwhmMm)} mm</span></div>`}).join('')}
async function exportLineDose(){if(!lineDoseState.profile)return;try{const response=await fetch('/api/line-dose/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(lineDosePayload())});if(!response.ok){let j={};try{j=await response.json()}catch(_e){}throw new Error(j.error||response.statusText)}const blob=await response.blob(),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download=`line_dose_${el('output_tag').value}.csv`;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),2000);const saved=response.headers.get('X-Saved-Line-Dose');toast(saved?'Line-dose CSV downloaded and cached in this patient run':'Line-dose CSV exported');refreshResultCache()}catch(e){toast('CSV export failed: '+e.message,true)}}
async function exportMcDicom(){
 if(!lineDoseState.meta?.hasMC){toast('Load an MC dose first',true);return}
 const originalMcSource=el('mc_binary').value;
 const mode=el('mcDicomMode').value,labels={particle_calibrated:'independently particle-calibrated QA Gy',peak_scaled:'legacy TPS-peak-fitted diagnostic dose',raw:'raw TOPAS per-run dose'},label=labels[mode],patient=`${lineDoseState.meta.patientID||'<missing>'} / ${lineDoseState.meta.patientName||'<missing>'}`;
 if(mode==='particle_calibrated'&&!lineDoseState.meta.mcAbsoluteCalibrated){toast('This MC source has no commissioned N_plan/N_sim calibration',true);return}
 if(!confirm(`Export MC as DICOM RTDOSE (${label})?\n\nTarget patient: ${patient}\nStudy UID: ${lineDoseState.meta.studyInstanceUID||'<missing>'}\nRTPLAN UID: ${lineDoseState.meta.referencedRTPlanUID||'<missing>'}\n\nPatient, Study, Frame and RTPLAN references will be validated before download. Particle calibration is research/QA physical dose, not clinical acceptance.`))return;
 try{
  const j=await request('/api/mc-rtdose/export',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...values(),mode})});
  const a=document.createElement('a');a.href=j.fileUrl;a.download=j.path.split('/').at(-1);document.body.appendChild(a);a.click();a.remove();
  // Export is a copy operation.  Keep the currently loaded TOPAS source;
  // changing it to the derivative DICOM made the original MC appear cleared.
  if(originalMcSource){el('mc_binary').value=originalMcSource}
  else if(j.source_mc){el('mc_binary').value=j.source_mc;invalidateLineDose();await initializeLineDose(true)}
  await refreshResultCache();
  toast(`MC RTDOSE exported for PatientID ${j.patient_id}; original MC source and line-dose data retained`)
 }catch(e){
  el('mc_binary').value=originalMcSource;
  toast('MC RTDOSE export failed: '+e.message,true)
 }
}
async function importMcDicom(file){if(!file)return;try{const q=new URLSearchParams({root:el('root').value,tag:el('output_tag').value,name:file.name}),response=await fetch('/api/mc-rtdose/import?'+q,{method:'POST',headers:{'Content-Type':'application/dicom'},body:file}),j=await response.json();if(!response.ok)throw new Error(j.error||response.statusText);el('mc_binary').value=j.path;invalidateLineDose();await refreshResultCache();await initializeLineDose(true);toast(j.message)}catch(e){toast('MC RTDOSE import failed: '+e.message,true)}finally{el('mcDicomFile').value=''}}
el('mcDicomFile').onchange=event=>importMcDicom(event.target.files[0]);
let lineSliceTimer=null;el('lineSlice').oninput=()=>{el('lineSliceLabel').textContent=`Slice ${Number(el('lineSlice').value)+1}`;clearTimeout(lineSliceTimer);lineSliceTimer=setTimeout(()=>loadLineDoseFrame(true),120)};el('lineSamples').onchange=sampleLineDose;
el('doseCanvas').addEventListener('pointerdown',event=>{const p=canvasToFrame(event);if(!p)return;lineDoseState.drawing=true;el('doseCanvas').setPointerCapture(event.pointerId);lineDoseState.line={u1:p.u,v1:p.v,u2:p.u,v2:p.v,p1:patientPoint(p.u,p.v),p2:patientPoint(p.u,p.v)};renderDoseFrame()});
el('doseCanvas').addEventListener('pointermove',event=>{const p=canvasToFrame(event);if(!p)return;updateDoseReadout(p);if(lineDoseState.drawing){Object.assign(lineDoseState.line,{u2:p.u,v2:p.v,p2:patientPoint(p.u,p.v)});renderDoseFrame()}});
el('doseCanvas').addEventListener('pointerup',event=>{if(!lineDoseState.drawing)return;lineDoseState.drawing=false;const L=lineDoseState.line;if(Math.hypot(L.u2-L.u1,L.v2-L.v1)<2){defaultLine();return}sampleLineDose()});
el('lineDoseChart').addEventListener('pointermove',event=>{const c=lineDoseState.chart,p=lineDoseState.profile;if(!c||!p)return;const rect=el('lineDoseChart').getBoundingClientRect(),x=event.clientX-rect.left,index=Math.round(Math.max(0,Math.min(1,(x-c.pad.l)/(c.w-c.pad.l-c.pad.r)))*(p.distanceMm.length-1));renderLineDoseChart(index);const values=p.layers.map(L=>`${L.id.toUpperCase()} ${Number(L.display[index]).toFixed(3)}`).join(' · ');el('lineDoseHover').textContent=`${p.distanceMm[index].toFixed(2)} mm · ${values} · XYZ ${fmtPoint(p.pointsXYZmm[index])} mm`});
el('lineDoseChart').addEventListener('pointerleave',()=>{renderLineDoseChart();el('lineDoseHover').textContent='Move over the chart for exact values.'});window.addEventListener('resize',()=>{if(el('resultMode').value==='line-dose'){renderDoseFrame();renderLineDoseChart()}});
function runtimeText(e){const low=Number(e.low_hours??e.low),high=Number(e.high_hours??e.high),hours=Number(e.hours);if(!Number.isFinite(hours))return 'Estimate unavailable';const range=Number.isFinite(low)&&Number.isFinite(high)?` · range ${low.toFixed(1)}–${high.toFixed(1)} h`:'';return `${hours.toFixed(1)} h${range}`}
function formatDuration(seconds){seconds=Number(seconds);if(!Number.isFinite(seconds)||seconds<0)return '—';const total=Math.round(seconds),days=Math.floor(total/86400),hours=Math.floor(total%86400/3600),minutes=Math.floor(total%3600/60),secs=total%60;if(days)return `${days}d ${hours}h ${minutes}m`;if(hours)return `${hours}h ${minutes}m`;if(minutes)return `${minutes}m ${secs}s`;return `${secs}s`}
function formatBytes(bytes){bytes=Number(bytes);if(!Number.isFinite(bytes)||bytes<0)return '—';const units=['B','KiB','MiB','GiB','TiB'];let value=bytes,index=0;while(value>=1024&&index<units.length-1){value/=1024;index++}return `${value.toFixed(index<2?0:1)} ${units[index]}`}
let runOptionData=[];
async function openRunModal(){try{const q=new URLSearchParams(values());const j=await request('/api/run-options?'+q);runOptionData=j.options;el('runOptions').innerHTML=j.options.map((o,i)=>`<label class="run-option ${o.id==='recommended'?'selected':''}"><input type="radio" name="runPlan" value="${esc(o.id)}" ${o.id==='recommended'?'checked':''}><span><strong>${esc(o.label)}</strong><small>${esc(o.purpose)}</small></span><span>${Number(o.histories).toLocaleString()}</span><span>${o.threads}</span><span class="estimate">${runtimeText(o)}<small>${esc(o.confidence||'low')} confidence · ${o.requires_rebuild?'Stages 6–8 will rebuild':'Current preparation matches'}</small></span></label>`).join('');el('runCustomHistories').value=el('histories').value;el('runCustomThreads').value=el('threads').value;updateCustomEstimate();el('runModal').classList.add('open');document.querySelectorAll('input[name="runPlan"]').forEach(r=>r.onchange=()=>{document.querySelectorAll('.run-option').forEach(x=>x.classList.toggle('selected',x.contains(r)&&r.checked))})}catch(e){toast(e.message,true)}}
function closeRunModal(){el('runModal').classList.remove('open')}
let customEstimateTimer=null,customEstimateData=null,customEstimateKey='';
async function fetchRuntimeEstimate(h,t){const params={...values(),histories:h,threads:t};return (await request('/api/runtime-estimate?'+new URLSearchParams(params))).estimate}
function updateCustomEstimate(){const h=+el('runCustomHistories').value,t=+el('runCustomThreads').value,target=el('runCustomEstimate');clearTimeout(customEstimateTimer);customEstimateData=null;customEstimateKey='';if(!(h>0&&Number.isInteger(t)&&t>0)){target.textContent='Enter valid positive values';return}if(t>MAX_THREADS){target.textContent=`Too many threads: this Mac has ${MAX_THREADS} logical CPUs. More Geant4 workers than cores makes the run slower, not faster.`;return}target.textContent='Estimating from this case…';customEstimateTimer=setTimeout(async()=>{try{const estimate=await fetchRuntimeEstimate(h,t);if(+el('runCustomHistories').value!==h||+el('runCustomThreads').value!==t)return;customEstimateData=estimate;customEstimateKey=`${h}:${t}`;target.innerHTML=`${esc(runtimeText(estimate))}<small>${esc(estimate.confidence||'low')} confidence · ${esc(estimate.method||'case model')}</small>${estimate.history_budget_note?`<small class="sparse-warning">${esc(estimate.history_budget_note)}</small>`:''}`}catch(error){target.textContent='Estimate unavailable: '+error.message}},250)}
el('runCustomHistories').oninput=updateCustomEstimate;el('runCustomThreads').oninput=updateCustomEstimate;el('runCustomHistories').onclick=()=>el('runCustomRadio').checked=true;el('runCustomThreads').onclick=()=>el('runCustomRadio').checked=true;
async function confirmRunPlan(){const selected=document.querySelector('input[name="runPlan"]:checked');if(!selected){toast('Select a calculation plan',true);return}let h,t,label,e;if(selected.value==='custom'){h=+el('runCustomHistories').value;t=+el('runCustomThreads').value;label='Custom'}else{const o=runOptionData.find(x=>x.id===selected.value);h=o.histories;t=o.threads;label=o.label;e=o}if(!Number.isInteger(h)||h<1||!Number.isInteger(t)||t<1){toast('Histories must be positive and threads must be a positive integer',true);return}if(t>MAX_THREADS){toast(`Threads must not exceed the ${MAX_THREADS} logical CPUs on this Mac; oversubscribed Geant4 workers measured 1.4–2.1× slower`,true);return}const modelError=commissionedSelectionError();if(modelError){toast(modelError,true);return}const mode=beamInputMode(),manualError=mode==='manual'?manualBeamError():'';if(manualError){toast(manualError,true);return}const layerValue=selectedEnergyLayerValue();if(mode==='rtplan'&&layerValue==='none'){toast('Select at least one RTPLAN energy layer',true);return}if(!e)e=customEstimateKey===`${h}:${t}`&&customEstimateData?customEstimateData:await fetchRuntimeEstimate(h,t);const selectedProfile=el('beamModelProfile').selectedOptions[0]?.textContent||'automatic exact match',model=beamModelMode()==='commissioned'?`Machine commissioned: ${selectedProfile}`:'RTPLAN baseline (uncommissioned)',beam=mode==='manual'?`\nManual beam: ${el('manualEnergyMeVu').value} MeV/u at IEC (${el('manualSpotX').value}, ${el('manualSpotY').value}) mm; FWHM (${el('manualSpotFwhmX').value}, ${el('manualSpotFwhmY').value}) mm.\nThis is one research spot, not the TPS spot plan.\n`:el('beamOverrideEnabled').checked?`\nRTPLAN overrides: energy ${el('beamEnergyScale').value}% + ${el('beamEnergyOffset').value} MeV/u; spot ${el('beamSpotScale').value}%; spread ${el('beamEnergySpread').value}%\n`:'',energy=mode==='manual'?'Manual single Energy + spot':layerValue==='all'?'All RTPLAN energy layers':'Energy LayerIndex subset: '+layerValue,low=e.low_hours??e.low,high=e.high_hours??e.high,sparse=e.history_budget_note||'';if(sparse&&!confirm(`${sparse}\n\nStart this sparse test run anyway?`))return;if(!confirm(`${label}\nHistories: ${h.toLocaleString()}\nThreads requested: ${t} (of ${MAX_THREADS} logical CPUs)\nBeam model: ${model}\n${energy}\nEstimated time: ${Number(e.hours).toFixed(1)} h\nEstimated range: ${Number(low).toFixed(1)}–${Number(high).toFixed(1)} h\nConfidence: ${e.confidence||'low'}\nBasis: ${e.method||'case model'}\nEffective CPU cores predicted: ${Number(e.effective_threads||0).toFixed(2)}\n${beam}\nExisting production dose is archived automatically before a new run.\nStages 4 and 6–8 rebuild automatically if required.\nResearch physical-dose QA; independent commissioning acceptance is still required. Start?`))return;el('histories').value=h;el('threads').value=t;el('output_tag').value=mode==='manual'?`manual_spot_${h}`:beamModelMode()==='commissioned'?`full_plan_${h}_commissioned`:`full_plan_${h}`;closeRunModal();await submitAction('run_topas')}
function toast(text,bad=false){const e=document.getElementById('toast');e.textContent=text;e.style.background=bad?'#8b2635':'#15243e';e.style.display='block';setTimeout(()=>e.style.display='none',5000)}
function updateProgress(p){p=p||{};const fraction=Math.max(0,Math.min(1,Number(p.fraction)||0));const fill=el('progressFill');const track=fill&&fill.parentElement;if(!fill||!track)return;fill.style.width=(fraction*100).toFixed(1)+'%';track.setAttribute('aria-valuenow',String(Math.round(fraction*100)));el('progressText').textContent=p.label||p.phase||'Idle'}
function updateComputeStatus(c,state){c=c||{};state=state||{};const running=!!state.running,paused=!!state.paused,planned=c.planned_estimate||{},requestInfo=c.runtime_request||{},processes=Array.isArray(c.processes)?c.processes:[],top=Array.isArray(c.top_system_processes)?c.top_system_processes:[],hasEta=c.eta_seconds!==null&&c.eta_seconds!==undefined,eta=hasEta?Number(c.eta_seconds):NaN,taskCpu=Number(c.task_cpu_percent),taskCores=Number(c.task_cpu_cores),systemCpu=Number(c.system_cpu_percent),logical=Number(c.logical_cpus),taskThreads=Number(c.task_os_threads),requestedThreads=requestInfo.threads!==null&&requestInfo.threads!==undefined?Number(requestInfo.threads):NaN,memoryPercent=Number(c.memory_percent),load=Array.isArray(c.load_average)?c.load_average:[];el('computeSummary').textContent=paused?'Paused':running?'Running':'Idle';el('computeCommand').textContent=c.current_command||'Idle';el('computeElapsed').textContent=`${formatDuration(c.task_elapsed_seconds)} wall · ${formatDuration(c.process_active_elapsed_seconds)} active${Number(c.paused_seconds)>0?` · ${formatDuration(c.paused_seconds)} paused`:''}`;el('computeEta').textContent=Number.isFinite(eta)?`${formatDuration(eta)}${c.estimated_finish_epoch?` · finish ${new Date(Number(c.estimated_finish_epoch)*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'})}`:''}`:(running?'Warming up…':'—');el('computePlan').textContent=planned.seconds!==null&&planned.seconds!==undefined&&Number.isFinite(Number(planned.seconds))?`${formatDuration(planned.seconds)} · ${planned.confidence||'low'} confidence`:'—';el('computeCpu').textContent=running&&Number.isFinite(taskCpu)?`${taskCpu.toFixed(1)}% · ${taskCores.toFixed(2)} cores${Number.isFinite(logical)?` · ${(Number(c.task_cpu_normalized_percent)||0).toFixed(1)}% of Mac`:''}`:'—';el('computeMemory').textContent=running&&Number.isFinite(Number(c.task_rss_bytes))?`${formatBytes(c.task_rss_bytes)} RSS`:'—';el('computeThreads').textContent=running?`${Number.isFinite(taskThreads)?taskThreads:0} OS thread${taskThreads===1?'':'s'}${Number.isFinite(requestedThreads)?` · ${requestedThreads} requested`:''}`:'—';el('computeSystem').textContent=`${Number.isFinite(systemCpu)?systemCpu.toFixed(1)+'% CPU':'CPU —'}${Number.isFinite(memoryPercent)?` · ${memoryPercent.toFixed(0)}% memory`:''}${load.length?` · load ${load.slice(0,3).map(x=>Number(x).toFixed(1)).join('/')}`:''}`;el('computeProcesses').innerHTML=processes.length?processes.slice(0,20).map(p=>`<tr title="${esc(p.arguments||'')}"><td>${esc(p.pid)}</td><td>${esc(p.command||'process')}</td><td>${Number(p.cpu_percent||0).toFixed(1)}%</td><td>${formatBytes(p.rss_bytes)}</td><td>${p.threads??'—'}</td><td>${esc(p.state||'—')}</td><td>${esc(p.os_elapsed||'—')}</td></tr>`).join(''):'<tr><td colspan="7">No active task process.</td></tr>';el('computeTop').innerHTML=top.length?top.map(p=>`<span title="${esc(p.arguments||'')}"><b>${esc(p.command||'process')}</b> PID ${esc(p.pid)} · ${Number(p.cpu_percent||0).toFixed(1)}% CPU · ${formatBytes(p.rss_bytes)}</span>`).join('<br>'):'No process sample is available.';const progress=state.progress||{},spotText=Number(progress.total_spots)>0?`${Number(progress.completed_spots||0).toLocaleString()} / ${Number(progress.total_spots).toLocaleString()} sequential spots · `:'',historyText=Number(progress.total_histories)>0?`${Number(progress.completed_histories||0).toLocaleString()} / ${Number(progress.total_histories).toLocaleString()} histories · `:'',hardware=[c.cpu_model,Number.isFinite(Number(c.physical_cpus))?`${c.physical_cpus} physical cores`:'',Number.isFinite(logical)?`${logical} logical CPUs`:''].filter(Boolean).join(' · ');el('computeNote').textContent=`${spotText}${historyText}${c.eta_basis?`ETA: ${c.eta_basis} (${c.eta_confidence||'low'} confidence). `:''}${hardware}${c.error?` · Monitor warning: ${c.error}`:''}`;if(running&&el('computeMonitor'))el('computeMonitor').open=true}
async function request(path,opts={}){const r=await fetch(path,opts);const j=await r.json();if(!r.ok)throw new Error(j.error||r.statusText);return j}
function queueButtons(job){const id=JSON.stringify(job.id),buttons=[];if(job.status==='running')buttons.push(`<button class="pause" onclick='queueControl(${id},"pause")'>Pause</button>`,`<button class="danger" onclick='queueControl(${id},"cancel")'>Cancel</button>`);else if(job.status==='paused')buttons.push(`<button class="resume" onclick='queueControl(${id},"resume")'>Resume</button>`,`<button class="danger" onclick='queueControl(${id},"cancel")'>Cancel</button>`);else if(['failed','cancelled','interrupted','completed_with_warnings'].includes(job.status))buttons.push(`<button class="resume" onclick='queueControl(${id},"retry")'>Retry</button>`,`<button onclick='queueControl(${id},"remove")'>Remove</button>`);else if(job.status==='queued')buttons.push(`<button class="danger" onclick='queueControl(${id},"cancel")'>Cancel</button>`,`<button onclick='queueControl(${id},"remove")'>Remove</button>`);else if(job.status==='completed')buttons.push(`<button onclick='queueControl(${id},"remove")'>Remove</button>`);buttons.push(`<button onclick='viewQueueLog(${id})'>View log</button>`);return buttons.join('')}
function queueTime(job){const elapsed=job.started_at?formatDuration(job.elapsed_seconds):'Not started',eta=job.status==='paused'?'Paused':job.eta_seconds==null?'—':formatDuration(job.eta_seconds);return `<span>${esc(elapsed)}</span><small>ETA ${esc(eta)}</small>`}
function renderQueue(state){el('queueMode').textContent=state.enabled?'Auto-run enabled':'Scheduling stopped';el('queueActive').textContent=`${state.active} active`;el('queueWaiting').textContent=`${state.queued} waiting`;el('queueParallel').value=String(state.max_parallel);const rows=state.jobs.map(job=>{const progress=Math.max(0,Math.min(100,Number(job.progress||0)*100)),cpu=Number(job.compute?.task_cpu_percent),compute=Number.isFinite(cpu)?` · ${cpu.toFixed(1)}% CPU`:'';return `<tr><td class="queue-case"><strong>${esc(job.label||job.case_root)}</strong><small>${esc(job.case_root)}</small><small>Job ${esc(job.id)} · attempt ${Number(job.attempts||0)}</small></td><td class="queue-config"><span>${Number(job.histories||0).toLocaleString()} histories · ${Number(job.threads||0)} threads</span><small>${esc(job.beam_model_mode||'baseline')} · ${esc(job.output_tag||'')}</small></td><td><span class="queue-state ${esc(job.status)}">${esc(job.status)}</span><small>${esc(job.detail||'')}${compute}</small>${job.error?`<small title="${esc(job.error)}">${esc(job.error)}</small>`:''}</td><td><span>${progress.toFixed(1)}%</span><div class="queue-progress"><span style="width:${progress}%"></span></div><small>${esc(job.stage||'Waiting')}</small></td><td class="queue-time">${queueTime(job)}</td><td><div class="queue-actions">${queueButtons(job)}</div></td></tr>`}).join('');el('queueJobs').innerHTML=rows||'<tr><td colspan="6" class="queue-empty">No cases in the queue.</td></tr>'}
async function refreshQueue(){if(queueRefreshBusy)return;queueRefreshBusy=true;try{const state=await request('/api/queue');renderQueue(state);if(queueSelectedJob){try{const log=await request(`/api/queue/log?job_id=${encodeURIComponent(queueSelectedJob)}&after=${queueLogCursor}`);if(log.text){const box=el('queueLog');box.textContent+=log.text;box.scrollTop=box.scrollHeight}queueLogCursor=log.cursor}catch(e){queueSelectedJob='';queueLogCursor=0;el('queueLogCaption').textContent='The selected queue record is no longer available.';el('queueLog').textContent='No queue log selected.'}}}catch(e){if(el('queue').classList.contains('active'))toast(e.message,true)}finally{queueRefreshBusy=false}}
function queueSettingsValid(){const modelError=commissionedSelectionError();if(modelError){toast(modelError,true);return false}if(beamInputMode()==='manual'){const error=manualBeamError();if(error){toast(error,true);return false}}else if(selectedEnergyLayerValue()==='none'){toast('Select at least one RTPLAN energy layer before adding this case',true);return false}return true}
async function addCurrentToQueue(){if(!queueSettingsValid())return;try{const e=await fetchRuntimeEstimate(+el('histories').value,+el('threads').value);if(e.history_budget_note&&!confirm(`${e.history_budget_note}\n\nQueue this sparse test run anyway?`))return}catch(_){}if(!confirm(`Add the current case to the waiting queue?\n\nHistories: ${Number(el('histories').value).toLocaleString()}\nThreads: ${el('threads').value}\nBeam model: ${beamModelMode()}\nEnergy: ${beamInputMode()==='manual'?'manual spot':selectedEnergyLayerValue()==='all'?'all RTPLAN layers':'selected RTPLAN subset'}\n\nThis snapshots the current settings; it does not start unless auto-run is enabled.`))return;try{const j=await request('/api/queue/add-current',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(values())});toast(j.message);await refreshQueue();showTab('queue')}catch(e){toast(e.message,true)}}
function updateQueueIntakeCounts(counts){for(const modality of ['CT','RTPLAN','RTDOSE','RTSTRUCT']){const count=Number(counts?.[modality]||0);queueIntakeCounts[modality]=count;el('queue_count_'+modality).textContent=`${count} file${count===1?'':'s'}`;el('queue_tile_'+modality).classList.toggle('ready-import',count>0)}el('queueAddCase').disabled=!queueIntakeRoot||Object.values(queueIntakeCounts).some(count=>count<1)}
async function chooseQueueCaseFolder(){try{const j=await request('/api/queue/select-case-folder',{method:'POST'});if(j.cancelled)return;queueIntakeRoot=j.root;el('queueIntakePath').textContent=j.root;el('queueIntake').hidden=false;updateQueueIntakeCounts(j.counts||{});showTab('queue');el('queueIntake').scrollIntoView({behavior:'smooth',block:'start'})}catch(e){toast(e.message,true)}}
function closeQueueIntake(){queueIntakeRoot='';el('queueIntake').hidden=true;el('queueIntakePath').textContent='No case folder selected.';updateQueueIntakeCounts({});for(const modality of ['CT','RTPLAN','RTDOSE','RTSTRUCT'])el('queue_file_'+modality).value=''}
function selectQueueDicom(modality){if(!queueIntakeRoot){toast('Choose a batch case folder first',true);return}el('queue_file_'+modality).click()}
async function importQueueDicom(modality,files){files=files.filter(file=>!file.name.startsWith('.'));if(!queueIntakeRoot){toast('Choose a batch case folder first',true);return}if(!files.length){toast('No DICOM files selected',true);return}const current=queueIntakeCounts[modality]||0,replace=current>0;if(replace&&!confirm(`${modality} already contains ${current} file(s) in this batch case.\n\nReplace them with the selected files? Existing files and previous-patient results will be archived safely.`))return;const tile=el('queue_tile_'+modality);tile.classList.add('busy');document.querySelectorAll('[data-import]').forEach(button=>button.disabled=true);try{const start=await request('/api/import/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({root:queueIntakeRoot,modality,count:files.length,replace,settings:values()})});for(let index=0;index<files.length;index++){const response=await fetch(`/api/import/file?token=${encodeURIComponent(start.token)}&index=${index}&name=${encodeURIComponent(files[index].name)}`,{method:'POST',headers:{'Content-Type':'application/dicom'},body:files[index]});const result=await response.json();if(!response.ok)throw new Error(result.error||response.statusText);el('queue_count_'+modality).textContent=`Uploading ${index+1} / ${files.length}`;}const done=await request('/api/import/finish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:start.token})});updateQueueIntakeCounts(done.counts||{});toast(done.message)}catch(e){toast(e.message,true);try{const status=await request('/api/status?root='+encodeURIComponent(queueIntakeRoot));updateQueueIntakeCounts(status.dicom_counts||{})}catch(_){}}finally{tile.classList.remove('busy');document.querySelectorAll('[data-import]').forEach(button=>button.disabled=false);el('queue_file_'+modality).value=''}}
for(const modality of ['CT','RTPLAN','RTDOSE','RTSTRUCT'])el('queue_file_'+modality).onchange=event=>importQueueDicom(modality,[...event.target.files]);
async function addImportedCaseToQueue(){if(!queueIntakeRoot||Object.values(queueIntakeCounts).some(count=>count<1)){toast('Import CT, RTPLAN, RTDOSE and RTSTRUCT before adding this case',true);return}const modelError=commissionedSelectionError();if(modelError){toast(modelError,true);return}if(beamInputMode()==='manual'){const error=manualBeamError();if(error){toast(error,true);return}}if(!confirm(`Add this imported case to the waiting queue?\n\nCase: ${queueIntakeRoot}\nHistories: ${Number(el('histories').value).toLocaleString()}\nThreads: ${el('threads').value}\nBeam model: ${beamModelMode()}\n\nRTPLAN mode uses all energy layers belonging to this case. The selected immutable model version is mapped into the queued case and revalidated against its RTPLAN.`))return;try{const payload={...values(),root:queueIntakeRoot,energy_layer_indices:'all'},j=await request('/api/queue/add-case',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});toast(j.message);closeQueueIntake();await refreshQueue()}catch(e){toast(e.message,true)}}
async function startQueue(){try{toast((await request('/api/queue/start',{method:'POST'})).message);await refreshQueue()}catch(e){toast(e.message,true)}}
async function stopQueueScheduling(){try{toast((await request('/api/queue/stop-scheduling',{method:'POST'})).message);await refreshQueue()}catch(e){toast(e.message,true)}}
async function setQueueParallel(){try{const value=Number(el('queueParallel').value);toast((await request('/api/queue/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_parallel:value})})).message);await refreshQueue()}catch(e){toast(e.message,true);await refreshQueue()}}
async function queueControl(jobId,queueAction){if(['cancel','remove'].includes(queueAction)&&!confirm(queueAction==='cancel'?'Cancel this case task? Partial outputs and its log will be preserved.':'Remove this queue record? Case data, results and its on-disk log will be preserved.'))return;try{const j=await request('/api/queue/control',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_id:jobId,queue_action:queueAction})});toast(j.message);await refreshQueue()}catch(e){toast(e.message,true)}}
async function viewQueueLog(jobId){queueSelectedJob=jobId;queueLogCursor=0;el('queueLog').textContent='';el('queueLogCaption').textContent=`Persistent log for job ${jobId}`;el('queueLogPanel').open=true;await refreshQueue();el('queueLogPanel').scrollIntoView({behavior:'smooth',block:'start'})}
async function selectCaseFolder(){try{const j=await request('/api/select-case',{method:'POST'});if(j.cancelled)return;el('root').value=j.root;el('casePath').textContent=j.root;cursor=0;lastDone=null;el('log').textContent='';invalidateLineDose();await useLatestMC(false);await refreshAll();toast('Case selected and initialized')}catch(e){toast(e.message,true)}}
function selectDicom(modality){el('file_'+modality).click()}
async function importDicom(modality,files){files=files.filter(file=>!file.name.startsWith('.'));if(!files.length){toast('No DICOM files selected',true);return}const current=Number((el('count_'+modality).textContent.match(/\d+/)||['0'])[0]);const replace=current>0;if(replace&&!confirm(`${modality} already contains ${current} file(s).\n\nReplace them with the selected files? Existing files will be archived. If this starts a new patient/plan, the current settings and production result will be cached automatically.`))return;const tile=el('tile_'+modality);tile.classList.add('busy');document.querySelectorAll('[data-import]').forEach(button=>button.disabled=true);try{const start=await request('/api/import/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({root:el('root').value,modality,count:files.length,replace,settings:values()})});for(let index=0;index<files.length;index++){const response=await fetch(`/api/import/file?token=${encodeURIComponent(start.token)}&index=${index}&name=${encodeURIComponent(files[index].name)}`,{method:'POST',headers:{'Content-Type':'application/dicom'},body:files[index]});const result=await response.json();if(!response.ok)throw new Error(result.error||response.statusText);el('count_'+modality).textContent=`Uploading ${index+1} / ${files.length}`;}const done=await request('/api/import/finish',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:start.token})});if(done.caseChanged)await resetDefaults(false,false);refreshAll();toast(done.message+(done.caseChanged?' Run parameters were reset to defaults.':''))}catch(e){toast(e.message,true)}finally{tile.classList.remove('busy');document.querySelectorAll('[data-import]').forEach(button=>button.disabled=false);el('file_'+modality).value=''}}
for(const modality of ['CT','RTPLAN','RTDOSE','RTSTRUCT'])el('file_'+modality).onchange=event=>importDicom(modality,[...event.target.files]);
async function submitAction(name){submittedHere=true;try{const j=await request('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...values(),action:name})});toast(j.message);focusWorkflowLog()}catch(e){toast(e.message,true)}}
async function action(name){
 if(name==='run_topas'){await openRunModal();return}
 const mode=beamInputMode(),energy=selectedEnergyLayerValue(),build=['pipeline','full_plan'].includes(name);
 if(build){const modelError=commissionedSelectionError();if(modelError){toast(modelError,true);return}}
 if(build&&mode==='manual'){const error=manualBeamError();if(error){toast(error,true);return}if(!confirm(`Generate one manual research spot?\n\nEnergy: ${el('manualEnergyMeVu').value} MeV/u\nIEC X/Y: ${el('manualSpotX').value} / ${el('manualSpotY').value} mm\nFWHM X/Y: ${el('manualSpotFwhmX').value} / ${el('manualSpotFwhmY').value} mm\n\nThis is not a reconstruction of the TPS spot plan.`))return}
 if(build&&mode==='rtplan'&&energy==='none'){toast('Select at least one RTPLAN energy layer',true);return}
 if(build&&mode==='rtplan'&&energy!=='all'&&!confirm('Only selected RTPLAN energy layers will be generated. This is an energy-subset research run, not a complete TPS-plan reconstruction. Continue?'))return;
 if(mode==='rtplan'&&el('beamOverrideEnabled').checked&&build&&!confirm('Advanced beam overrides are enabled. They replace DICOM-derived energy/spot parameters for research use and will be written to the audit. Continue?'))return;
 if(name==='analyze'||name==='gamma'){
  if(!el('tpsDose').value){try{await refreshTpsDoses()}catch(e){toast(e.message,true);return}}
  const tps=selectedTpsDoseInfo();if(!tps){toast('Select a TPS RTDOSE first',true);return}
  if(!el('mc_binary').value&&!confirm('No MC path entered. Use the newest discoverable historical/production binary?'))return;
  const nonstandard=!tps.isDefault,warning=nonstandard?'\n\nWarning: this is not the standard PLAN / PHYSICAL reference. Comparison with TOPAS physical PLAN dose is diagnostic/research only.':'';
  if(name==='analyze'&&!confirm(`Generate three-direction profiles with:\n${tps.label}?${warning}`))return;
  if(name==='gamma'&&!confirm(`Run global 3D Gamma at ${el('gamma_dd_percent').value}% / ${el('gamma_dta_mm').value} mm?\n\nTPS reference: ${tps.label}\nThreshold: 10% of selected TPS maximum.\nMC uses the independent commissioned N_plan/N_sim scale; TPS dose is not used to fit MC output.${warning}`))return;
 }
 await submitAction(name)
}
async function stopTask(){if(!confirm('Terminate the current process?'))return;try{toast((await request('/api/stop',{method:'POST'})).message)}catch(e){toast(e.message,true)}}
async function togglePauseTask(){const endpoint=taskPaused?'resume':'pause';try{const j=await request('/api/'+endpoint,{method:'POST'});taskPaused=!!j.paused;updateTaskControls({running:true,paused:taskPaused,task:el('busy').textContent.replace(/^Paused · /,'')});toast(j.message)}catch(e){toast(e.message,true)}}
function updateTaskControls(state){taskPaused=!!state.paused;const button=el('pauseTaskButton');button.disabled=!state.running;button.textContent=taskPaused?'Resume task':'Pause task';button.classList.toggle('pause',!taskPaused);button.classList.toggle('resume',taskPaused);button.setAttribute('aria-pressed',String(taskPaused));el('progressFill').classList.toggle('paused',taskPaused);el('busy').textContent=state.running?(taskPaused?'Paused · '+state.task:state.task):'Idle'}
async function useLatestMC(notify=true){try{const j=await request('/api/latest-mc?root='+encodeURIComponent(el('root').value));el('mc_binary').value=j.path||'';el('cacheRun').value='';invalidateLineDose();if(notify)toast(j.path?'Selected newest MC binary':'No MC binary found',!j.path)}catch(e){if(notify)toast(e.message,true)}}
async function resetDefaults(notify=true,selectLatest=true){el('histories').value='100000';el('threads').value='12';el('seed').value='1699';el('profile_depth').value='100';el('gamma_dta_mm').value='3';el('gamma_dd_percent').value='3';el('output_tag').value='full_plan_100000';el('topas_executable').value=defaultTopasExecutable;el('direction').value='depth_direction';el('linePlane').value='axial';el('lineImageLayer').value='tps';el('lineNormalization').value='absolute';el('mcDicomMode').value='particle_calibrated';el('lineSamples').value='512';document.querySelector('input[name="beamInputMode"][value="rtplan"]').checked=true;el('beamModelMode').value='baseline';el('beamModelProfile').value='';el('manualEnergyMeVu').value='250';el('manualEnergySpread').value='0';el('manualSpotX').value='0';el('manualSpotY').value='0';el('manualSpotFwhmX').value='8';el('manualSpotFwhmY').value='8';el('beamOverrideEnabled').checked=false;el('beamEnergyScale').value='100';el('beamEnergyOffset').value='0';el('beamSpotScale').value='100';el('beamEnergySpread').value='0';toggleBeamInputMode();selectAllEnergyLayers(true);el('cacheRun').value='';closeRunModal();invalidateLineDose();await refreshTpsDoses(true);if(selectLatest)await useLatestMC(false);else el('mc_binary').value='';await refreshResults();if(notify)toast('Defaults restored. TPS RTDOSE returned to PLAN / PHYSICAL. Particle-calibrated result display is preferred when a commissioned run is available; case files, machine models and cached results were not changed.')}
async function refreshStatus(){try{const j=await request('/api/status?root='+encodeURIComponent(el('root').value));el('status').innerHTML=j.status.map(s=>`<tr><td>${esc(s.stage)}</td><td class="${s.state.toLowerCase()}">${esc(s.state)}</td><td>${esc(s.detail)}</td></tr>`).join('');for(const modality of ['CT','RTPLAN','RTDOSE','RTSTRUCT'])el('count_'+modality).textContent=`${j.dicom_counts[modality]} file${j.dicom_counts[modality]===1?'':'s'}`}catch(e){toast(e.message,true)}}
async function poll(){try{const j=await request('/api/log?after='+cursor);cursor=j.cursor;const box=el('log');if(j.lines.length){box.textContent+=j.lines.join('');box.scrollTop=box.scrollHeight}updateProgress(j.progress);updateTaskControls(j);updateComputeStatus(j.compute,j);reattachToRunningTask(j);if(!j.running&&j.last_ok!==null&&lastDone!==j.cursor){lastDone=j.cursor;toast(j.last_ok?'Task completed successfully':j.last_error,true===!j.last_ok);refreshAll()}}catch(e){}setTimeout(poll,900)}
// A page reload must never change what the running task is doing. It cannot:
// the task is a thread plus a detached process group inside the server. All
// this does is put the form back the way the running task left it and freeze
// the transport-defining inputs, so a reloaded page cannot submit settings
// that disagree with the calculation already in flight.
const RUN_LOCKED_INPUTS=['histories','threads','seed','output_tag','topas_executable','beamModelMode','beamModelProfile','beamOverrideEnabled','beamEnergyScale','beamEnergyOffset','beamSpotScale','beamEnergySpread','manualEnergyMeVu','manualEnergySpread','manualSpotX','manualSpotY','manualSpotFwhmX','manualSpotFwhmY'];
function setRunLock(running){for(const id of RUN_LOCKED_INPUTS){const field=el(id);if(field)field.disabled=running}document.querySelectorAll('input[name="beamInputMode"]').forEach(r=>r.disabled=running);document.querySelectorAll('#energyLayerGrid input[type="checkbox"]').forEach(b=>b.disabled=running);document.querySelectorAll('#steps button').forEach(b=>b.disabled=running);el('runPipelineButton').disabled=running;el('runLockNote').hidden=!running;if(!running)applyDerivedFieldStates()}
function applyLayerSelection(value){if(!value)return;const boxes=energyLayerBoxes();if(!boxes.length){pendingLayerSelection=value;return}pendingLayerSelection='';if(value==='all'||value==='none'){for(const box of boxes)box.checked=value==='all'}else{const wanted=new Set(String(value).split(',').map(x=>x.trim()));for(const box of boxes)box.checked=wanted.has(box.value)}updateEnergyLayerSummary()}
function restoreFormState(s){const set=(id,key)=>{if(s[key]!==undefined&&el(id))el(id).value=s[key]};
 for(const [id,key] of [['histories','histories'],['threads','threads'],['seed','seed'],['profile_depth','profile_depth'],['gamma_dta_mm','gamma_dta_mm'],['gamma_dd_percent','gamma_dd_percent'],['topas_executable','topas_executable'],['mc_binary','mc_binary'],['beamModelMode','beam_model_mode'],['beamModelProfile','beam_model_profile'],['beamEnergyScale','beam_energy_scale_percent'],['beamEnergyOffset','beam_energy_offset_mevu'],['beamSpotScale','beam_spot_scale_percent'],['beamEnergySpread','beam_energy_spread_percent'],['manualEnergyMeVu','manual_energy_mevu'],['manualEnergySpread','manual_energy_spread_percent'],['manualSpotX','manual_spot_x_mm'],['manualSpotY','manual_spot_y_mm'],['manualSpotFwhmX','manual_spot_fwhm_x_mm'],['manualSpotFwhmY','manual_spot_fwhm_y_mm']])set(id,key);
 if(s.beam_override_enabled!==undefined)el('beamOverrideEnabled').checked=s.beam_override_enabled==='true';
 if(s.beam_input_mode){const radio=document.querySelector(`input[name="beamInputMode"][value="${s.beam_input_mode}"]`);if(radio)radio.checked=true}
 toggleBeamInputMode();
 // toggleBeamInputMode cascades into toggleBeamModelMode, which rewrites the
 // tag; restore the real one afterwards.
 set('output_tag','output_tag');
 applyLayerSelection(s.energy_layer_indices)}
function reattachToRunningTask(state){if(!state.running){if(reattached){reattached=false;setRunLock(false)}return}
 if(!reattached&&!submittedHere&&state.form_state&&Object.keys(state.form_state).length){restoreFormState(state.form_state);toast('Reattached to the task already running on this machine. Refreshing this page does not interrupt it.')}
 reattached=true;setRunLock(true)}
function updateCacheDeleteButton(){el('deleteCachedRunButton').disabled=!el('cacheRun').value}
async function refreshResultCache(){try{const j=await request('/api/result-cache?root='+encodeURIComponent(el('root').value)),select=el('cacheRun'),current=select.value;el('caseResultIdentity').textContent=`${j.identity.patientKey} / ${j.identity.planKey}`;select.innerHTML='<option value="">Current settings</option>'+j.runs.map(r=>`<option value="${esc(r.path)}" data-tag="${esc(r.tag)}" data-mc="${esc(r.mcPath)}" data-tps-dose-uid="${esc(r.tpsDoseUID||'')}">${esc(r.tag)} · ${r.hasGamma?'Gamma ':''}${r.hasProfiles?'Profiles ':''}${r.hasDicom?'DICOM ':''}${r.hasTopasRun?'TOPAS':''}</option>`).join('');if([...select.options].some(o=>o.value===current))select.value=current;updateCacheDeleteButton()}catch(e){el('caseResultIdentity').textContent='No standardized case cache yet';updateCacheDeleteButton()}}
async function deleteCachedRun(){const option=el('cacheRun').selectedOptions[0];if(!option||!option.value){toast('Select a cached run first',true);return}const label=option.dataset.tag||option.textContent,path=option.value;if(!confirm(`Delete cached result "${label}" from the Results list?\n\nThis does not delete DICOM, the current production dose or machine models. The run directory will be moved to this case's recoverable analysis/_trash folder. Deletion is blocked while this case is queued or calculating.`))return;try{const j=await request('/api/result-cache/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({root:el('root').value,run:path})});el('cacheRun').value='';updateCacheDeleteButton();invalidateLineDose();await useLatestMC(false);await Promise.all([refreshResultCache(),refreshResults()]);toast(j.message)}catch(e){toast(e.message,true)}}
async function loadCachedRun(){const option=el('cacheRun').selectedOptions[0];if(!option||!option.value){toast('Select a cached run',true);return}el('output_tag').value=option.dataset.tag||el('output_tag').value;if(option.dataset.mc)el('mc_binary').value=option.dataset.mc;if(option.dataset.tpsDoseUid&&[...el('tpsDose').options].some(item=>item.value===option.dataset.tpsDoseUid))el('tpsDose').value=option.dataset.tpsDoseUid;invalidateLineDose();await refreshResults();if(el('resultMode').value==='line-dose')await initializeLineDose(true);toast('Cached patient result and its TPS RTDOSE selection loaded')}
async function refreshResults(){const q=new URLSearchParams({root:el('root').value,tag:el('output_tag').value,direction:el('direction').value,run:el('cacheRun').value});try{const j=await request('/api/results?'+q);el('resultImage').src=j.image_url||'';el('resultImage').style.display=j.image_url?'block':'none';el('resultImage').alt=j.image_url?'Result plot':'No current result image';el('summary').textContent=j.summary||'No analysis summary yet.';el('gammaHeadline').textContent=j.gamma_pass_rate===null?'No Gamma result':`Gamma pass rate: ${Number(j.gamma_pass_rate).toFixed(2)}%`;el('gammaProtocol').textContent=j.gamma_protocol||'Run Gamma analysis to show the pass rate.'}catch(e){toast(e.message,true)}}
async function refreshAll(){await Promise.all([refreshStatus(),refreshResults(),refreshResultCache(),refreshEnergyLayers(),refreshTpsDoses(),refreshQueue(),refreshMachineModels(),refreshSshServer()])}function clearLog(){document.getElementById('log').textContent='';cursor=(lastDone||cursor)}function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]))}
function clampThreadInput(id){const field=el(id),value=Math.round(Number(field.value));if(Number.isFinite(value)&&value>MAX_THREADS){field.value=String(MAX_THREADS);toast(`Threads capped at the ${MAX_THREADS} logical CPUs on this Mac`,true)}}
el('threads').onchange=()=>clampThreadInput('threads');el('runCustomThreads').onchange=()=>{clampThreadInput('runCustomThreads');updateCustomEstimate()};
el('threads').value=String(Math.min(Number(el('threads').value)||1,MAX_THREADS));el('runCustomThreads').value=el('threads').value;
el('runLockUrl').textContent=location.origin+'/';
el('root').value='__ROOT__';refreshAll();useLatestMC(false);poll();setInterval(refreshQueue,1800);
</script></body></html>'''


class Handler(BaseHTTPRequestHandler):
    server_version = "TPSTOPASLocal/1.0"

    def log_message(self, _format: str, *_args) -> None:
        return

    def json_response(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def require_local_origin(self) -> None:
        port = self.server.server_address[1]
        expected_origin = f"http://127.0.0.1:{port}"
        expected_host = f"127.0.0.1:{port}"
        if self.headers.get("Origin") != expected_origin or self.headers.get("Host") != expected_host:
            raise RuntimeError("Rejected non-local or cross-origin action request")

    def json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError("JSON request body must be an object")
        return payload

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        try:
            if parsed.path == "/":
                topas = shutil.which("topas") or str(Path.home() / "bin" / "topas")
                body = (
                    HTML.replace("__ROOT__", str(STATE.root))
                    .replace("__TOPAS__", topas)
                    .replace("__MAX_THREADS__", str(logical_cpu_count()))
                    .encode("utf-8")
                )
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
                    "script-src 'unsafe-inline'; frame-ancestors 'none'",
                )
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/status":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                self.json_response(
                    {
                        "status": collect_status(root),
                        "dicom_counts": dicom_counts(root),
                        "logical_cpus": logical_cpu_count(),
                    }
                )
                return
            if parsed.path == "/api/log":
                self.json_response(STATE.snapshot(int(query.get("after", ["0"])[0])))
                return
            if parsed.path == "/api/queue":
                self.json_response(batch_manager().snapshot())
                return
            if parsed.path == "/api/queue/log":
                self.json_response(
                    batch_manager().read_log(
                        query.get("job_id", [""])[0],
                        int(query.get("after", ["0"])[0]),
                    )
                )
                return
            if parsed.path == "/api/latest-mc":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                path = discover_mc_binary(root)
                self.json_response({"path": str(path) if path else ""})
                return
            if parsed.path == "/api/result-cache":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                identity = case_identity(root)
                self.json_response(
                    {
                        "identity": {
                            "patientKey": identity.patient_key,
                            "planKey": identity.plan_key,
                            "patientId": identity.patient_id,
                            "planLabel": identity.plan_label,
                        },
                        "analysisPath": str(analysis_plan_dir(root)),
                        "runs": discover_cached_runs(root),
                    }
                )
                return
            if parsed.path == "/api/machine-models":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                self.json_response(list_machine_models(root))
                return
            if parsed.path == "/api/ssh-server":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                self.json_response(public_server_status(APP_ROOT, root))
                return
            if parsed.path == "/api/energy-layers":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                self.json_response(available_energy_layers(root))
                return
            if parsed.path == "/api/tps-doses":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                self.json_response(list_tps_doses(root))
                return
            if parsed.path == "/api/run-options":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                seed = int(query.get("seed", ["1699"])[0])
                beam_payload = {
                    "beam_input_mode": query.get("beam_input_mode", ["rtplan"])[0],
                    "beam_model_mode": query.get("beam_model_mode", ["baseline"])[0],
                    "beam_model_profile": query.get("beam_model_profile", [""])[0],
                    "beam_override_enabled": query.get("beam_override_enabled", ["false"])[0],
                    "beam_energy_scale_percent": query.get("beam_energy_scale_percent", ["100"])[0],
                    "beam_energy_offset_mevu": query.get("beam_energy_offset_mevu", ["0"])[0],
                    "beam_spot_scale_percent": query.get("beam_spot_scale_percent", ["100"])[0],
                    "beam_energy_spread_percent": query.get("beam_energy_spread_percent", ["0"])[0],
                    "manual_energy_mevu": query.get("manual_energy_mevu", [""])[0],
                    "manual_spot_x_mm": query.get("manual_spot_x_mm", [""])[0],
                    "manual_spot_y_mm": query.get("manual_spot_y_mm", [""])[0],
                    "manual_spot_fwhm_x_mm": query.get("manual_spot_fwhm_x_mm", [""])[0],
                    "manual_spot_fwhm_y_mm": query.get("manual_spot_fwhm_y_mm", [""])[0],
                    "manual_energy_spread_percent": query.get("manual_energy_spread_percent", ["0"])[0],
                    "energy_layer_indices": query.get("energy_layer_indices", ["all"])[0],
                }
                beam = beam_settings(beam_payload)
                validate_selected_beam_profile(root, beam)
                layers = selected_energy_layers(beam_payload, root)
                spots = requested_spot_count(root, beam, layers)
                options = run_configuration_options(
                    root,
                    str(beam.get("beam_model_mode", "baseline")),
                    spots,
                )
                for option in options:
                    option["requires_rebuild"] = not prepared_run_matches(
                        root,
                        int(option["histories"]),
                        int(option["threads"]),
                        seed,
                        beam,
                        layers,
                    )
                self.json_response(
                    {
                        "options": options,
                        "benchmark": {
                            "description": "Current-case completed TOPAS logs when available; legacy water-phantom fallback otherwise",
                        },
                    }
                )
                return
            if parsed.path == "/api/runtime-estimate":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                payload = {key: values[-1] for key, values in query.items()}
                context = runtime_context_from_payload(root, payload)
                self.json_response(context)
                return
            if parsed.path in {"/api/line-dose/meta", "/api/line-dose/frame"}:
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                mc_value = unquote(query.get("mc_binary", [""])[0]).strip()
                mc_path = Path(mc_value).expanduser().resolve() if mc_value else discover_mc_binary(root)
                tps_dose_uid = unquote(query.get("tps_dose_uid", [""])[0]).strip() or None
                dataset = get_line_dose_dataset(root, mc_path, tps_dose_uid)
                if parsed.path == "/api/line-dose/meta":
                    summary = dataset.summary()
                    expected = expected_mc_binary(root)
                    selected_is_expected = bool(
                        mc_path and expected and mc_path.resolve() == expected.resolve()
                    )
                    mc_stage = next(
                        (row for row in collect_status(root) if row["stage"] == "MC dose"),
                        None,
                    )
                    summary["mcIsCurrent"] = bool(
                        selected_is_expected and mc_stage and mc_stage["state"] == "READY"
                    )
                    summary["mcIsCachedExport"] = bool(mc_path and mc_path.suffix.casefold() == ".dcm")
                    self.json_response(summary)
                    return
                plane = query.get("plane", ["axial"])[0]
                index = int(query.get("index", ["0"])[0])
                frame = dataset.frame(plane, index)
                body = frame_binary(frame)
                encoded_meta = base64.b64encode(meta_header(frame.meta).encode("utf-8")).decode("ascii")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("X-Line-Dose-Meta", encoded_meta)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/results":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                tag = query.get("tag", [""])[0]
                direction = query.get("direction", ["depth_direction"])[0]
                if direction not in {
                    "depth_direction",
                    "transverse_x",
                    "transverse_y",
                    "gamma_map",
                    "gamma_pass_fail",
                }:
                    raise RuntimeError("Unknown result direction")
                run_value = unquote(query.get("run", [""])[0]).strip()
                run_dir = Path(run_value).expanduser().resolve() if run_value else analysis_run_dir(root, tag)
                try:
                    run_dir.relative_to(root / "analysis")
                except ValueError as exc:
                    raise RuntimeError("Cached result directory is outside this case analysis folder") from exc
                exact = run_dir / "figures" / f"{direction}_{tag}.png"
                if not exact.is_file() and not run_value:
                    exact = root / "analysis" / "figures" / f"{direction}_{tag}.png"
                image = exact if exact.is_file() else None
                is_gamma = direction.startswith("gamma_")
                if is_gamma:
                    summary_path = run_dir / "gamma" / f"gamma_summary_{tag}.txt"
                    if not summary_path.is_file() and not run_value:
                        summary_path = root / "analysis" / "gamma" / f"gamma_summary_{tag}.txt"
                    summary_path = summary_path if summary_path.is_file() else None
                else:
                    summary_path = run_dir / "profiles" / f"profile_export_summary_{tag}.txt"
                    if not summary_path.is_file() and not run_value:
                        summary_path = root / "analysis" / "profiles" / f"profile_export_summary_{tag}.txt"
                    summary_path = summary_path if summary_path.is_file() else None
                gamma_metrics_path = run_dir / "gamma" / f"gamma_metrics_{tag}.csv"
                if not gamma_metrics_path.is_file() and not run_value:
                    gamma_metrics_path = root / "analysis" / "gamma" / f"gamma_metrics_{tag}.csv"
                gamma_metrics_path = gamma_metrics_path if gamma_metrics_path.is_file() else None
                gamma_pass_rate = None
                gamma_protocol = ""
                if gamma_metrics_path:
                    with gamma_metrics_path.open(newline="", encoding="utf-8") as stream:
                        row = next(csv.DictReader(stream))
                    gamma_pass_rate = float(row["Gamma_pass_rate_percent"])
                    source_note = f"Current output tag: {tag}. "
                    gamma_protocol = (
                        source_note
                        + f"Global 3D: {float(row['DD_percent_global']):g}% / "
                        f"{float(row['DTA_mm']):g} mm; "
                        f"TPS threshold {float(row['Low_dose_threshold_percent_of_TPS_max']):g}%; "
                        + (
                            f"MC independently scaled by N_plan/N_sim = "
                            f"{float(row['MC_particle_scale_N_plan_over_N_sim']):.8g}"
                            if row.get("MC_particle_scale_N_plan_over_N_sim")
                            else "legacy result: MC was scaled to the TPS maximum"
                        )
                    )
                self.json_response(
                    {
                        "image_url": f"/file?path={quote(str(image))}&root={quote(str(root))}&v={image.stat().st_mtime_ns}" if image else "",
                        "summary": summary_path.read_text(encoding="utf-8") if summary_path else "",
                        "gamma_pass_rate": gamma_pass_rate,
                        "gamma_protocol": gamma_protocol,
                        "run_path": str(run_dir),
                    }
                )
                return
            if parsed.path == "/file":
                root = safe_root(query.get("root", [str(STATE.root)])[0])
                path = Path(unquote(query.get("path", [""])[0])).resolve()
                try:
                    path.relative_to(root)
                except ValueError as exc:
                    raise RuntimeError("File is outside the selected case") from exc
                if not path.is_file():
                    raise RuntimeError("File not found")
                body = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.json_response({"error": str(exc)}, 400)

    def do_POST(self) -> None:
        try:
            self.require_local_origin()
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            if parsed.path == "/api/ssh-server/config":
                payload = self.json_body()
                self.json_response(save_server_config(APP_ROOT, payload))
                return
            if parsed.path == "/api/ssh-server/select-identity":
                selected = choose_ssh_identity_file()
                self.json_response(
                    {"cancelled": selected is None, "path": str(selected) if selected else ""}
                )
                return
            if parsed.path == "/api/ssh-server/inspect-host-key":
                self.json_response(inspect_host_keys(APP_ROOT))
                return
            if parsed.path == "/api/ssh-server/trust-host-key":
                payload = self.json_body()
                self.json_response(
                    trust_host_key(
                        APP_ROOT,
                        str(payload.get("fingerprint", "")).strip(),
                        replace=bool(payload.get("replace", False)),
                    )
                )
                return
            if parsed.path == "/api/ssh-server/test":
                self.json_response(test_ssh_connection(APP_ROOT))
                return
            if parsed.path == "/api/ssh-server/environment":
                self.json_response(check_server_environment(APP_ROOT))
                return
            if parsed.path == "/api/machine-models/inspect":
                root = safe_root(unquote(query.get("root", [str(STATE.root)])[0]))
                original_name = Path(unquote(query.get("name", ["machine-model.zip"])[0])).name
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_MACHINE_PACKAGE_BYTES:
                    raise RuntimeError("Machine package is empty or exceeds the 512 MiB upload limit")
                stage = Path(tempfile.mkdtemp(prefix="plan1699-machine-model-"))
                archive = stage / "package.zip"
                try:
                    remaining = length
                    with archive.open("xb") as stream:
                        while remaining:
                            chunk = self.rfile.read(min(1024**2, remaining))
                            if not chunk:
                                raise RuntimeError("Machine package upload ended before completion")
                            stream.write(chunk)
                            remaining -= len(chunk)
                    package_root = extract_package(archive, stage / "extracted")
                    inspection = inspect_extracted_package(root, package_root)
                except Exception:
                    shutil.rmtree(stage, ignore_errors=True)
                    raise
                token = uuid.uuid4().hex
                with STATE.lock:
                    stale_tokens = [
                        old_token
                        for old_token, record in STATE.machine_imports.items()
                        if time.time() - float(record["created"]) > 3600.0
                    ]
                    stale_stages = [
                        Path(STATE.machine_imports.pop(old_token)["stage"])
                        for old_token in stale_tokens
                    ]
                    STATE.machine_imports[token] = {
                        "root": root,
                        "stage": stage,
                        "created": time.time(),
                        "original_name": original_name,
                        "inspection": inspection,
                    }
                for stale_stage in stale_stages:
                    shutil.rmtree(stale_stage, ignore_errors=True)
                public = {
                    key: value
                    for key, value in inspection.items()
                    if key not in {"packageRoot", "manifestPath", "manifest"}
                }
                public.update({"token": token, "originalName": original_name})
                self.json_response(public)
                return
            if parsed.path == "/api/machine-models/import":
                payload = self.json_body()
                token = str(payload.get("token", "")).strip()
                with STATE.lock:
                    record = STATE.machine_imports.get(token)
                if record is None:
                    raise RuntimeError("Machine package inspection is missing or expired")
                busy = active_machine_model_work()
                if busy:
                    raise RuntimeError(
                        f"Machine models cannot change while {busy}. The current calculation is left untouched."
                    )
                result = import_inspected_package(Path(record["root"]), record["inspection"])
                discard_machine_import(token)
                result["message"] = (
                    "Identical immutable model was already registered"
                    if result.get("alreadyImported")
                    else "Machine model package imported as an immutable version"
                )
                self.json_response(result)
                return
            if parsed.path == "/api/machine-models/status":
                payload = self.json_body()
                root = safe_root(str(payload.get("root", STATE.root)))
                busy = active_machine_model_work()
                if busy:
                    raise RuntimeError(
                        f"Machine model status cannot change while {busy}. The current calculation is left untouched."
                    )
                model = set_model_active(
                    root,
                    str(payload.get("model_id", "")),
                    bool(payload.get("active", False)),
                )
                self.json_response(
                    {
                        "model": model,
                        "message": "Model reactivated" if model["active"] else "Model deactivated; immutable files were preserved",
                    }
                )
                return
            if parsed.path == "/api/queue/select-case-folder":
                selected = choose_case_folder()
                if selected is None:
                    self.json_response({"cancelled": True})
                    return
                initialize_case(selected)
                self.json_response(
                    {
                        "cancelled": False,
                        "root": str(selected),
                        "counts": dicom_counts(selected),
                    }
                )
                return
            if parsed.path == "/api/queue/add-current":
                payload = self.json_body()
                root = safe_root(str(payload.get("root", STATE.root)))
                job = enqueue_case(root, payload, use_all_layers=False)
                self.json_response(
                    {"job": job, "message": f"Added {job['label']} to the waiting queue"}
                )
                return
            if parsed.path == "/api/queue/add-case":
                payload = self.json_body()
                root = safe_root(str(payload.get("root", "")))
                job = enqueue_case(root, payload, use_all_layers=True)
                self.json_response(
                    {"job": job, "message": f"Added {job['label']} to the waiting queue"}
                )
                return
            if parsed.path == "/api/queue/add-folders":
                payload = self.json_body()
                selected = choose_case_folders()
                if not selected:
                    self.json_response({"cancelled": True, "added": [], "errors": []})
                    return
                added: list[dict[str, Any]] = []
                errors: list[dict[str, str]] = []
                for root in selected:
                    try:
                        added.append(enqueue_case(root, payload, use_all_layers=True))
                    except Exception as exc:
                        errors.append({"root": str(root), "error": str(exc)})
                self.json_response(
                    {
                        "cancelled": False,
                        "added": added,
                        "errors": errors,
                        "message": f"Added {len(added)} case(s); {len(errors)} rejected",
                    }
                )
                return
            if parsed.path == "/api/queue/start":
                batch_manager().set_enabled(True)
                self.json_response({"message": "Queue auto-run started"})
                return
            if parsed.path == "/api/queue/stop-scheduling":
                batch_manager().set_enabled(False)
                self.json_response(
                    {"message": "Automatic starts stopped; active jobs continue unchanged"}
                )
                return
            if parsed.path == "/api/queue/settings":
                payload = self.json_body()
                batch_manager().set_max_parallel(integer(payload, "max_parallel"))
                self.json_response({"message": "Local concurrency updated"})
                return
            if parsed.path == "/api/queue/control":
                payload = self.json_body()
                message = batch_manager().control(
                    str(payload.get("job_id", "")),
                    str(payload.get("queue_action", "")),
                )
                self.json_response({"message": message})
                return
            if parsed.path == "/api/result-cache/delete":
                payload = self.json_body()
                root = safe_root(str(payload.get("root", STATE.root)))
                run_value = str(payload.get("run", "")).strip()
                if not run_value:
                    raise RuntimeError("Select one cached run to delete")
                busy = active_case_work(root)
                if busy:
                    raise RuntimeError(
                        f"Cached data cannot be changed while {busy}. Wait for completion or remove the waiting job first."
                    )
                record = trash_cached_run(root, Path(run_value))
                self.json_response(
                    {
                        "message": (
                            "Cached run removed from Results and moved to recoverable case trash"
                        ),
                        "trashPath": record["trash_directory"],
                        "outputTag": record["output_tag"],
                    }
                )
                return
            if parsed.path == "/api/mc-rtdose/import":
                root = safe_root(unquote(query.get("root", [str(STATE.root)])[0]))
                tag = unquote(query.get("tag", [""])[0]).strip()
                original = Path(unquote(query.get("name", ["MC_RTDose.dcm"])[0])).name
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > MAX_DICOM_BYTES:
                    raise RuntimeError("MC RTDOSE import is empty or exceeds the 2 GiB limit")
                run = analysis_run_dir(root, tag, create=True)
                safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", original) or "MC_RTDose.dcm"
                if not safe_name.casefold().endswith(".dcm"):
                    safe_name += ".dcm"
                stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
                destination = run / "dicom" / f"Imported_{stamp}_{safe_name}"
                try:
                    remaining = length
                    with destination.open("xb") as stream:
                        while remaining:
                            chunk = self.rfile.read(min(1024**2, remaining))
                            if not chunk:
                                raise RuntimeError("MC RTDOSE upload ended before the file was complete")
                            stream.write(chunk)
                            remaining -= len(chunk)
                    imported = pydicom.dcmread(destination, stop_before_pixels=True)
                    if str(getattr(imported, "Modality", "")).upper() != "RTDOSE":
                        raise RuntimeError("Selected file is not a DICOM RTDOSE object")
                    get_line_dose_dataset(root, destination)
                except Exception:
                    destination.unlink(missing_ok=True)
                    raise
                update_run_manifest(root, tag, mc_dicom=destination, additions={"imported_mc_rtdose": str(destination)})
                self.json_response({"path": str(destination), "message": "Compatible MC RTDOSE imported and cached"})
                return
            if parsed.path == "/api/mc-rtdose/export":
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                root = safe_root(str(payload.get("root", STATE.root)))
                mc_value = str(payload.get("mc_binary", "")).strip()
                mc_path = Path(mc_value).expanduser().resolve() if mc_value else discover_mc_binary(root)
                if not mc_path or not mc_path.is_file():
                    raise RuntimeError("Select a TOPAS binary or compatible MC RTDOSE first")
                tag = str(payload.get("output_tag", "")).strip()
                mode = str(payload.get("mode", "particle_calibrated"))
                result = export_mc_rtdose(root, mc_path, tag, mode)
                result["fileUrl"] = (
                    f"/file?path={quote(result['path'])}&root={quote(str(root))}&v={Path(result['path']).stat().st_mtime_ns}"
                )
                self.json_response(result)
                return
            if parsed.path in {"/api/line-dose/sample", "/api/line-dose/export"}:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                root = safe_root(str(payload.get("root", STATE.root)))
                mc_value = str(payload.get("mc_binary", "")).strip()
                mc_path = Path(mc_value).expanduser().resolve() if mc_value else discover_mc_binary(root)
                tps_dose_uid = str(payload.get("tps_dose_uid", "")).strip() or None
                dataset = get_line_dose_dataset(root, mc_path, tps_dose_uid)
                if parsed.path == "/api/line-dose/sample":
                    self.json_response(
                        dataset.profile(
                            payload.get("p1", []),
                            payload.get("p2", []),
                            int(payload.get("samples", 512)),
                            str(payload.get("normalization", "absolute")),
                        )
                    )
                    return
                body = profile_csv(dataset, payload).encode("utf-8")
                tag = str(payload.get("output_tag", "")).strip()
                run = analysis_run_dir(root, tag, create=True)
                stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
                saved = run / "line_dose" / f"line_dose_{tag}_{stamp}.csv"
                saved.write_bytes(body)
                update_run_manifest(root, tag, mc_source=mc_path, additions={"last_line_dose_csv": str(saved)})
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", 'attachment; filename="line_dose.csv"')
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Saved-Line-Dose", base64.b64encode(str(saved).encode("utf-8")).decode("ascii"))
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/select-case":
                selected = choose_case_folder()
                if selected is None:
                    self.json_response({"cancelled": True})
                    return
                initialize_case(selected)
                with STATE.lock:
                    STATE.root = selected
                self.json_response({"cancelled": False, "root": str(selected)})
                return
            if parsed.path == "/api/import/start":
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                root = safe_root(str(payload.get("root", STATE.root)))
                if batch_manager().case_is_active(root):
                    raise RuntimeError(
                        "This case is active in the batch queue; pause/cancel it before replacing DICOM"
                    )
                modality = str(payload.get("modality", "")).upper()
                if modality not in DICOM_MODALITIES:
                    raise RuntimeError("Unknown DICOM modality")
                count = integer(payload, "count")
                maximum = 10000 if modality == "CT" else (20 if modality == "RTDOSE" else 1)
                if count > maximum:
                    raise RuntimeError(f"Too many {modality} files selected: {count} (maximum {maximum})")
                token = uuid.uuid4().hex
                stage = Path(tempfile.mkdtemp(prefix=f"plan1699-{modality.lower()}-"))
                with STATE.lock:
                    stale_tokens = [
                        old_token
                        for old_token, old_batch in STATE.imports.items()
                        if time.time() - float(old_batch["created"]) > 3600.0
                    ]
                    stale_stages = [Path(STATE.imports.pop(old_token)["stage"]) for old_token in stale_tokens]
                    STATE.imports[token] = {
                        "root": root,
                        "modality": modality,
                        "count": count,
                        "replace": bool(payload.get("replace", False)),
                        "settings": payload.get("settings", {}) if isinstance(payload.get("settings"), dict) else {},
                        "stage": stage,
                        "received": {},
                        "bytes": 0,
                        "created": time.time(),
                    }
                for stale_stage in stale_stages:
                    shutil.rmtree(stale_stage, ignore_errors=True)
                self.json_response({"token": token})
                return
            if parsed.path == "/api/import/file":
                token = query.get("token", [""])[0]
                index = int(query.get("index", ["-1"])[0])
                original_name = Path(query.get("name", [""])[0]).name
                with STATE.lock:
                    batch = STATE.imports.get(token)
                if not batch:
                    raise RuntimeError("Import session is missing or expired")
                if not 0 <= index < int(batch["count"]):
                    raise RuntimeError("Import file index is out of range")
                if index in batch["received"]:
                    raise RuntimeError("Duplicate file in import session")
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or batch["bytes"] + length > MAX_DICOM_BYTES:
                    raise RuntimeError("DICOM import exceeds the 2 GiB safety limit")
                target = Path(batch["stage"]) / f"{index:05d}.dcm"
                try:
                    remaining = length
                    digest = hashlib.sha256()
                    with target.open("xb") as stream:
                        while remaining:
                            chunk = self.rfile.read(min(1024**2, remaining))
                            if not chunk:
                                raise RuntimeError(f"Upload stopped before {original_name} was complete")
                            stream.write(chunk)
                            digest.update(chunk)
                            remaining -= len(chunk)
                    record = validate_imported_dicom(target, str(batch["modality"]))
                    record.update(
                        {
                            "original_name": original_name,
                            "size_bytes": length,
                            "sha256": digest.hexdigest(),
                        }
                    )
                except Exception:
                    discard_import(token)
                    raise
                with STATE.lock:
                    batch["received"][index] = record
                    batch["bytes"] += length
                self.json_response({"received": index + 1})
                return
            if parsed.path == "/api/import/finish":
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                token = str(payload.get("token", ""))
                with STATE.lock:
                    batch = STATE.imports.pop(token, None)
                if not batch:
                    raise RuntimeError("Import session is missing or expired")
                try:
                    count, archive, case_changed, cache_path = commit_import_batch(
                        Path(batch["root"]), batch
                    )
                except Exception:
                    shutil.rmtree(Path(batch["stage"]), ignore_errors=True)
                    raise
                archive_note = f" Previous files archived at {archive}." if archive else ""
                patient_note = (
                    f" Previous patient settings/results cached at {cache_path}."
                    if case_changed and cache_path
                    else ""
                )
                self.json_response(
                    {
                        "message": (
                            f"Imported {count} {batch['modality']} file(s)."
                            f"{archive_note}{patient_note}"
                        ),
                        "counts": dicom_counts(Path(batch["root"])),
                        "caseChanged": case_changed,
                        "previousCache": str(cache_path) if cache_path else "",
                    }
                )
                return
            if self.path == "/api/pause":
                self.json_response({"message": pause_active_task(), "paused": True})
                return
            if self.path == "/api/resume":
                self.json_response({"message": resume_active_task(), "paused": False})
                return
            if self.path == "/api/stop":
                self.json_response({"message": stop_active_task(), "paused": False})
                return
            if self.path != "/api/action":
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            action = str(payload.get("action", ""))
            root = safe_root(str(payload.get("root", STATE.root)))
            if action == "open_output":
                tag = str(payload.get("output_tag", "")).strip()
                output = analysis_run_dir(root, tag, create=True)
                output.mkdir(parents=True, exist_ok=True)
                subprocess.Popen(["open", str(output)])
                self.json_response({"message": f"Opened {output}"})
                return
            with STATE.lock:
                if STATE.running:
                    raise RuntimeError(f"Another task is running: {STATE.task}")
            queue_state = batch_manager().snapshot()
            if queue_state["active"] or (queue_state["enabled"] and queue_state["queued"]):
                raise RuntimeError(
                    "The batch queue is active. Stop queue scheduling or use its per-case controls."
                )
            runtime_context = (
                runtime_context_from_payload(root, payload) if action == "run_topas" else None
            )
            title, commands = build_commands(action, payload)
            start_commands(
                title, commands, root, runtime_context,
                gui_form_state(payload), plan_warnings(root, payload),
            )
            self.json_response({"message": f"Started: {title}"})
        except Exception as exc:
            self.json_response({"error": str(exc)}, 400)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=APP_ROOT)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    return parser.parse_args()


def main() -> int:
    global STATE, BATCH
    args = parse_args()
    STATE = WorkflowState(args.root.expanduser().resolve())
    BATCH = BatchQueueManager(
        APP_ROOT / "analysis" / "_batch_queue" / "queue.json",
        initialize_case=initialize_case,
        build_action=build_commands,
        can_start=lambda: not STATE.running,
    )
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        if args.port == 0:
            raise
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"TPS-TOPAS GUI: {url}", flush=True)
    print("Keep this terminal window open while using the interface.", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
