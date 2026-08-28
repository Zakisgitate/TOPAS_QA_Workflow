#!/usr/bin/env python3
"""Initialize a non-destructive TPS-TOPAS QA case folder from this template."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil


DIRECTORIES = (
    "dicom/CT",
    "dicom/RTPLAN",
    "dicom/RTDOSE",
    "dicom/RTSTRUCT",
    "plan_parsed",
    "topas/beam",
    "topas/geometry",
    "topas/graphics",
    "topas/physics",
    "topas/scoring",
    "topas_output/production",
    "topas_output/test",
    "analysis/figures",
    "analysis/profiles",
    "analysis/gamma",
    "machine_model",
    "measurement",
    "scripts",
    "scripts/utils",
)

STATIC_FILES = (
    "requirements.txt",
    "topas/beam/beam_model.txt",
    "topas/beam/MRF4.txt",
    "machine_model/HUtoMaterialSchneider.txt",
    "topas/validate_plan_full_parse.txt",
    "topas/validate_dose_grid.txt",
)


def parse_args() -> argparse.Namespace:
    template = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case-root", type=Path, required=True)
    parser.add_argument("--template-root", type=Path, default=template)
    return parser.parse_args()


def copy_if_missing(source: Path, target: Path) -> str:
    if target.exists():
        return "kept"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return "created"


def main() -> int:
    args = parse_args()
    template = args.template_root.expanduser().resolve()
    case = args.case_root.expanduser().resolve()
    if case == Path("/") or len(case.parts) < 3:
        raise RuntimeError(f"Refusing unsafe case root: {case}")
    case.mkdir(parents=True, exist_ok=True)
    for name in DIRECTORIES:
        (case / name).mkdir(parents=True, exist_ok=True)

    results: list[tuple[str, str]] = []
    for source in sorted((template / "scripts").rglob("*.py")):
        target = case / "scripts" / source.relative_to(template / "scripts")
        if source.resolve() == target.resolve():
            results.append((str(target), "template"))
        else:
            results.append((str(target), copy_if_missing(source, target)))
    commissioned_root = template / "machine_model" / "beam_commissioning"
    if commissioned_root.is_dir():
        for source in sorted(path for path in commissioned_root.rglob("*") if path.is_file()):
            target = case / "machine_model" / "beam_commissioning" / source.relative_to(commissioned_root)
            if source.resolve() == target.resolve():
                results.append((str(target), "template"))
            else:
                results.append((str(target), copy_if_missing(source, target)))
    asset_root = template / "machine_model" / "asset_registry"
    if asset_root.is_dir():
        for source in sorted(path for path in asset_root.rglob("*") if path.is_file()):
            target = case / "machine_model" / "asset_registry" / source.relative_to(asset_root)
            if source.resolve() == target.resolve():
                results.append((str(target), "template"))
            else:
                results.append((str(target), copy_if_missing(source, target)))
    registry_source = template / "machine_model" / "model_registry.json"
    if registry_source.is_file():
        registry_target = case / "machine_model" / "model_registry.json"
        if registry_source.resolve() == registry_target.resolve():
            results.append((str(registry_target), "template"))
        else:
            results.append((str(registry_target), copy_if_missing(registry_source, registry_target)))
    for name in STATIC_FILES:
        source = template / name
        target = case / name
        if not source.exists():
            continue
        if source.resolve() == target.resolve():
            results.append((str(target), "template"))
        else:
            results.append((str(target), copy_if_missing(source, target)))

    manifest = case / "case_config.json"
    if not manifest.exists():
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "workflow": "TPS-TOPAS carbon-ion PBS physical-dose shape QA",
                    "supported_scope": {
                        "beam_count": 1,
                        "patient_position": "HFS",
                        "gantry_deg": 90,
                        "couch_deg": 0,
                        "patient_models": [
                            "axis-aligned rectangular water phantom",
                            "axial DICOM CT with generic uncommissioned Schneider conversion",
                        ],
                        "beam_models": [
                            "RTPLAN baseline",
                            "machine-matched measured-IDD spectrum plus Fermi-Eyges emittance",
                        ],
                    },
                    "dicom_is_read_only": True,
                },
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        results.append((str(manifest), "created"))
    else:
        results.append((str(manifest), "kept"))

    created = sum(status == "created" for _, status in results)
    kept = sum(status == "kept" for _, status in results)
    print(f"Initialized case: {case}")
    print(f"Created {created} missing file(s); kept {kept} existing file(s).")
    print("No existing file and no DICOM input was overwritten.")
    print("Next: place DICOM files under dicom/CT, RTPLAN, RTDOSE and RTSTRUCT.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
