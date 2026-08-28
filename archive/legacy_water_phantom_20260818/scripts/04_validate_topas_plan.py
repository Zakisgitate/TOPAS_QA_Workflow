#!/usr/bin/env python3
"""Validate the Stage-6 TOPAS phase space against its generated spot audit."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np
import pandas as pd


CARBON12_PDG = 1_000_060_120


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--allocation", type=Path)
    parser.add_argument("--phase-space", type=Path)
    parser.add_argument("--reverse-header", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def read_phase_space(path: Path) -> np.ndarray:
    rows = [line.split() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    values = np.asarray(rows, dtype=float)
    if values.ndim != 2 or values.shape[1] != 12:
        raise RuntimeError(f"Expected 12-column TOPAS ASCII phase space, got {values.shape}")
    return values


def check(condition: bool, message: str, failures: list[str]) -> None:
    if not condition:
        failures.append(message)


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    allocation_path = (
        args.allocation or root / "plan_parsed" / "plan_validation_history_allocation.csv"
    ).resolve()
    phase_path = (
        args.phase_space or root / "topas_output" / "test" / "plan_validation_phase_space.phsp"
    ).resolve()
    reverse_header_path = (
        args.reverse_header
        or root / "topas_output" / "test" / "plan_validation_reverse_phase_space.header"
    ).resolve()
    output = (
        args.output or root / "plan_parsed" / "topas_plan_validation_summary.txt"
    ).resolve()
    if output.exists() and not args.overwrite:
        raise RuntimeError(f"Output exists: {output}; inspect it or add --overwrite")
    output.parent.mkdir(parents=True, exist_ok=True)

    allocation = pd.read_csv(allocation_path)
    phase = read_phase_space(phase_path)
    reverse_header = reverse_header_path.read_text(encoding="utf-8")
    failures: list[str] = []

    expected_total = int(allocation["AllocatedHistories"].sum())
    check(len(phase) == expected_total, "forward phase-space history count mismatch", failures)
    check(np.all(phase[:, 7].astype(np.int64) == CARBON12_PDG), "non-C12 PDG found", failures)
    check(np.allclose(phase[:, 3], -1.0, atol=1e-12), "beam direction is not world -X", failures)
    check(np.allclose(phase[:, 4], 0.0, atol=1e-12), "unexpected world-Y direction cosine", failures)
    check(
        "Number of Original Histories that Reached Phase Space: 0" in reverse_header,
        "one or more particles reached the reverse (+X) plane",
        failures,
    )

    run_lines: list[str] = []
    for run_id, expected in allocation.reset_index(drop=True).iterrows():
        sample = phase[phase[:, 10].astype(int) == run_id]
        expected_n = int(expected["AllocatedHistories"])
        check(len(sample) == expected_n, f"run {run_id}: history count mismatch", failures)
        if len(sample) == 0:
            continue

        energy = float(expected["Energy_Total_MeV"])
        check(np.allclose(sample[:, 5], energy, atol=1e-6), f"run {run_id}: energy mismatch", failures)

        # Source rotation maps IEC X to world patient Y and IEC Y to patient Z.
        observed_y = sample[:, 1] * 10.0
        observed_z = sample[:, 2] * 10.0
        expected_y = float(expected["IEC_X_mm"])
        expected_z = float(expected["IEC_Y_mm"])
        expected_sigma_y = float(expected["Sigma_TOPAS_LocalY_mm"])
        expected_sigma_z = float(expected["Sigma_TOPAS_LocalX_mm"])
        sigma_y = float(observed_y.std(ddof=0))
        sigma_z = float(observed_z.std(ddof=0))
        mean_y = float(observed_y.mean())
        mean_z = float(observed_z.mean())

        se_y = expected_sigma_y / math.sqrt(len(sample))
        se_z = expected_sigma_z / math.sqrt(len(sample))
        check(abs(mean_y - expected_y) <= 4.0 * se_y, f"run {run_id}: Y mean >4 SE", failures)
        check(abs(mean_z - expected_z) <= 4.0 * se_z, f"run {run_id}: Z mean >4 SE", failures)
        check(
            abs(sigma_y / expected_sigma_y - 1.0) <= 0.08,
            f"run {run_id}: Y sigma differs by >8 percent",
            failures,
        )
        check(
            abs(sigma_z / expected_sigma_z - 1.0) <= 0.08,
            f"run {run_id}: Z sigma differs by >8 percent",
            failures,
        )
        run_lines.append(
            f"Run {run_id}: N={len(sample)}, E={energy:.6g} MeV; "
            f"Y mean/sigma={mean_y:.5f}/{sigma_y:.5f} mm "
            f"(expected {expected_y:.5f}/{expected_sigma_y:.5f}); "
            f"Z mean/sigma={mean_z:.5f}/{sigma_z:.5f} mm "
            f"(expected {expected_z:.5f}/{expected_sigma_z:.5f})"
        )

    status = "PASS" if not failures else "FAIL"
    report = "\n".join(
        [
            "PLAN1699 generated TOPAS plan validation",
            "========================================",
            f"Status: {status}",
            f"Allocation: {allocation_path}",
            f"Forward phase space: {phase_path}",
            f"Reverse-plane header: {reverse_header_path}",
            "",
            f"Forward histories / C12 particles: {len(phase)} / {np.sum(phase[:, 7].astype(np.int64) == CARBON12_PDG)}",
            f"Forward world direction cosine X range: {phase[:, 3].min():.6g} .. {phase[:, 3].max():.6g}",
            f"Kinetic energy range: {phase[:, 5].min():.6g} .. {phase[:, 5].max():.6g} MeV",
            "Reverse (+X) plane reached histories: 0",
            "",
            "Per-run checks",
            "--------------",
            *run_lines,
            "",
            "Tolerance: spot means within 4 standard errors; sampled sigma within 8 percent.",
            "Validation physics: Transportation_Only (source reconstruction, not dose physics).",
            "",
            "Failures",
            "--------",
            *(failures or ["None"]),
            "",
        ]
    )
    output.write_text(report, encoding="utf-8")
    print(f"{status}: {output}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
