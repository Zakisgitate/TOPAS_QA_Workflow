#!/usr/bin/env python3
"""Clickable local GUI for the reusable TPS-TOPAS QA workflow."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable, Optional, Sequence

from PIL import Image, ImageTk

try:
    from gui.runtime_monitor import estimate_topas_runtime as _case_runtime_estimate
except ImportError:
    from runtime_monitor import estimate_topas_runtime as _case_runtime_estimate


APP_ROOT = Path(__file__).resolve().parents[1]
READY_TEXT = "READY FOR CURRENT QA WORKFLOW"
DEFAULT_HISTORIES = 100_000
DEFAULT_THREADS = min(12, max(1, os.cpu_count() or 1))
DEFAULT_RANDOM_SEED = 1699
DEFAULT_PROFILE_DEPTH_MM = 100
def estimate_topas_runtime(
    histories: int,
    threads: int,
    *,
    root: Optional[Path] = None,
    beam_model_mode: str = "baseline",
    spot_count: int = 0,
) -> dict[str, object]:
    return _case_runtime_estimate(
        histories,
        threads,
        root=root,
        beam_model_mode=beam_model_mode,
        spot_count=spot_count,
    )


def run_configuration_options(
    root: Path,
    beam_model_mode: str = "baseline",
    spot_count: Optional[int] = None,
) -> list[dict[str, object]]:
    spots_path = root / "plan_parsed" / "spots.csv"
    measured_spot_count = 0
    if spots_path.is_file():
        with spots_path.open(newline="", encoding="utf-8") as stream:
            measured_spot_count = sum(1 for _ in csv.DictReader(stream))
    selected_spot_count = max(1, int(spot_count if spot_count is not None else measured_spot_count))
    logical_cpus = max(1, os.cpu_count() or 1)
    medium_threads = min(6, logical_cpus)
    recommended_threads = min(8, logical_cpus)
    high_threads = min(12, logical_cpus)
    quick_histories = max(100_000, selected_spot_count)
    definitions = (
        ("quick", "Quick diagnostic", quick_histories, min(4, logical_cpus), "Fast geometry/range sanity check; visibly noisy dose."),
        ("balanced", "Balanced QA", max(500_000, selected_spot_count), medium_threads, "Moderate statistics for iteration and profile review."),
        ("recommended", "Recommended", max(1_000_000, selected_spot_count), recommended_threads, "Default full-plan research QA configuration."),
        ("high", "Higher statistics", max(2_000_000, selected_spot_count), high_threads, "Lower Monte Carlo noise; longer sustained system load."),
    )
    options: list[dict[str, object]] = []
    for identifier, label, histories, threads, purpose in definitions:
        estimate = estimate_topas_runtime(
            histories,
            threads,
            root=root,
            beam_model_mode=beam_model_mode,
            spot_count=selected_spot_count,
        )
        options.append(
            {
                "id": identifier,
                "label": label,
                "histories": histories,
                "threads": threads,
                "purpose": purpose,
                **estimate,
            }
        )
    return options


def prepared_run_matches(
    root: Path,
    histories: int,
    threads: int,
    seed: int | None = None,
    beam_settings: Optional[dict[str, object]] = None,
    energy_layer_indices: Optional[list[int]] = None,
) -> bool:
    entry = root / "topas" / "run_full_plan_qa.txt"
    plan_summary = root / "plan_parsed" / "topas_plan_generation_summary.txt"
    states = {item["stage"]: item["state"] for item in collect_status(root)}
    required = (
        "Compatibility gate",
        "RTPLAN parsed",
        "TOPAS geometry",
        "TPS dose grid",
        "Full spot plan",
        "TOPAS run entry",
        "TOPAS preflight",
    )
    settings = beam_settings or {
        "beam_input_mode": "rtplan",
        "beam_model_mode": "baseline",
        "energy_scale": 1.0,
        "energy_offset_mevu": 0.0,
        "spot_size_scale": 1.0,
        "energy_spread_percent": 0.0,
    }
    summary_text = plan_summary.read_text(encoding="utf-8") if plan_summary.is_file() else ""
    mode = str(settings.get("beam_input_mode", "rtplan"))
    model_mode = str(settings.get("beam_model_mode", "baseline"))
    settings_match = all(
        line in summary_text
        for line in (
            f"Beam input mode: {'MANUAL_SINGLE_SPOT' if mode == 'manual' else 'RTPLAN'}",
            f"Beam model mode: {model_mode.upper()}",
            f"Applied energy scale: {settings['energy_scale']:.10g}",
            f"Applied energy offset: {settings['energy_offset_mevu']:.10g} MeV/u",
            f"Applied spot-size scale: {settings['spot_size_scale']:.10g}",
        )
    )
    if model_mode == "commissioned":
        settings_match = settings_match and all(
            line in summary_text
            for line in (
                "Energy source: measured-IDD NNLS discrete spectrum",
                "Transverse source: measured spot-sigma Fermi-Eyges BiGaussian emittance",
                "Fluence source: meterset * commissioned energy-dependent number-per-MU",
            )
        )
        selected_profile = str(settings.get("beam_model_profile", "")).strip()
        if selected_profile:
            settings_match = settings_match and (
                f"Commissioned profile: {Path(selected_profile).expanduser().resolve()}"
                in summary_text
            )
    else:
        settings_match = settings_match and (
            f"BeamEnergySpread: {settings['energy_spread_percent']:.10g} percent" in summary_text
        )
    if mode == "manual":
        settings_match = settings_match and all(
            line in summary_text
            for line in (
                f"Manual energy: {settings['manual_energy_mevu']:.10g} MeV/u",
                f"Manual IEC spot X / Y: {settings['manual_spot_x_mm']:.10g} / {settings['manual_spot_y_mm']:.10g} mm",
                f"Manual IEC spot FWHM X / Y: {settings['manual_spot_fwhm_x_mm']:.10g} / {settings['manual_spot_fwhm_y_mm']:.10g} mm",
            )
        )
    layer_selection = "MANUAL" if mode == "manual" else (
        "ALL" if energy_layer_indices is None else ",".join(str(item) for item in energy_layer_indices)
    )
    layers_match = f"Selected LayerIndex values: {layer_selection}" in summary_text
    return bool(
        entry.is_file()
        and plan_summary.is_file()
        and all(states.get(name) == "READY" for name in required)
        and f"i:Ts/NumberOfThreads = {threads}" in entry.read_text(encoding="utf-8")
        and (seed is None or f"i:Ts/Seed = {seed}" in entry.read_text(encoding="utf-8"))
        and f"Histories requested / allocated: {histories} / {histories}"
        in summary_text
        and settings_match
        and layers_match
    )


@dataclass
class Command:
    label: str
    argv: list[str]
    cwd: Path
    log_path: Optional[Path] = None


def newest(paths: Sequence[Path]) -> Optional[Path]:
    existing = [path for path in paths if path.is_file()]
    return max(existing, key=lambda item: item.stat().st_mtime) if existing else None


def expected_mc_byte_count(root: Path) -> Optional[int]:
    grid = root / "topas" / "scoring" / "dose_grid.txt"
    if not grid.is_file():
        return None
    text = grid.read_text(encoding="utf-8")
    bins: list[int] = []
    for axis in ("X", "Y", "Z"):
        match = re.search(rf"^i:Ge/TPSDoseGrid/{axis}Bins = (\d+)$", text, re.MULTILINE)
        if not match:
            return None
        bins.append(int(match.group(1)))
    return bins[0] * bins[1] * bins[2] * 8


def mc_binary_matches_current_grid(root: Path, path: Path) -> bool:
    expected_bytes = expected_mc_byte_count(root)
    return bool(
        expected_bytes
        and path.is_file()
        and path.stat().st_size == expected_bytes
        and "dose_grid_zero" not in path.name
    )


def discover_mc_binary(root: Path) -> Optional[Path]:
    production = [
        path
        for path in (root / "topas_output" / "production").glob("*.bin")
        if mc_binary_matches_current_grid(root, path)
    ]
    candidate = newest(production)
    if candidate:
        return candidate
    tests = [
        path
        for path in (root / "topas_output" / "test").glob("*.bin")
        if mc_binary_matches_current_grid(root, path)
    ]
    return newest(tests)


def latest_mtime(paths: Sequence[Path]) -> float:
    existing = [path for path in paths if path.exists()]
    return max((path.stat().st_mtime for path in existing), default=0.0)


def outputs_are_current(outputs: Sequence[Path], inputs: Sequence[Path]) -> bool:
    return bool(outputs) and all(path.is_file() for path in outputs) and latest_mtime(outputs) >= latest_mtime(inputs)


def expected_mc_binary(root: Path) -> Optional[Path]:
    scorer = root / "topas" / "scoring" / "dose.txt"
    if not scorer.is_file():
        return None
    match = re.search(
        r'^s:Sc/TPSDoseToMedium/OutputFile = "([^"]+)"$',
        scorer.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    if not match:
        return None
    return (root / "topas" / match.group(1)).resolve().with_suffix(".bin")


def format_history_tag(histories: int) -> str:
    return f"full_plan_{histories}"


def _commissioned_inputs_for_current_outputs(root: Path) -> list[Path]:
    """Track only the model actually recorded by generated outputs.

    Importing an unrelated machine/version must not make a completed case look
    stale.  A newly selected profile is still detected by
    :func:`prepared_run_matches` and forces regeneration.
    """
    allowed = root / "machine_model" / "beam_commissioning"
    candidates = (
        (root / "plan_parsed" / "topas_plan_generation_summary.txt", r"^Commissioned profile: (.+)$"),
        (root / "topas" / "beam" / "beam_geometry.txt", r"profile=([^;\n]+)"),
    )
    for path, pattern in candidates:
        if not path.is_file():
            continue
        match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
        if not match:
            continue
        profile = Path(match.group(1).strip()).expanduser().resolve()
        try:
            profile.relative_to(allowed.resolve())
        except ValueError:
            continue
        if profile.is_file():
            return [item for item in profile.parent.iterdir() if item.is_file()]
    return []


def collect_status(root: Path) -> list[dict[str, str]]:
    root = root.expanduser().resolve()
    ct_files = list((root / "dicom" / "CT").glob("*.dcm"))
    ct_count = len(ct_files)
    modality_counts = {
        name: len(list((root / "dicom" / name).glob("*.dcm")))
        for name in ("RTPLAN", "RTDOSE", "RTSTRUCT")
    }
    dicom_ready = ct_count >= 2 and all(value >= 1 for value in modality_counts.values())
    dicom_files = ct_files + [
        path
        for name in ("RTPLAN", "RTDOSE", "RTSTRUCT")
        for path in (root / "dicom" / name).glob("*.dcm")
    ]
    rtplan_files = list((root / "dicom" / "RTPLAN").glob("*.dcm"))
    dose_files = list((root / "dicom" / "RTDOSE").glob("*.dcm"))
    structure_files = list((root / "dicom" / "RTSTRUCT").glob("*.dcm"))
    compatibility = root / "plan_parsed" / "compatibility_summary.txt"
    patient_model_path = root / "plan_parsed" / "patient_model.json"
    commissioned_inputs = _commissioned_inputs_for_current_outputs(root)
    compatibility_current = outputs_are_current([compatibility, patient_model_path], dicom_files)
    compatible = (
        compatibility_current
        and READY_TEXT in compatibility.read_text(encoding="utf-8")
    )
    parsed_outputs = [root / "plan_parsed" / name for name in ("plan_summary.txt", "energy_layers.csv", "spots.csv")]
    parsed = outputs_are_current(parsed_outputs, rtplan_files)
    geometry_outputs = [
        root / name
        for name in (
            "topas/geometry/world.txt",
            "topas/geometry/patient.txt",
            "topas/geometry/isocenter.txt",
            "topas/beam/beam_geometry.txt",
        )
    ]
    geometry = outputs_are_current(
        geometry_outputs,
        rtplan_files + dose_files + structure_files + ct_files
        + [compatibility, patient_model_path, *commissioned_inputs],
    )
    scoring_outputs = [root / name for name in ("topas/scoring/dose_grid.txt", "topas/scoring/dose.txt")]
    scoring = outputs_are_current(scoring_outputs, rtplan_files + dose_files)
    generation_summary_path = root / "plan_parsed" / "topas_plan_generation_summary.txt"
    plan_outputs = [
        root / "topas" / "beam" / "plan_generated.txt",
        root / "plan_parsed" / "spot_history_allocation.csv",
        root / "plan_parsed" / "spot_history_allocation_metadata.json",
        generation_summary_path,
    ]
    generation_summary = (
        generation_summary_path.read_text(encoding="utf-8")
        if generation_summary_path.is_file()
        else ""
    )
    selection_match = re.search(r"^Selected LayerIndex values: (.+)$", generation_summary, re.MULTILINE)
    beam_model_match = re.search(r"^Beam model mode: (.+)$", generation_summary, re.MULTILINE)
    plan = bool(
        outputs_are_current(
            plan_outputs,
            [root / "plan_parsed" / "spots.csv", *commissioned_inputs]
            if beam_model_match and beam_model_match.group(1).strip() == "COMMISSIONED"
            else [root / "plan_parsed" / "spots.csv"],
        )
        and selection_match
    )
    plan_detail = (
        "One manually defined Energy + spot with all requested histories"
        if selection_match and selection_match.group(1) == "MANUAL"
        else "All RTPLAN energy layers with relative history allocation"
        if selection_match and selection_match.group(1) == "ALL"
        else f"Energy-layer subset {selection_match.group(1)} with relative history allocation"
        if selection_match
        else "Missing current energy-layer audit; rerun stage 6"
    )
    if beam_model_match:
        plan_detail += f"; beam model={beam_model_match.group(1)}"
    prepared_path = root / "topas" / "run_full_plan_qa.txt"
    prepared = bool(
        plan
        and outputs_are_current(
            [prepared_path], [compatibility, *geometry_outputs, *scoring_outputs, *plan_outputs]
        )
    )
    preflight_path = root / "plan_parsed" / "topas_preflight_summary.txt"
    grid_validation_path = root / "plan_parsed" / "topas_dose_grid_validation_summary.txt"
    preflight = bool(
        plan
        and outputs_are_current(
            [preflight_path, grid_validation_path],
            [
                *geometry_outputs, *scoring_outputs, *plan_outputs,
                root / "machine_model" / "HUtoMaterialSchneider.txt", *commissioned_inputs,
            ],
        )
    )
    patient_mode = "unknown"
    if patient_model_path.is_file():
        try:
            patient_mode = str(json.loads(patient_model_path.read_text(encoding="utf-8")).get("mode", "unknown"))
        except (OSError, ValueError):
            patient_mode = "invalid patient_model.json"
    mc = discover_mc_binary(root)
    mc_current = bool(
        mc
        and prepared
        and mc_binary_matches_current_grid(root, mc)
        and mc.stat().st_mtime >= prepared_path.stat().st_mtime
    )
    figures = list((root / "analysis").glob("**/figures/depth_direction*.png"))
    profiles = list((root / "analysis").glob("**/profiles/depth_dose*.csv"))
    profiles_current = bool(
        mc_current
        and figures
        and profiles
        and latest_mtime([*figures, *profiles]) >= mc.stat().st_mtime
    )
    gamma_summaries = list((root / "analysis").glob("**/gamma/gamma_summary_*.txt"))
    gamma_metrics = list((root / "analysis").glob("**/gamma/gamma_metrics_*.csv"))
    gamma_current = False
    if mc_current and gamma_summaries and gamma_metrics:
        mc_marker = f"MC binary: {mc.resolve()}"
        matching_summaries = [
            path
            for path in gamma_summaries
            if path.stat().st_mtime >= mc.stat().st_mtime
            and mc_marker in path.read_text(encoding="utf-8")
        ]
        gamma_current = bool(matching_summaries)
    return [
        {
            "stage": "DICOM input",
            "state": "READY" if dicom_ready else "WAITING",
            "detail": f"CT={ct_count}, RTPLAN={modality_counts['RTPLAN']}, RTDOSE={modality_counts['RTDOSE']}, RTSTRUCT={modality_counts['RTSTRUCT']}",
        },
        {
            "stage": "Compatibility gate",
            "state": "READY" if compatible else "WAITING",
            "detail": f"{patient_mode}: {compatibility}" if compatible else ("Result is stale; rerun stage 2" if compatibility.exists() else "Run stage 2"),
        },
        {
            "stage": "RTPLAN parsed",
            "state": "READY" if parsed else "WAITING",
            "detail": "energy_layers.csv + spots.csv" if parsed else "Missing or stale; run stage 3",
        },
        {
            "stage": "TOPAS geometry",
            "state": "READY" if geometry else "WAITING",
            "detail": f"{patient_mode} generated from current DICOM" if geometry else "Missing or stale; run stage 4",
        },
        {
            "stage": "TPS dose grid",
            "state": "READY" if scoring else "WAITING",
            "detail": "DoseToMedium on exact RPPD grid" if scoring else "Missing or stale; run stage 5",
        },
        {
            "stage": "Full spot plan",
            "state": "READY" if plan else "WAITING",
            "detail": plan_detail if plan else "Missing or stale; run stage 6",
        },
        {
            "stage": "TOPAS run entry",
            "state": "READY" if prepared else "WAITING",
            "detail": "run_full_plan_qa.txt" if prepared else "Missing or stale; run stage 7",
        },
        {
            "stage": "TOPAS preflight",
            "state": "READY" if preflight else "WAITING",
            "detail": "Full parse + zero-history grid PASS" if preflight else "Missing or stale; run stage 8",
        },
        {
            "stage": "MC dose",
            "state": "READY" if mc_current else "WAITING",
            "detail": str(mc) if mc_current else (f"Existing result is older than current preparation: {mc}" if mc else "Run TOPAS or select an existing .bin"),
        },
        {
            "stage": "Profiles and CSV",
            "state": "READY" if profiles_current else "WAITING",
            "detail": (
                f"Current depth figures={len(figures)}, depth CSV={len(profiles)}"
                if profiles_current
                else (
                    f"Historical outputs found ({len(figures)} depth figures, {len(profiles)} depth CSV); export after current MC"
                    if figures or profiles
                    else "No current MC; export after current MC"
                )
            ),
        },
        {
            "stage": "Gamma analysis",
            "state": "READY" if gamma_current else "WAITING",
            "detail": (
                "Current global 3D Gamma result available"
                if gamma_current
                else (
                    f"Historical Gamma results={len(gamma_summaries)}; run after current MC"
                    if gamma_summaries
                    else "No current MC; run Gamma after current MC"
                )
            ),
        },
    ]


class WorkflowApp(tk.Tk):
    def __init__(self, initial_root: Path):
        super().__init__()
        self.title("TPS–TOPAS Carbon-Ion QA Workflow")
        self.geometry("1320x860")
        self.minsize(1120, 720)
        self.configure(background="#F3F6FA")
        self.case_root = tk.StringVar(value=str(initial_root.resolve()))
        self.histories = tk.StringVar(value=str(DEFAULT_HISTORIES))
        self.threads = tk.StringVar(value=str(DEFAULT_THREADS))
        self.seed = tk.StringVar(value=str(DEFAULT_RANDOM_SEED))
        self.profile_depth = tk.StringVar(value=str(DEFAULT_PROFILE_DEPTH_MM))
        self.output_tag = tk.StringVar(value=format_history_tag(DEFAULT_HISTORIES))
        default_topas = shutil.which("topas") or str(Path.home() / "bin" / "topas")
        self.topas_executable = tk.StringVar(value=default_topas)
        mc = discover_mc_binary(initial_root)
        self.mc_binary = tk.StringVar(value=str(mc) if mc else "")
        self.image_direction = tk.StringVar(value="Depth")
        self._image_reference: Optional[ImageTk.PhotoImage] = None
        self._message_queue: queue.Queue[tuple] = queue.Queue()
        self._process: Optional[subprocess.Popen] = None
        self._busy = False
        self._action_buttons: list[ttk.Button] = []
        self._build_style()
        self._build_header()
        self._build_tabs()
        self.after(100, self._drain_queue)
        self.refresh_all()

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background="#F3F6FA")
        style.configure("Card.TFrame", background="white", relief="flat")
        style.configure("TLabel", background="#F3F6FA", foreground="#243247", font=("Helvetica", 12))
        style.configure("Title.TLabel", font=("Helvetica", 24, "bold"), foreground="#14213D")
        style.configure("Subtitle.TLabel", font=("Helvetica", 11), foreground="#54657E")
        style.configure("Card.TLabel", background="white", foreground="#243247", font=("Helvetica", 11))
        style.configure("CardTitle.TLabel", background="white", foreground="#14213D", font=("Helvetica", 13, "bold"))
        style.configure("Primary.TButton", font=("Helvetica", 11, "bold"), padding=(14, 9))
        style.configure("TButton", font=("Helvetica", 11), padding=(11, 7))
        style.configure("Treeview", rowheight=31, font=("Helvetica", 11), background="white", fieldbackground="white")
        style.configure("Treeview.Heading", font=("Helvetica", 11, "bold"))
        style.configure("TNotebook.Tab", font=("Helvetica", 11, "bold"), padding=(16, 9))

    def _build_header(self) -> None:
        header = ttk.Frame(self, padding=(24, 18, 24, 12))
        header.pack(fill="x")
        ttk.Label(header, text="TPS–TOPAS QA Workflow", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Reusable single-beam carbon-ion PBS physical-dose shape verification",
            style="Subtitle.TLabel",
        ).grid(row=1, column=0, sticky="w", pady=(3, 12))
        path_row = ttk.Frame(header)
        path_row.grid(row=2, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        path_row.columnconfigure(1, weight=1)
        ttk.Label(path_row, text="Case folder").grid(row=0, column=0, padx=(0, 8))
        ttk.Entry(path_row, textvariable=self.case_root).grid(row=0, column=1, sticky="ew")
        ttk.Button(path_row, text="Browse…", command=self.choose_root).grid(row=0, column=2, padx=8)
        init = ttk.Button(path_row, text="Initialize case", command=self.initialize_case)
        init.grid(row=0, column=3)
        self._action_buttons.append(init)

    def _build_tabs(self) -> None:
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=24, pady=(0, 20))
        self.workflow_tab = ttk.Frame(self.notebook, padding=16)
        self.results_tab = ttk.Frame(self.notebook, padding=16)
        self.logs_tab = ttk.Frame(self.notebook, padding=16)
        self.guide_tab = ttk.Frame(self.notebook, padding=16)
        self.notebook.add(self.workflow_tab, text="Workflow")
        self.notebook.add(self.results_tab, text="Results")
        self.notebook.add(self.logs_tab, text="Run log")
        self.notebook.add(self.guide_tab, text="Scope & guide")
        self._build_workflow_tab()
        self._build_results_tab()
        self._build_logs_tab()
        self._build_guide_tab()

    def _build_workflow_tab(self) -> None:
        self.workflow_tab.columnconfigure(0, weight=3)
        self.workflow_tab.columnconfigure(1, weight=2)
        left = ttk.Frame(self.workflow_tab, style="Card.TFrame", padding=18)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        right = ttk.Frame(self.workflow_tab, style="Card.TFrame", padding=18)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self.workflow_tab.rowconfigure(0, weight=1)

        ttk.Label(left, text="Preparation and calculation", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))
        steps = (
            ("1", "DICOM geometry check", "References, grid and isocenter", self.run_geometry_check),
            ("2", "Compatibility gate", "Stop unsupported geometry/patient cases", self.run_compatibility),
            ("3", "Parse RT Ion Plan", "Energy layers, spots and weights", self.run_plan_parse),
            ("4", "Generate case geometry", "Water box or DICOM CT patient and beam transform", self.run_case_geometry),
            ("5", "Build TPS dose grid", "Exact RPPD-aligned DoseToMedium scorer", self.run_scoring),
            ("6", "Generate full spot plan", "Allocate selected Monte Carlo histories", self.run_full_plan),
            ("7", "Prepare TOPAS run", "Entry point, threads and safe output", self.run_prepare),
            ("8", "TOPAS preflight", "Zero-history parse and exact grid test", self.run_topas_preflight),
            ("9", "Run TOPAS", "Long calculation; explicit confirmation", self.run_topas),
            ("10", "Export profiles", "English plots and depth-dose CSV", self.run_analysis),
        )
        for row, (number, title, detail, callback) in enumerate(steps, start=1):
            ttk.Label(left, text=number, style="CardTitle.TLabel", width=3).grid(row=row, column=0, sticky="nw", pady=5)
            text_box = ttk.Frame(left, style="Card.TFrame")
            text_box.grid(row=row, column=1, sticky="ew", pady=5)
            ttk.Label(text_box, text=title, style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(text_box, text=detail, style="Card.TLabel").pack(anchor="w")
            button = ttk.Button(left, text="Run", command=callback, width=10)
            button.grid(row=row, column=2, sticky="e", padx=(12, 0), pady=5)
            self._action_buttons.append(button)
        left.columnconfigure(1, weight=1)

        parameters = ttk.LabelFrame(right, text=" Run parameters ", padding=12)
        parameters.pack(fill="x")
        fields = (
            ("Histories", self.histories),
            ("Threads", self.threads),
            ("Random seed", self.seed),
            ("Profile depth (mm)", self.profile_depth),
            ("Output tag", self.output_tag),
            ("TOPAS executable", self.topas_executable),
        )
        for row, (label, variable) in enumerate(fields):
            ttk.Label(parameters, text=label).grid(row=row, column=0, sticky="w", pady=4)
            ttk.Entry(parameters, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=(10, 0), pady=4)
        parameters.columnconfigure(1, weight=1)
        mc_row = ttk.Frame(parameters)
        mc_row.grid(row=len(fields), column=0, columnspan=2, sticky="ew", pady=(6, 0))
        mc_row.columnconfigure(0, weight=1)
        ttk.Entry(mc_row, textvariable=self.mc_binary).grid(row=0, column=0, sticky="ew")
        ttk.Button(mc_row, text="Select MC…", command=self.choose_mc).grid(row=0, column=1, padx=(8, 0))
        reset = ttk.Button(parameters, text="Reset defaults", command=self.reset_defaults)
        reset.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self._action_buttons.append(reset)

        batch = ttk.Button(right, text="Run stages 1–7", style="Primary.TButton", command=self.run_preparation_pipeline)
        batch.pack(fill="x", pady=(15, 8))
        self._action_buttons.append(batch)
        stop = ttk.Button(right, text="Stop current process", command=self.stop_process)
        stop.pack(fill="x", pady=4)
        self.stop_button = stop
        ttk.Button(right, text="Refresh status", command=self.refresh_all).pack(fill="x", pady=4)

        ttk.Label(right, text="Case status", style="CardTitle.TLabel").pack(anchor="w", pady=(18, 7))
        self.status_tree = ttk.Treeview(right, columns=("state", "detail"), show="tree headings", height=10)
        self.status_tree.heading("#0", text="Stage")
        self.status_tree.heading("state", text="State")
        self.status_tree.heading("detail", text="Detail")
        self.status_tree.column("#0", width=155, stretch=False)
        self.status_tree.column("state", width=85, stretch=False)
        self.status_tree.column("detail", width=320)
        self.status_tree.pack(fill="both", expand=True)
        self.status_tree.tag_configure("READY", foreground="#16734B")
        self.status_tree.tag_configure("WAITING", foreground="#9A5C00")

    def _build_results_tab(self) -> None:
        toolbar = ttk.Frame(self.results_tab)
        toolbar.pack(fill="x", pady=(0, 10))
        ttk.Label(toolbar, text="Direction").pack(side="left")
        selector = ttk.Combobox(
            toolbar,
            textvariable=self.image_direction,
            values=("Depth", "Transverse X", "Transverse Y"),
            state="readonly",
            width=18,
        )
        selector.pack(side="left", padx=8)
        selector.bind("<<ComboboxSelected>>", lambda _event: self.refresh_results())
        ttk.Button(toolbar, text="Refresh", command=self.refresh_results).pack(side="left")
        ttk.Button(toolbar, text="Open output folder", command=self.open_output_folder).pack(side="right")
        pane = ttk.Panedwindow(self.results_tab, orient="horizontal")
        pane.pack(fill="both", expand=True)
        image_card = ttk.Frame(pane, style="Card.TFrame", padding=12)
        summary_card = ttk.Frame(pane, style="Card.TFrame", padding=12)
        pane.add(image_card, weight=4)
        pane.add(summary_card, weight=2)
        self.image_label = ttk.Label(image_card, text="No result image yet", style="Card.TLabel", anchor="center")
        self.image_label.pack(fill="both", expand=True)
        ttk.Label(summary_card, text="Latest profile summary", style="CardTitle.TLabel").pack(anchor="w", pady=(0, 8))
        self.summary_text = tk.Text(summary_card, wrap="word", font=("Menlo", 10), bd=0, padx=8, pady=8)
        self.summary_text.pack(fill="both", expand=True)

    def _build_logs_tab(self) -> None:
        top = ttk.Frame(self.logs_tab)
        top.pack(fill="x", pady=(0, 8))
        ttk.Label(top, text="Commands and live output", style="CardTitle.TLabel").pack(side="left")
        ttk.Button(top, text="Clear", command=lambda: self.log_text.delete("1.0", "end")).pack(side="right")
        self.log_text = tk.Text(
            self.logs_tab,
            wrap="none",
            background="#101827",
            foreground="#D7E2F0",
            insertbackground="white",
            font=("Menlo", 10),
            padx=12,
            pady=12,
        )
        self.log_text.pack(fill="both", expand=True)

    def _build_guide_tab(self) -> None:
        guide = tk.Text(self.guide_tab, wrap="word", font=("Helvetica", 12), bd=0, padx=22, pady=18)
        guide.pack(fill="both", expand=True)
        guide.insert(
            "1.0",
            """Validated workflow scope

This GUI currently accepts one carbon-ion PBS beam in HFS position, gantry 90°, couch/pitch/roll 0°, and an axis-aligned regular physical-plan RTDOSE grid. The patient model is selected automatically: an artificial uniform 0-HU CT plus a rectangular External becomes a water box; a supported axial clinical CT becomes a TOPAS TsDicomPatient using the project HU-to-material table. Unsupported coordinates are blocked before calculation.

How to verify a new TPS plan

1. Create or select a case folder and click Initialize case.
2. Copy—not move—the exported DICOM objects into dicom/CT, RTPLAN, RTDOSE and RTSTRUCT.
3. Click Run stages 1–7. Review the compatibility and parsing summaries.
4. For patient CT, review the HU range and calibration warning. Click Run TOPAS and select Quick, Balanced, Recommended, Higher-statistics or a custom histories/threads plan. Review the estimated time range and every uncommissioned-model warning before starting.
5. If the selected histories, threads or seed differ from the prepared run, the GUI automatically rebuilds stages 6–8 before particle transport.
6. Select the generated .bin and export English depth/X/Y plots plus CSV tables.

Interpretation limits

Commissioned RTPLAN results use an audited N_plan/N_sim particle-number scale; TPS dose is not used to fit MC output. This remains research physical-dose QA, not biological/RBE dose or clinical commissioning. The bundled Schneider table is generic, and monitor-chamber traceability, MRF4 validation, model uncertainty and acceptance criteria still require independent validation.
""",
        )
        guide.configure(state="disabled")

    def root_path(self) -> Path:
        value = Path(self.case_root.get()).expanduser().resolve()
        if value == Path("/"):
            raise RuntimeError("Unsafe case folder")
        return value

    def int_parameter(self, variable: tk.StringVar, label: str, minimum: int = 1) -> int:
        try:
            value = int(variable.get())
        except ValueError as exc:
            raise RuntimeError(f"{label} must be an integer") from exc
        if value < minimum:
            raise RuntimeError(f"{label} must be at least {minimum}")
        return value

    def thread_parameter(self) -> int:
        """Threads converge onto the local core count before any script runs.

        More Geant4 workers than logical CPUs only buys kernel context
        switching: the same 43,919-spot plan measured 1.4-2.1x longer wall time
        at 64 threads than at 4 on a 15-core machine.
        """
        requested = self.int_parameter(self.threads, "Threads")
        logical_cpus = max(1, os.cpu_count() or 1)
        if requested <= logical_cpus:
            return requested
        self.threads.set(str(logical_cpus))
        messagebox.showwarning(
            "Thread count capped",
            f"{requested} threads were requested but this machine has {logical_cpus} "
            f"logical CPUs. Using {logical_cpus}; oversubscribed workers make the run "
            "slower and non-reproducible.",
        )
        return logical_cpus

    def script(self, name: str, *arguments: str) -> Command:
        return Command(
            label=name,
            argv=[sys.executable, str(APP_ROOT / "scripts" / name), "--root", str(self.root_path()), *arguments],
            cwd=self.root_path(),
        )

    def choose_root(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.case_root.get(), title="Select TPS-TOPAS case folder")
        if selected:
            self.case_root.set(selected)
            mc = discover_mc_binary(Path(selected))
            self.mc_binary.set(str(mc) if mc else "")
            self.refresh_all()

    def choose_mc(self) -> None:
        selected = filedialog.askopenfilename(
            initialdir=str(self.root_path() / "topas_output"),
            title="Select TOPAS DoseToMedium binary",
            filetypes=(("TOPAS binary", "*.bin"), ("All files", "*")),
        )
        if selected:
            self.mc_binary.set(selected)

    def reset_defaults(self) -> None:
        """Restore editable run settings without touching the selected case or its files."""
        self.histories.set(str(DEFAULT_HISTORIES))
        self.threads.set(str(DEFAULT_THREADS))
        self.seed.set(str(DEFAULT_RANDOM_SEED))
        self.profile_depth.set(str(DEFAULT_PROFILE_DEPTH_MM))
        self.output_tag.set(format_history_tag(DEFAULT_HISTORIES))
        self.topas_executable.set(shutil.which("topas") or str(Path.home() / "bin" / "topas"))
        mc = discover_mc_binary(self.root_path())
        self.mc_binary.set(str(mc) if mc else "")
        self.image_direction.set("Depth")
        self.refresh_all()
        messagebox.showinfo(
            "Defaults restored",
            "Run parameters and the result view were restored to their initial values.\n\n"
            "The current case, imported DICOM, logs and calculation results were not changed.",
        )

    def initialize_case(self) -> None:
        root = self.root_path()
        command = Command(
            "Initialize case",
            [sys.executable, str(APP_ROOT / "scripts" / "10_initialize_case.py"), "--case-root", str(root)],
            APP_ROOT,
        )
        self.run_commands("Initialize case", [command])

    def run_geometry_check(self) -> None:
        self.run_commands("DICOM geometry check", [self.script("02_check_dicom_geometry.py")])

    def run_compatibility(self) -> None:
        self.run_commands("Compatibility gate", [self.script("07_validate_case_compatibility.py", "--overwrite")])

    def run_plan_parse(self) -> None:
        self.run_commands("RTPLAN parsing", [self.script("01_parse_ion_plan.py", "--overwrite")])

    def run_case_geometry(self) -> None:
        self.run_commands("Case geometry", [self.script("08_generate_case_geometry.py", "--overwrite")])

    def run_scoring(self) -> None:
        self.run_commands("TPS dose grid", [self.script("03_build_topas_dose_scoring.py", "--overwrite")])

    def run_full_plan(self) -> None:
        histories = self.int_parameter(self.histories, "Histories")
        self.run_commands(
            "Full spot plan",
            [self.script("04_generate_topas_plan.py", "--total-histories", str(histories), "--overwrite")],
        )

    def run_prepare(self) -> None:
        histories = self.int_parameter(self.histories, "Histories")
        threads = self.thread_parameter()
        seed = self.int_parameter(self.seed, "Random seed", 0)
        self.run_commands(
            "TOPAS run preparation",
            [
                self.script(
                    "09_prepare_topas_run.py",
                    "--histories",
                    str(histories),
                    "--threads",
                    str(threads),
                    "--seed",
                    str(seed),
                    "--overwrite",
                )
            ],
        )

    def preparation_commands(self) -> list[Command]:
        histories = self.int_parameter(self.histories, "Histories")
        threads = self.thread_parameter()
        seed = self.int_parameter(self.seed, "Random seed", 0)
        return [
            self.script("02_check_dicom_geometry.py"),
            self.script("07_validate_case_compatibility.py", "--overwrite"),
            self.script("01_parse_ion_plan.py", "--overwrite"),
            self.script("08_generate_case_geometry.py", "--overwrite"),
            self.script("03_build_topas_dose_scoring.py", "--overwrite"),
            self.script("04_generate_topas_plan.py", "--total-histories", str(histories), "--overwrite"),
            self.script(
                "09_prepare_topas_run.py",
                "--histories",
                str(histories),
                "--threads",
                str(threads),
                "--seed",
                str(seed),
                "--overwrite",
            ),
        ]

    def run_preparation_pipeline(self) -> None:
        try:
            commands = self.preparation_commands()
        except Exception as exc:
            messagebox.showerror("Invalid parameters", str(exc))
            return
        self.run_commands("Preparation stages 1–7", commands)

    def select_run_configuration(self) -> Optional[tuple[int, int]]:
        options = run_configuration_options(self.root_path())
        dialog = tk.Toplevel(self)
        dialog.title("Select TOPAS calculation plan")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        selected = tk.StringVar(value="recommended")
        custom_histories = tk.StringVar(value=self.histories.get())
        custom_threads = tk.StringVar(value=self.threads.get())
        result: list[tuple[int, int]] = []

        frame = ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="TOPAS calculation plans", style="Title.TLabel").grid(
            row=0, column=0, columnspan=5, sticky="w"
        )
        ttk.Label(
            frame,
            text="Estimates use the measured historical water-phantom 150,000-history / 4-thread benchmark. "
            "Patient CT and system load may change the actual time.",
            wraplength=720,
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(5, 15))
        headings = ("", "Plan", "Histories", "Threads", "Estimated time")
        for column, label in enumerate(headings):
            ttk.Label(frame, text=label, style="CardTitle.TLabel").grid(
                row=2, column=column, sticky="w", padx=(0, 12), pady=(0, 7)
            )
        for row, option in enumerate(options, start=3):
            estimate = (
                f"{option['hours']:.1f} h "
                f"(rough range {option['low_hours']:.1f}–{option['high_hours']:.1f} h)"
            )
            ttk.Radiobutton(frame, variable=selected, value=option["id"]).grid(row=row, column=0)
            plan_box = ttk.Frame(frame)
            plan_box.grid(row=row, column=1, sticky="w", padx=(0, 12), pady=5)
            ttk.Label(plan_box, text=str(option["label"]), style="CardTitle.TLabel").pack(anchor="w")
            ttk.Label(plan_box, text=str(option["purpose"]), wraplength=285).pack(anchor="w")
            ttk.Label(frame, text=f"{int(option['histories']):,}").grid(row=row, column=2, sticky="w")
            ttk.Label(frame, text=str(option["threads"])).grid(row=row, column=3, sticky="w")
            ttk.Label(frame, text=estimate).grid(row=row, column=4, sticky="w")

        custom_row = 3 + len(options)
        ttk.Radiobutton(frame, variable=selected, value="custom").grid(row=custom_row, column=0)
        ttk.Label(frame, text="Custom", style="CardTitle.TLabel").grid(row=custom_row, column=1, sticky="w")
        ttk.Entry(frame, textvariable=custom_histories, width=14).grid(row=custom_row, column=2, sticky="w")
        ttk.Entry(frame, textvariable=custom_threads, width=8).grid(row=custom_row, column=3, sticky="w")
        custom_estimate = ttk.Label(frame, text="")
        custom_estimate.grid(row=custom_row, column=4, sticky="w")

        def update_custom(*_args) -> None:
            try:
                histories = int(custom_histories.get())
                threads = int(custom_threads.get())
                estimate = estimate_topas_runtime(histories, threads, root=self.root)
                custom_estimate.configure(
                    text=f"{estimate['hours']:.1f} h (rough range {estimate['low_hours']:.1f}–{estimate['high_hours']:.1f} h)"
                )
            except (ValueError, ZeroDivisionError):
                custom_estimate.configure(text="Enter positive integers")

        custom_histories.trace_add("write", update_custom)
        custom_threads.trace_add("write", update_custom)
        update_custom()

        ttk.Label(
            frame,
            text="If histories or threads differ from the current prepared run, stages 6–8 are rebuilt automatically before transport.",
            wraplength=720,
        ).grid(row=custom_row + 1, column=0, columnspan=5, sticky="w", pady=(14, 8))
        buttons = ttk.Frame(frame)
        buttons.grid(row=custom_row + 2, column=0, columnspan=5, sticky="e", pady=(8, 0))

        def accept() -> None:
            identifier = selected.get()
            if identifier == "custom":
                try:
                    histories = int(custom_histories.get())
                    threads = int(custom_threads.get())
                except ValueError:
                    messagebox.showerror("Invalid custom plan", "Histories and threads must be integers.", parent=dialog)
                    return
            else:
                option = next(item for item in options if item["id"] == identifier)
                histories, threads = int(option["histories"]), int(option["threads"])
            logical_cpus = max(1, os.cpu_count() or 1)
            if histories < 1 or threads < 1:
                messagebox.showerror("Invalid custom plan", "Histories and threads must be positive.", parent=dialog)
                return
            if threads > logical_cpus:
                messagebox.showerror(
                    "Too many threads",
                    f"This machine has {logical_cpus} logical CPUs. Running more Geant4 workers than "
                    "cores only adds kernel context switching: the same plan measured 1.4–2.1x longer "
                    f"wall time at 64 threads. Use 1–{logical_cpus}.",
                    parent=dialog,
                )
                return
            result.append((histories, threads))
            dialog.destroy()

        ttk.Button(buttons, text="Cancel", command=dialog.destroy).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Use selected plan", style="Primary.TButton", command=accept).pack(side="left")
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        self.wait_window(dialog)
        return result[0] if result else None

    def run_topas(self) -> None:
        root = self.root_path()
        core_status = {item["stage"]: item["state"] for item in collect_status(root)}
        core_required = ("Compatibility gate", "RTPLAN parsed", "TOPAS geometry", "TPS dose grid")
        core_missing = [name for name in core_required if core_status.get(name) != "READY"]
        if core_missing:
            messagebox.showerror(
                "Preparation is incomplete",
                "Run stages 1–5 before selecting a TOPAS plan:\n" + "\n".join(core_missing),
            )
            return
        configuration = self.select_run_configuration()
        if configuration is None:
            return
        histories, threads = configuration
        self.histories.set(str(histories))
        self.threads.set(str(threads))
        self.output_tag.set(format_history_tag(histories))
        status = {item["stage"]: item["state"] for item in collect_status(root)}
        executable = self.resolve_topas()
        if not executable:
            messagebox.showerror("TOPAS not found", f"Cannot find executable: {self.topas_executable.get()}")
            return
        output = expected_mc_binary(root)
        if output and (output.exists() or Path(str(output) + "header").exists()):
            messagebox.showerror("Output collision", f"Archive or rename the existing production output first:\n{output}")
            return
        estimate = estimate_topas_runtime(histories, threads, root=root)
        seed = self.int_parameter(self.seed, "Random seed", 0)
        needs_rebuild = not prepared_run_matches(root, histories, threads, seed)
        proceed = messagebox.askyesno(
            "Start long TOPAS calculation?",
            f"Histories: {histories:,}\nThreads: {threads}\n"
            f"Estimated time: about {estimate['hours']:.1f} h\n"
            f"Rough range: {estimate['low_hours']:.1f}–{estimate['high_hours']:.1f} h\n"
            f"Automatic preparation + preflight: {'required' if needs_rebuild else 'not required'}\n\n"
            "This is an uncommissioned physical-dose shape QA, not a clinical calculation. Start now?",
        )
        if not proceed:
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        log_path = root / "topas_output" / "production" / f"run_full_plan_qa_{timestamp}.log"
        commands: list[Command] = []
        if needs_rebuild:
            commands.extend(
                [
                    self.script("04_generate_topas_plan.py", "--total-histories", str(histories), "--overwrite"),
                    self.script(
                        "09_prepare_topas_run.py",
                        "--histories", str(histories), "--threads", str(threads),
                        "--seed", self.seed.get(), "--overwrite",
                    ),
                    Command(
                        "TOPAS full plan parse", [str(executable), "validate_plan_full_parse.txt"],
                        root / "topas", root / "topas_output" / "test" / "validate_plan_full_parse.log",
                    ),
                    Command(
                        "TOPAS zero-history grid", [str(executable), "validate_dose_grid.txt"],
                        root / "topas", root / "topas_output" / "test" / "validate_dose_grid.log",
                    ),
                    self.script("03_validate_topas_dose_scoring.py", "--overwrite"),
                    self.script("12_validate_topas_preflight.py", "--overwrite"),
                ]
            )
        entry = root / "topas" / "run_full_plan_qa.txt"
        commands.append(Command("TOPAS full-plan run", [str(executable), entry.name], entry.parent, log_path))
        self.run_commands("Prepare and run TOPAS" if needs_rebuild else "TOPAS full-plan run", commands, on_success=self._select_expected_mc)

    def resolve_topas(self) -> Optional[Path]:
        executable = Path(self.topas_executable.get()).expanduser()
        if executable.is_file():
            return executable
        resolved = shutil.which(self.topas_executable.get())
        return Path(resolved) if resolved else None

    def run_topas_preflight(self) -> None:
        root = self.root_path()
        executable = self.resolve_topas()
        if not executable:
            messagebox.showerror("TOPAS not found", f"Cannot find executable: {self.topas_executable.get()}")
            return
        required = [
            root / "topas" / "validate_plan_full_parse.txt",
            root / "topas" / "validate_dose_grid.txt",
        ]
        missing = [path for path in required if not path.is_file()]
        if missing:
            messagebox.showerror(
                "Preflight files missing",
                "Initialize this case from the full project template or restore:\n"
                + "\n".join(map(str, missing)),
            )
            return
        commands = [
            Command(
                "TOPAS full plan parse",
                [str(executable), required[0].name],
                required[0].parent,
                root / "topas_output" / "test" / "validate_plan_full_parse.log",
            ),
            Command(
                "TOPAS zero-history grid",
                [str(executable), required[1].name],
                required[1].parent,
                root / "topas_output" / "test" / "validate_dose_grid.log",
            ),
            self.script("03_validate_topas_dose_scoring.py", "--overwrite"),
            self.script("12_validate_topas_preflight.py", "--overwrite"),
        ]
        self.run_commands("TOPAS zero-history preflight", commands)

    def _select_expected_mc(self) -> None:
        expected = expected_mc_binary(self.root_path())
        if expected and expected.is_file():
            self.mc_binary.set(str(expected))
            self.output_tag.set(format_history_tag(self.int_parameter(self.histories, "Histories")))

    def run_analysis(self) -> None:
        mc = Path(self.mc_binary.get()).expanduser()
        if not mc.is_file():
            messagebox.showerror("MC dose missing", "Select an existing TOPAS .bin dose file.")
            return
        expected = expected_mc_binary(self.root_path())
        selected_is_current_production = bool(expected and mc.resolve() == expected.resolve())
        if not selected_is_current_production:
            proceed = messagebox.askyesno(
                "Analyze a non-current MC result?",
                "The selected binary is not the expected production result for the current preparation. "
                "Use it only as an explicitly historical/diagnostic comparison. Continue?",
            )
            if not proceed:
                return
        try:
            float(self.profile_depth.get())
        except ValueError:
            messagebox.showerror("Invalid profile depth", "Profile depth must be numeric.")
            return
        tag = self.output_tag.get().strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]+", tag):
            messagebox.showerror("Invalid output tag", "Use letters, digits, underscore or hyphen only.")
            return
        log = newest(
            list((self.root_path() / "topas_output" / "production").glob("run*.log"))
            + list((self.root_path() / "topas_output" / "test").glob("run*.log"))
        )
        arguments = [
            "--mc-binary",
            str(mc.resolve()),
            "--profile-depth-mm",
            self.profile_depth.get(),
            "--output-tag",
            tag,
            "--mc-label",
            "MC (TOPAS particle-calibrated)",
            "--full-plan",
            "--overwrite",
        ]
        if log:
            arguments.extend(("--run-log", str(log)))
        self.run_commands(
            "Profile export",
            [self.script("06_export_three_direction_profiles.py", *arguments)],
            on_success=lambda: (self.refresh_results(), self.notebook.select(self.results_tab)),
        )

    def run_commands(
        self,
        title: str,
        commands: Sequence[Command],
        on_success: Optional[Callable[[], None]] = None,
    ) -> None:
        if self._busy:
            messagebox.showwarning("Workflow busy", "Wait for or stop the current process first.")
            return
        self._set_busy(True)
        self.notebook.select(self.logs_tab)
        self._append_log(f"\n=== {title} ===\n")

        def worker() -> None:
            ok = True
            error = ""
            try:
                for command in commands:
                    self._message_queue.put(("log", f"\n$ {' '.join(command.argv)}\n"))
                    if command.log_path:
                        command.log_path.parent.mkdir(parents=True, exist_ok=True)
                    process = subprocess.Popen(
                        command.argv,
                        cwd=str(command.cwd),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1,
                    )
                    self._process = process
                    captured: list[str] = []
                    assert process.stdout is not None
                    for line in process.stdout:
                        captured.append(line)
                        self._message_queue.put(("log", line))
                    return_code = process.wait()
                    self._process = None
                    if command.log_path:
                        command.log_path.write_text("".join(captured), encoding="utf-8")
                    if return_code != 0:
                        ok = False
                        error = f"{command.label} exited with status {return_code}"
                        break
            except Exception as exc:
                ok = False
                error = str(exc)
            self._message_queue.put(("done", title, ok, error, on_success))

        threading.Thread(target=worker, daemon=True).start()

    def stop_process(self) -> None:
        process = self._process
        if process and process.poll() is None:
            if messagebox.askyesno("Stop process?", "Terminate the current calculation/process?"):
                process.terminate()
                self._append_log("\nTermination requested by user.\n")

    def _drain_queue(self) -> None:
        try:
            while True:
                item = self._message_queue.get_nowait()
                if item[0] == "log":
                    self._append_log(item[1])
                elif item[0] == "done":
                    _, title, ok, error, callback = item
                    self._set_busy(False)
                    self.refresh_all()
                    if ok:
                        if callback:
                            callback()
                        messagebox.showinfo("Completed", f"{title} completed successfully.")
                    else:
                        messagebox.showerror("Workflow stopped", f"{title} did not complete.\n\n{error}\n\nSee Run log for details.")
        except queue.Empty:
            pass
        self.after(100, self._drain_queue)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        for button in self._action_buttons:
            button.configure(state=state)
        self.stop_button.configure(state="normal" if busy else "disabled")

    def _append_log(self, text: str) -> None:
        self.log_text.insert("end", text)
        self.log_text.see("end")

    def refresh_all(self) -> None:
        try:
            status = collect_status(self.root_path())
        except Exception as exc:
            status = [{"stage": "Case folder", "state": "WAITING", "detail": str(exc)}]
        for item in self.status_tree.get_children():
            self.status_tree.delete(item)
        for row in status:
            self.status_tree.insert("", "end", text=row["stage"], values=(row["state"], row["detail"]), tags=(row["state"],))
        self.refresh_results()

    def result_path(self) -> Optional[Path]:
        root = self.root_path()
        tag = self.output_tag.get().strip()
        prefix = {
            "Depth": "depth_direction",
            "Transverse X": "transverse_x",
            "Transverse Y": "transverse_y",
        }[self.image_direction.get()]
        exact = root / "analysis" / "figures" / f"{prefix}_{tag}.png"
        if exact.is_file():
            return exact
        return newest(list((root / "analysis" / "figures").glob(f"{prefix}*.png")))

    def refresh_results(self) -> None:
        path = self.result_path()
        if path:
            image = Image.open(path).convert("RGB")
            image.thumbnail((820, 630), Image.Resampling.LANCZOS)
            self._image_reference = ImageTk.PhotoImage(image)
            self.image_label.configure(image=self._image_reference, text="")
        else:
            self._image_reference = None
            self.image_label.configure(image="", text="No result image yet")
        tag = self.output_tag.get().strip()
        summary = self.root_path() / "analysis" / "profiles" / f"profile_export_summary_{tag}.txt"
        if not summary.is_file():
            summary = newest(list((self.root_path() / "analysis" / "profiles").glob("profile_export_summary*.txt")))
        self.summary_text.configure(state="normal")
        self.summary_text.delete("1.0", "end")
        self.summary_text.insert("1.0", summary.read_text(encoding="utf-8") if summary else "No profile summary yet.")
        self.summary_text.configure(state="disabled")

    def open_output_folder(self) -> None:
        path = self.root_path() / "analysis"
        path.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["open", str(path)])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=APP_ROOT)
    parser.add_argument("--smoke-test", action="store_true", help="Inspect status without starting Tk")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.smoke_test:
        print(json.dumps(collect_status(args.root), indent=2, ensure_ascii=False))
        return 0
    app = WorkflowApp(args.root.expanduser().resolve())
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
