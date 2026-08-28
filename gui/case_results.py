"""Standardized per-patient/per-plan analysis paths and persistent result index."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import unicodedata
import warnings
from typing import Any, Optional

import pydicom


SCHEMA_VERSION = 1


def _slug(value: object, fallback: str) -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return (text[:48] or fallback).strip("-")


def _uid_key(value: object) -> str:
    return hashlib.sha256(str(value or "missing").encode("utf-8")).hexdigest()[:10]


def _one_dicom(directory: Path, modality: str) -> tuple[Optional[Path], Optional[Any]]:
    matches: list[tuple[Path, Any]] = []
    for path in sorted(directory.glob("*.dcm")):
        try:
            dataset = pydicom.dcmread(path, stop_before_pixels=True)
        except Exception:
            continue
        if str(getattr(dataset, "Modality", "")).upper() == modality:
            matches.append((path.resolve(), dataset))
    return matches[0] if len(matches) == 1 else (None, None)


@dataclass(frozen=True)
class CaseIdentity:
    patient_key: str
    plan_key: str
    patient_id: str
    study_date: str
    study_uid: str
    plan_label: str
    plan_uid: str
    # False when neither RTPLAN nor CT could be read, i.e. the keys below are
    # the "anonymous"/"plan" placeholders rather than a real patient. Reading
    # this state is fine (an empty case folder is a normal thing to inspect);
    # writing a cache directory under it is not.
    identified: bool = True

    @property
    def relative_base(self) -> Path:
        return Path(self.patient_key) / self.plan_key


def require_identified_case(root: Path) -> CaseIdentity:
    """Case identity for anything that creates or moves cache directories.

    A placeholder identity silently collects results from unrelated plans into
    one `patient-anonymous--study-.../plan-plan--.../` tree, which is how runs
    tagged for one case ended up filed under another.
    """

    identity = case_identity(root)
    if not identity.identified:
        raise RuntimeError(
            f"Cannot determine the patient/plan identity of {root}: no readable RTPLAN or CT "
            "was found under its dicom/ folder. Import the DICOM for this case first — "
            "writing results under a placeholder identity mixes unrelated plans together."
        )
    return identity


def case_identity(root: Path) -> CaseIdentity:
    root = root.expanduser().resolve()
    _plan_path, plan = _one_dicom(root / "dicom" / "RTPLAN", "RTPLAN")
    ct_paths = sorted((root / "dicom" / "CT").glob("*.dcm"))
    ct = None
    if ct_paths:
        try:
            ct = pydicom.dcmread(ct_paths[0], stop_before_pixels=True)
        except Exception:
            ct = None
    source = plan or ct
    patient_id = str(getattr(source, "PatientID", "") or "anonymous")
    study_date = str(getattr(source, "StudyDate", "") or "undated")
    study_uid = str(getattr(source, "StudyInstanceUID", "") or "missing")
    # Some vendor exports contain a useful RTPlanLabel longer than the DICOM
    # SH limit.  The geometry/import checks remain responsible for reporting
    # that source conformance issue; cache-key generation should stay quiet.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"The value length .* exceeds the maximum length of 16 allowed for VR SH.*",
        )
        plan_label = str(
            getattr(plan, "RTPlanLabel", "")
            or getattr(plan, "RTPlanName", "")
            or "plan"
        )
    plan_uid = str(getattr(plan, "SOPInstanceUID", "") or "missing")
    patient_key = f"patient-{_slug(patient_id, 'anonymous')}--study-{_uid_key(study_uid)}"
    plan_key = f"plan-{_slug(plan_label, 'plan')}--{_uid_key(plan_uid)}"
    return CaseIdentity(
        patient_key=patient_key,
        plan_key=plan_key,
        patient_id=patient_id,
        study_date=study_date,
        study_uid=study_uid,
        plan_label=plan_label,
        plan_uid=plan_uid,
        identified=source is not None,
    )


def normalize_output_tag(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value or ""):
        raise RuntimeError("output_tag may contain only letters, digits, underscore and hyphen")
    return value


def analysis_plan_dir(root: Path) -> Path:
    identity = case_identity(root)
    return root.expanduser().resolve() / "analysis" / identity.relative_base


def analysis_run_dir(root: Path, output_tag: str, *, create: bool = False) -> Path:
    tag = normalize_output_tag(output_tag)
    if create:
        require_identified_case(root)
    path = analysis_plan_dir(root) / f"run-{tag}"
    if create:
        for name in ("figures", "profiles", "gamma", "calibration", "dicom", "line_dose", "topas_runs"):
            (path / name).mkdir(parents=True, exist_ok=True)
        update_run_manifest(root, tag)
    return path


def _iso_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def update_run_manifest(
    root: Path,
    output_tag: str,
    *,
    mc_source: Optional[Path] = None,
    mc_dicom: Optional[Path] = None,
    additions: Optional[dict[str, Any]] = None,
) -> Path:
    root = root.expanduser().resolve()
    tag = normalize_output_tag(output_tag)
    identity = require_identified_case(root)
    run = analysis_plan_dir(root) / f"run-{tag}"
    run.mkdir(parents=True, exist_ok=True)
    manifest = run / "manifest.json"
    payload: dict[str, Any] = {}
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
    created_at = payload.get("created_at", _iso_now())
    payload.update(
        {
            "schema_version": SCHEMA_VERSION,
            "output_tag": tag,
            "case_root": str(root),
            "analysis_directory": str(run),
            "identity": asdict(identity),
            "created_at": created_at,
            "updated_at": _iso_now(),
        }
    )
    if mc_source is not None:
        payload["mc_source"] = str(mc_source.expanduser().resolve())
    if mc_dicom is not None:
        payload["mc_dicom"] = str(mc_dicom.expanduser().resolve())
    if additions:
        payload.update(additions)
    temporary = manifest.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest)
    return manifest


def discover_cached_runs(root: Path) -> list[dict[str, Any]]:
    root = root.expanduser().resolve()
    base = analysis_plan_dir(root)
    results: list[dict[str, Any]] = []
    if not base.is_dir():
        return results
    for manifest in base.glob("run-*/manifest.json"):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        run = manifest.parent.resolve()
        mc_candidates = [
            Path(str(payload.get("mc_dicom", ""))),
            Path(str(payload.get("mc_source", ""))),
        ]
        selected = next((path.resolve() for path in mc_candidates if str(path) not in {"", "."} and path.is_file()), None)
        results.append(
            {
                "tag": str(payload.get("output_tag", run.name.removeprefix("run-"))),
                "path": str(run),
                "updatedAt": str(payload.get("updated_at", "")),
                "mcPath": str(selected) if selected else "",
                "mcDicom": str(payload.get("mc_dicom", "")),
                "tpsDoseUID": str(payload.get("selected_tps_dose_uid", "")),
                "tpsDosePath": str(payload.get("selected_tps_rtdose", "")),
                "hasProfiles": any((run / "profiles").glob("*.csv")),
                "hasGamma": any((run / "gamma").glob("gamma_metrics_*.csv")),
                "hasDicom": any((run / "dicom").glob("*.dcm")),
                "hasTopasRun": any((run / "topas_runs").glob("archived-*")),
            }
        )
    return sorted(results, key=lambda item: item["updatedAt"], reverse=True)


def trash_cached_run(root: Path, run_path: Path) -> dict[str, str]:
    """Remove one standardized run from the cache using a recoverable move."""
    root = root.expanduser().resolve()
    base = analysis_plan_dir(root).resolve()
    run = run_path.expanduser().resolve()
    if run.parent != base or not run.name.startswith("run-"):
        raise RuntimeError("Selected cache is not a direct standardized run for this patient/plan")
    manifest = run / "manifest.json"
    if not run.is_dir() or not manifest.is_file():
        raise RuntimeError("Selected cached run no longer exists or has no manifest")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Selected cached run has an unreadable manifest") from exc
    manifest_root = str(payload.get("case_root", "")).strip()
    if manifest_root and Path(manifest_root).expanduser().resolve() != root:
        raise RuntimeError("Cached-run manifest belongs to a different case root")

    identity = case_identity(root)
    trash_base = root / "analysis" / "_trash" / identity.patient_key / identity.plan_key
    trash_base.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S_%f")
    destination = trash_base / f"{run.name}--deleted-{stamp}"
    if destination.exists():
        raise RuntimeError("A cache trash destination unexpectedly already exists")
    shutil.move(str(run), str(destination))
    record = {
        "deleted_at": _iso_now(),
        "deletion_mode": "recoverable_move",
        "case_root": str(root),
        "source_cache": str(run),
        "trash_directory": str(destination),
        "output_tag": str(payload.get("output_tag", run.name.removeprefix("run-"))),
    }
    try:
        (destination / "deletion.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        if destination.exists() and not run.exists():
            shutil.move(str(destination), str(run))
        raise
    return {key: str(value) for key, value in record.items()}


def expected_production_output(root: Path) -> Optional[Path]:
    root = root.expanduser().resolve()
    scorer = root / "topas" / "scoring" / "dose.txt"
    if not scorer.is_file():
        return None
    match = re.search(
        r'^s:Sc/TPSDoseToMedium/OutputFile = "([^"]+)"$',
        scorer.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        return None
    return (root / "topas" / match.group(1)).resolve().with_suffix(".bin")


def archive_production_output(
    root: Path,
    output_tag: str,
    *,
    output_binary: Optional[Path] = None,
    reason: str = "Preparing another TOPAS run",
) -> Optional[dict[str, Any]]:
    """Move a collision-prone production result into its patient/run cache."""
    root = root.expanduser().resolve()
    tag = normalize_output_tag(output_tag)
    binary = (output_binary or expected_production_output(root))
    if binary is None:
        return None
    binary = binary.expanduser().resolve()
    header = Path(str(binary) + "header")
    sources = [path for path in (binary, header) if path.is_file()]
    if not sources:
        return None

    run = analysis_run_dir(root, tag, create=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S_%f")
    destination = run / "topas_runs" / f"archived-{stamp}"
    dose_dir = destination / "dose"
    config_dir = destination / "configuration"
    dose_dir.mkdir(parents=True, exist_ok=False)
    config_dir.mkdir(parents=True, exist_ok=False)
    moved: list[tuple[Path, Path]] = []
    copied: list[str] = []
    try:
        for source in sources:
            target = dose_dir / source.name
            shutil.move(str(source), str(target))
            moved.append((source, target))
        configuration_sources = (
            root / "plan_parsed" / "topas_plan_generation_summary.txt",
            root / "plan_parsed" / "topas_run_preparation_summary.txt",
            root / "plan_parsed" / "spot_history_allocation.csv",
            root / "plan_parsed" / "spot_history_allocation_metadata.json",
            root / "topas" / "run_full_plan_qa.txt",
            root / "topas" / "beam" / "plan_generated.txt",
        )
        for source in configuration_sources:
            if source.is_file():
                shutil.copy2(source, config_dir / source.name)
                copied.append(str(source))
        allocation_metadata = root / "plan_parsed" / "spot_history_allocation_metadata.json"
        if allocation_metadata.is_file():
            try:
                metadata = json.loads(allocation_metadata.read_text(encoding="utf-8"))
                machine_record = metadata.get("machine_calibration") or {}
                profile_source = Path(str(machine_record.get("profile_file", ""))).expanduser()
                if profile_source.is_file():
                    snapshot = config_dir / "machine_model"
                    shutil.copytree(profile_source.parent, snapshot)
                    copied.append(str(profile_source.parent))
            except (OSError, ValueError, TypeError):
                # The calibration resolver will reject an incomplete binding;
                # archiving the dose itself must remain recoverable.
                pass
        logs = sorted(
            (root / "topas_output" / "production").glob("run_full_plan_qa_*.log"),
            key=lambda item: item.stat().st_mtime_ns,
        )
        if logs:
            shutil.copy2(logs[-1], config_dir / logs[-1].name)
            copied.append(str(logs[-1]))
        archived_binary = next(
            (target for source, target in moved if source == binary),
            None,
        )
        record = {
            "archived_at": _iso_now(),
            "reason": reason,
            "source_output": str(binary),
            "archived_directory": str(destination),
            "archived_binary": str(archived_binary) if archived_binary else "",
            "moved_files": [str(target) for _source, target in moved],
            "copied_configuration_files": copied,
        }
        audit = destination / "archive.json"
        audit.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        update_run_manifest(
            root,
            tag,
            mc_source=archived_binary,
            additions={"last_archived_topas_run": record},
        )
        return record
    except Exception:
        for source, target in reversed(moved):
            if target.exists() and not source.exists():
                shutil.move(str(target), str(source))
        shutil.rmtree(destination, ignore_errors=True)
        raise


def snapshot_run_settings(root: Path, output_tag: str, settings: dict[str, Any]) -> Path:
    """Persist the user-facing calculation settings before a patient/plan switch."""
    allowed = {
        "histories",
        "threads",
        "seed",
        "profile_depth",
        "gamma_dta_mm",
        "gamma_dd_percent",
        "output_tag",
        "topas_executable",
        "mc_binary",
        "tps_dose_uid",
        "beam_input_mode",
        "beam_model_mode",
        "beam_model_profile",
        "beam_override_enabled",
        "beam_energy_scale_percent",
        "beam_energy_offset_mevu",
        "beam_spot_scale_percent",
        "beam_energy_spread_percent",
        "manual_energy_mevu",
        "manual_energy_spread_percent",
        "manual_spot_x_mm",
        "manual_spot_y_mm",
        "manual_spot_fwhm_x_mm",
        "manual_spot_fwhm_y_mm",
        "energy_layer_indices",
    }
    snapshot = {key: settings[key] for key in sorted(allowed) if key in settings}
    snapshot["saved_at"] = _iso_now()
    run = analysis_run_dir(root, output_tag, create=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S_%f")
    path = run / "topas_runs" / f"settings-{stamp}.json"
    path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    update_run_manifest(
        root,
        output_tag,
        additions={"last_settings_snapshot": {"path": str(path), **snapshot}},
    )
    return path
