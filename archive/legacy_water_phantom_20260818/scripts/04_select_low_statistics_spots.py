#!/usr/bin/env python3
"""Select five spatial anchor spots per energy layer for Stage-8 transport QA.

The selected anchors are closest to field center, X-/X+, and Y-/Y+ for each
layer. This is a coarse, deterministic transport sample, not a full-plan dose.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--summary-output", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def choose_anchors(layer: pd.DataFrame) -> list[tuple[str, int]]:
    x = layer["X_mm"].to_numpy(dtype=float)
    y = layer["Y_mm"].to_numpy(dtype=float)
    scores = {
        "center": x * x + y * y,
        "x_min": (x - x.min()) ** 2 + 1e-6 * y * y,
        "x_max": (x - x.max()) ** 2 + 1e-6 * y * y,
        "y_min": (y - y.min()) ** 2 + 1e-6 * x * x,
        "y_max": (y - y.max()) ** 2 + 1e-6 * x * x,
    }
    selected: list[tuple[str, int]] = []
    used: set[int] = set()
    for name, score in scores.items():
        for position in np.argsort(score, kind="stable"):
            row_index = int(layer.index[int(position)])
            if row_index not in used:
                selected.append((name, row_index))
                used.add(row_index)
                break
    if len(selected) != 5:
        raise RuntimeError(f"Could not select five unique anchors for layer {layer.LayerIndex.iloc[0]}")
    return selected


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    source = (args.input or root / "plan_parsed" / "spots.csv").resolve()
    output = (
        args.output or root / "plan_parsed" / "low_statistics_selected_spots.csv"
    ).resolve()
    summary = (
        args.summary_output
        or root / "plan_parsed" / "low_statistics_selection_summary.txt"
    ).resolve()
    if not source.is_file():
        raise RuntimeError(f"Input spot table does not exist: {source}")
    dicom_root = (root / "dicom").resolve()
    for path in (output, summary):
        try:
            path.relative_to(dicom_root)
        except ValueError:
            pass
        else:
            raise RuntimeError(f"Derived output cannot be inside dicom/: {path}")
        if path.exists() and not args.overwrite:
            raise RuntimeError(f"Output exists: {path}; add --overwrite")
        path.parent.mkdir(parents=True, exist_ok=True)

    spots = pd.read_csv(source)
    required = {"LayerIndex", "SpotIndex", "Energy_MeVu", "X_mm", "Y_mm", "MetersetWeight_MU"}
    missing = sorted(required.difference(spots.columns))
    if missing:
        raise RuntimeError(f"Input is missing columns: {missing}")
    selected_rows: list[int] = []
    labels: dict[int, str] = {}
    original_order: dict[int, int] = {}
    for _, layer in spots.groupby("LayerIndex", sort=True):
        for order, (label, row_index) in enumerate(choose_anchors(layer)):
            selected_rows.append(row_index)
            labels[row_index] = label
            original_order[row_index] = order
    selected = spots.loc[selected_rows].copy()
    selected["LowStatisticsAnchor"] = [labels[int(index)] for index in selected.index]
    selected["OriginalSpotIndex"] = selected["SpotIndex"]
    selected["SpotIndex"] = [original_order[int(index)] + 1 for index in selected.index]
    if selected["LayerIndex"].nunique() != spots["LayerIndex"].nunique():
        raise RuntimeError("Selection did not preserve every energy layer")
    if not (selected.groupby("LayerIndex").size() == 5).all():
        raise RuntimeError("Selection is not exactly five spots per layer")

    # Preserve the complete plan's energy-layer fluence exactly. Each layer's
    # total meterset is shared equally by its five spatial QA anchors. These are
    # sampling weights, not claims about the five original spots' clinical MU.
    layer_totals = spots.groupby("LayerIndex")["MetersetWeight_MU"].sum()
    selected["OriginalSpotMetersetWeight_MU"] = selected["MetersetWeight_MU"]
    selected["MetersetWeight_MU"] = selected["LayerIndex"].map(layer_totals) / 5.0
    selected["WeightPerPainting_MU"] = selected["MetersetWeight_MU"]
    selected["RelativeWeight"] = selected["MetersetWeight_MU"] / selected[
        "MetersetWeight_MU"
    ].sum()
    selected["PlanRelativeWeight"] = selected["RelativeWeight"]
    observed_layer_totals = selected.groupby("LayerIndex")["MetersetWeight_MU"].sum()
    if not np.allclose(
        observed_layer_totals.to_numpy(), layer_totals.to_numpy(), rtol=1e-12, atol=1e-8
    ):
        raise RuntimeError("Layer-preserving QA weights do not reproduce full-plan layer totals")
    selected = selected.sort_values(["BeamNumber", "LayerIndex", "SpotIndex"], kind="stable")
    selected.to_csv(output, index=False)

    text = f"""PLAN1699 Stage-8 low-statistics spot selection
================================================
Input spot table (read-only): {source}
Output subset: {output}
Input spots / layers: {len(spots)} / {spots['LayerIndex'].nunique()}
Selected spots / layers: {len(selected)} / {selected['LayerIndex'].nunique()}
Anchors per layer: center, x_min, x_max, y_min, y_max
QA weighting: each full-plan layer total is shared equally by its five anchors
Full-plan / QA meterset sum: {spots['MetersetWeight_MU'].sum():.12g} / {selected['MetersetWeight_MU'].sum():.12g} MU
Energy range: {selected['Energy_MeVu'].min():.10g} .. {selected['Energy_MeVu'].max():.10g} MeV/u
Selected X range: {selected['X_mm'].min():.10g} .. {selected['X_mm'].max():.10g} mm
Selected Y range: {selected['Y_mm'].min():.10g} .. {selected['Y_mm'].max():.10g} mm

Scope
-----
This deterministic subset covers all {spots['LayerIndex'].nunique()} energies, preserves every energy layer's
complete-plan total weight, and samples five spatial field anchors while avoiding
{len(spots):,} sequential Geant4 runs. It is suitable only for coarse direction, range
and field-location QA. It does not reproduce the complete TPS transverse fluence
map and must not be used for quantitative TPS-vs-MC dose agreement.
"""
    summary.write_text(text, encoding="utf-8")
    print(f"Selected {len(selected)} anchors from {spots['LayerIndex'].nunique()} layers")
    print(f"Subset: {output}")
    print(f"Summary: {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
