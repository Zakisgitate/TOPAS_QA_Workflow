#!/usr/bin/env python3
"""Prepare a case-specific full-spot TOPAS QA entry point and output path."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys

import pandas as pd


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from gui.case_results import archive_production_output
from gui.runtime_monitor import clamp_threads, logical_cpu_count


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--histories", type=int, default=100_000)
    parser.add_argument("--threads", type=int, default=12)
    parser.add_argument("--seed", type=int, default=1699)
    parser.add_argument("--output-tag", help="Patient-cache run tag (default: full_plan_<histories>)")
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def generated_source_names(plan: Path) -> list[str]:
    names = re.findall(r'^s:So/([A-Za-z0-9_]+)/Type\s*=\s*"', plan.read_text(encoding="utf-8"), re.MULTILINE)
    # PlanCarbonBeam is defined in beam_model.txt rather than plan_generated.txt.
    result = ["PlanCarbonBeam", *names]
    return list(dict.fromkeys(result))


def write_zero_history_entries(root: Path, source_names: list[str]) -> tuple[Path, Path]:
    overrides = "\n".join(
        f"{'i' if name == 'PlanCarbonBeam' else 'ic'}:So/{name}/NumberOfHistoriesInRun = 0"
        for name in source_names
    )
    parse_entry = root / "topas" / "validate_plan_full_parse.txt"
    grid_entry = root / "topas" / "validate_dose_grid.txt"
    parse_entry.write_text(
        "# Auto-generated complete-plan zero-history parse.\n"
        "includeFile = beam/plan_generated.txt\n\n"
        'sv:Ph/Default/Modules = 1 "Transportation_Only"\n\n'
        "i:Tf/NumberOfSequentialTimes = 1\n"
        f"{overrides}\n\n"
        'b:Ge/CheckForOverlaps = "False"\n'
        "i:Ts/NumberOfThreads = 1\n"
        'b:Ts/PauseBeforeQuit = "False"\n',
        encoding="utf-8",
    )
    grid_entry.write_text(
        "# Auto-generated scorer/grid zero-history initialization.\n"
        "includeFile = scoring/dose.txt\n\n"
        'sv:Ph/Default/Modules = 1 "Transportation_Only"\n\n'
        "i:Tf/NumberOfSequentialTimes = 1\n"
        f"{overrides}\n\n"
        's:Sc/TPSDoseToMedium/OutputFile = "../topas_output/test/dose_grid_zero_validation"\n'
        's:Sc/TPSDoseToMedium/IfOutputFileAlreadyExists = "Overwrite"\n\n'
        'b:Ge/CheckForOverlaps = "False"\n'
        "i:Ts/NumberOfThreads = 1\n"
        'b:Ts/PauseBeforeQuit = "False"\n',
        encoding="utf-8",
    )
    return parse_entry, grid_entry


def main() -> int:
    args = parse_args()
    if args.histories < 1:
        raise RuntimeError("--histories must be positive")
    if args.threads < 1:
        raise RuntimeError("--threads must be at least 1")
    requested_threads = args.threads
    threads, thread_note = clamp_threads(requested_threads)
    if thread_note:
        print(f"WARNING: {thread_note}")
    root = args.root.expanduser().resolve()
    compatibility = root / "plan_parsed" / "compatibility_summary.txt"
    if not compatibility.is_file() or "READY FOR CURRENT QA WORKFLOW" not in compatibility.read_text(encoding="utf-8"):
        raise RuntimeError("Compatibility gate is missing or blocked; run script 07 first")

    plan = root / "topas" / "beam" / "plan_generated.txt"
    allocation = root / "plan_parsed" / "spot_history_allocation.csv"
    generation_summary = root / "plan_parsed" / "topas_plan_generation_summary.txt"
    dose_grid = root / "topas" / "scoring" / "dose_grid.txt"
    for path in (plan, allocation, generation_summary, dose_grid):
        if not path.is_file():
            raise RuntimeError(f"Required generated input is missing: {path}")
    manual_mode = "Beam input mode: MANUAL_SINGLE_SPOT" in generation_summary.read_text(
        encoding="utf-8"
    )
    source_names = generated_source_names(plan)
    run_kind = "manual single-spot research" if manual_mode else "full RTPLAN spot"
    allocated_total = int(pd.read_csv(allocation)["AllocatedHistories"].sum())
    if allocated_total != args.histories:
        raise RuntimeError(
            f"Generated plan has {allocated_total} histories, requested {args.histories}; "
            "rerun script 04 with the requested total first"
        )

    dose_text_path = root / "topas" / "scoring" / "dose.txt"
    dose_text = dose_text_path.read_text(encoding="utf-8")
    output_match = re.search(r'^s:Sc/TPSDoseToMedium/OutputFile = "([^"]+)"$', dose_text, re.MULTILINE)
    if not output_match:
        raise RuntimeError("Cannot determine scorer output from topas/scoring/dose.txt")
    output_base = output_match.group(1)
    output_binary = (root / "topas" / output_base).resolve().with_suffix(".bin")
    output_header = Path(str(output_binary) + "header")
    archived = None
    if output_binary.exists() or output_header.exists():
        if not args.overwrite:
            raise RuntimeError(
                "Production output already exists and TOPAS is configured for collision-safe Exit; "
                f"rerun with --overwrite to archive it safely before preparing: {output_binary}"
            )
        archived = archive_production_output(
            root,
            args.output_tag or f"full_plan_{args.histories}",
            output_binary=output_binary,
            reason="Automatic archive before TOPAS run preparation",
        )
    output_binary.parent.mkdir(parents=True, exist_ok=True)

    entry = root / "topas" / "run_full_plan_qa.txt"
    summary = root / "plan_parsed" / "topas_run_preparation_summary.txt"
    if (entry.exists() or summary.exists()) and not args.overwrite:
        raise RuntimeError("Run preparation outputs exist; add --overwrite")
    entry.write_text(
        f"# Auto-generated {run_kind} physical-dose QA run.\n"
        "# Not commissioned absolute clinical dose.\n\n"
        "includeFile = physics/carbon_production.txt\n\n"
        f"i:Ts/Seed = {args.seed}\n"
        f"i:Ts/NumberOfThreads = {threads}\n"
        "i:Ts/ShowHistoryCountAtInterval = 1000\n"
        'b:Ge/CheckForOverlaps = "False"\n'
        'b:Ts/PauseBeforeQuit = "False"\n',
        encoding="utf-8",
    )
    physics = root / "topas" / "physics" / "carbon_production.txt"
    physics.parent.mkdir(parents=True, exist_ok=True)
    physics.write_text(
        "# Auto-generated full-plan physics/scorer include.\n\n"
        "includeFile = scoring/dose.txt\n\n"
        'sv:Ph/Default/Modules = 6 "g4em-standard_opt4" "g4h-phy_QGSP_BIC_HP" "g4decay" "g4ion-binarycascade" "g4h-elastic_HP" "g4stopping"\n'
        "d:Ph/Default/CutForAllParticles = 0.05 mm\n",
        encoding="utf-8",
    )
    parse_entry, grid_entry = write_zero_history_entries(root, source_names)
    summary.write_text(
        f"TPS-TOPAS {run_kind} QA run preparation\n"
        "======================================\n"
        f"Histories: {args.histories}\nThreads: {threads}\nSeed: {args.seed}\n"
        f"Threads requested / used: {requested_threads} / {threads} "
        f"(logical CPUs: {logical_cpu_count()})\n"
        + (f"Thread cap applied: {thread_note}\n" if thread_note else "")
        + f"Entry point: {entry}\nExpected dose: {output_binary}\n"
        f"Previous production output archived: {archived['archived_directory'] if archived else 'none'}\n"
        "Important: script 04 must have generated plan_generated.txt with the same history total.\n"
        f"Beam mode: {'MANUAL_SINGLE_SPOT' if manual_mode else 'RTPLAN'}\n"
        f"TOPAS source count: {len(source_names)} ({', '.join(source_names)})\n"
        f"Zero-history entries: {parse_entry}, {grid_entry}\n"
        "Scope: research physical-dose QA; commissioned RTPLAN runs use audited N_plan/N_sim post-calibration, not TPS peak fitting.\n",
        encoding="utf-8",
    )
    print(summary.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
