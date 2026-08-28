#!/usr/bin/env python3
"""Generate a standalone water-phantom single-energy single-spot TOPAS run.

This validation needs no DICOM at all: no RTPLAN, no CT, no RTSTRUCT and no
RTDOSE.  It builds a uniform water box, one commissioned energy, one spot, and
three kinds of strictly one-dimensional scorer:

* integral depth dose in a cylinder that reproduces the commissioning detector,
* central-axis depth dose in a narrow cylinder,
* lateral profiles in thin bars at selected depths.

Because every scorer has exactly one binned axis, the TOPAS binary output has an
unambiguous order and the analysis stage never has to assume a bin convention.
The run also uses a single Geant4 run rather than one run per spot, so a much
finer depth sampling step is affordable than in the full-plan QA workflow.

Nothing here is fitted to a reference curve.  Absolute dose, when requested, is
the same audited machine-bound ``N_plan / N_sim * C_machine`` scale that the
full-plan workflow uses.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import shutil
import sys
from typing import Any, Iterable, Optional, Sequence

import numpy as np


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from gui.runtime_monitor import clamp_threads, logical_cpu_count
from scripts.utils.commissioned_beam import (
    CommissionedBeamModel,
    load_commissioned_model,
    sha256,
)
from scripts.utils.water_phantom import (
    CARBON_MASS_NUMBER,
    FWHM_TO_SIGMA,
    find_measured_curve,
    measured_idd_energies,
    nearest_spot_sigma,
    parse_measured_idd,
    parse_measured_spot_sigma,
    project_spot_axis,
)


IDD_SCORER = "WaterPhantomIdd"
PDD_SCORER = "WaterPhantomPdd"
LETD_SCORER = "WaterPhantomLetd"
SOURCE_NAME = "WaterPhantomSpot"
GENERATED_DIRNAME = "water_phantom"
ENTRY_FILENAME = "run_water_phantom_spot.txt"


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root, help=f"Project root (default: {root})")
    parser.add_argument(
        "--energy-mevu",
        type=float,
        required=True,
        help="Nominal carbon energy in MeV/u; must match a commissioned energy exactly",
    )
    parser.add_argument(
        "--beam-model-mode",
        choices=("commissioned", "baseline"),
        default="commissioned",
        help="Imported machine-commissioned source or an uncommissioned diagnostic beam",
    )
    parser.add_argument("--beam-model-profile", type=Path, help="Commissioned profile.json override")
    parser.add_argument(
        "--treatment-machine-name",
        help="Select the commissioned profile by machine name instead of the active pointer",
    )
    parser.add_argument("--histories", type=int, default=1_000_000, help="Primary histories (default: 1000000)")
    parser.add_argument("--seed", type=int, default=1699)
    parser.add_argument("--threads", type=int, default=12)

    parser.add_argument("--spot-x-mm", type=float, default=0.0, help="IEC X spot position at isocenter")
    parser.add_argument("--spot-y-mm", type=float, default=0.0, help="IEC Y spot position at isocenter")
    parser.add_argument(
        "--meterset-mu",
        type=float,
        help="Optional spot meterset; enables the audited absolute-dose scale via NF(E)",
    )

    parser.add_argument("--phantom-depth-mm", type=float, help="Water depth (default: from measured curve)")
    parser.add_argument("--phantom-lateral-mm", type=float, default=200.0, help="Full lateral size (default: 200)")
    parser.add_argument(
        "--surface-distance-mm",
        type=float,
        help="Isocenter-to-phantom-surface distance (default: from measured curve, else 150)",
    )
    parser.add_argument(
        "--depth-step-mm",
        type=float,
        default=0.5,
        help="Depth sampling step for the IDD and PDD scorers (default: 0.5)",
    )
    parser.add_argument(
        "--idd-radius-mm",
        type=float,
        help="Integral-depth-dose scoring radius (default: the commissioning detector radius)",
    )
    parser.add_argument(
        "--letd-step-mm",
        type=float,
        help="LETd depth sampling step (default: same as --depth-step-mm)",
    )
    parser.add_argument(
        "--pdd-radius-mm",
        type=float,
        default=5.0,
        help="Central-axis depth-dose scoring radius (default: 5)",
    )
    parser.add_argument(
        "--profile-depths-mm",
        help="Comma-separated water depths for lateral profiles (default: derived from the measured curve)",
    )
    parser.add_argument(
        "--lateral-step-mm", type=float, default=0.5, help="Lateral profile sampling step (default: 0.5)"
    )
    parser.add_argument(
        "--profile-half-width-mm", type=float, default=60.0, help="Lateral profile half extent (default: 60)"
    )
    parser.add_argument(
        "--profile-slab-mm",
        type=float,
        default=2.0,
        help="Thickness of a profile bar in its two unbinned axes (default: 2)",
    )

    parser.add_argument(
        "--baseline-fwhm-mm",
        type=float,
        default=8.0,
        help="Diagnostic baseline mode only: in-air spot FWHM at isocenter (default: 8)",
    )
    parser.add_argument(
        "--baseline-energy-spread-percent",
        type=float,
        default=0.0,
        help="Diagnostic baseline mode only: TOPAS BeamEnergySpread percent (default: 0)",
    )

    parser.add_argument("--output-tag", help="Run tag (default: wp_E<energy>_<histories>)")
    parser.add_argument("--overwrite", action="store_true", help="Replace an existing generated run")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Formatting helpers shared with the full-plan generator's conventions
# ---------------------------------------------------------------------------


def fmt_float(value: float) -> str:
    return f"{float(value):.10g}"


def vector_line(prefix: str, values: Sequence[object], unit: Optional[str] = None) -> str:
    body = " ".join(str(value) for value in values)
    suffix = f" {unit}" if unit else ""
    return f"{prefix} = {len(values)} {body}{suffix}"


def slug(value: object, fallback: str = "unknown") -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "")).strip("_")
    return text or fallback


def parse_depth_list(value: Optional[str]) -> Optional[list[float]]:
    if value is None:
        return None
    depths: list[float] = []
    for chunk in str(value).replace(";", ",").split(","):
        text = chunk.strip()
        if not text:
            continue
        try:
            depth = float(text)
        except ValueError as exc:
            raise RuntimeError(f"Invalid profile depth {text!r}") from exc
        if not math.isfinite(depth) or depth < 0:
            raise RuntimeError(f"Profile depth must be a finite non-negative number, got {text!r}")
        depths.append(depth)
    if not depths:
        raise RuntimeError("--profile-depths-mm was given but contains no usable value")
    return sorted(dict.fromkeys(round(value, 4) for value in depths))


# ---------------------------------------------------------------------------
# Geometry resolution
# ---------------------------------------------------------------------------


def bin_count(extent_mm: float, step_mm: float, label: str) -> tuple[int, float]:
    """Bin count and the exact step that divides ``extent_mm`` into whole bins."""

    if not math.isfinite(step_mm) or step_mm <= 0:
        raise RuntimeError(f"{label} step must be positive, got {step_mm!r}")
    if not math.isfinite(extent_mm) or extent_mm <= 0:
        raise RuntimeError(f"{label} extent must be positive, got {extent_mm!r}")
    count = int(round(extent_mm / step_mm))
    if count < 1:
        raise RuntimeError(
            f"{label} step {step_mm:g} mm is larger than the {extent_mm:g} mm extent"
        )
    return count, extent_mm / count


def default_profile_depths(measured, phantom_depth_mm: float) -> list[float]:
    """Entrance, mid-plateau, Bragg peak and distal R80 depths from the measured curve."""

    from scripts.utils.water_phantom import depth_curve_metrics

    metrics = depth_curve_metrics(measured.depth_mm, measured.dose_au)
    peak = metrics["R100_mm"]
    r80 = metrics.get("R80_mm") or peak
    candidates = [
        min(10.0, 0.25 * peak),
        0.5 * peak,
        peak,
        r80,
    ]
    depths: list[float] = []
    for value in candidates:
        depth = round(float(value), 2)
        if 0.0 <= depth <= phantom_depth_mm and depth not in depths:
            depths.append(depth)
    if not depths:
        raise RuntimeError("Could not derive default profile depths from the measured curve")
    return sorted(depths)


# ---------------------------------------------------------------------------
# TOPAS parameter emission
# ---------------------------------------------------------------------------


def build_geometry(setup: dict[str, Any]) -> str:
    geometry = setup["geometry"]
    scoring = setup["scoring"]
    surface_z = geometry["surface_z_mm"]
    centre_z = geometry["phantom_centre_z_mm"]
    lateral_half = geometry["phantom_lateral_mm"] / 2.0
    lines = [
        "# AUTO-GENERATED FILE -- DO NOT EDIT BY HAND",
        "# Generator: scripts/16_generate_water_phantom_spot.py",
        "# Standalone uniform-water validation geometry. No DICOM input is used.",
        "#",
        "# Frame: isocenter at the world origin, beam travelling along world +Z.",
        "# The water surface is upstream of isocenter by the commissioning",
        f"# isocenter-to-surface distance ({fmt_float(geometry['surface_distance_mm'])} mm), so water depth"
        " d maps to world",
        f"# z = {fmt_float(surface_z)} mm + d.",
        "",
        's:Ge/World/Material = "G4_AIR"',
        f"d:Ge/World/HLX = {fmt_float(geometry['world_hlx_mm'])} mm",
        f"d:Ge/World/HLY = {fmt_float(geometry['world_hly_mm'])} mm",
        f"d:Ge/World/HLZ = {fmt_float(geometry['world_hlz_mm'])} mm",
        'b:Ge/World/Invisible = "True"',
        "",
        "# Uniform water phantom; there is no CT and no HU-to-material conversion.",
        's:Ge/WaterPhantom/Type = "TsBox"',
        's:Ge/WaterPhantom/Parent = "World"',
        's:Ge/WaterPhantom/Material = "G4_WATER"',
        f"d:Ge/WaterPhantom/HLX = {fmt_float(lateral_half)} mm",
        f"d:Ge/WaterPhantom/HLY = {fmt_float(lateral_half)} mm",
        f"d:Ge/WaterPhantom/HLZ = {fmt_float(geometry['phantom_depth_mm'] / 2.0)} mm",
        "d:Ge/WaterPhantom/TransX = 0. mm",
        "d:Ge/WaterPhantom/TransY = 0. mm",
        f"d:Ge/WaterPhantom/TransZ = {fmt_float(centre_z)} mm",
        's:Ge/WaterPhantom/Color = "blue"',
        's:Ge/WaterPhantom/DrawingStyle = "WireFrame"',
        "",
        "# Source frame. Local +Z is the beam direction; local X/Y are IEC X/Y,",
        "# so no axis swap is applied here. The full-plan generator swaps them",
        "# only because it works in the G90 patient frame.",
        's:Ge/SpotBeamPlane/Type = "Group"',
        's:Ge/SpotBeamPlane/Parent = "World"',
        "d:Ge/SpotBeamPlane/TransX = 0. mm",
        "d:Ge/SpotBeamPlane/TransY = 0. mm",
        f"d:Ge/SpotBeamPlane/TransZ = {fmt_float(-geometry['source_plane_mm'])} mm",
        "d:Ge/SpotBeamPlane/RotX = 0. deg",
        "d:Ge/SpotBeamPlane/RotY = 0. deg",
        "d:Ge/SpotBeamPlane/RotZ = 0. deg",
        "",
        's:Ge/SpotPosition/Type = "Group"',
        's:Ge/SpotPosition/Parent = "SpotBeamPlane"',
        f"d:Ge/SpotPosition/TransX = {fmt_float(setup['spot']['source_local_x_mm'])} mm",
        f"d:Ge/SpotPosition/TransY = {fmt_float(setup['spot']['source_local_y_mm'])} mm",
        "d:Ge/SpotPosition/TransZ = 0. mm",
        f"d:Ge/SpotPosition/RotX = {fmt_float(setup['spot']['rotation_x_deg'])} deg",
        f"d:Ge/SpotPosition/RotY = {fmt_float(setup['spot']['rotation_y_deg'])} deg",
        "d:Ge/SpotPosition/RotZ = 0. deg",
        "",
        "# Scoring components live in a parallel world so they cannot displace or",
        "# overlap the water. Every one of them has exactly one binned axis.",
    ]

    idd = scoring["idd"]
    pdd = scoring["pdd"]
    letd = scoring["letd"]
    for name, spec in (
        ("IddCylinder", idd),
        ("PddCylinder", pdd),
        ("LetdCylinder", letd),
    ):
        lines += [
            "",
            f"# {spec['description']}",
            f's:Ge/{name}/Type = "TsCylinder"',
            f's:Ge/{name}/Parent = "World"',
            f'b:Ge/{name}/IsParallel = "True"',
            f"d:Ge/{name}/RMin = 0. mm",
            f"d:Ge/{name}/RMax = {fmt_float(spec['radius_mm'])} mm",
            f"d:Ge/{name}/HL = {fmt_float(geometry['phantom_depth_mm'] / 2.0)} mm",
            f"d:Ge/{name}/TransX = {fmt_float(setup['spot']['spot_x_mm'])} mm",
            f"d:Ge/{name}/TransY = {fmt_float(setup['spot']['spot_y_mm'])} mm",
            f"d:Ge/{name}/TransZ = {fmt_float(centre_z)} mm",
            f"i:Ge/{name}/RBins = 1",
            f"i:Ge/{name}/PhiBins = 1",
            f"i:Ge/{name}/ZBins = {int(spec['bins'])}",
            f's:Ge/{name}/Color = "{spec["color"]}"',
            f's:Ge/{name}/DrawingStyle = "WireFrame"',
        ]

    for profile in scoring["profiles"]:
        name = profile["component"]
        half = profile["half_width_mm"]
        slab = profile["slab_mm"] / 2.0
        axis = profile["axis"]
        lines += [
            "",
            f"# Lateral {axis} profile bar at water depth {fmt_float(profile['depth_mm'])} mm",
            f's:Ge/{name}/Type = "TsBox"',
            f's:Ge/{name}/Parent = "World"',
            f'b:Ge/{name}/IsParallel = "True"',
            f"d:Ge/{name}/HLX = {fmt_float(half if axis == 'X' else slab)} mm",
            f"d:Ge/{name}/HLY = {fmt_float(half if axis == 'Y' else slab)} mm",
            f"d:Ge/{name}/HLZ = {fmt_float(slab)} mm",
            f"d:Ge/{name}/TransX = {fmt_float(setup['spot']['spot_x_mm'])} mm",
            f"d:Ge/{name}/TransY = {fmt_float(setup['spot']['spot_y_mm'])} mm",
            f"d:Ge/{name}/TransZ = {fmt_float(profile['centre_z_mm'])} mm",
            f"i:Ge/{name}/XBins = {int(profile['bins']) if axis == 'X' else 1}",
            f"i:Ge/{name}/YBins = {int(profile['bins']) if axis == 'Y' else 1}",
            f"i:Ge/{name}/ZBins = 1",
            f's:Ge/{name}/Color = "yellow"',
            f's:Ge/{name}/DrawingStyle = "WireFrame"',
        ]
    lines.append("")
    return "\n".join(lines)


def build_source(setup: dict[str, Any]) -> str:
    spot = setup["spot"]
    beam = setup["beam"]
    lines = [
        "# AUTO-GENERATED FILE -- DO NOT EDIT BY HAND",
        "# Generator: scripts/16_generate_water_phantom_spot.py",
        f"# Beam model mode: {beam['mode'].upper()}",
        f"# Nominal energy: {fmt_float(beam['nominal_mevu'])} MeV/u",
        f"# Requested spot at isocenter: IEC X={fmt_float(spot['spot_x_mm'])} mm, "
        f"Y={fmt_float(spot['spot_y_mm'])} mm",
        f"# Spot-axis geometric back-check error: {spot['projection_error_mm']:.3g} mm",
        f"# Histories in a single Geant4 run: {int(setup['run']['histories'])}",
        "",
        "includeFile = geometry.txt",
        "",
    ]
    if beam["mode"] == "commissioned":
        lines += [
            f"# Commissioned profile: {beam['profile_path']}",
            f"# Commissioned fingerprint: {beam['profile_fingerprint']}",
            f"# Treatment machine: {beam['machine_name']}",
            f"# Machine particle-calibration SHA-256: {beam['particle_calibration']['binding_sha256']}",
            f"# Number-per-MU SHA-256: {beam['particle_calibration']['number_per_mu_sha256']}",
            f"# Source plane: {fmt_float(setup['geometry']['source_plane_mm'])} mm upstream of isocenter",
            "# Energy: measured-IDD NNLS discrete spectrum; no additional nozzle WET is added.",
            "# Transverse phase space: measured spot-sigma Fermi-Eyges BiGaussian emittance.",
            "",
            f's:So/{SOURCE_NAME}/Type = "Emittance"',
            f's:So/{SOURCE_NAME}/Component = "SpotPosition"',
            f's:So/{SOURCE_NAME}/EmittanceParticle = "GenericIon(6,12,6)"',
            f's:So/{SOURCE_NAME}/EmittanceEnergySpectrumType = "Discrete"',
            vector_line(
                f"dv:So/{SOURCE_NAME}/EmittanceEnergySpectrumValues",
                [fmt_float(value) for value in beam["spectrum_total_mev"]],
                "MeV",
            ),
            vector_line(
                f"uv:So/{SOURCE_NAME}/EmittanceEnergySpectrumWeights",
                [fmt_float(value) for value in beam["spectrum_weights"]],
            ),
            f's:So/{SOURCE_NAME}/Distribution = "BiGaussian"',
            f"d:So/{SOURCE_NAME}/SigmaX = {fmt_float(beam['phase']['sigma_x_mm'])} mm",
            f"d:So/{SOURCE_NAME}/SigmaY = {fmt_float(beam['phase']['sigma_y_mm'])} mm",
            f"u:So/{SOURCE_NAME}/SigmaXprime = {fmt_float(beam['phase']['sigma_x_prime_rad'])}",
            f"u:So/{SOURCE_NAME}/SigmaYprime = {fmt_float(beam['phase']['sigma_y_prime_rad'])}",
            f"u:So/{SOURCE_NAME}/CorrelationX = {fmt_float(beam['phase']['correlation_x'])}",
            f"u:So/{SOURCE_NAME}/CorrelationY = {fmt_float(beam['phase']['correlation_y'])}",
            f"i:So/{SOURCE_NAME}/NumberOfHistoriesInRun = {int(setup['run']['histories'])}",
            "",
        ]
    else:
        sigma = beam["baseline_fwhm_mm"] * FWHM_TO_SIGMA
        lines += [
            "# UNCOMMISSIONED DIAGNOSTIC SOURCE.",
            "# A monoenergetic Gaussian beam with no measured spectrum and no measured",
            "# emittance. It exists to isolate spectrum and emittance effects during",
            "# debugging and must not be used for any range or dose claim.",
            "",
            f's:So/{SOURCE_NAME}/Type = "Beam"',
            f's:So/{SOURCE_NAME}/Component = "SpotPosition"',
            f's:So/{SOURCE_NAME}/BeamParticle = "GenericIon(6,12,6)"',
            f"d:So/{SOURCE_NAME}/BeamEnergy = {fmt_float(beam['nominal_mevu'] * CARBON_MASS_NUMBER)} MeV",
            f"u:So/{SOURCE_NAME}/BeamEnergySpread = {fmt_float(beam['baseline_energy_spread_percent'])}",
            f's:So/{SOURCE_NAME}/BeamPositionDistribution = "Gaussian"',
            f's:So/{SOURCE_NAME}/BeamPositionCutoffShape = "Ellipse"',
            f"d:So/{SOURCE_NAME}/BeamPositionSpreadX = {fmt_float(sigma)} mm",
            f"d:So/{SOURCE_NAME}/BeamPositionSpreadY = {fmt_float(sigma)} mm",
            f"d:So/{SOURCE_NAME}/BeamPositionCutoffX = {fmt_float(max(40.0, 6.0 * sigma))} mm",
            f"d:So/{SOURCE_NAME}/BeamPositionCutoffY = {fmt_float(max(40.0, 6.0 * sigma))} mm",
            f's:So/{SOURCE_NAME}/BeamAngularDistribution = "None"',
            f"i:So/{SOURCE_NAME}/NumberOfHistoriesInRun = {int(setup['run']['histories'])}",
            "",
        ]
    return "\n".join(lines)


def build_scoring(setup: dict[str, Any]) -> str:
    scoring = setup["scoring"]
    output_prefix = setup["run"]["scorer_output_prefix"]
    lines = [
        "# AUTO-GENERATED FILE -- DO NOT EDIT BY HAND",
        "# Generator: scripts/16_generate_water_phantom_spot.py",
        "# Every scorer below has exactly one binned axis, so its binary output is",
        "# a plain one-dimensional array and no bin-order convention is assumed.",
        "",
        "includeFile = source.txt",
        "",
    ]

    def scorer(name: str, component: str, output: str, description: str) -> list[str]:
        return [
            f"# {description}",
            f's:Sc/{name}/Quantity = "DoseToMedium"',
            f's:Sc/{name}/Component = "{component}"',
            f'sv:Sc/{name}/Report = 1 "Sum"',
            f's:Sc/{name}/OutputType = "binary"',
            f's:Sc/{name}/OutputFile = "{output_prefix}/{output}"',
            f's:Sc/{name}/IfOutputFileAlreadyExists = "Exit"',
            f'b:Sc/{name}/OutputAfterRun = "False"',
            f'b:Sc/{name}/OutputToConsole = "False"',
            f'b:Sc/{name}/Visualize = "False"',
            "",
        ]

    lines += scorer(
        IDD_SCORER, "IddCylinder", "idd", scoring["idd"]["description"]
    )
    lines += [
        f"# {scoring['letd']['description']}",
        f's:Sc/{LETD_SCORER}/Quantity = "myHadronLET"',
        f's:Sc/{LETD_SCORER}/Component = "LetdCylinder"',
        f's:Sc/{LETD_SCORER}/WeightBy = "dose"',
        f's:Sc/{LETD_SCORER}/OutputType = "binary"',
        f's:Sc/{LETD_SCORER}/OutputFile = "{output_prefix}/letd"',
        f's:Sc/{LETD_SCORER}/IfOutputFileAlreadyExists = "Exit"',
        f'b:Sc/{LETD_SCORER}/OutputAfterRun = "False"',
        f'b:Sc/{LETD_SCORER}/OutputToConsole = "False"',
        f'b:Sc/{LETD_SCORER}/Visualize = "False"',
        "",
    ]
    lines += scorer(
        PDD_SCORER, "PddCylinder", "pdd", scoring["pdd"]["description"]
    )
    for profile in scoring["profiles"]:
        lines += scorer(
            profile["scorer"],
            profile["component"],
            profile["output"],
            f"Lateral {profile['axis']} profile at water depth "
            f"{fmt_float(profile['depth_mm'])} mm",
        )
    return "\n".join(lines)


def build_physics() -> str:
    return (
        "# AUTO-GENERATED FILE -- DO NOT EDIT BY HAND\n"
        "# Generator: scripts/16_generate_water_phantom_spot.py\n"
        "# Identical physics list to the full-plan production run so the water\n"
        "# phantom validates the same transport configuration.\n\n"
        "includeFile = scoring.txt\n\n"
        'sv:Ph/Default/Modules = 6 "g4em-standard_opt4" "g4h-phy_QGSP_BIC_HP" "g4decay"'
        ' "g4ion-binarycascade" "g4h-elastic_HP" "g4stopping"\n'
        "d:Ph/Default/CutForAllParticles = 0.05 mm\n"
    )


def build_entry(setup: dict[str, Any]) -> str:
    run = setup["run"]
    return (
        "# Auto-generated water-phantom single-energy single-spot QA run.\n"
        "# Research physical-dose validation of the commissioned beam model.\n"
        "# Not commissioned absolute clinical dose and not a clinical acceptance test.\n\n"
        "includeFile = physics.txt\n\n"
        f"i:Ts/Seed = {int(run['seed'])}\n"
        f"i:Ts/NumberOfThreads = {int(run['threads'])}\n"
        "i:Ts/ShowHistoryCountAtInterval = 100000\n"
        'b:Ge/CheckForOverlaps = "False"\n'
        'b:Ts/PauseBeforeQuit = "False"\n'
    )


# ---------------------------------------------------------------------------
# Setup assembly
# ---------------------------------------------------------------------------


def resolve_setup(args: argparse.Namespace, root: Path) -> dict[str, Any]:
    energy = float(args.energy_mevu)
    if not math.isfinite(energy) or energy <= 0:
        raise RuntimeError("--energy-mevu must be a positive finite number")
    if args.histories < 1:
        raise RuntimeError("--histories must be positive")

    requested_threads = int(args.threads)
    threads, thread_note = clamp_threads(requested_threads)

    model: Optional[CommissionedBeamModel] = None
    measured_curves: list = []
    measured = None
    spot_sigma_table: dict = {}
    beam: dict[str, Any] = {"mode": args.beam_model_mode, "nominal_mevu": energy}

    if args.beam_model_mode == "commissioned":
        model = load_commissioned_model(
            root, args.beam_model_profile, args.treatment_machine_name
        )
        spectrum = model.spectrum(energy)
        phase = model.phase(energy)
        calibration = model.particle_calibration()
        measured_curves = parse_measured_idd(model.measured_idd_path)
        measured = find_measured_curve(measured_curves, energy)
        try:
            spot_sigma_table = parse_measured_spot_sigma(
                model._file_paths["measured_spot_sigma"]
            )
        except (KeyError, RuntimeError):
            spot_sigma_table = {}
        beam.update(
            {
                "profile_path": str(model.profile_path),
                "profile_fingerprint": model.fingerprint,
                "machine_name": model.machine_name,
                "source_plane_mm": model.source_plane_mm,
                "expected_vsad_mm": [float(value) for value in model.expected_vsad_mm],
                "spectrum_total_mev": [float(value) for value in spectrum.total_energies_mev],
                "spectrum_weights": [float(value) for value in spectrum.weights],
                "spectrum_mean_total_mev": float(
                    np.dot(spectrum.total_energies_mev, spectrum.weights)
                ),
                "spectrum_lines": int(spectrum.total_energies_mev.size),
                "phase": {
                    "sigma_x_mm": phase.sigma_x_mm,
                    "sigma_y_mm": phase.sigma_y_mm,
                    "sigma_x_prime_rad": phase.sigma_x_prime_rad,
                    "sigma_y_prime_rad": phase.sigma_y_prime_rad,
                    "correlation_x": phase.correlation_x,
                    "correlation_y": phase.correlation_y,
                    "correlation_was_clamped": phase.correlation_was_clamped,
                },
                "number_per_mu": model.number_per_mu(energy),
                "particle_calibration": calibration.to_dict(),
                "measured_idd_available": measured is not None,
                "measured_idd_energies_mevu": measured_idd_energies(measured_curves),
                "measured_idd_path": str(model.measured_idd_path),
                "measured_idd_sha256": sha256(model.measured_idd_path),
            }
        )
        beam["measured_spot_sigma_in_air"] = nearest_spot_sigma(spot_sigma_table, energy) or {}
        source_plane_mm = model.source_plane_mm
        vsad = [float(value) for value in model.expected_vsad_mm]
    else:
        # The diagnostic baseline has no imported model, so the source plane and
        # projection distances fall back to explicit, clearly labelled values.
        source_plane_mm = 680.0
        vsad = [5398.68, 6198.24]
        beam.update(
            {
                "profile_path": None,
                "machine_name": None,
                "source_plane_mm": source_plane_mm,
                "expected_vsad_mm": vsad,
                "baseline_fwhm_mm": float(args.baseline_fwhm_mm),
                "baseline_energy_spread_percent": float(args.baseline_energy_spread_percent),
                "measured_idd_available": False,
                "measured_idd_energies_mevu": [],
                "uncommissioned_warning": (
                    "Diagnostic monoenergetic Gaussian beam; no measured spectrum, "
                    "no measured emittance, no NF(E). Range and dose claims are invalid."
                ),
            }
        )

    surface_distance = args.surface_distance_mm
    if surface_distance is None:
        surface_distance = (
            float(measured.surface_distance_mm)
            if measured is not None and measured.surface_distance_mm
            else 150.0
        )
    surface_distance = float(surface_distance)
    if not math.isfinite(surface_distance) or surface_distance < 0:
        raise RuntimeError("--surface-distance-mm must be finite and non-negative")
    if surface_distance >= source_plane_mm:
        raise RuntimeError(
            f"Water surface at {surface_distance:g} mm upstream is at or behind the "
            f"{source_plane_mm:g} mm source plane"
        )

    phantom_depth = args.phantom_depth_mm
    if phantom_depth is None:
        if measured is not None:
            phantom_depth = float(math.ceil(float(measured.depth_mm.max()) + 10.0))
        else:
            phantom_depth = 400.0
    phantom_depth = float(phantom_depth)
    if not math.isfinite(phantom_depth) or phantom_depth <= 0:
        raise RuntimeError("--phantom-depth-mm must be positive")

    lateral = float(args.phantom_lateral_mm)
    if not math.isfinite(lateral) or lateral <= 0:
        raise RuntimeError("--phantom-lateral-mm must be positive")

    idd_radius = args.idd_radius_mm
    if idd_radius is None:
        idd_radius = (
            measured.detector_radius_mm
            if measured is not None and measured.detector_radius_mm
            else 40.0
        )
    idd_radius = float(idd_radius)
    pdd_radius = float(args.pdd_radius_mm)
    for label, radius in (("--idd-radius-mm", idd_radius), ("--pdd-radius-mm", pdd_radius)):
        if not math.isfinite(radius) or radius <= 0:
            raise RuntimeError(f"{label} must be positive")
        if radius > lateral / 2.0:
            raise RuntimeError(
                f"{label}={radius:g} mm does not fit inside the {lateral:g} mm water phantom"
            )

    profile_half = float(args.profile_half_width_mm)
    if profile_half <= 0 or profile_half > lateral / 2.0:
        raise RuntimeError(
            f"--profile-half-width-mm must be positive and at most {lateral / 2.0:g} mm"
        )
    profile_slab = float(args.profile_slab_mm)
    if not math.isfinite(profile_slab) or profile_slab <= 0:
        raise RuntimeError("--profile-slab-mm must be positive")

    spot_x, spot_y = float(args.spot_x_mm), float(args.spot_y_mm)
    projection = project_spot_axis(spot_x, spot_y, source_plane_mm, vsad[0], vsad[1])
    projection.update({"spot_x_mm": spot_x, "spot_y_mm": spot_y})

    depth_bins, depth_step = bin_count(phantom_depth, float(args.depth_step_mm), "Depth")
    requested_letd_step = (
        float(args.letd_step_mm)
        if args.letd_step_mm is not None
        else float(args.depth_step_mm)
    )
    letd_bins, letd_step = bin_count(phantom_depth, requested_letd_step, "LETd depth")
    lateral_bins, lateral_step = bin_count(
        2.0 * profile_half, float(args.lateral_step_mm), "Lateral"
    )

    profile_depths = parse_depth_list(args.profile_depths_mm)
    if profile_depths is None:
        if measured is None:
            raise RuntimeError(
                "No measured depth-dose curve is available for this energy, so profile "
                "depths cannot be derived. Pass --profile-depths-mm explicitly."
            )
        profile_depths = default_profile_depths(measured, phantom_depth)
    for depth in profile_depths:
        if depth > phantom_depth:
            raise RuntimeError(
                f"Profile depth {depth:g} mm is deeper than the {phantom_depth:g} mm phantom"
            )
        if depth - profile_slab / 2.0 < -1e-9 or depth + profile_slab / 2.0 > phantom_depth + 1e-9:
            raise RuntimeError(
                f"Profile bar at depth {depth:g} mm with a {profile_slab:g} mm slab would "
                f"extend outside the {phantom_depth:g} mm water phantom"
            )

    surface_z = -surface_distance
    centre_z = surface_z + phantom_depth / 2.0
    profiles: list[dict[str, Any]] = []
    for index, depth in enumerate(profile_depths, start=1):
        for axis in ("X", "Y"):
            profiles.append(
                {
                    "index": index,
                    "axis": axis,
                    "depth_mm": float(depth),
                    "centre_z_mm": float(surface_z + depth),
                    "component": f"Profile{axis}{index:02d}",
                    "scorer": f"WaterPhantomProfile{axis}{index:02d}",
                    "output": f"profile_{axis.lower()}_{index:02d}",
                    "bins": int(lateral_bins),
                    "step_mm": float(lateral_step),
                    "half_width_mm": float(profile_half),
                    "slab_mm": float(profile_slab),
                }
            )

    machine_slug = slug(beam.get("machine_name") or "baseline", "baseline")
    tag = args.output_tag or f"wp_E{energy:g}_{int(args.histories)}"
    tag = slug(tag, "water_phantom_run")

    world_lateral = max(lateral / 2.0, profile_half) + 200.0
    world_z = max(source_plane_mm, abs(surface_z + phantom_depth)) + 100.0

    n_plan = None
    if args.meterset_mu is not None:
        meterset = float(args.meterset_mu)
        if not math.isfinite(meterset) or meterset <= 0:
            raise RuntimeError("--meterset-mu must be positive")
        if args.beam_model_mode != "commissioned":
            raise RuntimeError(
                "--meterset-mu requires the commissioned beam model; the diagnostic "
                "baseline has no audited number-per-MU table"
            )
        n_plan = meterset * float(beam["number_per_mu"])

    return {
        "schema_version": 1,
        "generator": "scripts/16_generate_water_phantom_spot.py",
        "kind": "water_phantom_single_spot",
        "run": {
            "output_tag": tag,
            "histories": int(args.histories),
            "seed": int(args.seed),
            "threads": int(threads),
            "requested_threads": requested_threads,
            "thread_limit_note": thread_note,
            "logical_cpus": logical_cpu_count(),
            "meterset_mu": float(args.meterset_mu) if args.meterset_mu is not None else None,
            "planned_particles": n_plan,
            "scorer_output_prefix": f"../../topas_output/{GENERATED_DIRNAME}/{tag}",
        },
        "beam": beam,
        "spot": projection,
        "geometry": {
            "frame": "isocenter at origin, beam along +Z, water depth d at z = -surface + d",
            "source_plane_mm": float(source_plane_mm),
            "surface_distance_mm": surface_distance,
            "surface_z_mm": float(surface_z),
            "phantom_depth_mm": phantom_depth,
            "phantom_lateral_mm": lateral,
            "phantom_centre_z_mm": float(centre_z),
            "world_hlx_mm": float(world_lateral),
            "world_hly_mm": float(world_lateral),
            "world_hlz_mm": float(world_z),
        },
        "scoring": {
            "idd": {
                "radius_mm": idd_radius,
                "bins": int(depth_bins),
                "step_mm": float(depth_step),
                "requested_step_mm": float(args.depth_step_mm),
                "color": "magenta",
                "description": (
                    f"Integral depth dose in a {2.0 * idd_radius:g} mm diameter cylinder, "
                    f"{int(depth_bins)} bins of {depth_step:g} mm"
                ),
            },
            "pdd": {
                "radius_mm": pdd_radius,
                "bins": int(depth_bins),
                "step_mm": float(depth_step),
                "requested_step_mm": float(args.depth_step_mm),
                "color": "red",
                "description": (
                    f"Central-axis depth dose in a {2.0 * pdd_radius:g} mm diameter cylinder, "
                    f"{int(depth_bins)} bins of {depth_step:g} mm"
                ),
            },
            "letd": {
                "radius_mm": idd_radius,
                "bins": int(letd_bins),
                "step_mm": float(letd_step),
                "requested_step_mm": requested_letd_step,
                "color": "green",
                "quantity": "myHadronLET",
                "weight_by": "dose",
                "units": "keV/um",
                "description": (
                    f"Dose-weighted hadron LETd in a {2.0 * idd_radius:g} mm diameter cylinder, "
                    f"{int(letd_bins)} bins of {letd_step:g} mm"
                ),
            },
            "profiles": profiles,
            "profile_depths_mm": [float(value) for value in profile_depths],
            "profile_depths_source": (
                "explicit" if args.profile_depths_mm else "derived from the measured depth-dose curve"
            ),
        },
        "measured_reference": (
            {
                "available": True,
                "nominal_mevu": measured.nominal_mevu,
                "machine": measured.machine,
                "medium": measured.medium,
                "origin": measured.origin,
                "single_spot": measured.single_spot,
                "detector_shape": measured.detector_shape,
                "detector_size_mm": measured.detector_size_mm,
                "detector_radius_mm": measured.detector_radius_mm,
                "surface_distance_mm": measured.surface_distance_mm,
                "snout_distance_mm": measured.snout_distance_mm,
                "depth_range_mm": [
                    float(measured.depth_mm.min()),
                    float(measured.depth_mm.max()),
                ],
                "samples": int(measured.depth_mm.size),
            }
            if measured is not None
            else {
                "available": False,
                "reason": (
                    "This energy has no measured integral depth dose in the machine model; "
                    "no interpolation between measured energies is performed."
                ),
                "measured_energies_mevu": beam.get("measured_idd_energies_mevu", []),
            }
        ),
        "machine_cache_key": machine_slug,
        "limitations": [
            "Research physical-dose validation of the imported beam model, not a clinical acceptance test.",
            "Uniform G4_WATER; no CT, no HU-to-material conversion and no patient anatomy.",
            "MRF4 geometry is not modelled; the measured-IDD spectrum already carries upstream losses.",
            "The measured spot-sigma reference is an in-air measurement, not an in-water lateral profile.",
        ],
    }


def write_summary(setup: dict[str, Any], entry: Path, cache_dir: Path) -> str:
    beam = setup["beam"]
    geometry = setup["geometry"]
    scoring = setup["scoring"]
    run = setup["run"]
    reference = setup["measured_reference"]
    lines = [
        "TPS-TOPAS water-phantom single-energy single-spot QA generation",
        "==============================================================",
        f"Beam model mode: {beam['mode'].upper()}",
        f"Nominal energy: {beam['nominal_mevu']:.10g} MeV/u "
        f"({beam['nominal_mevu'] * CARBON_MASS_NUMBER:.10g} MeV total per carbon ion)",
    ]
    if beam["mode"] == "commissioned":
        lines += [
            f"Treatment machine: {beam['machine_name']}",
            f"Commissioned profile: {beam['profile_path']}",
            f"Commissioned fingerprint: {beam['profile_fingerprint']}",
            f"Discrete spectrum lines: {beam['spectrum_lines']}; "
            f"weighted-mean total energy {beam['spectrum_mean_total_mev']:.10g} MeV",
            f"Phase space sigma X/Y: {beam['phase']['sigma_x_mm']:.6g} / "
            f"{beam['phase']['sigma_y_mm']:.6g} mm",
            f"Phase space sigma X'/Y': {beam['phase']['sigma_x_prime_rad']:.6g} / "
            f"{beam['phase']['sigma_y_prime_rad']:.6g} rad",
            f"Number per MU: {beam['number_per_mu']:.10g}",
        ]
    else:
        lines += [
            "UNCOMMISSIONED DIAGNOSTIC BEAM: monoenergetic Gaussian, no measured spectrum,",
            "no measured emittance and no number-per-MU. Not valid for range or dose claims.",
            f"Baseline in-air FWHM: {beam['baseline_fwhm_mm']:.6g} mm",
            f"Baseline energy spread: {beam['baseline_energy_spread_percent']:.6g} %",
        ]
    lines += [
        "",
        "Geometry",
        "--------",
        f"Frame: {geometry['frame']}",
        f"Source plane: {geometry['source_plane_mm']:.10g} mm upstream of isocenter",
        f"Water surface: {geometry['surface_distance_mm']:.10g} mm upstream of isocenter "
        f"(world z = {geometry['surface_z_mm']:.10g} mm)",
        f"Water phantom: {geometry['phantom_depth_mm']:.10g} mm deep, "
        f"{geometry['phantom_lateral_mm']:.10g} mm lateral, uniform G4_WATER",
        f"Requested spot at isocenter: IEC X={setup['spot']['spot_x_mm']:.6g} mm, "
        f"Y={setup['spot']['spot_y_mm']:.6g} mm",
        f"Spot-axis geometric back-check error: {setup['spot']['projection_error_mm']:.3g} mm",
        "",
        "Sampling",
        "--------",
        f"Depth: {scoring['idd']['bins']} bins of {scoring['idd']['step_mm']:.6g} mm "
        f"(requested {scoring['idd']['requested_step_mm']:.6g} mm)",
        f"LETd depth: {scoring['letd']['bins']} bins of {scoring['letd']['step_mm']:.6g} mm "
        f"(requested {scoring['letd']['requested_step_mm']:.6g} mm)",
        f"Integral depth dose radius: {scoring['idd']['radius_mm']:.6g} mm",
        f"Central-axis depth dose radius: {scoring['pdd']['radius_mm']:.6g} mm",
        f"Lateral: {scoring['profiles'][0]['bins']} bins of "
        f"{scoring['profiles'][0]['step_mm']:.6g} mm over "
        f"+/-{scoring['profiles'][0]['half_width_mm']:.6g} mm",
        f"Profile depths ({scoring['profile_depths_source']}): "
        + ", ".join(f"{value:.6g}" for value in scoring["profile_depths_mm"])
        + " mm",
        f"Profile bar thickness: {scoring['profiles'][0]['slab_mm']:.6g} mm",
        f"Scorers: 2 depth curves + {len(scoring['profiles'])} lateral profiles, "
        "each with exactly one binned axis",
        "",
        "Run",
        "---",
        f"Histories: {run['histories']} in a single Geant4 run "
        "(the full-plan workflow instead runs one Geant4 run per spot)",
        f"Seed: {run['seed']}",
        f"Threads requested / used: {run['requested_threads']} / {run['threads']} "
        f"(logical CPUs: {run['logical_cpus']})",
    ]
    if run["thread_limit_note"]:
        lines.append(f"Thread cap applied: {run['thread_limit_note']}")
    if run["planned_particles"] is not None:
        lines += [
            f"Meterset: {run['meterset_mu']:.10g} MU",
            f"Planned particles N_plan = MU * NF(E) = {run['planned_particles']:.10g}",
            f"Absolute dose scale N_plan / N_sim = "
            f"{run['planned_particles'] / run['histories']:.10g} "
            "(machine output correction applied downstream)",
        ]
    else:
        lines.append(
            "Meterset: not given; results are normalized curves only, with no absolute dose."
        )
    lines += [
        "",
        "Measured reference",
        "------------------",
    ]
    if reference["available"]:
        lines += [
            f"Measured integral depth dose is available at {reference['nominal_mevu']:.6g} MeV/u",
            f"Detector: {reference['detector_shape']} {reference['detector_size_mm']:.6g} mm "
            f"(scoring radius {reference['detector_radius_mm']:.6g} mm)",
            f"Measurement surface distance: {reference['surface_distance_mm']:.6g} mm",
            f"Depth range: {reference['depth_range_mm'][0]:.6g} .. "
            f"{reference['depth_range_mm'][1]:.6g} mm over {reference['samples']} samples",
        ]
    else:
        lines += [
            "No measured integral depth dose exists for this energy.",
            "Measured energies (MeV/u): "
            + ", ".join(f"{value:.6g}" for value in reference.get("measured_energies_mevu", [])),
        ]
    lines += [
        "",
        "Outputs",
        "-------",
        f"TOPAS entry point: {entry}",
        f"Run cache: {cache_dir}",
        "",
        "Scope and limitations",
        "---------------------",
    ]
    lines += [f"- {item}" for item in setup["limitations"]]
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    setup = resolve_setup(args, root)
    tag = setup["run"]["output_tag"]

    generated = root / "topas" / GENERATED_DIRNAME
    dose_dir = root / "topas_output" / GENERATED_DIRNAME / tag
    cache_dir = (
        root / "analysis" / "_water_phantom" / setup["machine_cache_key"] / tag
    )

    existing = [path for path in (dose_dir, cache_dir) if path.exists() and any(path.iterdir())]
    if existing and not args.overwrite:
        raise RuntimeError(
            "Water-phantom run outputs already exist; rerun with --overwrite to replace them: "
            + ", ".join(str(path) for path in existing)
        )
    for path in existing:
        shutil.rmtree(path)

    generated.mkdir(parents=True, exist_ok=True)
    dose_dir.mkdir(parents=True, exist_ok=True)
    for name in ("setup", "curves", "figures", "metrics", "topas", "logs"):
        (cache_dir / name).mkdir(parents=True, exist_ok=True)

    files = {
        "geometry.txt": build_geometry(setup),
        "source.txt": build_source(setup),
        "scoring.txt": build_scoring(setup),
        "physics.txt": build_physics(),
        ENTRY_FILENAME: build_entry(setup),
    }
    for name, text in files.items():
        (generated / name).write_text(text, encoding="utf-8")

    entry = generated / ENTRY_FILENAME
    setup["run"]["entry_point"] = str(entry)
    setup["run"]["working_directory"] = str(generated)
    setup["run"]["dose_directory"] = str(dose_dir)
    setup["run"]["cache_directory"] = str(cache_dir)
    setup["run"]["generated_files"] = {
        name: sha256(generated / name) for name in sorted(files)
    }
    setup["run"]["expected_outputs"] = {
        "idd": str(dose_dir / "idd.bin"),
        "pdd": str(dose_dir / "pdd.bin"),
        **{
            f"{profile['axis'].lower()}_{profile['index']:02d}": str(
                dose_dir / f"{profile['output']}.bin"
            )
            for profile in setup["scoring"]["profiles"]
        },
    }

    summary_text = write_summary(setup, entry, cache_dir)
    (cache_dir / "setup" / "water_phantom_spot_setup.json").write_text(
        json.dumps(setup, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
    )
    (cache_dir / "setup" / "water_phantom_spot_generation_summary.txt").write_text(
        summary_text, encoding="utf-8"
    )
    for name in files:
        shutil.copy2(generated / name, cache_dir / "topas" / name)

    print(summary_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
