"""Export a TOPAS DoseToMedium grid as a standards-compliant derived RTDOSE."""

from __future__ import annotations

import copy
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any

import numpy as np
import pydicom
from pydicom.dataset import FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, PYDICOM_IMPLEMENTATION_UID, RTDoseStorage, generate_uid
from pydicom.valuerep import format_number_as_ds

from gui.case_results import analysis_run_dir, case_identity, update_run_manifest
from gui.line_dose import LineDoseDataset, find_physical_plan_dose


EXPORT_MODES = {"particle_calibrated", "raw", "peak_scaled"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _dose_values(dataset: LineDoseDataset, mode: str) -> tuple[np.ndarray, float, str]:
    if dataset.mc is None or dataset.mc_path is None:
        raise RuntimeError("A TOPAS/MC dose source is required for RTDOSE export")
    if mode not in EXPORT_MODES:
        raise RuntimeError(f"Unknown MC RTDOSE export mode: {mode}")
    if mode == "peak_scaled":
        scale = float(dataset.mc_peak_scale or 0.0)
        values = dataset.mc * scale
        applied_scale = scale * (
            dataset.mc_calibration_scale if dataset.mc_source_type == "topas_binary" else 1.0
        )
        description = "TOPAS legacy TPS-peak-fitted diagnostic QA dose"
    elif mode == "raw":
        if dataset.mc_source_type != "topas_binary":
            raise RuntimeError(
                "Raw per-run export requires the original TOPAS .bin; it cannot be reconstructed from RTDOSE"
            )
        scale = 1.0
        applied_scale = scale
        values = dataset.mc_source
        description = "Raw TOPAS per-run uncalibrated diagnostic QA dose"
    else:
        if not dataset.mc_absolute_calibrated:
            raise RuntimeError(
                "Particle-calibrated RTDOSE export is unavailable: "
                + dataset.mc_calibration_reason
            )
        scale = dataset.mc_calibration_scale if dataset.mc_source_type == "topas_binary" else 1.0
        applied_scale = scale
        values = dataset.mc
        description = "TOPAS Nplan/Nsim particle-calibrated research QA dose"
    if not np.isfinite(values).all() or float(values.max()) <= 0.0 or float(values.min()) < 0.0:
        raise RuntimeError("MC dose must be finite, non-negative and non-empty")
    return np.asarray(values, dtype=np.float64), applied_scale, description


def _plan_reference(root: Path) -> tuple[Path, pydicom.dataset.FileDataset]:
    matches: list[tuple[Path, pydicom.dataset.FileDataset]] = []
    for path in sorted((root / "dicom" / "RTPLAN").glob("*.dcm")):
        dataset = pydicom.dcmread(path, stop_before_pixels=True)
        if str(getattr(dataset, "Modality", "")).upper() == "RTPLAN":
            matches.append((path.resolve(), dataset))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one RTPLAN for RTDOSE reference, found {len(matches)}")
    return matches[0]


def _copy_case_hierarchy(
    output: pydicom.dataset.FileDataset,
    plan: pydicom.dataset.FileDataset,
) -> None:
    """Make the derived object inherit the active plan's patient/study identity.

    The dose grid still comes from the referenced physical PLAN RTDOSE.  These
    fields are copied explicitly so a stale or foreign template can never leak
    a second patient hierarchy into an otherwise valid derived object.
    """

    for keyword in (
        "PatientName",
        "PatientID",
        "IssuerOfPatientID",
        "TypeOfPatientID",
        "PatientBirthDate",
        "PatientBirthTime",
        "PatientSex",
        "StudyInstanceUID",
        "StudyID",
        "AccessionNumber",
        "FrameOfReferenceUID",
    ):
        if keyword in plan:
            output[keyword] = copy.deepcopy(plan[keyword])


def _hierarchy_values(dataset: pydicom.dataset.Dataset) -> dict[str, str]:
    return {
        keyword: str(getattr(dataset, keyword, "") or "")
        for keyword in (
            "PatientID",
            "PatientName",
            "PatientBirthDate",
            "PatientSex",
            "StudyInstanceUID",
            "FrameOfReferenceUID",
        )
    }


def _validate_exported_object(
    check: pydicom.dataset.FileDataset,
    template: pydicom.dataset.FileDataset,
    plan: pydicom.dataset.FileDataset,
    values: np.ndarray,
    sop_uid: str,
    series_uid: str,
    plan_instance_uid: str,
) -> tuple[float, float]:
    if str(check.SOPClassUID) != str(RTDoseStorage):
        raise RuntimeError("Exported object does not use RT Dose Storage")
    if str(check.SOPInstanceUID) != sop_uid or str(check.SeriesInstanceUID) != series_uid:
        raise RuntimeError("Exported RTDOSE instance or series UID changed during serialization")
    if str(check.file_meta.TransferSyntaxUID) != str(ExplicitVRLittleEndian):
        raise RuntimeError("Exported RTDOSE is not Explicit VR Little Endian")
    if (
        str(getattr(check, "DoseUnits", "")).upper() != "GY"
        or str(getattr(check, "DoseType", "")).upper() != "PHYSICAL"
        or str(getattr(check, "DoseSummationType", "")).upper() != "PLAN"
    ):
        raise RuntimeError("Exported RTDOSE dose identity fields are invalid")
    exported_hierarchy = _hierarchy_values(check)
    plan_hierarchy = _hierarchy_values(plan)
    for keyword in ("PatientID", "PatientName", "StudyInstanceUID", "FrameOfReferenceUID"):
        if not plan_hierarchy[keyword] or exported_hierarchy[keyword] != plan_hierarchy[keyword]:
            raise RuntimeError(
                f"Exported RTDOSE {keyword} does not match the active RTPLAN: "
                f"RTDOSE={exported_hierarchy[keyword] or '<missing>'}, "
                f"RTPLAN={plan_hierarchy[keyword] or '<missing>'}"
            )
    for keyword in ("PatientBirthDate", "PatientSex"):
        if plan_hierarchy[keyword] and exported_hierarchy[keyword] != plan_hierarchy[keyword]:
            raise RuntimeError(f"Exported RTDOSE {keyword} does not match the active RTPLAN")
    for keyword in (
        "ImagePositionPatient",
        "ImageOrientationPatient",
        "PixelSpacing",
        "GridFrameOffsetVector",
    ):
        before = np.asarray(getattr(template, keyword, []), dtype=float)
        after = np.asarray(getattr(check, keyword, []), dtype=float)
        if before.shape != after.shape or not np.allclose(before, after, rtol=0.0, atol=1e-8):
            raise RuntimeError(f"Exported RTDOSE {keyword} changed")
    references = getattr(check, "ReferencedRTPlanSequence", [])
    if len(references) != 1 or str(
        getattr(references[0], "ReferencedSOPInstanceUID", "")
    ) != plan_instance_uid:
        raise RuntimeError("Exported RTDOSE does not reference the current RTPLAN")
    scaling = float(check.DoseGridScaling)
    if not np.isfinite(scaling) or scaling <= 0.0:
        raise RuntimeError("Exported RTDOSE DoseGridScaling is invalid")
    roundtrip = check.pixel_array.astype(np.float64) * scaling
    if roundtrip.shape != values.shape:
        raise RuntimeError(f"Exported RTDOSE shape mismatch: {roundtrip.shape} versus {values.shape}")
    maximum = float(values.max())
    max_error = float(np.max(np.abs(roundtrip - values)))
    # Pixel quantisation is nominally <= 0.5 * scaling.  The DS string
    # representation of DoseGridScaling and the subsequent floating-point
    # multiplication add a few ulps, so one full stored-value step is the
    # appropriate strict round-trip bound.
    allowed = max(scaling * 1.05, maximum * 1e-10)
    if max_error > allowed:
        raise RuntimeError(f"Exported RTDOSE round-trip error {max_error:g} Gy exceeds {allowed:g} Gy")
    return scaling, max_error


def export_mc_rtdose(
    root: Path,
    mc_path: Path,
    output_tag: str,
    mode: str,
) -> dict[str, Any]:
    root = root.expanduser().resolve()
    mc_path = mc_path.expanduser().resolve()
    reference_path = find_physical_plan_dose(root)
    dataset = LineDoseDataset(root, reference_path, mc_path)
    values, applied_scale, description = _dose_values(dataset, mode)
    template = pydicom.dcmread(reference_path)
    output = copy.deepcopy(template)
    plan_path, plan = _plan_reference(root)
    _copy_case_hierarchy(output, plan)
    now = datetime.now()
    sop_uid = generate_uid()
    series_uid = generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = RTDoseStorage
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = PYDICOM_IMPLEMENTATION_UID
    file_meta.ImplementationVersionName = "PLAN1699_1"
    output.file_meta = file_meta
    output.preamble = b"\0" * 128
    output.SOPClassUID = RTDoseStorage
    output.SOPInstanceUID = sop_uid
    output.SeriesInstanceUID = series_uid
    output.Modality = "RTDOSE"
    output.SeriesDescription = {
        "particle_calibrated": "MC QA TOPAS particle-calibrated",
        "peak_scaled": "MC QA TOPAS legacy peak-scaled",
        "raw": "MC QA TOPAS raw per-run",
    }[mode]
    output.DoseUnits = "GY"
    output.DoseType = "PHYSICAL"
    output.DoseSummationType = "PLAN"
    output.InstanceCreationDate = now.strftime("%Y%m%d")
    output.InstanceCreationTime = now.strftime("%H%M%S.%f")[:13]
    output.ContentDate = now.strftime("%Y%m%d")
    output.ContentTime = now.strftime("%H%M%S.%f")[:13]
    output.InstanceNumber = 9001
    output.Manufacturer = "PLAN1699 research workflow"
    output.ManufacturerModelName = "TOPAS DoseToMedium exporter"
    output.SoftwareVersions = "PLAN1699"
    output.DoseComment = description
    output.DerivationDescription = description
    plan_class_uid = str(plan.SOPClassUID)
    plan_instance_uid = str(plan.SOPInstanceUID)
    reference = pydicom.dataset.Dataset()
    reference.ReferencedSOPClassUID = plan_class_uid
    reference.ReferencedSOPInstanceUID = plan_instance_uid
    output.ReferencedRTPlanSequence = Sequence([reference])

    maximum = float(values.max())
    scaling = maximum / float(np.iinfo(np.uint32).max)
    stored = np.rint(values / scaling).clip(0, np.iinfo(np.uint32).max).astype("<u4")
    output.SamplesPerPixel = 1
    output.PhotometricInterpretation = "MONOCHROME2"
    output.BitsAllocated = 32
    output.BitsStored = 32
    output.HighBit = 31
    output.PixelRepresentation = 0
    output.NumberOfFrames = int(values.shape[0])
    output.Rows = int(values.shape[1])
    output.Columns = int(values.shape[2])
    output.DoseGridScaling = format_number_as_ds(scaling)
    output.PixelData = stored.tobytes(order="C")
    output.is_little_endian = True
    output.is_implicit_VR = False

    run = analysis_run_dir(root, output_tag, create=True)
    identity = case_identity(root)
    stamp = now.strftime("%Y%m%dT%H%M%S")
    filename = f"MC_RTDose_{identity.patient_key}_{output_tag}_{mode}_{stamp}.dcm"
    destination = run / "dicom" / filename
    pydicom.dcmwrite(destination, output, write_like_original=False)

    try:
        check = pydicom.dcmread(destination)
        dose_grid_scaling, max_error = _validate_exported_object(
            check,
            template,
            plan,
            values,
            sop_uid,
            series_uid,
            plan_instance_uid,
        )
    except Exception:
        destination.unlink(missing_ok=True)
        raise

    audit = {
        "schema_version": 2,
        "created_at": now.astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "qa_only": True,
        "absolute_dose_calibrated": mode == "particle_calibrated" and dataset.mc_absolute_calibrated,
        "calibration_protocol": dataset.mc_calibration_protocol if mode == "particle_calibrated" else "",
        "calibration": dataset.mc_calibration_details if mode == "particle_calibrated" else {},
        "description": description,
        "source_mc": str(mc_path),
        "source_mc_sha256": _sha256(mc_path),
        "source_tps_rtdose": str(reference_path),
        "source_tps_rtdose_sha256": _sha256(reference_path),
        "source_rtplan": str(plan_path),
        "source_rtplan_sha256": _sha256(plan_path),
        "applied_mc_scale": applied_scale,
        "shape_zyx": list(values.shape),
        "dose_grid_scaling": dose_grid_scaling,
        "maximum_gy": maximum,
        "roundtrip_max_error_gy": max_error,
        "sop_instance_uid": sop_uid,
        "series_instance_uid": series_uid,
        "referenced_rtplan_sop_instance_uid": plan_instance_uid,
        "patient_id": str(getattr(check, "PatientID", "")),
        "patient_name": str(getattr(check, "PatientName", "")),
        "study_instance_uid": str(getattr(check, "StudyInstanceUID", "")),
        "frame_of_reference_uid": str(getattr(check, "FrameOfReferenceUID", "")),
        "output": str(destination),
        "output_sha256": _sha256(destination),
    }
    audit_path = destination.with_suffix(".json")
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    update_run_manifest(
        root,
        output_tag,
        mc_source=mc_path,
        mc_dicom=destination,
        additions={"last_mc_rtdose_export": audit},
    )
    return {"path": str(destination), "auditPath": str(audit_path), **audit}
