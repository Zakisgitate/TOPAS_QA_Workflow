#!/usr/bin/env python3
"""Validate the zero-history TOPAS DoseToMedium grid against TPS RPPD metadata."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pydicom


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root, help=f"Project root (default: {root})")
    parser.add_argument("--rppd", type=Path, help="TPS plan physical RTDOSE")
    parser.add_argument("--rtplan", type=Path, help="RT Ion Plan")
    parser.add_argument("--dose-grid-parameters", type=Path, help="TOPAS dose_grid.txt")
    parser.add_argument("--header", type=Path, help="TOPAS .binheader validation output")
    parser.add_argument("--binary", type=Path, help="TOPAS .bin validation output")
    parser.add_argument("--summary-output", type=Path, help="Validation summary output")
    parser.add_argument("--overwrite", action="store_true", help="Replace derived summary")
    return parser.parse_args()


def discover_rppd(root: Path) -> Path:
    matches: list[Path] = []
    for path in sorted((root / "dicom" / "RTDOSE").glob("*.dcm")):
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        if (
            str(getattr(ds, "DoseUnits", "")).upper() == "GY"
            and str(getattr(ds, "DoseType", "")).upper() == "PHYSICAL"
            and str(getattr(ds, "DoseSummationType", "")).upper() == "PLAN"
        ):
            matches.append(path.resolve())
    if len(matches) != 1:
        raise RuntimeError(f"Expected one GY/PHYSICAL/PLAN RTDOSE, found {matches}")
    return matches[0]


def discover_rtplan(root: Path) -> Path:
    matches: list[Path] = []
    expected_uid = "1.2.840.10008.5.1.4.1.1.481.8"
    for path in sorted((root / "dicom" / "RTPLAN").glob("*.dcm")):
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        if str(getattr(ds, "SOPClassUID", "")) == expected_uid:
            matches.append(path.resolve())
    if len(matches) != 1:
        raise RuntimeError(f"Expected one RT Ion Plan, found {matches}")
    return matches[0]


def plan_isocenter(plan) -> np.ndarray:
    values: list[np.ndarray] = []
    for beam in getattr(plan, "IonBeamSequence", []):
        for cp in getattr(beam, "IonControlPointSequence", []):
            if getattr(cp, "IsocenterPosition", None) is not None:
                values.append(np.asarray(cp.IsocenterPosition, dtype=float))
                break
    if not values or not all(np.allclose(values[0], item, atol=1e-8) for item in values):
        raise RuntimeError(f"RT Ion Plan isocenter is absent or inconsistent: {values}")
    return values[0]


def topas_mm_parameter(text: str, name: str) -> float:
    match = re.search(
        rf"^d:Ge/TPSDoseGrid/{name}\s*=\s*([0-9.eE+-]+)\s+mm\s*$",
        text,
        re.MULTILINE,
    )
    if not match:
        raise RuntimeError(f"TOPAS dose grid lacks {name} in mm")
    return float(match.group(1))


def header_axis(header: str, axis: str) -> tuple[int, float, str]:
    match = re.search(
        rf"^# {axis} in (\d+) bins of ([0-9.eE+-]+) (\S+)\s*$", header, re.MULTILINE
    )
    if not match:
        raise RuntimeError(f"TOPAS header lacks a valid {axis}-bin record")
    return int(match.group(1)), float(match.group(2)), match.group(3)


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    rppd_path = args.rppd.resolve() if args.rppd else discover_rppd(root)
    rtplan_path = args.rtplan.resolve() if args.rtplan else discover_rtplan(root)
    parameters_path = (
        args.dose_grid_parameters or root / "topas" / "scoring" / "dose_grid.txt"
    ).resolve()
    header_path = (
        args.header or root / "topas_output" / "test" / "dose_grid_zero_validation.binheader"
    ).resolve()
    binary_path = (
        args.binary or root / "topas_output" / "test" / "dose_grid_zero_validation.bin"
    ).resolve()
    summary_path = (
        args.summary_output
        or root / "plan_parsed" / "topas_dose_grid_validation_summary.txt"
    ).resolve()
    for path in (rppd_path, rtplan_path, parameters_path, header_path, binary_path):
        if not path.is_file():
            raise RuntimeError(f"Required validation input does not exist: {path}")
    dicom_root = (root / "dicom").resolve()
    try:
        summary_path.relative_to(dicom_root)
    except ValueError:
        pass
    else:
        raise RuntimeError(f"Validation summary cannot be written inside dicom/: {summary_path}")
    if summary_path.exists() and not args.overwrite:
        raise RuntimeError(f"Validation summary exists: {summary_path}; add --overwrite")
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    dose = pydicom.dcmread(rppd_path, stop_before_pixels=True)
    plan = pydicom.dcmread(rtplan_path, stop_before_pixels=True)
    expected_shape = (
        int(dose.NumberOfFrames),
        int(dose.Rows),
        int(dose.Columns),
    )
    expected_spacing_xyz_mm = np.asarray(
        [
            float(dose.PixelSpacing[1]),
            float(dose.PixelSpacing[0]),
            float(np.diff(np.asarray(dose.GridFrameOffsetVector, dtype=float))[0]),
        ]
    )
    ipp = np.asarray(dose.ImagePositionPatient, dtype=float)
    offsets = np.asarray(dose.GridFrameOffsetVector, dtype=float)
    last_center = ipp + np.asarray(
        [
            (expected_shape[2] - 1) * expected_spacing_xyz_mm[0],
            (expected_shape[1] - 1) * expected_spacing_xyz_mm[1],
            offsets[-1],
        ]
    )
    edge_min = ipp - 0.5 * expected_spacing_xyz_mm
    edge_max = last_center + 0.5 * expected_spacing_xyz_mm
    expected_half_lengths_mm = 0.5 * (edge_max - edge_min)
    isocenter = plan_isocenter(plan)
    expected_translation_mm = 0.5 * (edge_min + edge_max) - isocenter

    parameters = parameters_path.read_text(encoding="utf-8")
    observed_half_lengths_mm = np.asarray(
        [topas_mm_parameter(parameters, f"HL{axis}") for axis in "XYZ"]
    )
    observed_translation_mm = np.asarray(
        [topas_mm_parameter(parameters, f"Trans{axis}") for axis in "XYZ"]
    )
    if not np.allclose(observed_half_lengths_mm, expected_half_lengths_mm, atol=1e-8):
        raise RuntimeError(
            f"TOPAS half lengths {observed_half_lengths_mm} != TPS {expected_half_lengths_mm}"
        )
    if not np.allclose(observed_translation_mm, expected_translation_mm, atol=1e-8):
        raise RuntimeError(
            f"TOPAS translation {observed_translation_mm} != TPS {expected_translation_mm}"
        )
    header = header_path.read_text(encoding="utf-8")
    axes = {axis: header_axis(header, axis) for axis in "XYZ"}
    observed_bins_xyz = tuple(axes[axis][0] for axis in "XYZ")
    observed_widths_mm = np.asarray(
        [axes[axis][1] * (10.0 if axes[axis][2] == "cm" else 1.0) for axis in "XYZ"]
    )
    if any(axes[axis][2] not in {"mm", "cm"} for axis in "XYZ"):
        raise RuntimeError(f"Unexpected TOPAS grid-width unit: {axes}")
    expected_bins_xyz = (expected_shape[2], expected_shape[1], expected_shape[0])
    if observed_bins_xyz != expected_bins_xyz:
        raise RuntimeError(
            f"TOPAS bin counts {observed_bins_xyz} != TPS {expected_bins_xyz}"
        )
    if not np.allclose(observed_widths_mm, expected_spacing_xyz_mm, atol=1e-10):
        raise RuntimeError(
            f"TOPAS spacing {observed_widths_mm} != TPS {expected_spacing_xyz_mm} mm"
        )
    required_header_text = (
        "# Results for scorer: TPSDoseToMedium",
        "# Scored in component: TPSDoseGrid",
        "# DoseToMedium ( Gy ) : Sum",
    )
    for text in required_header_text:
        if text not in header:
            raise RuntimeError(f"TOPAS header lacks: {text}")

    values = np.fromfile(binary_path, dtype=np.float64)
    expected_voxels = int(np.prod(expected_shape, dtype=np.int64))
    if values.size != expected_voxels:
        raise RuntimeError(
            f"TOPAS binary has {values.size} values; expected {expected_voxels}"
        )
    dose_zyx = values.reshape(expected_shape)
    if not np.isfinite(dose_zyx).all():
        raise RuntimeError("Zero-history TOPAS binary contains non-finite values")
    if np.count_nonzero(dose_zyx):
        raise RuntimeError("Zero-history TOPAS initialization unexpectedly scored nonzero dose")

    summary = f"""TPS-TOPAS dose-grid validation
==================================
Status: PASS

Validation scope
----------------
TOPAS initialized the complete DoseToMedium scorer with zero transported histories.
This validates configuration and output geometry, not dose transport or dose agreement.

Inputs
------
TPS RPPD: {rppd_path}
RT Ion Plan: {rtplan_path}
TOPAS grid parameters: {parameters_path}
TOPAS header: {header_path}
TOPAS binary: {binary_path}

Checks
------
Scorer / component: TPSDoseToMedium / TPSDoseGrid
Quantity / report: DoseToMedium (Gy) / Sum
TPS pydicom shape [Z,Y,X]: {expected_shape}
TOPAS bins [X,Y,Z]: {observed_bins_xyz}
TPS spacing [X,Y,Z]: {expected_spacing_xyz_mm.tolist()} mm
TOPAS spacing [X,Y,Z]: {observed_widths_mm.tolist()} mm
TPS-derived grid half lengths [X,Y,Z]: {expected_half_lengths_mm.tolist()} mm
TOPAS grid half lengths [X,Y,Z]: {observed_half_lengths_mm.tolist()} mm
TPS-derived translation from isocenter [X,Y,Z]: {expected_translation_mm.tolist()} mm
TOPAS grid translation [X,Y,Z]: {observed_translation_mm.tolist()} mm
Voxel count: {expected_voxels}
Binary byte count: {binary_path.stat().st_size} (= {expected_voxels} float64 values)
Validated reshape: {dose_zyx.shape}
Non-finite / nonzero values: 0 / 0 (expected for zero histories)

Result
------
PASS: the TOPAS output grid exactly matches the TPS RPPD array dimensions and spacing.
The binary can be read as numpy.float64 and reshaped directly to {list(expected_shape)}.
No low-statistics or production dose calculation was performed in this stage.
"""
    summary_path.write_text(summary, encoding="utf-8")
    print(f"PASS: wrote validation summary: {summary_path}")
    print(f"TOPAS bins XYZ={observed_bins_xyz}, TPS shape ZYX={expected_shape}")
    print(f"Binary values={values.size}, bytes={binary_path.stat().st_size}, nonzero=0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
