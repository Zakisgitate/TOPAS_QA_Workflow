#!/usr/bin/env python3
"""Audit the N_plan/N_sim calibration for a completed TOPAS dose binary."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from utils.mc_dose_calibration import require_particle_calibration, write_calibration_audit
from gui.case_results import analysis_run_dir, update_run_manifest


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--mc-binary", type=Path, required=True)
    parser.add_argument("--analysis-dir", type=Path)
    parser.add_argument("--output-tag", required=True)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    mc_path = args.mc_binary.expanduser().resolve()
    if not mc_path.is_file():
        raise RuntimeError(f"MC binary does not exist: {mc_path}")

    analysis_dir = (
        args.analysis_dir.expanduser().resolve()
        if args.analysis_dir
        else analysis_run_dir(root, args.output_tag, create=True)
    )
    output = analysis_dir / "calibration" / f"mc_dose_calibration_{args.output_tag}.json"
    if output.exists() and not args.overwrite:
        raise RuntimeError(f"Calibration audit exists; add --overwrite: {output}")
    calibration = require_particle_calibration(root, mc_path)
    write_calibration_audit(calibration, mc_path, output)
    update_run_manifest(
        root,
        args.output_tag,
        mc_source=mc_path,
        additions={"mc_dose_calibration": calibration.to_dict(), "mc_dose_calibration_audit": str(output)},
    )
    print(f"Independent particle calibration: {calibration.scale:.12g}")
    print(f"N_plan / N_sim: {calibration.planned_particles:.12g} / {calibration.simulated_histories}")
    print(f"Treatment machine: {calibration.treatment_machine_name}")
    print(f"Machine profile fingerprint: {calibration.commissioned_profile_fingerprint}")
    print(f"Number-per-MU SHA-256: {calibration.number_per_mu_sha256}")
    print(f"Machine calibration binding SHA-256: {calibration.machine_calibration_binding_sha256}")
    print(f"Wrote: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
