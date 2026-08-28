#!/usr/bin/env python3
"""Export the five requested water-phantom result sets into one directory.

This is an export-only operation. It reads completed TOPAS analysis outputs,
excludes 240.63 MeV/u, copies the original per-energy CSV/figures, and writes
combined CSV tables and overview plots for IDD, LETd and the carbon spectrum.
No transport or GUI state is changed.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path
import shutil
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
ENERGIES = (120.26, 190.19, 261.03, 330.09, 399.92)
MACHINE = "lzRoom1_90_RF4_260226"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--source-root", type=Path,
        help="Existing water-phantom result root (default: analysis/_water_phantom/<machine>)",
    )
    parser.add_argument(
        "--output-dir", type=Path,
        help="Summary output directory (default: analysis/_water_phantom/<machine>_five_energy_summary)",
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def _energy_dir(source_root: Path, energy: float) -> Path:
    tag = f"{MACHINE}_E{energy:g}_rel_100000_D150_LETd2_grid2"
    path = source_root / tag
    if not path.is_dir():
        raise RuntimeError(f"Missing result directory for {energy:g} MeV/u: {path}")
    return path


def _letd_csv(curves: Path) -> Path:
    exact = curves / "letd.csv"
    if exact.is_file():
        return exact
    candidates = sorted(curves.glob("letd*.csv"))
    if len(candidates) != 1:
        raise RuntimeError(f"Could not uniquely identify LETd CSV in {curves}: {candidates}")
    return candidates[0]


def _copy_required(source: Path, destination: Path, energy: float) -> dict[str, str]:
    destination.mkdir(parents=True, exist_ok=True)
    source_curves = source / "curves"
    source_figures = source / "figures"
    required = {
        "idd_csv": source_curves / "idd.csv",
        "idd_measured_csv": source_curves / "idd_at_measured_depths.csv",
        "letd_csv": _letd_csv(source_curves),
        "carbon_spectrum_csv": source_curves / "carbon_energy_spectrum.csv",
        "idd_figure": source_figures / "idd_comparison.png",
        "letd_figure": source_figures / "letd.png",
        "carbon_spectrum_figure": source_figures / "carbon_energy_spectrum.png",
    }
    for label, path in required.items():
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"Missing or empty {label} for {energy:g} MeV/u: {path}")
    target_names = {
        "idd_csv": "idd.csv",
        "idd_measured_csv": "idd_at_measured_depths.csv",
        "letd_csv": "letd.csv",
        "carbon_spectrum_csv": "carbon_energy_spectrum.csv",
        "idd_figure": "idd_comparison.png",
        "letd_figure": "letd.png",
        "carbon_spectrum_figure": "carbon_energy_spectrum.png",
    }
    result = {}
    for label, path in required.items():
        target = destination / target_names[label]
        shutil.copy2(path, target)
        result[label] = str(target)
    shutil.copy2(source / "metrics" / "water_phantom_metrics.json", destination / "water_phantom_metrics.json")
    shutil.copy2(source / "water_phantom_run_summary.json", destination / "water_phantom_run_summary.json")
    return result


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _plots(output: Path, datasets: list[dict[str, Any]]) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    colors = ["#4472C4", "#ED7D31", "#70AD47", "#A64D79", "#5B9BD5"]
    fig, axes = plt.subplots(3, 2, figsize=(12, 13), sharey=True)
    axes = axes.ravel()
    for ax, data, color in zip(axes, datasets, colors):
        depth = data["idd_depth"]
        dose = data["idd_relative"]
        measured_depth = data["measured_depth"]
        measured = data["measured_relative"]
        ax.plot(depth, dose, color=color, lw=1.4, label="TOPAS IDD, 150 mm")
        ax.scatter(measured_depth, measured, color="#333333", s=9, label="Measured IDD, 80 mm", zorder=3)
        ax.set_title(f"{data['energy']:g} MeV/u")
        ax.set_ylim(0, 1.4)
        ax.grid(True, alpha=0.25)
        ax.legend(loc="upper left", fontsize=8, framealpha=0.5)
    axes[-1].axis("off")
    fig.supxlabel("Water depth (mm)")
    fig.supylabel("IDD relative to maximum")
    fig.suptitle("Five-energy IDD comparison")
    fig.tight_layout(rect=(0.03, 0.03, 1, 0.97))
    fig.savefig(output / "idd_five_energy_overview.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(3, 2, figsize=(12, 13), sharey=False)
    axes = axes.ravel()
    for ax, data, color in zip(axes, datasets, colors):
        ax.plot(data["letd_depth"], data["letd"], color=color, lw=1.25)
        peak_index = int(np.nanargmax(data["letd"]))
        ax.scatter([data["letd_depth"][peak_index]], [data["letd"][peak_index]], color=color, s=22)
        ax.annotate(
            f"peak = {data['letd'][peak_index]:.2f} keV/um\n"
            f"at {data['letd_depth'][peak_index]:.1f} mm",
            xy=(data["letd_depth"][peak_index], data["letd"][peak_index]),
            xytext=(8, -10), textcoords="offset points", fontsize=8,
            ha="left", va="top", annotation_clip=True,
        )
        ax.set_title(f"{data['energy']:g} MeV/u")
        ax.grid(True, alpha=0.25)
    axes[-1].axis("off")
    fig.supxlabel("Water depth (mm)")
    fig.supylabel("LETd (keV/um)")
    fig.suptitle("Five-energy dose-weighted LETd")
    fig.tight_layout(rect=(0.03, 0.03, 1, 0.97))
    fig.savefig(output / "letd_five_energy_overview.png", dpi=180)
    plt.close(fig)

    fig, axes = plt.subplots(3, 2, figsize=(12, 13), sharey=True)
    axes = axes.ravel()
    for ax, data, color in zip(axes, datasets, colors):
        markerline, stemlines, baseline = ax.stem(
            data["spectrum_mevu"], data["spectrum_weight"], basefmt="none"
        )
        plt.setp(markerline, color=color, marker="o")
        plt.setp(stemlines, color=color)
        ax.set_title(f"{data['energy']:g} MeV/u")
        ax.grid(True, alpha=0.25)
    axes[-1].axis("off")
    fig.supxlabel("Carbon-ion component energy (MeV/u)")
    fig.supylabel("Relative weight")
    fig.suptitle("Five-energy discrete carbon-ion spectra")
    fig.tight_layout(rect=(0.03, 0.03, 1, 0.97))
    fig.savefig(output / "carbon_energy_spectra_five_energy_overview.png", dpi=180)
    plt.close(fig)


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    source_root = (args.source_root or root / "analysis" / "_water_phantom" / MACHINE).expanduser().resolve()
    output = (args.output_dir or root / "analysis" / "_water_phantom" / f"{MACHINE}_five_energy_summary").expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        if not args.overwrite:
            raise RuntimeError(f"Output already exists; use --overwrite: {output}")
        for child in output.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    output.mkdir(parents=True, exist_ok=True)

    datasets: list[dict[str, Any]] = []
    summary_rows: list[dict[str, Any]] = []
    idd_rows: list[dict[str, Any]] = []
    letd_rows: list[dict[str, Any]] = []
    spectrum_rows: list[dict[str, Any]] = []
    per_energy_rows = []

    for energy in ENERGIES:
        source = _energy_dir(source_root, energy)
        energy_dir = output / f"E{energy:g}_MeV_u"
        files = _copy_required(source, energy_dir, energy)
        idd = _read_csv(energy_dir / "idd.csv")
        measured = _read_csv(energy_dir / "idd_at_measured_depths.csv")
        letd = _read_csv(energy_dir / "letd.csv")
        spectrum = _read_csv(energy_dir / "carbon_energy_spectrum.csv")
        idd_depth = np.asarray([float(row["position_mm"]) for row in idd])
        idd_relative = np.asarray([float(row["relative_to_max"]) for row in idd])
        measured_depth = np.asarray([float(row["depth_mm"]) for row in measured])
        measured_relative = np.asarray([float(row["measured_relative"]) for row in measured])
        letd_depth = np.asarray([float(row["depth_mm"]) for row in letd])
        letd_values = np.asarray([float(row["letd_keV_per_um"]) for row in letd])
        spectrum_mevu = np.asarray([float(row["energy_per_nucleon_MeV_u"]) for row in spectrum])
        spectrum_weight = np.asarray([float(row["relative_weight"]) for row in spectrum])
        datasets.append({"energy": energy, "idd_depth": idd_depth, "idd_relative": idd_relative,
                         "measured_depth": measured_depth, "measured_relative": measured_relative,
                         "letd_depth": letd_depth, "letd": letd_values,
                         "spectrum_mevu": spectrum_mevu, "spectrum_weight": spectrum_weight})
        for row in idd:
            idd_rows.append({"energy_mevu": energy, **row})
        for row in letd:
            letd_rows.append({"energy_mevu": energy, **row})
        for row in spectrum:
            spectrum_rows.append({"nominal_energy_mevu": energy, **row})
        metrics = __import__("json").loads((energy_dir / "water_phantom_metrics.json").read_text(encoding="utf-8"))
        summary_rows.append({
            "energy_mevu": energy,
            "histories": metrics["histories"],
            "idd_R80_mm": metrics["idd"]["R80_mm"],
            "idd_R90_mm": metrics["idd"]["R90_mm"],
            "letd_peak_keV_per_um": metrics["letd"]["peak_letd_keV_per_um"],
            "letd_peak_depth_mm": metrics["letd"]["peak_depth_mm"],
            "spectrum_components": metrics["carbon_energy_spectrum"]["components"],
            "spectrum_mean_mevu": metrics["carbon_energy_spectrum"]["weighted_mean_mevu"],
        })
        per_energy_rows.append((energy, files))

    _write_csv(output / "idd_all.csv", ["energy_mevu", "position_mm", "dose_Gy_per_run", "dose_Gy_calibrated", "relative_to_max"], idd_rows)
    _write_csv(output / "letd_all.csv", ["energy_mevu", "depth_mm", "letd_keV_per_um"], letd_rows)
    _write_csv(output / "carbon_energy_spectrum_all.csv", ["nominal_energy_mevu", "component", "total_energy_MeV", "energy_per_nucleon_MeV_u", "relative_weight"], spectrum_rows)
    _write_csv(output / "summary_metrics.csv", list(summary_rows[0]), summary_rows)
    _plots(output, datasets)

    lines = [
        "# Five-energy TOPAS water-phantom summary",
        "",
        f"Machine: `{MACHINE}`",
        "Energies: `120.26, 190.19, 261.03, 330.09, 399.92 MeV/u`",
        "Excluded: `240.63 MeV/u`",
        "Simulation: `100000 histories`, single energy, single spot, IDD diameter `150 mm`, IDD/LETd spacing `2 mm`.",
        "The copied IDD measurement reference remains the original 80 mm measured curve; TOPAS IDD is 150 mm.",
        "",
        "## Combined outputs",
        "",
        "- [IDD combined CSV](idd_all.csv)",
        "- [LETd combined CSV](letd_all.csv)",
        "- [Carbon spectrum combined table](carbon_energy_spectrum_all.csv)",
        "- [Summary metrics](summary_metrics.csv)",
        "- [IDD five-energy overview](idd_five_energy_overview.png)",
        "- [LETd five-energy overview](letd_five_energy_overview.png)",
        "- [Carbon spectra five-energy overview](carbon_energy_spectra_five_energy_overview.png)",
        "",
        "## Per-energy outputs",
        "",
    ]
    for energy, files in per_energy_rows:
        lines += [
            f"### {energy:g} MeV/u",
            f"[IDD CSV](E{energy:g}_MeV_u/idd.csv) · [IDD figure](E{energy:g}_MeV_u/idd_comparison.png)",
            f"[LETd CSV](E{energy:g}_MeV_u/letd.csv) · [LETd figure](E{energy:g}_MeV_u/letd.png)",
            f"[Carbon spectrum table](E{energy:g}_MeV_u/carbon_energy_spectrum.csv) · [Carbon spectrum figure](E{energy:g}_MeV_u/carbon_energy_spectrum.png)",
            "",
        ]
    (output / "README.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"Exported {len(ENERGIES)} energy sets to {output}")
    print(f"Combined CSV/figures and README: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
