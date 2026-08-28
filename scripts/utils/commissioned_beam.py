"""Validated access to an imported machine-commissioned carbon beam model.

The model format intentionally keeps the measured/derived tables separate from
the patient plan.  A model is accepted only when the active RTPLAN machine name
matches exactly and its DICOM VSAD values agree with the commissioning record.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np


PARTICLE_CALIBRATION_FILENAME = "particle_calibration.json"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_positive(values: Iterable[float], label: str) -> np.ndarray:
    result = np.asarray(list(values), dtype=float)
    if result.size == 0 or not np.isfinite(result).all() or np.any(result <= 0):
        raise RuntimeError(f"Commissioned beam model has invalid {label}")
    return result


@dataclass(frozen=True)
class Spectrum:
    nominal_mevu: float
    total_energies_mev: np.ndarray
    weights: np.ndarray


@dataclass(frozen=True)
class PhaseSpace:
    nominal_mevu: float
    sigma_x_mm: float
    sigma_y_mm: float
    sigma_x_prime_rad: float
    sigma_y_prime_rad: float
    correlation_x: float
    correlation_y: float
    correlation_was_clamped: bool


@dataclass(frozen=True)
class MachineParticleCalibration:
    """Immutable binding between one machine profile and its dose calibration."""

    binding_path: Path
    binding_sha256: str
    treatment_machine_name: str
    profile_path: Path
    profile_sha256: str
    profile_fingerprint: str
    number_per_mu_path: Path
    number_per_mu_sha256: str
    dose_output_correction_factor: float
    dose_output_correction_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_file": str(self.binding_path),
            "binding_sha256": self.binding_sha256,
            "treatment_machine_name": self.treatment_machine_name,
            "profile_file": str(self.profile_path),
            "profile_sha256": self.profile_sha256,
            "profile_fingerprint": self.profile_fingerprint,
            "number_per_mu_file": str(self.number_per_mu_path),
            "number_per_mu_sha256": self.number_per_mu_sha256,
            "dose_output_correction_factor": self.dose_output_correction_factor,
            "dose_output_correction_status": self.dose_output_correction_status,
        }


class CommissionedBeamModel:
    """Machine model used by the commissioned TOPAS source generator."""

    def __init__(self, profile_path: Path):
        self.profile_path = profile_path.expanduser().resolve()
        if not self.profile_path.is_file():
            raise RuntimeError(f"Commissioned beam profile does not exist: {self.profile_path}")
        try:
            self.profile: dict[str, Any] = json.loads(
                self.profile_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot read commissioned beam profile: {self.profile_path}") from exc
        if int(self.profile.get("schema_version", 0)) != 1:
            raise RuntimeError("Unsupported commissioned beam profile schema")
        self.machine_name = str(self.profile.get("treatment_machine_name", "")).strip()
        if not self.machine_name:
            raise RuntimeError("Commissioned beam profile has no treatment_machine_name")
        self.source_plane_mm = float(self.profile.get("source_plane_upstream_mm", float("nan")))
        if not math.isfinite(self.source_plane_mm) or self.source_plane_mm <= 0:
            raise RuntimeError("Commissioned beam source_plane_upstream_mm must be positive")
        expected_vsad = np.asarray(self.profile.get("expected_vsad_mm", []), dtype=float)
        if expected_vsad.shape != (2,) or not np.isfinite(expected_vsad).all() or np.any(expected_vsad <= 0):
            raise RuntimeError("Commissioned beam expected_vsad_mm must contain positive X/Y values")
        self.expected_vsad_mm = expected_vsad
        self.vsad_tolerance_mm = float(self.profile.get("vsad_tolerance_mm", 25.0))
        if not math.isfinite(self.vsad_tolerance_mm) or self.vsad_tolerance_mm < 0:
            raise RuntimeError("Commissioned beam vsad_tolerance_mm is invalid")

        files = self.profile.get("files")
        if not isinstance(files, dict):
            raise RuntimeError("Commissioned beam profile files section is missing")
        self._file_paths = {
            key: self._resolve_file(files, key)
            for key in (
                "energy_spectrum",
                "phase_space",
                "number_per_mu",
                "measured_idd",
                "measured_spot_sigma",
                "energy_list",
            )
        }
        self.energy_path = self._file_paths["energy_spectrum"]
        self.phase_path = self._file_paths["phase_space"]
        self.nf_path = self._file_paths["number_per_mu"]
        self.measured_idd_path = self._file_paths["measured_idd"]
        self.measured_spot_sigma_path = self._file_paths["measured_spot_sigma"]
        self.energy_list_path = self._file_paths["energy_list"]
        self._verify_recorded_hashes()
        self._spectra = self._load_spectra()
        self._phase_rows = self._load_phase()
        self._nf_rows = self._load_nf()
        self.phase_measurement_audit = self._audit_phase_measurements()

    def _resolve_file(self, files: dict[str, Any], key: str) -> Path:
        value = str(files.get(key, "")).strip()
        if not value:
            raise RuntimeError(f"Commissioned beam profile file '{key}' is missing")
        path = (self.profile_path.parent / value).resolve()
        if not path.is_file():
            raise RuntimeError(f"Commissioned beam file does not exist: {path}")
        return path

    def _verify_recorded_hashes(self) -> None:
        recorded = self.profile.get("sha256", {})
        if not isinstance(recorded, dict):
            raise RuntimeError("Commissioned beam profile sha256 section is missing")
        for key, path in self._file_paths.items():
            expected = str(recorded.get(key, "")).lower()
            actual = sha256(path)
            if not expected or actual != expected:
                raise RuntimeError(
                    f"Commissioned beam file hash mismatch for {key}: expected {expected or 'MISSING'}, got {actual}"
                )

    def _load_spectra(self) -> dict[float, Spectrum]:
        try:
            rows = json.loads(self.energy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot read energy spectrum: {self.energy_path}") from exc
        if not isinstance(rows, list) or not rows:
            raise RuntimeError("Commissioned energy spectrum is empty")
        result: dict[float, Spectrum] = {}
        for row in rows:
            nominal_total = float(row["measEnergy"])
            nominal = nominal_total / 12.0
            energies = _finite_positive(row["energys"], "spectrum energy values")
            weights = np.asarray(row["weights"], dtype=float)
            if energies.shape != weights.shape or not np.isfinite(weights).all() or np.any(weights < 0):
                raise RuntimeError(f"Invalid commissioned spectrum at {nominal:.6g} MeV/u")
            weight_sum = float(weights.sum())
            if weight_sum <= 0:
                raise RuntimeError(f"Zero commissioned spectrum weight at {nominal:.6g} MeV/u")
            weights = weights / weight_sum
            if nominal in result:
                raise RuntimeError(f"Duplicate commissioned spectrum energy: {nominal:.6g} MeV/u")
            result[nominal] = Spectrum(nominal, energies, weights)
        return result

    def _load_phase(self) -> np.ndarray:
        try:
            rows = json.loads(self.phase_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot read phase-space model: {self.phase_path}") from exc
        values = np.asarray(
            [
                [
                    float(row["energy"]), float(row["x"]), float(row["y"]),
                    float(row["xtheta"]), float(row["ytheta"]),
                    float(row["xrelation"]), float(row["yrelation"]),
                ]
                for row in rows
            ],
            dtype=float,
        )
        if values.ndim != 2 or values.shape[1] != 7 or not np.isfinite(values).all():
            raise RuntimeError("Commissioned phase-space table is invalid")
        values = values[np.argsort(values[:, 0])]
        if np.any(np.diff(values[:, 0]) <= 0) or np.any(values[:, 1:5] <= 0):
            raise RuntimeError("Commissioned phase-space energies/sigmas are invalid")
        return values

    def _load_nf(self) -> np.ndarray:
        try:
            values = np.loadtxt(self.nf_path, dtype=float)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Cannot read number-per-MU table: {self.nf_path}") from exc
        values = np.atleast_2d(values)
        if values.shape[1] < 2 or not np.isfinite(values[:, :2]).all():
            raise RuntimeError("Commissioned number-per-MU table is invalid")
        values = values[:, :2]
        values = values[np.argsort(values[:, 0])]
        if np.any(np.diff(values[:, 0]) <= 0) or np.any(values[:, 1] <= 0):
            raise RuntimeError("Commissioned number-per-MU energies/factors are invalid")
        return values

    def _read_spot_measurements(self) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """Read the measurement evidence without assuming comma versus tab delimiters."""

        try:
            text = self.measured_spot_sigma_path.read_text(encoding="utf-8-sig")
            dialect = csv.Sniffer().sniff(text[:8192], delimiters=",\t;")
            reader = csv.DictReader(text.splitlines(), dialect=dialect)
        except (OSError, csv.Error) as exc:
            raise RuntimeError(
                f"Cannot read commissioned spot-sigma measurements: {self.measured_spot_sigma_path}"
            ) from exc
        fields = list(reader.fieldnames or [])
        selected: dict[str, str] = {}
        for field in fields:
            normalized = field.strip().lower().replace("_", "")
            if "energy" in normalized:
                selected.setdefault("energy", field)
            elif "depth" in normalized:
                selected.setdefault("depth", field)
            elif "sigmax" in normalized and "fit" not in normalized:
                selected.setdefault("sigma_x", field)
            elif "sigmay" in normalized and "fit" not in normalized:
                selected.setdefault("sigma_y", field)
            elif "sigma" in normalized and "fit" not in normalized:
                selected.setdefault("sigma", field)
        if not {"energy", "depth"}.issubset(selected) or not (
            "sigma" in selected or {"sigma_x", "sigma_y"}.issubset(selected)
        ):
            raise RuntimeError(
                "Commissioned spot-sigma measurements need Energy, Depth and Sigma (or SigmaX/SigmaY) columns"
            )
        values: list[tuple[float, float, float, float]] = []
        try:
            for row in reader:
                sigma_x = float(row[selected.get("sigma_x", selected.get("sigma", ""))])
                sigma_y = float(row[selected.get("sigma_y", selected.get("sigma", ""))])
                values.append(
                    (float(row[selected["energy"]]), float(row[selected["depth"]]), sigma_x, sigma_y)
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Commissioned spot-sigma measurements contain invalid numeric data") from exc
        array = np.asarray(values, dtype=float)
        if array.ndim != 2 or array.shape[1] != 4 or not np.isfinite(array).all():
            raise RuntimeError("Commissioned spot-sigma measurement table is empty or invalid")
        if np.any(array[:, 2:] <= 0):
            raise RuntimeError("Commissioned spot-sigma measurements must be positive")
        return array[:, 0], array[:, 1], array[:, 2], array[:, 3]

    def _audit_phase_measurements(self) -> dict[str, float | int | str]:
        """Back-propagate the imported Fermi-Eyges state to its measurement planes.

        TOPAS_Test reverses the sigma samples while retaining the sorted depth
        coordinates because its measurement depth axis points opposite to the
        TOPAS source-to-patient axis.  Recording and testing this convention
        makes the otherwise easy-to-miss sign/units assumption explicit.
        """

        validation = self.profile.get("phase_space_fit_validation", {})
        if not isinstance(validation, dict):
            raise RuntimeError("Commissioned phase_space_fit_validation must be an object")
        depth_mapping = str(validation.get("depth_mapping", "reverse_sigma_order"))
        if depth_mapping != "reverse_sigma_order":
            raise RuntimeError(f"Unsupported commissioned phase-space depth mapping: {depth_mapping}")
        energy, depth, sigma_x, sigma_y = self._read_spot_measurements()
        rmse_values: list[float] = []
        isocenter_errors: list[float] = []
        audited_energies = 0
        z0 = -self.source_plane_mm
        for phase_row in self._phase_rows:
            nominal = float(phase_row[0])
            mask = np.isclose(energy, nominal, atol=1e-6, rtol=0.0)
            if not np.any(mask):
                raise RuntimeError(
                    f"No measured spot-sigma evidence for phase-space energy {nominal:.6g} MeV/u"
                )
            order = np.argsort(depth[mask])
            depths = depth[mask][order]
            measured_axes = (sigma_x[mask][order][::-1], sigma_y[mask][order][::-1])
            delta = depths - z0
            for axis, measured in enumerate(measured_axes):
                sigma0 = float(phase_row[1 + axis])
                sigma_prime = float(phase_row[3 + axis])
                correlation = float(phase_row[5 + axis])
                variance = (
                    sigma0**2
                    + 2.0 * correlation * sigma0 * sigma_prime * delta
                    + sigma_prime**2 * delta**2
                )
                if np.any(variance < -1e-8):
                    raise RuntimeError(
                        f"Commissioned phase-space gives negative propagated variance at {nominal:.6g} MeV/u"
                    )
                predicted = np.sqrt(np.maximum(variance, 0.0))
                rmse_values.append(float(np.sqrt(np.mean((predicted - measured) ** 2))))
                iso_index = int(np.argmin(np.abs(depths)))
                isocenter_errors.append(float(abs(predicted[iso_index] - measured[iso_index])))
            audited_energies += 1
        maximum_rmse = max(rmse_values)
        maximum_isocenter_error = max(isocenter_errors)
        rmse_limit = float(validation.get("maximum_rmse_mm", 0.25))
        isocenter_limit = float(validation.get("maximum_isocenter_error_mm", 0.25))
        if maximum_rmse > rmse_limit or maximum_isocenter_error > isocenter_limit:
            raise RuntimeError(
                "Commissioned phase-space does not reproduce its measured spot sigma: "
                f"max RMSE={maximum_rmse:.6g} mm (limit {rmse_limit:g}), "
                f"max isocenter error={maximum_isocenter_error:.6g} mm (limit {isocenter_limit:g})"
            )
        return {
            "depth_mapping": depth_mapping,
            "audited_energies": audited_energies,
            "median_rmse_mm": float(np.median(rmse_values)),
            "maximum_rmse_mm": maximum_rmse,
            "median_isocenter_error_mm": float(np.median(isocenter_errors)),
            "maximum_isocenter_error_mm": maximum_isocenter_error,
        }

    @property
    def input_paths(self) -> tuple[Path, ...]:
        return self.profile_path, *self._file_paths.values()

    @property
    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for path in self.input_paths:
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def validate_rtplan(self, treatment_machine_name: str, vsad_mm: Iterable[float]) -> np.ndarray:
        if treatment_machine_name.strip() != self.machine_name:
            raise RuntimeError(
                "Commissioned beam model machine mismatch: "
                f"RTPLAN={treatment_machine_name!r}, profile={self.machine_name!r}"
            )
        actual = np.asarray(list(vsad_mm), dtype=float)
        if actual.shape != (2,) or not np.isfinite(actual).all() or np.any(actual <= 0):
            raise RuntimeError("RTPLAN VirtualSourceAxisDistances must contain positive X/Y values")
        delta = np.abs(actual - self.expected_vsad_mm)
        if np.any(delta > self.vsad_tolerance_mm):
            raise RuntimeError(
                "RTPLAN VSAD does not match commissioned beam model: "
                f"actual={actual.tolist()} mm, expected={self.expected_vsad_mm.tolist()} mm, "
                f"tolerance={self.vsad_tolerance_mm:g} mm"
            )
        return actual

    def spectrum(self, nominal_mevu: float, tolerance_mevu: float = 0.02) -> Spectrum:
        energies = np.asarray(list(self._spectra), dtype=float)
        index = int(np.argmin(np.abs(energies - nominal_mevu)))
        matched = float(energies[index])
        if abs(matched - nominal_mevu) > tolerance_mevu:
            raise RuntimeError(
                f"No commissioned discrete spectrum for {nominal_mevu:.6g} MeV/u; "
                f"nearest is {matched:.6g} MeV/u (no extrapolation allowed)"
            )
        return self._spectra[matched]

    def phase(self, nominal_mevu: float) -> PhaseSpace:
        energy = self._phase_rows[:, 0]
        if nominal_mevu < energy[0] or nominal_mevu > energy[-1]:
            raise RuntimeError(
                f"Energy {nominal_mevu:.6g} MeV/u is outside commissioned phase-space range "
                f"{energy[0]:.6g}..{energy[-1]:.6g} MeV/u"
            )
        values = [float(np.interp(nominal_mevu, energy, self._phase_rows[:, index])) for index in range(1, 7)]
        raw_corr_x, raw_corr_y = values[4], values[5]
        corr_x = float(np.clip(raw_corr_x, -0.999999, 0.999999))
        corr_y = float(np.clip(raw_corr_y, -0.999999, 0.999999))
        return PhaseSpace(
            nominal_mevu=nominal_mevu,
            sigma_x_mm=values[0], sigma_y_mm=values[1],
            sigma_x_prime_rad=values[2], sigma_y_prime_rad=values[3],
            correlation_x=corr_x, correlation_y=corr_y,
            correlation_was_clamped=(corr_x != raw_corr_x or corr_y != raw_corr_y),
        )

    def number_per_mu(self, nominal_mevu: float) -> float:
        energy = self._nf_rows[:, 0]
        if nominal_mevu < energy[0] or nominal_mevu > energy[-1]:
            raise RuntimeError(
                f"Energy {nominal_mevu:.6g} MeV/u is outside commissioned number-per-MU range "
                f"{energy[0]:.6g}..{energy[-1]:.6g} MeV/u"
            )
        return float(np.interp(nominal_mevu, energy, self._nf_rows[:, 1]))

    def particle_calibration(self) -> MachineParticleCalibration:
        """Load and strictly validate the machine-specific calibration binding.

        The binding is deliberately kept beside, rather than inside, profile.json.
        This avoids a recursive fingerprint while still making the profile, NF(E)
        table and optional output correction an inseparable audited unit.
        """

        binding_path = self.profile_path.parent / PARTICLE_CALIBRATION_FILENAME
        if not binding_path.is_file():
            raise RuntimeError(
                f"Machine particle-calibration binding is missing: {binding_path}"
            )
        try:
            payload = json.loads(binding_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Cannot read machine particle-calibration binding: {binding_path}"
            ) from exc
        if int(payload.get("schema_version", 0)) != 1:
            raise RuntimeError("Unsupported machine particle-calibration binding schema")
        binding_machine = str(payload.get("treatment_machine_name", "")).strip()
        if binding_machine != self.machine_name:
            raise RuntimeError(
                "Machine particle-calibration binding mismatch: "
                f"profile={self.machine_name!r}, binding={binding_machine!r}"
            )
        profile_record = payload.get("commissioned_profile", {})
        number_record = payload.get("number_per_mu", {})
        correction_record = payload.get("dose_output_correction", {})
        if not all(isinstance(item, dict) for item in (profile_record, number_record, correction_record)):
            raise RuntimeError("Machine particle-calibration binding sections are invalid")
        recorded_profile = (binding_path.parent / str(profile_record.get("file", ""))).resolve()
        recorded_nf = (binding_path.parent / str(number_record.get("file", ""))).resolve()
        if recorded_profile != self.profile_path or recorded_nf != self.nf_path:
            raise RuntimeError(
                "Machine particle-calibration binding points to a different profile or NF(E) table"
            )
        actual_profile_sha = sha256(self.profile_path)
        actual_nf_sha = sha256(self.nf_path)
        expected_profile_sha = str(profile_record.get("sha256", "")).lower()
        expected_fingerprint = str(profile_record.get("fingerprint", "")).lower()
        expected_nf_sha = str(number_record.get("sha256", "")).lower()
        if actual_profile_sha != expected_profile_sha:
            raise RuntimeError(
                "Machine calibration profile hash mismatch: "
                f"expected {expected_profile_sha or 'MISSING'}, got {actual_profile_sha}"
            )
        if self.fingerprint != expected_fingerprint:
            raise RuntimeError(
                "Machine calibration profile fingerprint mismatch: "
                f"expected {expected_fingerprint or 'MISSING'}, got {self.fingerprint}"
            )
        if actual_nf_sha != expected_nf_sha:
            raise RuntimeError(
                "Machine calibration NF(E) hash mismatch: "
                f"expected {expected_nf_sha or 'MISSING'}, got {actual_nf_sha}"
            )
        factor = float(correction_record.get("factor", float("nan")))
        status = str(correction_record.get("status", "")).strip()
        if not math.isfinite(factor) or factor <= 0.0 or not status:
            raise RuntimeError("Machine dose-output correction factor/status is invalid")
        if not math.isclose(factor, 1.0) and status != "commissioned_with_traceable_evidence":
            raise RuntimeError(
                "A non-identity machine dose-output correction requires status "
                "'commissioned_with_traceable_evidence'"
            )
        return MachineParticleCalibration(
            binding_path=binding_path.resolve(),
            binding_sha256=sha256(binding_path),
            treatment_machine_name=self.machine_name,
            profile_path=self.profile_path,
            profile_sha256=actual_profile_sha,
            profile_fingerprint=self.fingerprint,
            number_per_mu_path=self.nf_path,
            number_per_mu_sha256=actual_nf_sha,
            dose_output_correction_factor=factor,
            dose_output_correction_status=status,
        )


def _profiles_for_machine(root: Path, treatment_machine_name: str) -> list[Path]:
    base = root.resolve() / "machine_model" / "beam_commissioning"
    matches: list[Path] = []
    for path in sorted(base.glob("*/profile.json")):
        if _registered_profile_active(root, path) is False:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(payload.get("treatment_machine_name", "")).strip() == treatment_machine_name.strip():
            matches.append(path.resolve())
    # A standard import may register the same physical content that previously
    # existed as a legacy folder. Prefer the registered immutable copy so this
    # migration does not create a false "multiple versions" ambiguity.
    by_fingerprint: dict[str, Path] = {}
    for path in matches:
        try:
            fingerprint = CommissionedBeamModel(path).fingerprint
        except RuntimeError:
            fingerprint = str(path)
        current = by_fingerprint.get(fingerprint)
        if current is None or (
            _registered_profile_active(root, current) is None
            and _registered_profile_active(root, path) is not None
        ):
            by_fingerprint[fingerprint] = path
    return sorted(by_fingerprint.values())


def _registered_profile_active(root: Path, profile: Path) -> bool | None:
    """Return registry state while treating pre-registry profiles as legacy-active."""
    registry_path = root.resolve() / "machine_model" / "model_registry.json"
    if not registry_path.is_file():
        return None
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in payload.get("entries", []) if isinstance(payload, dict) else []:
        if not isinstance(entry, dict) or not entry.get("profile"):
            continue
        recorded = (root.resolve() / str(entry["profile"])).resolve()
        if recorded == profile.expanduser().resolve():
            return bool(entry.get("active", True))
    return None


def resolve_profile(
    root: Path,
    explicit: Path | None = None,
    treatment_machine_name: str | None = None,
) -> Path:
    if explicit is not None:
        selected = explicit.expanduser().resolve()
        if _registered_profile_active(root, selected) is False:
            raise RuntimeError(f"Selected commissioned beam profile is deactivated: {selected}")
        return selected
    machine_name = str(treatment_machine_name or "").strip()
    if machine_name:
        matches = _profiles_for_machine(root, machine_name)
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise RuntimeError(
                f"No commissioned beam profile matches RTPLAN TreatmentMachineName {machine_name!r}"
            )
        raise RuntimeError(
            f"Multiple commissioned profiles match RTPLAN machine {machine_name!r}; "
            "select one explicitly with --beam-model-profile: "
            + ", ".join(str(path) for path in matches)
        )
    pointer = root.resolve() / "machine_model" / "beam_commissioning" / "active_profile.json"
    if not pointer.is_file():
        raise RuntimeError(
            "No active commissioned beam profile. Import one with "
            "scripts/13_import_topas_test_beam_model.py or select RTPLAN baseline mode."
        )
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        value = str(payload["profile"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Invalid active commissioned beam profile pointer: {pointer}") from exc
    return (pointer.parent / value).resolve()


def load_commissioned_model(
    root: Path,
    explicit: Path | None = None,
    treatment_machine_name: str | None = None,
) -> CommissionedBeamModel:
    return CommissionedBeamModel(resolve_profile(root, explicit, treatment_machine_name))
