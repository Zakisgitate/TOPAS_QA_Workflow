#!/usr/bin/env python3
"""Export TPS/MC beam-depth dose and two transverse profiles."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager, ticker
import numpy as np
import pandas as pd
import pydicom
from scipy.ndimage import gaussian_filter1d

from utils.mc_dose_calibration import resolve_particle_calibration


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--tps-dose", type=Path, help="Selected TPS RTDOSE (default: PHYSICAL/PLAN)")
    parser.add_argument(
        "--analysis-dir",
        type=Path,
        help="Run-specific analysis directory (default: ROOT/analysis)",
    )
    parser.add_argument("--mc-binary", type=Path, required=True)
    parser.add_argument("--profile-depth-mm", type=float, default=100.0)
    parser.add_argument("--central-roi-half-width-mm", type=float, default=5.0)
    parser.add_argument("--profile-slab-half-width-mm", type=float, default=4.0)
    parser.add_argument("--smoothing-sigma-voxels", type=float, default=1.0)
    parser.add_argument(
        "--output-tag",
        help="Optional filename suffix, for example full_plan_150k",
    )
    parser.add_argument(
        "--mc-label",
        default="MC (low-statistics QA)",
        help="Legend label for the MC series",
    )
    parser.add_argument(
        "--full-plan",
        action="store_true",
        help="Treat the input as a complete-spot plan rather than the 265-anchor QA subset",
    )
    parser.add_argument("--run-log", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def discover_rppd(root: Path) -> Path:
    matches: list[Path] = []
    for path in sorted((root / "dicom" / "RTDOSE").glob("*.dcm")):
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        if (
            str(ds.DoseUnits).upper() == "GY"
            and str(ds.DoseType).upper() == "PHYSICAL"
            and str(ds.DoseSummationType).upper() == "PLAN"
        ):
            matches.append(path.resolve())
    if len(matches) != 1:
        raise RuntimeError(f"Expected one physical plan RTDOSE, found: {matches}")
    return matches[0]


def discover_rtplan(root: Path) -> Path:
    matches: list[Path] = []
    for path in sorted((root / "dicom" / "RTPLAN").glob("*.dcm")):
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        if str(getattr(ds, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.481.8":
            matches.append(path.resolve())
    if len(matches) != 1:
        raise RuntimeError(f"Expected one RT Ion Plan, found: {matches}")
    return matches[0]


def plan_geometry(root: Path) -> tuple[np.ndarray, int, float, float]:
    plan_path = discover_rtplan(root)
    ds = pydicom.dcmread(plan_path, stop_before_pixels=True)
    beams = list(getattr(ds, "IonBeamSequence", []))
    if len(beams) != 1:
        raise RuntimeError(
            f"Profile export currently supports one beam, found {len(beams)}; "
            "multi-beam profiles require beam-specific depth-axis transforms"
        )
    beam = beams[0]
    cps = list(getattr(beam, "IonControlPointSequence", []))
    if not cps or not hasattr(cps[0], "IsocenterPosition"):
        raise RuntimeError("RT Ion Plan has no usable IsocenterPosition")
    isocenter = np.asarray(cps[0].IsocenterPosition, dtype=float)
    gantry = float(getattr(cps[0], "GantryAngle", getattr(beam, "GantryAngle", float("nan"))))
    support = float(getattr(cps[0], "PatientSupportAngle", 0.0))
    position = ""
    if getattr(ds, "PatientSetupSequence", None):
        position = str(getattr(ds.PatientSetupSequence[0], "PatientPosition", ""))
    if position != "HFS" or not np.isclose(gantry, 90.0) or not np.isclose(support, 0.0):
        raise RuntimeError(
            "Profile export currently supports the validated HFS/G90/couch0 mapping only; "
            f"got PatientPosition={position}, gantry={gantry}, couch={support}"
        )
    return isocenter, len(beams), gantry, support


def beam_entry_x(root: Path, isocenter: np.ndarray, dose_grid_edge_x: float) -> tuple[float, str]:
    """Find the +X beam-entry surface at isocenter Y/Z for the gated G90 beam."""
    model_path = root / "plan_parsed" / "patient_model.json"
    if not model_path.is_file():
        return dose_grid_edge_x, "RPPD +X grid edge (legacy fallback)"
    model = json.loads(model_path.read_text(encoding="utf-8"))
    if model.get("mode") == "WATER_PHANTOM":
        external = model.get("external_roi") or {}
        bounds = external.get("bounds_max_mm")
        if bounds:
            return float(bounds[0]), f"water-box External ROI '{external.get('name', '')}'"
    external = model.get("external_roi") or {}
    roi_number = external.get("number")
    structure_path = (model.get("sources") or {}).get("rtstruct")
    if roi_number is None or not structure_path or not Path(structure_path).is_file():
        return dose_grid_edge_x, "RPPD +X grid edge (External unavailable)"
    structure = pydicom.dcmread(Path(structure_path))
    contours: list[np.ndarray] = []
    for roi in getattr(structure, "ROIContourSequence", []):
        if int(getattr(roi, "ReferencedROINumber", -1)) != int(roi_number):
            continue
        for contour in getattr(roi, "ContourSequence", []):
            values = np.asarray(getattr(contour, "ContourData", []), dtype=float)
            if values.size and values.size % 3 == 0:
                contours.append(values.reshape(-1, 3))
    if not contours:
        return dose_grid_edge_x, "RPPD +X grid edge (External has no contours)"
    contour_z = np.asarray([float(np.mean(points[:, 2])) for points in contours])
    selected_z = contour_z[int(np.argmin(np.abs(contour_z - isocenter[2])))]
    intersections: list[float] = []
    for points, z_value in zip(contours, contour_z):
        if not np.isclose(z_value, selected_z, atol=1e-3):
            continue
        for first, second in zip(points, np.roll(points, -1, axis=0)):
            y1, y2 = first[1], second[1]
            if np.isclose(y1, y2):
                if np.isclose(isocenter[1], y1, atol=1e-6):
                    intersections.extend((float(first[0]), float(second[0])))
            elif (y1 <= isocenter[1] < y2) or (y2 <= isocenter[1] < y1):
                fraction = (isocenter[1] - y1) / (y2 - y1)
                intersections.append(float(first[0] + fraction * (second[0] - first[0])))
    if not intersections:
        return dose_grid_edge_x, "RPPD +X grid edge (External does not cross central axis)"
    return max(intersections), f"External ROI '{external.get('name', '')}' at patient Z={selected_z:.3f} mm"


def register_plot_font() -> None:
    candidates = (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Helvetica.ttc"),
    )
    for path in candidates:
        if path.is_file():
            font_manager.fontManager.addfont(path)
            plt.rcParams["font.family"] = font_manager.FontProperties(
                fname=path
            ).get_name()
            break
    plt.rcParams["axes.unicode_minus"] = False


def ensure_outputs(paths: list[Path], overwrite: bool) -> None:
    existing = [path for path in paths if path.exists()]
    if existing and not overwrite:
        listing = "\n".join(str(path) for path in existing)
        raise RuntimeError(f"Output files already exist; add --overwrite:\n{listing}")
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)


def smooth_and_normalize(values: np.ndarray, sigma: float) -> tuple[np.ndarray, np.ndarray]:
    smooth = gaussian_filter1d(np.asarray(values, dtype=float), sigma=sigma)
    smooth = np.clip(smooth, 0.0, None)
    peak = float(smooth.max())
    if not np.isfinite(peak) or peak <= 0:
        raise RuntimeError("Cannot normalize an empty or non-finite dose profile")
    return smooth, smooth / peak


def output_limit(coord: np.ndarray, tps_norm: np.ndarray, margin_mm: float = 20.0) -> tuple[float, float]:
    indices = np.flatnonzero(tps_norm >= 0.005)
    if not len(indices):
        return float(coord.min()), float(coord.max())
    low = max(float(coord.min()), float(coord[indices[0]]) - margin_mm)
    high = min(float(coord.max()), float(coord[indices[-1]]) + margin_mm)
    return math.floor(low / 10.0) * 10.0, math.ceil(high / 10.0) * 10.0


def range_at_level(depth: np.ndarray, normalized: np.ndarray, level: float) -> float:
    indices = np.flatnonzero(normalized >= level)
    return float(depth[indices[-1]]) if len(indices) else float("nan")


def width_at_level(coord: np.ndarray, normalized: np.ndarray, level: float) -> float:
    indices = np.flatnonzero(normalized >= level)
    return float(coord[indices[-1]] - coord[indices[0]]) if len(indices) else float("nan")


def excel_style_plot(
    coord: np.ndarray,
    tps_norm: np.ndarray,
    mc_norm: np.ndarray,
    title: str,
    xlabel: str,
    output: Path,
    xlim: tuple[float, float],
    tps_label: str,
    mc_label: str,
) -> None:
    fig, ax = plt.subplots(figsize=(11.0, 7.8), facecolor="white")
    ax.set_facecolor("white")
    markevery = max(1, len(coord) // 45)
    series = (
        (tps_norm, tps_label, "#4472C4"),
        (mc_norm, mc_label, "#70AD47"),
    )
    for values, label, color in series:
        ax.plot(
            coord,
            values,
            color=color,
            linewidth=2.4,
            marker="o",
            markersize=5.7,
            markevery=markevery,
            markerfacecolor=color,
            markeredgecolor=color,
            label=label,
        )
    ax.set_title(title, fontsize=24, color="#555555", pad=25, fontweight="normal")
    ax.set_xlabel(xlabel, fontsize=14, color="#666666", labelpad=12)
    ax.set_ylabel("Normalized dose", fontsize=14, color="#666666", labelpad=12)
    ax.set_xlim(*xlim)
    ax.set_ylim(0.0, 1.2)
    ax.yaxis.set_major_locator(ticker.MultipleLocator(0.2))
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.4f"))
    span = xlim[1] - xlim[0]
    major_step = 50.0 if span > 250.0 else 20.0
    ax.xaxis.set_major_locator(ticker.MultipleLocator(major_step))
    ax.xaxis.set_major_formatter(ticker.FormatStrFormatter("%.0f"))
    ax.grid(True, which="major", color="#D9D9D9", linewidth=1.35)
    ax.set_axisbelow(True)
    for spine in ax.spines.values():
        spine.set_color("#BFBFBF")
        spine.set_linewidth(1.2)
    ax.tick_params(axis="both", colors="#666666", labelsize=13, length=0, pad=8)
    ax.legend(
        loc="upper center",
        bbox_to_anchor=(0.5, -0.14),
        ncol=2,
        frameon=False,
        fontsize=14,
        handlelength=2.8,
        handletextpad=0.45,
        columnspacing=1.8,
    )
    fig.subplots_adjust(left=0.12, right=0.97, top=0.88, bottom=0.22)
    fig.savefig(output, dpi=180, facecolor="white")
    plt.close(fig)


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    mc_path = args.mc_binary.expanduser().resolve()
    analysis_dir = (args.analysis_dir or root / "analysis").expanduser().resolve()
    figures = analysis_dir / "figures"
    profiles = analysis_dir / "profiles"
    if args.output_tag and not re.fullmatch(r"[A-Za-z0-9_-]+", args.output_tag):
        raise RuntimeError("--output-tag may contain only letters, digits, underscore and hyphen")
    suffix = f"_{args.output_tag}" if args.output_tag else ""
    depth_csv = profiles / f"depth_dose{suffix}.csv"
    profile_x_csv = profiles / f"transverse_profile_x{suffix}.csv"
    profile_y_csv = profiles / f"transverse_profile_y{suffix}.csv"
    depth_png = figures / (
        f"depth_direction{suffix}.png" if suffix else "depth_direction_tps_mc_qa.png"
    )
    profile_x_png = figures / (
        f"transverse_x{suffix}.png" if suffix else "transverse_x_tps_mc_qa.png"
    )
    profile_y_png = figures / (
        f"transverse_y{suffix}.png" if suffix else "transverse_y_tps_mc_qa.png"
    )
    summary_path = profiles / f"profile_export_summary{suffix}.txt"
    outputs = [
        depth_csv,
        profile_x_csv,
        profile_y_csv,
        depth_png,
        profile_x_png,
        profile_y_png,
        summary_path,
    ]
    ensure_outputs(outputs, args.overwrite)
    if not mc_path.is_file():
        raise RuntimeError(f"MC binary does not exist: {mc_path}")

    run_histories: int | None = None
    run_real_seconds: float | None = None
    run_log = (args.run_log or root / "topas_output" / "test" / "run_low_statistics.log").resolve()
    if run_log.is_file():
        log_text = run_log.read_bytes().replace(b"\x00", b"").decode("utf-8", errors="replace")
        history_matches = re.findall(
            r"Particle source PlanCarbonBeam: Total number of histories: (\d+)", log_text
        )
        time_matches = re.findall(r"Total: User=.*?Real=([0-9.]+)s", log_text)
        if history_matches:
            run_histories = int(history_matches[-1])
        if time_matches:
            run_real_seconds = float(time_matches[-1])

    rppd_path = (args.tps_dose or discover_rppd(root)).expanduser().resolve()
    if not rppd_path.is_file():
        raise RuntimeError(f"Selected TPS RTDOSE does not exist: {rppd_path}")
    isocenter_mm, beam_count, gantry_deg, support_deg = plan_geometry(root)
    ds = pydicom.dcmread(rppd_path)
    if str(getattr(ds, "Modality", "")).upper() != "RTDOSE":
        raise RuntimeError(f"Selected TPS dose is not RTDOSE: {rppd_path}")
    dose_units = str(getattr(ds, "DoseUnits", "")).upper()
    if dose_units not in {"GY", "CGY"}:
        raise RuntimeError(f"Selected TPS RTDOSE must use GY or CGY, got {dose_units}")
    dose_type = str(getattr(ds, "DoseType", "")).upper() or "UNKNOWN"
    summation_type = str(getattr(ds, "DoseSummationType", "")).upper() or "UNKNOWN"
    tps_plot_label = f"TPS ({summation_type}/{dose_type})"
    tps = ds.pixel_array.astype(np.float64) * float(ds.DoseGridScaling)
    if dose_units == "CGY":
        tps /= 100.0
    mc = np.fromfile(mc_path, dtype=np.float64)
    if mc.size != tps.size:
        raise RuntimeError(f"MC/TPS grid size mismatch: {mc.size} versus {tps.size}")
    mc = mc.reshape(tps.shape)
    if not np.isfinite(mc).all() or mc.max() <= 0:
        raise RuntimeError("MC binary is empty or contains non-finite values")
    calibration = resolve_particle_calibration(root, mc_path)
    mc_topas_run_max = float(mc.max())
    if calibration.available:
        mc *= calibration.scale
    if calibration.simulated_histories:
        # The allocation snapshot is authoritative for multi-source commissioned
        # runs; TOPAS log lines may report zero for the legacy PlanCarbonBeam name.
        run_histories = calibration.simulated_histories
    mc_column_prefix = "MC_particle_calibrated" if calibration.available else "MC_TOPAS_per_run_uncalibrated"

    ipp = np.asarray(ds.ImagePositionPatient, dtype=float)
    x = ipp[0] + np.arange(tps.shape[2]) * float(ds.PixelSpacing[1])
    y = ipp[1] + np.arange(tps.shape[1]) * float(ds.PixelSpacing[0])
    z = ipp[2] + np.asarray(ds.GridFrameOffsetVector, dtype=float)
    spacing_x = float(ds.PixelSpacing[1])
    dose_grid_edge_x = float(x.max() + 0.5 * spacing_x)
    entrance_x_mm, entrance_source = beam_entry_x(root, isocenter_mm, dose_grid_edge_x)
    depth = entrance_x_mm - x

    center_y = np.flatnonzero(
        np.abs(y - isocenter_mm[1]) <= args.central_roi_half_width_mm
    )
    center_z = np.flatnonzero(
        np.abs(z - isocenter_mm[2]) <= args.central_roi_half_width_mm
    )
    if not len(center_y) or not len(center_z):
        raise RuntimeError("Central-axis ROI does not intersect the dose grid")
    all_x = np.arange(tps.shape[2])
    tps_depth_raw = tps[np.ix_(center_z, center_y, all_x)].mean(axis=(0, 1))
    mc_depth_raw = mc[np.ix_(center_z, center_y, all_x)].mean(axis=(0, 1))
    order = np.argsort(depth)
    depth = depth[order]
    x_for_depth = x[order]
    tps_depth_raw = tps_depth_raw[order]
    mc_depth_raw = mc_depth_raw[order]
    tps_depth_smooth, tps_depth_norm = smooth_and_normalize(
        tps_depth_raw, args.smoothing_sigma_voxels
    )
    mc_depth_smooth, mc_depth_norm = smooth_and_normalize(
        mc_depth_raw, args.smoothing_sigma_voxels
    )
    depth_frame = pd.DataFrame(
        {
            "Depth_from_beam_entry_mm": depth,
            "DICOM_patient_X_mm": x_for_depth,
            "TPS_ROI_mean_raw_Gy": tps_depth_raw,
            "TPS_ROI_mean_smoothed_Gy": tps_depth_smooth,
            "TPS_normalized": tps_depth_norm,
            f"{mc_column_prefix}_ROI_mean_Gy": mc_depth_raw,
            f"{mc_column_prefix}_ROI_mean_smoothed_Gy": mc_depth_smooth,
            "MC_QA_normalized": mc_depth_norm,
        }
    )
    depth_frame.to_csv(depth_csv, index=False, float_format="%.10g")

    profile_index = int(np.argmin(np.abs(depth - args.profile_depth_mm)))
    selected_depth = float(depth[profile_index])
    selected_x = float(x_for_depth[profile_index])
    # Return from sorted depth indexing to the original ascending DICOM X grid.
    original_profile_index = int(np.argmin(np.abs(x - selected_x)))
    profile_x_indices = np.flatnonzero(
        np.abs(depth - selected_depth) <= args.profile_slab_half_width_mm
    )
    profile_x_coords = x_for_depth[profile_x_indices]
    original_depth_indices = np.asarray(
        [int(np.argmin(np.abs(x - value))) for value in profile_x_coords], dtype=int
    )

    tps_profile_x_raw = tps[np.ix_(center_z, np.arange(tps.shape[1]), original_depth_indices)].mean(
        axis=(0, 2)
    )
    mc_profile_x_raw = mc[np.ix_(center_z, np.arange(mc.shape[1]), original_depth_indices)].mean(
        axis=(0, 2)
    )
    tps_profile_x_smooth, tps_profile_x_norm = smooth_and_normalize(
        tps_profile_x_raw, args.smoothing_sigma_voxels
    )
    mc_profile_x_smooth, mc_profile_x_norm = smooth_and_normalize(
        mc_profile_x_raw, args.smoothing_sigma_voxels
    )
    profile_x_frame = pd.DataFrame(
        {
            "IEC_X_relative_to_isocenter_mm": y - isocenter_mm[1],
            "DICOM_patient_Y_mm": y,
            "TPS_raw_Gy": tps_profile_x_raw,
            "TPS_smoothed_Gy": tps_profile_x_smooth,
            "TPS_normalized": tps_profile_x_norm,
            f"{mc_column_prefix}_Gy": mc_profile_x_raw,
            f"{mc_column_prefix}_smoothed_Gy": mc_profile_x_smooth,
            "MC_QA_normalized": mc_profile_x_norm,
        }
    )
    profile_x_frame.to_csv(profile_x_csv, index=False, float_format="%.10g")

    tps_profile_y_raw = tps[np.ix_(np.arange(tps.shape[0]), center_y, original_depth_indices)].mean(
        axis=(1, 2)
    )
    mc_profile_y_raw = mc[np.ix_(np.arange(mc.shape[0]), center_y, original_depth_indices)].mean(
        axis=(1, 2)
    )
    tps_profile_y_smooth, tps_profile_y_norm = smooth_and_normalize(
        tps_profile_y_raw, args.smoothing_sigma_voxels
    )
    mc_profile_y_smooth, mc_profile_y_norm = smooth_and_normalize(
        mc_profile_y_raw, args.smoothing_sigma_voxels
    )
    profile_y_frame = pd.DataFrame(
        {
            "IEC_Y_relative_to_isocenter_mm": z - isocenter_mm[2],
            "DICOM_patient_Z_mm": z,
            "TPS_raw_Gy": tps_profile_y_raw,
            "TPS_smoothed_Gy": tps_profile_y_smooth,
            "TPS_normalized": tps_profile_y_norm,
            f"{mc_column_prefix}_Gy": mc_profile_y_raw,
            f"{mc_column_prefix}_smoothed_Gy": mc_profile_y_smooth,
            "MC_QA_normalized": mc_profile_y_norm,
        }
    )
    profile_y_frame.to_csv(profile_y_csv, index=False, float_format="%.10g")

    register_plot_font()
    excel_style_plot(
        depth,
        tps_depth_norm,
        mc_depth_norm,
        "Depth Direction",
        "Depth from Beam Entry (mm)",
        depth_png,
        output_limit(depth, tps_depth_norm),
        tps_plot_label,
        args.mc_label,
    )
    x_limits = output_limit(y, tps_profile_x_norm)
    excel_style_plot(
        y,
        tps_profile_x_norm,
        mc_profile_x_norm,
        f"Transverse X Direction (Depth {selected_depth:.0f} mm)",
        "Patient Y Coordinate / IEC X (mm)",
        profile_x_png,
        x_limits,
        tps_plot_label,
        args.mc_label,
    )
    y_limits = output_limit(z, tps_profile_y_norm)
    excel_style_plot(
        z,
        tps_profile_y_norm,
        mc_profile_y_norm,
        f"Transverse Y Direction (Depth {selected_depth:.0f} mm)",
        "Patient Z Coordinate / IEC Y (mm)",
        profile_y_png,
        y_limits,
        tps_plot_label,
        args.mc_label,
    )

    plan_spot_count = len(pd.read_csv(root / "plan_parsed" / "spots.csv"))
    if args.full_plan:
        if calibration.available:
            scope_text = f"""The MC input uses all {plan_spot_count:,} planned spots with their relative
weights. The latest matching run log reports {run_histories if run_histories is not None else 'unknown'}
histories. MC Gy values use the independent commissioned particle-number scale
N_plan/N_sim; TPS dose was not used to fit MC output. The plotted profiles remain
independently normalized so their geometry/range shapes can be read clearly.
This is still a low-particle research result and not a clinical acceptance test."""
            status = "EXPORTED: PARTICLE-CALIBRATED COMPLETE-SPOT RESEARCH QA"
        else:
            scope_text = f"""The MC input uses all {plan_spot_count:,} planned spots, but independent
particle calibration is unavailable: {calibration.reason}. MC Gy columns contain
TOPAS per-run values and the figures are independently normalized shape checks.
Gamma and particle-calibrated DICOM export remain disabled for this result."""
            status = "EXPORTED: UNCALIBRATED COMPLETE-SPOT SHAPE QA"
    else:
        layer_count = pd.read_csv(root / "plan_parsed" / "spots.csv")["LayerIndex"].nunique()
        scope_text = f"""The MC input contains 265 representative spatial anchors across {layer_count} energy layers
and 15,000 histories. It is not the full {plan_spot_count:,}-spot fluence map, is not absolutely
calibrated, and does not include the commissioned machine/range-shifter model.
The three figures are suitable for visualization and coarse geometry/range QA only.
The transverse MC curves are expected to show the sparse anchor pattern rather than
the flat field of a complete-plan calculation."""
        status = "EXPORTED WITH LOW-STATISTICS MC QA LIMITATIONS"

    calibration_summary = (
        f"""{calibration.protocol}
N_plan / N_sim / scale: {calibration.planned_particles:.10g} / {calibration.simulated_histories:,} / {calibration.scale:.10g}
Allocation snapshot: {calibration.allocation_file}
Spot-allocation L1 difference: {calibration.allocation_l1_fraction:.4%}"""
        if calibration.available
        else f"UNAVAILABLE — {calibration.reason}"
    )
    summary_text = f"""TPS-TOPAS three-direction profile export
========================================
Status: {status}

Inputs
------
Selected TPS dose: {rppd_path}
TPS DoseType / DoseSummationType / DoseUnits: {dose_type} / {summation_type} / {dose_units}
MC QA dose: {mc_path}
MC normalization: {calibration_summary}
TOPAS-per-run / particle-calibrated volume maximum: {mc_topas_run_max:.10g} / {float(mc.max()):.10g} Gy
Run log: {run_log if run_log.is_file() else 'not available'}
Run histories / real time: {run_histories if run_histories is not None else 'unknown'} / {run_real_seconds if run_real_seconds is not None else 'unknown'} s
Measurement data: not present in measurement/; no measured series was plotted
Grid shape [Z,Y,X]: {tps.shape}
Isocenter patient [X,Y,Z]: {isocenter_mm.tolist()} mm
Beam count / gantry / couch: {beam_count} / {gantry_deg:.1f} / {support_deg:.1f} deg
Beam entrance patient X: {entrance_x_mm:.3f} mm ({entrance_source})

Extraction
----------
Depth direction: central-axis transverse ROI mean, half width {args.central_roi_half_width_mm:.1f} mm
Transverse profiles: layer centered at beam depth {selected_depth:.1f} mm
                     patient X {selected_x:.1f} mm, slab half width {args.profile_slab_half_width_mm:.1f} mm
IEC X profile mapping: patient Y
IEC Y profile mapping: patient Z
Gaussian display smoothing sigma: {args.smoothing_sigma_voxels:.1f} voxel
CSV dose columns: TPS Gy and {'independently particle-number-calibrated MC Gy' if calibration.available else 'uncalibrated TOPAS per-run dose'}
Figure normalization: TPS and MC independently normalized to their own smoothed maxima

Derived coarse values
---------------------
TPS / MC distal R50: {range_at_level(depth, tps_depth_norm, 0.5):.1f} / {range_at_level(depth, mc_depth_norm, 0.5):.1f} mm
TPS / MC IEC-X FWHM: {width_at_level(y, tps_profile_x_norm, 0.5):.1f} / {width_at_level(y, mc_profile_x_norm, 0.5):.1f} mm
TPS / MC IEC-Y FWHM: {width_at_level(z, tps_profile_y_norm, 0.5):.1f} / {width_at_level(z, mc_profile_y_norm, 0.5):.1f} mm

Limitations
-----------
{scope_text}
No empirical 0.976 correction was applied. {'The particle scale removes the former TPS-peak-normalization circularity.' if calibration.available else 'No TPS-peak scale was substituted for the missing particle calibration.'}
Commissioning inputs and Monte Carlo statistical uncertainty must still be
validated independently. Low-statistics flag: {calibration.preliminary_low_statistics}.

Outputs
-------
Depth CSV: {depth_csv}
Transverse X CSV: {profile_x_csv}
Transverse Y CSV: {profile_y_csv}
Depth figure: {depth_png}
Transverse X figure: {profile_x_png}
Transverse Y figure: {profile_y_png}
"""
    summary_path.write_text(summary_text, encoding="utf-8")
    print(f"Exported: {depth_png}")
    print(f"Exported: {profile_x_png}")
    print(f"Exported: {profile_y_png}")
    print(f"Exported: {depth_csv}")
    print(f"Profile depth: {selected_depth:.1f} mm (DICOM X={selected_x:.1f} mm)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
