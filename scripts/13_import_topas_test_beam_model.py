#!/usr/bin/env python3
"""Import and validate the matching TOPAS_Test machine beam commissioning model."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

import numpy as np
import pydicom


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from scripts.utils.commissioned_beam import CommissionedBeamModel, sha256


SUPPORTED = {
    "hzRoom1_90_RF4_250701": {
        "reference_case": "Hangzhou/1685",
        "source_plane_upstream_mm": 680.0,
        "expected_vsad_mm": [5398.68, 6198.24],
        "water_mean_excitation_energy_ev": 75.0,
    }
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=APP_ROOT)
    parser.add_argument("--source-root", type=Path, required=True, help="TOPAS_Test project root")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def active_plan(root: Path):
    candidates = []
    for path in sorted((root / "dicom" / "RTPLAN").glob("*.dcm")):
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        if hasattr(ds, "IonBeamSequence"):
            candidates.append((path.resolve(), ds))
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one active RT Ion Plan, found {len(candidates)}")
    return candidates[0]


def safe_name(value: str) -> str:
    result = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    if not result:
        raise RuntimeError("Cannot derive a safe machine profile folder name")
    return result


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    source_root = args.source_root.expanduser().resolve()
    if not source_root.is_dir():
        raise RuntimeError(f"TOPAS_Test root does not exist: {source_root}")
    plan_path, plan = active_plan(root)
    beams = list(plan.IonBeamSequence)
    if len(beams) != 1:
        raise RuntimeError("Commissioned beam import currently requires exactly one ion beam")
    beam = beams[0]
    machine = str(getattr(beam, "TreatmentMachineName", "")).strip()
    if machine not in SUPPORTED:
        raise RuntimeError(
            f"No reviewed TOPAS_Test import mapping for RTPLAN machine {machine!r}; "
            f"supported: {', '.join(sorted(SUPPORTED))}"
        )
    config = SUPPORTED[machine]
    actual_vsad = np.asarray(getattr(beam, "VirtualSourceAxisDistances", []), dtype=float)
    expected_vsad = np.asarray(config["expected_vsad_mm"], dtype=float)
    if actual_vsad.shape != (2,) or np.any(np.abs(actual_vsad - expected_vsad) > 25.0):
        raise RuntimeError(
            f"RTPLAN VSAD {actual_vsad.tolist()} mm does not match {machine} commissioning "
            f"{expected_vsad.tolist()} mm within 25 mm"
        )

    reference = source_root / str(config["reference_case"])
    conversion = reference / "TOPAS计划验证" / "02 TPS计划转TOPAS输入"
    machine_data = (
        source_root / "pipeline" / "01 束流commission" / "machines" / machine
    )
    sources = {
        "energy_spectrum": conversion / "energySpectrum75.json",
        "phase_space": conversion / "phase.json",
        "number_per_mu": reference / "machine_47" / "beamQuality_47" / "beamModel" / "NF.txt",
        "measured_idd": machine_data / f"{machine}_PristineBraggPeaks.csv",
        "measured_spot_sigma": machine_data / f"{machine}_SpotSigma.csv",
        "energy_list": machine_data / "energy_list.txt",
    }
    missing = [path for path in sources.values() if not path.is_file()]
    if missing:
        raise RuntimeError("TOPAS_Test commissioning input(s) missing: " + ", ".join(map(str, missing)))

    destination = root / "machine_model" / "beam_commissioning" / safe_name(machine)
    pointer = destination.parent / "active_profile.json"
    profile_path = destination / "profile.json"
    if (destination.exists() or pointer.exists()) and not args.overwrite:
        raise RuntimeError(f"Commissioned beam profile already exists; add --overwrite: {destination}")
    destination.mkdir(parents=True, exist_ok=True)
    target_names = {
        "energy_spectrum": "energy_spectrum.json",
        "phase_space": "phase_space.json",
        "number_per_mu": "number_per_mu.txt",
        "measured_idd": "measured_pristine_bragg_peaks.csv",
        "measured_spot_sigma": "measured_spot_sigma.csv",
        "energy_list": "commissioned_energy_list.txt",
    }
    for key, source in sources.items():
        shutil.copy2(source, destination / target_names[key])

    profile = {
        "schema_version": 1,
        "model_kind": "measured_idd_discrete_spectrum_plus_fermi_eyges_emittance",
        "treatment_machine_name": machine,
        "source_plane_upstream_mm": config["source_plane_upstream_mm"],
        "expected_vsad_mm": config["expected_vsad_mm"],
        "vsad_tolerance_mm": 25.0,
        "water_mean_excitation_energy_ev": config["water_mean_excitation_energy_ev"],
        "units": {
            "phase_space_position_sigma": "mm",
            "phase_space_angular_sigma": "rad",
            "energy_spectrum": "total MeV per carbon ion"
        },
        "nozzle_wet_policy": "not_added; measured IDD-derived spectrum already includes upstream nozzle loss",
        "fluence_policy": "spot meterset multiplied by energy-dependent number-per-MU before relative history allocation",
        "phase_space_fit_validation": {
            "depth_mapping": "reverse_sigma_order",
            "maximum_rmse_mm": 0.25,
            "maximum_isocenter_error_mm": 0.25,
        },
        "files": {key: target_names[key] for key in target_names},
        "sha256": {
            key: sha256(destination / target_names[key])
            for key in target_names
        },
        "provenance": {
            "source_project": str(source_root),
            "reference_case": str(config["reference_case"]),
            "active_rtplan": str(plan_path),
            "active_rtplan_sop_instance_uid": str(getattr(plan, "SOPInstanceUID", "")),
            "imported_utc": datetime.now(timezone.utc).isoformat(),
            "method": (
                "TOPAS_Test IDD NNLS discrete-energy spectrum; Fermi-Eyges phase-space fit; "
                "DICOM VSAD spot-axis projection"
            ),
        },
        "limitations": [
            "Research QA model; not a substitute for independent institutional beam commissioning.",
            "Imported data are accepted only for an exact TreatmentMachineName match and VSAD tolerance.",
            "The referenced TOPAS_Test multi-plan report did not fully exclude residual beam-model mismatch.",
            "The TOPAS_Test phase-space fitter outputs position sigma in mm; this importer records and emits mm explicitly rather than copying the reference generator's cm label.",
            "MRF4 physical geometry and institution-specific CT HU calibration remain separate open items.",
        ],
    }
    profile_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # Re-open through the production loader so a bad import cannot be activated.
    model = CommissionedBeamModel(profile_path)
    model.validate_rtplan(machine, actual_vsad)
    calibration_path = destination / "particle_calibration.json"
    calibration = {
        "schema_version": 1,
        "calibration_kind": "machine_specific_energy_dependent_particle_number",
        "treatment_machine_name": machine,
        "commissioned_profile": {
            "file": profile_path.name,
            "sha256": sha256(profile_path),
            "fingerprint": model.fingerprint,
        },
        "number_per_mu": {
            "file": target_names["number_per_mu"],
            "sha256": sha256(model.nf_path),
            "units": "primary carbon ions per MU",
            "scope": "machine_specific_and_energy_dependent",
        },
        "dose_output_correction": {
            "factor": 1.0,
            "status": "identity_no_empirical_correction",
            "scope": "machine_beam_model_transport_and_measurement_protocol_specific",
        },
        "run_specific_quantities": {
            "planned_particles": "N_plan = sum_i(MU_i * NF_machine(E_i))",
            "simulated_histories": "N_sim = sum_i(AllocatedHistories_i)",
            "dose_scale": "N_plan / N_sim * dose_output_correction.factor",
        },
        "policy": [
            "NF(E) and any independently commissioned dose-output correction belong to this treatment machine profile.",
            "N_plan, N_sim and their ratio belong to one patient/run and must never be stored as machine constants.",
            "A non-identity output correction may be used only with traceable commissioning evidence.",
        ],
    }
    calibration_path.write_text(
        json.dumps(calibration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    # Re-open the binding through the production validator before activation.
    model.particle_calibration()
    pointer.write_text(
        json.dumps({"schema_version": 1, "profile": f"{destination.name}/profile.json"}, indent=2) + "\n",
        encoding="utf-8",
    )
    audit = model.phase_measurement_audit
    print("Imported commissioned TOPAS beam model")
    print(f"RTPLAN machine: {machine}")
    print(f"RTPLAN VSAD X/Y: {actual_vsad.tolist()} mm")
    print(f"Source plane: {model.source_plane_mm:g} mm upstream")
    print(
        "Phase-space measurement audit: "
        f"{audit['audited_energies']} energies, median/max RMSE "
        f"{audit['median_rmse_mm']:.6g}/{audit['maximum_rmse_mm']:.6g} mm"
    )
    print(f"Profile: {profile_path}")
    print(f"Fingerprint: {model.fingerprint}")
    print(f"Machine particle calibration: {calibration_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
