"""Inspection and immutable registration of TPS-TOPAS machine model packages.

Package inspection is deliberately read-only with respect to ``machine_model``.
Only :func:`import_inspected_package` writes an accepted package, and it never
overwrites model content that is already present.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import threading
from typing import Any
import zipfile

import pydicom

from scripts.utils.commissioned_beam import CommissionedBeamModel, sha256


PACKAGE_MANIFEST = "machine_package.json"
REGISTRY_SCHEMA = 1
REGISTRY_NAME = "model_registry.json"
MAX_PACKAGE_FILES = 2_000
MAX_PACKAGE_UNCOMPRESSED_BYTES = 2 * 1024**3
PACKAGE_KINDS = {
    "beam_commissioning": "Beam commissioning",
    "ct_calibration": "CT HU-material/RSP calibration",
    "nozzle_geometry": "MRF/nozzle geometry and WET",
    "absolute_output_calibration": "Absolute output calibration",
}
BEAM_REQUIRED_FILES = {
    "profile",
    "particle_calibration",
    "energy_spectrum",
    "phase_space",
    "number_per_mu",
    "measured_idd",
    "measured_spot_sigma",
    "energy_list",
}
BEAM_REQUIRED_UNITS = {
    "energy_spectrum": "total MeV per carbon ion",
    "phase_space_position_sigma": "mm",
    "phase_space_angular_sigma": "rad",
    "number_per_mu": "primary carbon ions per MU",
    "measured_idd_depth": "mm",
    "measured_spot_sigma": "mm",
    "commissioned_energy": "MeV/u",
}
_REGISTRY_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe_component(value: str, label: str) -> str:
    raw = str(value).strip()
    if not raw:
        raise RuntimeError(f"Package {label} is missing")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip(".-_")
    if not safe:
        raise RuntimeError(f"Package {label} has no safe filename characters")
    return safe[:100]


def _report(rows: list[dict[str, str]], level: str, check: str, detail: str) -> None:
    rows.append({"level": level, "check": check, "detail": detail})


def _json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read {label}: {path}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} must contain a JSON object")
    return payload


def _relative_file(package_root: Path, value: object, label: str) -> Path:
    relative = PurePosixPath(str(value).strip())
    if not str(relative) or relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"Unsafe package path for {label}: {value!r}")
    path = (package_root / Path(*relative.parts)).resolve()
    try:
        path.relative_to(package_root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Package path escapes its root for {label}") from exc
    if not path.is_file():
        raise RuntimeError(f"Package file is missing for {label}: {relative}")
    return path


def extract_package(archive: Path, destination: Path) -> Path:
    """Safely extract a ZIP and return the directory containing its manifest."""
    archive = archive.expanduser().resolve()
    destination = destination.expanduser().resolve()
    if not archive.is_file() or not zipfile.is_zipfile(archive):
        raise RuntimeError("Machine model package must be a readable ZIP archive")
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        members = [item for item in source.infolist() if not item.is_dir()]
        if not members or len(members) > MAX_PACKAGE_FILES:
            raise RuntimeError(
                f"Package must contain 1–{MAX_PACKAGE_FILES} files (found {len(members)})"
            )
        total = sum(max(0, item.file_size) for item in members)
        if total > MAX_PACKAGE_UNCOMPRESSED_BYTES:
            raise RuntimeError("Package uncompressed content exceeds the 2 GiB safety limit")
        for member in source.infolist():
            relative = PurePosixPath(member.filename)
            if (
                not str(relative)
                or relative.is_absolute()
                or ".." in relative.parts
                or "" in relative.parts
            ):
                raise RuntimeError(f"Unsafe path in ZIP package: {member.filename!r}")
            unix_mode = (member.external_attr >> 16) & 0o170000
            if unix_mode == stat.S_IFLNK:
                raise RuntimeError(f"Symbolic links are not allowed in packages: {member.filename}")
            target = (destination / Path(*relative.parts)).resolve()
            try:
                target.relative_to(destination)
            except ValueError as exc:
                raise RuntimeError(f"ZIP path escapes staging folder: {member.filename}") from exc
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open(member) as input_stream, target.open("xb") as output_stream:
                shutil.copyfileobj(input_stream, output_stream, length=1024 * 1024)
    manifests = sorted(destination.rglob(PACKAGE_MANIFEST))
    if len(manifests) != 1:
        raise RuntimeError(
            f"Package must contain exactly one {PACKAGE_MANIFEST} (found {len(manifests)})"
        )
    package_root = manifests[0].parent.resolve()
    extra_top_level = [
        path for path in destination.iterdir() if path.resolve() != package_root
    ]
    if package_root != destination and extra_top_level:
        raise RuntimeError("A package may use one enclosing folder but cannot contain sibling content")
    return package_root


def _rtplan_context(root: Path) -> dict[str, Any]:
    plans = sorted((root / "dicom" / "RTPLAN").glob("*.dcm"))
    if len(plans) != 1:
        return {
            "available": False,
            "detail": f"Expected one current RTPLAN; found {len(plans)}",
            "machineName": "",
            "energiesMeVu": [],
            "vsadMm": [],
        }
    try:
        dataset = pydicom.dcmread(plans[0], stop_before_pixels=True)
        beams = list(getattr(dataset, "IonBeamSequence", []))
        machines = {
            str(getattr(beam, "TreatmentMachineName", "")).strip()
            for beam in beams
            if str(getattr(beam, "TreatmentMachineName", "")).strip()
        }
        vsads = {
            tuple(float(value) for value in getattr(beam, "VirtualSourceAxisDistances", []))
            for beam in beams
            if len(getattr(beam, "VirtualSourceAxisDistances", [])) == 2
        }
        energies = sorted(
            {
                float(getattr(control, "NominalBeamEnergy"))
                for beam in beams
                for control in getattr(beam, "IonControlPointSequence", [])
                if hasattr(control, "NominalBeamEnergy")
            }
        )
        if len(machines) != 1 or len(vsads) != 1:
            raise RuntimeError(
                f"RTPLAN must have one TreatmentMachineName and one X/Y VSAD pair; "
                f"found machines={sorted(machines)}, VSAD pairs={sorted(vsads)}"
            )
        return {
            "available": True,
            "path": str(plans[0].resolve()),
            "machineName": next(iter(machines)),
            "energiesMeVu": energies,
            "vsadMm": list(next(iter(vsads))),
            "detail": f"{next(iter(machines))}; {len(energies)} distinct energies",
        }
    except Exception as exc:
        return {
            "available": False,
            "detail": f"Current RTPLAN could not be inspected: {exc}",
            "machineName": "",
            "energiesMeVu": [],
            "vsadMm": [],
        }


def inspect_extracted_package(root: Path, package_root: Path) -> dict[str, Any]:
    """Inspect extracted package content without writing to ``machine_model``."""
    root = root.expanduser().resolve()
    package_root = package_root.expanduser().resolve()
    rows: list[dict[str, str]] = []
    manifest_path = package_root / PACKAGE_MANIFEST
    manifest = _json(manifest_path, PACKAGE_MANIFEST)
    if int(manifest.get("schema_version", 0)) != 1:
        _report(rows, "BLOCK", "Package schema", "schema_version must be 1")
    else:
        _report(rows, "PASS", "Package schema", "schema_version 1")

    kind = str(manifest.get("package_kind", "")).strip()
    if kind not in PACKAGE_KINDS:
        _report(rows, "BLOCK", "Package kind", f"Unsupported package_kind {kind!r}")
    else:
        _report(rows, "PASS", "Package kind", PACKAGE_KINDS[kind])
    subject = manifest.get("subject")
    if not isinstance(subject, dict):
        subject = {}
        _report(rows, "BLOCK", "Subject", "subject must be a JSON object")
    version = str(manifest.get("package_version", "")).strip()
    try:
        _safe_component(version, "version")
        _report(rows, "PASS", "Version", version)
    except RuntimeError as exc:
        _report(rows, "BLOCK", "Version", str(exc))

    provenance = manifest.get("provenance")
    if not isinstance(provenance, dict) or not str(provenance.get("source", "")).strip():
        _report(rows, "BLOCK", "Provenance", "provenance.source is required")
    else:
        _report(rows, "PASS", "Provenance", str(provenance.get("source")))
    approval = manifest.get("approval")
    approval_ok = bool(
        isinstance(approval, dict)
        and str(approval.get("status", "")).strip().lower() == "approved"
        and str(approval.get("approved_by", "")).strip()
        and str(approval.get("approved_at", "")).strip()
        and str(approval.get("evidence", "")).strip()
    )
    _report(
        rows,
        "PASS" if approval_ok else "BLOCK",
        "Approval record",
        (
            f"Approved by {approval.get('approved_by')} at {approval.get('approved_at')}"
            if approval_ok
            else "status=approved, approved_by, approved_at and evidence are required"
        ),
    )

    files = manifest.get("files")
    hashes = manifest.get("sha256")
    if not isinstance(files, dict) or not files:
        files = {}
        _report(rows, "BLOCK", "File inventory", "files must be a non-empty object")
    if not isinstance(hashes, dict):
        hashes = {}
        _report(rows, "BLOCK", "SHA-256 inventory", "sha256 must be an object")
    declared_paths: dict[str, Path] = {}
    for key, value in files.items():
        try:
            path = _relative_file(package_root, value, f"files.{key}")
            declared_paths[str(key)] = path
            expected = str(hashes.get(key, "")).strip().lower()
            actual = sha256(path)
            if not re.fullmatch(r"[a-f0-9]{64}", expected) or expected != actual:
                _report(
                    rows,
                    "BLOCK",
                    f"SHA-256 · {key}",
                    f"expected {expected or 'MISSING'}, actual {actual}",
                )
            else:
                _report(rows, "PASS", f"SHA-256 · {key}", actual)
        except RuntimeError as exc:
            _report(rows, "BLOCK", f"File · {key}", str(exc))
    actual_content = {
        path.resolve()
        for path in package_root.rglob("*")
        if path.is_file() and path.name != PACKAGE_MANIFEST
    }
    unlisted = actual_content.difference(set(declared_paths.values()))
    duplicated = len(declared_paths) != len(set(declared_paths.values()))
    if unlisted or duplicated:
        detail = []
        if unlisted:
            detail.append("unlisted: " + ", ".join(str(p.relative_to(package_root)) for p in sorted(unlisted)))
        if duplicated:
            detail.append("one physical file is assigned to multiple logical names")
        _report(rows, "BLOCK", "Complete file inventory", "; ".join(detail))
    elif declared_paths:
        _report(rows, "PASS", "Complete file inventory", f"All {len(declared_paths)} content files are hashed")

    units = manifest.get("units")
    if not isinstance(units, dict) or not units:
        units = {}
        _report(rows, "BLOCK", "Units", "A non-empty units object is required")
    else:
        _report(rows, "PASS", "Units", f"{len(units)} declared unit fields")

    machine_name = str(subject.get("treatment_machine_name", "")).strip()
    asset_id = str(subject.get("asset_id", "")).strip()
    model_fingerprint = ""
    rtplan = _rtplan_context(root)
    if kind == "beam_commissioning":
        missing = sorted(BEAM_REQUIRED_FILES.difference(files))
        if missing:
            _report(rows, "BLOCK", "Beam file set", "Missing logical files: " + ", ".join(missing))
        else:
            _report(rows, "PASS", "Beam file set", "All eight required logical files are present")
        for key, expected in BEAM_REQUIRED_UNITS.items():
            actual = str(units.get(key, "")).strip()
            _report(
                rows,
                "PASS" if actual == expected else "BLOCK",
                f"Unit · {key}",
                actual if actual == expected else f"expected {expected!r}, got {actual or 'MISSING'!r}",
            )
        try:
            profile_path = declared_paths.get("profile")
            particle_path = declared_paths.get("particle_calibration")
            if profile_path is None or particle_path is None:
                raise RuntimeError("profile or particle_calibration file is unavailable")
            model = CommissionedBeamModel(profile_path)
            calibration = model.particle_calibration()
            model_fingerprint = model.fingerprint
            if machine_name != model.machine_name:
                raise RuntimeError(
                    f"manifest machine {machine_name!r} does not equal profile machine {model.machine_name!r}"
                )
            if particle_path.resolve() != calibration.binding_path:
                raise RuntimeError("manifest particle_calibration does not point to the profile binding")
            profile_units = model.profile.get("units", {})
            for key in ("energy_spectrum", "phase_space_position_sigma", "phase_space_angular_sigma"):
                if str(profile_units.get(key, "")) != str(units.get(key, "")):
                    raise RuntimeError(f"manifest/profile unit mismatch for {key}")
            _report(
                rows,
                "PASS",
                "Commissioned model validation",
                f"{model.machine_name}; fingerprint {model.fingerprint[:16]}…; measured spot audit passed",
            )
            if not math.isclose(calibration.dose_output_correction_factor, 1.0):
                _report(
                    rows,
                    "BLOCK",
                    "Particle calibration binding",
                    "A beam package may bind NF(E) but may not embed a non-identity absolute-output correction. "
                    "Register that correction and its measurement evidence as an absolute_output_calibration package.",
                )
            else:
                _report(
                    rows,
                    "PASS",
                    "Particle calibration binding",
                    f"NF(E) and profile hashes bound; identity output factor "
                    f"({calibration.dose_output_correction_status})",
                )
            if rtplan["available"]:
                try:
                    model.validate_rtplan(rtplan["machineName"], rtplan["vsadMm"])
                    _report(
                        rows,
                        "PASS",
                        "RTPLAN machine + VSAD",
                        f"Exact machine match; RTPLAN {rtplan['vsadMm']} mm, model {model.expected_vsad_mm.tolist()} mm",
                    )
                except RuntimeError as exc:
                    _report(rows, "BLOCK", "RTPLAN machine + VSAD", str(exc))
                energy_failures: list[str] = []
                for energy in rtplan["energiesMeVu"]:
                    for method in (model.spectrum, model.phase, model.number_per_mu):
                        try:
                            method(float(energy))
                        except RuntimeError as exc:
                            energy_failures.append(str(exc))
                            break
                if energy_failures:
                    _report(rows, "BLOCK", "RTPLAN energy coverage", energy_failures[0])
                else:
                    energies = rtplan["energiesMeVu"]
                    _report(
                        rows,
                        "PASS",
                        "RTPLAN energy coverage",
                        f"All {len(energies)} RTPLAN energies covered"
                        + (f" ({min(energies):g}–{max(energies):g} MeV/u)" if energies else ""),
                    )
            else:
                _report(rows, "WARN", "RTPLAN compatibility", rtplan["detail"])
        except Exception as exc:
            _report(rows, "BLOCK", "Commissioned model validation", str(exc))
    elif kind in PACKAGE_KINDS:
        if not asset_id:
            _report(rows, "BLOCK", "Independent asset ID", "subject.asset_id is required")
        else:
            _report(rows, "PASS", "Independent asset ID", asset_id)
        if kind in {"nozzle_geometry", "absolute_output_calibration"} and not machine_name:
            _report(rows, "BLOCK", "Treatment machine", "subject.treatment_machine_name is required")
        elif machine_name:
            _report(rows, "PASS", "Treatment machine", machine_name)
        if kind == "ct_calibration":
            scanner = str(subject.get("scanner_name", "")).strip()
            protocol = str(subject.get("scan_protocol", "")).strip()
            _report(
                rows,
                "PASS" if scanner and protocol else "BLOCK",
                "CT scanner + protocol",
                f"{scanner} · {protocol}" if scanner and protocol else "subject.scanner_name and subject.scan_protocol are required",
            )
        if kind == "nozzle_geometry":
            nozzle = str(subject.get("nozzle_id", "")).strip()
            _report(
                rows,
                "PASS" if nozzle else "BLOCK",
                "Nozzle / MRF identity",
                nozzle or "subject.nozzle_id is required",
            )
        if kind == "absolute_output_calibration":
            protocol = str(subject.get("calibration_protocol", "")).strip()
            _report(
                rows,
                "PASS" if protocol else "BLOCK",
                "Absolute-output protocol",
                protocol or "subject.calibration_protocol is required",
            )
        _report(
            rows,
            "WARN",
            "Calculation binding",
            "This independent asset will be registered and audited, but it is not automatically applied to transport until its dedicated calculation binding is configured.",
        )

    content_digest = hashlib.sha256()
    content_digest.update(manifest_path.read_bytes())
    for key in sorted(declared_paths):
        content_digest.update(key.encode("utf-8"))
        content_digest.update(sha256(declared_paths[key]).encode("ascii"))
    package_fingerprint = content_digest.hexdigest()
    blocking = sum(row["level"] == "BLOCK" for row in rows)
    warning = sum(row["level"] == "WARN" for row in rows)
    identifier = machine_name if kind == "beam_commissioning" else (asset_id or machine_name)
    return {
        "schemaVersion": 1,
        "packageRoot": str(package_root),
        "manifestPath": str(manifest_path),
        "kind": kind,
        "kindLabel": PACKAGE_KINDS.get(kind, kind or "Unknown"),
        "identifier": identifier,
        "machineName": machine_name,
        "version": version,
        "packageFingerprint": package_fingerprint,
        "modelFingerprint": model_fingerprint,
        "rtplan": rtplan,
        "report": rows,
        "summary": {"pass": sum(row["level"] == "PASS" for row in rows), "warn": warning, "block": blocking},
        "importAllowed": blocking == 0,
        "manifest": manifest,
    }


def _registry_path(root: Path) -> Path:
    return root.resolve() / "machine_model" / REGISTRY_NAME


def _load_registry(root: Path) -> dict[str, Any]:
    path = _registry_path(root)
    if not path.is_file():
        return {"schema_version": REGISTRY_SCHEMA, "updated_at": _now(), "entries": []}
    payload = _json(path, "machine model registry")
    if int(payload.get("schema_version", 0)) != REGISTRY_SCHEMA or not isinstance(payload.get("entries"), list):
        raise RuntimeError(f"Unsupported or invalid machine model registry: {path}")
    return payload


def _write_registry(root: Path, payload: dict[str, Any]) -> None:
    path = _registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload["schema_version"] = REGISTRY_SCHEMA
    payload["updated_at"] = _now()
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=".model-registry-", delete=False
    ) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def _historical_references(root: Path, profile: Path | None, fingerprints: set[str]) -> list[str]:
    results: list[str] = []
    profile_text = str(profile.resolve()) if profile else ""
    for manifest_path in sorted((root / "analysis").glob("**/manifest.json")):
        try:
            text = manifest_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if (profile_text and profile_text in text) or any(value and value in text for value in fingerprints):
            results.append(str(manifest_path.resolve()))
    return results


def _entry_view(root: Path, entry: dict[str, Any]) -> dict[str, Any]:
    path_value = str(entry.get("path", ""))
    content_path = (root / path_value).resolve() if path_value else None
    profile_value = str(entry.get("profile", ""))
    profile = (root / profile_value).resolve() if profile_value else None
    fingerprints = {
        str(entry.get("package_fingerprint", "")),
        str(entry.get("model_fingerprint", "")),
    }
    references = _historical_references(root, profile, fingerprints)
    return {
        "id": str(entry.get("id", "")),
        "kind": str(entry.get("kind", "")),
        "kindLabel": PACKAGE_KINDS.get(str(entry.get("kind", "")), str(entry.get("kind", ""))),
        "identifier": str(entry.get("identifier", "")),
        "machineName": str(entry.get("machine_name", "")),
        "version": str(entry.get("version", "")),
        "active": bool(entry.get("active", True)),
        "legacy": bool(entry.get("legacy", False)),
        "path": str(content_path) if content_path else "",
        "profile": str(profile) if profile else "",
        "packageFingerprint": str(entry.get("package_fingerprint", "")),
        "modelFingerprint": str(entry.get("model_fingerprint", "")),
        "importedAt": str(entry.get("imported_at", "")),
        "provenance": entry.get("provenance", {}),
        "approval": entry.get("approval", {}),
        "referenceCount": len(references),
        "references": references,
        "contentPresent": bool(content_path and content_path.is_dir()),
    }


def list_machine_models(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    with _REGISTRY_LOCK:
        registry = _load_registry(root)
        entries = [dict(item) for item in registry["entries"] if isinstance(item, dict)]
    known_profiles = {
        str((root / str(entry.get("profile", ""))).resolve())
        for entry in entries
        if entry.get("profile")
    }
    known_model_fingerprints = {
        str(entry.get("model_fingerprint", ""))
        for entry in entries
        if str(entry.get("model_fingerprint", ""))
    }
    base = root / "machine_model" / "beam_commissioning"
    for profile_path in sorted(base.glob("*/profile.json")):
        if str(profile_path.resolve()) in known_profiles:
            continue
        try:
            model = CommissionedBeamModel(profile_path)
            model.particle_calibration()
        except Exception:
            continue
        if model.fingerprint in known_model_fingerprints:
            continue
        entries.append(
            {
                "id": "legacy-" + model.fingerprint[:16],
                "kind": "beam_commissioning",
                "identifier": model.machine_name,
                "machine_name": model.machine_name,
                "version": profile_path.parent.name,
                "active": True,
                "legacy": True,
                "path": str(profile_path.parent.relative_to(root)),
                "profile": str(profile_path.relative_to(root)),
                "package_fingerprint": "",
                "model_fingerprint": model.fingerprint,
                "imported_at": "legacy/unregistered",
                "provenance": model.profile.get("provenance", {}),
                "approval": {},
            }
        )
    views = [_entry_view(root, entry) for entry in entries]
    views.sort(key=lambda item: (item["kind"], item["identifier"], item["version"], item["id"]))
    rtplan = _rtplan_context(root)
    compatible = [
        item
        for item in views
        if item["kind"] == "beam_commissioning"
        and item["active"]
        and item["contentPresent"]
        and (not rtplan["machineName"] or item["machineName"] == rtplan["machineName"])
    ]
    return {
        "schemaVersion": REGISTRY_SCHEMA,
        "rtplan": rtplan,
        "models": views,
        "compatibleBeamProfiles": compatible,
        "autoSelection": (
            compatible[0]["profile"] if len(compatible) == 1 else ""
        ),
        "explicitSelectionRequired": len(compatible) > 1,
    }


def import_inspected_package(root: Path, inspection: dict[str, Any]) -> dict[str, Any]:
    """Copy a re-inspected package into immutable content-addressed storage."""
    root = root.expanduser().resolve()
    package_root = Path(str(inspection.get("packageRoot", ""))).resolve()
    current = inspect_extracted_package(root, package_root)
    if current["packageFingerprint"] != inspection.get("packageFingerprint"):
        raise RuntimeError("Staged package changed after inspection; inspect it again")
    if not current["importAllowed"]:
        raise RuntimeError("Package has BLOCK findings and cannot be imported")
    kind = current["kind"]
    identifier = _safe_component(current["identifier"], "identifier")
    version = _safe_component(current["version"], "version")
    fingerprint = current["packageFingerprint"]
    folder_name = f"{identifier}--v-{version}--{fingerprint[:12]}"
    if kind == "beam_commissioning":
        destination = root / "machine_model" / "beam_commissioning" / folder_name
        profile_relative = destination.relative_to(root) / str(current["manifest"]["files"]["profile"])
    else:
        destination = root / "machine_model" / "asset_registry" / kind / folder_name
        profile_relative = None
    with _REGISTRY_LOCK:
        registry = _load_registry(root)
        entry_id = f"{kind}:{identifier}:{version}:{fingerprint[:16]}"
        existing = next(
            (item for item in registry["entries"] if isinstance(item, dict) and item.get("id") == entry_id),
            None,
        )
        if existing is not None:
            if not destination.is_dir():
                raise RuntimeError("Registry entry exists but immutable model content is missing")
            return {"alreadyImported": True, "model": _entry_view(root, existing)}
        if destination.exists():
            raise RuntimeError(f"Immutable model destination already exists without a registry record: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(package_root, destination)
        entry = {
            "id": entry_id,
            "kind": kind,
            "identifier": current["identifier"],
            "machine_name": current["machineName"],
            "version": current["version"],
            "active": True,
            "legacy": False,
            "path": str(destination.relative_to(root)),
            "profile": str(profile_relative) if profile_relative else "",
            "package_fingerprint": fingerprint,
            "model_fingerprint": current["modelFingerprint"],
            "imported_at": _now(),
            "provenance": current["manifest"].get("provenance", {}),
            "approval": current["manifest"].get("approval", {}),
        }
        audit = {
            "schema_version": 1,
            "imported_at": entry["imported_at"],
            "entry_id": entry_id,
            "package_fingerprint": fingerprint,
            "model_fingerprint": current["modelFingerprint"],
            "inspection_report": current["report"],
            "policy": "immutable content; registry status may be deactivated but content is never overwritten",
        }
        try:
            (destination / "package_import.json").write_text(
                json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            # The import audit is generated by this application and therefore is
            # intentionally outside the supplier package hash inventory.
            registry["entries"].append(entry)
            _write_registry(root, registry)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    return {"alreadyImported": False, "model": _entry_view(root, entry)}


def set_model_active(root: Path, model_id: str, active: bool) -> dict[str, Any]:
    root = root.expanduser().resolve()
    with _REGISTRY_LOCK:
        registry = _load_registry(root)
        entry = next(
            (item for item in registry["entries"] if isinstance(item, dict) and item.get("id") == model_id),
            None,
        )
        if entry is None:
            if not str(model_id).startswith("legacy-"):
                raise RuntimeError("Machine model registry entry was not found")
            base = root / "machine_model" / "beam_commissioning"
            for profile in sorted(base.glob("*/profile.json")):
                try:
                    model = CommissionedBeamModel(profile)
                    model.particle_calibration()
                except Exception:
                    continue
                candidate_id = "legacy-" + model.fingerprint[:16]
                if candidate_id != model_id:
                    continue
                entry = {
                    "id": candidate_id,
                    "kind": "beam_commissioning",
                    "identifier": model.machine_name,
                    "machine_name": model.machine_name,
                    "version": profile.parent.name,
                    "active": True,
                    "legacy": True,
                    "path": str(profile.parent.relative_to(root)),
                    "profile": str(profile.relative_to(root)),
                    "package_fingerprint": "",
                    "model_fingerprint": model.fingerprint,
                    "imported_at": "legacy status registered " + _now(),
                    "provenance": model.profile.get("provenance", {}),
                    "approval": {},
                }
                registry["entries"].append(entry)
                break
            if entry is None:
                raise RuntimeError("Legacy machine model was not found")
        entry["active"] = bool(active)
        entry["status_changed_at"] = _now()
        _write_registry(root, registry)
        return _entry_view(root, entry)


def profile_registry_status(root: Path, profile: Path) -> bool | None:
    """Return registered active state; ``None`` denotes a legacy profile."""
    root = root.expanduser().resolve()
    profile = profile.expanduser().resolve()
    with _REGISTRY_LOCK:
        registry = _load_registry(root)
    for entry in registry["entries"]:
        if not isinstance(entry, dict) or not entry.get("profile"):
            continue
        if (root / str(entry["profile"])).resolve() == profile:
            return bool(entry.get("active", True))
    return None
