#!/usr/bin/env python3
"""Gate a TPS case and select its supported TOPAS patient model."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np
import pydicom


ION_PLAN_UID = "1.2.840.10008.5.1.4.1.1.481.8"
IDENTITY_IOP = np.asarray([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
SCHNEIDER_HU_MIN = -1000.0
SCHNEIDER_HU_MAX = 2995.0


@dataclass
class Gate:
    name: str
    status: str
    details: str


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def discover_one(directory: Path, predicate, description: str) -> tuple[Path, object]:
    matches: list[tuple[Path, object]] = []
    for path in sorted(directory.glob("*.dcm")):
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        if predicate(ds):
            matches.append((path.resolve(), ds))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {description}, found {len(matches)}")
    return matches[0]


def gate(name: str, passed: bool, details: str, *, warning: bool = False) -> Gate:
    return Gate(name, "WARN" if warning and not passed else ("PASS" if passed else "BLOCK"), details)


def integer(dataset, keyword: str, default: int = -1) -> int:
    try:
        return int(getattr(dataset, keyword, default))
    except (TypeError, ValueError):
        return default


def hu_pixels(dataset) -> np.ndarray:
    return (
        dataset.pixel_array.astype(np.float64)
        * float(getattr(dataset, "RescaleSlope", 1.0))
        + float(getattr(dataset, "RescaleIntercept", 0.0))
    )


def select_external_roi(structure) -> dict[str, object] | None:
    names = {
        int(item.ROINumber): str(getattr(item, "ROIName", ""))
        for item in getattr(structure, "StructureSetROISequence", [])
    }
    interpreted = {
        int(getattr(item, "ReferencedROINumber", -1)): str(
            getattr(item, "RTROIInterpretedType", "")
        ).upper()
        for item in getattr(structure, "RTROIObservationsSequence", [])
    }
    contour_by_number = {
        int(getattr(item, "ReferencedROINumber", -1)): item
        for item in getattr(structure, "ROIContourSequence", [])
    }
    candidates: list[tuple[int, int, str, str]] = []
    for number, name in names.items():
        normalized = "".join(character for character in name.casefold() if character.isalnum())
        roi_type = interpreted.get(number, "")
        priority = 0
        if roi_type == "EXTERNAL":
            priority = 100
        elif normalized == "external":
            priority = 90
        elif normalized.startswith("external"):
            priority = 80
        elif normalized in {"body", "skin", "e"}:
            priority = 60
        if priority and number in contour_by_number:
            candidates.append((priority, number, name, roi_type))
    if not candidates:
        return None
    _, number, name, roi_type = max(candidates, key=lambda item: (item[0], item[1]))
    points: list[np.ndarray] = []
    for contour in getattr(contour_by_number[number], "ContourSequence", []):
        values = np.asarray(getattr(contour, "ContourData", []), dtype=float)
        if values.size and values.size % 3 == 0:
            points.extend(values.reshape(-1, 3))
    array = np.asarray(points, dtype=float)
    rectangular = False
    bounds_min: list[float] | None = None
    bounds_max: list[float] | None = None
    if array.ndim == 2 and array.shape[1] == 3 and array.size:
        unique_x = np.unique(np.round(array[:, 0], 5))
        unique_y = np.unique(np.round(array[:, 1], 5))
        rectangular = unique_x.size == 2 and unique_y.size == 2
        bounds_min = array.min(axis=0).tolist()
        bounds_max = array.max(axis=0).tolist()
    return {
        "number": number,
        "name": name,
        "interpreted_type": roi_type,
        "point_count": len(points),
        "rectangular_xy": rectangular,
        "bounds_min_mm": bounds_min,
        "bounds_max_mm": bounds_max,
    }


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    dicom = root / "dicom"
    output_dir = (args.output_dir or root / "plan_parsed").expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "compatibility_checks.csv"
    summary_path = output_dir / "compatibility_summary.txt"
    model_path = output_dir / "patient_model.json"
    if not args.overwrite and any(path.exists() for path in (csv_path, summary_path, model_path)):
        raise RuntimeError("Compatibility outputs exist; add --overwrite")

    plan_path, plan = discover_one(
        dicom / "RTPLAN",
        lambda ds: str(getattr(ds, "SOPClassUID", "")) == ION_PLAN_UID,
        "RT Ion Plan",
    )
    dose_path, dose = discover_one(
        dicom / "RTDOSE",
        lambda ds: str(getattr(ds, "DoseType", "")).upper() == "PHYSICAL"
        and str(getattr(ds, "DoseSummationType", "")).upper() == "PLAN",
        "physical plan RTDOSE",
    )
    structure_path, structure = discover_one(
        dicom / "RTSTRUCT",
        lambda ds: str(getattr(ds, "Modality", "")) == "RTSTRUCT",
        "RTSTRUCT",
    )
    ct_paths = sorted((dicom / "CT").glob("*.dcm"))
    if not ct_paths:
        raise RuntimeError("No CT files found")
    sample_paths = [ct_paths[0], ct_paths[len(ct_paths) // 2], ct_paths[-1]]
    ct_samples = [pydicom.dcmread(path) for path in sample_paths]
    descriptions = " ".join(
        str(getattr(ct_samples[0], keyword, ""))
        for keyword in ("StudyDescription", "SeriesDescription", "ProtocolName", "ManufacturerModelName")
    ).casefold()
    uniform_samples = True
    sample_hu: list[float] = []
    for sample in ct_samples:
        pixels = hu_pixels(sample)
        uniform_samples = uniform_samples and bool(np.allclose(pixels, pixels.flat[0]))
        sample_hu.append(float(pixels.flat[0]))
    artificial_phantom = "phantom" in descriptions or "artificial" in descriptions
    water_phantom = bool(
        uniform_samples
        and artificial_phantom
        and all(np.isclose(value, 0.0) for value in sample_hu)
    )
    ct_headers = [pydicom.dcmread(path, stop_before_pixels=True) for path in ct_paths]
    ct_iops = [np.asarray(getattr(item, "ImageOrientationPatient", []), dtype=float) for item in ct_headers]
    ct_z = sorted(float(item.ImagePositionPatient[2]) for item in ct_headers)
    ct_spacing_z = np.diff(ct_z)
    axial_regular_ct = bool(
        all(iop.shape == (6,) and np.allclose(iop, IDENTITY_IOP) for iop in ct_iops)
        and all(
            integer(item, "Rows") == integer(ct_headers[0], "Rows")
            and integer(item, "Columns") == integer(ct_headers[0], "Columns")
            and np.allclose(
                np.asarray(getattr(item, "PixelSpacing", []), dtype=float),
                np.asarray(getattr(ct_headers[0], "PixelSpacing", []), dtype=float),
            )
            for item in ct_headers
        )
        and (len(ct_z) == 1 or (np.all(ct_spacing_z > 0) and np.allclose(ct_spacing_z, ct_spacing_z[0])))
    )

    hu_min = float("inf")
    hu_max = float("-inf")
    voxel_count = 0
    below_count = 0
    above_count = 0
    extreme_low_count = 0
    if not water_phantom:
        for path in ct_paths:
            pixels = hu_pixels(pydicom.dcmread(path))
            hu_min = min(hu_min, float(np.min(pixels)))
            hu_max = max(hu_max, float(np.max(pixels)))
            voxel_count += int(pixels.size)
            below_count += int(np.count_nonzero(pixels < SCHNEIDER_HU_MIN))
            above_count += int(np.count_nonzero(pixels > SCHNEIDER_HU_MAX))
            extreme_low_count += int(np.count_nonzero(pixels <= -4096.0))
    else:
        hu_min = hu_max = 0.0
        voxel_count = sum(integer(sample, "Rows", 0) * integer(sample, "Columns", 0) for sample in ct_samples)

    external = select_external_roi(structure)
    rectangular_external = bool(external and external["rectangular_xy"])
    beams = list(getattr(plan, "IonBeamSequence", []))
    first_cp = beams[0].IonControlPointSequence[0] if beams else None
    carbon_12 = bool(
        len(beams) == 1
        and integer(beams[0], "RadiationAtomicNumber") == 6
        and integer(beams[0], "RadiationMassNumber") == 12
        and integer(beams[0], "RadiationChargeState") == 6
    )
    scan_mode = str(getattr(beams[0], "ScanMode", "")) if beams else ""
    setup = list(getattr(plan, "PatientSetupSequence", []))
    patient_position = str(getattr(setup[0], "PatientPosition", "")) if setup else ""
    gantry = float(getattr(first_cp, "GantryAngle", float("nan"))) if first_cp else float("nan")
    couch = float(getattr(first_cp, "PatientSupportAngle", 0.0)) if first_cp else float("nan")
    pitch = float(getattr(first_cp, "TableTopPitchAngle", 0.0)) if first_cp else float("nan")
    roll = float(getattr(first_cp, "TableTopRollAngle", 0.0)) if first_cp else float("nan")
    iop = np.asarray(getattr(dose, "ImageOrientationPatient", []), dtype=float)
    offsets = np.asarray(getattr(dose, "GridFrameOffsetVector", []), dtype=float)
    pixel_spacing = np.asarray(getattr(dose, "PixelSpacing", []), dtype=float)
    modulators = [
        str(getattr(item, "RangeModulatorID", ""))
        for beam in beams
        for item in getattr(beam, "RangeModulatorSequence", [])
    ]

    patient_mode = "WATER_PHANTOM" if water_phantom else "DICOM_CT_SCHNEIDER"
    outside_count = below_count + above_count
    outside_fraction = outside_count / voxel_count if voxel_count else 0.0
    checks = [
        gate("SINGLE_BEAM", len(beams) == 1, f"beam count={len(beams)}"),
        gate(
            "CARBON_12_FULLY_STRIPPED",
            carbon_12,
            "RadiationAtomicNumber/MassNumber/ChargeState="
            f"{integer(beams[0], 'RadiationAtomicNumber') if beams else 'missing'}/"
            f"{integer(beams[0], 'RadiationMassNumber') if beams else 'missing'}/"
            f"{integer(beams[0], 'RadiationChargeState') if beams else 'missing'}; expected 6/12/6",
        ),
        gate("PBS_SCAN_MODE", scan_mode.upper() == "MODULATED", f"ScanMode={scan_mode}"),
        gate("PATIENT_POSITION_HFS", patient_position == "HFS", f"PatientPosition={patient_position}"),
        gate("GANTRY_90", np.isclose(gantry, 90.0), f"gantry={gantry} deg"),
        gate("COUCH_0", np.isclose(couch, 0.0), f"couch={couch} deg"),
        gate("PITCH_ROLL_0", np.isclose(pitch, 0.0) and np.isclose(roll, 0.0), f"pitch/roll={pitch}/{roll} deg"),
        gate("RPPD_AXIS_ALIGNED", iop.shape == (6,) and np.allclose(iop, IDENTITY_IOP), f"ImageOrientationPatient={iop.tolist()}"),
        gate(
            "RPPD_REGULAR_GRID",
            offsets.size >= 2 and np.all(np.diff(offsets) > 0) and np.allclose(np.diff(offsets), np.diff(offsets)[0]) and pixel_spacing.shape == (2,) and np.all(pixel_spacing > 0),
            f"frames={offsets.size}, PixelSpacing={pixel_spacing.tolist()}",
        ),
        gate(
            "SUPPORTED_PATIENT_MODEL",
            water_phantom or axial_regular_ct,
            (
                f"mode={patient_mode}; uniform 0-HU artificial CT and rectangular External are used as a water box"
                if water_phantom
                else f"mode={patient_mode}; {len(ct_paths)} CT slices, HU={hu_min:g}..{hu_max:g}, generic Schneider conversion"
            ),
        ),
        gate(
            "CT_AXIAL_REGULAR_GEOMETRY",
            axial_regular_ct,
            f"slices={len(ct_paths)}, rows/columns={integer(ct_headers[0], 'Rows')}/{integer(ct_headers[0], 'Columns')}, ImageOrientationPatient={ct_iops[0].tolist() if ct_iops else []}, slice spacing={float(ct_spacing_z[0]) if ct_spacing_z.size else 'single slice'} mm; current TsDicomPatient mapping requires regular axial identity orientation",
        ),
        gate(
            "EXTERNAL_ROI_SELECTION",
            external is not None,
            (
                f"selected ROI {external['number']} '{external['name']}', type={external['interpreted_type'] or 'unspecified'}, points={external['point_count']}"
                if external
                else "no ROI identified by interpreted type EXTERNAL or common body/external names"
            ),
            warning=not water_phantom,
        ),
        gate(
            "RECTANGULAR_EXTERNAL_FOR_WATER_PHANTOM",
            (not water_phantom) or rectangular_external,
            (
                "not required: DICOM CT voxels define patient mass geometry"
                if not water_phantom
                else f"selected External rectangular XY={rectangular_external}"
            ),
        ),
        gate(
            "HU_TO_MATERIAL_CALIBRATION",
            water_phantom,
            (
                "not applicable: uniform water-box model"
                if water_phantom
                else f"generic TOPAS Schneider table only; institution/scanner calibration not supplied; {extreme_low_count} extreme-low voxels (typically scan-FOV padding), {below_count - extreme_low_count} other underflow voxels and {above_count} overflow voxels will be clamped to {SCHNEIDER_HU_MIN:g}/{SCHNEIDER_HU_MAX:g} HU"
            ),
            warning=True,
        ),
        gate(
            "RANGE_MODULATOR_COMMISSIONED",
            not modulators,
            f"range modulators={modulators or ['none']}; physical geometry/WET is not commissioned",
            warning=True,
        ),
    ]
    blockers = [item for item in checks if item.status == "BLOCK"]
    warnings = [item for item in checks if item.status == "WARN"]
    ready = not blockers

    patient_model = {
        "schema_version": 1,
        "mode": patient_mode,
        "ready_for_research_qa": ready,
        "ct_directory": str((dicom / "CT").resolve()),
        "ct_slices": len(ct_paths),
        "ct_series_instance_uid": str(getattr(ct_samples[0], "SeriesInstanceUID", "")),
        "hu_statistics": {
            "minimum": hu_min,
            "maximum": hu_max,
            "voxel_count": voxel_count,
            "below_schneider_range": below_count,
            "above_schneider_range": above_count,
            "extreme_low_at_or_below_minus4096": extreme_low_count,
            "outside_schneider_fraction": outside_fraction,
        },
        "converter": {
            "name": "Schneider" if not water_phantom else "G4_WATER",
            "table": "machine_model/HUtoMaterialSchneider.txt" if not water_phantom else None,
            "hu_range": [SCHNEIDER_HU_MIN, SCHNEIDER_HU_MAX] if not water_phantom else None,
            "commissioning_status": "GENERIC_UNCOMMISSIONED" if not water_phantom else "NOT_APPLICABLE",
        },
        "external_roi": external,
        "sources": {
            "rtplan": str(plan_path),
            "rtdose": str(dose_path),
            "rtstruct": str(structure_path),
        },
    }
    model_path.write_text(json.dumps(patient_model, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["Check", "Status", "Details"])
        writer.writerows((item.name, item.status, item.details) for item in checks)

    summary = [
        "TPS-TOPAS case compatibility gate",
        "=================================",
        f"Project: {root}",
        f"RTPLAN: {plan_path}",
        f"RPPD: {dose_path}",
        f"Patient model: {patient_mode}",
        "",
        f"Result: {'READY FOR CURRENT QA WORKFLOW' if ready else 'BLOCKED FOR CURRENT TOPAS WORKFLOW'}",
        f"Checks: {len(checks) - len(blockers) - len(warnings)} PASS, {len(warnings)} WARN, {len(blockers)} BLOCK",
        "",
    ]
    summary.extend(f"[{item.status}] {item.name}: {item.details}" for item in checks)
    summary.extend(
        [
            "",
            "Scope",
            "-----",
            "READY means that geometry can enter the research physical-dose shape QA workflow.",
            "Water phantoms use an RTSTRUCT-derived G4_WATER box. Patient CT uses TOPAS",
            "TsDicomPatient with a generic Schneider HU-to-material table. The generic table",
            "is not a substitute for institution/scanner-specific calibration or commissioning.",
            "The beam model and range-modulator geometry/WET also remain uncommissioned.",
        ]
    )
    summary_path.write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(summary_path.read_text(encoding="utf-8"))
    return 2 if blockers else 0


if __name__ == "__main__":
    raise SystemExit(main())
