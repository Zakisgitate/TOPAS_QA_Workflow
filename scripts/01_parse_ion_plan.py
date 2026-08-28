#!/usr/bin/env python3
"""Parse a DICOM RT Ion Plan into energy-layer and scanning-spot tables.

The source ``dicom/`` tree is treated as read-only. Derived tables, summaries,
and figures are written to ``plan_parsed/`` by default.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import tempfile
import warnings
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


LOGGER = logging.getLogger("parse_ion_plan")
ION_PLAN_STORAGE_UID = "1.2.840.10008.5.1.4.1.1.481.8"
EXPECTED_OUTPUTS = (
    "plan_summary.txt",
    "spots.csv",
    "energy_layers.csv",
    "rtdose_summary.csv",
    "spot_map.png",
    "energy_layers.png",
    "spot_count_by_energy.png",
    "weight_by_energy.png",
)


@dataclass
class ParseWarning:
    scope: str
    message: str


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
        "--rtplan",
        type=Path,
        help="RT Ion Plan path (default: uniquely discover under ROOT/dicom/RTPLAN)",
    )
    parser.add_argument(
        "--rtdose-dir",
        type=Path,
        help="RTDOSE directory for the dose summary (default: ROOT/dicom/RTDOSE)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Derived output directory (default: ROOT/plan_parsed)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow replacement of existing derived outputs (never modifies DICOM inputs)",
    )
    return parser.parse_args()


def read_dicom(path: Path, *, pixels: bool = False) -> Dataset:
    try:
        # One private SH value in this plan exceeds DICOM's nominal 16-character
        # limit. It is unrelated to the standard ion-plan fields parsed here.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=r"The value length .* exceeds the maximum length of 16 allowed for VR SH\.",
                category=UserWarning,
            )
            return pydicom.dcmread(path, stop_before_pixels=not pixels)
    except Exception as exc:
        raise RuntimeError(f"Cannot read DICOM {path}: {exc}") from exc


def text(value: object, default: str = "MISSING") -> str:
    if value is None or value == "":
        return default
    return str(value)


def number(value: object, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def array(value: object) -> np.ndarray:
    if value is None:
        return np.asarray([], dtype=float)
    try:
        return np.atleast_1d(np.asarray(value, dtype=float))
    except (TypeError, ValueError):
        return np.asarray([], dtype=float)


def discover_ion_plan(directory: Path) -> Path:
    candidates: list[Path] = []
    unreadable: list[str] = []
    for path in sorted(directory.glob("*.dcm")):
        try:
            ds = read_dicom(path)
        except RuntimeError as exc:
            unreadable.append(str(exc))
            continue
        if text(getattr(ds, "SOPClassUID", None)) == ION_PLAN_STORAGE_UID:
            candidates.append(path)
    if len(candidates) != 1:
        detail = f"; unreadable={unreadable}" if unreadable else ""
        raise RuntimeError(f"Expected exactly one RT Ion Plan in {directory}, found {len(candidates)}{detail}")
    return candidates[0]


def ensure_output_policy(output_dir: Path, dicom_dir: Path, overwrite: bool) -> None:
    try:
        output_dir.relative_to(dicom_dir)
    except ValueError:
        pass
    else:
        raise RuntimeError(f"Output directory must not be inside the read-only DICOM tree: {output_dir}")
    existing = [output_dir / name for name in EXPECTED_OUTPUTS if (output_dir / name).exists()]
    if existing and not overwrite:
        names = ", ".join(path.name for path in existing)
        raise RuntimeError(f"Derived output(s) already exist: {names}. Inspect them or rerun with --overwrite")
    output_dir.mkdir(parents=True, exist_ok=True)


def write_csv(path: Path, fields: Sequence[str], rows: Iterable[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def inherited(cp: Dataset, previous: Dataset | None, keyword: str, default: object = None) -> object:
    value = getattr(cp, keyword, None)
    if value is not None:
        return value
    return getattr(previous, keyword, default) if previous is not None else default


def parse_range_modulators(beam: Dataset) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for item in getattr(beam, "RangeModulatorSequence", []):
        result.append(
            {
                "number": getattr(item, "RangeModulatorNumber", None),
                "id": text(getattr(item, "RangeModulatorID", None)),
                "type": text(getattr(item, "RangeModulatorType", None)),
                "description": text(getattr(item, "RangeModulatorDescription", None)),
            }
        )
    return result


def parse_plan(ds: Dataset) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[ParseWarning]]:
    layers: list[dict[str, object]] = []
    spots: list[dict[str, object]] = []
    beam_summaries: list[dict[str, object]] = []
    parse_warnings: list[ParseWarning] = []

    beams = list(getattr(ds, "IonBeamSequence", []))
    if not beams:
        raise RuntimeError("RT Ion Plan has no IonBeamSequence")

    for beam in beams:
        beam_number = int(getattr(beam, "BeamNumber", len(beam_summaries) + 1))
        beam_name = text(getattr(beam, "BeamName", None))
        dosimeter_unit = text(getattr(beam, "PrimaryDosimeterUnit", None))
        control_points = list(getattr(beam, "IonControlPointSequence", []))
        declared_cps = int(getattr(beam, "NumberOfControlPoints", len(control_points)))
        if declared_cps != len(control_points):
            parse_warnings.append(
                ParseWarning(f"Beam {beam_number}", f"declares {declared_cps} CPs but contains {len(control_points)}")
            )

        beam_layer_start = len(layers)
        beam_spot_start = len(spots)
        zero_cp_count = 0
        prior_cp: Dataset | None = None
        active_layer_index = 0
        for sequence_index, cp in enumerate(control_points):
            cp_index = int(getattr(cp, "ControlPointIndex", sequence_index))
            expected_spots = int(getattr(cp, "NumberOfScanSpotPositions", 0))
            positions = array(getattr(cp, "ScanSpotPositionMap", None))
            weights = array(getattr(cp, "ScanSpotMetersetWeights", None))

            if expected_spots < 0:
                raise RuntimeError(f"Beam {beam_number} CP {cp_index}: negative NumberOfScanSpotPositions")
            if positions.size != 2 * expected_spots:
                raise RuntimeError(
                    f"Beam {beam_number} CP {cp_index}: position map has {positions.size} values; expected {2 * expected_spots}"
                )
            if weights.size != expected_spots:
                raise RuntimeError(
                    f"Beam {beam_number} CP {cp_index}: meterset weights has {weights.size} values; expected {expected_spots}"
                )
            if not np.all(np.isfinite(weights)) or not np.all(np.isfinite(positions)):
                raise RuntimeError(f"Beam {beam_number} CP {cp_index}: non-finite spot position or weight")
            if np.any(weights < 0):
                raise RuntimeError(f"Beam {beam_number} CP {cp_index}: negative spot weights are not supported")

            # A delivered energy layer is identified by at least one non-zero
            # ScanSpotMetersetWeight. This explicitly excludes paired zero-weight CPs.
            if expected_spots == 0 or not np.any(weights != 0):
                zero_cp_count += 1
                prior_cp = cp
                continue

            active_layer_index += 1
            energy = number(inherited(cp, prior_cp, "NominalBeamEnergy"))
            if energy is None:
                raise RuntimeError(f"Beam {beam_number} CP {cp_index}: missing NominalBeamEnergy")
            energy_unit = text(inherited(cp, prior_cp, "NominalBeamEnergyUnit", "MEV/U"))
            if energy_unit.upper() not in {"MEV/U", "MEV/NUCLEON"}:
                parse_warnings.append(ParseWarning(f"Beam {beam_number} CP {cp_index}", f"energy unit is {energy_unit}"))

            spot_size = array(inherited(cp, prior_cp, "ScanningSpotSize"))
            if spot_size.size == 1:
                spot_size = np.repeat(spot_size, 2)
            if spot_size.size != 2:
                raise RuntimeError(f"Beam {beam_number} CP {cp_index}: ScanningSpotSize must contain X and Y")
            paintings = int(inherited(cp, prior_cp, "NumberOfPaintings", 1))
            if paintings <= 0:
                raise RuntimeError(f"Beam {beam_number} CP {cp_index}: NumberOfPaintings must be positive")

            xy = positions.reshape(expected_spots, 2)
            total_weight = float(np.sum(weights, dtype=np.float64))
            layer = {
                "BeamNumber": beam_number,
                "BeamName": beam_name,
                "LayerIndex": active_layer_index,
                "ControlPointIndex": cp_index,
                "Energy_MeVu": energy,
                "NumberOfSpots": expected_spots,
                "TotalMetersetWeight_MU": total_weight,
                "RelativeWeight": 0.0,
                "NumberOfPaintings": paintings,
                "FWHM_X_mm": float(spot_size[0]),
                "FWHM_Y_mm": float(spot_size[1]),
                "CumulativeMetersetWeightBeforeLayer_MU": number(getattr(cp, "CumulativeMetersetWeight", None)),
                "MinX_mm": float(np.min(xy[:, 0])),
                "MaxX_mm": float(np.max(xy[:, 0])),
                "MinY_mm": float(np.min(xy[:, 1])),
                "MaxY_mm": float(np.max(xy[:, 1])),
            }
            layers.append(layer)
            for spot_offset, ((x_mm, y_mm), spot_weight) in enumerate(zip(xy, weights), start=1):
                spots.append(
                    {
                        "BeamNumber": beam_number,
                        "BeamName": beam_name,
                        "LayerIndex": active_layer_index,
                        "ControlPointIndex": cp_index,
                        "Energy_MeVu": energy,
                        "SpotIndex": spot_offset,
                        "X_mm": float(x_mm),
                        "Y_mm": float(y_mm),
                        "MetersetWeight_MU": float(spot_weight),
                        "RelativeWeight": 0.0,
                        "NumberOfPaintings": paintings,
                        "WeightPerPainting_MU": float(spot_weight) / paintings,
                        "FWHM_X_mm": float(spot_size[0]),
                        "FWHM_Y_mm": float(spot_size[1]),
                    }
                )
            prior_cp = cp

        beam_layers = layers[beam_layer_start:]
        beam_spots = spots[beam_spot_start:]
        beam_weight = float(sum(float(row["MetersetWeight_MU"]) for row in beam_spots))
        if beam_weight <= 0:
            raise RuntimeError(f"Beam {beam_number}: total active spot weight is not positive")
        for row in beam_layers:
            row["RelativeWeight"] = float(row["TotalMetersetWeight_MU"]) / beam_weight
        for row in beam_spots:
            row["RelativeWeight"] = float(row["MetersetWeight_MU"]) / beam_weight

        final_weight = number(getattr(beam, "FinalCumulativeMetersetWeight", None))
        tolerance = max(1e-6, beam_weight * 1e-9)
        if final_weight is None:
            parse_warnings.append(ParseWarning(f"Beam {beam_number}", "FinalCumulativeMetersetWeight is missing"))
        elif not np.isclose(beam_weight, final_weight, rtol=1e-9, atol=tolerance):
            parse_warnings.append(
                ParseWarning(
                    f"Beam {beam_number}",
                    f"active weight sum {beam_weight:.12g} differs from final cumulative weight {final_weight:.12g}",
                )
            )

        cp0 = control_points[0] if control_points else Dataset()
        beam_summaries.append(
            {
                "BeamNumber": beam_number,
                "BeamName": beam_name,
                "PrimaryDosimeterUnit": dosimeter_unit,
                "ControlPoints": len(control_points),
                "ActiveLayers": len(beam_layers),
                "ZeroWeightControlPoints": zero_cp_count,
                "ActiveSpots": len(beam_spots),
                "ActiveMetersetWeight": beam_weight,
                "FinalCumulativeMetersetWeight": final_weight,
                "GantryAngle_deg": number(getattr(cp0, "GantryAngle", None)),
                "PatientSupportAngle_deg": number(getattr(cp0, "PatientSupportAngle", None)),
                "Isocenter_mm": array(getattr(cp0, "IsocenterPosition", None)).tolist(),
                "RangeModulators": parse_range_modulators(beam),
            }
        )

    plan_total = float(sum(float(row["MetersetWeight_MU"]) for row in spots))
    if plan_total <= 0:
        raise RuntimeError("Plan total active spot weight is not positive")
    for row in layers:
        row["PlanRelativeWeight"] = float(row["TotalMetersetWeight_MU"]) / plan_total
    for row in spots:
        row["PlanRelativeWeight"] = float(row["MetersetWeight_MU"]) / plan_total
    return layers, spots, beam_summaries, parse_warnings


def dose_summary_rows(rtdose_dir: Path, root: Path, plan_uid: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in sorted(rtdose_dir.glob("*.dcm")):
        ds = read_dicom(path)
        if text(getattr(ds, "Modality", None)) != "RTDOSE":
            LOGGER.warning("Skipping non-RTDOSE file in RTDOSE directory: %s", path)
            continue
        offsets = array(getattr(ds, "GridFrameOffsetVector", None))
        offset_steps = np.diff(offsets)
        frame_spacing = float(np.median(offset_steps)) if offset_steps.size else None
        referenced_plans = [
            text(getattr(item, "ReferencedSOPInstanceUID", None))
            for item in getattr(ds, "ReferencedRTPlanSequence", [])
        ]
        rows.append(
            {
                "RelativePath": path.relative_to(root).as_posix(),
                "SOPInstanceUID": text(getattr(ds, "SOPInstanceUID", None)),
                "DoseUnits": text(getattr(ds, "DoseUnits", None)),
                "DoseType": text(getattr(ds, "DoseType", None)),
                "DoseSummationType": text(getattr(ds, "DoseSummationType", None)),
                "ReferencedRTPlanUID": "|".join(referenced_plans),
                "ReferencesSelectedPlan": plan_uid in referenced_plans,
                "FrameOfReferenceUID": text(getattr(ds, "FrameOfReferenceUID", None)),
                "Frames": int(getattr(ds, "NumberOfFrames", 0)),
                "Rows": int(getattr(ds, "Rows", 0)),
                "Columns": int(getattr(ds, "Columns", 0)),
                "PixelSpacing_Row_mm": number(array(getattr(ds, "PixelSpacing", None))[0]) if array(getattr(ds, "PixelSpacing", None)).size >= 1 else None,
                "PixelSpacing_Column_mm": number(array(getattr(ds, "PixelSpacing", None))[1]) if array(getattr(ds, "PixelSpacing", None)).size >= 2 else None,
                "FrameSpacing_mm": frame_spacing,
                "ImagePositionPatient_mm": "|".join(f"{value:.12g}" for value in array(getattr(ds, "ImagePositionPatient", None))),
                "ImageOrientationPatient": "|".join(f"{value:.12g}" for value in array(getattr(ds, "ImageOrientationPatient", None))),
                "DoseGridScaling": number(getattr(ds, "DoseGridScaling", None)),
            }
        )
    return rows


def style_axis(axis: plt.Axes) -> None:
    axis.grid(True, alpha=0.22, linewidth=0.7)
    axis.spines[["top", "right"]].set_visible(False)


def save_figures(output_dir: Path, layers: list[dict[str, object]], spots: list[dict[str, object]], plan_name: str) -> None:
    energy = np.asarray([float(row["Energy_MeVu"]) for row in layers])
    layer_index = np.asarray([int(row["LayerIndex"]) for row in layers])
    spot_count = np.asarray([int(row["NumberOfSpots"]) for row in layers])
    layer_weight = np.asarray([float(row["TotalMetersetWeight_MU"]) for row in layers])
    fwhm_x = np.asarray([float(row["FWHM_X_mm"]) for row in layers])
    fwhm_y = np.asarray([float(row["FWHM_Y_mm"]) for row in layers])
    spot_x = np.asarray([float(row["X_mm"]) for row in spots])
    spot_y = np.asarray([float(row["Y_mm"]) for row in spots])
    spot_energy = np.asarray([float(row["Energy_MeVu"]) for row in spots])

    fig, axis = plt.subplots(figsize=(8.2, 7.2), constrained_layout=True)
    scatter = axis.scatter(spot_x, spot_y, c=spot_energy, cmap="turbo", s=2.0, alpha=0.24, linewidths=0, rasterized=True)
    axis.axhline(0, color="black", linewidth=0.6, alpha=0.35)
    axis.axvline(0, color="black", linewidth=0.6, alpha=0.35)
    axis.set(
        title=f"PBS scanning spot map — {plan_name}\n{len(spots):,} delivered spots in {len(layers)} energy layers",
        xlabel="Spot X at isocenter (mm)",
        ylabel="Spot Y at isocenter (mm)",
    )
    axis.set_aspect("equal")
    style_axis(axis)
    colorbar = fig.colorbar(scatter, ax=axis, shrink=0.82)
    colorbar.set_label("Nominal energy (MeV/u)")
    fig.savefig(output_dir / "spot_map.png", dpi=220)
    plt.close(fig)

    fig, axes = plt.subplots(2, 1, figsize=(9.2, 8.0), sharex=False, constrained_layout=True)
    axes[0].plot(layer_index, energy, color="#0066cc", marker="o", markersize=3.2, linewidth=1.5)
    axes[0].set(title=f"Active energy layers — {plan_name}", xlabel="Active layer index (1-based)", ylabel="Nominal energy (MeV/u)")
    style_axis(axes[0])
    axes[1].plot(energy, fwhm_x, color="#d62728", marker="o", markersize=3, linewidth=1.4, label="FWHM X")
    axes[1].plot(energy, fwhm_y, color="#ff7f0e", linestyle="--", linewidth=1.4, label="FWHM Y")
    axes[1].set(xlabel="Nominal energy (MeV/u)", ylabel="DICOM ScanningSpotSize / FWHM (mm)")
    axes[1].legend()
    style_axis(axes[1])
    fig.savefig(output_dir / "energy_layers.png", dpi=200)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(9.2, 5.4), constrained_layout=True)
    axis.plot(energy, spot_count, color="#6f42c1", marker="o", markersize=3.3, linewidth=1.4)
    axis.fill_between(energy, spot_count, color="#6f42c1", alpha=0.14)
    axis.set(
        title=f"Delivered spot count by energy — {plan_name}",
        xlabel="Nominal energy (MeV/u)",
        ylabel="Number of delivered spots",
    )
    style_axis(axis)
    fig.savefig(output_dir / "spot_count_by_energy.png", dpi=200)
    plt.close(fig)

    fig, axis = plt.subplots(figsize=(9.2, 5.4), constrained_layout=True)
    axis.plot(energy, layer_weight, color="#00875a", marker="o", markersize=3.3, linewidth=1.4)
    axis.fill_between(energy, layer_weight, color="#00875a", alpha=0.14)
    axis.set(
        title=f"Meterset weight by active energy layer — {plan_name}",
        xlabel="Nominal energy (MeV/u)",
        ylabel="Layer meterset weight (MU)",
    )
    style_axis(axis)
    fig.savefig(output_dir / "weight_by_energy.png", dpi=200)
    plt.close(fig)


def summary_text(
    plan_path: Path,
    ds: Dataset,
    layers: list[dict[str, object]],
    spots: list[dict[str, object]],
    beams: list[dict[str, object]],
    doses: list[dict[str, object]],
    parse_warnings: list[ParseWarning],
) -> str:
    energies = np.asarray([float(row["Energy_MeVu"]) for row in layers])
    x = np.asarray([float(row["X_mm"]) for row in spots])
    y = np.asarray([float(row["Y_mm"]) for row in spots])
    weight = np.asarray([float(row["MetersetWeight_MU"]) for row in spots])
    fwhm_x = np.asarray([float(row["FWHM_X_mm"]) for row in layers])
    fwhm_y = np.asarray([float(row["FWHM_Y_mm"]) for row in layers])
    paintings = sorted({int(row["NumberOfPaintings"]) for row in layers})
    beam = beams[0] if len(beams) == 1 else None
    all_modulators = [item for item in beams for item in item["RangeModulators"]]
    patient_position = text(
        getattr(ds.PatientSetupSequence[0], "PatientPosition", None)
        if getattr(ds, "PatientSetupSequence", None)
        else None
    )
    lines = [
        "TPS-TOPAS RT Ion Plan parsing summary",
        "=" * 39,
        f"Input RTPLAN (read-only): {plan_path}",
        f"SOPClassUID: {text(getattr(ds, 'SOPClassUID', None))} (RT Ion Plan Storage)",
        f"SOPInstanceUID: {text(getattr(ds, 'SOPInstanceUID', None))}",
        f"FrameOfReferenceUID: {text(getattr(ds, 'FrameOfReferenceUID', None))}",
        f"Plan label: {text(getattr(ds, 'RTPlanLabel', None))}",
        f"Plan name: {text(getattr(ds, 'RTPlanName', None))}",
        f"Patient position: {patient_position}",
        f"Plan approval: {text(getattr(ds, 'ApprovalStatus', None))}",
        "",
        "Ion and delivery:",
        f"  Ion: Carbon-12 (Z={getattr(ds.IonBeamSequence[0], 'RadiationAtomicNumber', 'MISSING')}, A={getattr(ds.IonBeamSequence[0], 'RadiationMassNumber', 'MISSING')}, charge={getattr(ds.IonBeamSequence[0], 'RadiationChargeState', 'MISSING'):+g})" if len(getattr(ds, "IonBeamSequence", [])) == 1 else "  Ion: see per-beam DICOM fields",
        f"  Scan mode: {text(getattr(ds.IonBeamSequence[0], 'ScanMode', None)) if len(getattr(ds, 'IonBeamSequence', [])) == 1 else 'multiple beams'}",
        f"  Number of beams: {len(beams)}",
        f"  Primary dosimeter unit(s): {sorted({text(item['PrimaryDosimeterUnit']) for item in beams})}",
        f"  Control points: {sum(int(item['ControlPoints']) for item in beams)}",
        f"  Active energy layers: {len(layers)}",
        f"  Filtered all-zero control points: {sum(int(item['ZeroWeightControlPoints']) for item in beams)}",
        f"  Delivered spots: {len(spots)}",
        f"  Energy range: {energies.min():.6g} to {energies.max():.6g} MeV/u",
        f"  Spot X range: {x.min():.6g} to {x.max():.6g} mm",
        f"  Spot Y range: {y.min():.6g} to {y.max():.6g} mm",
        f"  Spot FWHM X range: {fwhm_x.min():.6g} to {fwhm_x.max():.6g} mm",
        f"  Spot FWHM Y range: {fwhm_y.min():.6g} to {fwhm_y.max():.6g} mm",
        f"  NumberOfPaintings values: {paintings}",
        "",
        "Meterset weights:",
        f"  Sum of delivered spot weights: {weight.sum(dtype=np.float64):.12g} MU",
        f"  Minimum/maximum spot weight: {weight.min():.8g} / {weight.max():.8g} MU",
        "  Relative-weight definition: PlanRelativeWeight_i = W_i / sum(all delivered spot W)",
        "  No MU-to-particle or absolute-dose conversion has been applied.",
    ]
    if beam is not None:
        difference = (
            None
            if beam["FinalCumulativeMetersetWeight"] is None
            else float(beam["ActiveMetersetWeight"]) - float(beam["FinalCumulativeMetersetWeight"])
        )
        lines.extend(
            [
                f"  FinalCumulativeMetersetWeight: {beam['FinalCumulativeMetersetWeight']} MU",
                f"  Weight sum minus final cumulative: {difference:.12g} MU" if difference is not None else "  Weight sum minus final cumulative: unavailable",
                "",
                "Beam geometry (DICOM values; no IEC-to-TOPAS sign conversion):",
                f"  Beam {beam['BeamNumber']} ({beam['BeamName']}): gantry={beam['GantryAngle_deg']} deg, couch={beam['PatientSupportAngle_deg']} deg",
                f"  Isocenter: {beam['Isocenter_mm']} mm",
            ]
        )
    lines.extend(["", "Range modulator(s):"])
    if all_modulators:
        for item in all_modulators:
            lines.append(f"  {item['id']}: type={item['type']}; description={item['description']}; number={item['number']}")
    else:
        lines.append("  None declared")
    lines.extend(
        [
            "  NOTE: Presence in RTPLAN is confirmed; no MRF geometry or commissioning model is implemented by this parser.",
            "",
            "RTDOSE references:",
        ]
    )
    for item in doses:
        lines.append(
            f"  {Path(str(item['RelativePath'])).name}: {item['DoseType']} / {item['DoseSummationType']} / {item['DoseUnits']}; grid={item['Frames']}x{item['Rows']}x{item['Columns']}; references plan={item['ReferencesSelectedPlan']}"
        )
    lines.extend(["", "Validation:"])
    lines.append(f"  {'PASS' if not parse_warnings else 'PASS WITH WARNINGS'}: RT Ion Plan parsed successfully")
    if parse_warnings:
        for warning in parse_warnings:
            lines.append(f"  WARNING [{warning.scope}]: {warning.message}")
    else:
        lines.append("  No parser validation warnings")
    lines.extend(
        [
            "",
            "Index conventions:",
            "  LayerIndex and SpotIndex are 1-based derived indices.",
            "  ControlPointIndex is the original DICOM value (0-based in this plan).",
            "  Only CPs containing at least one non-zero ScanSpotMetersetWeight are emitted as active layers.",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    root = args.root.expanduser().resolve()
    dicom_dir = (root / "dicom").resolve()
    rtplan_dir = dicom_dir / "RTPLAN"
    rtdose_dir = (args.rtdose_dir or dicom_dir / "RTDOSE").expanduser().resolve()
    output_dir = (args.output_dir or root / "plan_parsed").expanduser().resolve()
    try:
        ensure_output_policy(output_dir, dicom_dir, args.overwrite)
        plan_path = args.rtplan.expanduser().resolve() if args.rtplan else discover_ion_plan(rtplan_dir)
        if not plan_path.is_file():
            raise RuntimeError(f"RTPLAN does not exist: {plan_path}")
        if not rtdose_dir.is_dir():
            raise RuntimeError(f"RTDOSE directory does not exist: {rtdose_dir}")
        ds = read_dicom(plan_path)
        if text(getattr(ds, "SOPClassUID", None)) != ION_PLAN_STORAGE_UID:
            raise RuntimeError(f"Selected file is not RT Ion Plan Storage: {plan_path}")
        layers, spots, beams, parse_warnings = parse_plan(ds)
        doses = dose_summary_rows(rtdose_dir, root, text(getattr(ds, "SOPInstanceUID", None)))
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        return 2

    layer_fields = [
        "BeamNumber",
        "BeamName",
        "LayerIndex",
        "ControlPointIndex",
        "Energy_MeVu",
        "NumberOfSpots",
        "TotalMetersetWeight_MU",
        "RelativeWeight",
        "PlanRelativeWeight",
        "NumberOfPaintings",
        "FWHM_X_mm",
        "FWHM_Y_mm",
        "CumulativeMetersetWeightBeforeLayer_MU",
        "MinX_mm",
        "MaxX_mm",
        "MinY_mm",
        "MaxY_mm",
    ]
    spot_fields = [
        "BeamNumber",
        "BeamName",
        "LayerIndex",
        "ControlPointIndex",
        "Energy_MeVu",
        "SpotIndex",
        "X_mm",
        "Y_mm",
        "MetersetWeight_MU",
        "RelativeWeight",
        "PlanRelativeWeight",
        "NumberOfPaintings",
        "WeightPerPainting_MU",
        "FWHM_X_mm",
        "FWHM_Y_mm",
    ]
    dose_fields = [
        "RelativePath",
        "SOPInstanceUID",
        "DoseUnits",
        "DoseType",
        "DoseSummationType",
        "ReferencedRTPlanUID",
        "ReferencesSelectedPlan",
        "FrameOfReferenceUID",
        "Frames",
        "Rows",
        "Columns",
        "PixelSpacing_Row_mm",
        "PixelSpacing_Column_mm",
        "FrameSpacing_mm",
        "ImagePositionPatient_mm",
        "ImageOrientationPatient",
        "DoseGridScaling",
    ]
    write_csv(output_dir / "energy_layers.csv", layer_fields, layers)
    write_csv(output_dir / "spots.csv", spot_fields, spots)
    write_csv(output_dir / "rtdose_summary.csv", dose_fields, doses)
    (output_dir / "plan_summary.txt").write_text(
        summary_text(plan_path, ds, layers, spots, beams, doses, parse_warnings),
        encoding="utf-8",
    )
    save_figures(output_dir, layers, spots, text(getattr(ds, "RTPlanName", None)))

    LOGGER.info("RT Ion Plan: %s", plan_path)
    LOGGER.info("Parsed %d active layer(s), %d delivered spot(s)", len(layers), len(spots))
    LOGGER.info("Total delivered meterset weight: %.12g MU", sum(float(row["MetersetWeight_MU"]) for row in spots))
    LOGGER.info("Energy range: %.6g to %.6g MeV/u", min(float(row["Energy_MeVu"]) for row in layers), max(float(row["Energy_MeVu"]) for row in layers))
    for name in EXPECTED_OUTPUTS:
        LOGGER.info("Wrote %s", output_dir / name)
    if parse_warnings:
        for warning in parse_warnings:
            LOGGER.warning("[%s] %s", warning.scope, warning.message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
