"""Independent TOPAS dose calibration from planned and simulated particles.

The commissioned beam model stores the planned primary-particle basis for
every spot in ``spot_history_allocation.csv``.  TOPAS transports a much smaller
number of histories, so its DoseToMedium grid must be multiplied by

    sum(planned spot particles) / sum(simulated spot histories)
    * machine dose-output correction

before it is compared with TPS dose in Gy.  This module deliberately never
changes the source TOPAS binary; consumers apply the audited scale in memory.
"""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import math
from pathlib import Path
from typing import Any, Optional

try:
    from .commissioned_beam import (
        CommissionedBeamModel,
        MachineParticleCalibration,
        load_commissioned_model,
        sha256,
    )
except ImportError:  # Direct script utility import (scripts/ on sys.path).
    from scripts.utils.commissioned_beam import (
        CommissionedBeamModel,
        MachineParticleCalibration,
        load_commissioned_model,
        sha256,
    )


PROTOCOL = "machine-bound commissioned calibration: N_plan / N_sim * C_machine"


@dataclass(frozen=True)
class MCDoseCalibration:
    available: bool
    protocol: str
    scale: float
    planned_particles: float
    simulated_histories: int
    allocation_file: str
    beam_input_mode: str
    beam_model_mode: str
    selected_spots: int
    positive_spots: int
    allocation_l1_fraction: float
    preliminary_low_statistics: bool
    treatment_machine_name: str
    commissioned_profile: str
    commissioned_profile_fingerprint: str
    number_per_mu_file: str
    number_per_mu_sha256: str
    machine_calibration_binding: str
    machine_calibration_binding_sha256: str
    machine_dose_output_correction_factor: float
    machine_calibration_verified: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _unavailable(reason: str, allocation: Optional[Path] = None) -> MCDoseCalibration:
    return MCDoseCalibration(
        available=False,
        protocol=PROTOCOL,
        scale=1.0,
        planned_particles=0.0,
        simulated_histories=0,
        allocation_file=str(allocation) if allocation else "",
        beam_input_mode="",
        beam_model_mode="",
        selected_spots=0,
        positive_spots=0,
        allocation_l1_fraction=math.nan,
        preliminary_low_statistics=True,
        treatment_machine_name="",
        commissioned_profile="",
        commissioned_profile_fingerprint="",
        number_per_mu_file="",
        number_per_mu_sha256="",
        machine_calibration_binding="",
        machine_calibration_binding_sha256="",
        machine_dose_output_correction_factor=1.0,
        machine_calibration_verified=False,
        reason=reason,
    )


def find_allocation_file(root: Path, mc_path: Path) -> Optional[Path]:
    """Resolve the allocation snapshot belonging to a current or cached binary."""
    root = root.expanduser().resolve()
    mc_path = mc_path.expanduser().resolve()
    production = root / "topas_output" / "production"
    try:
        mc_path.relative_to(production)
    except ValueError:
        pass
    else:
        candidate = root / "plan_parsed" / "spot_history_allocation.csv"
        return candidate if candidate.is_file() else None

    # Standard cache layout:
    # archived-.../dose/result.bin + archived-.../configuration/allocation.csv
    for parent in mc_path.parents:
        candidate = parent / "configuration" / "spot_history_allocation.csv"
        if candidate.is_file():
            return candidate.resolve()
        if parent == root:
            break

    # This also supports a manually copied binary packaged with its snapshot.
    for candidate in (
        mc_path.parent / "spot_history_allocation.csv",
        mc_path.parent.parent / "configuration" / "spot_history_allocation.csv",
    ):
        if candidate.is_file():
            return candidate.resolve()
    return None


def _summary_values(allocation: Path) -> dict[str, str]:
    summary = allocation.parent / "topas_plan_generation_summary.txt"
    if not summary.is_file():
        return {}
    result: dict[str, str] = {}
    for line in summary.read_text(encoding="utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        result[key.strip()] = value.strip()
    return result


def _load_machine_binding(
    root: Path,
    allocation: Path,
) -> tuple[CommissionedBeamModel, MachineParticleCalibration, str]:
    """Validate the allocation sidecar (or a legacy generation summary)."""

    metadata_path = allocation.with_name(allocation.stem + "_metadata.json")
    if metadata_path.is_file():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Allocation machine-binding metadata is unreadable: {metadata_path}") from exc
        if int(metadata.get("schema_version", 0)) != 1:
            raise RuntimeError("Unsupported allocation machine-binding metadata schema")
        if str(metadata.get("allocation_sha256", "")).lower() != sha256(allocation):
            raise RuntimeError("Allocation SHA-256 does not match its machine-binding metadata")
        if str(metadata.get("beam_input_mode", "")).upper() != "RTPLAN" or str(
            metadata.get("beam_model_mode", "")
        ).upper() != "COMMISSIONED":
            raise RuntimeError("Allocation metadata is not an RTPLAN/COMMISSIONED run")
        recorded = metadata.get("machine_calibration")
        if not isinstance(recorded, dict):
            raise RuntimeError("Allocation metadata has no machine calibration binding")
        machine = str(recorded.get("treatment_machine_name", "")).strip()
        profile_value = str(recorded.get("profile_file", "")).strip()
        explicit_profile = Path(profile_value).expanduser() if profile_value else None
        if explicit_profile is not None and not explicit_profile.is_file():
            snapshot_profile = allocation.parent / "machine_model" / "profile.json"
            explicit_profile = snapshot_profile if snapshot_profile.is_file() else None
        model = load_commissioned_model(root, explicit_profile, machine)
        binding = model.particle_calibration()
        checks = {
            "treatment_machine_name": binding.treatment_machine_name,
            "profile_sha256": binding.profile_sha256,
            "profile_fingerprint": binding.profile_fingerprint,
            "number_per_mu_sha256": binding.number_per_mu_sha256,
            "binding_sha256": binding.binding_sha256,
        }
        for key, actual in checks.items():
            if str(recorded.get(key, "")).lower() != str(actual).lower():
                raise RuntimeError(
                    f"Allocation machine calibration {key} does not match the installed machine profile"
                )
        recorded_factor = float(recorded.get("dose_output_correction_factor", float("nan")))
        if not math.isclose(
            recorded_factor,
            binding.dose_output_correction_factor,
            rel_tol=0.0,
            abs_tol=1e-15,
        ):
            raise RuntimeError("Allocation machine dose-output correction factor has changed")
        return model, binding, str(metadata_path.resolve())

    # Legacy allocations predate the JSON sidecar.  Their generation summary
    # still records the exact machine and full commissioned fingerprint.
    summary = _summary_values(allocation)
    machine = summary.get("Treatment machine", "").strip()
    fingerprint = summary.get("Commissioned fingerprint", "").strip().lower()
    profile_value = summary.get("Commissioned profile", "").strip()
    if not machine or not fingerprint:
        raise RuntimeError(
            "No allocation machine-binding metadata or legacy machine/fingerprint summary was found"
        )
    explicit_profile = Path(profile_value).expanduser() if profile_value else None
    if explicit_profile is not None and not explicit_profile.is_file():
        snapshot_profile = allocation.parent / "machine_model" / "profile.json"
        explicit_profile = snapshot_profile if snapshot_profile.is_file() else None
    model = load_commissioned_model(root, explicit_profile, machine)
    if model.fingerprint.lower() != fingerprint:
        raise RuntimeError(
            "Legacy allocation commissioned fingerprint does not match the installed machine profile"
        )
    binding = model.particle_calibration()
    return model, binding, "legacy topas_plan_generation_summary.txt"


def _validate_machine_particle_rows(
    rows: list[dict[str, str]],
    model: CommissionedBeamModel,
) -> None:
    required = {"Energy_MeVu", "MetersetWeight_MU", "CommissionedNumberPerMU"}
    missing = sorted(required.difference(rows[0]))
    if missing:
        raise RuntimeError(
            "The allocation lacks machine-calibration columns: " + ", ".join(missing)
        )
    cache: dict[float, float] = {}
    try:
        for row in rows:
            energy = float(row["Energy_MeVu"])
            meterset = float(row["MetersetWeight_MU"])
            recorded_nf = float(row["CommissionedNumberPerMU"])
            allocation_basis = float(row["AllocationBasis"])
            if not all(math.isfinite(value) for value in (energy, meterset, recorded_nf, allocation_basis)):
                raise ValueError
            if energy not in cache:
                cache[energy] = model.number_per_mu(energy)
            expected_nf = cache[energy]
            if not math.isclose(recorded_nf, expected_nf, rel_tol=5e-9, abs_tol=1e-8):
                raise RuntimeError(
                    f"Spot NF(E) does not match machine {model.machine_name!r} at {energy:.10g} MeV/u"
                )
            if not math.isclose(
                allocation_basis,
                meterset * expected_nf,
                rel_tol=5e-9,
                abs_tol=1e-7,
            ):
                raise RuntimeError(
                    f"Spot allocation basis is not MU * machine NF(E) at {energy:.10g} MeV/u"
                )
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Allocation machine-calibration columns contain invalid values") from exc


def resolve_particle_calibration(root: Path, mc_path: Path) -> MCDoseCalibration:
    """Return the independent particle-number scale for one TOPAS binary.

    An unavailable result is returned instead of silently falling back to a
    TPS-peak fit.  Callers that require an absolute comparison should use
    :func:`require_particle_calibration`.
    """
    root = root.expanduser().resolve()
    mc_path = mc_path.expanduser().resolve()
    allocation = find_allocation_file(root, mc_path)
    if allocation is None:
        return _unavailable(
            "No run-specific spot_history_allocation.csv was found for this MC binary"
        )

    production_allocation = (root / "plan_parsed" / "spot_history_allocation.csv").resolve()
    if allocation.resolve() == production_allocation and mc_path.is_file():
        # A newly regenerated plan paired with an older production binary would
        # produce a plausible but wrong scale.  Refuse that stale combination.
        if allocation.stat().st_mtime_ns > mc_path.stat().st_mtime_ns:
            return _unavailable(
                "The current spot allocation is newer than the MC binary; select its archived run snapshot",
                allocation,
            )

    with allocation.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    if not rows:
        return _unavailable("The spot allocation file is empty", allocation)

    required = {"AllocationBasis", "AllocatedHistories", "BeamInputMode", "BeamModelMode"}
    missing = sorted(required.difference(rows[0]))
    if missing:
        return _unavailable(
            "The spot allocation lacks required columns: " + ", ".join(missing), allocation
        )

    input_modes = {str(row.get("BeamInputMode", "")).strip().upper() for row in rows}
    model_modes = {str(row.get("BeamModelMode", "")).strip().upper() for row in rows}
    if input_modes != {"RTPLAN"} or model_modes != {"COMMISSIONED"}:
        return _unavailable(
            "Independent N_plan/N_sim calibration requires an RTPLAN run using the COMMISSIONED beam model",
            allocation,
        )

    try:
        model, machine_binding, _binding_source = _load_machine_binding(root, allocation)
        _validate_machine_particle_rows(rows, model)
    except RuntimeError as exc:
        return _unavailable(str(exc), allocation)

    planned: list[float] = []
    histories: list[int] = []
    try:
        for row in rows:
            planned_value = float(row["AllocationBasis"])
            history_value = int(row["AllocatedHistories"])
            if not math.isfinite(planned_value) or planned_value < 0.0 or history_value < 0:
                raise ValueError
            planned.append(planned_value)
            histories.append(history_value)
    except (TypeError, ValueError):
        return _unavailable("The allocation contains invalid particle or history values", allocation)

    planned_particles = math.fsum(planned)
    simulated_histories = sum(histories)
    positive_spots = sum(value > 0.0 for value in planned)
    if planned_particles <= 0.0 or simulated_histories <= 0:
        return _unavailable("Planned particles and simulated histories must both be positive", allocation)
    if any(particle > 0.0 and history == 0 for particle, history in zip(planned, histories)):
        return _unavailable(
            "At least one positive-weight planned spot has zero simulated histories", allocation
        )

    planned_fraction = [value / planned_particles for value in planned]
    simulated_fraction = [value / simulated_histories for value in histories]
    allocation_l1 = math.fsum(
        abs(left - right) for left, right in zip(planned_fraction, simulated_fraction)
    )
    # A median below about ten histories per positive spot is plainly a
    # low-statistics reconstruction even when the global output is calibrated.
    preliminary = simulated_histories < 10 * max(1, positive_spots)
    return MCDoseCalibration(
        available=True,
        protocol=PROTOCOL,
        scale=(
            planned_particles
            / simulated_histories
            * machine_binding.dose_output_correction_factor
        ),
        planned_particles=planned_particles,
        simulated_histories=simulated_histories,
        allocation_file=str(allocation.resolve()),
        beam_input_mode="RTPLAN",
        beam_model_mode="COMMISSIONED",
        selected_spots=len(rows),
        positive_spots=positive_spots,
        allocation_l1_fraction=allocation_l1,
        preliminary_low_statistics=preliminary,
        treatment_machine_name=machine_binding.treatment_machine_name,
        commissioned_profile=str(machine_binding.profile_path),
        commissioned_profile_fingerprint=machine_binding.profile_fingerprint,
        number_per_mu_file=str(machine_binding.number_per_mu_path),
        number_per_mu_sha256=machine_binding.number_per_mu_sha256,
        machine_calibration_binding=str(machine_binding.binding_path),
        machine_calibration_binding_sha256=machine_binding.binding_sha256,
        machine_dose_output_correction_factor=(
            machine_binding.dose_output_correction_factor
        ),
        machine_calibration_verified=True,
        reason="",
    )


def require_particle_calibration(root: Path, mc_path: Path) -> MCDoseCalibration:
    calibration = resolve_particle_calibration(root, mc_path)
    if not calibration.available:
        raise RuntimeError(
            "Independent MC dose calibration is unavailable: "
            + calibration.reason
            + ". Rebuild/run with the COMMISSIONED beam model; TPS-peak fitting is not used as a fallback."
        )
    return calibration


def read_dicom_calibration(dicom_path: Path) -> dict[str, Any]:
    """Read the audit sidecar of an exported MC RTDOSE, when present."""
    audit_path = dicom_path.with_suffix(".json")
    if not audit_path.is_file():
        return {
            "available": False,
            "absolute_dose_calibrated": False,
            "reason": "No calibration audit sidecar accompanies this MC RTDOSE",
        }
    try:
        payload = json.loads(audit_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {
            "available": False,
            "absolute_dose_calibrated": False,
            "reason": "The MC RTDOSE calibration audit sidecar is unreadable",
        }
    available = bool(payload.get("absolute_dose_calibrated"))
    return {
        **payload,
        "available": available,
        "reason": "" if available else str(
            payload.get("description", "The MC RTDOSE is not particle-number calibrated")
        ),
    }


def write_calibration_audit(
    calibration: MCDoseCalibration,
    mc_path: Path,
    destination: Path,
) -> Path:
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source_mc": str(mc_path.expanduser().resolve()),
        "source_binary_modified_ns": mc_path.stat().st_mtime_ns,
        "absolute_dose_calibrated": calibration.available,
        "calibration": calibration.to_dict(),
        "important_note": (
            "Particle-number-calibrated physical-dose estimate for research/QA. "
            "No TPS-dose fitting or empirical 0.976 correction was applied."
        ),
    }
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)
    return destination
