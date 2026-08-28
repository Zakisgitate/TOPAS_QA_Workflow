#!/usr/bin/env python3
"""Audit both TOPAS zero-history logs and the exact TPS dose-grid validation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

import pydicom


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def require(text: str, pattern: str, description: str) -> None:
    if not re.search(pattern, text, re.MULTILINE):
        raise RuntimeError(f"TOPAS preflight log lacks {description}: {pattern}")


def elapsed(text: str) -> str:
    matches = re.findall(r"^\s*Total: User=.*?Real=([0-9.]+)s", text, re.MULTILINE)
    return f"{float(matches[-1]):.3f} s" if matches else "not reported"


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    parse_log = root / "topas_output" / "test" / "validate_plan_full_parse.log"
    grid_log = root / "topas_output" / "test" / "validate_dose_grid.log"
    grid_summary = root / "plan_parsed" / "topas_dose_grid_validation_summary.txt"
    model_path = root / "plan_parsed" / "patient_model.json"
    generated_plan = root / "topas" / "beam" / "plan_generated.txt"
    output = root / "plan_parsed" / "topas_preflight_summary.txt"
    for path in (parse_log, grid_log, grid_summary, model_path, generated_plan):
        if not path.is_file():
            raise RuntimeError(f"Required preflight input is missing: {path}")
    if output.exists() and not args.overwrite:
        raise RuntimeError(f"Output exists; add --overwrite: {output}")

    parse_text = parse_log.read_bytes().replace(b"\x00", b"").decode("utf-8", errors="replace")
    grid_text = grid_log.read_bytes().replace(b"\x00", b"").decode("utf-8", errors="replace")
    grid_validation = grid_summary.read_text(encoding="utf-8")
    source_names = ["PlanCarbonBeam"] + re.findall(
        r'^s:So/([A-Za-z0-9_]+)/Type\s*=\s*"',
        generated_plan.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    source_names = list(dict.fromkeys(source_names))
    for label, text in (("full parse", parse_text), ("dose grid", grid_text)):
        require(text, r"TOPAS run sequence complete\.", f"successful {label} completion")
        for source_name in source_names:
            require(
                text,
                rf"Particle source {re.escape(source_name)}: Total number of histories: 0",
                f"zero {source_name} histories in {label}",
            )
    require(parse_text, r"Loading parameters starting from: validate_plan_full_parse\.txt", "full-parse entry point")
    require(grid_text, r"Loading parameters starting from: validate_dose_grid\.txt", "dose-grid entry point")
    require(grid_text, r"Scorer: TPSDoseToMedium", "TPSDoseToMedium scorer")
    if "Status: PASS" not in grid_validation:
        raise RuntimeError("Dose-grid validation summary is not PASS")

    model = json.loads(model_path.read_text(encoding="utf-8"))
    patient_mode = str(model.get("mode", ""))
    patient_details = "G4_WATER rectangular box"
    if patient_mode == "DICOM_CT_SCHNEIDER":
        ct_paths = sorted((root / "dicom" / "CT").glob("*.dcm"))
        if not ct_paths:
            raise RuntimeError("Patient model selects DICOM CT but no CT slices exist")
        first = pydicom.dcmread(ct_paths[0], stop_before_pixels=True)
        match = re.search(
            r"# of Voxels:\s*\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)", parse_text
        )
        if not match:
            raise RuntimeError("Full-parse log lacks DICOM CT voxel count")
        observed = tuple(map(int, match.groups()))
        expected = (int(first.Columns), int(first.Rows), len(ct_paths))
        if observed != expected:
            raise RuntimeError(f"TOPAS DICOM voxel count {observed} != expected {expected}")
        materials = re.search(r"Total number of materials used was: (\d+)", parse_text)
        if not materials or int(materials.group(1)) < 1:
            raise RuntimeError("Full-parse log lacks a valid DICOM material map")
        patient_details = f"TsDicomPatient voxels={observed}, materials={int(materials.group(1))}"

    version_match = re.search(r"Welcome to TOPAS.*?Version ([^)]+)\)", parse_text)
    version = version_match.group(1) if version_match else "unknown"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "TPS-TOPAS complete zero-history preflight\n"
        "=========================================\n"
        "Status: PASS\n\n"
        f"TOPAS version: {version}\n"
        f"Patient model: {patient_mode}\n"
        f"Patient initialization: {patient_details}\n"
        f"Full-plan parse elapsed: {elapsed(parse_text)}\n"
        f"Dose-grid initialization elapsed: {elapsed(grid_text)}\n"
        f"Zero-history TOPAS sources: {len(source_names)} ({', '.join(source_names)})\n"
        "Scorer: TPSDoseToMedium\n"
        f"Exact grid validation: PASS ({grid_summary})\n\n"
        "This proves configuration parsing, patient initialization and score-grid alignment only.\n"
        "It does not validate particle transport, HU calibration, the machine model or dose agreement.\n",
        encoding="utf-8",
    )
    print(output.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
