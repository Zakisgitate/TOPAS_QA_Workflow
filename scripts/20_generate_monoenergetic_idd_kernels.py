#!/usr/bin/env python3
"""Generate 150 mm / 0.5 mm monoenergetic TOPAS IDD kernels.

The measured IDD file supplies the commissioning energy list and documents the
80 mm detector geometry.  It is not treated as a substitute for transport:
for every measured energy this program runs a *pure* C-12 beam in uniform
water, scores dose in a 150 mm diameter cylinder at 0.5 mm depth spacing, and
saves the resulting curve as a kernel.

The run is resumable.  ``state.json`` is updated after every completed energy.
Create ``PAUSE`` in the output directory to suspend the active TOPAS process;
remove it to resume.  Press Ctrl-C to terminate the active kernel and save a
paused state; run the same command again to repeat that kernel.  A
``--dry-run`` prints the decks and state without invoking TOPAS.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.utils.water_phantom import parse_measured_idd, read_topas_1d


DEFAULT_IDD = Path(
    "/Users/jiangzhenmin/Desktop/PLAN1699_副本/"
    "analysis/_water_phantom/_runtime_profiles/"
    "lzRoom1_90_RF4_260226_relative_only/IDD_lzRoom1_90_RF4.csv"
)
DEFAULT_OUTPUT = ROOT / "analysis" / "monoenergetic_kernels_150mm_0.5mm"
CARBON_A = 12.0
DEFAULT_PHANTOM_DEPTH_MM = 400.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idd", type=Path, default=DEFAULT_IDD, help="Measured 80 mm IDD CSV")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--topas", type=Path, help="TOPAS executable or wrapper")
    parser.add_argument("--histories", type=int, default=100_000)
    parser.add_argument("--threads", type=int, default=12)
    parser.add_argument("--seed", type=int, default=1699)
    parser.add_argument("--phantom-depth-mm", type=float, default=DEFAULT_PHANTOM_DEPTH_MM)
    parser.add_argument("--surface-distance-mm", type=float, default=150.0)
    parser.add_argument("--phantom-lateral-mm", type=float, default=200.0)
    parser.add_argument("--idd-diameter-mm", type=float, default=150.0)
    parser.add_argument("--depth-step-mm", type=float, default=0.5)
    parser.add_argument("--energies-mevu", help="Optional comma-separated subset; default: every measured IDD energy")
    parser.add_argument("--pause-file", type=Path, help="Pause sentinel; default: <output-dir>/PAUSE")
    parser.add_argument("--dry-run", action="store_true", help="Generate decks/state only; do not run TOPAS")
    parser.add_argument("--overwrite", action="store_true", help="Start a new run and replace existing kernel files")
    return parser.parse_args()


def _finite_positive(value: float, label: str) -> float:
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{label} must be a positive finite number, got {value!r}")
    return value


def _parse_energies(value: str | None, available: list[float]) -> list[float]:
    if value is None:
        return available
    requested = []
    for part in value.replace(";", ",").split(","):
        if not part.strip():
            continue
        try:
            requested.append(float(part))
        except ValueError as exc:
            raise RuntimeError(f"Invalid energy value {part!r}") from exc
    if not requested:
        raise RuntimeError("--energies-mevu contains no usable energies")
    result = []
    for energy in requested:
        matches = [candidate for candidate in available if abs(candidate - energy) < 1e-4]
        if not matches:
            raise RuntimeError(f"Energy {energy:g} MeV/u is not present in measured IDD: {available}")
        if matches[0] not in result:
            result.append(matches[0])
    return result


def _validate_input(path: Path, depth_step: float, diameter: float) -> tuple[list[Any], list[float]]:
    if not path.is_file():
        raise RuntimeError(f"Measured IDD file does not exist: {path}")
    curves = parse_measured_idd(path)
    if not curves:
        raise RuntimeError(f"No measured IDD curves found in {path}")
    energies = []
    for curve in curves:
        if curve.detector_size_mm is None or abs(float(curve.detector_size_mm) - 80.0) > 1e-3:
            raise RuntimeError(
                f"{curve.nominal_mevu:g} MeV/u is not an 80 mm measured IDD "
                f"(detector={curve.detector_size_mm!r})"
            )
        if curve.depth_mm.size < 3 or np.any(np.diff(curve.depth_mm) <= 0):
            raise RuntimeError(f"Depth positions are invalid for {curve.nominal_mevu:g} MeV/u")
        # The source file is intentionally irregular: dense (0.1--0.5 mm)
        # around the Bragg peak and coarser in the long fragment tail.  Require
        # the advertised fine samples to be present, while preserving every
        # original measurement position for later interpolation/fitting.
        spacings = np.diff(curve.depth_mm)
        fine_samples = int(np.count_nonzero(spacings <= 0.5 + 1e-6))
        if fine_samples < 3:
            raise RuntimeError(
                f"{curve.nominal_mevu:g} MeV/u lacks sufficient 0.5 mm or finer measured samples"
            )
        energies.append(float(curve.nominal_mevu))
    return curves, energies


def _bin_count(depth_mm: float, step_mm: float) -> tuple[int, float]:
    count = int(round(depth_mm / step_mm))
    if count < 1 or not math.isclose(count * step_mm, depth_mm, rel_tol=0, abs_tol=1e-7):
        raise RuntimeError("phantom depth must be divisible by depth step")
    return count, depth_mm / count


def _topas_command(path: Path | None) -> list[str]:
    if path:
        return [str(path.expanduser().resolve())]
    configured = os.environ.get("TOPAS_EXECUTABLE", "").strip()
    if configured:
        return [configured]
    found = shutil.which("topas")
    if found:
        return [found]
    wrapper = Path("/Users/jiangzhenmin/bin/topas")
    if wrapper.is_file():
        return [str(wrapper)]
    raise RuntimeError("TOPAS executable not found; pass --topas /path/to/topas")


def _deck_text(energy_mevu: float, tag: str, histories: int, threads: int, seed: int,
               phantom_depth: float, lateral: float, surface: float, diameter: float,
               bins: int, step: float, output_prefix: str) -> str:
    radius = diameter / 2.0
    centre_z = -surface + phantom_depth / 2.0
    source_z = -680.0
    return f'''# AUTO-GENERATED monoenergetic IDD kernel; tag={tag}
# Pure C-12 at {energy_mevu:.10g} MeV/u, total energy {energy_mevu * CARBON_A:.10g} MeV.
# Uniform G4_WATER, IDD diameter {diameter:g} mm, depth spacing {step:g} mm.

s:Ge/World/Material = "G4_AIR"
d:Ge/World/HLX = 300. mm
d:Ge/World/HLY = 300. mm
d:Ge/World/HLZ = 780. mm
b:Ge/World/Invisible = "True"

s:Ge/Water/Type = "TsBox"
s:Ge/Water/Parent = "World"
s:Ge/Water/Material = "G4_WATER"
d:Ge/Water/HLX = {lateral / 2.0:.10g} mm
d:Ge/Water/HLY = {lateral / 2.0:.10g} mm
d:Ge/Water/HLZ = {phantom_depth / 2.0:.10g} mm
d:Ge/Water/TransX = 0. mm
d:Ge/Water/TransY = 0. mm
d:Ge/Water/TransZ = {centre_z:.10g} mm

s:Ge/SpotPlane/Type = "Group"
s:Ge/SpotPlane/Parent = "World"
d:Ge/SpotPlane/TransX = 0. mm
d:Ge/SpotPlane/TransY = 0. mm
d:Ge/SpotPlane/TransZ = {source_z:.10g} mm

s:Ge/KernelCylinder/Type = "TsCylinder"
s:Ge/KernelCylinder/Parent = "World"
b:Ge/KernelCylinder/IsParallel = "True"
d:Ge/KernelCylinder/RMin = 0. mm
d:Ge/KernelCylinder/RMax = {radius:.10g} mm
d:Ge/KernelCylinder/HL = {phantom_depth / 2.0:.10g} mm
d:Ge/KernelCylinder/TransX = 0. mm
d:Ge/KernelCylinder/TransY = 0. mm
d:Ge/KernelCylinder/TransZ = {centre_z:.10g} mm
i:Ge/KernelCylinder/RBins = 1
i:Ge/KernelCylinder/PhiBins = 1
i:Ge/KernelCylinder/ZBins = {bins}

s:So/MonoCarbon/Type = "Beam"
s:So/MonoCarbon/Component = "SpotPlane"
s:So/MonoCarbon/BeamParticle = "GenericIon(6,12,6)"
d:So/MonoCarbon/BeamEnergy = {energy_mevu * CARBON_A:.10g} MeV
u:So/MonoCarbon/BeamEnergySpread = 0.
s:So/MonoCarbon/BeamPositionDistribution = "None"
d:So/MonoCarbon/BeamPositionX = 0. mm
d:So/MonoCarbon/BeamPositionY = 0. mm
s:So/MonoCarbon/BeamAngularDistribution = "None"
i:So/MonoCarbon/NumberOfHistoriesInRun = {histories}

s:Sc/KernelIDD/Quantity = "DoseToMedium"
s:Sc/KernelIDD/Component = "KernelCylinder"
sv:Sc/KernelIDD/Report = 1 "Sum"
s:Sc/KernelIDD/OutputType = "binary"
s:Sc/KernelIDD/OutputFile = "{output_prefix}"
s:Sc/KernelIDD/IfOutputFileAlreadyExists = "Overwrite"
b:Sc/KernelIDD/OutputAfterRun = "False"
b:Sc/KernelIDD/OutputToConsole = "False"
b:Sc/KernelIDD/Visualize = "False"

sv:Ph/Default/Modules = 6 "g4em-standard_opt4" "g4h-phy_QGSP_BIC_HP" "g4decay" "g4ion-binarycascade" "g4h-elastic_HP" "g4stopping"
d:Ph/Default/CutForAllParticles = 0.05 mm
i:Ts/Seed = {seed}
i:Ts/NumberOfThreads = {threads}
i:Ts/ShowHistoryCountAtInterval = 10000
b:Ge/CheckForOverlaps = "False"
b:Ts/PauseBeforeQuit = "False"
'''


def _write_kernel_csv(path: Path, depth: np.ndarray, dose: np.ndarray, energy: float) -> None:
    relative = dose / max(float(np.max(dose)), 1e-30)
    data = np.column_stack((np.arange(depth.size), depth, dose, relative))
    np.savetxt(
        path,
        data,
        delimiter=",",
        header=f"bin,depth_mm,dose_Gy_per_run,relative_to_max; energy_mevu={energy:.10g}",
        comments="# ",
        fmt=["%d", "%.8g", "%.12g", "%.12g"],
    )


def _progress(done: int, total: int, elapsed: float, mean_kernel_seconds: float, label: str = "") -> None:
    remaining = (total - done) * mean_kernel_seconds if mean_kernel_seconds > 0 else float("inf")
    eta = "--:--" if not math.isfinite(remaining) else time.strftime("%H:%M:%S", time.gmtime(max(remaining, 0)))
    width = 28
    filled = int(width * done / total) if total else width
    bar = "#" * filled + "." * (width - filled)
    print(f"\r[{bar}] {done}/{total} {done / total * 100:6.2f}% | elapsed {time.strftime('%H:%M:%S', time.gmtime(elapsed))} | ETA {eta} | {label}", end="", flush=True)
    if done >= total:
        print()


def _write_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _run_topas_process(command: list[str], cwd: Path, stdout_path: Path, stderr_path: Path,
                       pause_file: Path, interrupted: list[bool]) -> tuple[int, bool]:
    """Run one deck while allowing a file-controlled SIGSTOP/SIGCONT pause."""
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, cwd=cwd, stdout=stdout, stderr=stderr, env=os.environ.copy())
        paused = False
        while process.poll() is None:
            if (pause_file.exists() or interrupted[0]) and not paused:
                process.send_signal(signal.SIGSTOP)
                paused = True
                print(f"\nTOPAS paused. Remove {pause_file} to continue this kernel.", flush=True)
            if paused and not pause_file.exists() and not interrupted[0]:
                process.send_signal(signal.SIGCONT)
                paused = False
                print("TOPAS resumed.", flush=True)
            if interrupted[0]:
                # SIGSTOP prevents terminate from being handled until resumed.
                if paused:
                    process.send_signal(signal.SIGCONT)
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                return process.returncode or 130, True
            time.sleep(0.5)
        if paused:
            process.send_signal(signal.SIGCONT)
        return int(process.returncode or 0), False


def _load_state(path: Path, args: argparse.Namespace, energies: list[float], bins: int, step: float) -> dict[str, Any]:
    if path.is_file() and not args.overwrite:
        state = json.loads(path.read_text(encoding="utf-8"))
        expected = {"energies_mevu": energies, "histories": args.histories, "depth_bins": bins, "depth_step_mm": step, "idd_diameter_mm": args.idd_diameter_mm}
        for key, value in expected.items():
            if state.get(key) != value:
                raise RuntimeError(f"Existing state {path} does not match current {key}; use the original arguments or --overwrite")
        return state
    return {
        "schema_version": 1,
        "kind": "monoenergetic_idd_kernel_generation",
        "source_idd": str(args.idd.expanduser().resolve()),
        "measured_detector_diameter_mm": 80.0,
        "idd_diameter_mm": float(args.idd_diameter_mm),
        "depth_step_mm": float(step),
        "depth_bins": int(bins),
        "phantom_depth_mm": float(args.phantom_depth_mm),
        "histories": int(args.histories),
        "threads": int(args.threads),
        "seed": int(args.seed),
        "energies_mevu": energies,
        "completed": {},
        "status": "pending",
    }


def main() -> int:
    args = parse_args()
    args.idd = args.idd.expanduser().resolve()
    args.output_dir = args.output_dir.expanduser().resolve()
    args.histories = int(args.histories)
    args.threads = max(1, int(args.threads))
    if args.histories < 1:
        raise RuntimeError("--histories must be positive")
    _finite_positive(args.phantom_depth_mm, "--phantom-depth-mm")
    _finite_positive(args.idd_diameter_mm, "--idd-diameter-mm")
    _finite_positive(args.depth_step_mm, "--depth-step-mm")
    if args.idd_diameter_mm > args.phantom_lateral_mm:
        raise RuntimeError("IDD diameter must fit inside the water phantom lateral size")
    bins, step = _bin_count(float(args.phantom_depth_mm), float(args.depth_step_mm))
    curves, available = _validate_input(args.idd, float(args.depth_step_mm), float(args.idd_diameter_mm))
    energies = _parse_energies(args.energies_mevu, available)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    pause_file = (args.pause_file or args.output_dir / "PAUSE").expanduser().resolve()
    state_path = args.output_dir / "state.json"
    state = _load_state(state_path, args, energies, bins, step)
    state["status"] = "running"
    state["last_start_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _write_state(state_path, state)

    decks = args.output_dir / "decks"
    logs = args.output_dir / "logs"
    # Kernel CSVs and energy_mapping.txt live at the library root, matching
    # the existing ``energyN.csv`` loader.  Runtime decks/logs stay grouped
    # in subdirectories below it.
    kernels = args.output_dir
    for directory in (decks, logs):
        directory.mkdir(exist_ok=True)
    mapping_path = args.output_dir / "energy_mapping.txt"
    mapping_lines = ["# index total_energy_MeV energy_MeV_u"]
    for index, energy in enumerate(energies, start=1):
        record = state["completed"].get(f"{energy:.10g}")
        if record and record.get("status") == "complete" and Path(record.get("kernel", "")).is_file():
            mapping_lines.append(f"{index} {energy * CARBON_A:.10g} {energy:.10g}")
    mapping_path.write_text("\n".join(mapping_lines) + "\n", encoding="utf-8")
    started = time.monotonic()
    completed_durations = [
        float(record["duration_seconds"])
        for record in state["completed"].values()
        if record.get("status") == "complete" and record.get("duration_seconds") is not None
    ]
    historic_elapsed = float(sum(completed_durations))
    command_base = _topas_command(args.topas) if not args.dry_run else []
    interrupted = False
    interrupted_ref = [False]

    def stop_handler(signum: int, _frame: Any) -> None:
        nonlocal interrupted
        interrupted = True
        interrupted_ref[0] = True
        print("\nPause requested; the current kernel will stop at the next safe boundary.")

    previous = signal.signal(signal.SIGINT, stop_handler)
    try:
        for index, energy in enumerate(energies, start=1):
            key = f"{energy:.10g}"
            tag = f"energy{index:04d}_{energy:.5f}MeVu"
            # Keep the legacy library's energyN.csv naming convention.  The
            # downstream fitter must still be told this is an 800-bin 0.5 mm
            # library rather than the older 400-bin 1 mm format.
            kernel_path = kernels / f"energy{index}.csv"
            if key in state["completed"] and kernel_path.is_file():
                mean_seconds = float(np.mean(completed_durations)) if completed_durations else 0.0
                _progress(index, len(energies), historic_elapsed, mean_seconds, f"skip {energy:g} MeV/u")
                continue
            deck = decks / f"{tag}.txt"
            output_prefix = str(args.output_dir / "topas_output" / tag)
            Path(output_prefix).parent.mkdir(exist_ok=True)
            deck.write_text(_deck_text(energy, tag, args.histories, args.threads, args.seed + index - 1,
                                       args.phantom_depth_mm, args.phantom_lateral_mm, args.surface_distance_mm,
                                       args.idd_diameter_mm, bins, step, output_prefix), encoding="utf-8")
            if args.dry_run:
                state["completed"][key] = {"status": "deck_only", "deck": str(deck), "kernel": str(kernel_path)}
                _write_state(state_path, state)
                _progress(index, len(energies), time.monotonic() - started, 0.0, f"deck {energy:g} MeV/u")
                continue
            if interrupted or pause_file.exists():
                state["status"] = "paused"
                _write_state(state_path, state)
                print(f"Paused before {energy:g} MeV/u. Remove {pause_file} and rerun to continue.")
                return 0
            stdout_path = logs / f"{tag}.stdout.log"
            stderr_path = logs / f"{tag}.stderr.log"
            kernel_started = time.monotonic()
            returncode, stopped = _run_topas_process(
                command_base + [deck.name], decks, stdout_path, stderr_path, pause_file, interrupted_ref
            )
            if stopped or interrupted_ref[0]:
                state["status"] = "paused"
                _write_state(state_path, state)
                print(f"\nPaused during {energy:g} MeV/u. Remove {pause_file} and rerun to repeat this kernel.")
                return 0
            if returncode != 0:
                state["status"] = "failed"
                state["last_error"] = f"TOPAS exit code {returncode} at {energy:g} MeV/u"
                _write_state(state_path, state)
                raise RuntimeError(f"TOPAS failed for {energy:g} MeV/u; see {stderr_path}")
            raw = read_topas_1d(Path(output_prefix + ".bin"), bins)
            depth = (np.arange(bins, dtype=float) + 0.5) * step
            _write_kernel_csv(kernel_path, depth, raw, energy)
            duration = time.monotonic() - kernel_started
            completed_durations.append(duration)
            state["completed"][key] = {"status": "complete", "deck": str(deck), "kernel": str(kernel_path), "histories": args.histories, "duration_seconds": duration}
            _write_state(state_path, state)
            with mapping_path.open("a", encoding="utf-8") as mapping:
                mapping.write(f"{index} {energy * CARBON_A:.10g} {energy:.10g}\n")
            elapsed = historic_elapsed + time.monotonic() - started
            _progress(index, len(energies), elapsed, float(np.mean(completed_durations)), f"done {energy:g} MeV/u")
            if pause_file.exists() or interrupted:
                state["status"] = "paused"
                _write_state(state_path, state)
                print(f"Paused after {energy:g} MeV/u. Remove {pause_file} and rerun to continue.")
                return 0
    finally:
        signal.signal(signal.SIGINT, previous)
    state["status"] = "deck_only" if args.dry_run else "complete"
    state["elapsed_seconds"] = historic_elapsed + time.monotonic() - started
    _write_state(state_path, state)
    print(f"Completed {len(energies)} monoenergetic kernels in {state['elapsed_seconds']:.1f} s.")
    print(f"Kernels: {kernels}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
