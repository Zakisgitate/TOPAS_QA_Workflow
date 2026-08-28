#!/usr/bin/env python3
"""Calculate a global 3D Gamma index between TPS RPPD and TOPAS MC dose."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import time

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.colors import BoundaryNorm, ListedColormap
from matplotlib.patches import Patch
import numpy as np
import pandas as pd
import pydicom
from scipy.ndimage import map_coordinates

from utils.mc_dose_calibration import require_particle_calibration


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
    parser.add_argument("--dta-mm", type=float, required=True, help="Distance-to-agreement criterion in mm")
    parser.add_argument("--dd-percent", type=float, required=True, help="Global dose-difference criterion in percent")
    parser.add_argument(
        "--low-dose-threshold-percent",
        type=float,
        default=10.0,
        help="Exclude TPS voxels below this percentage of TPS maximum (default: 10)",
    )
    parser.add_argument(
        "--interpolation-fraction",
        type=int,
        default=5,
        help="Gamma search step = min(grid spacing, DTA)/fraction (default: 5)",
    )
    parser.add_argument("--output-tag", required=True)
    parser.add_argument("--overwrite", action="store_true")
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


def safe_outputs(paths: list[Path], overwrite: bool) -> None:
    existing = [path for path in paths if path.exists()]
    if existing and not overwrite:
        raise RuntimeError("Gamma outputs exist; add --overwrite:\n" + "\n".join(map(str, existing)))
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)


def register_plot_font() -> None:
    for path in (
        Path("/System/Library/Fonts/Supplemental/Arial.ttf"),
        Path("/System/Library/Fonts/Helvetica.ttc"),
    ):
        if path.is_file():
            font_manager.fontManager.addfont(path)
            plt.rcParams["font.family"] = font_manager.FontProperties(fname=path).get_name()
            break
    plt.rcParams["axes.unicode_minus"] = False


def candidate_offsets(spacing_zyx: np.ndarray, dta_mm: float, fraction: int) -> np.ndarray:
    step = min(float(spacing_zyx.min()), dta_mm) / fraction
    count = int(np.ceil(dta_mm / step))
    axis = np.arange(-count, count + 1, dtype=float) * step
    zz, yy, xx = np.meshgrid(axis, axis, axis, indexing="ij")
    offsets = np.column_stack((zz.ravel(), yy.ravel(), xx.ravel()))
    distance = np.linalg.norm(offsets, axis=1)
    offsets = offsets[distance <= dta_mm + 1e-9]
    distance = distance[distance <= dta_mm + 1e-9]
    order = np.argsort(distance, kind="stable")
    return np.column_stack((offsets[order], distance[order]))


def gamma_3d(
    reference: np.ndarray,
    evaluation: np.ndarray,
    spacing_zyx: np.ndarray,
    mask: np.ndarray,
    dta_mm: float,
    dose_criterion: float,
    interpolation_fraction: int,
) -> tuple[np.ndarray, int, float]:
    selected = np.column_stack(np.where(mask)).astype(float)
    reference_values = reference[mask]
    gamma_squared = np.full(reference_values.shape, np.inf, dtype=np.float64)
    offsets = candidate_offsets(spacing_zyx, dta_mm, interpolation_fraction)
    started = time.perf_counter()
    for offset_z, offset_y, offset_x, distance_mm in offsets:
        coordinates = np.vstack(
            (
                selected[:, 0] + offset_z / spacing_zyx[0],
                selected[:, 1] + offset_y / spacing_zyx[1],
                selected[:, 2] + offset_x / spacing_zyx[2],
            )
        )
        evaluated = map_coordinates(
            evaluation,
            coordinates,
            order=1,
            mode="constant",
            cval=np.nan,
            prefilter=False,
        )
        candidate = (distance_mm / dta_mm) ** 2 + (
            (evaluated - reference_values) / dose_criterion
        ) ** 2
        gamma_squared = np.fmin(gamma_squared, candidate)
    gamma_values = np.sqrt(gamma_squared)
    gamma_map = np.full(reference.shape, np.nan, dtype=np.float32)
    gamma_map[mask] = gamma_values.astype(np.float32)
    return gamma_map, len(offsets), time.perf_counter() - started


def mid_index(mask: np.ndarray, axis: int) -> int:
    projected = mask.any(axis=tuple(item for item in range(3) if item != axis))
    indices = np.flatnonzero(projected)
    return int(round(float(indices[0] + indices[-1]) / 2.0))


def gamma_figure(
    gamma: np.ndarray,
    mask: np.ndarray,
    coords_xyz: tuple[np.ndarray, np.ndarray, np.ndarray],
    output: Path,
    title: str,
    pass_rate: float,
    threshold_percent: float,
) -> None:
    x, y, z = coords_xyz
    zi, yi, xi = mid_index(mask, 0), mid_index(mask, 1), mid_index(mask, 2)
    classification = np.full(gamma.shape, np.nan, dtype=np.float32)
    classification[np.isfinite(gamma) & (gamma <= 1.0)] = 0.0
    classification[np.isfinite(gamma) & (gamma > 1.0)] = 1.0
    views = (
        (classification[zi, :, :], [x.min(), x.max(), y.min(), y.max()], f"Axial (Z = {z[zi]:.1f} mm)", "Patient X (mm)", "Patient Y (mm)"),
        (classification[:, yi, :], [x.min(), x.max(), z.min(), z.max()], f"Coronal (Y = {y[yi]:.1f} mm)", "Patient X (mm)", "Patient Z (mm)"),
        (classification[:, :, xi], [y.min(), y.max(), z.min(), z.max()], f"Sagittal (X = {x[xi]:.1f} mm)", "Patient Y (mm)", "Patient Z (mm)"),
    )
    register_plot_font()
    cmap = ListedColormap(["#70AD47", "#C00000"])
    cmap.set_bad("#E7EAF0")
    norm = BoundaryNorm([-0.5, 0.5, 1.5], cmap.N)
    fig, axes = plt.subplots(1, 3, figsize=(16.0, 5.7), facecolor="white")
    image = None
    for axis, (values, extent, subtitle, xlabel, ylabel) in zip(axes, views):
        image = axis.imshow(
            np.ma.masked_invalid(values),
            origin="lower",
            extent=extent,
            cmap=cmap,
            norm=norm,
            interpolation="nearest",
            aspect="equal",
        )
        axis.set(title=subtitle, xlabel=xlabel, ylabel=ylabel)
        axis.grid(False)
    assert image is not None
    fig.legend(
        handles=[
            Patch(facecolor="#70AD47", label="Pass (γ ≤ 1)"),
            Patch(facecolor="#C00000", label="Fail (γ > 1)"),
            Patch(
                facecolor="#E7EAF0",
                label=f"Below {threshold_percent:g}% TPS threshold",
            ),
        ],
        loc="lower center",
        ncol=3,
        frameon=False,
        bbox_to_anchor=(0.5, 0.015),
    )
    fig.suptitle(f"{title}\n3D Gamma Pass Rate = {pass_rate:.2f}%", fontsize=18, color="#333333")
    fig.subplots_adjust(left=0.055, right=0.97, bottom=0.16, top=0.80, wspace=0.27)
    fig.savefig(output, dpi=180, facecolor="white")
    plt.close(fig)


def pass_fail_figure(passed: int, failed: int, output: Path, title: str, pass_rate: float) -> None:
    register_plot_font()
    fig, axis = plt.subplots(figsize=(10.5, 6.5), facecolor="white")
    counts = np.asarray([passed, failed], dtype=int)
    bars = axis.bar(["Pass (γ ≤ 1)", "Fail (γ > 1)"], counts, color=["#70AD47", "#C00000"], width=0.58)
    for bar, count in zip(bars, counts):
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{count:,}\n({count / counts.sum() * 100.0:.2f}%)",
            ha="center",
            va="bottom",
            fontsize=13,
        )
    axis.set(
        title=f"{title} — Pass/Fail Counts",
        ylabel="Evaluated voxel count",
    )
    axis.grid(True, axis="y", color="#D9D9D9", linewidth=1.0)
    axis.set_axisbelow(True)
    axis.margins(y=0.18)
    axis.text(
        0.98,
        0.95,
        f"Pass rate: {pass_rate:.2f}%",
        transform=axis.transAxes,
        ha="right",
        va="top",
        fontsize=14,
        color="#333333",
    )
    fig.tight_layout()
    fig.savefig(output, dpi=180, facecolor="white")
    plt.close(fig)


def main() -> int:
    args = parse_args()
    if not 0.0 < args.dta_mm <= 20.0:
        raise RuntimeError("--dta-mm must be within (0, 20]")
    if not 0.0 < args.dd_percent <= 100.0:
        raise RuntimeError("--dd-percent must be within (0, 100]")
    if not 0.0 <= args.low_dose_threshold_percent < 100.0:
        raise RuntimeError("--low-dose-threshold-percent must be within [0, 100)")
    if not 1 <= args.interpolation_fraction <= 10:
        raise RuntimeError("--interpolation-fraction must be between 1 and 10")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.output_tag):
        raise RuntimeError("--output-tag may contain only letters, digits, underscore and hyphen")

    root = args.root.expanduser().resolve()
    mc_path = args.mc_binary.expanduser().resolve()
    if not mc_path.is_file():
        raise RuntimeError(f"MC binary does not exist: {mc_path}")
    rppd_path = (args.tps_dose or discover_rppd(root)).expanduser().resolve()
    if not rppd_path.is_file():
        raise RuntimeError(f"Selected TPS RTDOSE does not exist: {rppd_path}")
    dose = pydicom.dcmread(rppd_path)
    if str(getattr(dose, "Modality", "")).upper() != "RTDOSE":
        raise RuntimeError(f"Selected TPS dose is not RTDOSE: {rppd_path}")
    dose_units = str(getattr(dose, "DoseUnits", "")).upper()
    if dose_units not in {"GY", "CGY"}:
        raise RuntimeError(f"Selected TPS RTDOSE must use GY or CGY, got {dose_units}")
    dose_type = str(getattr(dose, "DoseType", "")).upper() or "UNKNOWN"
    summation_type = str(getattr(dose, "DoseSummationType", "")).upper() or "UNKNOWN"
    reference = dose.pixel_array.astype(np.float64) * float(dose.DoseGridScaling)
    if dose_units == "CGY":
        reference /= 100.0
    evaluation = np.fromfile(mc_path, dtype=np.float64)
    if evaluation.size != reference.size:
        raise RuntimeError(f"MC/TPS grid size mismatch: {evaluation.size} versus {reference.size}")
    evaluation = evaluation.reshape(reference.shape)
    if not np.isfinite(evaluation).all() or float(evaluation.max()) <= 0.0:
        raise RuntimeError("MC binary is empty or contains non-finite values")

    reference_max = float(reference.max())
    if not np.isfinite(reference).all() or reference_max <= 0.0:
        raise RuntimeError("TPS RTDOSE is empty or contains non-finite values")
    evaluation_max = float(evaluation.max())
    calibration = require_particle_calibration(root, mc_path)
    scale = calibration.scale
    evaluation_scaled = evaluation * scale
    calibrated_max = float(evaluation_scaled.max())
    threshold_dose = reference_max * args.low_dose_threshold_percent / 100.0
    mask = reference >= threshold_dose
    if not np.any(mask):
        raise RuntimeError("Low-dose threshold leaves no TPS voxels for Gamma evaluation")
    dose_criterion = reference_max * args.dd_percent / 100.0
    high_dose_mask = reference >= 0.5 * reference_max
    high_dose_ratio_median = float(
        np.median(evaluation_scaled[high_dose_mask] / reference[high_dose_mask])
    )
    offsets = np.asarray(dose.GridFrameOffsetVector, dtype=float)
    if offsets.size < 2 or not np.all(np.diff(offsets) > 0.0):
        raise RuntimeError("TPS GridFrameOffsetVector must be strictly increasing")
    if not np.allclose(np.diff(offsets), np.diff(offsets)[0], rtol=0.0, atol=1e-6):
        raise RuntimeError("TPS dose grid must use regular frame spacing")
    if any(float(value) <= 0.0 for value in dose.PixelSpacing):
        raise RuntimeError("TPS PixelSpacing must be positive")
    spacing_zyx = np.asarray(
        [float(np.diff(offsets)[0]), float(dose.PixelSpacing[0]), float(dose.PixelSpacing[1])],
        dtype=float,
    )
    gamma, candidate_count, elapsed = gamma_3d(
        reference,
        evaluation_scaled,
        spacing_zyx,
        mask,
        args.dta_mm,
        dose_criterion,
        args.interpolation_fraction,
    )
    evaluated = gamma[np.isfinite(gamma)]
    passed = evaluated <= 1.0
    pass_rate = float(np.count_nonzero(passed) / evaluated.size * 100.0)

    analysis_dir = (args.analysis_dir or root / "analysis").expanduser().resolve()
    base = analysis_dir / "gamma"
    figure_dir = analysis_dir / "figures"
    summary_path = base / f"gamma_summary_{args.output_tag}.txt"
    metrics_path = base / f"gamma_metrics_{args.output_tag}.csv"
    volume_path = base / f"gamma_map_{args.output_tag}.npy"
    figure_path = figure_dir / f"gamma_map_{args.output_tag}.png"
    pass_fail_path = figure_dir / f"gamma_pass_fail_{args.output_tag}.png"
    outputs = [summary_path, metrics_path, volume_path, figure_path, pass_fail_path]
    safe_outputs(outputs, args.overwrite)
    np.save(volume_path, gamma, allow_pickle=False)

    ipp = np.asarray(dose.ImagePositionPatient, dtype=float)
    x = ipp[0] + np.arange(reference.shape[2]) * float(dose.PixelSpacing[1])
    y = ipp[1] + np.arange(reference.shape[1]) * float(dose.PixelSpacing[0])
    z = ipp[2] + offsets
    criteria = f"{args.dd_percent:g}% / {args.dta_mm:g} mm"
    gamma_figure(
        gamma,
        mask,
        (x, y, z),
        figure_path,
        f"Global 3D Gamma Analysis ({criteria})",
        pass_rate,
        args.low_dose_threshold_percent,
    )
    passed_count = int(np.count_nonzero(passed))
    failed_count = int(np.count_nonzero(~passed))
    pass_fail_figure(passed_count, failed_count, pass_fail_path, f"Global 3D Gamma ({criteria})", pass_rate)

    metrics = {
        "Output_tag": args.output_tag,
        "TPS_RPPD": str(rppd_path),
        "TPS_RTDose": str(rppd_path),
        "TPS_DoseType": dose_type,
        "TPS_DoseSummationType": summation_type,
        "TPS_DoseUnits": dose_units,
        "MC_binary": str(mc_path),
        "DD_percent_global": args.dd_percent,
        "DTA_mm": args.dta_mm,
        "Low_dose_threshold_percent_of_TPS_max": args.low_dose_threshold_percent,
        "Interpolation_fraction": args.interpolation_fraction,
        "Evaluated_voxels": int(evaluated.size),
        "Passed_voxels_gamma_le_1": passed_count,
        "Failed_voxels_gamma_gt_1": failed_count,
        "Gamma_pass_rate_percent": pass_rate,
        "TPS_max_Gy": reference_max,
        "MC_TOPAS_per_run_max_Gy": evaluation_max,
        "MC_particle_calibrated_max_Gy": calibrated_max,
        "MC_normalization_protocol": calibration.protocol,
        "MC_planned_particles_N_plan": calibration.planned_particles,
        "MC_simulated_histories_N_sim": calibration.simulated_histories,
        "MC_particle_scale_N_plan_over_N_sim": scale,
        "MC_allocation_file": calibration.allocation_file,
        "MC_allocation_L1_fraction": calibration.allocation_l1_fraction,
        "MC_preliminary_low_statistics": calibration.preliminary_low_statistics,
        "MC_to_TPS_median_ratio_TPS_ge_50_percent": high_dose_ratio_median,
        "Candidate_offsets": candidate_count,
        "Calculation_seconds": elapsed,
    }
    pd.DataFrame([metrics]).to_csv(metrics_path, index=False, float_format="%.10g")
    summary = f"""TPS-TOPAS global 3D Gamma analysis
====================================
Status: COMPLETED — RESEARCH/QA USE ONLY

Result
------
Gamma pass rate: {pass_rate:.4f}%
Passed / evaluated voxels: {passed_count:,} / {evaluated.size:,}
Failed voxels: {failed_count:,}

Criteria and protocol
---------------------
Dose difference: {args.dd_percent:g}% of global TPS maximum
Distance to agreement: {args.dta_mm:g} mm
Passing condition: Gamma <= 1.0
Low-dose threshold: TPS >= {args.low_dose_threshold_percent:g}% of global TPS maximum
Reference: selected TPS {summation_type}/{dose_type} RTDOSE
Evaluation: TOPAS DoseToMedium, independently particle-number calibrated
Normalization: N_plan / N_sim = {calibration.planned_particles:.10g} / {calibration.simulated_histories:,} = {scale:.10g}
TPS dose values are not used to determine this MC scale
Interpolation: trilinear MC interpolation
Search step: min(voxel spacing, DTA) / {args.interpolation_fraction}
Candidate spatial offsets: {candidate_count}
Search scope: DTA-radius sphere, sufficient for the Gamma <= 1 pass/fail decision

Inputs
------
TPS RTDOSE: {rppd_path}
TPS DoseType / DoseSummationType / DoseUnits: {dose_type} / {summation_type} / {dose_units}
MC binary: {mc_path}
Grid shape [Z,Y,X]: {reference.shape}
Grid spacing [Z,Y,X]: {spacing_zyx.tolist()} mm
TPS / TOPAS-per-run / calibrated MC maximum: {reference_max:.10g} / {evaluation_max:.10g} / {calibrated_max:.10g} Gy
Median calibrated MC/TPS ratio where TPS >= 50% maximum: {high_dose_ratio_median:.10g}
Allocation snapshot: {calibration.allocation_file}
Planned particles / simulated histories: {calibration.planned_particles:.10g} / {calibration.simulated_histories:,}
Spot-allocation L1 difference: {calibration.allocation_l1_fraction:.4%}
Calculation time: {elapsed:.3f} s

Important limitations
---------------------
The MC scale is independent of TPS dose and follows the commissioned
number-per-MU particle model. No TPS-peak fit and no empirical 0.976 correction
is applied. This removes the former normalization circularity, but it remains a
research physical-dose estimate rather than a clinical acceptance result.
This run is flagged as {'LOW STATISTICS: individual-spot allocation noise is material' if calibration.preliminary_low_statistics else 'adequate by the coarse histories-per-spot flag'}.
The global MC maximum may be dominated by a single low-history voxel; use the
high-dose-region ratio, profiles, uncertainty and Gamma map rather than the raw
volume maximum alone.
Gamma results also depend strongly on DD, DTA, low-dose threshold, normalization,
interpolation and evaluated volume. Those conditions must accompany the pass rate.
The stored map contains the sampled minimum Gamma within the DTA sphere. Values
<= 1 support the pass decision; failed-voxel values are not a full unbounded Gamma
distribution and are used only for pass/fail classification.

Outputs
-------
Metrics CSV: {metrics_path}
Gamma volume (.npy, float32 [Z,Y,X], NaN outside threshold): {volume_path}
Gamma map figure: {figure_path}
Gamma pass/fail counts: {pass_fail_path}
"""
    summary_path.write_text(summary, encoding="utf-8")
    print(f"Gamma pass rate: {pass_rate:.4f}%")
    print(f"Evaluated voxels: {evaluated.size:,}; criteria: {criteria}; threshold: {args.low_dose_threshold_percent:g}%")
    print(f"Wrote: {summary_path}")
    print(f"Wrote: {metrics_path}")
    print(f"Wrote: {figure_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
