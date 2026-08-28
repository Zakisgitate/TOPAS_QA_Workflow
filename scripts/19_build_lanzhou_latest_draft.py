#!/usr/bin/env python3
"""Ingest the latest LanZhou RF4 commissioning folder into an isolated draft.

This command deliberately stops short of GUI activation.  It reconstructs the
discrete incident-energy spectra from measured IDD curves and records the
machine-template, range, absolute-output, minimum-MU and spot-summary evidence.
It does not turn SpotSummary widths into Fermi-Eyges sigma values and does not
invent primary-carbon-per-MU calibration when the source folder does not supply
those definitions.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import sys
from typing import Any
from zipfile import ZipFile
from xml.etree import ElementTree as ET

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
LEGACY_BUILDER = Path(__file__).with_name("18_build_machine3_lanzhou_draft.py")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_legacy_helpers():
    spec = importlib.util.spec_from_file_location("legacy_lanzhou_builder", LEGACY_BUILDER)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load helper module: {LEGACY_BUILDER}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_xlsx(path: Path) -> dict[str, str]:
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(path) as archive:
        shared_root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = [
            "".join(text.text or "" for text in item.iter("{%s}t" % ns["m"]))
            for item in shared_root.findall("m:si", ns)
        ]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
    values: dict[str, str] = {}
    for row in sheet.findall(".//m:row", ns):
        cells = {}
        for cell in row.findall("m:c", ns):
            ref = cell.attrib.get("r", "")
            value = cell.find("m:v", ns)
            text = "" if value is None else (value.text or "")
            if cell.attrib.get("t") == "s" and text:
                text = shared[int(text)]
            cells[ref] = text
        row_number = row.attrib.get("r", "")
        key = cells.get(f"A{row_number}", "")
        if not key:
            key = cells.get(f"B{row_number}", "")
        actual = cells.get(f"F{row_number}", "")
        if key and actual:
            values.setdefault(key, actual)
            values[f"{key}__row{row_number}"] = actual
    return values


def parse_energy_limits(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            energy = (row.get("Energy[MeV]") or "").strip()
            if not energy:
                continue
            mu = (row.get("Min spot meterset [MU/fx]") or "").strip()
            rows.append({"energy_total_mev": float(energy), "minimum_mu": float(mu) if mu else None})
    return rows


def parse_two_column_table(path: Path) -> list[dict[str, float]]:
    rows = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        fields = [item.strip() for item in line.replace(",", "\t").split("\t") if item.strip()]
        if len(fields) < 2:
            continue
        try:
            rows.append({"energy_mevu": float(fields[0]), "value": float(fields[1])})
        except ValueError:
            continue
    return rows


def parse_absolute(path: Path) -> dict[str, Any]:
    values = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if ";" not in line or line.startswith("End"):
            continue
        fields = [item.strip() for item in line.split(";")]
        if len(fields) < 4:
            continue
        try:
            values.append({
                "energy_total_mev": float(fields[0]),
                "depth_mm": float(fields[1]),
                "meterset_mu": float(fields[2]),
                "dose_cgy": float(fields[3]),
            })
        except ValueError:
            continue
    return {"rows": values, "row_count": len(values)}


def parse_spot_summary(path: Path) -> dict[str, Any]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    planes = lines[0].split() if lines else []
    rows = []
    for line in lines[1:]:
        fields = line.replace("\t", " ").split()
        if len(fields) < 1 + 2 * len(planes):
            continue
        try:
            energy = float(fields[0])
            numbers = [float(value) for value in fields[1:1 + 2 * len(planes)]]
        except ValueError:
            continue
        rows.append({
            "energy_total_mev": energy,
            "planes": [
                {"plane": float(planes[index]), "x_raw": numbers[2 * index], "y_raw": numbers[2 * index + 1]}
                for index in range(len(planes))
            ],
        })
    return {
        "planes": [float(value) for value in planes],
        "rows": rows,
        "row_count": len(rows),
        "width_definition": "unspecified in source; values are retained as raw X/Y summary numbers",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--kernel-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "machine_model" / "drafts" / "lzRoom1_90_RF4_260226_latest")
    parser.add_argument(
        "--strict-spectrum-upper-bound", action="store_true",
        help="Restrict every fitted primary-carbon component to <= its nominal MeV/u energy",
    )
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    source = args.source_root.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    if output.exists():
        if not args.overwrite:
            raise RuntimeError(f"Output exists: {output}; use --overwrite")
        if args.root.resolve() not in output.parents:
            raise RuntimeError(f"Refusing to overwrite outside project root: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True)

    required = {
        "machine_template": source / "lzRoom1_90_RF4-25A.xlsx",
        "measured_idd": source / "IDD_lzRoom1_90_RF4.csv",
        "range": source / "Range.csv",
        "absolute": source / "Absolute_lzRoom1_90_RF4.csv",
        "energy_limits": source / "Energy-and-limitation-25A.csv",
        "spot_air": source / "SpotSummary_lzRoom1PBS_RF4.txt",
        "spot_rs": source / "SpotSummary_lzRoom1PBS_RF4 - RS.txt",
    }
    missing = [str(path) for path in required.values() if not path.is_file()]
    if missing:
        raise RuntimeError("Missing latest LanZhou inputs: " + ", ".join(missing))

    helpers = load_legacy_helpers()
    kernel_dir = args.kernel_dir.expanduser().resolve()
    kernel_depth, kernel_energy_total, kernel_idd = helpers.load_kernels(kernel_dir)
    spectra, spectrum_audit = helpers.fit_spectra(
        required["measured_idd"], kernel_depth, kernel_energy_total, kernel_idd,
        strict_upper_bound=args.strict_spectrum_upper_bound,
    )
    for key, path in required.items():
        shutil.copy2(path, output / path.name)
    (output / "energy_spectrum.json").write_text(json.dumps(spectra, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "energy_spectrum_fit_audit.csv").write_text("", encoding="utf-8")
    with (output / "energy_spectrum_fit_audit.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(spectrum_audit[0]))
        writer.writeheader()
        writer.writerows(spectrum_audit)

    template = parse_xlsx(required["machine_template"])
    energy_limits = parse_energy_limits(required["energy_limits"])
    range_rows = parse_two_column_table(required["range"])
    absolute = parse_absolute(required["absolute"])
    spot_air = parse_spot_summary(required["spot_air"])
    spot_rs = parse_spot_summary(required["spot_rs"])
    idd_curves = helpers.parse_measured_idd(required["measured_idd"])
    measured_energies = [float(curve.nominal_mevu) for curve in idd_curves]
    all_energies = [row["energy_total_mev"] / 12.0 for row in energy_limits]
    range_energy = np.asarray([row["energy_mevu"] for row in range_rows], dtype=float)
    range_value = np.asarray([row["value"] for row in range_rows], dtype=float)
    range_crosscheck = []
    for curve in idd_curves:
        measured_r80 = helpers.r80_mm(curve.depth_mm, curve.dose_au / float(np.max(curve.dose_au)))
        range_reference = float(np.interp(curve.nominal_mevu, range_energy, range_value))
        range_crosscheck.append({
            "energy_mevu": float(curve.nominal_mevu),
            "idd_r80_mm_project_metric": float(measured_r80),
            "range_csv_value": range_reference,
            "range_minus_idd_r80_mm": range_reference - float(measured_r80),
        })

    (output / "energy_list_mevu.txt").write_text("".join(f"{energy:.5f}\n" for energy in all_energies), encoding="utf-8")
    (output / "energy_limits.json").write_text(json.dumps(energy_limits, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "range_reference.json").write_text(json.dumps({"rows": range_rows, "row_count": len(range_rows), "role": "cross_check_only"}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "range_crosscheck.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(range_crosscheck[0]))
        writer.writeheader()
        writer.writerows(range_crosscheck)
    (output / "absolute_reference.json").write_text(json.dumps(absolute, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "machine_template_values.json").write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "spot_summary_air.json").write_text(json.dumps(spot_air, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "spot_summary_rs.json").write_text(json.dumps(spot_rs, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    profile = {
        "schema_version": 1,
        "model_kind": "latest_lanzhou_data_intake_draft",
        "treatment_machine_name": template.get("Name__row4", "lzRoom1_90_RF4_260226"),
        "machine_alias": template.get("Machine name alias__row13", "90DegreeRoom1"),
        "source_plane_upstream_mm": 680.0,
        "expected_vsad_mm": [float(template.get("Focal length X__row74", 6228.28)), float(template.get("Focal length Y__row75", 7007.64))],
        "machine_parameters": {
            "gantry_deg": float(template.get("Discrete gantry angle__row73", 90)),
            "snout_name": template.get("snout(s)__row21", "lzRoom1_90_RF4"),
            "snout_position_mm": float(template.get("Positions__row23", 585.5)),
            "range_shifter_name": template.get("Range shifter(s)__row37", "RangeShifter_35mm"),
            "range_shifter_physical_thickness_mm": float(template.get("Physical thickness__row39", 30.05)),
            "range_shifter_wet_mm": float(template.get("Water equivalent thickness__row40", 35)),
            "range_shifter_tray_to_iso_mm": float(template.get("Tray to iso distance__row44", 595.5)),
            "range_shifter_material": template.get("Material__row48", "PMMA"),
            "range_shifter_density_g_cm3": float(template.get("Material mass density__row49", 1.19)),
            "mrf4_material": template.get("Material__row60", "Aluminum"),
            "mrf4_wet_mm": float(template.get("Water equivalent thickness__row59", 4)),
        },
        "energy_grid": {
            "layer_count": len(energy_limits),
            "energy_range_mevu": [min(all_energies), max(all_energies)],
            "measured_idd_energy_count": len(measured_energies),
            "measured_idd_energies_mevu": measured_energies,
        },
        "evidence": {
            "range_role": "cross_check_only; depth origin and definition require confirmation",
            "absolute_role": "measured cGy/MU reference; not primary-carbon NF(E)",
            "spot_summary_role": "raw width summary only; sigma/FWHM definition unspecified",
        },
        "files": {
            "energy_spectrum": "energy_spectrum.json",
            "energy_spectrum_fit_audit": "energy_spectrum_fit_audit.csv",
            "measured_idd": required["measured_idd"].name,
            "range_reference": "range_reference.json",
            "range_crosscheck": "range_crosscheck.csv",
            "absolute_reference": "absolute_reference.json",
            "energy_limits": "energy_limits.json",
            "spot_summary_air": "spot_summary_air.json",
            "spot_summary_rs": "spot_summary_rs.json",
            "machine_template_values": "machine_template_values.json",
            "energy_list": "energy_list_mevu.txt",
        },
        "sha256": {path.name: sha256(output / path.name) for path in required.values()},
        "draft_status": "incomplete_unapproved_not_imported_not_for_clinical_use",
        "missing_required_inputs": [
            "Raw SpotProfile or written confirmation that SpotSummary values are sigma in mm with defined plane convention",
            "Primary carbon ions per MU NF(E) table or approved calibration pathway",
            "LanZhou RTPLAN with exact TreatmentMachineName and VSAD identity",
            "Range.csv depth origin and R80 definition/correction confirmation",
        ],
        "provenance": {
            "source_root": str(source),
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "kernel_library": str(kernel_dir),
            "method": (
                "NNLS measured-IDT spectrum against existing ideal-water kernels; "
                + ("strict primary-carbon upper bound <= nominal energy; " if args.strict_spectrum_upper_bound else "")
                + "no phase-space or NF(E) fabricated"
            ),
        },
    }
    (output / "profile.json").write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {
        "schema_version": 1,
        "package_kind": "beam_commissioning_data_intake_draft",
        "package_version": "lzRoom1_90_RF4_260226-data-intake-20260826",
        "subject": {"treatment_machine_name": profile["treatment_machine_name"]},
        "approval": {"status": "draft_incomplete_unapproved", "approved_by": "", "approved_at": ""},
        "files": profile["files"],
        "provenance": profile["provenance"],
        "missing_required_inputs": profile["missing_required_inputs"],
    }
    (output / "machine_package.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    audit = {
        "draft_status": profile["draft_status"],
        "machine_name": profile["treatment_machine_name"],
        "energy_spectrum_fit": {
            "curve_count": len(spectrum_audit),
            "median_normalized_rmse": float(np.median([row["normalized_rmse"] for row in spectrum_audit])),
            "maximum_normalized_rmse": float(np.max([row["normalized_rmse"] for row in spectrum_audit])),
            "maximum_abs_r80_delta_mm": float(np.max(np.abs([row["r80_delta_mm"] for row in spectrum_audit]))),
        },
        "layer_count": len(energy_limits),
        "range_count": len(range_rows),
        "range_crosscheck": {
            "matched_measured_curves": len(range_crosscheck),
            "median_range_minus_idd_r80_mm": float(np.median([row["range_minus_idd_r80_mm"] for row in range_crosscheck])),
            "maximum_abs_range_minus_idd_r80_mm": float(np.max(np.abs([row["range_minus_idd_r80_mm"] for row in range_crosscheck]))),
        },
        "absolute_count": absolute["row_count"],
        "spot_air_count": spot_air["row_count"],
        "spot_rs_count": spot_rs["row_count"],
        "missing_required_inputs": profile["missing_required_inputs"],
    }
    (output / "latest_lanzhou_intake_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output_dir": str(output), **audit}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
