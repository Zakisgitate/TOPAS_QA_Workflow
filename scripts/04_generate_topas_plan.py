#!/usr/bin/env python3
"""Generate a TOPAS PBS plan from parsed RT Ion Plan scanning spots.

The source ``plan_parsed/spots.csv`` is never modified.  Baseline histories use
relative meterset weights.  Commissioned histories use the RTPLAN machine's
audited energy-dependent primary-particles-per-MU table; absolute dose scaling
is still performed downstream from the run-specific N_plan/N_sim ratio.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Iterable, Sequence

import numpy as np
import pandas as pd
import pydicom


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from scripts.utils.commissioned_beam import CommissionedBeamModel, load_commissioned_model


FWHM_TO_SIGMA = 1.0 / (2.0 * math.sqrt(2.0 * math.log(2.0)))
CARBON_MASS_NUMBER = 12
REQUIRED_COLUMNS = (
    "BeamNumber",
    "BeamName",
    "LayerIndex",
    "ControlPointIndex",
    "Energy_MeVu",
    "SpotIndex",
    "X_mm",
    "Y_mm",
    "MetersetWeight_MU",
    "RelativeWeight",
    "PlanRelativeWeight",
    "NumberOfPaintings",
    "FWHM_X_mm",
    "FWHM_Y_mm",
)


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root, help=f"Project root (default: {root})")
    parser.add_argument("--input", type=Path, help="Input spots.csv")
    parser.add_argument("--output", type=Path, help="Generated TOPAS plan")
    parser.add_argument("--allocation-output", type=Path, help="Per-spot history allocation CSV")
    parser.add_argument("--summary-output", type=Path, help="Generation audit summary")
    parser.add_argument(
        "--total-histories",
        type=int,
        default=100_000,
        help="Relative-weight Monte Carlo histories (default: 100000)",
    )
    parser.add_argument(
        "--beam-input-mode",
        choices=("rtplan", "manual"),
        default="rtplan",
        help="Use RTPLAN energy/spots or one manually defined research spot (default: rtplan)",
    )
    parser.add_argument(
        "--beam-model-mode",
        choices=("baseline", "commissioned"),
        default="baseline",
        help="Uncommissioned RTPLAN baseline or imported machine-commissioned source (default: baseline)",
    )
    parser.add_argument(
        "--beam-model-profile",
        type=Path,
        help=(
            "Commissioned profile.json override (default: uniquely match RTPLAN "
            "TreatmentMachineName in machine_model/beam_commissioning)"
        ),
    )
    parser.add_argument("--manual-energy-mevu", type=float, help="Manual carbon-ion energy in MeV/u")
    parser.add_argument("--manual-spot-x-mm", type=float, help="Manual IEC spot X at isocenter in mm")
    parser.add_argument("--manual-spot-y-mm", type=float, help="Manual IEC spot Y at isocenter in mm")
    parser.add_argument("--manual-spot-fwhm-x-mm", type=float, help="Manual IEC spot FWHM X in mm")
    parser.add_argument("--manual-spot-fwhm-y-mm", type=float, help="Manual IEC spot FWHM Y in mm")
    parser.add_argument(
        "--energy-scale",
        type=float,
        default=1.0,
        help="Research override multiplying every nominal MeV/u value (default: 1)",
    )
    parser.add_argument(
        "--energy-offset-mevu",
        type=float,
        default=0.0,
        help="Research override added after energy scaling, in MeV/u (default: 0)",
    )
    parser.add_argument(
        "--spot-size-scale",
        type=float,
        default=1.0,
        help="Research override multiplying DICOM spot FWHM (default: 1)",
    )
    parser.add_argument(
        "--energy-spread-percent",
        type=float,
        default=0.0,
        help="TOPAS BeamEnergySpread percent, uncommissioned unless measured (default: 0)",
    )
    parser.add_argument(
        "--layer-indices",
        help="Optional comma-separated 1-based LayerIndex subset, e.g. 1,27,53",
    )
    parser.add_argument(
        "--spots-per-layer",
        type=int,
        help="Optional first N spots from each selected layer (validation only)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace existing derived outputs; never modifies DICOM or spots.csv",
    )
    return parser.parse_args()


def parse_layer_indices(value: str | None) -> list[int] | None:
    if value is None:
        return None
    try:
        result = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise RuntimeError("--layer-indices must be comma-separated integers") from exc
    if not result or any(item <= 0 for item in result) or len(result) != len(set(result)):
        raise RuntimeError("--layer-indices must contain unique positive integers")
    return result


def resolve_paths(args: argparse.Namespace) -> tuple[Path, Path, Path, Path]:
    root = args.root.resolve()
    input_path = (args.input or root / "plan_parsed" / "spots.csv").resolve()
    output = (args.output or root / "topas" / "beam" / "plan_generated.txt").resolve()
    allocation = (
        args.allocation_output or root / "plan_parsed" / "spot_history_allocation.csv"
    ).resolve()
    summary = (
        args.summary_output or root / "plan_parsed" / "topas_plan_generation_summary.txt"
    ).resolve()
    return input_path, output, allocation, summary


def ensure_safe_outputs(
    input_path: Path,
    outputs: Sequence[Path],
    root: Path,
    overwrite: bool,
) -> None:
    dicom_root = (root / "dicom").resolve()
    if not input_path.is_file():
        raise RuntimeError(f"Input spot table does not exist: {input_path}")
    for path in outputs:
        if path == input_path:
            raise RuntimeError(f"Refusing to overwrite input table: {path}")
        try:
            path.relative_to(dicom_root)
        except ValueError:
            pass
        else:
            raise RuntimeError(f"Derived output must not be inside read-only DICOM tree: {path}")
        if path.exists() and not overwrite:
            raise RuntimeError(f"Derived output exists: {path}; inspect it or add --overwrite")
        path.parent.mkdir(parents=True, exist_ok=True)


def load_and_validate(path: Path) -> pd.DataFrame:
    table = pd.read_csv(path)
    missing = [name for name in REQUIRED_COLUMNS if name not in table.columns]
    if missing:
        raise RuntimeError(f"Input is missing required column(s): {', '.join(missing)}")
    if table.empty:
        raise RuntimeError("Input spot table is empty")

    numeric = [name for name in REQUIRED_COLUMNS if name != "BeamName"]
    if not np.isfinite(table[numeric].to_numpy(dtype=float)).all():
        raise RuntimeError("Input contains a non-finite required numeric value")
    if (table["MetersetWeight_MU"] <= 0).any():
        raise RuntimeError("All emitted spots must have positive MetersetWeight_MU")
    if (table[["FWHM_X_mm", "FWHM_Y_mm"]] <= 0).any().any():
        raise RuntimeError("DICOM scanning spot FWHM must be positive")
    if (table["NumberOfPaintings"] != 1).any():
        raise RuntimeError("This first-stage generator requires NumberOfPaintings == 1")
    if table["BeamNumber"].nunique() != 1:
        raise RuntimeError("This first-stage generator supports one beam per generated file")

    expected_order = table.sort_values(
        ["BeamNumber", "LayerIndex", "SpotIndex"], kind="stable"
    ).index.to_numpy()
    if not np.array_equal(expected_order, table.index.to_numpy()):
        raise RuntimeError("Input rows are not in Beam/Layer/Spot delivery order")
    if table.duplicated(["BeamNumber", "LayerIndex", "SpotIndex"]).any():
        raise RuntimeError("Input contains duplicate BeamNumber/LayerIndex/SpotIndex keys")
    return table


def select_rows(
    table: pd.DataFrame,
    layer_indices: list[int] | None,
    spots_per_layer: int | None,
) -> pd.DataFrame:
    result = table
    if layer_indices is not None:
        available = set(int(item) for item in table["LayerIndex"].unique())
        absent = [item for item in layer_indices if item not in available]
        if absent:
            raise RuntimeError(f"Requested LayerIndex value(s) not present: {absent}")
        order = {layer: position for position, layer in enumerate(layer_indices)}
        result = table[table["LayerIndex"].isin(layer_indices)].copy()
        result["_selection_order"] = result["LayerIndex"].map(order)
        result = result.sort_values(["_selection_order", "SpotIndex"], kind="stable").drop(
            columns="_selection_order"
        )
    if spots_per_layer is not None:
        if spots_per_layer <= 0:
            raise RuntimeError("--spots-per-layer must be positive")
        result = result.groupby("LayerIndex", sort=False, group_keys=False).head(spots_per_layer)
    if result.empty:
        raise RuntimeError("Spot selection is empty")
    return result.reset_index(drop=True)


def build_manual_spot(full_table: pd.DataFrame, args: argparse.Namespace) -> pd.DataFrame:
    values = {
        "--manual-energy-mevu": args.manual_energy_mevu,
        "--manual-spot-x-mm": args.manual_spot_x_mm,
        "--manual-spot-y-mm": args.manual_spot_y_mm,
        "--manual-spot-fwhm-x-mm": args.manual_spot_fwhm_x_mm,
        "--manual-spot-fwhm-y-mm": args.manual_spot_fwhm_y_mm,
    }
    missing = [name for name, value in values.items() if value is None]
    if missing:
        raise RuntimeError("Manual beam mode requires: " + ", ".join(missing))
    if not all(math.isfinite(float(value)) for value in values.values()):
        raise RuntimeError("Manual beam values must be finite")
    if not 1.0 <= float(args.manual_energy_mevu) <= 500.0:
        raise RuntimeError("--manual-energy-mevu must be within [1, 500]")
    for name in ("manual_spot_x_mm", "manual_spot_y_mm"):
        if not -500.0 <= float(getattr(args, name)) <= 500.0:
            raise RuntimeError(f"--{name.replace('_', '-')} must be within [-500, 500]")
    for name in ("manual_spot_fwhm_x_mm", "manual_spot_fwhm_y_mm"):
        if not 0.01 <= float(getattr(args, name)) <= 200.0:
            raise RuntimeError(f"--{name.replace('_', '-')} must be within [0.01, 200]")
    if args.layer_indices is not None or args.spots_per_layer is not None:
        raise RuntimeError("Manual beam mode cannot be combined with RTPLAN layer/spot selection")
    if not (
        math.isclose(args.energy_scale, 1.0)
        and math.isclose(args.energy_offset_mevu, 0.0)
        and math.isclose(args.spot_size_scale, 1.0)
    ):
        raise RuntimeError("Manual beam mode cannot be combined with RTPLAN energy/spot-size overrides")

    # Retain the current RTPLAN beam identity and geometry linkage, but replace all
    # delivery values with an explicitly audited one-spot research beam.
    manual = full_table.iloc[[0]].copy()
    manual["BeamName"] = "MANUAL_SINGLE_SPOT"
    manual.loc[:, "LayerIndex"] = 0
    manual.loc[:, "ControlPointIndex"] = 0
    manual.loc[:, "Energy_MeVu"] = float(args.manual_energy_mevu)
    manual.loc[:, "SpotIndex"] = 0
    manual.loc[:, "X_mm"] = float(args.manual_spot_x_mm)
    manual.loc[:, "Y_mm"] = float(args.manual_spot_y_mm)
    manual.loc[:, "MetersetWeight_MU"] = 1.0
    manual.loc[:, "RelativeWeight"] = 1.0
    manual.loc[:, "PlanRelativeWeight"] = 1.0
    manual.loc[:, "NumberOfPaintings"] = 1
    manual.loc[:, "FWHM_X_mm"] = float(args.manual_spot_fwhm_x_mm)
    manual.loc[:, "FWHM_Y_mm"] = float(args.manual_spot_fwhm_y_mm)
    if "WeightPerPainting_MU" in manual.columns:
        manual.loc[:, "WeightPerPainting_MU"] = 1.0
    return manual.reset_index(drop=True)


def allocate_histories(weights: np.ndarray, total_histories: int) -> np.ndarray:
    if total_histories <= 0:
        raise RuntimeError("--total-histories must be positive")
    weights = np.asarray(weights, dtype=np.float64)
    if not np.isfinite(weights).all() or np.any(weights <= 0):
        raise RuntimeError("History-allocation weights must be finite and positive")
    if total_histories < len(weights):
        # Sparse test run: there is no room for one primary per spot, so fall
        # through to the same largest-remainder tail the normal path ends with.
        # Every quota is below 1, which hands the single primary to the
        # heaviest spots and zero to the rest. The caller is responsible for
        # dropping the zero-history spots and marking the run as a test.
        quotas = total_histories * weights / weights.sum()
        allocated = np.zeros(len(weights), dtype=np.int64)
        order = np.argsort(-quotas, kind="stable")
        allocated[order[:total_histories]] = 1
        return allocated
    # Lower-bounded proportional allocation: clamp only the spots whose ideal
    # share is below one, then re-scale the still-free spots. This is much less
    # biased than adding one history to every spot before proportional division.
    free = np.ones(len(weights), dtype=bool)
    quotas = np.ones(len(weights), dtype=np.float64)
    while True:
        remaining = total_histories - int((~free).sum())
        scale = remaining / float(weights[free].sum())
        newly_clamped = free & (scale * weights < 1.0)
        if not newly_clamped.any():
            quotas[free] = scale * weights[free]
            break
        free[newly_clamped] = False
    allocated = np.floor(quotas).astype(np.int64)
    remainder = int(total_histories - allocated.sum())
    if remainder:
        order = np.argsort(-(quotas - allocated), kind="stable")
        allocated[order[:remainder]] += 1
    if int(allocated.sum()) != total_histories:
        raise AssertionError("Largest-remainder allocation did not preserve the total")
    return allocated


def fmt_float(value: float) -> str:
    return f"{value:.10g}"


def vector_line(prefix: str, values: Iterable[object], unit: str | None = None) -> str:
    items = list(values)
    suffix = f" {unit}" if unit else ""
    return f"{prefix} = {len(items)} " + " ".join(str(item) for item in items) + suffix


def time_feature(name: str, values: Sequence[object], kind: str, unit: str | None) -> list[str]:
    count = len(values)
    times = [f"{index}." for index in range(1, count + 1)]
    return [
        f's:Tf/{name}/Function = "Step"',
        vector_line(f"dv:Tf/{name}/Times", times, "s"),
        vector_line(f"{kind}v:Tf/{name}/Values", values, unit),
        "",
    ]


def discover_plan_beam(root: Path):
    candidates = []
    for path in sorted((root / "dicom" / "RTPLAN").glob("*.dcm")):
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        if hasattr(ds, "IonBeamSequence"):
            candidates.append((path.resolve(), ds))
    if len(candidates) != 1:
        raise RuntimeError(f"Expected one active RT Ion Plan, found {len(candidates)}")
    path, plan = candidates[0]
    beams = list(plan.IonBeamSequence)
    if len(beams) != 1:
        raise RuntimeError("Commissioned source generation currently requires exactly one beam")
    return path, plan, beams[0]


def commissioned_spot_audit(
    table: pd.DataFrame,
    model: CommissionedBeamModel,
    vsad_mm: np.ndarray,
) -> dict[str, np.ndarray]:
    """Project RTPLAN spots to the commissioned source plane and verify each ray."""

    target_x = table["Y_mm"].to_numpy(dtype=float)  # local X is IEC Y
    target_y = table["X_mm"].to_numpy(dtype=float)  # local Y is IEC X
    vsad_x, vsad_y = float(vsad_mm[0]), float(vsad_mm[1])
    source_distance = model.source_plane_mm
    source_x = target_x * (vsad_y - source_distance) / vsad_y
    source_y = target_y * (vsad_x - source_distance) / vsad_x
    delta_x = target_x - source_x
    delta_y = target_y - source_y
    magnitude = np.sqrt(delta_x**2 + delta_y**2 + source_distance**2)
    rot_y = np.degrees(-np.arcsin(delta_x / magnitude))
    rot_x = np.degrees(np.arctan2(delta_y, source_distance))

    alpha = np.radians(rot_x)
    beta = np.radians(rot_y)
    direction_x = -np.sin(beta)
    direction_y = np.sin(alpha) * np.cos(beta)
    direction_z = np.cos(alpha) * np.cos(beta)
    hit_x = source_x + direction_x * source_distance / direction_z
    hit_y = source_y + direction_y * source_distance / direction_z
    error = np.hypot(hit_x - target_x, hit_y - target_y)
    if not np.isfinite(error).all() or float(error.max()) > 0.01:
        raise RuntimeError(
            "Commissioned VSAD spot-axis projection failed its geometric back-check: "
            f"maximum isocenter error={float(error.max()):.6g} mm"
        )

    nominal = table["Energy_MeVu"].to_numpy(dtype=float)
    nf = np.asarray([model.number_per_mu(value) for value in nominal], dtype=float)
    spectrum_cache: dict[float, tuple[float, float, float, float]] = {}
    spectrum_mean = np.empty_like(nominal)
    spectrum_min = np.empty_like(nominal)
    spectrum_max = np.empty_like(nominal)
    spectrum_sigma = np.empty_like(nominal)
    for index, value in enumerate(nominal):
        key = float(value)
        if key not in spectrum_cache:
            spectrum = model.spectrum(key)
            mean = float(np.dot(spectrum.total_energies_mev, spectrum.weights))
            sigma = float(
                np.sqrt(np.dot((spectrum.total_energies_mev - mean) ** 2, spectrum.weights))
            )
            spectrum_cache[key] = (
                mean,
                float(spectrum.total_energies_mev.min()),
                float(spectrum.total_energies_mev.max()),
                sigma,
            )
        spectrum_mean[index], spectrum_min[index], spectrum_max[index], spectrum_sigma[index] = (
            spectrum_cache[key]
        )
    return {
        "source_local_x_mm": source_x,
        "source_local_y_mm": source_y,
        "rotation_x_deg": rot_x,
        "rotation_y_deg": rot_y,
        "projection_error_mm": error,
        "number_per_mu": nf,
        "spectrum_mean_total_mev": spectrum_mean,
        "spectrum_min_total_mev": spectrum_min,
        "spectrum_max_total_mev": spectrum_max,
        "spectrum_sigma_total_mev": spectrum_sigma,
        "allocation_weight": table["MetersetWeight_MU"].to_numpy(dtype=float) * nf,
    }


def require_commissioned_geometry(root: Path, model: CommissionedBeamModel) -> None:
    path = root / "topas" / "beam" / "beam_geometry.txt"
    if not path.is_file():
        raise RuntimeError("TOPAS beam geometry is missing; run stage 4 with the commissioned model")
    text = path.read_text(encoding="utf-8")
    marker = f"# Beam model mode: COMMISSIONED ({model.machine_name})"
    distance = f"d:Ge/PlanBeamPosition/TransX = {fmt_float(model.source_plane_mm)} mm"
    if marker not in text or distance not in text:
        raise RuntimeError(
            "TOPAS beam geometry does not use the active commissioned source plane; "
            "rerun stage 4 after selecting the commissioned beam model"
        )


def require_baseline_geometry(root: Path) -> None:
    path = root / "topas" / "beam" / "beam_geometry.txt"
    if not path.is_file() or "# Beam model mode: BASELINE" not in path.read_text(encoding="utf-8"):
        raise RuntimeError(
            "TOPAS beam geometry does not use baseline mode; rerun stage 4 after selecting RTPLAN baseline"
        )


def build_commissioned_plan(
    table: pd.DataFrame,
    histories: np.ndarray,
    source_csv: Path,
    model: CommissionedBeamModel,
    audit: dict[str, np.ndarray],
    beam_input_mode: str,
) -> str:
    """Build one audited Emittance source per energy layer in a single TOPAS session."""

    count = len(table)
    layer_values = [int(value) for value in table["LayerIndex"].drop_duplicates()]
    particle_calibration = model.particle_calibration()
    lines = [
        "# AUTO-GENERATED FILE -- DO NOT EDIT SPOTS BY HAND",
        "# Generator: scripts/04_generate_topas_plan.py",
        f"# Source: {source_csv}",
        f"# Beam input mode: {'RTPLAN' if beam_input_mode == 'rtplan' else 'MANUAL_SINGLE_SPOT'}",
        "# Beam model mode: COMMISSIONED",
        f"# Commissioned profile: {model.profile_path}",
        f"# Commissioned fingerprint: {model.fingerprint}",
        f"# Treatment machine: {model.machine_name}",
        f"# Machine particle-calibration SHA-256: {particle_calibration.binding_sha256}",
        f"# Number-per-MU SHA-256: {particle_calibration.number_per_mu_sha256}",
        f"# Machine dose-output correction: {particle_calibration.dose_output_correction_factor:.12g} ({particle_calibration.dose_output_correction_status})",
        f"# Source plane: {model.source_plane_mm:.10g} mm upstream of isocenter",
        f"# Spots / active layers / active sources: {count} / {len(layer_values)} / {len(layer_values)}",
        f"# Allocated NF-weighted histories: {int(histories.sum())}",
        "# Spot axes: DICOM VSAD projection with an explicit geometric back-check.",
        "# Energy: measured-IDD NNLS discrete spectrum; no additional nozzle WET is added.",
        "# Transverse phase space: measured spot-sigma Fermi-Eyges BiGaussian emittance.",
        "# Fluence: meterset weight multiplied by commissioned energy-dependent number-per-MU.",
        "",
        "includeFile = beam/beam_model.txt",
        "",
        "# The baseline Beam source remains defined for compatibility but is inactive.",
        "i:So/PlanCarbonBeam/NumberOfHistoriesInRun = 0",
        "",
    ]
    lines += time_feature(
        "PlanSpotLocalX", [fmt_float(value) for value in audit["source_local_x_mm"]], "d", "mm"
    )
    lines += time_feature(
        "PlanSpotLocalY", [fmt_float(value) for value in audit["source_local_y_mm"]], "d", "mm"
    )
    lines += time_feature(
        "PlanSpotRotX", [fmt_float(value) for value in audit["rotation_x_deg"]], "d", "deg"
    )
    lines += time_feature(
        "PlanSpotRotY", [fmt_float(value) for value in audit["rotation_y_deg"]], "d", "deg"
    )
    lines += [
        "d:Ge/PlanSpotPosition/TransX = Tf/PlanSpotLocalX/Value mm",
        "d:Ge/PlanSpotPosition/TransY = Tf/PlanSpotLocalY/Value mm",
        "d:Ge/PlanSpotPosition/RotX = Tf/PlanSpotRotX/Value deg",
        "d:Ge/PlanSpotPosition/RotY = Tf/PlanSpotRotY/Value deg",
        "",
    ]

    layer_array = table["LayerIndex"].to_numpy(dtype=int)
    for sequence, layer_index in enumerate(layer_values, start=1):
        rows = table[layer_array == layer_index]
        nominal_values = rows["Energy_MeVu"].to_numpy(dtype=float)
        if not np.allclose(nominal_values, nominal_values[0], atol=1e-8, rtol=0):
            raise RuntimeError(f"Layer {layer_index} contains more than one nominal energy")
        nominal = float(nominal_values[0])
        spectrum = model.spectrum(nominal)
        phase = model.phase(nominal)
        source_name = f"PlanCarbonBeamLayer{sequence:03d}"
        current_name = f"PlanLayer{sequence:03d}Histories"
        layer_histories = np.where(layer_array == layer_index, histories, 0).astype(np.int64)
        # The imported phase-space axes are IEC X/Y; this project's local source
        # frame maps local X=IEC Y and local Y=IEC X, so the parameters are swapped.
        lines += [
            f"# LayerIndex {layer_index}; nominal {nominal:.10g} MeV/u",
            f's:So/{source_name}/Type = "Emittance"',
            f's:So/{source_name}/Component = "PlanSpotPosition"',
            f's:So/{source_name}/EmittanceParticle = "GenericIon(6,12,6)"',
            f's:So/{source_name}/EmittanceEnergySpectrumType = "Discrete"',
            vector_line(
                f"dv:So/{source_name}/EmittanceEnergySpectrumValues",
                [fmt_float(value) for value in spectrum.total_energies_mev],
                "MeV",
            ),
            vector_line(
                f"uv:So/{source_name}/EmittanceEnergySpectrumWeights",
                [fmt_float(value) for value in spectrum.weights],
            ),
            f's:So/{source_name}/Distribution = "BiGaussian"',
            f"d:So/{source_name}/SigmaX = {fmt_float(phase.sigma_y_mm)} mm",
            f"d:So/{source_name}/SigmaY = {fmt_float(phase.sigma_x_mm)} mm",
            f"u:So/{source_name}/SigmaXprime = {fmt_float(phase.sigma_y_prime_rad)}",
            f"u:So/{source_name}/SigmaYprime = {fmt_float(phase.sigma_x_prime_rad)}",
            f"u:So/{source_name}/CorrelationX = {fmt_float(phase.correlation_y)}",
            f"u:So/{source_name}/CorrelationY = {fmt_float(phase.correlation_x)}",
            f's:Tf/{current_name}/Function = "Step"',
            f"dv:Tf/{current_name}/Times = Tf/PlanSpotLocalX/Times s",
            vector_line(f"iv:Tf/{current_name}/Values", [int(value) for value in layer_histories]),
            f"ic:So/{source_name}/NumberOfHistoriesInRun = Tf/{current_name}/Value",
            "",
        ]
    lines += [
        "d:Tf/TimelineStart = 0. s",
        f"d:Tf/TimelineEnd = {count}. s",
        f"i:Tf/NumberOfSequentialTimes = {count}",
        "i:Tf/Verbosity = 0",
        "",
    ]
    return "\n".join(lines)


def build_plan(
    table: pd.DataFrame,
    histories: np.ndarray,
    source_csv: Path,
    energy_scale: float,
    energy_offset_mevu: float,
    spot_size_scale: float,
    energy_spread_percent: float,
    beam_input_mode: str,
) -> str:
    count = len(table)
    # DICOM IEC X maps to source-local Y; IEC Y maps to source-local X.
    local_x = table["Y_mm"].to_numpy(dtype=float)
    local_y = table["X_mm"].to_numpy(dtype=float)
    energy_mevu = table["Energy_MeVu"].to_numpy(dtype=float) * energy_scale + energy_offset_mevu
    if np.any(energy_mevu <= 0.0):
        raise RuntimeError("Beam energy override produces a non-positive energy")
    energy_total = energy_mevu * CARBON_MASS_NUMBER
    sigma_local_x = table["FWHM_Y_mm"].to_numpy(dtype=float) * FWHM_TO_SIGMA * spot_size_scale
    sigma_local_y = table["FWHM_X_mm"].to_numpy(dtype=float) * FWHM_TO_SIGMA * spot_size_scale

    lines = [
        "# AUTO-GENERATED FILE -- DO NOT EDIT SPOTS BY HAND",
        "# Generator: scripts/04_generate_topas_plan.py",
        f"# Source: {source_csv}",
        f"# Beam input mode: {'RTPLAN' if beam_input_mode == 'rtplan' else 'MANUAL_SINGLE_SPOT'}",
        "# Beam model mode: BASELINE",
        f"# Spots / active layers: {count} / {table['LayerIndex'].nunique()}",
        f"# Allocated relative-weight histories: {int(histories.sum())}",
        "# Energy conversion: TOPAS total kinetic MeV = DICOM MeV/u * A(12)",
        "# Coordinate mapping: local X = IEC Y; local Y = IEC X (signs preserved)",
        "# Spot-size conversion: TOPAS sigma = DICOM FWHM / 2.354820045",
        f"# Beam override: energy scale={energy_scale:.10g}, offset={energy_offset_mevu:.10g} MeV/u",
        f"# Beam override: spot-size scale={spot_size_scale:.10g}, energy spread={energy_spread_percent:.10g}%",
        "# Angular distribution = None remains an explicit uncommissioned placeholder.",
        "# MRF4 geometry is not included; see beam/MRF4.txt.",
        "",
        "includeFile = beam/beam_model.txt",
        "",
    ]
    lines += time_feature("PlanSpotLocalX", [fmt_float(v) for v in local_x], "d", "mm")
    lines += time_feature("PlanSpotLocalY", [fmt_float(v) for v in local_y], "d", "mm")
    lines += time_feature("PlanEnergyTotal", [fmt_float(v) for v in energy_total], "d", "MeV")
    lines += time_feature("PlanSigmaLocalX", [fmt_float(v) for v in sigma_local_x], "d", "mm")
    lines += time_feature("PlanSigmaLocalY", [fmt_float(v) for v in sigma_local_y], "d", "mm")
    lines += time_feature("PlanHistories", [int(v) for v in histories], "i", None)
    lines += [
        "d:Ge/PlanSpotPosition/TransX = Tf/PlanSpotLocalX/Value mm",
        "d:Ge/PlanSpotPosition/TransY = Tf/PlanSpotLocalY/Value mm",
        "d:So/PlanCarbonBeam/BeamEnergy = Tf/PlanEnergyTotal/Value MeV",
        "d:So/PlanCarbonBeam/BeamPositionSpreadX = Tf/PlanSigmaLocalX/Value mm",
        "d:So/PlanCarbonBeam/BeamPositionSpreadY = Tf/PlanSigmaLocalY/Value mm",
        f"u:So/PlanCarbonBeam/BeamEnergySpread = {fmt_float(energy_spread_percent)}",
        "i:So/PlanCarbonBeam/NumberOfHistoriesInRun = Tf/PlanHistories/Value",
        "",
        "d:Tf/TimelineStart = 0. s",
        f"d:Tf/TimelineEnd = {count}. s",
        f"i:Tf/NumberOfSequentialTimes = {count}",
        "i:Tf/Verbosity = 0",
        "",
    ]
    return "\n".join(lines)


def write_allocation(
    path: Path,
    table: pd.DataFrame,
    histories: np.ndarray,
    energy_scale: float,
    energy_offset_mevu: float,
    spot_size_scale: float,
    beam_input_mode: str,
    beam_model_mode: str,
    commissioned_audit: dict[str, np.ndarray] | None = None,
) -> None:
    selected_sum = float(table["MetersetWeight_MU"].sum())
    fields = [
        "BeamInputMode", "BeamModelMode", "BeamNumber", "LayerIndex", "ControlPointIndex", "SpotIndex",
        "Energy_MeVu", "EffectiveEnergy_MeVu", "Energy_Total_MeV", "IEC_X_mm", "IEC_Y_mm",
        "CommissionedSpectrumMinTotalMeV", "CommissionedSpectrumMaxTotalMeV",
        "CommissionedSpectrumSigmaTotalMeV",
        "TOPAS_LocalX_mm", "TOPAS_LocalY_mm", "FWHM_IEC_X_mm", "FWHM_IEC_Y_mm",
        "Sigma_TOPAS_LocalX_mm", "Sigma_TOPAS_LocalY_mm", "EffectiveSigma_TOPAS_LocalX_mm",
        "EffectiveSigma_TOPAS_LocalY_mm", "MetersetWeight_MU",
        "CommissionedNumberPerMU", "AllocationBasis", "SelectedRelativeWeight",
        "SourceLocalX_mm", "SourceLocalY_mm", "SourceRotX_deg", "SourceRotY_deg",
        "VSADProjectionError_mm", "AllocatedHistories",
    ]
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        if commissioned_audit is None:
            allocation_basis = table["MetersetWeight_MU"].to_numpy(dtype=float)
        else:
            allocation_basis = commissioned_audit["allocation_weight"]
        basis_sum = float(allocation_basis.sum())
        for index, (row, allocated) in enumerate(zip(table.itertuples(index=False), histories)):
            if commissioned_audit is None:
                effective_total_energy = (
                    row.Energy_MeVu * energy_scale + energy_offset_mevu
                ) * CARBON_MASS_NUMBER
            else:
                effective_total_energy = commissioned_audit["spectrum_mean_total_mev"][index]
            effective_energy = effective_total_energy / CARBON_MASS_NUMBER
            writer.writerow(
                {
                    "BeamInputMode": "RTPLAN" if beam_input_mode == "rtplan" else "MANUAL_SINGLE_SPOT",
                    "BeamModelMode": beam_model_mode.upper(),
                    "BeamNumber": row.BeamNumber,
                    "LayerIndex": row.LayerIndex,
                    "ControlPointIndex": row.ControlPointIndex,
                    "SpotIndex": row.SpotIndex,
                    "Energy_MeVu": fmt_float(row.Energy_MeVu),
                    "EffectiveEnergy_MeVu": fmt_float(effective_energy),
                    "Energy_Total_MeV": fmt_float(effective_total_energy),
                    "CommissionedSpectrumMinTotalMeV": (
                        fmt_float(commissioned_audit["spectrum_min_total_mev"][index])
                        if commissioned_audit is not None else ""
                    ),
                    "CommissionedSpectrumMaxTotalMeV": (
                        fmt_float(commissioned_audit["spectrum_max_total_mev"][index])
                        if commissioned_audit is not None else ""
                    ),
                    "CommissionedSpectrumSigmaTotalMeV": (
                        fmt_float(commissioned_audit["spectrum_sigma_total_mev"][index])
                        if commissioned_audit is not None else ""
                    ),
                    "IEC_X_mm": fmt_float(row.X_mm),
                    "IEC_Y_mm": fmt_float(row.Y_mm),
                    "TOPAS_LocalX_mm": fmt_float(row.Y_mm),
                    "TOPAS_LocalY_mm": fmt_float(row.X_mm),
                    "FWHM_IEC_X_mm": fmt_float(row.FWHM_X_mm),
                    "FWHM_IEC_Y_mm": fmt_float(row.FWHM_Y_mm),
                    "Sigma_TOPAS_LocalX_mm": fmt_float(row.FWHM_Y_mm * FWHM_TO_SIGMA),
                    "Sigma_TOPAS_LocalY_mm": fmt_float(row.FWHM_X_mm * FWHM_TO_SIGMA),
                    "EffectiveSigma_TOPAS_LocalX_mm": fmt_float(row.FWHM_Y_mm * FWHM_TO_SIGMA * spot_size_scale),
                    "EffectiveSigma_TOPAS_LocalY_mm": fmt_float(row.FWHM_X_mm * FWHM_TO_SIGMA * spot_size_scale),
                    "MetersetWeight_MU": fmt_float(row.MetersetWeight_MU),
                    "CommissionedNumberPerMU": (
                        fmt_float(commissioned_audit["number_per_mu"][index])
                        if commissioned_audit is not None else ""
                    ),
                    "AllocationBasis": fmt_float(allocation_basis[index]),
                    "SelectedRelativeWeight": fmt_float(allocation_basis[index] / basis_sum),
                    "SourceLocalX_mm": (
                        fmt_float(commissioned_audit["source_local_x_mm"][index])
                        if commissioned_audit is not None else fmt_float(row.Y_mm)
                    ),
                    "SourceLocalY_mm": (
                        fmt_float(commissioned_audit["source_local_y_mm"][index])
                        if commissioned_audit is not None else fmt_float(row.X_mm)
                    ),
                    "SourceRotX_deg": (
                        fmt_float(commissioned_audit["rotation_x_deg"][index])
                        if commissioned_audit is not None else "0"
                    ),
                    "SourceRotY_deg": (
                        fmt_float(commissioned_audit["rotation_y_deg"][index])
                        if commissioned_audit is not None else "0"
                    ),
                    "VSADProjectionError_mm": (
                        fmt_float(commissioned_audit["projection_error_mm"][index])
                        if commissioned_audit is not None else ""
                    ),
                    "AllocatedHistories": int(allocated),
                }
            )


def write_allocation_metadata(
    path: Path,
    allocation: Path,
    beam_input_mode: str,
    beam_model_mode: str,
    model: CommissionedBeamModel | None,
) -> None:
    """Bind a per-run spot allocation to an immutable machine calibration."""

    payload: dict[str, object] = {
        "schema_version": 1,
        "allocation_file": allocation.name,
        "allocation_sha256": sha256(allocation),
        "beam_input_mode": "RTPLAN" if beam_input_mode == "rtplan" else "MANUAL_SINGLE_SPOT",
        "beam_model_mode": beam_model_mode.upper(),
        "machine_calibration": None,
    }
    if model is not None:
        binding = model.particle_calibration()
        payload["machine_calibration"] = binding.to_dict()
        payload["formula"] = {
            "planned_particles": "N_plan = sum_i(MU_i * NF_machine(E_i))",
            "simulated_histories": "N_sim = sum_i(AllocatedHistories_i)",
            "dose_scale": "N_plan / N_sim * machine_dose_output_correction_factor",
        }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_summary(
    path: Path,
    source: Path,
    output: Path,
    allocation: Path,
    full_table: pd.DataFrame,
    selected: pd.DataFrame,
    histories: np.ndarray,
    energy_scale: float,
    energy_offset_mevu: float,
    spot_size_scale: float,
    energy_spread_percent: float,
    beam_input_mode: str,
    beam_model_mode: str,
    model: CommissionedBeamModel | None = None,
    vsad_mm: np.ndarray | None = None,
    commissioned_audit: dict[str, np.ndarray] | None = None,
    sparse_test: bool = False,
    requested_spot_count: int = 0,
) -> None:
    manual_mode = beam_input_mode == "manual"
    dropped = max(0, (requested_spot_count or len(selected)) - len(selected))
    sparse_block = ""
    if sparse_test:
        sparse_block = (
            "\n*** SPARSE TEST RUN — NOT A PLAN DOSE ***\n"
            f"{dropped} of {requested_spot_count} selected spots received zero primaries and were\n"
            "dropped from the TOPAS timeline. Only the highest-weight spots are simulated, so the\n"
            "resulting dose is not a scaled version of the plan: whole regions receive nothing.\n"
            "Absolute particle-number calibration, Gamma analysis and TPS profile comparison are\n"
            "NOT valid for this run. Geometry, range and pipeline checks only.\n"
            f"For a physically meaningful result use at least {requested_spot_count} histories.\n\n"
        )
    all_input_rows_generated = not manual_mode and len(full_table) == len(selected)
    project_full_plan_generation = all_input_rows_generated and source.name == "spots.csv"
    selected_layer_text = (
        "MANUAL"
        if manual_mode
        else "ALL"
        if all_input_rows_generated
        else ",".join(str(int(item)) for item in selected["LayerIndex"].drop_duplicates())
    )
    mode_text = "MANUAL_SINGLE_SPOT" if manual_mode else "RTPLAN"
    commissioned = beam_model_mode == "commissioned"
    allocation_text = (
        "Single manual spot receives all requested histories"
        if manual_mode
        else (
            "Lower-bounded proportional allocation plus deterministic largest remainder using meterset * commissioned number-per-MU"
            if commissioned
            else "Lower-bounded proportional allocation plus deterministic largest remainder using selected meterset weight"
        )
    )
    source_energy_label = "Manual" if manual_mode else "DICOM nominal"
    manual_energy_audit = (
        f"Manual energy: {selected['Energy_MeVu'].iloc[0]:.10g} MeV/u" if manual_mode else ""
    )
    manual_spot_audit = ""
    if manual_mode:
        manual_spot_audit = (
            f"Manual IEC spot X / Y: {selected['X_mm'].iloc[0]:.10g} / {selected['Y_mm'].iloc[0]:.10g} mm\n"
            + (
                "Manual IEC FWHM input: not applied; commissioned phase space defines source sigma/emittance"
                if commissioned
                else f"Manual IEC spot FWHM X / Y: {selected['FWHM_X_mm'].iloc[0]:.10g} / {selected['FWHM_Y_mm'].iloc[0]:.10g} mm"
            )
        )
    spot_position_source = "Manual IEC spot coordinates" if manual_mode else "DICOM ScanSpotPositionMap"
    spot_size_source = (
        "Commissioned Fermi-Eyges source-plane phase space (DICOM/manual FWHM is not applied)"
        if commissioned
        else "Manual spot size" if manual_mode else "DICOM ScanningSpotSize"
    )
    if commissioned:
        assert model is not None and vsad_mm is not None and commissioned_audit is not None
        phase_audit = model.phase_measurement_audit
        particle_calibration = model.particle_calibration()
        profile_block = f"""Beam model mode: COMMISSIONED
Commissioned profile: {model.profile_path}
Commissioned fingerprint: {model.fingerprint}
Treatment machine: {model.machine_name}
Machine particle-calibration binding: {particle_calibration.binding_path}
Machine particle-calibration SHA-256: {particle_calibration.binding_sha256}
Number-per-MU table: {particle_calibration.number_per_mu_path}
Number-per-MU SHA-256: {particle_calibration.number_per_mu_sha256}
Machine dose-output correction factor / status: {particle_calibration.dose_output_correction_factor:.12g} / {particle_calibration.dose_output_correction_status}
Source plane upstream: {model.source_plane_mm:.10g} mm
RTPLAN VSAD X / Y: {vsad_mm[0]:.10g} / {vsad_mm[1]:.10g} mm
Profile expected VSAD X / Y: {model.expected_vsad_mm[0]:.10g} / {model.expected_vsad_mm[1]:.10g} mm
Maximum spot-axis projection error: {float(commissioned_audit['projection_error_mm'].max()):.10g} mm
Phase-space measured-sigma audit energies: {phase_audit['audited_energies']}
Phase-space measured-sigma median / maximum RMSE: {phase_audit['median_rmse_mm']:.10g} / {phase_audit['maximum_rmse_mm']:.10g} mm
Phase-space isocenter-sigma maximum error: {phase_audit['maximum_isocenter_error_mm']:.10g} mm
TOPAS active commissioned sources: {selected['LayerIndex'].nunique()}
Energy source: measured-IDD NNLS discrete spectrum
Transverse source: measured spot-sigma Fermi-Eyges BiGaussian emittance
Fluence source: meterset * commissioned energy-dependent number-per-MU
Nozzle WET: NOT ADDED (already represented in measured IDD-derived energy spectrum)
Commissioning water mean excitation energy: {float(model.profile.get('water_mean_excitation_energy_ev', float('nan'))):.10g} eV
"""
        commissioning_status = f"""IMPLEMENTED from matched machine commissioning: discrete incident-energy spectra,
BiGaussian position-angle emittance, DICOM VSAD spot-axis projection, and energy-dependent number-per-MU.
PROFILE MATCH: exact TreatmentMachineName plus bounded VSAD agreement.
MRF4 caveat: no separate physical MRF4 geometry/WET is added because the measured IDD-derived spectrum
contains the upstream energy loss; residual scatter/fragmentation-model validation is still required.
NOT INCLUDED: absolute clinical dose calibration, institution-specific CT HU calibration, independent end-to-end acceptance.
"""
    else:
        profile_block = "Beam model mode: BASELINE\nCommissioned profile: NOT USED\n"
        commissioning_status = f"""IMPLEMENTED from {'manual research input plus current DICOM geometry' if manual_mode else 'DICOM'}: ion identity, energy, spot X/Y, relative weights,
Spot FWHM-derived sigma, one painting, beam direction, isocenter.
BeamEnergySpread: {energy_spread_percent:.10g} percent ({'monoenergetic placeholder' if energy_spread_percent == 0 else 'USER RESEARCH OVERRIDE; requires measured commissioning evidence'}).
PLACEHOLDER: BeamAngularDistribution = None (no divergence/emittance).
SOURCE PLANE: generated upstream distance is a simulation plane, not physical SAD.
KNOWN BUT NOT MODELED: DICOM VSAD values (recorded in plan_summary.txt).
NOT INCLUDED: MRF4 physical geometry/material/WET; beam/MRF4.txt is an audit stub.
NOT INCLUDED: MU-to-primary calibration, absolute dose, transport-physics validation.
"""
    if commissioned:
        assert commissioned_audit is not None
        spectrum_mean = commissioned_audit["spectrum_mean_total_mev"]
        spectrum_min = commissioned_audit["spectrum_min_total_mev"]
        spectrum_max = commissioned_audit["spectrum_max_total_mev"]
        spectrum_sigma = commissioned_audit["spectrum_sigma_total_mev"]
        energy_block = f"""TOPAS incident spectrum weighted-mean total kinetic energy range: {spectrum_mean.min():.10g} .. {spectrum_mean.max():.10g} MeV
TOPAS incident discrete-spectrum envelope: {spectrum_min.min():.10g} .. {spectrum_max.max():.10g} MeV
TOPAS incident spectrum sigma range: {spectrum_sigma.min():.10g} .. {spectrum_sigma.max():.10g} MeV per ion
Weighted-mean incident energy-per-nucleon range: {(spectrum_mean / CARBON_MASS_NUMBER).min():.10g} .. {(spectrum_mean / CARBON_MASS_NUMBER).max():.10g} MeV/u
Energy interpretation: nominal DICOM MeV/u selects a measured-IDD spectrum; its TOPAS values are already total MeV per carbon ion and include upstream energy loss.
Applied energy scale: 1 (commissioned override disabled)
Applied energy offset: 0 MeV/u (commissioned override disabled)"""
    else:
        effective_nominal = selected["Energy_MeVu"] * energy_scale + energy_offset_mevu
        energy_block = f"""TOPAS total kinetic energy range: {effective_nominal.min() * CARBON_MASS_NUMBER:.10g} .. {effective_nominal.max() * CARBON_MASS_NUMBER:.10g} MeV
Conversion: E_total = A(12) * E_MeV/u
Applied energy scale: {energy_scale:.10g}
Applied energy offset: {energy_offset_mevu:.10g} MeV/u
Effective nominal range: {effective_nominal.min():.10g} .. {effective_nominal.max():.10g} MeV/u"""
    text = f"""TPS-TOPAS plan generation summary
====================================
Input spots table (read-only): {source}
Input SHA-256: {sha256(source)}
Generated TOPAS file: {output}
Generated SHA-256: {sha256(output)}
History allocation: {allocation}
Beam input mode: {mode_text}
{profile_block.rstrip()}

Selection and weights
---------------------
Input plan spots / layers: {len(full_table)} / {full_table['LayerIndex'].nunique()}
Generated spots / layers: {len(selected)} / {selected['LayerIndex'].nunique()}
Selected LayerIndex values: {selected_layer_text}
All input rows generated: {all_input_rows_generated}
Project full-plan generation: {project_full_plan_generation}
Selected meterset-weight sum: {selected['MetersetWeight_MU'].sum():.12g} MU
Histories requested / allocated: {int(histories.sum())} / {int(histories.sum())}
Minimum / maximum histories per spot: {int(histories.min())} / {int(histories.max())}
Spot coverage: {len(selected)} / {requested_spot_count or len(selected)} selected spots simulated
Run class: {'SPARSE TEST RUN (NOT A PLAN DOSE)' if sparse_test else 'COMPLETE SELECTION'}
{sparse_block}Allocation: {allocation_text}
MU-to-particle basis: {'MACHINE-BOUND COMMISSIONED NF(E) APPLIED; downstream dose scale is sum(AllocationBasis)/sum(AllocatedHistories) times the machine output correction' if commissioned else 'NOT AVAILABLE'}

Energy
------
Particle: GenericIon(6,12,6), fully stripped Carbon-12
{source_energy_label} energy range: {selected['Energy_MeVu'].min():.10g} .. {selected['Energy_MeVu'].max():.10g} MeV/u
{energy_block}
{manual_energy_audit}

Spot coordinates and size
-------------------------
{spot_position_source}: IEC GANTRY isocentric plane, millimetres
{spot_size_source}: air at isocenter, FWHM
Conversion: sigma = FWHM / 2.3548200450309493
Applied spot-size scale: {spot_size_scale:.10g}
{manual_spot_audit}
IEC X -> patient +Y -> TOPAS PlanBeamPosition local +Y
IEC Y -> patient +Z -> TOPAS PlanBeamPosition local +X
Mapping evidence for this G90/HFS case:
  meterset-weighted spot centroid IEC X/Y = {np.average(selected['X_mm'], weights=selected['MetersetWeight_MU']):+.6f} / {np.average(selected['Y_mm'], weights=selected['MetersetWeight_MU']):+.6f} mm
  The HFS/G90/couch0 transform must pass scripts/07_validate_case_compatibility.py.

Commissioning status
--------------------
{commissioning_status.rstrip()}

Required commissioning inputs
-----------------------------
1. Independent end-to-end validation of the imported energy spectra and phase space on this TOPAS version.
2. MRF4 scatter/fragmentation validation and a documented policy preventing WET double counting.
3. Institution/scanner-specific HU-to-material and stopping-power calibration.
4. Independent monitor-chamber/dose traceability and uncertainty validation of the imported primary/MU table.
5. Commissioning acceptance thresholds across representative water and heterogeneous cases.
"""
    path.write_text(text, encoding="utf-8")


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    compatibility = root / "plan_parsed" / "compatibility_summary.txt"
    if not compatibility.is_file() or "READY FOR CURRENT QA WORKFLOW" not in compatibility.read_text(
        encoding="utf-8"
    ):
        raise RuntimeError(
            "Compatibility gate is missing or blocked; run scripts/07_validate_case_compatibility.py first"
        )
    source, output, allocation, summary = resolve_paths(args)
    ensure_safe_outputs(source, (output, allocation, summary), root, args.overwrite)
    full_table = load_and_validate(source)
    if not all(
        math.isfinite(value)
        for value in (
            args.energy_scale,
            args.energy_offset_mevu,
            args.spot_size_scale,
            args.energy_spread_percent,
        )
    ):
        raise RuntimeError("Beam override values must be finite")
    if not 0.8 <= args.energy_scale <= 1.2:
        raise RuntimeError("--energy-scale must be within [0.8, 1.2]")
    if not -20.0 <= args.energy_offset_mevu <= 20.0:
        raise RuntimeError("--energy-offset-mevu must be within [-20, 20]")
    if not 0.25 <= args.spot_size_scale <= 4.0:
        raise RuntimeError("--spot-size-scale must be within [0.25, 4]")
    if not 0.0 <= args.energy_spread_percent <= 20.0:
        raise RuntimeError("--energy-spread-percent must be within [0, 20]")
    if args.beam_model_mode == "commissioned" and not (
        math.isclose(args.energy_scale, 1.0)
        and math.isclose(args.energy_offset_mevu, 0.0)
        and math.isclose(args.spot_size_scale, 1.0)
        and math.isclose(args.energy_spread_percent, 0.0)
    ):
        raise RuntimeError(
            "Commissioned beam mode cannot be combined with baseline energy, spot-size or spread overrides; "
            "modify and re-commission the machine profile instead"
        )
    if args.beam_input_mode == "manual":
        selected = build_manual_spot(full_table, args)
    else:
        selected = select_rows(full_table, parse_layer_indices(args.layer_indices), args.spots_per_layer)

    model = None
    vsad_mm = None
    commissioned_audit = None
    if args.beam_model_mode == "commissioned":
        _, _, beam = discover_plan_beam(root)
        treatment_machine = str(getattr(beam, "TreatmentMachineName", ""))
        model = load_commissioned_model(root, args.beam_model_profile, treatment_machine)
        vsad_mm = model.validate_rtplan(
            treatment_machine, getattr(beam, "VirtualSourceAxisDistances", [])
        )
        model.particle_calibration()
        require_commissioned_geometry(root, model)
        commissioned_audit = commissioned_spot_audit(selected, model, vsad_mm)
        allocation_weights = commissioned_audit["allocation_weight"]
    else:
        require_baseline_geometry(root)
        allocation_weights = selected["MetersetWeight_MU"].to_numpy(dtype=float)

    sparse_test = False
    requested_spot_count = len(selected)
    if args.beam_input_mode == "manual":
        if args.total_histories <= 0:
            raise RuntimeError("--total-histories must be positive")
        histories = np.asarray([args.total_histories], dtype=np.int64)
    else:
        histories = allocate_histories(allocation_weights, args.total_histories)
        keep = histories > 0
        if not keep.all():
            # A zero-history spot still costs a full sequential Geant4 Run --
            # one BeamOn, one worker barrier, one scorer merge. Leaving it in
            # the timeline would make the short test as slow as the full plan
            # (measured 0.186 s per spot, essentially independent of the
            # history count), which defeats the point of running a test.
            sparse_test = True
            selected = selected.loc[keep].reset_index(drop=True)
            histories = histories[keep]
            allocation_weights = allocation_weights[keep]
            if commissioned_audit is not None:
                commissioned_audit = {
                    key: value[keep] for key, value in commissioned_audit.items()
                }
            print(
                "\n"
                "================================ WARNING ================================\n"
                f"SPARSE TEST RUN: {args.total_histories:,} histories cannot give each of the\n"
                f"{requested_spot_count:,} selected spots one primary. "
                f"{len(selected):,} spots\n"
                f"({100.0 * len(selected) / requested_spot_count:.2f}% of the selection, the "
                "highest-weight ones) will be\n"
                f"simulated with one primary each; {requested_spot_count - len(selected):,} "
                "spots are dropped entirely.\n"
                "\n"
                "The result is NOT a scaled version of the plan dose. Whole regions of the\n"
                "target receive no dose at all, so absolute particle-number calibration,\n"
                "Gamma analysis and TPS profile comparison are meaningless for this run.\n"
                "Use it only for geometry, range and pipeline sanity checks.\n"
                f"For a physically meaningful result use at least {requested_spot_count:,} "
                "histories.\n"
                "=========================================================================\n",
                flush=True,
            )

    if args.beam_model_mode == "commissioned":
        assert model is not None and commissioned_audit is not None
        generated_text = build_commissioned_plan(
            selected, histories, source, model, commissioned_audit, args.beam_input_mode
        )
    else:
        generated_text = build_plan(
            selected,
            histories,
            source,
            args.energy_scale,
            args.energy_offset_mevu,
            args.spot_size_scale,
            args.energy_spread_percent,
            args.beam_input_mode,
        )
    output.write_text(generated_text, encoding="utf-8")
    write_allocation(
        allocation,
        selected,
        histories,
        args.energy_scale,
        args.energy_offset_mevu,
        args.spot_size_scale,
        args.beam_input_mode,
        args.beam_model_mode,
        commissioned_audit,
    )
    allocation_metadata = allocation.with_name(allocation.stem + "_metadata.json")
    write_allocation_metadata(
        allocation_metadata,
        allocation,
        args.beam_input_mode,
        args.beam_model_mode,
        model,
    )
    write_summary(
        summary,
        source,
        output,
        allocation,
        full_table,
        selected,
        histories,
        args.energy_scale,
        args.energy_offset_mevu,
        args.spot_size_scale,
        args.energy_spread_percent,
        args.beam_input_mode,
        args.beam_model_mode,
        model,
        vsad_mm,
        commissioned_audit,
        sparse_test,
        requested_spot_count,
    )
    print(f"Beam input mode: {args.beam_input_mode}")
    print(f"Beam model mode: {args.beam_model_mode}")
    print(f"Generated {len(selected)} spots in {selected['LayerIndex'].nunique()} layers")
    print(f"Allocated histories: {int(histories.sum())}")
    print(f"TOPAS: {output}")
    print(f"Allocation: {allocation}")
    print(f"Allocation metadata: {allocation_metadata}")
    print(f"Summary: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
