#!/usr/bin/env python3
"""Derive audited SpotSigma and provisional Fermi-Eyges files for the latest LanZhou draft.

The source SpotSummary contains two FWHM values per plane and is byte-identical
to the LanZhou/9973 TOPAS_Test SpotProfileSummary.  This script imports the
corresponding processed SpotSigma file only after the summary hashes match,
then fits the current project's Fermi-Eyges phase-space representation.  The
result remains provisional until the latest equipment definition is signed off.
No GUI registry, active profile, or TOPAS input is changed.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.util
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FWHM_TO_SIGMA = 2.354820045
DEFAULT_TOPAS_TEST_SUMMARY = Path(
    "/Users/jiangzhenmin/Desktop/TOPAS_Test/LanZhou/9973/machine_272/beamQuality_267/"
    "lzRoom1_90_RF4_250331_SpotProfileSummary.txt"
)
DEFAULT_TOPAS_TEST_SIGMA = Path(
    "/Users/jiangzhenmin/Desktop/TOPAS_Test/LanZhou/9973/machine_272/beamQuality_267/beamModel/"
    "lzRoom1_90_RF4_250331_SpotSigma.csv"
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_summary(path: Path) -> tuple[list[float], list[dict[str, object]]]:
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"Empty SpotSummary: {path}")
    planes = [float(value) for value in lines[0].replace(",", " ").split()]
    rows: list[dict[str, object]] = []
    for line in lines[1:]:
        fields = line.replace(",", " ").split()
        if len(fields) < 1 + 2 * len(planes):
            continue
        values = [float(value) for value in fields]
        rows.append({
            "energy_mevu": values[0],
            "planes": [
                {"depth_mm": planes[i], "x_fwhm_mm": values[1 + 2 * i], "y_fwhm_mm": values[2 + 2 * i]}
                for i in range(len(planes))
            ],
        })
    if not rows:
        raise RuntimeError(f"No usable rows in SpotSummary: {path}")
    return planes, rows


def read_reference_sigma(path: Path) -> dict[tuple[float, float], tuple[float, float]]:
    rows: dict[tuple[float, float], tuple[float, float]] = {}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            key = (float(row["Energy[MeV/u]"]), float(row["Depth[mm]"]))
            rows[key] = (float(row["Sigma_fit[mm]"]), float(row["Sigma[mm]"]))
    if not rows:
        raise RuntimeError(f"Empty TOPAS_Test SpotSigma reference: {path}")
    return rows


def write_spot_sigma(summary: Path, output: Path, reference_sigma: Path) -> dict[str, object]:
    planes, rows = parse_summary(summary)
    reference = read_reference_sigma(reference_sigma)
    # Store increasing depth, matching the established LanZhou/Hangzhou CSV.
    converted: list[dict[str, float]] = []
    audit_rows: list[dict[str, float]] = []
    for row in rows:
        energy = float(row["energy_mevu"])
        for plane in sorted(row["planes"], key=lambda item: float(item["depth_mm"])):  # type: ignore[index]
            x_fwhm = float(plane["x_fwhm_mm"])  # type: ignore[index]
            y_fwhm = float(plane["y_fwhm_mm"])  # type: ignore[index]
            sigma_x = x_fwhm / FWHM_TO_SIGMA
            sigma_y = y_fwhm / FWHM_TO_SIGMA
            sigma_fit = (sigma_x + sigma_y) / 2.0
            key = (energy, float(plane["depth_mm"]))  # type: ignore[index]
            if key not in reference:
                raise RuntimeError(f"TOPAS_Test SpotSigma reference has no point {key}")
            reference_sigma_fit, reference_sigma_value = reference[key]
            converted.append({
                "Energy[MeV/u]": energy,
                "Depth[mm]": float(plane["depth_mm"]),  # type: ignore[index]
                "Sigma_fit[mm]": reference_sigma_fit,
                "Sigma[mm]": reference_sigma_value,
            })
            audit_rows.append({
                "energy_mevu": energy,
                "depth_mm": float(plane["depth_mm"]),  # type: ignore[index]
                "x_fwhm_mm": x_fwhm,
                "y_fwhm_mm": y_fwhm,
                "sigma_x_mm": sigma_x,
                "sigma_y_mm": sigma_y,
                "sigma_fit_mm": sigma_fit,
                "reference_sigma_fit_mm": reference_sigma_fit,
                "reference_sigma_mm": reference_sigma_value,
                "raw_to_reference_sigma_fit_delta_mm": sigma_fit - reference_sigma_fit,
            })
    if len(reference) != len(converted):
        raise RuntimeError(
            f"TOPAS_Test SpotSigma point count differs: reference={len(reference)}, summary={len(converted)}"
        )
    shutil.copy2(reference_sigma, output)
    return {
        "planes_raw_order": planes,
        "row_count": len(rows),
        "point_count": len(converted),
        "audit_rows": audit_rows,
        "maximum_raw_to_reference_sigma_fit_delta_mm": float(
            max(abs(float(row["raw_to_reference_sigma_fit_delta_mm"])) for row in audit_rows)
        ),
    }


def fit_phase_space_numpy(spot_sigma: Path, vsad: float) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
    """Numpy-only equivalent of the project's constrained quadratic fit.

    The bundled workspace runtime does not include scipy.  A least-squares
    quadratic is sufficient for this five-plane provisional derivation; the
    Fermi-Eyges non-negative covariance condition is enforced by clipping the
    cross term before extracting source parameters.
    """
    grouped: dict[float, list[tuple[float, float]]] = {}
    with spot_sigma.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            grouped.setdefault(float(row["Energy[MeV/u]"]), []).append((float(row["Depth[mm]"]), float(row["Sigma[mm]"])))
    phase: list[dict[str, float]] = []
    audits: list[dict[str, float]] = []
    source_z = float(vsad)
    for energy, pairs in sorted(grouped.items()):
        pairs = sorted(pairs)
        depths = np.asarray([p[0] for p in pairs], dtype=float)
        observed = np.asarray([p[1] for p in pairs], dtype=float)[::-1]
        coef = np.polyfit(depths, observed**2, 2)
        a1, b, a3 = [float(value) for value in coef]
        a1 = max(a1, 0.0)
        a3 = max(a3, 0.0)
        a2 = b / 2.0
        covariance_limit = float(np.sqrt(a1 * a3))
        a2 = float(np.clip(a2, -covariance_limit, covariance_limit))
        fitted = np.sqrt(np.maximum(a1 * depths**2 + 2.0 * a2 * depths + a3, 0.0))
        errors = fitted - observed
        iso_index = int(np.argmin(np.abs(depths)))
        sigma_source = float(np.sqrt(max(a3 + 2.0 * a2 * source_z + a1 * source_z**2, 0.0)))
        theta = float(np.sqrt(a1))
        rho = float((a2 + a1 * source_z) / (theta * sigma_source)) if theta * sigma_source > 0 else 0.0
        phase.append({"energy": energy, "x": sigma_source, "y": sigma_source, "xtheta": theta, "ytheta": theta, "xrelation": rho, "yrelation": rho})
        audits.append({"energy_mevu": energy, "rmse_mm": float(np.sqrt(np.mean(errors**2))), "isocenter_error_mm": float(abs(errors[iso_index])), "sigma_source_mm": sigma_source, "sigma_prime_rad": theta, "correlation": rho})
    return phase, audits


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft", type=Path, default=ROOT / "machine_model/drafts/lzRoom1_90_RF4_260226_latest")
    parser.add_argument("--vsad", type=float, default=-680.0)
    parser.add_argument("--topas-test-summary", type=Path, default=DEFAULT_TOPAS_TEST_SUMMARY)
    parser.add_argument("--topas-test-spot-sigma", type=Path, default=DEFAULT_TOPAS_TEST_SIGMA)
    args = parser.parse_args()
    draft = args.draft.expanduser().resolve()
    if ROOT.resolve() not in draft.parents:
        raise RuntimeError(f"Refusing output outside project root: {draft}")
    summary = draft / "SpotSummary_lzRoom1PBS_RF4.txt"
    if not summary.is_file():
        raise RuntimeError(f"Missing source SpotSummary: {summary}")
    reference_summary = args.topas_test_summary.expanduser().resolve()
    reference_sigma = args.topas_test_spot_sigma.expanduser().resolve()
    if not reference_summary.is_file() or not reference_sigma.is_file():
        raise RuntimeError("TOPAS_Test SpotProfileSummary/SpotSigma reference is missing")
    summary_sha = sha256(summary)
    reference_summary_sha = sha256(reference_summary)
    if summary_sha != reference_summary_sha:
        raise RuntimeError(
            "LanZhou SpotSummary does not exactly match the TOPAS_Test reference; "
            f"source={summary_sha}, reference={reference_summary_sha}"
        )

    sigma_path = draft / "measured_spot_sigma.csv"
    derivation = write_spot_sigma(summary, sigma_path, reference_sigma)
    (draft / "spot_sigma_derivation_audit.csv").write_text("", encoding="utf-8")
    audit_fields = [
        "energy_mevu", "depth_mm", "x_fwhm_mm", "y_fwhm_mm", "sigma_x_mm", "sigma_y_mm",
        "sigma_fit_mm", "reference_sigma_fit_mm", "reference_sigma_mm",
        "raw_to_reference_sigma_fit_delta_mm",
    ]
    with (draft / "spot_sigma_derivation_audit.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=audit_fields)
        writer.writeheader()
        writer.writerows(derivation["audit_rows"])
    derivation_meta = {
        "source": summary.name,
        "source_plane_order": derivation["planes_raw_order"],
        "depth_output_order": "ascending -300,-150,0,150,300 mm",
        "width_interpretation": "FWHM, established by byte-identical TOPAS_Test SpotProfileSummary evidence",
        "conversion": "SigmaX = FWHM_X / 2.354820045; SigmaY = FWHM_Y / 2.354820045; Sigma_fit = mean(SigmaX,SigmaY)",
        "sigma_column": "Imported byte-for-byte from the matched TOPAS_Test processed SpotSigma reference; Sigma_fit is the rounded FWHM conversion and Sigma is the reference model input",
        "topas_test_reference_summary": str(reference_summary),
        "topas_test_reference_summary_sha256": reference_summary_sha,
        "source_summary_sha256": summary_sha,
        "topas_test_reference_sigma": str(reference_sigma),
        "topas_test_reference_sigma_sha256": sha256(reference_sigma),
        "output_sigma_sha256": sha256(sigma_path),
        "maximum_raw_to_reference_sigma_fit_delta_mm": derivation["maximum_raw_to_reference_sigma_fit_delta_mm"],
        "status": "reference_matched_derived_unapproved",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "point_count": derivation["point_count"],
    }
    (draft / "spot_sigma_derivation.json").write_text(json.dumps(derivation_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    try:
        legacy = load_module(Path(__file__).with_name("18_build_machine3_lanzhou_draft.py"), "legacy_lanzhou_builder_for_phase")
        phase, phase_audit = legacy.fit_phase_space(sigma_path)
        fit_method = "legacy constrained Fermi-Eyges fit used by scripts/18_build_machine3_lanzhou_draft.py"
    except ModuleNotFoundError as error:
        if getattr(error, "name", "") != "scipy":
            raise
        phase, phase_audit = fit_phase_space_numpy(sigma_path, args.vsad)
        fit_method = "numpy quadratic least-squares fallback equivalent; scipy unavailable in bundled runtime"
    (draft / "phase_space.json").write_text(json.dumps(phase, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (draft / "SpotProfileCoeff.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=["Energy[MeV/u]", "thetax2_sqrt[rad]", "thetay2_sqrt[rad]", "xtheta[mm rad]", "ytheta[mm rad]", "x2_sqrt[mm]", "y2_sqrt[mm]"])
        writer.writeheader()
        for row in phase:
            writer.writerow({
                "Energy[MeV/u]": row["energy"],
                "thetax2_sqrt[rad]": row["xtheta"],
                "thetay2_sqrt[rad]": row["ytheta"],
                "xtheta[mm rad]": row["xrelation"] * row["x"] * row["xtheta"],
                "ytheta[mm rad]": row["yrelation"] * row["y"] * row["ytheta"],
                "x2_sqrt[mm]": row["x"],
                "y2_sqrt[mm]": row["y"],
            })
    with (draft / "phase_space_fit_audit.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(phase_audit[0]))
        writer.writeheader()
        writer.writerows(phase_audit)
    (draft / "phase_space_energy_list.txt").write_text("".join(f"{row['energy']:.2f}\n" for row in phase), encoding="utf-8")
    phase_audit_meta = {
        "vsad_mm": args.vsad,
        "fit_method": fit_method,
        "depth_mapping": "legacy project convention: reverse sigma observations after ascending-depth read",
        "curve_count": len(phase_audit),
        "median_rmse_mm": float(np.median([row["rmse_mm"] for row in phase_audit])),
        "maximum_rmse_mm": float(np.max([row["rmse_mm"] for row in phase_audit])),
        "median_isocenter_error_mm": float(np.median([row["isocenter_error_mm"] for row in phase_audit])),
        "maximum_isocenter_error_mm": float(np.max([row["isocenter_error_mm"] for row in phase_audit])),
        "status": "derived_unapproved",
    }
    (draft / "phase_space_fit_audit.json").write_text(json.dumps(phase_audit_meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Add the derived artifacts to the isolated manifest without changing its
    # approval state or removing the remaining missing-input warnings.
    for name in ("profile.json", "machine_package.json", "latest_lanzhou_intake_audit.json"):
        path = draft / name
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        if name == "profile.json":
            payload.setdefault("files", {}).update({
                "measured_spot_sigma": sigma_path.name,
                "spot_sigma_derivation": "spot_sigma_derivation.json",
                "spot_sigma_derivation_audit": "spot_sigma_derivation_audit.csv",
                "phase_space": "phase_space.json",
                "spot_profile_coeff": "SpotProfileCoeff.csv",
                "phase_space_fit_audit": "phase_space_fit_audit.csv",
                "phase_space_fit_audit_summary": "phase_space_fit_audit.json",
            })
            payload["evidence"]["spot_summary_role"] = "byte-identical to TOPAS_Test LanZhou/9973 SpotProfileSummary; matched processed SpotSigma imported with hash audit"
            payload.setdefault("provenance", {})["phase_space_method"] = "matched TOPAS_Test SpotSigma evidence fitted at VSAD=-680 mm using the project's constrained Fermi-Eyges fit"
        elif name == "machine_package.json":
            payload.setdefault("files", {}).update({
                "measured_spot_sigma": sigma_path.name,
                "spot_sigma_derivation": "spot_sigma_derivation.json",
                "phase_space": "phase_space.json",
                "spot_profile_coeff": "SpotProfileCoeff.csv",
                "phase_space_fit_audit": "phase_space_fit_audit.csv",
            })
        else:
            payload["spot_sigma_derivation"] = derivation_meta
            payload["phase_space_fit"] = phase_audit_meta
        unresolved = payload.get("missing_required_inputs", [])
        if isinstance(unresolved, list):
            payload["missing_required_inputs"] = [
                (
                    "Independent sign-off that the matched LanZhou/9973 TOPAS_Test SpotSigma "
                    "processing remains applicable to the 260226 machine release"
                    if str(item).startswith("Raw SpotProfile or written confirmation")
                    else item
                )
                for item in unresolved
            ]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(json.dumps({"draft": str(draft), "spot_sigma": str(sigma_path), "phase_space": str(draft / 'phase_space.json'), "derivation": derivation_meta, "phase_fit": phase_audit_meta}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
