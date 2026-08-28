from __future__ import annotations

import importlib.util
from pathlib import Path
import re
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

from gui.batch_queue import _failure_reason
from gui.web_app import history_budget_warning


APP_ROOT = Path(__file__).resolve().parents[1]
BASELINE_BEAM = {"beam_input_mode": "rtplan", "beam_model_mode": "baseline"}


def load_generator():
    spec = importlib.util.spec_from_file_location(
        "plan_generator", APP_ROOT / "scripts" / "04_generate_topas_plan.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SparseAllocationTest(unittest.TestCase):
    """A short test run is allowed; it must also actually be short.

    A spot with zero primaries still costs one sequential Geant4 Run, so the
    zero-history spots have to leave the timeline or the "test" takes as long
    as the full plan.
    """

    def setUp(self) -> None:
        self.generator = load_generator()

    def test_sparse_total_allocates_one_primary_to_the_heaviest_spots(self) -> None:
        weights = np.array([5.0, 1.0, 4.0, 2.0, 3.0])
        allocated = self.generator.allocate_histories(weights, 2)
        self.assertEqual(int(allocated.sum()), 2)
        # Heaviest two are indices 0 (5.0) and 2 (4.0).
        self.assertEqual(allocated.tolist(), [1, 0, 1, 0, 0])

    def test_sparse_total_is_preserved_exactly(self) -> None:
        weights = np.linspace(0.1, 9.0, 5000)
        for total in (1, 17, 999, 4999):
            allocated = self.generator.allocate_histories(weights, total)
            self.assertEqual(int(allocated.sum()), total)
            self.assertEqual(int((allocated > 0).sum()), total)
            self.assertEqual(int(allocated.max()), 1)

    def test_exactly_one_per_spot_is_not_sparse(self) -> None:
        weights = np.linspace(0.1, 9.0, 500)
        allocated = self.generator.allocate_histories(weights, 500)
        self.assertEqual(allocated.tolist(), [1] * 500)

    def test_normal_totals_are_unchanged(self) -> None:
        weights = np.linspace(0.1, 9.0, 500)
        allocated = self.generator.allocate_histories(weights, 5000)
        self.assertEqual(int(allocated.sum()), 5000)
        self.assertGreaterEqual(int(allocated.min()), 1)

    def test_non_positive_total_is_still_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            self.generator.allocate_histories(np.array([1.0, 2.0]), 0)

    def test_dropping_zero_spots_shrinks_the_topas_timeline(self) -> None:
        table = pd.DataFrame(
            {
                "BeamNumber": [1] * 6,
                "BeamName": ["B"] * 6,
                "LayerIndex": [1, 1, 1, 2, 2, 2],
                "ControlPointIndex": [0, 0, 0, 1, 1, 1],
                "Energy_MeVu": [250.0] * 6,
                "SpotIndex": list(range(6)),
                "X_mm": [0.0, 1.0, 2.0, 3.0, 4.0, 5.0],
                "Y_mm": [0.0] * 6,
                "MetersetWeight_MU": [9.0, 8.0, 0.1, 0.1, 0.1, 0.1],
                "RelativeWeight": [1.0] * 6,
                "PlanRelativeWeight": [1.0] * 6,
                "NumberOfPaintings": [1] * 6,
                "FWHM_X_mm": [8.0] * 6,
                "FWHM_Y_mm": [8.0] * 6,
            }
        )
        weights = table["MetersetWeight_MU"].to_numpy(dtype=float)
        allocated = self.generator.allocate_histories(weights, 2)
        keep = allocated > 0
        self.assertEqual(int(keep.sum()), 2)

        source = Path(tempfile.mkdtemp(prefix="plan1699-sparse-")) / "spots.csv"
        table.to_csv(source, index=False)
        full = self.generator.build_plan(
            table, np.ones(6, dtype=np.int64), source, 1.0, 0.0, 1.0, 0.0, "rtplan"
        )
        sparse = self.generator.build_plan(
            table.loc[keep].reset_index(drop=True),
            allocated[keep],
            source,
            1.0,
            0.0,
            1.0,
            0.0,
            "rtplan",
        )
        self.assertIn("i:Tf/NumberOfSequentialTimes = 6", full)
        self.assertIn("i:Tf/NumberOfSequentialTimes = 2", sparse)


class HistoryBudgetWarningTest(unittest.TestCase):
    """The GUI warns about an under-sampled run; it must not block it."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="plan1699-budget-test-")
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_allocation(self, spots: int) -> None:
        path = self.root / "plan_parsed" / "spot_history_allocation.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "AllocatedHistories\n" + "".join("1\n" for _ in range(spots)), encoding="utf-8"
        )

    def test_under_sampled_run_warns_and_names_the_numbers(self) -> None:
        self.write_allocation(43_919)
        note = history_budget_warning(self.root, 10_000, BASELINE_BEAM, None)
        self.assertIn("SPARSE TEST RUN", note)
        self.assertIn("10,000", note)
        self.assertIn("43,919", note)
        self.assertIn("not valid", note.lower())

    def test_sufficient_budget_is_silent(self) -> None:
        self.write_allocation(1_000)
        self.assertEqual(history_budget_warning(self.root, 1_000, BASELINE_BEAM, None), "")
        self.assertEqual(history_budget_warning(self.root, 100_000, BASELINE_BEAM, None), "")

    def test_unparsed_case_is_not_second_guessed(self) -> None:
        self.assertEqual(history_budget_warning(self.root, 1, BASELINE_BEAM, None), "")

    def test_manual_single_spot_needs_one_history(self) -> None:
        self.write_allocation(43_919)
        manual = {"beam_input_mode": "manual", "beam_model_mode": "baseline"}
        self.assertEqual(history_budget_warning(self.root, 1, manual, None), "")


class FailureReasonTest(unittest.TestCase):
    """The queue table showed only "<script> exited with status 1"."""

    def test_python_traceback_tail_is_extracted(self) -> None:
        captured = [
            "Wrote something\n",
            'Traceback (most recent call last):\n  File "x.py", line 1\n',
            "RuntimeError: geometry check failed for beam 2\n",
        ]
        reason = _failure_reason(captured)
        self.assertIn("RuntimeError:", reason)
        self.assertIn("beam 2", reason)

    def test_block_lines_are_extracted(self) -> None:
        self.assertIn("BLOCK", _failure_reason(["fine\nBLOCK: HU calibration missing\ntail\n"]))

    def test_last_line_is_the_fallback(self) -> None:
        self.assertEqual(_failure_reason(["one\ntwo\nthree\n"]), " — three")

    def test_empty_output_yields_nothing(self) -> None:
        self.assertEqual(_failure_reason([]), "")
        self.assertEqual(_failure_reason(["   \n\n"]), "")

    def test_reason_is_truncated(self) -> None:
        reason = _failure_reason(["RuntimeError: " + "x" * 900], limit=100)
        self.assertLessEqual(len(reason), 104)


if __name__ == "__main__":
    sys.exit(unittest.main())
