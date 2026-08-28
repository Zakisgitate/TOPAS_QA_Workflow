"""Interactive TPS/TOPAS line-dose data service.

The implementation adapts the geometry and trilinear sampling model from the
standalone RT Line Dose Viewer in ``/Users/jiangzhenmin/project`` to this QA
project.  The user may display any verified RTDOSE linked to the active RTPLAN;
the physical PLAN RTDOSE remains the default and the TOPAS scoring grid.
"""

from __future__ import annotations

from collections import OrderedDict
import csv
from dataclasses import dataclass
import io
import json
from pathlib import Path
import threading
from typing import Optional

import numpy as np
import pydicom
from scipy.ndimage import map_coordinates

from scripts.utils.mc_dose_calibration import (
    find_allocation_file,
    read_dicom_calibration,
    resolve_particle_calibration,
)


_CACHE_LOCK = threading.RLock()
_CACHE: OrderedDict[tuple, "LineDoseDataset"] = OrderedDict()
_TRAPEZOID = getattr(np, "trapezoid", None) or np.trapz


def _active_plan(root: Path) -> tuple[Path, pydicom.dataset.Dataset]:
    plans: list[tuple[Path, pydicom.dataset.Dataset]] = []
    for path in sorted((root / "dicom" / "RTPLAN").glob("*.dcm")):
        dataset = pydicom.dcmread(path, stop_before_pixels=True)
        if str(getattr(dataset, "Modality", "")).upper() == "RTPLAN":
            plans.append((path.resolve(), dataset))
    if len(plans) != 1:
        raise RuntimeError(f"Expected one active RTPLAN, found {len(plans)}")
    return plans[0]


def _dose_case_mismatches(
    plan: pydicom.dataset.Dataset,
    dose: pydicom.dataset.Dataset,
) -> list[str]:
    comparisons = (
        ("PatientID", True),
        ("PatientName", True),
        ("PatientBirthDate", False),
        ("PatientSex", False),
        ("StudyInstanceUID", True),
        ("FrameOfReferenceUID", True),
    )
    mismatches: list[str] = []
    for keyword, required in comparisons:
        plan_value = str(getattr(plan, keyword, "") or "")
        dose_value = str(getattr(dose, keyword, "") or "")
        if required and (not plan_value or not dose_value):
            mismatches.append(
                f"{keyword}: RTPLAN={plan_value or '<missing>'}, "
                f"RTDOSE={dose_value or '<missing>'}"
            )
        elif plan_value and dose_value and plan_value != dose_value:
            mismatches.append(f"{keyword}: RTPLAN={plan_value}, RTDOSE={dose_value}")
    return mismatches


def _referenced_beam_numbers(dose: pydicom.dataset.Dataset) -> list[int]:
    numbers: set[int] = set()
    for plan_ref in getattr(dose, "ReferencedRTPlanSequence", []):
        for fraction_ref in getattr(plan_ref, "ReferencedFractionGroupSequence", []):
            for beam_ref in getattr(fraction_ref, "ReferencedBeamSequence", []):
                value = getattr(beam_ref, "ReferencedBeamNumber", None)
                if value is not None:
                    numbers.add(int(value))
    return sorted(numbers)


def list_tps_doses(root: Path) -> dict:
    root = root.expanduser().resolve()
    plan_path, plan = _active_plan(root)
    plan_uid = str(getattr(plan, "SOPInstanceUID", ""))
    if not plan_uid:
        raise RuntimeError(f"Active RTPLAN has no SOPInstanceUID: {plan_path}")

    doses: list[dict] = []
    for path in sorted((root / "dicom" / "RTDOSE").glob("*.dcm")):
        dataset = pydicom.dcmread(path, stop_before_pixels=True)
        if str(getattr(dataset, "Modality", "")).upper() != "RTDOSE":
            continue
        units = str(getattr(dataset, "DoseUnits", "")).upper()
        if units not in {"GY", "CGY"}:
            continue
        referenced_plans = {
            str(getattr(reference, "ReferencedSOPInstanceUID", ""))
            for reference in getattr(dataset, "ReferencedRTPlanSequence", [])
        }
        if plan_uid not in referenced_plans:
            continue
        mismatches = _dose_case_mismatches(plan, dataset)
        if mismatches:
            continue
        dose_type = str(getattr(dataset, "DoseType", "")).upper() or "UNKNOWN"
        summation = str(getattr(dataset, "DoseSummationType", "")).upper() or "UNKNOWN"
        uid = str(getattr(dataset, "SOPInstanceUID", ""))
        if not uid:
            continue
        beams = _referenced_beam_numbers(dataset)
        description = str(
            getattr(dataset, "SeriesDescription", "")
            or getattr(dataset, "DoseComment", "")
            or ""
        )
        label = f"{summation} / {dose_type} / {units} — {path.name}"
        if beams:
            label += " — Beam " + ",".join(str(number) for number in beams)
        doses.append(
            {
                "doseUID": uid,
                "path": str(path.resolve()),
                "fileName": path.name,
                "doseType": dose_type,
                "summationType": summation,
                "doseUnits": units,
                "seriesDescription": description,
                "referencedBeamNumbers": beams,
                "shapeZYX": [
                    int(getattr(dataset, "NumberOfFrames", 1)),
                    int(getattr(dataset, "Rows", 0)),
                    int(getattr(dataset, "Columns", 0)),
                ],
                "label": label,
                "isDefault": dose_type == "PHYSICAL" and summation == "PLAN",
            }
        )
    if not doses:
        raise RuntimeError("No selectable GY/CGY RTDOSE referencing the active RTPLAN is available")
    defaults = [item for item in doses if item["isDefault"]]
    return {
        "doses": doses,
        "defaultDoseUID": defaults[0]["doseUID"] if len(defaults) == 1 else "",
        "activeRTPlanUID": plan_uid,
    }


def resolve_tps_dose(root: Path, dose_uid: str | None = None) -> Path:
    inventory = list_tps_doses(root)
    if not dose_uid:
        dose_uid = str(inventory["defaultDoseUID"])
        if not dose_uid:
            raise RuntimeError("Expected exactly one PHYSICAL/PLAN RTDOSE for the default TPS dose")
    matches = [item for item in inventory["doses"] if item["doseUID"] == dose_uid]
    if len(matches) != 1:
        raise RuntimeError("The selected TPS RTDOSE is not available for the active RTPLAN")
    return Path(str(matches[0]["path"]))


def find_physical_plan_dose(root: Path) -> Path:
    inventory = list_tps_doses(root)
    matches = [item for item in inventory["doses"] if item["isDefault"]]
    if len(matches) != 1:
        raise RuntimeError(
            "Expected exactly one PHYSICAL/PLAN RTDOSE referencing the active RTPLAN, "
            f"found {[item['fileName'] for item in matches]}"
        )
    return Path(str(matches[0]["path"]))


def find_plan_isocenter(root: Path) -> np.ndarray:
    values: list[np.ndarray] = []
    for path in sorted((root / "dicom" / "RTPLAN").glob("*.dcm")):
        plan = pydicom.dcmread(path, stop_before_pixels=True)
        if str(getattr(plan, "Modality", "")).upper() != "RTPLAN":
            continue
        for beam in getattr(plan, "IonBeamSequence", []):
            for control_point in getattr(beam, "IonControlPointSequence", []):
                point = getattr(control_point, "IsocenterPosition", None)
                if point is not None:
                    values.append(np.asarray(point, dtype=float))
    if not values or any(value.shape != (3,) for value in values):
        raise RuntimeError("RTPLAN does not contain a usable isocenter")
    if not all(np.allclose(values[0], value, atol=1e-6) for value in values[1:]):
        raise RuntimeError(f"RTPLAN contains inconsistent isocenters: {[item.tolist() for item in values]}")
    return values[0]


def _file_key(path: Optional[Path]) -> tuple:
    if path is None:
        return (None,)
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size)


def get_line_dose_dataset(
    root: Path,
    mc_path: Optional[Path],
    tps_dose_uid: str | None = None,
) -> "LineDoseDataset":
    root = root.resolve()
    dose_path = resolve_tps_dose(root, tps_dose_uid)
    mc_path = mc_path.expanduser().resolve() if mc_path else None
    if mc_path is not None and not mc_path.is_file():
        raise RuntimeError(f"MC binary does not exist: {mc_path}")
    allocation = (
        find_allocation_file(root, mc_path)
        if mc_path is not None and mc_path.suffix.casefold() != ".dcm"
        else None
    )
    key = (_file_key(dose_path), _file_key(mc_path), _file_key(allocation))
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached
    dataset = LineDoseDataset(root, dose_path, mc_path)
    with _CACHE_LOCK:
        _CACHE[key] = dataset
        _CACHE.move_to_end(key)
        while len(_CACHE) > 2:
            _CACHE.popitem(last=False)
    return dataset


@dataclass(frozen=True)
class FrameData:
    tps: np.ndarray
    mc: Optional[np.ndarray]
    meta: dict


class LineDoseDataset:
    def __init__(self, root: Path, dose_path: Path, mc_path: Optional[Path]):
        self.root = root.expanduser().resolve()
        self.dose_path = dose_path
        self.mc_path = mc_path
        self.dicom = pydicom.dcmread(dose_path)
        self.tps_dose_type = str(getattr(self.dicom, "DoseType", "")).upper() or "UNKNOWN"
        self.tps_summation_type = (
            str(getattr(self.dicom, "DoseSummationType", "")).upper() or "UNKNOWN"
        )
        self.tps_dose_units = str(getattr(self.dicom, "DoseUnits", "")).upper() or "UNKNOWN"
        self.tps_label = f"TPS {self.tps_summation_type} {self.tps_dose_type}"
        tps = self.dicom.pixel_array.astype(np.float64)
        tps *= float(getattr(self.dicom, "DoseGridScaling", 1.0))
        if str(getattr(self.dicom, "DoseUnits", "GY")).upper() == "CGY":
            tps /= 100.0

        iop = np.asarray(
            getattr(self.dicom, "ImageOrientationPatient", [1, 0, 0, 0, 1, 0]),
            dtype=float,
        )
        i_dir = iop[:3] / np.linalg.norm(iop[:3])
        j_dir = iop[3:] / np.linalg.norm(iop[3:])
        k_dir = np.cross(i_dir, j_dir)
        pixel_spacing = np.asarray(getattr(self.dicom, "PixelSpacing", [1.0, 1.0]), dtype=float)
        dj, di = float(pixel_spacing[0]), float(pixel_spacing[1])
        offsets = np.asarray(getattr(self.dicom, "GridFrameOffsetVector", [0.0]), dtype=float)
        if offsets.size > 1:
            gaps = np.diff(offsets)
            dk = float(np.mean(gaps))
            if not np.allclose(gaps, dk, rtol=1e-4, atol=1e-4):
                raise RuntimeError("Interactive line dose requires a regular RTDOSE frame spacing")
        else:
            dk = float(getattr(self.dicom, "SliceThickness", 1.0) or 1.0)

        origin = np.asarray(self.dicom.ImagePositionPatient, dtype=float) + k_dir * float(offsets[0])
        mc: Optional[np.ndarray] = None
        mc_source: Optional[np.ndarray] = None
        mc_source_type = "none"
        mc_label = "MC (TOPAS DoseToMedium)"
        mc_calibration_scale = 1.0
        mc_calibration_protocol = ""
        mc_calibration_reason = "No MC dose loaded"
        mc_calibration_allocation = ""
        mc_absolute_calibrated = False
        mc_calibration_details: dict = {}
        if mc_path is not None:
            if mc_path.suffix.casefold() == ".dcm":
                evaluation = pydicom.dcmread(mc_path)
                if str(getattr(evaluation, "Modality", "")).upper() != "RTDOSE":
                    raise RuntimeError(f"Selected MC DICOM is not RTDOSE: {mc_path}")
                if str(getattr(evaluation, "DoseUnits", "")).upper() not in {"GY", "CGY"}:
                    raise RuntimeError("MC RTDOSE must use GY or CGY dose units")
                self._validate_matching_dose_case(self.dicom, evaluation)
                mc = evaluation.pixel_array.astype(np.float64)
                mc *= float(getattr(evaluation, "DoseGridScaling", 1.0))
                if str(evaluation.DoseUnits).upper() == "CGY":
                    mc /= 100.0
                self._validate_matching_dose_grid(self.dicom, evaluation, tps.shape, mc.shape)
                mc_source = mc.copy()
                audit = read_dicom_calibration(mc_path)
                mc_absolute_calibrated = bool(audit.get("available"))
                nested = audit.get("calibration") if isinstance(audit.get("calibration"), dict) else {}
                mc_calibration_scale = float(
                    audit.get("applied_mc_scale", nested.get("scale", 1.0)) or 1.0
                )
                mc_calibration_protocol = str(
                    audit.get("calibration_protocol", nested.get("protocol", ""))
                )
                mc_calibration_reason = str(audit.get("reason", ""))
                mc_calibration_allocation = str(nested.get("allocation_file", ""))
                mc_calibration_details = audit
                mc_source_type = "dicom_rtdose"
                mc_label = (
                    "MC (cached particle-calibrated RTDOSE)"
                    if mc_absolute_calibrated
                    else "MC (cached uncalibrated RTDOSE)"
                )
            else:
                scoring_reference = find_physical_plan_dose(self.root)
                if scoring_reference.resolve() != dose_path.resolve():
                    scoring_dose = pydicom.dcmread(scoring_reference, stop_before_pixels=True)
                    scoring_shape = (
                        int(getattr(scoring_dose, "NumberOfFrames", 1)),
                        int(getattr(scoring_dose, "Rows", 0)),
                        int(getattr(scoring_dose, "Columns", 0)),
                    )
                    self._validate_matching_dose_grid(
                        scoring_dose,
                        self.dicom,
                        scoring_shape,
                        tps.shape,
                    )
                values = np.fromfile(mc_path, dtype=np.float64)
                if values.size != tps.size:
                    raise RuntimeError(
                        f"MC/TPS grid size mismatch: {values.size} versus {tps.size} voxels"
                    )
                mc_source = values.reshape(tps.shape)
                calibration = resolve_particle_calibration(self.root, mc_path)
                mc_calibration_details = calibration.to_dict()
                mc_absolute_calibrated = calibration.available
                mc_calibration_scale = calibration.scale
                mc_calibration_protocol = calibration.protocol
                mc_calibration_reason = calibration.reason
                mc_calibration_allocation = calibration.allocation_file
                mc = mc_source * calibration.scale if calibration.available else mc_source.copy()
                mc_source_type = "topas_binary"
                mc_label = (
                    "MC (TOPAS particle-calibrated)"
                    if calibration.available
                    else "MC (TOPAS uncalibrated per-run dose)"
                )
            if not np.isfinite(mc).all() or float(mc.max()) <= 0:
                raise RuntimeError("MC dose is empty or contains non-finite values")

        if dk < 0:
            tps = tps[::-1]
            if mc is not None:
                mc = mc[::-1]
            if mc_source is not None:
                mc_source = mc_source[::-1]
            origin = origin + k_dir * float(offsets[-1] - offsets[0])
            dk = -dk

        self.tps = np.ascontiguousarray(tps)
        self.mc = np.ascontiguousarray(mc) if mc is not None else None
        self.mc_source = np.ascontiguousarray(mc_source) if mc_source is not None else None
        self.origin = origin
        self.spacing = np.asarray([di, dj, dk], dtype=float)
        self.direction = np.column_stack([i_dir, j_dir, k_dir])
        self.inverse_direction = np.linalg.inv(self.direction)
        self.shape = self.tps.shape
        self.tps_max = float(self.tps.max())
        self.mc_max = float(self.mc.max()) if self.mc is not None else None
        self.mc_source_max = float(self.mc_source.max()) if self.mc_source is not None else None
        self.mc_peak_scale = self.tps_max / self.mc_max if self.mc_max else None
        self.mc_source_type = mc_source_type
        self.mc_label = mc_label
        self.mc_absolute_calibrated = mc_absolute_calibrated
        self.mc_calibration_scale = mc_calibration_scale
        self.mc_calibration_protocol = mc_calibration_protocol
        self.mc_calibration_reason = mc_calibration_reason
        self.mc_calibration_allocation = mc_calibration_allocation
        self.mc_calibration_details = mc_calibration_details
        self.isocenter = find_plan_isocenter(self.root)
        self.isocenter_index = self.patient_to_index(self.isocenter[None, :])[0]
        if not np.isfinite(self.tps).all() or self.tps_max <= 0:
            raise RuntimeError("TPS physical dose is empty or contains non-finite values")

    @staticmethod
    def _validate_matching_dose_case(
        reference: pydicom.dataset.Dataset,
        evaluation: pydicom.dataset.Dataset,
    ) -> None:
        mismatches: list[str] = []
        for keyword in ("PatientID", "PatientName", "StudyInstanceUID", "FrameOfReferenceUID"):
            reference_value = str(getattr(reference, keyword, "") or "")
            evaluation_value = str(getattr(evaluation, keyword, "") or "")
            if not reference_value or not evaluation_value or reference_value != evaluation_value:
                mismatches.append(
                    f"{keyword}: TPS={reference_value or '<missing>'}, "
                    f"MC={evaluation_value or '<missing>'}"
                )
        if mismatches:
            raise RuntimeError(
                "MC RTDOSE belongs to a different DICOM patient/study: " + "; ".join(mismatches)
            )

    @staticmethod
    def _validate_matching_dose_grid(
        reference: pydicom.dataset.Dataset,
        evaluation: pydicom.dataset.Dataset,
        reference_shape: tuple[int, ...],
        evaluation_shape: tuple[int, ...],
    ) -> None:
        if reference_shape != evaluation_shape:
            raise RuntimeError(
                f"MC RTDOSE grid shape {evaluation_shape} does not match TPS {reference_shape}"
            )
        vector_fields = (
            "ImagePositionPatient",
            "ImageOrientationPatient",
            "PixelSpacing",
            "GridFrameOffsetVector",
        )
        for keyword in vector_fields:
            left = np.asarray(getattr(reference, keyword, []), dtype=float)
            right = np.asarray(getattr(evaluation, keyword, []), dtype=float)
            if left.shape != right.shape or not np.allclose(left, right, rtol=0.0, atol=1e-5):
                raise RuntimeError(f"MC RTDOSE {keyword} does not match the TPS dose grid")
        if str(getattr(reference, "FrameOfReferenceUID", "")) != str(
            getattr(evaluation, "FrameOfReferenceUID", "")
        ):
            raise RuntimeError("MC RTDOSE FrameOfReferenceUID does not match TPS")

    def index_to_patient(self, indices: np.ndarray | list[float]) -> np.ndarray:
        indices = np.asarray(indices, dtype=float)
        return self.origin + (indices * self.spacing) @ self.direction.T

    def patient_to_index(self, points: np.ndarray) -> np.ndarray:
        points = np.asarray(points, dtype=float)
        return ((points - self.origin) @ self.inverse_direction.T) / self.spacing

    def _inside(self, points: np.ndarray) -> np.ndarray:
        indices = self.patient_to_index(points)
        nk, nj, ni = self.shape
        return (
            (indices[:, 0] >= 0)
            & (indices[:, 0] <= ni - 1)
            & (indices[:, 1] >= 0)
            & (indices[:, 1] <= nj - 1)
            & (indices[:, 2] >= 0)
            & (indices[:, 2] <= nk - 1)
        )

    def _sample_volume(self, volume: np.ndarray, points: np.ndarray) -> np.ndarray:
        indices = self.patient_to_index(points)
        coordinates = np.vstack([indices[:, 2], indices[:, 1], indices[:, 0]])
        return map_coordinates(
            volume,
            coordinates,
            order=1,
            mode="constant",
            cval=0.0,
            prefilter=False,
        )

    def frame_count(self, plane: str) -> int:
        nk, nj, ni = self.shape
        return {"axial": nk, "coronal": nj, "sagittal": ni}[plane]

    def frame(self, plane: str, index: int) -> FrameData:
        nk, nj, ni = self.shape
        i_dir, j_dir, k_dir = self.direction.T
        di, dj, dk = self.spacing
        if plane == "axial":
            index = int(np.clip(index, 0, nk - 1))
            slicer = (index, slice(None), slice(None))
            origin = self.index_to_patient([0, 0, index])
            u_vec, v_vec = i_dir * di, j_dir * dj
            slice_spacing = dk
        elif plane == "coronal":
            index = int(np.clip(index, 0, nj - 1))
            slicer = (slice(None, None, -1), index, slice(None))
            origin = self.index_to_patient([0, index, nk - 1])
            u_vec, v_vec = i_dir * di, -k_dir * dk
            slice_spacing = dj
        elif plane == "sagittal":
            index = int(np.clip(index, 0, ni - 1))
            slicer = (slice(None, None, -1), slice(None), index)
            origin = self.index_to_patient([index, 0, nk - 1])
            u_vec, v_vec = j_dir * dj, -k_dir * dk
            slice_spacing = di
        else:
            raise RuntimeError(f"Unknown line-dose plane: {plane}")
        tps = np.ascontiguousarray(self.tps[slicer], dtype=np.float32)
        mc = np.ascontiguousarray(self.mc[slicer], dtype=np.float32) if self.mc is not None else None
        iso_delta = self.isocenter - origin
        isocenter_u = float(np.dot(iso_delta, u_vec) / np.dot(u_vec, u_vec))
        isocenter_v = float(np.dot(iso_delta, v_vec) / np.dot(v_vec, v_vec))
        projected = origin + isocenter_u * u_vec + isocenter_v * v_vec
        slice_distance = float(np.linalg.norm(self.isocenter - projected))
        meta = {
            "plane": plane,
            "index": index,
            "width": int(tps.shape[1]),
            "height": int(tps.shape[0]),
            "origin": origin.tolist(),
            "uVec": u_vec.tolist(),
            "vVec": v_vec.tolist(),
            "pixelSizeU": float(np.linalg.norm(u_vec)),
            "pixelSizeV": float(np.linalg.norm(v_vec)),
            "hasMC": mc is not None,
            "tpsMaxGy": self.tps_max,
            "mcMaxRawGy": self.mc_max,
            "mcSourcePerRunMaxGy": self.mc_source_max,
            "mcPeakScale": self.mc_peak_scale,
            "mcSourceType": self.mc_source_type,
            "mcAbsoluteCalibrated": self.mc_absolute_calibrated,
            "mcCalibrationScale": self.mc_calibration_scale,
            "mcCalibrationProtocol": self.mc_calibration_protocol,
            "isocenterXYZmm": self.isocenter.tolist(),
            "isocenterU": isocenter_u,
            "isocenterV": isocenter_v,
            "isocenterInsideFrame": bool(
                -0.5 <= isocenter_u <= tps.shape[1] - 0.5
                and -0.5 <= isocenter_v <= tps.shape[0] - 0.5
            ),
            "isocenterSliceDistanceMm": slice_distance,
            "isIsocenterSlice": bool(slice_distance <= 0.5 * slice_spacing + 1e-6),
        }
        return FrameData(tps=tps, mc=mc, meta=meta)

    def summary(self) -> dict:
        nk, nj, ni = self.shape
        corners = np.asarray(
            [
                self.index_to_patient([i, j, k])
                for i in (0, ni - 1)
                for j in (0, nj - 1)
                for k in (0, nk - 1)
            ]
        )
        return {
            "dosePath": str(self.dose_path),
            "mcPath": str(self.mc_path) if self.mc_path else "",
            "patientID": str(getattr(self.dicom, "PatientID", "")),
            "patientName": str(getattr(self.dicom, "PatientName", "")),
            "studyInstanceUID": str(getattr(self.dicom, "StudyInstanceUID", "")),
            "frameOfReferenceUID": str(getattr(self.dicom, "FrameOfReferenceUID", "")),
            "referencedRTPlanUID": next(
                (
                    str(getattr(reference, "ReferencedSOPInstanceUID", ""))
                    for reference in getattr(self.dicom, "ReferencedRTPlanSequence", [])
                ),
                "",
            ),
            "hasMC": self.mc is not None,
            "shapeZYX": list(self.shape),
            "spacingXYZmm": self.spacing.tolist(),
            "originXYZmm": self.origin.tolist(),
            "boundsMinXYZmm": corners.min(axis=0).tolist(),
            "boundsMaxXYZmm": corners.max(axis=0).tolist(),
            "dimensions": {"axial": nk, "coronal": nj, "sagittal": ni},
            "tpsMaxGy": self.tps_max,
            "mcMaxRawGy": self.mc_max,
            "mcSourcePerRunMaxGy": self.mc_source_max,
            "mcPeakScale": self.mc_peak_scale,
            "mcSourceType": self.mc_source_type,
            "mcLabel": self.mc_label,
            "mcAbsoluteCalibrated": self.mc_absolute_calibrated,
            "mcCalibrationScale": self.mc_calibration_scale,
            "mcCalibrationProtocol": self.mc_calibration_protocol,
            "mcCalibrationReason": self.mc_calibration_reason,
            "mcCalibrationAllocation": self.mc_calibration_allocation,
            "tpsLabel": self.tps_label,
            "tpsDoseUID": str(getattr(self.dicom, "SOPInstanceUID", "")),
            "tpsDoseType": self.tps_dose_type,
            "tpsSummationType": self.tps_summation_type,
            "tpsDoseUnits": self.tps_dose_units,
            "tpsFileName": self.dose_path.name,
            "isocenterXYZmm": self.isocenter.tolist(),
            "isocenterIndexIJK": self.isocenter_index.tolist(),
            "isocenterSlices": {
                "axial": int(np.clip(round(self.isocenter_index[2]), 0, nk - 1)),
                "coronal": int(np.clip(round(self.isocenter_index[1]), 0, nj - 1)),
                "sagittal": int(np.clip(round(self.isocenter_index[0]), 0, ni - 1)),
            },
            "normalizationNote": (
                "MC uses independent commissioned N_plan/N_sim particle calibration; "
                "TPS dose was not used to set its output."
                if self.mc_absolute_calibrated
                else "Independent particle calibration is unavailable. Use independent maximum (%) "
                "for shape review only; absolute/Gamma comparison is disabled."
            ),
        }

    def profile(
        self,
        p1: list[float],
        p2: list[float],
        samples: int,
        normalization: str,
    ) -> dict:
        first = np.asarray(p1, dtype=float)
        second = np.asarray(p2, dtype=float)
        if first.shape != (3,) or second.shape != (3,):
            raise RuntimeError("Line endpoints must each contain patient X/Y/Z in mm")
        length = float(np.linalg.norm(second - first))
        if not np.isfinite(length) or length <= 1e-6:
            raise RuntimeError("Line endpoints overlap")
        samples = int(np.clip(samples, 2, 5000))
        fraction = np.linspace(0.0, 1.0, samples)
        points = first[None, :] + fraction[:, None] * (second - first)[None, :]
        distance = fraction * length
        inside = self._inside(points)
        tps_raw = self._sample_volume(self.tps, points)
        mc_raw = self._sample_volume(self.mc, points) if self.mc is not None else None

        if normalization == "absolute":
            if mc_raw is not None and not self.mc_absolute_calibrated:
                raise RuntimeError(
                    "Absolute MC line dose requires a commissioned N_plan/N_sim calibration: "
                    + self.mc_calibration_reason
                )
            tps_display = tps_raw
            mc_display = mc_raw
            unit = "Gy (independent particle calibration)"
        elif normalization == "independent":
            tps_display = 100.0 * tps_raw / self.tps_max
            mc_display = 100.0 * mc_raw / self.mc_max if mc_raw is not None else None
            unit = "% of each global maximum"
        elif normalization == "peak_scaled":
            tps_display = tps_raw
            mc_display = mc_raw * self.mc_peak_scale if mc_raw is not None else None
            unit = "Gy (MC peak-scaled)"
        else:
            raise RuntimeError(f"Unknown line-dose normalization: {normalization}")

        layers = [self._layer_payload("tps", self.tps_label, tps_raw, tps_display, inside, distance)]
        if mc_raw is not None and mc_display is not None:
            layers.append(
                self._layer_payload(
                    "mc",
                    self.mc_label,
                    mc_raw,
                    mc_display,
                    inside,
                    distance,
                )
            )
        return {
            "p1": first.tolist(),
            "p2": second.tolist(),
            "lengthMm": length,
            "samples": samples,
            "distanceMm": np.round(distance, 5).tolist(),
            "pointsXYZmm": np.round(points, 5).tolist(),
            "insideDoseGrid": inside.tolist(),
            "normalization": normalization,
            "displayUnit": unit,
            "layers": layers,
            "mcPeakScale": self.mc_peak_scale,
            "mcAbsoluteCalibrated": self.mc_absolute_calibrated,
            "mcCalibrationScale": self.mc_calibration_scale,
            "mcCalibrationProtocol": self.mc_calibration_protocol,
        }

    @staticmethod
    def _layer_payload(
        identifier: str,
        label: str,
        raw: np.ndarray,
        display: np.ndarray,
        inside: np.ndarray,
        distance: np.ndarray,
    ) -> dict:
        valid = inside & np.isfinite(display)
        values = display[valid]
        locations = distance[valid]
        stats: dict[str, Optional[float]] = {
            "max": None,
            "maxAtMm": None,
            "min": None,
            "mean": None,
            "integral": None,
            "fwhmMm": None,
        }
        if values.size:
            maximum_index = int(np.argmax(values))
            stats.update(
                {
                    "max": float(values[maximum_index]),
                    "maxAtMm": float(locations[maximum_index]),
                    "min": float(values.min()),
                    "mean": float(values.mean()),
                    "integral": float(_TRAPEZOID(values, locations)) if values.size > 1 else 0.0,
                    "fwhmMm": _fwhm(locations, values),
                }
            )
        return {
            "id": identifier,
            "label": label,
            "rawGy": raw.tolist(),
            "display": display.tolist(),
            "stats": stats,
        }


def _fwhm(distance: np.ndarray, values: np.ndarray) -> Optional[float]:
    if values.size < 3 or float(values.max()) <= 0:
        return None
    level = float(values.max()) * 0.5
    indices = np.flatnonzero(values >= level)
    if not len(indices):
        return None
    return float(distance[indices[-1]] - distance[indices[0]])


def frame_binary(frame: FrameData) -> bytes:
    chunks = [frame.tps.astype("<f4", copy=False).tobytes(order="C")]
    if frame.mc is not None:
        chunks.append(frame.mc.astype("<f4", copy=False).tobytes(order="C"))
    return b"".join(chunks)


def profile_csv(dataset: LineDoseDataset, payload: dict) -> str:
    profile = dataset.profile(
        payload.get("p1", []),
        payload.get("p2", []),
        int(payload.get("samples", 512)),
        str(payload.get("normalization", "absolute")),
    )
    layers = {layer["id"]: layer for layer in profile["layers"]}
    out = io.StringIO()
    out.write("\ufeff")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["# TPS-TOPAS interactive line dose"])
    writer.writerow(["# Selected TPS RTDOSE", str(dataset.dose_path)])
    writer.writerow(["# TPS dose label", dataset.tps_label])
    writer.writerow(
        [
            "# TPS DoseType / DoseSummationType / DoseUnits",
            dataset.tps_dose_type,
            dataset.tps_summation_type,
            dataset.tps_dose_units,
        ]
    )
    writer.writerow(["# MC dose source", str(dataset.mc_path) if dataset.mc_path else "not available"])
    writer.writerow(["# MC source type", dataset.mc_source_type])
    writer.writerow(["# MC particle calibrated", dataset.mc_absolute_calibrated])
    writer.writerow(["# MC calibration protocol", dataset.mc_calibration_protocol])
    writer.writerow(["# MC N_plan/N_sim scale", dataset.mc_calibration_scale])
    writer.writerow(["# MC allocation snapshot", dataset.mc_calibration_allocation])
    writer.writerow(["# RTPLAN isocenter XYZ mm", *[f"{value:.6g}" for value in dataset.isocenter]])
    writer.writerow(["# normalization", profile["normalization"]])
    writer.writerow(["# display unit", profile["displayUnit"]])
    writer.writerow(["# Legacy diagnostic MC peak scale TPS_max/MC_calibrated_max", dataset.mc_peak_scale or ""])
    writer.writerow(["# start XYZ mm", *[f"{value:.6g}" for value in profile["p1"]]])
    writer.writerow(["# end XYZ mm", *[f"{value:.6g}" for value in profile["p2"]]])
    writer.writerow(["# samples", profile["samples"]])
    writer.writerow([])
    header = [
        "distance_mm",
        "patient_x_mm",
        "patient_y_mm",
        "patient_z_mm",
        "inside_dose_grid",
        "TPS_raw_Gy",
        "TPS_display",
    ]
    if "mc" in layers:
        header += ["MC_particle_calibrated_Gy", "MC_legacy_peak_scaled_Gy", "MC_display"]
    writer.writerow(header)
    for index, distance in enumerate(profile["distanceMm"]):
        point = profile["pointsXYZmm"][index]
        row = [
            f"{distance:.8g}",
            f"{point[0]:.8g}",
            f"{point[1]:.8g}",
            f"{point[2]:.8g}",
            int(profile["insideDoseGrid"][index]),
            f"{layers['tps']['rawGy'][index]:.10g}",
            f"{layers['tps']['display'][index]:.10g}",
        ]
        if "mc" in layers:
            mc_raw = float(layers["mc"]["rawGy"][index])
            row += [
                f"{mc_raw:.10g}",
                f"{mc_raw * float(dataset.mc_peak_scale):.10g}",
                f"{layers['mc']['display'][index]:.10g}",
            ]
        writer.writerow(row)
    return out.getvalue()


def meta_header(meta: dict) -> str:
    """Compact JSON used by the HTTP handler's binary frame response."""
    return json.dumps(meta, ensure_ascii=True, separators=(",", ":"))
