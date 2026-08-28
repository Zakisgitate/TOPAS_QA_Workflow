#!/usr/bin/env python3
"""Check CT/RTPLAN/RTDOSE/RTSTRUCT geometry and reference consistency.

The script treats ``dicom/`` as read-only. Reports and the geometry overview are
written to ``plan_parsed/`` (or an explicitly selected output directory).
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

_cache_root = Path(tempfile.gettempdir()) / "plan1699-python-cache"
os.environ.setdefault("MPLCONFIGDIR", str(_cache_root / "matplotlib"))
os.environ.setdefault("XDG_CACHE_HOME", str(_cache_root))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pydicom
from pydicom.dataset import Dataset


LOGGER = logging.getLogger("dicom_geometry")
ION_PLAN_STORAGE_UID = "1.2.840.10008.5.1.4.1.1.481.8"


@dataclass
class Check:
    name: str
    status: str
    details: str


def parse_args() -> argparse.Namespace:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=project_root,
        help=f"Project root (default: {project_root})",
    )
    parser.add_argument(
        "--dicom-dir",
        type=Path,
        help="DICOM input directory (default: ROOT/dicom)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Derived output directory (default: ROOT/plan_parsed)",
    )
    parser.add_argument(
        "--no-plot",
        action="store_true",
        help="Skip loading CT pixels and geometry_overview.png generation",
    )
    return parser.parse_args()


def as_text(value: object, default: str = "MISSING") -> str:
    if value is None or value == "":
        return default
    return str(value)


def as_float_array(value: object, expected: int | None = None) -> np.ndarray | None:
    if value is None:
        return None
    try:
        result = np.asarray(value, dtype=float)
    except (TypeError, ValueError):
        return None
    if expected is not None and result.size != expected:
        return None
    return result


def unique_text(datasets: Iterable[Dataset], keyword: str) -> list[str]:
    return sorted({as_text(getattr(ds, keyword, None)) for ds in datasets})


def add_check(checks: list[Check], name: str, passed: bool, details: str) -> None:
    checks.append(Check(name, "PASS" if passed else "FAIL", details))


def add_warning(checks: list[Check], name: str, details: str) -> None:
    checks.append(Check(name, "WARN", details))


def read_dicom(path: Path, *, pixels: bool = False) -> Dataset:
    try:
        return pydicom.dcmread(path, stop_before_pixels=not pixels)
    except Exception as exc:  # pydicom raises several format/value exceptions
        raise RuntimeError(f"Cannot read DICOM {path}: {exc}") from exc


def dicom_files(directory: Path) -> list[Path]:
    return sorted(path for path in directory.glob("*.dcm") if path.is_file())


def orientation(ds: Dataset) -> tuple[np.ndarray, np.ndarray, np.ndarray] | None:
    iop = as_float_array(getattr(ds, "ImageOrientationPatient", None), 6)
    if iop is None:
        return None
    row_direction = iop[:3]
    column_direction = iop[3:]
    normal = np.cross(row_direction, column_direction)
    if np.linalg.norm(normal) == 0:
        return None
    return row_direction, column_direction, normal / np.linalg.norm(normal)


def ct_slice_projection(ds: Dataset) -> float | None:
    axes = orientation(ds)
    position = as_float_array(getattr(ds, "ImagePositionPatient", None), 3)
    if axes is None or position is None:
        return None
    return float(np.dot(position, axes[2]))


def all_close(values: Sequence[np.ndarray], atol: float = 1e-6) -> bool:
    return bool(values) and all(np.allclose(values[0], item, atol=atol) for item in values[1:])


def referenced_structure_uids(plan: Dataset) -> set[str]:
    return {
        as_text(getattr(item, "ReferencedSOPInstanceUID", None))
        for item in getattr(plan, "ReferencedStructureSetSequence", [])
    }


def referenced_plan_uids(dose: Dataset) -> set[str]:
    return {
        as_text(getattr(item, "ReferencedSOPInstanceUID", None))
        for item in getattr(dose, "ReferencedRTPlanSequence", [])
    }


def structure_references(structure: Dataset) -> tuple[set[str], set[str], set[str]]:
    frame_uids: set[str] = set()
    series_uids: set[str] = set()
    image_uids: set[str] = set()
    for frame in getattr(structure, "ReferencedFrameOfReferenceSequence", []):
        frame_uids.add(as_text(getattr(frame, "FrameOfReferenceUID", None)))
        for study in getattr(frame, "RTReferencedStudySequence", []):
            for series in getattr(study, "RTReferencedSeriesSequence", []):
                series_uids.add(as_text(getattr(series, "SeriesInstanceUID", None)))
                for image in getattr(series, "ContourImageSequence", []):
                    image_uids.add(as_text(getattr(image, "ReferencedSOPInstanceUID", None)))
    return frame_uids, series_uids, image_uids


def contour_references(structure: Dataset) -> set[str]:
    refs: set[str] = set()
    for roi in getattr(structure, "ROIContourSequence", []):
        for contour in getattr(roi, "ContourSequence", []):
            for image in getattr(contour, "ContourImageSequence", []):
                refs.add(as_text(getattr(image, "ReferencedSOPInstanceUID", None)))
    return refs


def plan_geometry(plan: Dataset) -> tuple[list[np.ndarray], list[dict[str, object]]]:
    isocenters: list[np.ndarray] = []
    beams: list[dict[str, object]] = []
    for beam in getattr(plan, "IonBeamSequence", []):
        cps = list(getattr(beam, "IonControlPointSequence", []))
        cp0 = cps[0] if cps else Dataset()
        iso = as_float_array(getattr(cp0, "IsocenterPosition", None), 3)
        if iso is not None:
            isocenters.append(iso)
        beams.append(
            {
                "number": getattr(beam, "BeamNumber", None),
                "name": getattr(beam, "BeamName", None),
                "control_points": len(cps),
                "gantry_deg": getattr(cp0, "GantryAngle", None),
                "support_deg": getattr(cp0, "PatientSupportAngle", None),
                "pitch_deg": getattr(cp0, "TableTopPitchAngle", None),
                "roll_deg": getattr(cp0, "TableTopRollAngle", None),
                "isocenter_mm": None if iso is None else iso.tolist(),
            }
        )
    return isocenters, beams


def dose_grid_signature(ds: Dataset) -> tuple[object, ...] | None:
    iop = as_float_array(getattr(ds, "ImageOrientationPatient", None), 6)
    ipp = as_float_array(getattr(ds, "ImagePositionPatient", None), 3)
    spacing = as_float_array(getattr(ds, "PixelSpacing", None), 2)
    offsets = as_float_array(getattr(ds, "GridFrameOffsetVector", None))
    if iop is None or ipp is None or spacing is None or offsets is None:
        return None
    return (
        int(getattr(ds, "NumberOfFrames", 0)),
        int(getattr(ds, "Rows", 0)),
        int(getattr(ds, "Columns", 0)),
        tuple(np.round(spacing, 8)),
        tuple(np.round(ipp, 8)),
        tuple(np.round(iop, 8)),
        tuple(np.round(offsets, 8)),
    )


def grid_basis(ds: Dataset, slice_spacing: float) -> tuple[np.ndarray, np.ndarray] | None:
    axes = orientation(ds)
    origin = as_float_array(getattr(ds, "ImagePositionPatient", None), 3)
    spacing = as_float_array(getattr(ds, "PixelSpacing", None), 2)
    if axes is None or origin is None or spacing is None:
        return None
    row_direction, column_direction, normal = axes
    basis = np.column_stack(
        [
            row_direction * spacing[1],
            column_direction * spacing[0],
            normal * slice_spacing,
        ]
    )
    return origin, basis


def point_index(point: np.ndarray, origin: np.ndarray, basis: np.ndarray) -> np.ndarray:
    return np.linalg.solve(basis, point - origin)


def inside_index(index: np.ndarray, shape_xyz: Sequence[int], tolerance: float = 1e-6) -> bool:
    upper = np.asarray(shape_xyz, dtype=float) - 0.5 + tolerance
    return bool(np.all(index >= -0.5 - tolerance) and np.all(index <= upper))


def iso_roi_points(structure: Dataset) -> list[np.ndarray]:
    names = {
        int(item.ROINumber): as_text(getattr(item, "ROIName", None), "")
        for item in getattr(structure, "StructureSetROISequence", [])
    }
    points: list[np.ndarray] = []
    for roi in getattr(structure, "ROIContourSequence", []):
        number = int(getattr(roi, "ReferencedROINumber", -1))
        if "iso" not in names.get(number, "").lower():
            continue
        for contour in getattr(roi, "ContourSequence", []):
            data = as_float_array(getattr(contour, "ContourData", None))
            if data is not None and data.size % 3 == 0:
                points.extend(data.reshape(-1, 3))
    return points


def named_roi_bounds(structure: Dataset, roi_name: str) -> tuple[np.ndarray, np.ndarray] | None:
    names = {
        int(item.ROINumber): as_text(getattr(item, "ROIName", None), "")
        for item in getattr(structure, "StructureSetROISequence", [])
    }
    points: list[np.ndarray] = []
    for roi in getattr(structure, "ROIContourSequence", []):
        number = int(getattr(roi, "ReferencedROINumber", -1))
        if names.get(number, "").casefold() != roi_name.casefold():
            continue
        for contour in getattr(roi, "ContourSequence", []):
            data = as_float_array(getattr(contour, "ContourData", None))
            if data is not None and data.size % 3 == 0:
                points.extend(data.reshape(-1, 3))
    if not points:
        return None
    array = np.asarray(points)
    return array.min(axis=0), array.max(axis=0)


def physical_bounds(ds: Dataset, offsets: np.ndarray | None = None) -> tuple[np.ndarray, np.ndarray] | None:
    axes = orientation(ds)
    origin = as_float_array(getattr(ds, "ImagePositionPatient", None), 3)
    spacing = as_float_array(getattr(ds, "PixelSpacing", None), 2)
    if axes is None or origin is None or spacing is None:
        return None
    row_direction, column_direction, normal = axes
    columns = int(getattr(ds, "Columns", 0))
    rows = int(getattr(ds, "Rows", 0))
    if offsets is None:
        offsets = np.asarray([0.0])
    normal_half_width = 0.0
    if offsets.size > 1:
        offset_steps = np.diff(np.sort(offsets))
        if np.all(offset_steps > 0):
            normal_half_width = float(np.median(offset_steps)) / 2.0
    corners = []
    for column in (-0.5, columns - 0.5):
        for row in (-0.5, rows - 0.5):
            for offset in (
                float(offsets.min()) - normal_half_width,
                float(offsets.max()) + normal_half_width,
            ):
                corners.append(
                    origin
                    + column * spacing[1] * row_direction
                    + row * spacing[0] * column_direction
                    + offset * normal
                )
    corner_array = np.asarray(corners)
    return corner_array.min(axis=0), corner_array.max(axis=0)


def write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_ct_volume(ct_rows: list[tuple[Path, Dataset, float]]) -> np.ndarray:
    LOGGER.info("Loading %d CT pixel arrays for pixel validation", len(ct_rows))
    volume_slices: list[np.ndarray] = []
    for ct_path, header, _ in ct_rows:
        image = read_dicom(ct_path, pixels=True)
        slope = float(getattr(header, "RescaleSlope", 1.0))
        intercept = float(getattr(header, "RescaleIntercept", 0.0))
        volume_slices.append(image.pixel_array.astype(np.float32) * slope + intercept)
    return np.stack(volume_slices)


def create_geometry_plot(
    path: Path,
    ct_rows: list[tuple[Path, Dataset, float]],
    volume: np.ndarray,
    isocenter: np.ndarray,
    dose: Dataset,
    external_bounds: tuple[np.ndarray, np.ndarray] | None,
    pixel_data_has_contrast: bool,
) -> None:
    first = ct_rows[0][1]
    slice_spacing = float(np.median(np.diff([row[2] for row in ct_rows])))
    ct_grid = grid_basis(first, slice_spacing)
    if ct_grid is None:
        raise RuntimeError("Cannot construct CT coordinate basis for plotting")
    ct_origin, ct_basis = ct_grid
    iso_index = point_index(isocenter, ct_origin, ct_basis)
    col = int(np.clip(np.rint(iso_index[0]), 0, volume.shape[2] - 1))
    row = int(np.clip(np.rint(iso_index[1]), 0, volume.shape[1] - 1))
    frame = int(np.clip(np.rint(iso_index[2]), 0, volume.shape[0] - 1))

    ct_offsets = np.asarray([item[2] - ct_rows[0][2] for item in ct_rows])
    ct_bounds = physical_bounds(first, ct_offsets)
    dose_offsets = as_float_array(getattr(dose, "GridFrameOffsetVector", None))
    dose_bounds = physical_bounds(dose, dose_offsets)
    if ct_bounds is None or dose_bounds is None:
        raise RuntimeError("Cannot determine CT or RTDOSE physical bounds for plotting")
    ct_min, ct_max = ct_bounds
    dose_min, dose_max = dose_bounds

    fig, axes = plt.subplots(1, 3, figsize=(16, 5.5), constrained_layout=True)
    panels = [
        (
            axes[0],
            volume[frame],
            [ct_min[0], ct_max[0], ct_min[1], ct_max[1]],
            (isocenter[0], isocenter[1]),
            (dose_min[0], dose_max[0], dose_min[1], dose_max[1]),
            None if external_bounds is None else (external_bounds[0][0], external_bounds[1][0], external_bounds[0][1], external_bounds[1][1]),
            f"Axial near isocenter (z={ct_rows[frame][1].ImagePositionPatient[2]:.1f} mm)",
            "Patient x (mm)",
            "Patient y (mm)",
        ),
        (
            axes[1],
            volume[:, :, col],
            [ct_min[1], ct_max[1], ct_min[2], ct_max[2]],
            (isocenter[1], isocenter[2]),
            (dose_min[1], dose_max[1], dose_min[2], dose_max[2]),
            None if external_bounds is None else (external_bounds[0][1], external_bounds[1][1], external_bounds[0][2], external_bounds[1][2]),
            f"Sagittal near isocenter (x={ct_origin[0] + col * ct_basis[0, 0]:.1f} mm)",
            "Patient y (mm)",
            "Patient z (mm)",
        ),
        (
            axes[2],
            volume[:, row, :],
            [ct_min[0], ct_max[0], ct_min[2], ct_max[2]],
            (isocenter[0], isocenter[2]),
            (dose_min[0], dose_max[0], dose_min[2], dose_max[2]),
            None if external_bounds is None else (external_bounds[0][0], external_bounds[1][0], external_bounds[0][2], external_bounds[1][2]),
            f"Coronal near isocenter (y={ct_origin[1] + row * ct_basis[1, 1]:.1f} mm)",
            "Patient x (mm)",
            "Patient z (mm)",
        ),
    ]
    for axis, image, extent, iso_xy, dose_rect, external_rect, title, xlabel, ylabel in panels:
        if pixel_data_has_contrast:
            window_low, window_high = np.percentile(volume, [1.0, 99.0])
            if np.isclose(window_low, window_high):
                window_low, window_high = float(volume.min()), float(volume.max()) + 1.0
        else:
            window_low, window_high = -0.5, 0.5
        axis.imshow(image, cmap="gray", origin="lower", extent=extent, vmin=window_low, vmax=window_high)
        left, right, bottom, top = dose_rect
        axis.add_patch(
            plt.Rectangle(
                (left, bottom),
                right - left,
                top - bottom,
                fill=False,
                edgecolor="#00d4ff",
                linewidth=1.8,
                label="RPPD grid extent",
            )
        )
        if external_rect is not None:
            left, right, bottom, top = external_rect
            axis.add_patch(
                plt.Rectangle(
                    (left, bottom),
                    right - left,
                    top - bottom,
                    fill=False,
                    edgecolor="#ffcc00",
                    linestyle="--",
                    linewidth=1.8,
                    label="RTSTRUCT External bounds",
                )
            )
        axis.plot(*iso_xy, marker="+", markersize=14, markeredgewidth=2.2, color="#ff3b30", label="Plan isocenter")
        axis.set(title=title, xlabel=xlabel, ylabel=ylabel)
        axis.set_aspect("equal")
        axis.legend(loc="upper right", fontsize=8)
        if not pixel_data_has_contrast:
            axis.text(
                0.5,
                0.08,
                "Uniform artificial CT: 0 HU water phantom",
                color="white",
                fontsize=9,
                fontweight="bold",
                ha="center",
                va="center",
                transform=axis.transAxes,
                bbox={"facecolor": "#805500", "alpha": 0.9, "edgecolor": "none", "pad": 4},
            )
    title = "DICOM geometry overview — HFS patient coordinates"
    if not pixel_data_has_contrast:
        title += " (uniform QA phantom)"
    fig.suptitle(title, fontsize=14)
    fig.savefig(path, dpi=180)
    plt.close(fig)


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    root = args.root.expanduser().resolve()
    dicom_dir = (args.dicom_dir or root / "dicom").expanduser().resolve()
    output_dir = (args.output_dir or root / "plan_parsed").expanduser().resolve()

    if not dicom_dir.is_dir():
        LOGGER.error("DICOM directory does not exist: %s", dicom_dir)
        return 2
    try:
        output_dir.relative_to(dicom_dir)
    except ValueError:
        pass
    else:
        LOGGER.error("Output directory must not be inside read-only DICOM directory: %s", output_dir)
        return 2
    output_dir.mkdir(parents=True, exist_ok=True)

    required_dirs = {name: dicom_dir / name for name in ("CT", "RTPLAN", "RTDOSE", "RTSTRUCT")}
    missing = [str(path) for path in required_dirs.values() if not path.is_dir()]
    if missing:
        LOGGER.error("Missing required DICOM directories: %s", ", ".join(missing))
        return 2

    checks: list[Check] = []
    inventory: list[dict[str, object]] = []
    groups: dict[str, list[tuple[Path, Dataset]]] = {}
    for group, directory in required_dirs.items():
        items: list[tuple[Path, Dataset]] = []
        for path in dicom_files(directory):
            try:
                ds = read_dicom(path)
            except RuntimeError as exc:
                add_warning(checks, f"READ_{group}_{path.name}", str(exc))
                continue
            items.append((path, ds))
            inventory.append(
                {
                    "RelativePath": path.relative_to(root).as_posix(),
                    "Modality": as_text(getattr(ds, "Modality", None)),
                    "SOPClassUID": as_text(getattr(ds, "SOPClassUID", None)),
                    "SOPInstanceUID": as_text(getattr(ds, "SOPInstanceUID", None)),
                    "SeriesInstanceUID": as_text(getattr(ds, "SeriesInstanceUID", None)),
                    "FrameOfReferenceUID": as_text(getattr(ds, "FrameOfReferenceUID", None)),
                    "PatientID": as_text(getattr(ds, "PatientID", None)),
                    "PatientName": as_text(getattr(ds, "PatientName", None)),
                    "StudyInstanceUID": as_text(getattr(ds, "StudyInstanceUID", None)),
                    "PatientPosition": as_text(getattr(ds, "PatientPosition", None), ""),
                    "DoseUnits": as_text(getattr(ds, "DoseUnits", None), ""),
                    "DoseType": as_text(getattr(ds, "DoseType", None), ""),
                    "DoseSummationType": as_text(getattr(ds, "DoseSummationType", None), ""),
                }
            )
        groups[group] = items
        add_check(checks, f"{group}_FILES_READABLE", bool(items), f"read {len(items)} DICOM file(s)")

    if not all(groups.values()):
        LOGGER.error("At least one required DICOM group is empty or unreadable; see checks output")
        write_csv(output_dir / "geometry_checks.csv", ["Check", "Status", "Details"], [check.__dict__ for check in checks])
        return 2

    ct_items = groups["CT"]
    plans = groups["RTPLAN"]
    doses = groups["RTDOSE"]
    structures = groups["RTSTRUCT"]
    ct_headers = [item[1] for item in ct_items]
    plan = plans[0][1]
    structure = structures[0][1]

    add_check(
        checks,
        "CT_COUNT",
        len(ct_items) >= 2,
        f"found {len(ct_items)} slice(s); at least 2 are required for a 3D volume",
    )
    add_check(checks, "CT_MODALITY", unique_text(ct_headers, "Modality") == ["CT"], f"values={unique_text(ct_headers, 'Modality')}")
    ct_series = unique_text(ct_headers, "SeriesInstanceUID")
    ct_frames = unique_text(ct_headers, "FrameOfReferenceUID")
    patient_positions = unique_text(ct_headers, "PatientPosition")
    add_check(checks, "CT_SINGLE_SERIES", len(ct_series) == 1 and "MISSING" not in ct_series, f"SeriesInstanceUID={ct_series}")
    add_check(checks, "CT_SINGLE_FRAME", len(ct_frames) == 1 and "MISSING" not in ct_frames, f"FrameOfReferenceUID={ct_frames}")
    add_check(
        checks,
        "CT_PATIENT_POSITION_PRESENT",
        len(patient_positions) == 1 and patient_positions[0] != "MISSING",
        f"PatientPosition={patient_positions}; current TOPAS geometry implementation supports only HFS and requires a separate compatibility gate",
    )

    ct_orientations = [as_float_array(getattr(ds, "ImageOrientationPatient", None), 6) for ds in ct_headers]
    valid_ct_orientations = [item for item in ct_orientations if item is not None]
    expected_axial = np.asarray([1, 0, 0, 0, 1, 0], dtype=float)
    add_check(
        checks,
        "CT_ORIENTATION_CONSISTENT_AXIAL",
        len(valid_ct_orientations) == len(ct_headers)
        and all_close(valid_ct_orientations)
        and np.allclose(valid_ct_orientations[0], expected_axial),
        f"ImageOrientationPatient={valid_ct_orientations[0].tolist() if valid_ct_orientations else 'MISSING'}",
    )

    projected_items: list[tuple[Path, Dataset, float]] = []
    for path, ds in ct_items:
        projection = ct_slice_projection(ds)
        if projection is not None:
            projected_items.append((path, ds, projection))
    projected_items.sort(key=lambda item: item[2])
    positions = np.asarray([item[2] for item in projected_items], dtype=float)
    differences = np.diff(positions)
    regular_ct = (
        len(projected_items) == len(ct_items)
        and differences.size > 0
        and np.all(differences > 0)
        and np.allclose(differences, np.median(differences), atol=1e-6)
    )
    spacing_text = "MISSING" if not differences.size else f"{np.median(differences):.6g} mm"
    add_check(
        checks,
        "CT_SLICE_ORDER_AND_SPACING",
        regular_ct,
        f"projection range={positions[0]:.6g}..{positions[-1]:.6g} mm; spacing={spacing_text}; gaps=0" if positions.size else "missing positions",
    )
    pixel_spacings = [as_float_array(getattr(ds, "PixelSpacing", None), 2) for ds in ct_headers]
    valid_pixel_spacings = [item for item in pixel_spacings if item is not None]
    add_check(
        checks,
        "CT_PIXEL_SPACING",
        len(valid_pixel_spacings) == len(ct_headers)
        and all_close(valid_pixel_spacings)
        and np.all(valid_pixel_spacings[0] > 0),
        f"PixelSpacing={valid_pixel_spacings[0].tolist() if valid_pixel_spacings else 'MISSING'} mm",
    )
    ct_shapes = sorted({(int(ds.Rows), int(ds.Columns)) for ds in ct_headers if hasattr(ds, "Rows") and hasattr(ds, "Columns")})
    add_check(checks, "CT_MATRIX_CONSISTENT", len(ct_shapes) == 1, f"Rows x Columns={ct_shapes}")
    ct_sop_uids = {as_text(getattr(ds, "SOPInstanceUID", None)) for ds in ct_headers}
    add_check(checks, "CT_SOP_UIDS_UNIQUE", len(ct_sop_uids) == len(ct_items), f"unique={len(ct_sop_uids)}, files={len(ct_items)}")

    ct_volume: np.ndarray | None = None
    pixel_data_valid = False
    pixel_data_has_contrast = False
    if not args.no_plot:
        try:
            ct_volume = load_ct_volume(projected_items)
            pixel_min = float(np.min(ct_volume))
            pixel_max = float(np.max(ct_volume))
            nonzero_voxels = int(np.count_nonzero(ct_volume))
            nonconstant_slices = int(sum(not np.allclose(image, image.flat[0]) for image in ct_volume))
            pixel_data_valid = bool(np.all(np.isfinite(ct_volume))) and ct_volume.shape == (
                len(projected_items),
                int(projected_items[0][1].Rows),
                int(projected_items[0][1].Columns),
            )
            pixel_data_has_contrast = pixel_max > pixel_min and nonconstant_slices > 0
            add_check(
                checks,
                "CT_PIXEL_DATA_READABLE",
                pixel_data_valid,
                f"rescaled range={pixel_min:.6g}..{pixel_max:.6g}; nonzero voxels={nonzero_voxels}; nonconstant slices={nonconstant_slices}/{len(ct_volume)}",
            )
            artificial_description = " ".join(
                as_text(getattr(projected_items[0][1], keyword, None), "")
                for keyword in ("StudyDescription", "SeriesDescription", "ProtocolName", "ManufacturerModelName")
            )
            artificial_phantom = "phantom" in artificial_description.casefold() or "artificial" in artificial_description.casefold()
            if not pixel_data_has_contrast and artificial_phantom:
                add_warning(
                    checks,
                    "CT_UNIFORM_ARTIFICIAL_PHANTOM",
                    f"all CT voxels are {pixel_min:.6g} HU; metadata='{artificial_description.strip()}'. Use RTSTRUCT External to separate the water phantom from surrounding air in TOPAS",
                )
            elif not pixel_data_has_contrast:
                add_check(
                    checks,
                    "CT_PIXEL_DATA_HAS_CONTRAST",
                    False,
                    "CT is spatially constant and metadata does not identify an artificial/phantom CT",
                )
        except Exception as exc:
            add_check(checks, "CT_PIXEL_DATA_READABLE", False, f"pixel arrays could not be loaded: {exc}")
    else:
        add_warning(checks, "CT_PIXEL_DATA_READABLE", "not evaluated because --no-plot was selected")

    add_check(checks, "RTPLAN_SINGLE_FILE", len(plans) == 1, f"found {len(plans)} RTPLAN file(s); using {plans[0][0].name}")
    plan_uid = as_text(getattr(plan, "SOPInstanceUID", None))
    plan_frame = as_text(getattr(plan, "FrameOfReferenceUID", None))
    add_check(
        checks,
        "RTPLAN_ION_PLAN_STORAGE",
        as_text(getattr(plan, "SOPClassUID", None)) == ION_PLAN_STORAGE_UID and as_text(getattr(plan, "Modality", None)) == "RTPLAN",
        f"SOPClassUID={as_text(getattr(plan, 'SOPClassUID', None))}",
    )
    isocenters, beams = plan_geometry(plan)
    unique_isocenters: list[np.ndarray] = []
    for point in isocenters:
        if not any(np.allclose(point, existing) for existing in unique_isocenters):
            unique_isocenters.append(point)
    add_check(checks, "RTPLAN_ISOCENTER_AVAILABLE", len(unique_isocenters) == 1, f"isocenter(s)={[x.tolist() for x in unique_isocenters]}")
    add_check(checks, "RTPLAN_BEAM_AVAILABLE", bool(beams), f"beam geometry={beams}")

    structure_uid = as_text(getattr(structure, "SOPInstanceUID", None))
    structure_frame_refs, structure_series_refs, structure_image_refs = structure_references(structure)
    contour_image_uids = contour_references(structure)
    add_check(checks, "RTSTRUCT_SINGLE_FILE", len(structures) == 1, f"found {len(structures)} RTSTRUCT file(s); using {structures[0][0].name}")
    add_check(
        checks,
        "RTPLAN_REFERENCES_RTSTRUCT",
        structure_uid in referenced_structure_uids(plan),
        f"plan refs={sorted(referenced_structure_uids(plan))}; RTSTRUCT SOPInstanceUID={structure_uid}",
    )
    add_check(
        checks,
        "RTSTRUCT_REFERENCES_CT_FRAME",
        set(ct_frames) == structure_frame_refs,
        f"CT={ct_frames}; RTSTRUCT refs={sorted(structure_frame_refs)}",
    )
    add_check(
        checks,
        "RTSTRUCT_REFERENCES_CT_SERIES",
        set(ct_series) == structure_series_refs,
        f"CT={ct_series}; RTSTRUCT refs={sorted(structure_series_refs)}",
    )
    add_check(
        checks,
        "RTSTRUCT_REFERENCES_ALL_CT_IMAGES",
        ct_sop_uids == structure_image_refs,
        f"CT SOPs={len(ct_sop_uids)}; RTSTRUCT image refs={len(structure_image_refs)}; missing={len(ct_sop_uids - structure_image_refs)}; extra={len(structure_image_refs - ct_sop_uids)}",
    )
    add_check(
        checks,
        "RTSTRUCT_CONTOUR_REFS_IN_CT",
        contour_image_uids.issubset(ct_sop_uids),
        f"unique contour refs={len(contour_image_uids)}; refs outside CT={len(contour_image_uids - ct_sop_uids)}",
    )

    dose_headers = [item[1] for item in doses]
    case_headers = [*ct_headers, plan, structure, *dose_headers]
    patient_ids = unique_text(case_headers, "PatientID")
    patient_names = unique_text(case_headers, "PatientName")
    study_uids = unique_text(case_headers, "StudyInstanceUID")
    add_check(
        checks,
        "COMMON_PATIENT_ID",
        len(patient_ids) == 1 and "MISSING" not in patient_ids,
        f"PatientID values={patient_ids}",
    )
    add_check(
        checks,
        "COMMON_PATIENT_NAME",
        len(patient_names) == 1 and "MISSING" not in patient_names,
        f"PatientName values={patient_names}",
    )
    add_check(
        checks,
        "COMMON_STUDY_INSTANCE_UID",
        len(study_uids) == 1 and "MISSING" not in study_uids,
        f"StudyInstanceUID values={study_uids}",
    )
    rppd_candidates = [
        item
        for item in doses
        if as_text(getattr(item[1], "DoseType", None)) == "PHYSICAL"
        and as_text(getattr(item[1], "DoseSummationType", None)) == "PLAN"
    ]
    add_check(checks, "RPPD_IDENTIFIED", len(rppd_candidates) == 1, f"candidate file(s)={[item[0].name for item in rppd_candidates]}")
    rppd = rppd_candidates[0][1] if rppd_candidates else dose_headers[0]
    dose_refs_ok = all(plan_uid in referenced_plan_uids(ds) for ds in dose_headers)
    add_check(checks, "RTDOSE_REFERENCES_RTPLAN", dose_refs_ok, f"RTPLAN SOPInstanceUID={plan_uid}; checked {len(dose_headers)} dose file(s)")
    dose_frames = unique_text(dose_headers, "FrameOfReferenceUID")
    all_frames = set(ct_frames + dose_frames + [plan_frame]) | structure_frame_refs
    add_check(checks, "COMMON_FRAME_OF_REFERENCE", len(all_frames) == 1 and "MISSING" not in all_frames, f"FrameOfReferenceUIDs={sorted(all_frames)}")
    signatures = [dose_grid_signature(ds) for ds in dose_headers]
    add_check(
        checks,
        "RTDOSE_GRIDS_IDENTICAL",
        all(item is not None for item in signatures) and len(set(signatures)) == 1,
        f"checked {len(signatures)} grid signature(s)",
    )
    dose_offsets = as_float_array(getattr(rppd, "GridFrameOffsetVector", None))
    dose_offset_steps = np.diff(dose_offsets) if dose_offsets is not None else np.asarray([])
    regular_dose = dose_offset_steps.size > 0 and np.allclose(dose_offset_steps, dose_offset_steps[0])
    add_check(
        checks,
        "RPPD_GRID_REGULAR",
        regular_dose
        and int(getattr(rppd, "NumberOfFrames", 0)) >= 2
        and int(getattr(rppd, "Rows", 0)) > 0
        and int(getattr(rppd, "Columns", 0)) > 0
        and np.all(as_float_array(getattr(rppd, "PixelSpacing", None), 2) > 0)
        and dose_offset_steps[0] > 0,
        f"frames x rows x columns={getattr(rppd, 'NumberOfFrames', 'MISSING')} x {getattr(rppd, 'Rows', 'MISSING')} x {getattr(rppd, 'Columns', 'MISSING')}; PixelSpacing={getattr(rppd, 'PixelSpacing', 'MISSING')}; frame step={dose_offset_steps[0] if dose_offset_steps.size else 'MISSING'} mm",
    )
    rppd_iop = as_float_array(getattr(rppd, "ImageOrientationPatient", None), 6)
    add_check(
        checks,
        "CT_RPPD_ORIENTATION_MATCH",
        bool(valid_ct_orientations) and rppd_iop is not None and np.allclose(valid_ct_orientations[0], rppd_iop),
        f"CT={valid_ct_orientations[0].tolist() if valid_ct_orientations else 'MISSING'}; RPPD={rppd_iop.tolist() if rppd_iop is not None else 'MISSING'}",
    )

    ct_iso_index: np.ndarray | None = None
    dose_iso_index: np.ndarray | None = None
    isocenter = unique_isocenters[0] if len(unique_isocenters) == 1 else None
    if isocenter is not None and regular_ct and valid_ct_orientations:
        ct_basis_data = grid_basis(projected_items[0][1], float(np.median(differences)))
        if ct_basis_data is not None:
            ct_iso_index = point_index(isocenter, *ct_basis_data)
            add_check(
                checks,
                "ISOCENTER_INSIDE_CT",
                inside_index(ct_iso_index, [int(projected_items[0][1].Columns), int(projected_items[0][1].Rows), len(projected_items)]),
                f"isocenter patient mm={isocenter.tolist()}; CT continuous index [column,row,slice]={np.round(ct_iso_index, 4).tolist()}",
            )
    else:
        add_warning(checks, "ISOCENTER_INSIDE_CT", "not evaluated because CT basis or a unique isocenter is unavailable")

    if isocenter is not None and regular_dose:
        dose_basis_data = grid_basis(rppd, float(dose_offset_steps[0]))
        if dose_basis_data is not None:
            dose_iso_index = point_index(isocenter, *dose_basis_data)
            add_check(
                checks,
                "ISOCENTER_INSIDE_RPPD",
                inside_index(dose_iso_index, [int(rppd.Columns), int(rppd.Rows), int(rppd.NumberOfFrames)]),
                f"RPPD continuous index [column,row,frame]={np.round(dose_iso_index, 4).tolist()}",
            )
    else:
        add_warning(checks, "ISOCENTER_INSIDE_RPPD", "not evaluated because RPPD basis or a unique isocenter is unavailable")

    points = iso_roi_points(structure)
    external_bounds = named_roi_bounds(structure, "External")
    if isocenter is not None and points:
        distances = [float(np.linalg.norm(point - isocenter)) for point in points]
        add_check(
            checks,
            "RTSTRUCT_ISO_MATCHES_PLAN",
            min(distances) < 1e-3,
            f"closest RTSTRUCT ROI named iso is {min(distances):.6g} mm from plan isocenter; point(s)={[x.tolist() for x in points]}",
        )
    else:
        add_warning(checks, "RTSTRUCT_ISO_MATCHES_PLAN", "ROI named iso or unique plan isocenter unavailable")

    if regular_ct and regular_dose:
        ct_bounds = physical_bounds(projected_items[0][1], np.asarray([0.0, positions[-1] - positions[0]]))
        rppd_bounds = physical_bounds(rppd, dose_offsets)
        if ct_bounds is not None and rppd_bounds is not None:
            ct_min, ct_max = ct_bounds
            dose_min, dose_max = rppd_bounds
            contained = bool(np.all(dose_min >= ct_min - 1e-6) and np.all(dose_max <= ct_max + 1e-6))
            add_check(
                checks,
                "RPPD_GRID_INSIDE_CT_FOV",
                contained,
                f"CT bounds mm={np.round(ct_min, 3).tolist()}..{np.round(ct_max, 3).tolist()}; RPPD bounds mm={np.round(dose_min, 3).tolist()}..{np.round(dose_max, 3).tolist()}",
            )

    plot_path = output_dir / "geometry_overview.png"
    if not args.no_plot and ct_volume is not None and isocenter is not None and regular_ct and rppd is not None:
        try:
            create_geometry_plot(
                plot_path,
                projected_items,
                ct_volume,
                isocenter,
                rppd,
                external_bounds,
                pixel_data_has_contrast,
            )
        except Exception as exc:
            LOGGER.warning("Geometry overview could not be generated: %s", exc)
            add_warning(checks, "GEOMETRY_OVERVIEW", str(exc))
    elif not args.no_plot:
        LOGGER.warning("Geometry overview skipped because required geometry or pixels are unavailable")

    failed = [check for check in checks if check.status == "FAIL"]
    warned = [check for check in checks if check.status == "WARN"]
    summary_lines = [
        "TPS-TOPAS DICOM geometry consistency summary",
        "=" * 45,
        f"Project root: {root}",
        f"DICOM input (read-only): {dicom_dir}",
        f"CT slices: {len(ct_items)}",
        f"Patient position: {', '.join(patient_positions)}",
        f"CT SeriesInstanceUID: {', '.join(ct_series)}",
        f"Common FrameOfReferenceUID: {next(iter(all_frames)) if len(all_frames) == 1 else sorted(all_frames)}",
        f"RTPLAN: {plans[0][0].name}",
        f"RTSTRUCT: {structures[0][0].name}",
        f"RPPD: {rppd_candidates[0][0].name if rppd_candidates else 'not uniquely identified'}",
        f"Plan isocenter (patient mm): {isocenter.tolist() if isocenter is not None else 'unavailable'}",
        f"CT isocenter continuous index [column,row,slice]: {np.round(ct_iso_index, 4).tolist() if ct_iso_index is not None else 'unavailable'}",
        f"RPPD isocenter continuous index [column,row,frame]: {np.round(dose_iso_index, 4).tolist() if dose_iso_index is not None else 'unavailable'}",
        "",
        "Beam geometry (DICOM patient/IEC inputs; no TOPAS sign conversion applied):",
    ]
    summary_lines.extend(f"  {beam}" for beam in beams)
    summary_lines.extend(["", "Checks:"])
    summary_lines.extend(f"[{check.status}] {check.name}: {check.details}" for check in checks)
    summary_lines.extend(
        [
            "",
            f"Result: {'FAIL' if failed else 'PASS'} ({len(checks) - len(failed) - len(warned)} passed, {len(warned)} warnings, {len(failed)} failed)",
            "Coordinate note: these checks remain in DICOM patient coordinates. No IEC-to-TOPAS axis/sign mapping is inferred here.",
        ]
    )
    (output_dir / "geometry_summary.txt").write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    write_csv(
        output_dir / "dicom_inventory.csv",
        [
            "RelativePath",
            "Modality",
            "SOPClassUID",
            "SOPInstanceUID",
            "SeriesInstanceUID",
            "FrameOfReferenceUID",
            "PatientID",
            "PatientName",
            "StudyInstanceUID",
            "PatientPosition",
            "DoseUnits",
            "DoseType",
            "DoseSummationType",
        ],
        inventory,
    )
    write_csv(
        output_dir / "geometry_checks.csv",
        ["Check", "Status", "Details"],
        ({"Check": check.name, "Status": check.status, "Details": check.details} for check in checks),
    )

    LOGGER.info("Wrote %s", output_dir / "dicom_inventory.csv")
    LOGGER.info("Wrote %s", output_dir / "geometry_checks.csv")
    LOGGER.info("Wrote %s", output_dir / "geometry_summary.txt")
    if plot_path.exists():
        LOGGER.info("Wrote %s", plot_path)
    LOGGER.info("Result: %d PASS, %d WARN, %d FAIL", len(checks) - len(failed) - len(warned), len(warned), len(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
