#!/usr/bin/env python3
"""Build an isolated, unapproved machine_3 commissioned-beam draft.

No GUI registry, existing commissioned model, or source data are modified.
The output contains a complete hash-locked profile, but is explicitly marked
unapproved and is kept outside the importable commissioned-model directory.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import sys
from typing import Any

import numpy as np
from scipy.optimize import least_squares, lsq_linear


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.utils.commissioned_beam import CommissionedBeamModel, sha256
from scripts.utils.water_phantom import parse_measured_idd


MACHINE_NAME = "lzRoom1_90_RF4_241230"
SOURCE_PLANE_UPSTREAM_MM = 680.0
SPECTRUM_WEIGHT_THRESHOLD = 0.01


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--source-root",
        type=Path,
        default=Path("/Users/jiangzhenmin/Desktop/configData/machine_3/beamQuality_3"),
    )
    parser.add_argument(
        "--kernel-dir",
        type=Path,
        default=Path(
            "/Users/jiangzhenmin/Desktop/TOPAS_Test/Hangzhou/1685/"
            "TOPAS计划验证/01 束流commission/I75eV_from_results5"
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "machine_model" / "drafts" / MACHINE_NAME,
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def require_file(path: Path) -> Path:
    result = path.expanduser().resolve()
    if not result.is_file():
        raise RuntimeError(f"Required input does not exist: {result}")
    return result


def r80_mm(depth: np.ndarray, dose: np.ndarray) -> float:
    peak = int(np.argmax(dose))
    target = float(dose[peak]) * 0.8
    for index in range(peak, len(dose) - 1):
        y0, y1 = float(dose[index]), float(dose[index + 1])
        if y0 >= target >= y1 and y0 != y1:
            x0, x1 = float(depth[index]), float(depth[index + 1])
            return x0 + (target - y0) * (x1 - x0) / (y1 - y0)
    return float("nan")


def load_kernels(kernel_dir: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Load available monoenergetic 1 mm-rebinned kernels via their mapping."""

    mapping = require_file(kernel_dir / "energy_mapping.txt")
    energies: list[float] = []
    curves: list[np.ndarray] = []
    for line in mapping.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            raise RuntimeError(f"Invalid kernel mapping row: {line!r}")
        index, total_mev = int(parts[0]), float(parts[1])
        path = kernel_dir / f"energy{index}.csv"
        if not path.is_file():
            continue
        values = np.atleast_2d(np.loadtxt(path, delimiter=",", comments="#", dtype=float))
        if values.shape[1] < 4 or values.shape[0] != 400:
            raise RuntimeError(f"Unexpected kernel shape in {path}: {values.shape}")
        if not np.isfinite(values[:, 3]).all() or np.any(values[:, 3] < 0):
            raise RuntimeError(f"Invalid dose column in {path}")
        energies.append(total_mev)
        curves.append(values[:, 3])
    if not curves:
        raise RuntimeError(f"No usable monoenergetic IDD kernels in {kernel_dir}")
    return np.arange(0.5, 400.0, 1.0), np.asarray(energies), np.column_stack(curves)


def fit_spectra(
    measured_idd: Path,
    kernel_depth: np.ndarray,
    kernel_energy_total: np.ndarray,
    kernel_idd: np.ndarray,
    strict_upper_bound: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, float]]]:
    spectra: list[dict[str, Any]] = []
    audits: list[dict[str, float]] = []
    for curve in parse_measured_idd(measured_idd):
        candidate = np.abs(kernel_energy_total - curve.nominal_total_mev) <= 240.0
        if strict_upper_bound:
            # A fitted incident spectrum cannot contain a primary-carbon
            # component above the nominal machine energy.  The old broad
            # window was useful for diagnostics but allowed implausible high-
            # energy terms to mimic an unmodelled fragment tail.
            candidate &= kernel_energy_total <= curve.nominal_total_mev + 1e-9
        if not np.any(candidate):
            raise RuntimeError(f"No nearby kernels for {curve.nominal_mevu:.2f} MeV/u")
        candidate_energy = kernel_energy_total[candidate]
        candidate_idd = kernel_idd[:, candidate]
        matrix = np.column_stack(
            [
                np.interp(curve.depth_mm, kernel_depth, candidate_idd[:, index], left=0.0, right=0.0)
                for index in range(candidate_idd.shape[1])
            ]
        )
        # Keep a compact, physically relevant basis.  The full 740-column
        # library is highly collinear; ranking by measured-curve correlation
        # avoids singular NNLS solutions while retaining spectral neighbours.
        if matrix.shape[1] > 32:
            score = np.asarray(
                [
                    float(np.dot(column, curve.dose_au))
                    / max(float(np.linalg.norm(column) * np.linalg.norm(curve.dose_au)), np.finfo(float).eps)
                    for column in matrix.T
                ]
            )
            keep = np.argsort(score)[-32:]
            keep.sort()
            matrix = matrix[:, keep]
            candidate_energy = candidate_energy[keep]
        maximum = float(matrix.max())
        if maximum <= 0 or not np.isfinite(maximum):
            raise RuntimeError(f"Kernel interpolation failed for {curve.nominal_mevu:.2f} MeV/u")
        matrix /= maximum
        target_dose = curve.dose_au / float(np.max(curve.dose_au))
        solution = lsq_linear(
            matrix,
            target_dose,
            bounds=(0.0, 100.0),
            method="trf",
            lsmr_tol="auto",
            max_iter=1000,
        )
        if not solution.success or not np.isfinite(solution.x).all():
            raise RuntimeError(f"Bounded non-negative fit failed for {curve.nominal_mevu:.2f} MeV/u: {solution.message}")
        coefficients = solution.x
        if float(coefficients.sum()) <= 0:
            raise RuntimeError(f"NNLS returned zero spectrum for {curve.nominal_mevu:.2f} MeV/u")
        weights = coefficients / float(coefficients.sum())
        selected = weights > SPECTRUM_WEIGHT_THRESHOLD
        if not np.any(selected):
            selected[int(np.argmax(weights))] = True
        selected_weights = weights[selected]
        selected_weights /= float(selected_weights.sum())
        with np.errstate(over="ignore", invalid="ignore", divide="ignore"):
            fitted = matrix[:, selected] @ selected_weights
        if not np.isfinite(fitted).all():
            raise RuntimeError(
                f"Non-finite spectrum reconstruction at {curve.nominal_mevu:.2f} MeV/u "
                f"(matrix={matrix[:, selected].shape}, finite={np.isfinite(matrix[:, selected]).all()}, "
                f"weights_minmax={selected_weights.min():.3g},{selected_weights.max():.3g})"
            )
        measured_norm = curve.dose_au / float(np.max(curve.dose_au))
        fitted_norm = fitted / float(np.max(fitted))
        residual = fitted_norm - measured_norm
        measured_r80 = r80_mm(curve.depth_mm, measured_norm)
        fitted_r80 = r80_mm(curve.depth_mm, fitted_norm)
        spectra.append(
            {
                "measEnergy": float(curve.nominal_total_mev),
                "energys": [float(value) for value in candidate_energy[selected]],
                "weights": [float(value) for value in selected_weights],
                "flag": "True",
            }
        )
        audits.append(
            {
                "energy_mevu": float(curve.nominal_mevu),
                "measured_r80_mm": measured_r80,
                "fitted_r80_mm": fitted_r80,
                "r80_delta_mm": fitted_r80 - measured_r80,
                "normalized_rmse": float(np.sqrt(np.mean(residual**2))),
                "normalized_max_abs_error": float(np.max(np.abs(residual))),
                "spectrum_components": float(np.count_nonzero(selected)),
                "spectrum_mean_mevu": float(
                    np.dot(candidate_energy[selected] / 12.0, selected_weights)
                ),
            }
        )
    return spectra, audits


def read_spot_sigma(path: Path) -> dict[float, list[tuple[float, float]]]:
    result: dict[float, list[tuple[float, float]]] = defaultdict(list)
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            result[float(row["Energy[MeV/u]"])].append(
                (float(row["Depth[mm]"]), float(row["Sigma[mm]"]))
            )
    if not result:
        raise RuntimeError(f"No usable spot-sigma measurements in {path}")
    return result


def fit_phase_space(spot_sigma: Path) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    phase: list[dict[str, float]] = []
    audits: list[dict[str, float]] = []
    source_z_mm = -SOURCE_PLANE_UPSTREAM_MM
    for energy, pairs in sorted(read_spot_sigma(spot_sigma).items()):
        pairs = sorted(pairs)
        depths = np.asarray([pair[0] for pair in pairs], dtype=float)
        # Retain TOPAS_Test's recorded measurement-axis convention.
        observed = np.asarray([pair[1] for pair in pairs], dtype=float)[::-1]
        delta = depths - source_z_mm

        def prediction(parameters: np.ndarray) -> np.ndarray:
            sigma0, sigma_prime = np.exp(parameters[:2])
            rho = np.tanh(parameters[2])
            variance = sigma0**2 + 2.0 * rho * sigma0 * sigma_prime * delta + sigma_prime**2 * delta**2
            return np.sqrt(np.maximum(variance, 0.0))

        slope = max(1e-6, (observed[0] - observed[-1]) / (delta[0] - delta[-1]))
        solution = least_squares(
            lambda parameters: prediction(parameters) - observed,
            [np.log(max(observed[-1], 0.05)), np.log(slope), 0.0],
            max_nfev=3000,
            xtol=1e-14,
            ftol=1e-14,
            gtol=1e-14,
        )
        sigma0, sigma_prime = [float(value) for value in np.exp(solution.x[:2])]
        rho = float(np.clip(np.tanh(solution.x[2]), -0.999999, 0.999999))
        fitted = np.sqrt(
            np.maximum(
                sigma0**2 + 2.0 * rho * sigma0 * sigma_prime * delta + sigma_prime**2 * delta**2,
                0.0,
            )
        )
        errors = fitted - observed
        iso_index = int(np.argmin(np.abs(depths)))
        phase.append(
            {
                "energy": float(energy),
                "x": sigma0,
                "y": sigma0,
                "xtheta": sigma_prime,
                "ytheta": sigma_prime,
                "xrelation": rho,
                "yrelation": rho,
            }
        )
        audits.append(
            {
                "energy_mevu": float(energy),
                "rmse_mm": float(np.sqrt(np.mean(errors**2))),
                "isocenter_error_mm": float(abs(errors[iso_index])),
                "sigma_source_mm": sigma0,
                "sigma_prime_rad": sigma_prime,
                "correlation": rho,
            }
        )
    return phase, audits


def machine_vsad(machine_data: Path) -> list[float]:
    payload = json.loads(machine_data.read_text(encoding="utf-8"))
    values = payload["beamQualityInfo"]["focalLengthsXY"]
    return [float(values["x"]), float(values["y"])]


def write_audit(
    output_dir: Path,
    spectrum_audit: list[dict[str, float]],
    phase_audit: list[dict[str, float]],
    kernel_dir: Path,
    kernel_energy_total: np.ndarray,
) -> dict[str, Any]:
    for filename, rows in (
        ("energy_spectrum_fit_audit.csv", spectrum_audit),
        ("phase_space_fit_audit.csv", phase_audit),
    ):
        with (output_dir / filename).open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return {
        "draft_status": "unapproved_not_imported_not_for_clinical_use",
        "machine_name": MACHINE_NAME,
        "kernel_library": {
            "path": str(kernel_dir),
            "sha256_energy_mapping": sha256(kernel_dir / "energy_mapping.txt"),
            "available_kernel_count": int(kernel_energy_total.size),
            "energy_range_mevu": [
                float(kernel_energy_total.min() / 12.0),
                float(kernel_energy_total.max() / 12.0),
            ],
            "depth_sampling_mm": 1.0,
            "water_mean_excitation_energy_ev": 75.0,
        },
        "spectrum_fit": {
            "curve_count": len(spectrum_audit),
            "median_normalized_rmse": float(np.median([row["normalized_rmse"] for row in spectrum_audit])),
            "maximum_normalized_rmse": float(np.max([row["normalized_rmse"] for row in spectrum_audit])),
            "median_r80_delta_mm": float(np.median([row["r80_delta_mm"] for row in spectrum_audit])),
            "maximum_abs_r80_delta_mm": float(np.max(np.abs([row["r80_delta_mm"] for row in spectrum_audit]))),
            "weight_threshold": SPECTRUM_WEIGHT_THRESHOLD,
        },
        "phase_space_fit": {
            "source_plane_upstream_mm": SOURCE_PLANE_UPSTREAM_MM,
            "depth_mapping": "reverse_sigma_order",
            "curve_count": len(phase_audit),
            "median_rmse_mm": float(np.median([row["rmse_mm"] for row in phase_audit])),
            "maximum_rmse_mm": float(np.max([row["rmse_mm"] for row in phase_audit])),
            "median_isocenter_error_mm": float(np.median([row["isocenter_error_mm"] for row in phase_audit])),
            "maximum_isocenter_error_mm": float(np.max([row["isocenter_error_mm"] for row in phase_audit])),
        },
    }


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.expanduser().resolve()
    if output_dir.exists():
        if not args.overwrite:
            raise RuntimeError(f"Output exists: {output_dir}; use --overwrite after review")
        if args.root.resolve() not in output_dir.parents:
            raise RuntimeError(f"Refusing to overwrite unsafe output directory: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)
    source = args.source_root.expanduser().resolve()
    idd_source = require_file(source / "ptRoom1PBS_RF4_241230_PristineBraggPeaks.csv")
    spot_source = require_file(source / "beamModel" / "ptRoom1PBS_RF4_241230_SpotSigma.csv")
    nf_source = require_file(source / "beamModel" / "NF.txt")
    machine_data = require_file(source / "ptRoom1PBS_RF4_241230_MachineData.json")
    kernel_dir = args.kernel_dir.expanduser().resolve()
    require_file(kernel_dir / "energy_mapping.txt")

    shutil.copy2(idd_source, output_dir / "measured_pristine_bragg_peaks.csv")
    shutil.copy2(spot_source, output_dir / "measured_spot_sigma.csv")
    shutil.copy2(nf_source, output_dir / "number_per_mu.txt")
    kernel_depth, kernel_energy_total, kernel_idd = load_kernels(kernel_dir)
    spectra, spectrum_audit = fit_spectra(
        output_dir / "measured_pristine_bragg_peaks.csv", kernel_depth, kernel_energy_total, kernel_idd
    )
    phase, phase_audit = fit_phase_space(output_dir / "measured_spot_sigma.csv")
    write_json(output_dir / "energy_spectrum.json", spectra)
    write_json(output_dir / "phase_space.json", phase)
    (output_dir / "commissioned_energy_list.txt").write_text(
        "".join(f"{row['energy']:.2f}\n" for row in phase), encoding="utf-8"
    )
    audit = write_audit(output_dir, spectrum_audit, phase_audit, kernel_dir, kernel_energy_total)
    write_json(output_dir / "commissioning_draft_audit.json", audit)

    files = {
        "energy_spectrum": "energy_spectrum.json",
        "phase_space": "phase_space.json",
        "number_per_mu": "number_per_mu.txt",
        "measured_idd": "measured_pristine_bragg_peaks.csv",
        "measured_spot_sigma": "measured_spot_sigma.csv",
        "energy_list": "commissioned_energy_list.txt",
    }
    hashes = {key: sha256(output_dir / filename) for key, filename in files.items()}
    profile = {
        "schema_version": 1,
        "model_kind": "measured_idd_discrete_spectrum_plus_constrained_fermi_eyges_emittance",
        "treatment_machine_name": MACHINE_NAME,
        "source_plane_upstream_mm": SOURCE_PLANE_UPSTREAM_MM,
        "expected_vsad_mm": machine_vsad(machine_data),
        "vsad_tolerance_mm": 25.0,
        "water_mean_excitation_energy_ev": 75.0,
        "units": {
            "phase_space_position_sigma": "mm",
            "phase_space_angular_sigma": "rad",
            "energy_spectrum": "total MeV per carbon ion",
        },
        "nozzle_wet_policy": "not_added; measured IDD-derived spectrum includes upstream beamline loss",
        "fluence_policy": "spot meterset multiplied by energy-dependent number-per-MU before relative history allocation",
        "phase_space_fit_validation": {
            "depth_mapping": "reverse_sigma_order",
            "maximum_rmse_mm": 0.25,
            "maximum_isocenter_error_mm": 0.25,
        },
        "files": files,
        "sha256": hashes,
        "provenance": {
            "source": "configData/machine_3/beamQuality_3",
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "method": "NNLS measured-IDT spectrum against ideal-water I=75eV kernel; constrained Fermi-Eyges SpotSigma fit",
        },
        "draft_status": "unapproved_not_imported_not_for_clinical_use",
        "limitations": [
            "TreatmentMachineName is provisional until verified against a future LanZhou RTPLAN.",
            "source_plane_upstream_mm=680 is a provisional fitting convention.",
            "NNLS kernels are 1 mm depth-rebinned; this is not a 0.5 mm commissioning fit.",
            "Not imported into the GUI and not approved for clinical use.",
        ],
    }
    write_json(output_dir / "profile.json", profile)

    # Profile validation also reprojects phase space onto every measured plane.
    model = CommissionedBeamModel(output_dir / "profile.json")
    calibration = {
        "schema_version": 1,
        "calibration_kind": "machine_specific_energy_dependent_particle_number",
        "treatment_machine_name": MACHINE_NAME,
        "commissioned_profile": {
            "file": "profile.json",
            "sha256": sha256(output_dir / "profile.json"),
            "fingerprint": model.fingerprint,
        },
        "number_per_mu": {
            "file": "number_per_mu.txt",
            "sha256": sha256(output_dir / "number_per_mu.txt"),
            "units": "primary carbon ions per MU",
            "scope": "machine_specific_and_energy_dependent",
        },
        "dose_output_correction": {
            "factor": 1.0,
            "status": "identity_no_empirical_correction",
            "scope": "machine_beam_model_transport_and_measurement_protocol_specific",
        },
        "draft_status": "unapproved_not_imported_not_for_clinical_use",
    }
    write_json(output_dir / "particle_calibration.json", calibration)
    manifest = {
        "schema_version": 1,
        "package_kind": "beam_commissioning",
        "package_version": "draft-machine3-20260825",
        "subject": {"treatment_machine_name": MACHINE_NAME},
        "files": {"profile": "profile.json", "particle_calibration": "particle_calibration.json", **files},
        "sha256": {
            "profile": sha256(output_dir / "profile.json"),
            "particle_calibration": sha256(output_dir / "particle_calibration.json"),
            **hashes,
        },
        "provenance": profile["provenance"],
        "approval": {
            "status": "draft_unapproved",
            "approved_by": "",
            "approved_at": "",
            "evidence": "Awaiting fit-audit review and LanZhou RTPLAN identity.",
        },
    }
    write_json(output_dir / "machine_package.json", manifest)
    final_model = CommissionedBeamModel(output_dir / "profile.json")
    final_model.particle_calibration()
    write_json(output_dir / "construction_validation.json", final_model.phase_measurement_audit)
    print(json.dumps({"output_dir": str(output_dir), **audit}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
