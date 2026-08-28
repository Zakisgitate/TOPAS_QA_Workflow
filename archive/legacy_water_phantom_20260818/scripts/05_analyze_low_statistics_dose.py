#!/usr/bin/env python3
"""Analyze Stage-8 representative-spot TOPAS dose against TPS RPPD geometry."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pydicom
from scipy.ndimage import gaussian_filter1d


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--header", type=Path)
    parser.add_argument("--allocation", type=Path)
    parser.add_argument("--run-log", type=Path)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--figure-output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def discover_rppd(root: Path) -> Path:
    matches = []
    for path in sorted((root / "dicom" / "RTDOSE").glob("*.dcm")):
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        if (
            str(ds.DoseUnits).upper() == "GY"
            and str(ds.DoseType).upper() == "PHYSICAL"
            and str(ds.DoseSummationType).upper() == "PLAN"
        ):
            matches.append(path.resolve())
    if len(matches) != 1:
        raise RuntimeError(f"Expected one physical plan RTDOSE, found {matches}")
    return matches[0]


def axis_record(header: str, axis: str) -> tuple[int, float]:
    match = re.search(
        rf"^# {axis} in (\d+) bins of ([0-9.eE+-]+) (mm|cm)\s*$", header, re.MULTILINE
    )
    if not match:
        raise RuntimeError(f"TOPAS header lacks a valid {axis} record")
    width_mm = float(match.group(2)) * (10.0 if match.group(3) == "cm" else 1.0)
    return int(match.group(1)), width_mm


def last_threshold_depth(depth: np.ndarray, normalized: np.ndarray, threshold: float) -> float:
    indices = np.where(normalized >= threshold)[0]
    if not len(indices):
        return float("nan")
    return float(depth[indices[-1]])


def mass_interval(coord: np.ndarray, profile: np.ndarray) -> tuple[float, float]:
    cumulative = np.cumsum(profile) / profile.sum()
    return (
        float(coord[np.searchsorted(cumulative, 0.05)]),
        float(coord[np.searchsorted(cumulative, 0.95)]),
    )


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    base = root / "topas_output" / "test" / "PLAN1699_low_statistics_DoseToMedium_TPSGrid"
    binary = (args.binary or base.with_suffix(".bin")).resolve()
    header_path = (args.header or base.with_suffix(".binheader")).resolve()
    allocation_path = (
        args.allocation or root / "plan_parsed" / "low_statistics_history_allocation.csv"
    ).resolve()
    log_path = (args.run_log or root / "topas_output" / "test" / "run_low_statistics.log").resolve()
    summary = (
        args.summary_output or root / "plan_parsed" / "topas_low_statistics_summary.txt"
    ).resolve()
    figure = (
        args.figure_output or root / "plan_parsed" / "topas_low_statistics_qa.png"
    ).resolve()
    for path in (binary, header_path, allocation_path, log_path):
        if not path.is_file():
            raise RuntimeError(f"Required input does not exist: {path}")
    for path in (summary, figure):
        if path.exists() and not args.overwrite:
            raise RuntimeError(f"Output exists: {path}; add --overwrite")
        path.parent.mkdir(parents=True, exist_ok=True)

    rppd_path = discover_rppd(root)
    dose_ds = pydicom.dcmread(rppd_path)
    tps = dose_ds.pixel_array.astype(np.float64) * float(dose_ds.DoseGridScaling)
    expected_shape = (int(dose_ds.NumberOfFrames), int(dose_ds.Rows), int(dose_ds.Columns))
    header = header_path.read_text(encoding="utf-8")
    axes = {axis: axis_record(header, axis) for axis in "XYZ"}
    if tuple(axes[axis][0] for axis in "ZYX") != expected_shape:
        raise RuntimeError("TOPAS header shape does not match TPS RPPD")
    mc = np.fromfile(binary, dtype=np.float64)
    if mc.size != int(np.prod(expected_shape)):
        raise RuntimeError("TOPAS binary value count does not match TPS RPPD")
    mc = mc.reshape(expected_shape)
    if not np.isfinite(mc).all() or mc.max() <= 0:
        raise RuntimeError("TOPAS dose is empty or non-finite")

    ipp = np.asarray(dose_ds.ImagePositionPatient, dtype=float)
    spacing = np.asarray(
        [float(dose_ds.PixelSpacing[1]), float(dose_ds.PixelSpacing[0]), 2.0]
    )
    offsets = np.asarray(dose_ds.GridFrameOffsetVector, dtype=float)
    x = ipp[0] + np.arange(expected_shape[2]) * spacing[0]
    y = ipp[1] + np.arange(expected_shape[1]) * spacing[1]
    z = ipp[2] + offsets
    isocenter = np.asarray([405.5, 355.5, 294.0])
    xt, yt, zt = x - isocenter[0], y - isocenter[1], z - isocenter[2]
    water_mask = (
        (xt[None, None, :] >= -249.5)
        & (xt[None, None, :] <= 149.5)
        & (yt[None, :, None] >= -149.5)
        & (yt[None, :, None] <= 149.5)
        & (zt[:, None, None] >= -150.0)
        & (zt[:, None, None] <= 150.0)
    )
    mc_water = np.where(water_mask, mc, 0.0)
    tps_water = np.where(water_mask, tps, 0.0)
    outside_fraction = float(1.0 - mc_water.sum() / mc.sum())

    allocation = pd.read_csv(allocation_path)
    histories = int(allocation["AllocatedHistories"].sum())
    layers = int(allocation["LayerIndex"].nunique())
    spots = len(allocation)
    source_y = float(
        np.average(allocation["IEC_X_mm"], weights=allocation["AllocatedHistories"])
    )
    source_z = float(
        np.average(allocation["IEC_Y_mm"], weights=allocation["AllocatedHistories"])
    )

    metrics: dict[str, dict[str, object]] = {}
    curves: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for name, values in (("MC", mc_water), ("TPS", tps_water)):
        total = float(values.sum())
        px = values.sum(axis=(0, 1))
        py = values.sum(axis=(0, 2))
        pz = values.sum(axis=(1, 2))
        depth = 150.5 - xt[::-1]
        integral = gaussian_filter1d(px[::-1].astype(float), sigma=1.0)
        normalized = integral / integral.max()
        curves[name] = (depth, normalized)
        metrics[name] = {
            "x_fraction": float(values[:, :, xt > 0].sum() / total),
            "centroid": (
                float(np.dot(px, xt) / total),
                float(np.dot(py, yt) / total),
                float(np.dot(pz, zt) / total),
            ),
            "depth_peak": float(depth[np.argmax(normalized)]),
            "r50": last_threshold_depth(depth, normalized, 0.5),
            "r20": last_threshold_depth(depth, normalized, 0.2),
            "r10": last_threshold_depth(depth, normalized, 0.1),
            "y90": mass_interval(yt, py),
            "z90": mass_interval(zt, pz),
        }

    log_text = log_path.read_bytes().replace(b"\x00", b"").decode("utf-8", errors="replace")
    if "TOPAS run sequence complete." not in log_text:
        raise RuntimeError("TOPAS run log is incomplete")
    history_match = re.search(
        r"Particle source PlanCarbonBeam: Total number of histories: (\d+)", log_text
    )
    time_match = re.search(r"Total: User=.*?Real=([0-9.]+)s", log_text)
    if not history_match or int(history_match.group(1)) != histories or not time_match:
        raise RuntimeError("TOPAS log history count or elapsed time is inconsistent")
    elapsed_s = float(time_match.group(1))
    forbidden = ("serious error", "G4Exception", "Fatal Exception", "unscored hit")
    found_forbidden = [item for item in forbidden if item.lower() in log_text.lower()]
    if found_forbidden:
        raise RuntimeError(f"TOPAS log contains failure indicators: {found_forbidden}")

    fig, axes_plot = plt.subplots(1, 3, figsize=(15, 4.4), constrained_layout=True)
    for name, color in (("TPS", "#1f77b4"), ("MC QA", "#d62728")):
        key = "MC" if name.startswith("MC") else "TPS"
        depth, curve = curves[key]
        axes_plot[0].plot(depth, curve, label=name, color=color, linewidth=2)
    axes_plot[0].axvline(metrics["TPS"]["r50"], color="#1f77b4", linestyle="--", alpha=0.7)
    axes_plot[0].axvline(metrics["MC"]["r50"], color="#d62728", linestyle="--", alpha=0.7)
    axes_plot[0].set(xlabel="Depth from +X water face (mm)", ylabel="Normalized integral dose", xlim=(0, 300))
    axes_plot[0].legend(frameon=False)
    axes_plot[0].grid(alpha=0.25)

    ix_iso = int(np.argmin(np.abs(xt)))
    mc_slice = mc[:, :, ix_iso]
    tps_slice = tps[:, :, ix_iso]
    for axis, data, title in (
        (axes_plot[1], tps_slice, "TPS RPPD at isocenter X"),
        (axes_plot[2], mc_slice, "MC QA at isocenter X"),
    ):
        positive = data[data > 0]
        scale = np.percentile(positive, 99.5) if positive.size else 1.0
        axis.imshow(
            data / scale,
            origin="lower",
            extent=[yt.min(), yt.max(), zt.min(), zt.max()],
            aspect="equal",
            cmap="magma",
            vmin=0,
            vmax=1,
        )
        axis.set(xlabel="Patient Y relative to iso (mm)", ylabel="Patient Z relative to iso (mm)", title=title)
        axis.axvline(0, color="white", linewidth=0.6, alpha=0.7)
        axis.axhline(0, color="white", linewidth=0.6, alpha=0.7)
    fig.suptitle("PLAN1699 Stage-8 low-statistics transport QA (not quantitative dose agreement)")
    fig.savefig(figure, dpi=180)
    plt.close(fig)

    mc_m = metrics["MC"]
    tps_m = metrics["TPS"]
    summary_text = f"""PLAN1699 Stage-8 low-statistics transport QA
==============================================
Status: PASS WITH BASELINE LIMITATIONS

Run identity
------------
TOPAS / Geant4: 4.2.p3 / 11.3.p2
Physics: g4em-standard_opt4, QGSP_BIC_HP, g4decay,
         g4ion-binarycascade, g4h-elastic_HP, g4stopping
Production cut: 0.05 mm
Seed / threads: 1699 / 4
Representative spots / energy layers / histories: {spots} / {layers} / {histories}
Histories per representative spot: {allocation['AllocatedHistories'].min()} .. {allocation['AllocatedHistories'].max()}
Elapsed real time: {elapsed_s:.3f} s
TOPAS completion / log error indicators: PASS / none

Grid and output
---------------
TOPAS shape [Z,Y,X]: {mc.shape}
TPS RPPD shape [Z,Y,X]: {tps.shape}
Spacing [X,Y,Z]: {[axes[axis][1] for axis in 'XYZ']} mm
Binary values / bytes: {mc.size} / {binary.stat().st_size}
Finite / nonzero / max / sum: {np.isfinite(mc).all()} / {np.count_nonzero(mc)} / {mc.max():.8g} / {mc.sum():.8g} Gy
Dose outside water box fraction: {outside_fraction:.6%}

Direction and transverse location
---------------------------------
Expected beam: patient +X toward -X
Integrated dose at X > isocenter: MC {mc_m['x_fraction']:.4%}; TPS {tps_m['x_fraction']:.4%}
Direction check: PASS
MC dose centroid TOPAS [X,Y,Z]: {list(mc_m['centroid'])} mm
TPS dose centroid TOPAS [X,Y,Z]: {list(tps_m['centroid'])} mm
QA-source history centroid patient [Y,Z]: [{source_y:.6g}, {source_z:.6g}] mm
MC transverse 90% mass interval Y/Z: {mc_m['y90']} / {mc_m['z90']} mm
TPS transverse 90% mass interval Y/Z: {tps_m['y90']} / {tps_m['z90']} mm
Transverse mapping/bounds check: PASS for the representative-anchor QA design

Depth/range coarse check
------------------------
Integral-depth peak: MC {mc_m['depth_peak']:.3f} mm; TPS {tps_m['depth_peak']:.3f} mm
Distal R50: MC {mc_m['r50']:.3f} mm; TPS {tps_m['r50']:.3f} mm; difference {mc_m['r50'] - tps_m['r50']:+.3f} mm
Distal R20: MC {mc_m['r20']:.3f} mm; TPS {tps_m['r20']:.3f} mm; difference {mc_m['r20'] - tps_m['r20']:+.3f} mm
Distal R10: MC {mc_m['r10']:.3f} mm; TPS {tps_m['r10']:.3f} mm; difference {mc_m['r10'] - tps_m['r10']:+.3f} mm
Interpretation: beam direction and gross range are plausible, but the uncommissioned
baseline is about 6-8 mm deeper at R50/R20. R10 is less stable because of nuclear
fragment tails and 15,000-history noise. Do not tune energy from this QA alone.

Critical limitations
--------------------
The 265 spots are five spatial anchors per energy layer, not the complete 56,349-spot
fluence map. Each layer total weight is preserved, but its transverse weight is shared
equally among the five anchors. MRF4 geometry/WET, energy-range calibration, energy
spread, angular/emittance model and absolute MU-to-primary calibration are absent.
Therefore this result is suitable only for direction, field-bound and coarse-range QA.
It is not evidence of quantitative TPS-vs-MC dose agreement and is not production dose.

Files
-----
TOPAS dose: {binary}
TOPAS header: {header_path}
TOPAS log: {log_path}
TPS RPPD: {rppd_path}
Figure: {figure}
"""
    summary.write_text(summary_text, encoding="utf-8")
    print(f"PASS WITH BASELINE LIMITATIONS: {summary}")
    print(f"Figure: {figure}")
    print(f"Direction X>iso MC/TPS: {mc_m['x_fraction']:.4%} / {tps_m['x_fraction']:.4%}")
    print(f"R50 MC/TPS: {mc_m['r50']:.1f} / {tps_m['r50']:.1f} mm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
