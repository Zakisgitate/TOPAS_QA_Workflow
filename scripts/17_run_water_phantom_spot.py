#!/usr/bin/env python3
"""Run and analyse a commissioned TOPAS water-phantom single spot.

This is an intentionally separate command-line program.  It leaves the GUI and
the existing planning scripts untouched, invokes the existing water-phantom
input generator, runs one TOPAS transport job (no DICOM/TPS plan), and exports
one-dimensional IDD/PDD and transverse profiles together with range/width
metrics.  All distances in the exported CSV files are millimetres.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
GENERATOR_PATH = Path(__file__).with_name("16_generate_water_phantom_spot.py")

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.utils.water_phantom import (
    bin_centers,
    compare_depth_curves,
    depth_curve_metrics,
    parse_measured_idd,
    profile_metrics,
    read_topas_1d,
)


def _load_generator():
    spec = importlib.util.spec_from_file_location("water_phantom_generator", GENERATOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load generator: {GENERATOR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--energy-mevu", type=float, required=True, help="Commissioned carbon energy (MeV/u)")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--histories", type=int, default=20_000)
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--seed", type=int, default=1699)
    parser.add_argument("--depth-step-mm", type=float, default=0.5)
    parser.add_argument("--letd-step-mm", type=float, help="LETd depth step; default follows --depth-step-mm")
    parser.add_argument("--idd-radius-mm", type=float, help="IDD and LETd scoring-cylinder radius")
    parser.add_argument("--phantom-depth-mm", type=float, help="Uniform-water phantom depth")
    parser.add_argument("--lateral-step-mm", type=float, default=0.5)
    parser.add_argument("--profile-depths-mm", help="Comma-separated profile depths; default is derived from measured IDD")
    parser.add_argument("--spot-x-mm", type=float, default=0.0)
    parser.add_argument("--spot-y-mm", type=float, default=0.0)
    parser.add_argument("--meterset-mu", type=float, help="Optional MU for the audited N_plan/N_sim absolute scale")
    parser.add_argument("--output-tag", help="Output tag (default wp_E<energy>_<histories>)")
    parser.add_argument("--topas", type=Path, help="TOPAS executable or wrapper (default /Users/.../bin/topas or PATH)")
    parser.add_argument("--beam-model-profile", type=Path, help="Commissioned profile.json override passed to the water-phantom generator")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--analysis-only",
        action="store_true",
        help=(
            "Re-export curves, metrics and figures from an already simulated run. "
            "Neither the TOPAS input decks nor the transport are regenerated, so the "
            "dose binaries are read exactly as the previous transport wrote them."
        ),
    )
    return parser.parse_args()


def _write_curve(path: Path, coordinate: np.ndarray, dose: np.ndarray, scale: float) -> dict[str, Any]:
    calibrated = dose * scale
    peak = float(calibrated.max())
    relative = calibrated / peak if peak > 0 else np.zeros_like(calibrated)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["position_mm", "dose_Gy_per_run", "dose_Gy_calibrated", "relative_to_max"])
        writer.writerows(
            [f"{x:.8g}", f"{raw:.12g}", f"{cal:.12g}", f"{rel:.12g}"]
            for x, raw, cal, rel in zip(coordinate, dose, calibrated, relative)
        )
    return {"peak_dose_Gy_calibrated": peak, "metrics": depth_curve_metrics(coordinate, calibrated)}


def _write_letd_curve(path: Path, coordinate: np.ndarray, letd: np.ndarray) -> dict[str, Any]:
    peak_index = int(np.nanargmax(letd)) if letd.size else 0
    peak = float(letd[peak_index]) if letd.size else 0.0
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(["depth_mm", "letd_keV_per_um"])
        writer.writerows(
            [f"{x:.8g}", f"{value:.12g}"] for x, value in zip(coordinate, letd)
        )
    return {
        "peak_letd_keV_per_um": peak,
        "peak_depth_mm": float(coordinate[peak_index]) if coordinate.size else None,
        "mean_letd_keV_per_um": float(np.nanmean(letd)) if letd.size else 0.0,
    }


def _write_carbon_spectrum(
    path: Path,
    nominal_mevu: float,
    total_energy_mev: list[float],
    weights: list[float],
) -> dict[str, Any]:
    """Export the commissioned discrete carbon spectrum used by the source."""
    if len(total_energy_mev) != len(weights) or not total_energy_mev:
        raise ValueError("Carbon spectrum energies and weights must be non-empty and have equal length")
    mass_number = 12.0
    weight_sum = float(np.sum(weights))
    rows = []
    for index, (total, weight) in enumerate(zip(total_energy_mev, weights), start=1):
        rows.append(
            [
                index,
                f"{float(total):.12g}",
                f"{float(total) / mass_number:.12g}",
                f"{float(weight):.12g}",
            ]
        )
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(
            [
                "component",
                "total_energy_MeV",
                "energy_per_nucleon_MeV_u",
                "relative_weight",
            ]
        )
        writer.writerows(rows)
    return {
        "nominal_energy_mevu": float(nominal_mevu),
        "mass_number": int(mass_number),
        "components": len(rows),
        "weight_sum": weight_sum,
        "weighted_mean_total_mev": float(np.dot(total_energy_mev, weights) / weight_sum),
        "weighted_mean_mevu": float(np.dot(total_energy_mev, weights) / weight_sum / mass_number),
        "csv": str(path),
    }


def _plot_carbon_spectrum(
    path: Path,
    nominal_mevu: float,
    total_energy_mev: list[float],
    weights: list[float],
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    mass_number = 12.0
    energy_mevu = np.asarray(total_energy_mev, dtype=float) / mass_number
    values = np.asarray(weights, dtype=float)
    order = np.argsort(energy_mevu)
    energy_mevu = energy_mevu[order]
    values = values[order]
    fig, ax = plt.subplots(figsize=(9.0, 5.5))
    markerline, stemlines, baseline = ax.stem(energy_mevu, values, linefmt="#4472C4", markerfmt="o", basefmt=" ")
    plt.setp(stemlines, linewidth=1.5)
    plt.setp(markerline, markersize=5)
    for x, y in zip(energy_mevu, values):
        ax.annotate(f"{y:.3f}", (x, y), xytext=(0, 6), textcoords="offset points", ha="center", fontsize=8)
    ax.axvline(float(nominal_mevu), color="#C0504D", linestyle="--", linewidth=1.2, label=f"Nominal {nominal_mevu:g} MeV/u")
    ax.set(
        xlabel="Carbon-ion energy (MeV/u)",
        ylabel="Relative spectral weight",
        title="Commissioned discrete carbon-ion energy spectrum",
    )
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper right")
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _topas_command(args: argparse.Namespace) -> list[str]:
    if args.topas:
        return [str(args.topas.expanduser().resolve())]
    configured = os.environ.get("TOPAS_EXECUTABLE", "").strip()
    if configured:
        return [configured]
    found = shutil.which("topas")
    if found:
        return [found]
    wrapper = Path("/Users/jiangzhenmin/bin/topas")
    if wrapper.is_file():
        return [str(wrapper)]
    raise RuntimeError("TOPAS executable not found; pass --topas /path/to/topas")


def _plot_idd(
    path: Path,
    idd: tuple[np.ndarray, np.ndarray],
    measured: Any | None = None,
    diameter_mm: float | None = None,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    fig, ax = plt.subplots(figsize=(9.0, 5.5))
    depth, dose = idd
    dose_relative = dose / max(float(dose.max()), 1e-30)
    step_mm = float(np.median(np.diff(depth))) if depth.size > 1 else 0.0
    geometry_label = f", {diameter_mm:g} mm diameter" if diameter_mm is not None else ""
    ax.plot(
        depth, dose_relative,
        label=f"TOPAS IDD ({step_mm:g} mm spacing{geometry_label})",
        lw=1.5, color="#4472C4",
    )
    topas_r80 = depth_curve_metrics(depth, dose)["R80_mm"]
    annotation = f"TOPAS IDD R80 = {topas_r80:.2f} mm"
    if measured is not None:
        measured_relative = measured.dose_au / max(float(measured.dose_au.max()), 1e-30)
        ax.scatter(
            measured.depth_mm, measured_relative, s=9, color="#C0504D",
            edgecolors="none", label="Measured IDD (actual positions)", zorder=3,
        )
        measured_r80 = depth_curve_metrics(measured.depth_mm, measured.dose_au)["R80_mm"]
        annotation = f"measured R80 = {measured_r80:.2f} mm\n{annotation}"
    ax.text(
        0.985, 0.96, annotation, transform=ax.transAxes,
        ha="right", va="top", fontsize=9,
        bbox={"boxstyle": "square,pad=0.35", "facecolor": "white", "edgecolor": "#B7B7B7", "alpha": 0.9},
    )
    ax.set(xlabel="Water depth (mm)", ylabel="IDD relative to maximum", ylim=(0, 1.4))
    ax.grid(True, alpha=0.3)
    ax.legend(loc="upper left", framealpha=0.5)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _plot_letd(
    path: Path,
    idd: tuple[np.ndarray, np.ndarray],
    letd: tuple[np.ndarray, np.ndarray],
    diameter_mm: float | None = None,
) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    fig, letd_ax = plt.subplots(figsize=(9.0, 5.5))
    depth, dose = idd
    letd_depth, letd_values = letd
    letd_step_mm = float(np.median(np.diff(letd_depth))) if letd_depth.size > 1 else 0.0
    geometry_label = f", {diameter_mm:g} mm diameter" if diameter_mm is not None else ""
    dose_relative = dose / max(float(dose.max()), 1e-30)
    dose_relative_at_letd = np.interp(letd_depth, depth, dose_relative)
    visible_letd = np.where(dose_relative_at_letd >= 0.01, letd_values, np.nan)
    letd_ax.plot(
        letd_depth, visible_letd, color="#70AD47", lw=1.3,
        label=f"TOPAS dose-weighted LETd ({letd_step_mm:g} mm spacing{geometry_label}; IDD >= 1%)",
    )
    peak_index = int(np.nanargmax(visible_letd))
    letd_ax.scatter(
        [letd_depth[peak_index]], [visible_letd[peak_index]],
        s=28, color="#70AD47", zorder=3,
    )
    letd_ax.annotate(
        f"peak = {visible_letd[peak_index]:.2f} keV/um\nat {letd_depth[peak_index]:.2f} mm",
        xy=(letd_depth[peak_index], visible_letd[peak_index]),
        xytext=(12, -12), textcoords="offset points", fontsize=9,
        ha="left", va="top", linespacing=1.15, annotation_clip=True,
        arrowprops={"arrowstyle": "->", "color": "#70AD47"},
    )
    letd_ax.set(xlabel="Water depth (mm)", ylabel="LETd (keV/um)", ylim=(0, float(np.nanmax(visible_letd)) * 1.3))
    letd_ax.grid(True, alpha=0.3)
    letd_ax.legend(loc="upper left", framealpha=0.5)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def _plot_profiles(path: Path, profiles: list[tuple[float, str, np.ndarray, np.ndarray]]) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return
    fig, ax = plt.subplots(figsize=(8.5, 5.5))
    for depth, axis, position, dose in profiles:
        ax.plot(position, dose / max(float(dose.max()), 1e-30), label=f"{axis}, {depth:g} mm")
    ax.set(xlabel="Lateral position (mm)", ylabel="Relative dose", ylim=(0, 1.1))
    ax.grid(True, alpha=0.3)
    ax.legend(ncol=2, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=160)
    plt.close(fig)


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    generator = _load_generator()

    if args.analysis_only:
        if args.overwrite:
            raise RuntimeError("--analysis-only never regenerates inputs, so --overwrite is meaningless")
    else:
        generator_args = [
            sys.executable, str(GENERATOR_PATH), "--root", str(root),
            "--energy-mevu", str(args.energy_mevu), "--histories", str(args.histories),
            "--threads", str(args.threads), "--seed", str(args.seed),
            "--depth-step-mm", str(args.depth_step_mm), "--lateral-step-mm", str(args.lateral_step_mm),
            "--spot-x-mm", str(args.spot_x_mm), "--spot-y-mm", str(args.spot_y_mm),
        ]
        if args.beam_model_profile is not None:
            generator_args += ["--beam-model-profile", str(args.beam_model_profile.expanduser().resolve())]
        if args.letd_step_mm is not None:
            generator_args += ["--letd-step-mm", str(args.letd_step_mm)]
        if args.idd_radius_mm is not None:
            generator_args += ["--idd-radius-mm", str(args.idd_radius_mm)]
        if args.phantom_depth_mm is not None:
            generator_args += ["--phantom-depth-mm", str(args.phantom_depth_mm)]
        if args.overwrite:
            generator_args.append("--overwrite")
        if args.profile_depths_mm:
            generator_args += ["--profile-depths-mm", args.profile_depths_mm]
        if args.meterset_mu is not None:
            generator_args += ["--meterset-mu", str(args.meterset_mu)]
        if args.output_tag:
            generator_args += ["--output-tag", args.output_tag]
        generated = subprocess.run(generator_args, cwd=root, text=True, capture_output=True)
        if generated.returncode:
            raise RuntimeError(f"Water-phantom input generation failed:\n{generated.stdout}\n{generated.stderr}")

    tag = args.output_tag or f"wp_E{args.energy_mevu:g}_{int(args.histories)}"
    tag = generator.slug(tag, "water_phantom_run")
    setup_candidates = sorted((root / "analysis" / "_water_phantom").glob(f"*/{tag}/setup/water_phantom_spot_setup.json"))
    if len(setup_candidates) != 1:
        raise RuntimeError(f"Could not uniquely locate generated setup for tag {tag!r}: {setup_candidates}")
    setup_path = setup_candidates[0]
    cache = setup_path.parents[1]
    setup = json.loads(setup_path.read_text(encoding="utf-8"))
    dose_dir = Path(setup["run"]["dose_directory"])
    entry = Path(setup["run"]["entry_point"])
    log_dir = cache / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if args.analysis_only:
        command = None
        missing = [
            name for name in ["idd.bin", "pdd.bin"]
            + ["letd.bin"]
            + [f"{profile['output']}.bin" for profile in setup["scoring"]["profiles"]]
            if not (dose_dir / name).is_file() or (dose_dir / name).stat().st_size == 0
        ]
        if missing:
            raise RuntimeError(
                f"--analysis-only found no usable transport output for tag {tag!r}; "
                f"missing or empty: {', '.join(missing)} in {dose_dir}"
            )
    else:
        command = _topas_command(args) + [entry.name]
        run = subprocess.run(command, cwd=entry.parent, text=True, capture_output=True, env=os.environ.copy())
        (log_dir / "topas.stdout.log").write_text(run.stdout, encoding="utf-8")
        (log_dir / "topas.stderr.log").write_text(run.stderr, encoding="utf-8")
        if run.returncode:
            raise RuntimeError(f"TOPAS failed with exit code {run.returncode}; see {log_dir}")

    curves_dir = cache / "curves"
    metrics_dir = cache / "metrics"
    figures_dir = cache / "figures"
    curves_dir.mkdir(exist_ok=True)
    metrics_dir.mkdir(exist_ok=True)
    figures_dir.mkdir(exist_ok=True)
    scoring = setup["scoring"]
    geometry = setup["geometry"]
    scale = 1.0
    if setup["run"].get("planned_particles") is not None:
        scale = float(setup["run"]["planned_particles"]) / float(setup["run"]["histories"])
    depth_bins = int(scoring["idd"]["bins"])
    depth_step = float(scoring["idd"]["step_mm"])
    depth = bin_centers(depth_bins, depth_step, 0.0)
    letd_bins = int(scoring["letd"]["bins"])
    letd_step = float(scoring["letd"]["step_mm"])
    letd_depth = bin_centers(letd_bins, letd_step, 0.0)
    idd = read_topas_1d(dose_dir / "idd.bin", depth_bins)
    pdd = read_topas_1d(dose_dir / "pdd.bin", depth_bins)
    letd = read_topas_1d(dose_dir / "letd.bin", letd_bins)
    idd_result = _write_curve(curves_dir / "idd.csv", depth, idd, scale)
    pdd_result = _write_curve(curves_dir / "pdd.csv", depth, pdd, scale)
    letd_result = _write_letd_curve(curves_dir / "letd.csv", letd_depth, letd)
    spectrum_total_mev = [float(value) for value in setup["beam"]["spectrum_total_mev"]]
    spectrum_weights = [float(value) for value in setup["beam"]["spectrum_weights"]]
    spectrum_result = _write_carbon_spectrum(
        curves_dir / "carbon_energy_spectrum.csv",
        float(args.energy_mevu),
        spectrum_total_mev,
        spectrum_weights,
    )
    measured = None
    matched_idd_path = None
    reference = setup.get("measured_reference", {})
    if reference.get("available"):
        ref_path = Path(setup["beam"]["measured_idd_path"])
        measured = next(
            curve for curve in parse_measured_idd(ref_path)
            if abs(curve.nominal_mevu - float(args.energy_mevu)) <= 0.02
        )
        matched_topas = np.interp(measured.depth_mm, depth, idd * scale)
        matched_idd_path = curves_dir / "idd_at_measured_depths.csv"
        with matched_idd_path.open("w", newline="", encoding="utf-8") as stream:
            writer = csv.writer(stream)
            writer.writerow([
                "depth_mm", "measured_relative", "topas_idd_interpolated_Gy",
                "topas_idd_relative", "difference_percent",
            ])
            topas_relative = matched_topas / max(float((idd * scale).max()), 1e-30)
            measured_relative = measured.dose_au / max(float(measured.dose_au.max()), 1e-30)
            writer.writerows(
                [
                    f"{x:.8g}", f"{ref:.12g}", f"{mc:.12g}",
                    f"{mc_rel:.12g}", f"{(mc_rel - ref) * 100.0:.8g}",
                ]
                for x, ref, mc, mc_rel in zip(
                    measured.depth_mm, measured_relative, matched_topas, topas_relative
                )
            )

    profile_results: dict[str, Any] = {}
    profile_plot_data: list[tuple[float, str, np.ndarray, np.ndarray]] = []
    for profile in scoring["profiles"]:
        values = read_topas_1d(dose_dir / f"{profile['output']}.bin", int(profile["bins"]))
        position = bin_centers(int(profile["bins"]), float(profile["step_mm"]), -float(profile["half_width_mm"]))
        output = curves_dir / f"{profile['output']}.csv"
        _write_curve(output, position, values, scale)
        calibrated = values * scale
        profile_results[profile["output"]] = {
            "axis": profile["axis"], "depth_mm": profile["depth_mm"],
            "csv": str(output), "metrics": profile_metrics(position, calibrated),
        }
        profile_plot_data.append((float(profile["depth_mm"]), profile["axis"], position, calibrated))

    metrics: dict[str, Any] = {
        "energy_mevu": float(args.energy_mevu),
        "histories": int(setup["run"]["histories"]),
        "scale_N_plan_over_N_sim": scale, "idd": idd_result["metrics"],
        "pdd": pdd_result["metrics"], "profiles": profile_results,
        "letd": letd_result, "carbon_energy_spectrum": spectrum_result,
        "entry_point": str(entry), "topas_command": command,
        "analysis_only": bool(args.analysis_only),
        "idd_at_measured_depths_csv": str(matched_idd_path) if matched_idd_path else None,
    }
    if measured is not None:
        comparison = compare_depth_curves(measured.depth_mm, measured.dose_au, depth, idd * scale, reference_label="commissioned measured IDD")
        comparison.pop("curve", None)
        metrics["idd_vs_measured"] = comparison
    (metrics_dir / "water_phantom_metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    scoring_diameter_mm = 2.0 * float(scoring["idd"]["radius_mm"])
    _plot_idd(
        figures_dir / "idd_comparison.png", (depth, idd * scale), measured,
        scoring_diameter_mm,
    )
    _plot_letd(
        figures_dir / "letd.png", (depth, idd * scale), (letd_depth, letd),
        scoring_diameter_mm,
    )
    _plot_carbon_spectrum(
        figures_dir / "carbon_energy_spectrum.png",
        float(args.energy_mevu),
        spectrum_total_mev,
        spectrum_weights,
    )
    _plot_profiles(figures_dir / "transverse_profiles.png", profile_plot_data)

    summary = {
        "energy_mevu": float(args.energy_mevu), "idd_csv": str(curves_dir / "idd.csv"),
        "idd_at_measured_depths_csv": str(matched_idd_path) if matched_idd_path else None,
        "pdd_csv": str(curves_dir / "pdd.csv"), "metrics_json": str(metrics_dir / "water_phantom_metrics.json"),
        "idd_metrics": idd_result["metrics"], "pdd_metrics": pdd_result["metrics"],
        "letd_csv": str(curves_dir / "letd.csv"), "letd_metrics": letd_result,
        "carbon_energy_spectrum_csv": str(curves_dir / "carbon_energy_spectrum.csv"),
        "carbon_energy_spectrum_figure": str(figures_dir / "carbon_energy_spectrum.png"),
        "carbon_energy_spectrum": spectrum_result,
        "profile_csv": [str(curves_dir / f"{p['output']}.csv") for p in scoring["profiles"]],
    }
    (cache / "water_phantom_run_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
