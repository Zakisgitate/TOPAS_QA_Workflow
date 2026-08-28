"""Water-phantom single-spot validation: analysis library and deck helpers.

These tests pin the physics-facing behaviour of the water-phantom feature that
the exported CSV files, range metrics and TPS comparison all depend on.  Every
case is analytic or built from a hand-written fixture, so none of them needs a
TOPAS installation or a DICOM plan.
"""

from __future__ import annotations

import importlib.util
import math
import subprocess
import sys
import tempfile
import unittest
import warnings
from pathlib import Path

import numpy as np

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from scripts.utils.water_phantom import (  # noqa: E402
    MAX_PROJECTION_ERROR_MM,
    SIGMA_TO_FWHM,
    bin_centers,
    compare_depth_curves,
    crossing_position,
    depth_curve_metrics,
    find_measured_curve,
    gamma_1d,
    normalize_to_max,
    parse_measured_idd,
    parse_topas_binheader,
    peak_position,
    profile_metrics,
    project_spot_axis,
    read_topas_1d,
)


def _load_generator():
    """Import ``scripts/16_generate_water_phantom_spot.py`` despite its digit prefix."""

    path = APP_ROOT / "scripts" / "16_generate_water_phantom_spot.py"
    spec = importlib.util.spec_from_file_location("water_phantom_generator", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import the water-phantom generator from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GENERATOR = _load_generator()


def _triangular_peak(
    depth: np.ndarray,
    peak_mm: float,
    rise_mm: float,
    fall_mm: float,
    entrance: float = 0.2,
) -> np.ndarray:
    """A peaked curve whose crossings are known in closed form.

    Linear on both sides of the apex, so R80/R50/R20 and the 80-20 falloff can
    be predicted exactly and compared against the interpolating implementation.
    """

    values = np.empty_like(depth)
    rising = depth <= peak_mm
    values[rising] = entrance + (1.0 - entrance) * (
        (depth[rising] - (peak_mm - rise_mm)) / rise_mm
    )
    values[~rising] = 1.0 - (depth[~rising] - peak_mm) / fall_mm
    return np.clip(values, 0.0, None)


class BinGeometryTest(unittest.TestCase):
    """Bin centres and bin counts describe the same axis the deck scores."""

    def test_bin_centers_are_half_a_bin_inside_the_first_edge(self) -> None:
        centres = bin_centers(4, 0.5, 10.0)
        np.testing.assert_allclose(centres, [10.25, 10.75, 11.25, 11.75])

    def test_bin_centers_reject_an_empty_axis(self) -> None:
        with self.assertRaises(RuntimeError):
            bin_centers(0, 0.5, 0.0)

    def test_bin_count_returns_a_step_that_divides_the_extent_exactly(self) -> None:
        count, step = GENERATOR.bin_count(400.0, 0.5, "depth")
        self.assertEqual(count, 800)
        self.assertAlmostEqual(step, 0.5)
        self.assertAlmostEqual(count * step, 400.0)

    def test_bin_count_snaps_a_non_dividing_step_onto_whole_bins(self) -> None:
        count, step = GENERATOR.bin_count(100.0, 0.3, "depth")
        self.assertEqual(count, 333)
        self.assertAlmostEqual(count * step, 100.0)
        self.assertLess(abs(step - 0.3), 0.001)

    def test_bin_count_rejects_a_step_larger_than_the_extent(self) -> None:
        with self.assertRaises(RuntimeError):
            GENERATOR.bin_count(1.0, 5.0, "depth")

    def test_bin_count_rejects_a_non_positive_step(self) -> None:
        with self.assertRaises(RuntimeError):
            GENERATOR.bin_count(100.0, 0.0, "depth")


class DepthCurveMetricsTest(unittest.TestCase):
    """Range metrics recover the known geometry of an analytic peak."""

    def setUp(self) -> None:
        self.depth = np.arange(0.0, 200.0, 0.05)
        self.dose = _triangular_peak(self.depth, peak_mm=150.0, rise_mm=150.0, fall_mm=10.0)

    def test_r100_finds_the_apex(self) -> None:
        metrics = depth_curve_metrics(self.depth, self.dose)
        self.assertAlmostEqual(metrics["R100_mm"], 150.0, delta=0.05)

    def test_distal_crossings_match_the_closed_form(self) -> None:
        metrics = depth_curve_metrics(self.depth, self.dose)
        self.assertAlmostEqual(metrics["R90_mm"], 151.0, delta=0.01)
        self.assertAlmostEqual(metrics["R80_mm"], 152.0, delta=0.01)
        self.assertAlmostEqual(metrics["R50_mm"], 155.0, delta=0.01)
        self.assertAlmostEqual(metrics["R20_mm"], 158.0, delta=0.01)
        self.assertAlmostEqual(metrics["distal_falloff_80_20_mm"], 6.0, delta=0.02)

    def test_proximal_r80_is_on_the_plateau_side(self) -> None:
        metrics = depth_curve_metrics(self.depth, self.dose)
        self.assertIsNotNone(metrics["proximal_R80_mm"])
        self.assertLess(metrics["proximal_R80_mm"], metrics["R100_mm"])

    def test_missing_distal_crossing_is_reported_as_none(self) -> None:
        depth = np.arange(0.0, 50.0, 0.5)
        dose = np.linspace(0.5, 1.0, depth.size)  # never falls back down
        metrics = depth_curve_metrics(depth, dose)
        self.assertIsNone(metrics["R80_mm"])
        self.assertIsNone(metrics["distal_falloff_80_20_mm"])

    def test_peak_to_entrance_ratio_uses_the_first_sample(self) -> None:
        metrics = depth_curve_metrics(self.depth, self.dose)
        self.assertAlmostEqual(
            metrics["peak_to_entrance_ratio"],
            float(self.dose.max() / self.dose[0]),
            places=9,
        )

    def test_too_few_samples_are_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            depth_curve_metrics([0.0, 1.0], [1.0, 0.5])

    def test_peak_position_interpolates_below_the_bin_width(self) -> None:
        x = np.array([0.0, 1.0, 2.0])
        y = np.array([0.9, 1.0, 0.95])
        vertex = peak_position(x, y)
        self.assertGreater(vertex, 1.0)
        self.assertLess(vertex, 2.0)

    def test_normalize_to_max_rejects_a_curve_with_no_positive_dose(self) -> None:
        with self.assertRaises(RuntimeError):
            normalize_to_max([0.0, 0.0, 0.0])


class CrossingPositionTest(unittest.TestCase):
    """The interpolated crossing search is side-aware and never guesses."""

    def setUp(self) -> None:
        self.x = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
        self.y = np.array([0.0, 0.5, 1.0, 0.5, 0.0])

    def test_distal_crossing_is_after_the_peak(self) -> None:
        self.assertAlmostEqual(crossing_position(self.x, self.y, 0.5, "distal"), 3.0)

    def test_proximal_crossing_is_before_the_peak(self) -> None:
        self.assertAlmostEqual(crossing_position(self.x, self.y, 0.5, "proximal"), 1.0)

    def test_uncrossed_level_returns_none(self) -> None:
        self.assertIsNone(crossing_position(self.x, self.y, 1.5, "distal"))

    def test_unknown_side_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            crossing_position(self.x, self.y, 0.5, "sideways")

    def test_mismatched_arrays_are_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            crossing_position([0.0, 1.0, 2.0], [0.0, 1.0], 0.5)


class ProfileMetricsTest(unittest.TestCase):
    """Lateral widths recover an exact Gaussian, and the two sigmas stay separate."""

    def setUp(self) -> None:
        # An exact Gaussian leaves zero fit residual, so SciPy cannot estimate a
        # covariance and warns.  That is the expected outcome of this fixture.
        warnings.filterwarnings("ignore", message="Covariance of the parameters")
        self.sigma = 4.0
        self.position = np.arange(-60.0, 60.0 + 0.05, 0.05)
        self.dose = np.exp(-0.5 * (self.position / self.sigma) ** 2)

    def test_moment_sigma_recovers_the_input_sigma(self) -> None:
        metrics = profile_metrics(self.position, self.dose)
        self.assertAlmostEqual(metrics["sigma_moment_mm"], self.sigma, delta=0.02)

    def test_fwhm_matches_the_analytic_value(self) -> None:
        metrics = profile_metrics(self.position, self.dose)
        self.assertAlmostEqual(metrics["fwhm_mm"], self.sigma * SIGMA_TO_FWHM, delta=0.01)

    def test_gaussian_fit_is_reported_separately_from_the_moment(self) -> None:
        metrics = profile_metrics(self.position, self.dose)
        self.assertIn("gaussian_sigma_mm", metrics)
        self.assertIn("sigma_moment_mm", metrics)
        if metrics["gaussian_fit_status"] == "converged":
            self.assertAlmostEqual(metrics["gaussian_sigma_mm"], self.sigma, delta=0.02)
            self.assertAlmostEqual(metrics["gaussian_centre_mm"], 0.0, delta=0.02)

    def test_penumbra_is_half_the_width_difference(self) -> None:
        metrics = profile_metrics(self.position, self.dose)
        self.assertAlmostEqual(
            metrics["penumbra_80_20_mm"],
            (metrics["width_20_mm"] - metrics["width_80_mm"]) / 2.0,
            places=9,
        )
        self.assertGreater(metrics["penumbra_80_20_mm"], 0.0)

    def test_a_failed_gaussian_fit_does_not_fail_the_analysis(self) -> None:
        position = np.arange(-10.0, 10.0, 0.5)
        dose = np.where(np.abs(position) <= 5.0, 1.0, 0.01)  # flat top, not Gaussian
        metrics = profile_metrics(position, dose)
        self.assertIsNotNone(metrics["fwhm_mm"])
        self.assertIsInstance(metrics["gaussian_fit_status"], str)

    def test_a_profile_with_no_positive_dose_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            profile_metrics([-1.0, 0.0, 1.0], [0.0, 0.0, 0.0])


class Gamma1dTest(unittest.TestCase):
    """One-dimensional global gamma passes identical curves and fails shifted ones."""

    def setUp(self) -> None:
        self.depth = np.arange(0.0, 200.0, 0.5)
        self.dose = _triangular_peak(self.depth, peak_mm=150.0, rise_mm=150.0, fall_mm=10.0)

    def test_identical_curves_pass_completely_with_zero_gamma(self) -> None:
        result = gamma_1d(self.depth, self.dose, self.depth, self.dose)
        self.assertAlmostEqual(result["pass_rate_percent"], 100.0)
        self.assertLess(result["max_gamma"], 1e-6)

    def test_a_large_range_shift_fails(self) -> None:
        result = gamma_1d(self.depth, self.dose, self.depth + 12.0, self.dose)
        self.assertLess(result["pass_rate_percent"], 100.0)
        self.assertGreater(result["max_gamma"], 1.0)

    def test_a_shift_well_inside_the_distance_criterion_passes(self) -> None:
        result = gamma_1d(self.depth, self.dose, self.depth + 0.5, self.dose)
        self.assertAlmostEqual(result["pass_rate_percent"], 100.0)

    def test_the_dose_threshold_excludes_low_dose_reference_points(self) -> None:
        loose = gamma_1d(self.depth, self.dose, self.depth, self.dose, threshold_fraction=0.1)
        tight = gamma_1d(self.depth, self.dose, self.depth, self.dose, threshold_fraction=0.9)
        self.assertLess(tight["evaluated_points"], loose["evaluated_points"])

    def test_non_overlapping_curves_are_rejected_rather_than_extrapolated(self) -> None:
        with self.assertRaises(RuntimeError):
            gamma_1d(self.depth, self.dose, self.depth + 500.0, self.dose)

    def test_non_positive_criteria_are_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            gamma_1d(self.depth, self.dose, self.depth, self.dose, dose_percent=0.0)

    def test_the_reported_criteria_string_records_what_was_evaluated(self) -> None:
        result = gamma_1d(
            self.depth, self.dose, self.depth, self.dose, dose_percent=2.0, distance_mm=1.0
        )
        self.assertIn("2%/1 mm", result["criteria"])
        self.assertEqual(result["dose_percent"], 2.0)
        self.assertEqual(result["distance_mm"], 1.0)


class CompareDepthCurvesTest(unittest.TestCase):
    """The TPS/measured comparison reports the shift it was given."""

    def setUp(self) -> None:
        self.depth = np.arange(0.0, 200.0, 0.5)
        self.dose = _triangular_peak(self.depth, peak_mm=150.0, rise_mm=150.0, fall_mm=10.0)

    def test_a_known_range_shift_is_reported_on_every_level(self) -> None:
        result = compare_depth_curves(self.depth, self.dose, self.depth + 2.0, self.dose)
        for key in ("R100_mm", "R90_mm", "R80_mm", "R50_mm", "R20_mm"):
            self.assertAlmostEqual(result["range_difference_mm"][key], 2.0, delta=0.05)

    def test_the_falloff_width_is_unchanged_by_a_pure_shift(self) -> None:
        result = compare_depth_curves(self.depth, self.dose, self.depth + 2.0, self.dose)
        self.assertAlmostEqual(
            result["range_difference_mm"]["distal_falloff_80_20_mm"], 0.0, delta=0.05
        )

    def test_identical_curves_report_no_difference(self) -> None:
        result = compare_depth_curves(self.depth, self.dose, self.depth, self.dose)
        self.assertLess(result["max_abs_difference_percent"], 1e-6)
        self.assertAlmostEqual(result["gamma"]["pass_rate_percent"], 100.0)

    def test_the_overlap_is_the_intersection_of_the_two_depth_ranges(self) -> None:
        result = compare_depth_curves(
            self.depth, self.dose, self.depth[20:] + 0.0, self.dose[20:]
        )
        low, high = result["overlap_mm"]
        self.assertAlmostEqual(low, float(self.depth[20]))
        self.assertAlmostEqual(high, float(self.depth[-1]))
        self.assertGreater(result["compared_points"], 0)

    def test_the_curve_block_is_returned_for_export(self) -> None:
        result = compare_depth_curves(self.depth, self.dose, self.depth, self.dose)
        curve = result["curve"]
        self.assertEqual(curve["depth_mm"].size, result["compared_points"])
        self.assertEqual(curve["reference_relative"].size, result["compared_points"])
        self.assertEqual(curve["evaluated_relative"].size, result["compared_points"])

    def test_disjoint_curves_name_both_ranges_when_they_are_rejected(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            compare_depth_curves(
                self.depth, self.dose, self.depth + 1000.0, self.dose, reference_label="measured"
            )
        self.assertIn("measured", str(caught.exception))


class TopasBinaryReadingTest(unittest.TestCase):
    """A curve is only read back when the sidecar header says it is one-dimensional."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, values: np.ndarray, header: str) -> Path:
        binary = self.root / name
        values.astype(np.float64).tofile(binary)
        Path(str(binary) + "header").write_text(header, encoding="utf-8")
        return binary

    def test_a_one_dimensional_scorer_round_trips(self) -> None:
        values = np.linspace(1.0, 8.0, 8)
        binary = self._write(
            "idd.bin",
            values,
            "# X in 1 bins of 20 mm\n# Y in 1 bins of 20 mm\n# Z in 8 bins of 0.5 mm\n",
        )
        np.testing.assert_allclose(read_topas_1d(binary), values)
        np.testing.assert_allclose(read_topas_1d(binary, expected_bins=8), values)

    def test_a_transposed_two_dimensional_array_is_rejected(self) -> None:
        binary = self._write(
            "grid.bin",
            np.arange(12.0),
            "# X in 3 bins of 1 mm\n# Y in 1 bins of 20 mm\n# Z in 4 bins of 0.5 mm\n",
        )
        with self.assertRaises(RuntimeError) as caught:
            read_topas_1d(binary)
        self.assertIn("not one-dimensional", str(caught.exception))

    def test_a_header_bin_count_mismatch_is_rejected(self) -> None:
        binary = self._write(
            "short.bin",
            np.arange(5.0),
            "# X in 1 bins of 20 mm\n# Y in 1 bins of 20 mm\n# Z in 8 bins of 0.5 mm\n",
        )
        with self.assertRaises(RuntimeError):
            read_topas_1d(binary)

    def test_an_expected_bin_mismatch_is_rejected(self) -> None:
        binary = self._write(
            "idd.bin",
            np.arange(8.0),
            "# X in 1 bins of 20 mm\n# Y in 1 bins of 20 mm\n# Z in 8 bins of 0.5 mm\n",
        )
        with self.assertRaises(RuntimeError):
            read_topas_1d(binary, expected_bins=9)

    def test_an_empty_scorer_output_is_rejected(self) -> None:
        binary = self.root / "empty.bin"
        binary.write_bytes(b"")
        with self.assertRaises(RuntimeError) as caught:
            read_topas_1d(binary)
        self.assertIn("empty", str(caught.exception))

    def test_non_finite_values_are_rejected(self) -> None:
        binary = self.root / "nan.bin"
        np.array([1.0, np.nan, 3.0], dtype=np.float64).tofile(binary)
        with self.assertRaises(RuntimeError) as caught:
            read_topas_1d(binary)
        self.assertIn("non-finite", str(caught.exception))

    def test_the_header_parser_returns_axis_widths_and_units(self) -> None:
        header = self.root / "idd.binheader"
        header.write_text(
            "# TOPAS Binary Results\n"
            "# X in 1 bins of 20 mm\n"
            "# Y in 1 bins of 20 mm\n"
            "# Z in 800 bins of 0.5 mm\n",
            encoding="utf-8",
        )
        parsed = parse_topas_binheader(header)
        self.assertEqual(parsed["axes"]["Z"]["bins"], 800)
        self.assertAlmostEqual(parsed["axes"]["Z"]["width"], 0.5)
        self.assertEqual(parsed["axes"]["Z"]["unit"], "mm")

    def test_a_header_without_any_binned_axis_is_rejected(self) -> None:
        header = self.root / "bad.binheader"
        header.write_text("# TOPAS Binary Results\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            parse_topas_binheader(header)


class SpotAxisProjectionTest(unittest.TestCase):
    """The source-plane projection is gated by its own geometric back-check."""

    SOURCE_PLANE_MM = 680.0
    VSAD_X_MM = 5398.68
    VSAD_Y_MM = 6198.24

    def _project(self, x_mm: float, y_mm: float) -> dict:
        return project_spot_axis(
            x_mm, y_mm, self.SOURCE_PLANE_MM, self.VSAD_X_MM, self.VSAD_Y_MM
        )

    def test_a_central_spot_needs_no_rotation(self) -> None:
        result = self._project(0.0, 0.0)
        self.assertAlmostEqual(result["source_local_x_mm"], 0.0)
        self.assertAlmostEqual(result["source_local_y_mm"], 0.0)
        self.assertAlmostEqual(result["rotation_x_deg"], 0.0)
        self.assertAlmostEqual(result["rotation_y_deg"], 0.0)

    def test_every_projection_satisfies_the_back_check_tolerance(self) -> None:
        for x_mm, y_mm in ((0.0, 0.0), (50.0, 0.0), (0.0, -50.0), (-80.0, 120.0)):
            with self.subTest(spot=(x_mm, y_mm)):
                result = self._project(x_mm, y_mm)
                self.assertLessEqual(result["projection_error_mm"], MAX_PROJECTION_ERROR_MM)

    def test_each_axis_uses_its_own_vsad(self) -> None:
        result = self._project(50.0, 50.0)
        expected_x = 50.0 * (self.VSAD_X_MM - self.SOURCE_PLANE_MM) / self.VSAD_X_MM
        expected_y = 50.0 * (self.VSAD_Y_MM - self.SOURCE_PLANE_MM) / self.VSAD_Y_MM
        self.assertAlmostEqual(result["source_local_x_mm"], expected_x, places=9)
        self.assertAlmostEqual(result["source_local_y_mm"], expected_y, places=9)
        self.assertNotAlmostEqual(result["source_local_x_mm"], result["source_local_y_mm"])

    def test_rotation_signs_follow_the_full_plan_generator_convention(self) -> None:
        positive_x = self._project(50.0, 0.0)
        positive_y = self._project(0.0, 50.0)
        self.assertLess(positive_x["rotation_y_deg"], 0.0)
        self.assertAlmostEqual(positive_x["rotation_x_deg"], 0.0)
        self.assertGreater(positive_y["rotation_x_deg"], 0.0)
        self.assertAlmostEqual(positive_y["rotation_y_deg"], 0.0)

    def test_a_non_positive_vsad_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            project_spot_axis(0.0, 0.0, self.SOURCE_PLANE_MM, 0.0, self.VSAD_Y_MM)

    def test_a_non_finite_source_plane_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            project_spot_axis(0.0, 0.0, float("nan"), self.VSAD_X_MM, self.VSAD_Y_MM)


class MeasuredIddParsingTest(unittest.TestCase):
    """Measured commissioning blocks keep their per-nucleon energy and detector."""

    BLOCK = (
        "Origin of data [Measured/Computed];Measured\n"
        "Machine/Treatment room;hzRoom1\n"
        "Medium;Water\n"
        "Curve type [Depth/X/Y/AbsoluteDosimetry];Depth\n"
        "Single spot [Yes/No];Yes\n"
        "Nominal beam energy [MeV];2887.56\n"
        "Detector lateral shape [Quadratic/Circular];Circular\n"
        "Detector lateral side/diameter [mm];81.6\n"
        "Isocenter to phantom surface distance [mm];150\n"
        "Isocenter to snout distance [mm];420\n"
        "Range modulator name;RF4\n"
        "  1.0;0.30\n"
        "  2.0;0.32\n"
        "  3.0;0.35\n"
        "End:\n"
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "measured_idd.csv"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_the_nominal_energy_is_converted_to_mev_per_nucleon(self) -> None:
        self.path.write_text(self.BLOCK, encoding="utf-8")
        curve = parse_measured_idd(self.path)[0]
        self.assertAlmostEqual(curve.nominal_total_mev, 2887.56)
        self.assertAlmostEqual(curve.nominal_mevu, 2887.56 / 12.0)

    def test_a_circular_detector_reports_half_its_diameter_as_the_radius(self) -> None:
        self.path.write_text(self.BLOCK, encoding="utf-8")
        curve = parse_measured_idd(self.path)[0]
        self.assertAlmostEqual(curve.detector_radius_mm, 40.8)

    def test_a_quadratic_detector_reports_the_equal_area_radius(self) -> None:
        self.path.write_text(
            self.BLOCK.replace("Circular", "Quadratic"), encoding="utf-8"
        )
        curve = parse_measured_idd(self.path)[0]
        self.assertAlmostEqual(curve.detector_radius_mm, 81.6 / math.sqrt(math.pi))

    def test_samples_are_sorted_by_depth(self) -> None:
        self.path.write_text(self.BLOCK, encoding="utf-8")
        curve = parse_measured_idd(self.path)[0]
        np.testing.assert_allclose(curve.depth_mm, [1.0, 2.0, 3.0])
        np.testing.assert_allclose(curve.dose_au, [0.30, 0.32, 0.35])
        self.assertTrue(np.all(np.diff(curve.depth_mm) > 0))

    def test_two_blocks_are_parsed_independently(self) -> None:
        second = self.BLOCK.replace("2887.56", "1200.00")
        self.path.write_text(self.BLOCK + second, encoding="utf-8")
        curves = parse_measured_idd(self.path)
        self.assertEqual(len(curves), 2)
        self.assertAlmostEqual(curves[1].nominal_total_mev, 1200.00)

    def test_a_file_without_any_curve_is_rejected(self) -> None:
        self.path.write_text("Machine/Treatment room;hzRoom1\n", encoding="utf-8")
        with self.assertRaises(RuntimeError):
            parse_measured_idd(self.path)

    def test_an_unmeasured_energy_returns_none_instead_of_interpolating(self) -> None:
        self.path.write_text(self.BLOCK, encoding="utf-8")
        curves = parse_measured_idd(self.path)
        self.assertIsNotNone(find_measured_curve(curves, 2887.56 / 12.0))
        self.assertIsNone(find_measured_curve(curves, 100.0))


class GeneratorHelperTest(unittest.TestCase):
    """Deck-text helpers stay stable: the emitted decks are compared by hash."""

    def test_profile_depths_are_deduplicated_and_sorted(self) -> None:
        self.assertEqual(
            GENERATOR.parse_depth_list("30, 10; 20, 10"), [10.0, 20.0, 30.0]
        )

    def test_an_omitted_profile_depth_list_stays_none(self) -> None:
        self.assertIsNone(GENERATOR.parse_depth_list(None))

    def test_a_negative_profile_depth_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            GENERATOR.parse_depth_list("10,-5")

    def test_a_non_numeric_profile_depth_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            GENERATOR.parse_depth_list("10,abc")

    def test_an_empty_profile_depth_list_is_rejected(self) -> None:
        with self.assertRaises(RuntimeError):
            GENERATOR.parse_depth_list(" , ; ")

    def test_slug_keeps_only_path_safe_characters(self) -> None:
        self.assertEqual(GENERATOR.slug("hzRoom1 90/RF4 250701"), "hzRoom1_90_RF4_250701")
        self.assertEqual(GENERATOR.slug("", fallback="unknown"), "unknown")
        self.assertEqual(GENERATOR.slug(None, fallback="case"), "case")

    def test_float_formatting_is_deterministic_and_lossless_enough(self) -> None:
        self.assertEqual(GENERATOR.fmt_float(240.63), "240.63")
        self.assertEqual(GENERATOR.fmt_float(0.5), "0.5")
        self.assertEqual(GENERATOR.fmt_float(1.0 / 3.0), "0.3333333333")

    def test_vector_lines_declare_their_own_length(self) -> None:
        self.assertEqual(
            GENERATOR.vector_line("dv:So/Spot/Energies", [100.0, 200.0], "MeV"),
            "dv:So/Spot/Energies = 2 100.0 200.0 MeV",
        )
        self.assertEqual(
            GENERATOR.vector_line("uv:So/Spot/Weights", [0.4, 0.6]),
            "uv:So/Spot/Weights = 2 0.4 0.6",
        )


class AnalysisOnlyGuardTest(unittest.TestCase):
    """``--analysis-only`` re-exports existing output and never silently invents it."""

    RUNNER = APP_ROOT / "scripts" / "17_run_water_phantom_spot.py"

    def _run(self, *extra: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(self.RUNNER), "--energy-mevu", "240.63", "--analysis-only", *extra],
            cwd=APP_ROOT,
            text=True,
            capture_output=True,
        )

    def test_the_flag_is_offered_by_the_runner(self) -> None:
        helped = subprocess.run(
            [sys.executable, str(self.RUNNER), "--help"],
            cwd=APP_ROOT,
            text=True,
            capture_output=True,
        )
        self.assertEqual(helped.returncode, 0)
        self.assertIn("--analysis-only", helped.stdout)

    def test_combining_it_with_overwrite_is_rejected(self) -> None:
        result = self._run("--overwrite", "--output-tag", "no_such_run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--overwrite", result.stderr)

    def test_an_unknown_tag_is_rejected_rather_than_regenerated(self) -> None:
        result = self._run("--output-tag", "definitely_not_a_generated_tag")
        self.assertEqual(result.returncode, 1)
        self.assertIn("definitely_not_a_generated_tag", result.stderr)
        self.assertNotIn("Water-phantom input generation", result.stderr)

    def test_a_staged_but_unsimulated_run_is_rejected_as_empty(self) -> None:
        staged = sorted(
            (APP_ROOT / "analysis" / "_water_phantom").glob(
                "*/*/setup/water_phantom_spot_setup.json"
            )
        )
        if not staged:
            self.skipTest("no generated water-phantom run is available in this checkout")
        import json

        for setup_path in staged:
            setup = json.loads(setup_path.read_text(encoding="utf-8"))
            dose_dir = Path(setup["run"]["dose_directory"])
            idd = dose_dir / "idd.bin"
            if not idd.is_file() or idd.stat().st_size == 0:
                tag = setup_path.parents[1].name
                result = self._run("--output-tag", tag)
                self.assertEqual(result.returncode, 1)
                self.assertIn("no usable transport output", result.stderr)
                return
        self.skipTest("every generated run in this checkout already holds transport output")


if __name__ == "__main__":
    unittest.main()
