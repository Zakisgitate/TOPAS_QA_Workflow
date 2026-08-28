#!/usr/bin/env python3
"""Add one strictly bounded interpolated spectrum to a runtime profile.

This is for a nominal energy with no independent measured IDD.  Component
energies and weights are interpolated between two adjacent fitted spectra by
sorted component rank, then every component is hard-clipped to the nominal
energy before the hash-locked profile is rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np


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


def find_spectrum(rows: list[dict], energy_mevu: float) -> dict:
    matches = [row for row in rows if abs(float(row["measEnergy"]) / 12.0 - energy_mevu) <= 1e-6]
    if len(matches) != 1:
        raise RuntimeError(f"Expected one spectrum at {energy_mevu:g} MeV/u, found {len(matches)}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--lower-mevu", type=float, required=True)
    parser.add_argument("--upper-mevu", type=float, required=True)
    parser.add_argument("--target-mevu", type=float, required=True)
    args = parser.parse_args()
    profile_path = args.profile.expanduser().resolve()
    if not profile_path.is_file():
        raise RuntimeError(f"Profile does not exist: {profile_path}")
    if not args.lower_mevu < args.target_mevu < args.upper_mevu:
        raise RuntimeError("Require lower < target < upper energy")

    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    spectrum_path = (profile_path.parent / profile["files"]["energy_spectrum"]).resolve()
    rows = json.loads(spectrum_path.read_text(encoding="utf-8"))
    lower = find_spectrum(rows, args.lower_mevu)
    upper = find_spectrum(rows, args.upper_mevu)
    if len(lower["energys"]) != len(upper["energys"]):
        raise RuntimeError("Adjacent spectra must have equal component counts for rank interpolation")
    alpha = (args.target_mevu - args.lower_mevu) / (args.upper_mevu - args.lower_mevu)
    lower_e = np.asarray(lower["energys"], dtype=float)
    upper_e = np.asarray(upper["energys"], dtype=float)
    lower_w = np.asarray(lower["weights"], dtype=float)
    upper_w = np.asarray(upper["weights"], dtype=float)
    energies = (1.0 - alpha) * lower_e + alpha * upper_e
    weights = np.maximum((1.0 - alpha) * lower_w + alpha * upper_w, 0.0)
    energies = np.minimum(energies, args.target_mevu * 12.0)
    if not np.isfinite(energies).all() or not np.isfinite(weights).all() or weights.sum() <= 0:
        raise RuntimeError("Interpolated spectrum is invalid")
    weights /= weights.sum()
    row = {
        "measEnergy": float(args.target_mevu * 12.0),
        "energys": [float(value) for value in energies],
        "weights": [float(value) for value in weights],
        "flag": "interpolated_no_measured_idd_strict_upper_bound",
    }
    rows = [existing for existing in rows if abs(float(existing["measEnergy"]) / 12.0 - args.target_mevu) > 1e-6]
    rows.append(row)
    rows.sort(key=lambda item: float(item["measEnergy"]))
    spectrum_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    profile["sha256"]["energy_spectrum"] = sha256(spectrum_path)
    limitations = profile.setdefault("limitations", [])
    note = "284.81 MeV/u spectrum is interpolated from 282.69 and 309.82 MeV/u fitted spectra; no independent measured IDD exists."
    if note not in limitations:
        limitations.append(note)
    provenance = profile.setdefault("provenance", {})
    provenance["interpolated_spectrum"] = {
        "target_mevu": args.target_mevu,
        "lower_mevu": args.lower_mevu,
        "upper_mevu": args.upper_mevu,
        "method": "rank-wise linear interpolation of strict fitted component energies and weights; hard upper bound",
    }
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    files = profile["files"]
    ordered_paths = [profile_path.parent / files[key] for key in (
        "energy_spectrum", "phase_space", "number_per_mu",
        "measured_idd", "measured_spot_sigma", "energy_list",
    )]
    calibration_path = profile_path.parent / "particle_calibration.json"
    calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
    calibration["commissioned_profile"]["sha256"] = sha256(profile_path)
    calibration["commissioned_profile"]["fingerprint"] = fingerprint(profile_path, ordered_paths)
    calibration["number_per_mu"]["sha256"] = sha256(profile_path.parent / files["number_per_mu"])
    calibration_path.write_text(json.dumps(calibration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    actual = [energy / 12.0 for energy in row["energys"]]
    if max(actual) > args.target_mevu + 1e-9:
        raise RuntimeError("Strict upper-bound verification failed")
    audit = {
        "status": "interpolated_no_measured_idd_strict_upper_bound",
        "target_mevu": args.target_mevu,
        "source_measured_energies_mevu": [args.lower_mevu, args.upper_mevu],
        "component_count": len(actual),
        "maximum_component_mevu": max(actual),
        "weight_sum": float(weights.sum()),
        "profile": str(profile_path),
        "spectrum": str(spectrum_path),
    }
    audit_path = profile_path.parent / "interpolated_spectrum_284p81_audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(audit, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
