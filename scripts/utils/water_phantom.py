#!/usr/bin/env python3
"""Helpers for the water-phantom single-energy single-spot TOPAS validation.

This validation is deliberately independent of DICOM.  It needs no RTPLAN, no
CT, no RTSTRUCT and no RTDOSE: a uniform water box, one commissioned energy and
one spot are enough.  The scorers it analyses are all one-dimensional, so the
TOPAS binary bin order is unambiguous and no ``[Z, Y, X]`` convention has to be
assumed anywhere in this module.

Reference data live in the machine model that the commissioned source already
depends on:

* ``measured_pristine_bragg_peaks.csv`` - measured integral depth dose for a
  subset of the commissioned energies, together with the exact measurement
  geometry (circular detector diameter and isocenter-to-surface distance).
* ``measured_spot_sigma.csv`` - measured spot sigma **in air** at five planes
  relative to isocenter.  These are not in-water lateral profiles and are only
  reported for context.

Nothing in this module fits TOPAS output to a reference curve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import re
from typing import Any, Iterable, Optional, Sequence

import numpy as np


SIGMA_TO_FWHM = 2.0 * math.sqrt(2.0 * math.log(2.0))
FWHM_TO_SIGMA = 1.0 / SIGMA_TO_FWHM
CARBON_MASS_NUMBER = 12

# The commissioned spot-axis back-check in scripts/04_generate_topas_plan.py uses
# this limit; the water-phantom generator must not be more permissive.
MAX_PROJECTION_ERROR_MM = 0.01

_HEADER_BIN_RE = re.compile(
    r"^#\s*([A-Za-z]+)\s+in\s+(\d+)\s+bins\s+of\s+([0-9.eE+-]+)\s*(\S+)?\s*$"
)
_DATA_ROW_RE = re.compile(r"^\s*([-+0-9.eE]+)\s*;\s*([-+0-9.eE]+)\s*$")


# ---------------------------------------------------------------------------
# Measured commissioning evidence
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasuredDepthCurve:
    """One measured integral-depth-dose block with its measurement geometry."""

    nominal_mevu: float
    nominal_total_mev: float
    depth_mm: np.ndarray
    dose_au: np.ndarray
    machine: str
    medium: str
    curve_type: str
    origin: str
    single_spot: str
    detector_shape: str
    detector_size_mm: Optional[float]
    surface_distance_mm: Optional[float]
    snout_distance_mm: Optional[float]
    range_modulator: str
    headers: dict = field(default_factory=dict, repr=False)

    @property
    def detector_radius_mm(self) -> Optional[float]:
        """Scoring radius that reproduces this detector, or None if unknown."""
        if self.detector_size_mm is None or not math.isfinite(self.detector_size_mm):
            return None
        if self.detector_shape.strip().lower().startswith("circ"):
            return float(self.detector_size_mm) / 2.0
        # A quadratic detector is recorded by its side; an equal-area circle is
        # the closest single-radius cylinder and is reported as such.
        return float(self.detector_size_mm) / math.sqrt(math.pi)


def _as_float(value: str) -> Optional[float]:
    try:
        result = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def parse_measured_idd(path: Path) -> list[MeasuredDepthCurve]:
    """Parse the multi-block measured pristine-Bragg-peak commissioning file."""

    text = Path(path).read_text(encoding="utf-8", errors="strict")
    curves: list[MeasuredDepthCurve] = []
    headers: dict[str, str] = {}
    depths: list[float] = []
    doses: list[float] = []

    def flush() -> None:
        if not depths:
            return
        total_mev = _as_float(headers.get("Nominal beam energy [MeV]", ""))
        if total_mev is None or total_mev <= 0:
            raise RuntimeError(
                f"Measured depth-dose block without a usable nominal energy in {path}"
            )
        depth = np.asarray(depths, dtype=float)
        dose = np.asarray(doses, dtype=float)
        order = np.argsort(depth, kind="stable")
        depth, dose = depth[order], dose[order]
        if np.any(np.diff(depth) <= 0):
            raise RuntimeError(
                f"Measured depth-dose block at {total_mev:g} MeV has repeated depths in {path}"
            )
        curves.append(
            MeasuredDepthCurve(
                nominal_mevu=total_mev / CARBON_MASS_NUMBER,
                nominal_total_mev=total_mev,
                depth_mm=depth,
                dose_au=dose,
                machine=headers.get("Machine/Treatment room", "").strip(),
                medium=headers.get("Medium", "").strip(),
                curve_type=headers.get("Curve type [Depth/X/Y/AbsoluteDosimetry]", "").strip(),
                origin=headers.get("Origin of data [Measured/Computed]", "").strip(),
                single_spot=headers.get("Single spot [Yes/No]", "").strip(),
                detector_shape=headers.get("Detector lateral shape [Quadratic/Circular]", "").strip(),
                detector_size_mm=_as_float(headers.get("Detector lateral side/diameter [mm]", "")),
                surface_distance_mm=_as_float(
                    headers.get("Isocenter to phantom surface distance [mm]", "")
                ),
                snout_distance_mm=_as_float(headers.get("Isocenter to snout distance [mm]", "")),
                range_modulator=headers.get("Range modulator name", "").strip(),
                headers=dict(headers),
            )
        )
        depths.clear()
        doses.clear()

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("Origin of data"):
            flush()
            headers = {}
        match = _DATA_ROW_RE.match(line)
        if match:
            depth_value = _as_float(match.group(1))
            dose_value = _as_float(match.group(2))
            if depth_value is None or dose_value is None:
                raise RuntimeError(f"Non-finite measured depth-dose sample in {path}: {line!r}")
            depths.append(depth_value)
            doses.append(dose_value)
            continue
        if line.startswith("End:"):
            continue
        if ";" in line:
            key, _, value = line.partition(";")
            headers[key.rstrip(":").strip()] = value.strip()
    flush()

    if not curves:
        raise RuntimeError(f"No measured depth-dose curve found in {path}")
    return curves


def find_measured_curve(
    curves: Sequence[MeasuredDepthCurve],
    nominal_mevu: float,
    tolerance_mevu: float = 0.02,
) -> Optional[MeasuredDepthCurve]:
    """Return the measured curve for this energy, or None when none was measured.

    A missing reference is a normal condition: only a subset of the commissioned
    energies has a measured integral depth dose.  No interpolation between
    measured energies is performed.
    """

    best: Optional[MeasuredDepthCurve] = None
    best_delta = float("inf")
    for curve in curves:
        delta = abs(curve.nominal_mevu - float(nominal_mevu))
        if delta < best_delta:
            best, best_delta = curve, delta
    if best is None or best_delta > tolerance_mevu:
        return None
    return best


def measured_idd_energies(curves: Sequence[MeasuredDepthCurve]) -> list[float]:
    return sorted(curve.nominal_mevu for curve in curves)


def parse_measured_spot_sigma(path: Path) -> dict[float, dict[float, float]]:
    """Parse measured in-air spot sigma as ``{energy_mevu: {plane_mm: sigma_mm}}``."""

    result: dict[float, dict[float, float]] = {}
    lines = Path(path).read_text(encoding="utf-8", errors="strict").splitlines()
    if not lines:
        raise RuntimeError(f"Measured spot-sigma file is empty: {path}")
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        parts = re.split(r"[\t,;]", line)
        if len(parts) < 4:
            continue
        energy = _as_float(parts[0])
        plane = _as_float(parts[1])
        sigma = _as_float(parts[3])
        if energy is None or plane is None or sigma is None:
            continue
        result.setdefault(energy, {})[plane] = sigma
    if not result:
        raise RuntimeError(f"No measured spot-sigma rows found in {path}")
    return result


def nearest_spot_sigma(
    table: dict[float, dict[float, float]],
    nominal_mevu: float,
    tolerance_mevu: float = 0.02,
) -> Optional[dict[float, float]]:
    if not table:
        return None
    energies = np.asarray(sorted(table), dtype=float)
    index = int(np.argmin(np.abs(energies - float(nominal_mevu))))
    matched = float(energies[index])
    if abs(matched - float(nominal_mevu)) > tolerance_mevu:
        return None
    return table[matched]


# ---------------------------------------------------------------------------
# Spot-axis projection
# ---------------------------------------------------------------------------


def project_spot_axis(
    target_local_x_mm: float,
    target_local_y_mm: float,
    source_plane_mm: float,
    vsad_for_local_x_mm: float,
    vsad_for_local_y_mm: float,
) -> dict[str, float]:
    """Project one spot back to the commissioned source plane and verify the ray.

    ``target_local_*`` are the requested spot coordinates at isocenter expressed
    in the source component's local frame.  Each local axis is projected with the
    virtual source-axis distance that belongs to that axis.  The returned
    rotations follow the TOPAS ``RotX`` then ``RotY`` convention used by
    ``scripts/04_generate_topas_plan.py``; the geometric back-check below is the
    same one that gates the full-plan generator.
    """

    for label, value in (
        ("source plane", source_plane_mm),
        ("local-X VSAD", vsad_for_local_x_mm),
        ("local-Y VSAD", vsad_for_local_y_mm),
    ):
        if not math.isfinite(float(value)) or float(value) <= 0:
            raise RuntimeError(f"Spot-axis projection needs a positive {label}, got {value!r}")

    target_x = float(target_local_x_mm)
    target_y = float(target_local_y_mm)
    distance = float(source_plane_mm)
    source_x = target_x * (float(vsad_for_local_x_mm) - distance) / float(vsad_for_local_x_mm)
    source_y = target_y * (float(vsad_for_local_y_mm) - distance) / float(vsad_for_local_y_mm)
    delta_x = target_x - source_x
    delta_y = target_y - source_y
    magnitude = math.sqrt(delta_x**2 + delta_y**2 + distance**2)
    rot_y = math.degrees(-math.asin(delta_x / magnitude))
    rot_x = math.degrees(math.atan2(delta_y, distance))

    alpha = math.radians(rot_x)
    beta = math.radians(rot_y)
    direction_x = -math.sin(beta)
    direction_y = math.sin(alpha) * math.cos(beta)
    direction_z = math.cos(alpha) * math.cos(beta)
    hit_x = source_x + direction_x * distance / direction_z
    hit_y = source_y + direction_y * distance / direction_z
    error = math.hypot(hit_x - target_x, hit_y - target_y)
    if not math.isfinite(error) or error > MAX_PROJECTION_ERROR_MM:
        raise RuntimeError(
            "Water-phantom spot-axis projection failed its geometric back-check: "
            f"isocenter error={error:.6g} mm"
        )
    return {
        "source_local_x_mm": source_x,
        "source_local_y_mm": source_y,
        "rotation_x_deg": rot_x,
        "rotation_y_deg": rot_y,
        "projection_error_mm": error,
    }


# ---------------------------------------------------------------------------
# TOPAS one-dimensional scorer output
# ---------------------------------------------------------------------------


def parse_topas_binheader(path: Path) -> dict[str, Any]:
    """Parse a TOPAS ``.binheader`` into axis bin counts, widths and units."""

    text = Path(path).read_text(encoding="utf-8", errors="strict")
    axes: dict[str, dict[str, Any]] = {}
    meta: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.rstrip()
        match = _HEADER_BIN_RE.match(line)
        if match:
            axes[match.group(1).upper()] = {
                "bins": int(match.group(2)),
                "width": float(match.group(3)),
                "unit": (match.group(4) or "").strip(),
            }
            continue
        if line.startswith("#") and ":" in line:
            key, _, value = line.lstrip("#").partition(":")
            meta[key.strip()] = value.strip()
    if not axes:
        raise RuntimeError(f"No binned axis found in TOPAS header: {path}")
    return {"axes": axes, "meta": meta}


def read_topas_1d(binary_path: Path, expected_bins: Optional[int] = None) -> np.ndarray:
    """Read a strictly one-dimensional TOPAS binary scorer output.

    The caller is responsible for having generated a scorer whose other two
    axes have exactly one bin.  That is verified here against the sidecar
    header, so a silently transposed multi-dimensional array can never be
    mistaken for a curve.
    """

    binary_path = Path(binary_path)
    header_path = Path(str(binary_path) + "header")
    values = np.fromfile(binary_path, dtype=np.float64)
    if values.size == 0:
        raise RuntimeError(f"TOPAS scorer output is empty: {binary_path}")
    if not np.isfinite(values).all():
        raise RuntimeError(f"TOPAS scorer output contains non-finite values: {binary_path}")
    if header_path.is_file():
        header = parse_topas_binheader(header_path)
        binned = {name: axis for name, axis in header["axes"].items() if axis["bins"] > 1}
        if len(binned) > 1:
            raise RuntimeError(
                f"{binary_path} is not one-dimensional; binned axes: "
                + ", ".join(f"{name}={axis['bins']}" for name, axis in sorted(binned.items()))
            )
        total = 1
        for axis in header["axes"].values():
            total *= int(axis["bins"])
        if total != values.size:
            raise RuntimeError(
                f"TOPAS header declares {total} bins but {binary_path} holds {values.size}"
            )
    if expected_bins is not None and values.size != int(expected_bins):
        raise RuntimeError(
            f"Expected {int(expected_bins)} bins in {binary_path}, found {values.size}"
        )
    return values


def bin_centers(count: int, width_mm: float, first_edge_mm: float) -> np.ndarray:
    """Centres of ``count`` equal bins of ``width_mm`` starting at ``first_edge_mm``."""

    if count < 1:
        raise RuntimeError("A binned axis needs at least one bin")
    return first_edge_mm + (np.arange(int(count), dtype=float) + 0.5) * float(width_mm)


# ---------------------------------------------------------------------------
# Depth-curve analysis
# ---------------------------------------------------------------------------


def normalize_to_max(values: Sequence[float]) -> np.ndarray:
    array = np.asarray(values, dtype=float)
    peak = float(array.max()) if array.size else 0.0
    if not math.isfinite(peak) or peak <= 0:
        raise RuntimeError("Cannot normalize a curve whose maximum is not positive")
    return array / peak


def peak_position(x: Sequence[float], y: Sequence[float]) -> float:
    """Sub-bin peak position from a parabola through the maximum and neighbours."""

    x_array = np.asarray(x, dtype=float)
    y_array = np.asarray(y, dtype=float)
    index = int(np.argmax(y_array))
    if index == 0 or index == y_array.size - 1:
        return float(x_array[index])
    x0, x1, x2 = x_array[index - 1 : index + 2]
    y0, y1, y2 = y_array[index - 1 : index + 2]
    denominator = (y0 - 2.0 * y1 + y2)
    if not math.isfinite(denominator) or abs(denominator) < 1e-15:
        return float(x_array[index])
    # Equal spacing is not assumed; solve the three-point parabola directly.
    matrix = np.array([[x0**2, x0, 1.0], [x1**2, x1, 1.0], [x2**2, x2, 1.0]], dtype=float)
    try:
        coefficients = np.linalg.solve(matrix, np.array([y0, y1, y2], dtype=float))
    except np.linalg.LinAlgError:
        return float(x_array[index])
    if abs(coefficients[0]) < 1e-18:
        return float(x_array[index])
    vertex = -coefficients[1] / (2.0 * coefficients[0])
    if not math.isfinite(vertex) or vertex < x_array[index - 1] or vertex > x_array[index + 1]:
        return float(x_array[index])
    return float(vertex)


def crossing_position(
    x: Sequence[float],
    y: Sequence[float],
    level: float,
    side: str = "distal",
) -> Optional[float]:
    """Linearly interpolated position where ``y`` crosses ``level``.

    ``side='distal'`` searches after the maximum, ``side='proximal'`` before it.
    Returns None when the curve never crosses the level on that side.
    """

    x_array = np.asarray(x, dtype=float)
    y_array = np.asarray(y, dtype=float)
    if x_array.size != y_array.size or x_array.size < 2:
        raise RuntimeError("Crossing search needs two matching arrays of at least two samples")
    peak_index = int(np.argmax(y_array))
    target = float(level)
    if side == "distal":
        segment = range(peak_index, x_array.size - 1)
        descending = True
    elif side == "proximal":
        segment = range(peak_index - 1, -1, -1)
        descending = False
    else:
        raise RuntimeError(f"Unknown crossing side: {side!r}")
    for index in segment:
        low, high = (index, index + 1) if descending else (index, index + 1)
        y_low, y_high = y_array[low], y_array[high]
        if descending:
            if y_low >= target >= y_high and y_low != y_high:
                fraction = (y_low - target) / (y_low - y_high)
                return float(x_array[low] + fraction * (x_array[high] - x_array[low]))
        else:
            if y_low <= target <= y_high and y_low != y_high:
                fraction = (target - y_low) / (y_high - y_low)
                return float(x_array[low] + fraction * (x_array[high] - x_array[low]))
    return None


def depth_curve_metrics(depth_mm: Sequence[float], dose: Sequence[float]) -> dict[str, Any]:
    """Range and shape metrics for one depth-dose curve."""

    depth = np.asarray(depth_mm, dtype=float)
    values = np.asarray(dose, dtype=float)
    if depth.size != values.size or depth.size < 3:
        raise RuntimeError("Depth-curve metrics need at least three matching samples")
    normalized = normalize_to_max(values)
    metrics: dict[str, Any] = {
        "peak_dose": float(values.max()),
        "R100_mm": peak_position(depth, values),
        "first_depth_mm": float(depth[0]),
        "last_depth_mm": float(depth[-1]),
        "entrance_dose_relative": float(normalized[0]),
    }
    for level in (0.9, 0.8, 0.5, 0.2, 0.1):
        distal = crossing_position(depth, normalized, level, "distal")
        metrics[f"R{int(round(level * 100)):d}_mm"] = distal
    proximal_80 = crossing_position(depth, normalized, 0.8, "proximal")
    metrics["proximal_R80_mm"] = proximal_80
    r80 = metrics.get("R80_mm")
    r20 = metrics.get("R20_mm")
    metrics["distal_falloff_80_20_mm"] = (
        float(r20 - r80) if r80 is not None and r20 is not None else None
    )
    peak = float(values.max())
    metrics["peak_to_entrance_ratio"] = (
        float(peak / values[0]) if values[0] > 0 else None
    )
    return metrics


# ---------------------------------------------------------------------------
# Lateral-profile analysis
# ---------------------------------------------------------------------------


def _gaussian(x: np.ndarray, amplitude: float, centre: float, sigma: float, baseline: float):
    return baseline + amplitude * np.exp(-0.5 * ((x - centre) / sigma) ** 2)


def profile_metrics(position_mm: Sequence[float], dose: Sequence[float]) -> dict[str, Any]:
    """Width metrics for one lateral profile.

    Both a moment-based sigma and, when SciPy is available and the fit converges,
    a Gaussian-fit sigma are reported.  They are reported separately rather than
    reconciled: a carbon spot at depth has fragment tails that are not Gaussian,
    and quietly preferring one estimate would hide that.
    """

    position = np.asarray(position_mm, dtype=float)
    values = np.asarray(dose, dtype=float)
    if position.size != values.size or position.size < 3:
        raise RuntimeError("Profile metrics need at least three matching samples")
    normalized = normalize_to_max(values)
    weights = np.clip(values, 0.0, None)
    total = float(weights.sum())
    if total <= 0:
        raise RuntimeError("Profile has no positive dose")
    centroid = float(np.dot(position, weights) / total)
    variance = float(np.dot((position - centroid) ** 2, weights) / total)
    metrics: dict[str, Any] = {
        "peak_dose": float(values.max()),
        "peak_position_mm": peak_position(position, values),
        "centroid_mm": centroid,
        "sigma_moment_mm": math.sqrt(variance) if variance > 0 else None,
        "gaussian_sigma_mm": None,
        "gaussian_centre_mm": None,
        "gaussian_baseline_relative": None,
        "gaussian_fit_status": "not attempted",
    }
    metrics["fwhm_mm"] = _width_at(position, normalized, 0.5)
    metrics["width_80_mm"] = _width_at(position, normalized, 0.8)
    metrics["width_20_mm"] = _width_at(position, normalized, 0.2)
    if metrics["width_20_mm"] is not None and metrics["width_80_mm"] is not None:
        metrics["penumbra_80_20_mm"] = float(
            (metrics["width_20_mm"] - metrics["width_80_mm"]) / 2.0
        )
    else:
        metrics["penumbra_80_20_mm"] = None
    if metrics["sigma_moment_mm"]:
        metrics["fwhm_from_moment_mm"] = float(metrics["sigma_moment_mm"] * SIGMA_TO_FWHM)
    else:
        metrics["fwhm_from_moment_mm"] = None

    try:
        from scipy.optimize import curve_fit  # type: ignore

        guess_sigma = metrics["sigma_moment_mm"] or max(float(position.ptp()) / 6.0, 1e-3)
        popt, _ = curve_fit(
            _gaussian,
            position,
            normalized,
            p0=[1.0, centroid, guess_sigma, 0.0],
            maxfev=20000,
        )
        sigma_fit = abs(float(popt[2]))
        if math.isfinite(sigma_fit) and sigma_fit > 0:
            metrics["gaussian_sigma_mm"] = sigma_fit
            metrics["gaussian_centre_mm"] = float(popt[1])
            metrics["gaussian_baseline_relative"] = float(popt[3])
            metrics["gaussian_fwhm_mm"] = float(sigma_fit * SIGMA_TO_FWHM)
            metrics["gaussian_fit_status"] = "converged"
        else:
            metrics["gaussian_fit_status"] = "rejected non-positive sigma"
    except ImportError:
        metrics["gaussian_fit_status"] = "scipy not available"
    except Exception as exc:  # noqa: BLE001 - a failed fit must not fail the analysis
        metrics["gaussian_fit_status"] = f"failed: {type(exc).__name__}"
    return metrics


def _width_at(position: np.ndarray, normalized: np.ndarray, level: float) -> Optional[float]:
    left = crossing_position(position, normalized, level, "proximal")
    right = crossing_position(position, normalized, level, "distal")
    if left is None or right is None:
        return None
    return float(right - left)


# ---------------------------------------------------------------------------
# Reference comparison
# ---------------------------------------------------------------------------


def gamma_1d(
    reference_x: Sequence[float],
    reference_y: Sequence[float],
    evaluated_x: Sequence[float],
    evaluated_y: Sequence[float],
    dose_percent: float = 3.0,
    distance_mm: float = 3.0,
    threshold_fraction: float = 0.1,
    resample_step_mm: float = 0.05,
) -> dict[str, Any]:
    """One-dimensional global gamma of an evaluated curve against a reference.

    Both curves are normalized to their own maximum first, so this compares
    shape and range, not absolute output.  The evaluated curve is resampled onto
    a fine grid before the search so the distance term is not quantized by the
    simulation bin width.
    """

    ref_x = np.asarray(reference_x, dtype=float)
    ref_y = normalize_to_max(reference_y)
    eval_x = np.asarray(evaluated_x, dtype=float)
    eval_y = normalize_to_max(evaluated_y)
    if ref_x.size != ref_y.size or eval_x.size != eval_y.size:
        raise RuntimeError("Gamma needs matching position/value arrays")

    low = max(float(ref_x.min()), float(eval_x.min()))
    high = min(float(ref_x.max()), float(eval_x.max()))
    if not math.isfinite(low) or not math.isfinite(high) or high <= low:
        raise RuntimeError("Reference and evaluated curves do not overlap")
    step = float(resample_step_mm)
    if step <= 0:
        raise RuntimeError("Gamma resample step must be positive")
    fine_x = np.arange(low, high + 0.5 * step, step)
    fine_y = np.interp(fine_x, eval_x, eval_y)

    selection = (ref_x >= low) & (ref_x <= high) & (ref_y >= float(threshold_fraction))
    selected_x = ref_x[selection]
    selected_y = ref_y[selection]
    if selected_x.size == 0:
        raise RuntimeError(
            "No reference sample is inside the overlap and above the gamma dose threshold"
        )
    dose_criterion = float(dose_percent) / 100.0
    distance_criterion = float(distance_mm)
    if dose_criterion <= 0 or distance_criterion <= 0:
        raise RuntimeError("Gamma criteria must be positive")

    gamma = np.empty(selected_x.size, dtype=float)
    for index in range(selected_x.size):
        dose_term = (fine_y - selected_y[index]) / dose_criterion
        distance_term = (fine_x - selected_x[index]) / distance_criterion
        gamma[index] = math.sqrt(float(np.min(dose_term**2 + distance_term**2)))
    return {
        "criteria": f"{dose_percent:g}%/{distance_mm:g} mm global, {threshold_fraction * 100:g}% threshold",
        "dose_percent": float(dose_percent),
        "distance_mm": float(distance_mm),
        "threshold_fraction": float(threshold_fraction),
        "evaluated_points": int(selected_x.size),
        "pass_rate_percent": float(100.0 * np.count_nonzero(gamma <= 1.0) / gamma.size),
        "max_gamma": float(gamma.max()),
        "mean_gamma": float(gamma.mean()),
        "overlap_mm": [low, high],
    }


def compare_depth_curves(
    reference_depth_mm: Sequence[float],
    reference_dose: Sequence[float],
    evaluated_depth_mm: Sequence[float],
    evaluated_dose: Sequence[float],
    dose_percent: float = 3.0,
    distance_mm: float = 3.0,
    threshold_fraction: float = 0.1,
    reference_label: str = "reference",
) -> dict[str, Any]:
    """Compare two depth-dose curves by shape, range and one-dimensional gamma."""

    ref_depth = np.asarray(reference_depth_mm, dtype=float)
    ref_dose = normalize_to_max(reference_dose)
    eval_depth = np.asarray(evaluated_depth_mm, dtype=float)
    eval_dose = normalize_to_max(evaluated_dose)

    low = max(float(ref_depth.min()), float(eval_depth.min()))
    high = min(float(ref_depth.max()), float(eval_depth.max()))
    if high <= low:
        raise RuntimeError(
            f"{reference_label} covers {ref_depth.min():g}..{ref_depth.max():g} mm and the "
            f"simulation covers {eval_depth.min():g}..{eval_depth.max():g} mm; they do not overlap"
        )
    inside = (ref_depth >= low) & (ref_depth <= high)
    common_depth = ref_depth[inside]
    common_reference = ref_dose[inside]
    common_evaluated = np.interp(common_depth, eval_depth, eval_dose)
    difference = (common_evaluated - common_reference) * 100.0

    reference_metrics = depth_curve_metrics(ref_depth, ref_dose)
    evaluated_metrics = depth_curve_metrics(eval_depth, eval_dose)
    range_delta: dict[str, Any] = {}
    for key in ("R100_mm", "R90_mm", "R80_mm", "R50_mm", "R20_mm", "distal_falloff_80_20_mm"):
        left, right = evaluated_metrics.get(key), reference_metrics.get(key)
        range_delta[key] = (
            float(left - right) if left is not None and right is not None else None
        )

    return {
        "reference_label": reference_label,
        "overlap_mm": [low, high],
        "compared_points": int(common_depth.size),
        "max_abs_difference_percent": float(np.abs(difference).max()),
        "mean_abs_difference_percent": float(np.abs(difference).mean()),
        "rms_difference_percent": float(math.sqrt(float(np.mean(difference**2)))),
        "signed_mean_difference_percent": float(difference.mean()),
        "reference_metrics": reference_metrics,
        "evaluated_metrics": evaluated_metrics,
        "range_difference_mm": range_delta,
        "gamma": gamma_1d(
            ref_depth[inside],
            common_reference,
            eval_depth,
            eval_dose,
            dose_percent=dose_percent,
            distance_mm=distance_mm,
            threshold_fraction=threshold_fraction,
        ),
        "curve": {
            "depth_mm": common_depth,
            "reference_relative": common_reference,
            "evaluated_relative": common_evaluated,
            "difference_percent": difference,
        },
    }
