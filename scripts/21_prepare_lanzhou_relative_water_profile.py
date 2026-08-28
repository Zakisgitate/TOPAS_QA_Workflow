#!/usr/bin/env python3
"""Build an isolated relative-dose runtime profile from the LanZhou draft.

The current LanZhou intake has no commissioned primary-carbon-ions-per-MU
table.  A standalone water-phantom shape run does not need an absolute MU
scale, but the existing commissioned-source loader requires a calibration
binding.  This helper therefore creates a run-local unit NF table and marks it
as a non-physical placeholder.  It must never be used with meterset or for an
absolute-dose claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DRAFT = ROOT / "machine_model/drafts/lzRoom1_90_RF4_260226_latest"
DEFAULT_OUTPUT = ROOT / "analysis/_water_phantom/_runtime_profiles/lzRoom1_90_RF4_260226_relative_only"
REQUIRED_FILES = {
    "energy_spectrum": "energy_spectrum.json",
    "phase_space": "phase_space.json",
    "measured_idd": "IDD_lzRoom1_90_RF4.csv",
    "measured_spot_sigma": "measured_spot_sigma.csv",
    "energy_list": "energy_list_mevu.txt",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprint(profile: Path, file_paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in [profile, *file_paths]:
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, default=DEFAULT_DRAFT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    draft = args.draft.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if ROOT.resolve() not in output.parents:
        raise RuntimeError(f"Refusing runtime profile outside project root: {output}")
    if output.exists() and not args.overwrite:
        raise RuntimeError(f"Runtime profile already exists: {output}; pass --overwrite")
    output.mkdir(parents=True, exist_ok=True)

    draft_profile = json.loads((draft / "profile.json").read_text(encoding="utf-8"))
    for filename in REQUIRED_FILES.values():
        source = draft / filename
        if not source.is_file():
            raise RuntimeError(f"Missing LanZhou draft input: {source}")
        shutil.copy2(source, output / filename)

    energy_values = [
        float(line.strip())
        for line in (output / REQUIRED_FILES["energy_list"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    nf_path = output / "number_per_mu.txt"
    nf_path.write_text("".join(f"{energy:.8g} 1.0\n" for energy in energy_values), encoding="utf-8")

    files = {**REQUIRED_FILES, "number_per_mu": nf_path.name}
    profile = {
        "schema_version": 1,
        "model_kind": "provisional_lanzhou_relative_water_validation_only",
        "treatment_machine_name": draft_profile["treatment_machine_name"],
        "source_plane_upstream_mm": float(draft_profile["source_plane_upstream_mm"]),
        "expected_vsad_mm": draft_profile["expected_vsad_mm"],
        "vsad_tolerance_mm": 25.0,
        "water_mean_excitation_energy_ev": 75.0,
        "files": files,
        "phase_space_fit_validation": {
            "depth_mapping": "reverse_sigma_order",
            "maximum_rmse_mm": 0.25,
            "maximum_isocenter_error_mm": 0.25,
        },
        "sha256": {key: sha256(output / filename) for key, filename in files.items()},
        "limitations": [
            "Research-only relative water-phantom run.",
            "SpotSummary is provisionally interpreted as X/Y FWHM and converted to sigma.",
            "number_per_mu.txt contains unit placeholders, not measured primary carbon ions per MU.",
            "Do not specify meterset and do not interpret dose as absolute or per MU.",
            "LanZhou RTPLAN identity and VSAD convention have not yet been independently verified.",
        ],
        "provenance": {
            "draft_profile": str(draft / "profile.json"),
            "purpose": "single-energy single-spot relative IDD, LETd and lateral-profile validation",
        },
    }
    profile_path = output / "profile.json"
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    ordered_paths = [output / files[key] for key in (
        "energy_spectrum", "phase_space", "number_per_mu",
        "measured_idd", "measured_spot_sigma", "energy_list",
    )]
    calibration = {
        "schema_version": 1,
        "calibration_kind": "relative_shape_only_unit_nf_placeholder_not_physical",
        "treatment_machine_name": profile["treatment_machine_name"],
        "commissioned_profile": {
            "file": profile_path.name,
            "sha256": sha256(profile_path),
            "fingerprint": fingerprint(profile_path, ordered_paths),
        },
        "number_per_mu": {
            "file": nf_path.name,
            "sha256": sha256(nf_path),
            "units": "unit placeholder; NOT primary carbon ions per MU",
            "scope": "relative water-phantom shape only",
        },
        "dose_output_correction": {
            "factor": 1.0,
            "status": "identity_no_empirical_correction",
            "scope": "relative shape only; no absolute-dose interpretation",
        },
    }
    calibration_path = output / "particle_calibration.json"
    calibration_path.write_text(json.dumps(calibration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    audit = {
        "status": "provisional_relative_only_not_for_clinical_or_absolute_dose_use",
        "profile": str(profile_path),
        "particle_calibration": str(calibration_path),
        "energy_count": len(energy_values),
        "unit_nf_placeholder": True,
        "source_draft": str(draft),
    }
    (output / "runtime_profile_audit.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
