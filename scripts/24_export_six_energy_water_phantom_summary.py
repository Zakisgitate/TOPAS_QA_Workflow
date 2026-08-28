#!/usr/bin/env python3
"""Export strict 120.26 plus five-energy results, adding 284.81 MeV/u."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import shutil

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
MACHINE = "lzRoom1_90_RF4_260226"
ENERGIES = (120.26, 190.19, 261.03, 284.81, 330.09, 399.92)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def source_dirs(root: Path) -> dict[float, Path]:
    base = root / "analysis" / "_water_phantom" / MACHINE
    return {
        120.26: base / f"{MACHINE}_E120.26_strict_100000_D150_LETd2_grid2",
        190.19: base / f"{MACHINE}_E190.19_rel_100000_D150_LETd2_grid2",
        261.03: base / f"{MACHINE}_E261.03_rel_100000_D150_LETd2_grid2",
        284.81: base / f"{MACHINE}_E284.81_strict_interpolated_100000_D150_LETd2_grid2",
        330.09: base / f"{MACHINE}_E330.09_rel_100000_D150_LETd2_grid2",
        399.92: base / f"{MACHINE}_E399.92_rel_100000_D150_LETd2_grid2",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "analysis/_water_phantom/lzRoom1_90_RF4_260226_six_energy_summary")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        if not args.overwrite:
            raise RuntimeError(f"Output exists; use --overwrite: {output}")
        for child in output.iterdir():
            if child.is_dir(): shutil.rmtree(child)
            else: child.unlink()
    output.mkdir(parents=True, exist_ok=True)

    idd_rows: list[dict] = []
    measured_rows: list[dict] = []
    letd_rows: list[dict] = []
    spectrum_rows: list[dict] = []
    datasets: list[dict] = []
    summary: list[dict] = []
    sources = source_dirs(root)
    for energy in ENERGIES:
        source = sources[energy]
        if not source.is_dir(): raise RuntimeError(f"Missing source result: {source}")
        dest = output / f"E{energy:g}_MeV_u"
        dest.mkdir()
        for sub, names in {
            "curves": ["idd.csv", "letd.csv", "carbon_energy_spectrum.csv"],
            "figures": ["idd_comparison.png", "letd.png", "carbon_energy_spectrum.png", "transverse_profiles.png"],
        }.items():
            for name in names:
                src = source / sub / name
                # One legacy 330.09 MeV/u run used a localized LETd filename.
                if not src.is_file() and energy == 330.09 and name == "letd.csv":
                    localized = sorted((source / sub).glob("letd*.csv"))
                    if len(localized) == 1:
                        src = localized[0]
                if not src.is_file(): raise RuntimeError(f"Missing {src}")
                shutil.copy2(src, dest / name)
        measured = source / "curves" / "idd_at_measured_depths.csv"
        if measured.is_file(): shutil.copy2(measured, dest / "idd_at_measured_depths.csv")
        shutil.copy2(source / "metrics" / "water_phantom_metrics.json", dest / "water_phantom_metrics.json")
        idd = read_csv(source / "curves/idd.csv")
        letd_path = source / "curves/letd.csv"
        if not letd_path.is_file() and energy == 330.09:
            localized = sorted((source / "curves").glob("letd*.csv"))
            if len(localized) == 1:
                letd_path = localized[0]
        letd = read_csv(letd_path)
        spectrum = read_csv(source / "curves/carbon_energy_spectrum.csv")
        for row in idd: idd_rows.append({"energy_mevu": energy, **row})
        for row in letd: letd_rows.append({"energy_mevu": energy, **row})
        for row in spectrum: spectrum_rows.append({"nominal_energy_mevu": energy, **row})
        if measured.is_file():
            for row in read_csv(measured): measured_rows.append({"energy_mevu": energy, **row})
        metrics = json.loads((source / "metrics/water_phantom_metrics.json").read_text(encoding="utf-8"))
        summary.append({"energy_mevu": energy, "histories": metrics["histories"], "idd_R80_mm": metrics["idd"]["R80_mm"], "letd_peak_keV_per_um": metrics["letd"]["peak_letd_keV_per_um"], "letd_peak_depth_mm": metrics["letd"]["peak_depth_mm"], "spectrum_components": metrics["carbon_energy_spectrum"]["components"], "spectrum_mean_mevu": metrics["carbon_energy_spectrum"]["weighted_mean_mevu"], "idd_measured_reference": measured.is_file()})
        datasets.append({"energy": energy, "idd": idd, "measured": read_csv(measured) if measured.is_file() else [], "letd": letd, "spectrum": spectrum})

    write_csv(output / "idd_all.csv", ["energy_mevu", "position_mm", "dose_Gy_per_run", "dose_Gy_calibrated", "relative_to_max"], idd_rows)
    write_csv(output / "idd_measured_all.csv", ["energy_mevu", "depth_mm", "measured_relative", "topas_idd_interpolated_Gy", "topas_idd_relative", "difference_percent"], measured_rows)
    write_csv(output / "letd_all.csv", ["energy_mevu", "depth_mm", "letd_keV_per_um"], letd_rows)
    write_csv(output / "carbon_energy_spectrum_all.csv", ["nominal_energy_mevu", "component", "total_energy_MeV", "energy_per_nucleon_MeV_u", "relative_weight"], spectrum_rows)
    write_csv(output / "summary_metrics.csv", list(summary[0]), summary)

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    colors = ["#4472C4", "#ED7D31", "#70AD47", "#A64D79", "#5B9BD5", "#8064A2"]
    fig, axes = plt.subplots(2, 3, figsize=(14, 8), sharey=True); axes = axes.ravel()
    for ax, data, color in zip(axes, datasets, colors):
        x = np.array([float(r["position_mm"]) for r in data["idd"]]); y = np.array([float(r["relative_to_max"]) for r in data["idd"]]); ax.plot(x, y, color=color, lw=1.4, label="TOPAS IDD, 150 mm")
        if data["measured"]:
            mx = [float(r["depth_mm"]) for r in data["measured"]]; my = [float(r["measured_relative"]) for r in data["measured"]]; ax.scatter(mx, my, color="#333333", s=8, label="Measured IDD, 80 mm", zorder=3)
        ax.set_title(f"{data['energy']:g} MeV/u"); ax.set_ylim(0, 1.4); ax.grid(True, alpha=.25); ax.legend(loc="upper left", fontsize=7, framealpha=.5)
    fig.supxlabel("Water depth (mm)"); fig.supylabel("IDD relative to maximum"); fig.suptitle("Six-energy IDD comparison (240.63 MeV/u excluded)"); fig.tight_layout(); fig.savefig(output / "idd_six_energy_overview.png", dpi=180); plt.close(fig)

    fig, axes = plt.subplots(2, 3, figsize=(14, 8)); axes = axes.ravel()
    for ax, data, color in zip(axes, datasets, colors):
        x = np.array([float(r["depth_mm"]) for r in data["letd"]]); y = np.array([float(r["letd_keV_per_um"]) for r in data["letd"]]); ax.plot(x, y, color=color, lw=1.25); i = int(np.nanargmax(y)); ax.scatter([x[i]], [y[i]], color=color, s=20); ax.annotate(f"peak = {y[i]:.2f}\nat {x[i]:.1f} mm", (x[i], y[i]), xytext=(7,-10), textcoords="offset points", fontsize=8, annotation_clip=True); ax.set_title(f"{data['energy']:g} MeV/u"); ax.grid(True, alpha=.25)
    fig.supxlabel("Water depth (mm)"); fig.supylabel("LETd (keV/um)"); fig.suptitle("Six-energy dose-weighted LETd"); fig.tight_layout(); fig.savefig(output / "letd_six_energy_overview.png", dpi=180); plt.close(fig)

    fig, axes = plt.subplots(2, 3, figsize=(14, 8), sharey=True); axes = axes.ravel()
    for ax, data, color in zip(axes, datasets, colors):
        x = [float(r["energy_per_nucleon_MeV_u"]) for r in data["spectrum"]]; y = [float(r["relative_weight"]) for r in data["spectrum"]]; markerline, stemlines, _ = ax.stem(x, y, basefmt="none"); plt.setp(markerline, color=color, markersize=4); plt.setp(stemlines, color=color); ax.set_title(f"{data['energy']:g} MeV/u"); ax.grid(True, alpha=.25)
    fig.supxlabel("Carbon-ion component energy (MeV/u)"); fig.supylabel("Relative weight"); fig.suptitle("Six-energy discrete carbon-ion spectra"); fig.tight_layout(); fig.savefig(output / "carbon_energy_spectra_six_energy_overview.png", dpi=180); plt.close(fig)

    (output / "README.md").write_text("\n".join([
        "# Six-energy TOPAS water-phantom summary", "", f"Machine: `{MACHINE}`", "Energies: `120.26, 190.19, 261.03, 284.81, 330.09, 399.92 MeV/u`", "Excluded: `240.63 MeV/u`", "Simulation: `100000 histories`, single energy, single spot, IDD diameter `150 mm`, IDD/LETd/Profile spacing `2 mm`.", "`284.81 MeV/u` uses a strict upper-bounded interpolated spectrum and has no independent measured IDD.", "", "## Combined outputs", "", "- [IDD combined CSV](idd_all.csv)", "- [Measured IDD points](idd_measured_all.csv)", "- [LETd combined CSV](letd_all.csv)", "- [Carbon spectrum table](carbon_energy_spectrum_all.csv)", "- [Summary metrics](summary_metrics.csv)", "- [IDD overview](idd_six_energy_overview.png)", "- [LETd overview](letd_six_energy_overview.png)", "- [Carbon spectra overview](carbon_energy_spectra_six_energy_overview.png)", "", "Per-energy folders contain IDD/LETd/spectrum CSVs and figures.", "" ]), encoding="utf-8")
    print(f"Exported {len(ENERGIES)} energy sets to {output}")
    return 0


if __name__ == "__main__": raise SystemExit(main())
