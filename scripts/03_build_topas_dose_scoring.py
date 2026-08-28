#!/usr/bin/env python3
"""Build the TOPAS DoseToMedium scorer on the TPS plan-physical-dose grid.

The source DICOM tree is read-only. The generated TOPAS parameter files and
audit summary are derived outputs outside ``dicom/``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np
import pydicom
from pydicom.dataset import Dataset


RT_ION_PLAN_STORAGE_UID = "1.2.840.10008.5.1.4.1.1.481.8"
RT_DOSE_STORAGE_UID = "1.2.840.10008.5.1.4.1.1.481.2"
IDENTITY_IOP = np.asarray([1.0, 0.0, 0.0, 0.0, 1.0, 0.0])


@dataclass(frozen=True)
class DoseGrid:
    source: Path
    sop_instance_uid: str
    frame_of_reference_uid: str
    shape_zyx: tuple[int, int, int]
    spacing_xyz_mm: np.ndarray
    first_center_xyz_mm: np.ndarray
    last_center_xyz_mm: np.ndarray
    edge_min_xyz_mm: np.ndarray
    edge_max_xyz_mm: np.ndarray
    center_xyz_mm: np.ndarray


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root, help=f"Project root (default: {root})")
    parser.add_argument("--rppd", type=Path, help="TPS plan physical RTDOSE")
    parser.add_argument("--rtplan", type=Path, help="RT Ion Plan used to obtain isocenter")
    parser.add_argument("--dose-grid-output", type=Path, help="Output dose_grid.txt")
    parser.add_argument("--dose-output", type=Path, help="Output dose.txt")
    parser.add_argument("--summary-output", type=Path, help="Output audit summary")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing derived outputs; never modifies input DICOM",
    )
    return parser.parse_args()


def metadata(path: Path) -> Dataset:
    try:
        return pydicom.dcmread(path, stop_before_pixels=True)
    except Exception as exc:
        raise RuntimeError(f"Cannot read DICOM metadata: {path}: {exc}") from exc


def discover_one(directory: Path, predicate, description: str) -> Path:
    matches: list[Path] = []
    for path in sorted(directory.rglob("*.dcm")):
        ds = metadata(path)
        if predicate(ds):
            matches.append(path.resolve())
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {description}, found {len(matches)}: {matches}")
    return matches[0]


def resolve_inputs(args: argparse.Namespace) -> tuple[Path, Path]:
    root = args.root.resolve()
    dicom = root / "dicom"
    rppd = (
        args.rppd.resolve()
        if args.rppd
        else discover_one(
            dicom / "RTDOSE",
            lambda ds: (
                str(getattr(ds, "SOPClassUID", "")) == RT_DOSE_STORAGE_UID
                and str(getattr(ds, "DoseType", "")).upper() == "PHYSICAL"
                and str(getattr(ds, "DoseSummationType", "")).upper() == "PLAN"
            ),
            "PHYSICAL/PLAN RTDOSE",
        )
    )
    rtplan = (
        args.rtplan.resolve()
        if args.rtplan
        else discover_one(
            dicom / "RTPLAN",
            lambda ds: str(getattr(ds, "SOPClassUID", "")) == RT_ION_PLAN_STORAGE_UID,
            "RT Ion Plan",
        )
    )
    return rppd, rtplan


def resolve_outputs(args: argparse.Namespace) -> tuple[Path, Path, Path]:
    root = args.root.resolve()
    return (
        (args.dose_grid_output or root / "topas" / "scoring" / "dose_grid.txt").resolve(),
        (args.dose_output or root / "topas" / "scoring" / "dose.txt").resolve(),
        (
            args.summary_output
            or root / "plan_parsed" / "topas_dose_scoring_summary.txt"
        ).resolve(),
    )


def ensure_safe_outputs(
    root: Path,
    inputs: Sequence[Path],
    outputs: Sequence[Path],
    overwrite: bool,
) -> None:
    dicom_root = (root / "dicom").resolve()
    for path in inputs:
        if not path.is_file():
            raise RuntimeError(f"Input does not exist: {path}")
    for path in outputs:
        if path in inputs:
            raise RuntimeError(f"Refusing to overwrite DICOM input: {path}")
        try:
            path.relative_to(dicom_root)
        except ValueError:
            pass
        else:
            raise RuntimeError(f"Derived output must not be inside DICOM tree: {path}")
        if path.exists() and not overwrite:
            raise RuntimeError(f"Derived output exists: {path}; inspect it or add --overwrite")
        path.parent.mkdir(parents=True, exist_ok=True)


def get_plan_isocenter(plan: Dataset) -> np.ndarray:
    isocenters: list[np.ndarray] = []
    for beam in getattr(plan, "IonBeamSequence", []):
        for cp in getattr(beam, "IonControlPointSequence", []):
            value = getattr(cp, "IsocenterPosition", None)
            if value is not None:
                isocenters.append(np.asarray(value, dtype=float))
                break
    if not isocenters:
        raise RuntimeError("RT Ion Plan contains no IsocenterPosition")
    if any(item.shape != (3,) for item in isocenters):
        raise RuntimeError("RT Ion Plan contains a malformed IsocenterPosition")
    if not all(np.allclose(isocenters[0], item, atol=1e-6) for item in isocenters[1:]):
        raise RuntimeError(f"Multiple plan isocenters are not identical: {isocenters}")
    return isocenters[0]


def get_dose_grid(path: Path, dose: Dataset) -> DoseGrid:
    if str(getattr(dose, "SOPClassUID", "")) != RT_DOSE_STORAGE_UID:
        raise RuntimeError("Selected RPPD is not RT Dose Storage")
    signature = (
        str(getattr(dose, "DoseUnits", "")).upper(),
        str(getattr(dose, "DoseType", "")).upper(),
        str(getattr(dose, "DoseSummationType", "")).upper(),
    )
    if signature != ("GY", "PHYSICAL", "PLAN"):
        raise RuntimeError(f"Expected GY/PHYSICAL/PLAN RPPD, got {signature}")

    iop = np.asarray(getattr(dose, "ImageOrientationPatient", []), dtype=float)
    if iop.shape != (6,) or not np.allclose(iop, IDENTITY_IOP, atol=1e-8):
        raise RuntimeError(
            "This TOPAS world is DICOM-axis-parallel and requires RPPD IOP "
            f"[1,0,0,0,1,0], got {iop.tolist()}"
        )
    ipp = np.asarray(getattr(dose, "ImagePositionPatient", []), dtype=float)
    pixel_spacing = np.asarray(getattr(dose, "PixelSpacing", []), dtype=float)
    offsets = np.asarray(getattr(dose, "GridFrameOffsetVector", []), dtype=float)
    if ipp.shape != (3,) or pixel_spacing.shape != (2,):
        raise RuntimeError("RPPD lacks a valid ImagePositionPatient or PixelSpacing")

    nz = int(getattr(dose, "NumberOfFrames", 0))
    ny = int(getattr(dose, "Rows", 0))
    nx = int(getattr(dose, "Columns", 0))
    if min(nx, ny, nz) <= 0 or offsets.shape != (nz,):
        raise RuntimeError("RPPD dimensions and GridFrameOffsetVector disagree")
    if nz < 2:
        raise RuntimeError("A three-dimensional RPPD grid needs at least two frames")
    offset_steps = np.diff(offsets)
    if not np.all(offset_steps > 0) or not np.allclose(
        offset_steps, offset_steps[0], atol=1e-8
    ):
        raise RuntimeError("RPPD GridFrameOffsetVector must be uniform and increasing")
    if not np.isclose(offsets[0], 0.0, atol=1e-8):
        raise RuntimeError(
            "This dataset is expected to encode frame offsets relative to ImagePositionPatient"
        )

    spacing_xyz = np.asarray([pixel_spacing[1], pixel_spacing[0], offset_steps[0]])
    if np.any(spacing_xyz <= 0):
        raise RuntimeError(f"RPPD has non-positive spacing: {spacing_xyz.tolist()}")
    first_center = ipp.copy()
    last_center = first_center + np.asarray(
        [(nx - 1) * spacing_xyz[0], (ny - 1) * spacing_xyz[1], offsets[-1]]
    )
    edge_min = first_center - 0.5 * spacing_xyz
    edge_max = last_center + 0.5 * spacing_xyz
    return DoseGrid(
        source=path,
        sop_instance_uid=str(dose.SOPInstanceUID),
        frame_of_reference_uid=str(dose.FrameOfReferenceUID),
        shape_zyx=(nz, ny, nx),
        spacing_xyz_mm=spacing_xyz,
        first_center_xyz_mm=first_center,
        last_center_xyz_mm=last_center,
        edge_min_xyz_mm=edge_min,
        edge_max_xyz_mm=edge_max,
        center_xyz_mm=0.5 * (edge_min + edge_max),
    )


def fmt(value: float) -> str:
    if np.isclose(value, 0.0, atol=5e-12):
        value = 0.0
    return f"{value:.10g}"


def fmt_vec(values: np.ndarray) -> str:
    return "[" + ", ".join(fmt(float(value)) for value in values) + "]"


def root_output_base(rppd_path: Path) -> str:
    """Return a filesystem-safe, case-specific TOPAS scorer base name."""
    stem = "".join(character if character.isalnum() else "_" for character in rppd_path.stem)
    stem = stem.strip("_") or "Plan"
    return f"{stem}_DoseToMedium_TPSGrid"


def build_dose_grid_text(grid: DoseGrid, isocenter: np.ndarray) -> str:
    nz, ny, nx = grid.shape_zyx
    full_lengths = grid.edge_max_xyz_mm - grid.edge_min_xyz_mm
    half_lengths = 0.5 * full_lengths
    translation = grid.center_xyz_mm - isocenter
    return f"""# AUTO-GENERATED FILE -- DO NOT EDIT GRID VALUES BY HAND
# Generator: scripts/03_build_topas_dose_scoring.py
# TPS reference: {grid.source}
# RPPD SOPInstanceUID: {grid.sop_instance_uid}
# DICOM [frames, rows, columns] = [{nz}, {ny}, {nx}]
# DICOM voxel centers map directly to TOPAS [Z, Y, X] binary-array indices.

includeFile = beam/plan_generated.txt

# Parallel-world grid: it cannot displace or overlap the mass-geometry patient.
# Its axes are parallel to DICOM patient X/Y/Z and its origin is the plan isocenter.
s:Ge/TPSDoseGrid/Type = "TsBox"
s:Ge/TPSDoseGrid/Parent = "World"
b:Ge/TPSDoseGrid/IsParallel = "True"
d:Ge/TPSDoseGrid/HLX = {fmt(half_lengths[0])} mm
d:Ge/TPSDoseGrid/HLY = {fmt(half_lengths[1])} mm
d:Ge/TPSDoseGrid/HLZ = {fmt(half_lengths[2])} mm
d:Ge/TPSDoseGrid/TransX = {fmt(translation[0])} mm
d:Ge/TPSDoseGrid/TransY = {fmt(translation[1])} mm
d:Ge/TPSDoseGrid/TransZ = {fmt(translation[2])} mm
d:Ge/TPSDoseGrid/RotX = 0. deg
d:Ge/TPSDoseGrid/RotY = 0. deg
d:Ge/TPSDoseGrid/RotZ = 0. deg
i:Ge/TPSDoseGrid/XBins = {nx}
i:Ge/TPSDoseGrid/YBins = {ny}
i:Ge/TPSDoseGrid/ZBins = {nz}
"""


def build_dose_text(output_base: str) -> str:
    return f"""# Physical-dose scorer on the TPS RPPD grid
# DoseToMedium is accumulated across every sequential spot run and written once
# at end of session. It is not the TPS EFFECTIVE/RBE-weighted dose.

includeFile = scoring/dose_grid.txt

s:Sc/TPSDoseToMedium/Quantity = "DoseToMedium"
s:Sc/TPSDoseToMedium/Component = "TPSDoseGrid"
sv:Sc/TPSDoseToMedium/Report = 1 "Sum"
s:Sc/TPSDoseToMedium/OutputType = "binary"
s:Sc/TPSDoseToMedium/OutputFile = "../topas_output/production/{output_base}"
s:Sc/TPSDoseToMedium/IfOutputFileAlreadyExists = "Exit"
b:Sc/TPSDoseToMedium/OutputAfterRun = "False"
b:Sc/TPSDoseToMedium/OutputToConsole = "False"
b:Sc/TPSDoseToMedium/Visualize = "False"
b:Sc/TPSDoseToMedium/Sparsify = "False"
b:Sc/TPSDoseToMedium/SingleIndex = "False"
"""


def build_summary(
    grid: DoseGrid,
    plan_path: Path,
    plan: Dataset,
    isocenter: np.ndarray,
    outputs: Sequence[Path],
) -> str:
    nz, ny, nx = grid.shape_zyx
    half_lengths = 0.5 * (grid.edge_max_xyz_mm - grid.edge_min_xyz_mm)
    translation = grid.center_xyz_mm - isocenter
    first_topas = grid.first_center_xyz_mm - isocenter
    last_topas = grid.last_center_xyz_mm - isocenter
    return f"""TPS-TOPAS DoseToMedium scoring-grid summary
===============================================
Status: CONFIGURED

Read-only source DICOM
----------------------
RPPD: {grid.source}
RPPD SOPInstanceUID: {grid.sop_instance_uid}
RT Ion Plan: {plan_path}
RTPLAN SOPInstanceUID: {plan.SOPInstanceUID}
FrameOfReferenceUID: {grid.frame_of_reference_uid}
Dose identity: GY / PHYSICAL / PLAN

Exact TPS grid
--------------
DICOM array shape [frames Z, rows Y, columns X]: [{nz}, {ny}, {nx}]
TOPAS bins [X, Y, Z]: [{nx}, {ny}, {nz}]
Voxel spacing [X, Y, Z]: {fmt_vec(grid.spacing_xyz_mm)} mm
First voxel center DICOM [X, Y, Z]: {fmt_vec(grid.first_center_xyz_mm)} mm
Last voxel center DICOM [X, Y, Z]: {fmt_vec(grid.last_center_xyz_mm)} mm
Voxel edge bounds DICOM: {fmt_vec(grid.edge_min_xyz_mm)} .. {fmt_vec(grid.edge_max_xyz_mm)} mm
Grid center DICOM: {fmt_vec(grid.center_xyz_mm)} mm

TOPAS placement
---------------
TOPAS origin / plan isocenter DICOM: {fmt_vec(isocenter)} mm
Grid half lengths [X, Y, Z]: {fmt_vec(half_lengths)} mm
Grid translation [X, Y, Z]: {fmt_vec(translation)} mm
First voxel center TOPAS [X, Y, Z]: {fmt_vec(first_topas)} mm
Last voxel center TOPAS [X, Y, Z]: {fmt_vec(last_topas)} mm
Grid type: parallel-world TsBox (does not replace or overlap mass geometry)

Scoring and output
------------------
Quantity: DoseToMedium, report Sum, unit Gy
Output: native TOPAS binary (one double per voxel) plus .binheader
OutputAfterRun: False, so all sequential spot runs accumulate into one grid
Production collision policy: Exit (no silent overwrite)
Production output base: topas_output/production/{root_output_base(grid.source)}

TOPAS binary index mapping
--------------------------
TOPAS writes X fastest, then Y, then Z. On this machine, read with:
  dose_zyx = numpy.fromfile(path, dtype=numpy.float64).reshape(({nz}, {ny}, {nx}))
This shape/order matches pydicom's RPPD pixel_array [frame, row, column].

Generated outputs
-----------------
""" + "".join(f"{path}\n" for path in outputs) + """

Scope guard
-----------
This stage configures the physical-dose grid only. It does not run a low-statistics
or production transport calculation, does not compare dose values, and does not
convert relative meterset weight to absolute particles/MU. RPED EFFECTIVE dose is
not used as the physical-dose reference.
"""


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    rppd_path, plan_path = resolve_inputs(args)
    dose_grid_output, dose_output, summary_output = resolve_outputs(args)
    outputs = (dose_grid_output, dose_output, summary_output)
    ensure_safe_outputs(root, (rppd_path, plan_path), outputs, args.overwrite)

    dose = metadata(rppd_path)
    plan = metadata(plan_path)
    grid = get_dose_grid(rppd_path, dose)
    isocenter = get_plan_isocenter(plan)
    plan_frame_uid = str(getattr(plan, "FrameOfReferenceUID", ""))
    if plan_frame_uid != grid.frame_of_reference_uid:
        raise RuntimeError(
            "RPPD and RTPLAN FrameOfReferenceUID differ: "
            f"{grid.frame_of_reference_uid} != {plan_frame_uid}"
        )

    output_base = root_output_base(rppd_path)
    dose_grid_output.write_text(build_dose_grid_text(grid, isocenter), encoding="utf-8")
    dose_output.write_text(build_dose_text(output_base), encoding="utf-8")
    summary_output.write_text(
        build_summary(grid, plan_path, plan, isocenter, outputs), encoding="utf-8"
    )
    print(f"Wrote exact TPS-aligned dose grid: {dose_grid_output}")
    print(f"Wrote DoseToMedium scorer: {dose_output}")
    print(f"Wrote audit summary: {summary_output}")
    print(f"Grid: TOPAS X/Y/Z={grid.shape_zyx[2]}/{grid.shape_zyx[1]}/{grid.shape_zyx[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
