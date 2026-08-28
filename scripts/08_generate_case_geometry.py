#!/usr/bin/env python3
"""Generate TOPAS geometry for a supported water phantom or DICOM CT patient."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

import numpy as np
import pydicom


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from scripts.utils.commissioned_beam import load_commissioned_model


SCHNEIDER_REFERENCE_SHA256 = "5022cd89617b28dbd8ee8bf8b095ea20cfd99f6405218693c0df238b3617a139"


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=root)
    parser.add_argument(
        "--beam-model-mode", choices=("baseline", "commissioned"), default="baseline"
    )
    parser.add_argument("--beam-model-profile", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def discover_one(directory: Path, predicate, description: str):
    matches = []
    for path in sorted(directory.glob("*.dcm")):
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        if predicate(ds):
            matches.append((path.resolve(), ds))
    if len(matches) != 1:
        raise RuntimeError(f"Expected one {description}, found {len(matches)}")
    return matches[0]


def fmt(value: float) -> str:
    return f"{value:.10g}"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_schneider_table(root: Path) -> tuple[Path, str]:
    destination = root / "machine_model" / "HUtoMaterialSchneider.txt"
    if destination.is_file():
        return destination, "project machine_model"
    candidates: list[Path] = []
    configured = os.environ.get("TOPAS_REFERENCE_ROOT")
    if configured:
        candidates.append(Path(configured).expanduser() / "examples/Patient/HUtoMaterialSchneider.txt")
    candidates.extend(
        [
            Path.home() / "Applications/TOPAS/OpenTOPAS/examples/Patient/HUtoMaterialSchneider.txt",
            Path("/Applications/TOPAS/OpenTOPAS/examples/Patient/HUtoMaterialSchneider.txt"),
        ]
    )
    source = next((path for path in candidates if path.is_file()), None)
    if source is None:
        raise RuntimeError(
            "DICOM CT mode requires machine_model/HUtoMaterialSchneider.txt. "
            "Copy a reviewed HU-to-material table there, or set TOPAS_REFERENCE_ROOT."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    return destination, f"TOPAS reference example {source}"


def ct_bounds(ct_directory: Path) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    slices = []
    for path in ct_directory.glob("*.dcm"):
        ds = pydicom.dcmread(path, stop_before_pixels=True)
        position = np.asarray(ds.ImagePositionPatient, dtype=float)
        slices.append((float(position[2]), position, ds))
    if not slices:
        raise RuntimeError("No CT slices found")
    slices.sort(key=lambda item: item[0])
    _, first_position, first = slices[0]
    _, last_position, last = slices[-1]
    iop = np.asarray(first.ImageOrientationPatient, dtype=float)
    if iop.shape != (6,) or not np.allclose(iop, [1, 0, 0, 0, 1, 0]):
        raise RuntimeError(f"DICOM CT patient geometry currently requires axial identity IOP, got {iop.tolist()}")
    spacing = np.asarray(
        [float(first.PixelSpacing[1]), float(first.PixelSpacing[0])], dtype=float
    )
    if len(slices) >= 2:
        slice_spacing = float(np.median(np.diff([item[0] for item in slices])))
    else:
        slice_spacing = float(getattr(first, "SliceThickness", 1.0))
    if slice_spacing <= 0:
        raise RuntimeError(f"Invalid CT slice spacing: {slice_spacing}")
    bounds_min = np.asarray(
        [
            first_position[0] - 0.5 * spacing[0],
            first_position[1] - 0.5 * spacing[1],
            first_position[2] - 0.5 * slice_spacing,
        ],
        dtype=float,
    )
    bounds_max = np.asarray(
        [
            first_position[0] + (int(first.Columns) - 0.5) * spacing[0],
            first_position[1] + (int(first.Rows) - 0.5) * spacing[1],
            last_position[2] + 0.5 * slice_spacing,
        ],
        dtype=float,
    )
    voxel_size = np.asarray([spacing[0], spacing[1], slice_spacing], dtype=float)
    voxel_count = np.asarray([int(first.Columns), int(first.Rows), len(slices)], dtype=int)
    if any(
        int(ds.Rows) != int(first.Rows)
        or int(ds.Columns) != int(first.Columns)
        or not np.allclose(np.asarray(ds.PixelSpacing, dtype=float), np.asarray(first.PixelSpacing, dtype=float))
        for _, _, ds in slices
    ):
        raise RuntimeError("CT rows, columns or in-plane spacing are inconsistent")
    return bounds_min, bounds_max, voxel_size, voxel_count


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    compatibility = root / "plan_parsed" / "compatibility_summary.txt"
    model_path = root / "plan_parsed" / "patient_model.json"
    if not compatibility.is_file() or "READY FOR CURRENT QA WORKFLOW" not in compatibility.read_text(
        encoding="utf-8"
    ):
        raise RuntimeError("Compatibility gate is missing or blocked; run script 07 first")
    if not model_path.is_file():
        raise RuntimeError("Patient model selection is missing; rerun script 07")
    patient_model = json.loads(model_path.read_text(encoding="utf-8"))
    mode = str(patient_model.get("mode", ""))
    if mode not in {"WATER_PHANTOM", "DICOM_CT_SCHNEIDER"}:
        raise RuntimeError(f"Unsupported patient model mode: {mode}")

    plan_path, plan = discover_one(
        root / "dicom" / "RTPLAN",
        lambda ds: str(getattr(ds, "SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.481.8",
        "RT Ion Plan",
    )
    structure_path, _ = discover_one(
        root / "dicom" / "RTSTRUCT",
        lambda ds: str(getattr(ds, "Modality", "")) == "RTSTRUCT",
        "RTSTRUCT",
    )
    dose_path, dose = discover_one(
        root / "dicom" / "RTDOSE",
        lambda ds: str(getattr(ds, "DoseType", "")).upper() == "PHYSICAL"
        and str(getattr(ds, "DoseSummationType", "")).upper() == "PLAN",
        "physical plan RTDOSE",
    )
    beams = list(getattr(plan, "IonBeamSequence", []))
    if len(beams) != 1:
        raise RuntimeError("Current geometry generator supports exactly one beam")
    cp = beams[0].IonControlPointSequence[0]
    setup = list(getattr(plan, "PatientSetupSequence", []))
    position = str(getattr(setup[0], "PatientPosition", "")) if setup else ""
    gantry = float(getattr(cp, "GantryAngle", float("nan")))
    couch = float(getattr(cp, "PatientSupportAngle", 0.0))
    if position != "HFS" or not np.isclose(gantry, 90.0) or not np.isclose(couch, 0.0):
        raise RuntimeError(f"Supported mapping is HFS/G90/couch0, got {position}/{gantry}/{couch}")
    isocenter = np.asarray(cp.IsocenterPosition, dtype=float)

    if mode == "WATER_PHANTOM":
        external = patient_model.get("external_roi") or {}
        if not external.get("rectangular_xy"):
            raise RuntimeError("Water phantom requires a rectangular selected External ROI")
        bounds_min = np.asarray(external["bounds_min_mm"], dtype=float)
        bounds_max = np.asarray(external["bounds_max_mm"], dtype=float)
        voxel_size = None
        voxel_count = None
    else:
        bounds_min, bounds_max, voxel_size, voxel_count = ct_bounds(root / "dicom" / "CT")
    center = 0.5 * (bounds_min + bounds_max)
    half = 0.5 * (bounds_max - bounds_min)
    translation = center - isocenter
    beam_model_description = "BASELINE (uncommissioned simulation source plane)"
    beam_model_audit = "not applicable"
    if args.beam_model_mode == "commissioned":
        treatment_machine = str(getattr(beams[0], "TreatmentMachineName", ""))
        model = load_commissioned_model(root, args.beam_model_profile, treatment_machine)
        source_distance = float(model.source_plane_mm)
        model.validate_rtplan(
            treatment_machine,
            getattr(beams[0], "VirtualSourceAxisDistances", []),
        )
        particle_calibration = model.particle_calibration()
        minimum_clearance = float(bounds_max[0] - isocenter[0] + 10.0)
        if source_distance <= minimum_clearance:
            raise RuntimeError(
                f"Commissioned source plane {source_distance:g} mm intersects/starts inside patient bounds; "
                f"required > {minimum_clearance:g} mm"
            )
        beam_model_description = (
            f"COMMISSIONED ({model.machine_name}); profile={model.profile_path}; "
            f"fingerprint={model.fingerprint}; particle_calibration_sha256="
            f"{particle_calibration.binding_sha256}"
        )
        audit = model.phase_measurement_audit
        beam_model_audit = (
            f"phase-space measured-sigma energies={audit['audited_energies']}, "
            f"median/max RMSE={audit['median_rmse_mm']:.10g}/{audit['maximum_rmse_mm']:.10g} mm, "
            f"max isocenter error={audit['maximum_isocenter_error_mm']:.10g} mm"
        )
    else:
        source_distance = max(300.0, float(bounds_max[0] - isocenter[0] + 100.0))

    ipp = np.asarray(dose.ImagePositionPatient, dtype=float)
    offsets = np.asarray(dose.GridFrameOffsetVector, dtype=float)
    spacing = np.asarray(
        [float(dose.PixelSpacing[1]), float(dose.PixelSpacing[0]), float(np.diff(offsets)[0])],
        dtype=float,
    )
    shape_xyz = np.asarray([int(dose.Columns), int(dose.Rows), int(dose.NumberOfFrames)], dtype=float)
    grid_half = 0.5 * shape_xyz * spacing
    grid_first_edge = ipp - 0.5 * spacing
    grid_center = grid_first_edge + grid_half
    grid_translation = grid_center - isocenter
    relevant = np.r_[
        bounds_min - isocenter,
        bounds_max - isocenter,
        grid_center - grid_half - isocenter,
        grid_center + grid_half - isocenter,
    ]
    world_half = max(350.0, source_distance + 50.0, float(np.max(np.abs(relevant))) + 50.0)

    world = root / "topas" / "geometry" / "world.txt"
    patient = root / "topas" / "geometry" / "patient.txt"
    isocenter_path = root / "topas" / "geometry" / "isocenter.txt"
    beam = root / "topas" / "beam" / "beam_geometry.txt"
    generated_schneider = root / "topas" / "materials" / "HUtoMaterialSchneider.txt"
    summary = root / "plan_parsed" / "topas_case_geometry_summary.txt"
    outputs = [world, patient, isocenter_path, beam, summary]
    if mode == "DICOM_CT_SCHNEIDER":
        outputs.append(generated_schneider)
    existing = [path for path in outputs if path.exists()]
    if existing and not args.overwrite:
        raise RuntimeError("Outputs exist; add --overwrite: " + ", ".join(map(str, existing)))
    for path in outputs:
        path.parent.mkdir(parents=True, exist_ok=True)

    world.write_text(
        f"# Auto-generated DICOM-patient-axis world; origin is plan isocenter {isocenter.tolist()} mm.\n\n"
        's:Ge/World/Material = "G4_AIR"\n'
        f"d:Ge/World/HLX = {fmt(world_half)} mm\n"
        f"d:Ge/World/HLY = {fmt(world_half)} mm\n"
        f"d:Ge/World/HLZ = {fmt(world_half)} mm\n"
        'b:Ge/World/Invisible = "True"\n',
        encoding="utf-8",
    )

    table_description = "not applicable"
    if mode == "WATER_PHANTOM":
        patient.write_text(
            f"# Auto-generated water phantom from selected RTSTRUCT External bounds.\n"
            f"# Source: {structure_path}\n\n"
            "includeFile = geometry/world.txt\n\n"
            's:Ge/Patient/Type = "TsBox"\n'
            's:Ge/Patient/Parent = "World"\n'
            's:Ge/Patient/Material = "G4_WATER"\n'
            f"d:Ge/Patient/HLX = {fmt(half[0])} mm\n"
            f"d:Ge/Patient/HLY = {fmt(half[1])} mm\n"
            f"d:Ge/Patient/HLZ = {fmt(half[2])} mm\n"
            f"d:Ge/Patient/TransX = {fmt(translation[0])} mm\n"
            f"d:Ge/Patient/TransY = {fmt(translation[1])} mm\n"
            f"d:Ge/Patient/TransZ = {fmt(translation[2])} mm\n"
            's:Ge/Patient/Color = "green"\n'
            's:Ge/Patient/DrawingStyle = "WireFrame"\n',
            encoding="utf-8",
        )
        material_summary = "G4_WATER inside selected RTSTRUCT External bounding box"
    else:
        table, table_origin = ensure_schneider_table(root)
        table_hash = sha256(table)
        generated_schneider.write_text(
            "# Auto-generated include chain for patient CT.\n"
            f"# Table source: {table}\n"
            f"# SHA-256: {table_hash}\n"
            "# Generic reference table; replace only after institutional review/commissioning.\n\n"
            "includeFile = geometry/world.txt\n\n"
            + table.read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        table_description = f"{table_origin}; SHA-256={table_hash}"
        patient.write_text(
            "# Auto-generated DICOM CT patient geometry.\n"
            f"# CT source: {(root / 'dicom' / 'CT').resolve()}\n\n"
            "includeFile = materials/HUtoMaterialSchneider.txt\n\n"
            f"d:Ge/PlanIsoCenterX = {fmt(isocenter[0])} mm\n"
            f"d:Ge/PlanIsoCenterY = {fmt(isocenter[1])} mm\n"
            f"d:Ge/PlanIsoCenterZ = {fmt(isocenter[2])} mm\n\n"
            's:Ge/Patient/Type = "TsDicomPatient"\n'
            's:Ge/Patient/Parent = "World"\n'
            's:Ge/Patient/Material = "G4_WATER"\n'
            'd:Ge/Patient/RotX = 0 deg\n'
            'd:Ge/Patient/RotY = 0 deg\n'
            'd:Ge/Patient/RotZ = 0 deg\n'
            'dc:Ge/Patient/DicomOriginX = 0 mm\n'
            'dc:Ge/Patient/DicomOriginY = 0 mm\n'
            'dc:Ge/Patient/DicomOriginZ = 0 mm\n'
            'd:Ge/Patient/TransX = Ge/Patient/DicomOriginX - Ge/PlanIsoCenterX mm\n'
            'd:Ge/Patient/TransY = Ge/Patient/DicomOriginY - Ge/PlanIsoCenterY mm\n'
            'd:Ge/Patient/TransZ = Ge/Patient/DicomOriginZ - Ge/PlanIsoCenterZ mm\n'
            f's:Ge/Patient/DicomDirectory = "{(root / "dicom" / "CT").resolve()}"\n'
            'sv:Ge/Patient/DicomModalityTags = 1 "CT"\n',
            encoding="utf-8",
        )
        material_summary = "TsDicomPatient with generic TOPAS Schneider HU-to-material conversion (uncommissioned)"

    isocenter_path.write_text(
        "# Auto-generated isocenter and RPPD grid guides.\n"
        f"# Dose source: {dose_path}\n\n"
        "includeFile = geometry/patient.txt\n\n"
        's:Ge/IsocenterMarker/Type = "TsSphere"\n'
        's:Ge/IsocenterMarker/Parent = "World"\n'
        'b:Ge/IsocenterMarker/IsParallel = "True"\n'
        'd:Ge/IsocenterMarker/RMin = 0 mm\n'
        'd:Ge/IsocenterMarker/RMax = 4 mm\n'
        'd:Ge/IsocenterMarker/TransX = 0 mm\n'
        'd:Ge/IsocenterMarker/TransY = 0 mm\n'
        'd:Ge/IsocenterMarker/TransZ = 0 mm\n'
        's:Ge/IsocenterMarker/Color = "red"\n'
        's:Ge/IsocenterMarker/DrawingStyle = "Solid"\n\n'
        's:Ge/RPPDGridOutline/Type = "TsBox"\n'
        's:Ge/RPPDGridOutline/Parent = "World"\n'
        'b:Ge/RPPDGridOutline/IsParallel = "True"\n'
        f"d:Ge/RPPDGridOutline/HLX = {fmt(grid_half[0])} mm\n"
        f"d:Ge/RPPDGridOutline/HLY = {fmt(grid_half[1])} mm\n"
        f"d:Ge/RPPDGridOutline/HLZ = {fmt(grid_half[2])} mm\n"
        f"d:Ge/RPPDGridOutline/TransX = {fmt(grid_translation[0])} mm\n"
        f"d:Ge/RPPDGridOutline/TransY = {fmt(grid_translation[1])} mm\n"
        f"d:Ge/RPPDGridOutline/TransZ = {fmt(grid_translation[2])} mm\n"
        's:Ge/RPPDGridOutline/Color = "magenta"\n'
        's:Ge/RPPDGridOutline/DrawingStyle = "WireFrame"\n',
        encoding="utf-8",
    )
    beam.write_text(
        "# Auto-generated single-beam HFS/G90/couch0 geometry.\n"
        f"# Source: {plan_path}\n\n"
        + (
            f"# Beam model mode: COMMISSIONED ({model.machine_name})\n"
            if args.beam_model_mode == "commissioned"
            else "# Beam model mode: BASELINE\n"
        )
        + f"# Beam source plane upstream: {fmt(source_distance)} mm\n\n"
        + "includeFile = geometry/isocenter.txt\n\n"
        's:Ge/PlanBeamPosition/Type = "Group"\n'
        's:Ge/PlanBeamPosition/Parent = "World"\n'
        f"d:Ge/PlanBeamPosition/TransX = {fmt(source_distance)} mm\n"
        'd:Ge/PlanBeamPosition/TransY = 0 mm\n'
        'd:Ge/PlanBeamPosition/TransZ = 0 mm\n'
        'd:Ge/PlanBeamPosition/RotX = 0 deg\n'
        'd:Ge/PlanBeamPosition/RotY = 90 deg\n'
        'd:Ge/PlanBeamPosition/RotZ = 0 deg\n\n'
        's:Ge/BeamAxisGuide/Type = "TsCylinder"\n'
        's:Ge/BeamAxisGuide/Parent = "World"\n'
        'b:Ge/BeamAxisGuide/IsParallel = "True"\n'
        'd:Ge/BeamAxisGuide/RMin = 0 mm\n'
        'd:Ge/BeamAxisGuide/RMax = 1.2 mm\n'
        f"d:Ge/BeamAxisGuide/HL = {fmt(source_distance)} mm\n"
        'd:Ge/BeamAxisGuide/TransX = 0 mm\n'
        'd:Ge/BeamAxisGuide/TransY = 0 mm\n'
        'd:Ge/BeamAxisGuide/TransZ = 0 mm\n'
        'd:Ge/BeamAxisGuide/RotX = 0 deg\n'
        'd:Ge/BeamAxisGuide/RotY = 90 deg\n'
        'd:Ge/BeamAxisGuide/RotZ = 0 deg\n'
        's:Ge/BeamAxisGuide/Color = "yellow"\n'
        's:Ge/BeamAxisGuide/DrawingStyle = "Solid"\n',
        encoding="utf-8",
    )
    summary.write_text(
        "TPS-TOPAS generated case geometry\n"
        "=================================\n"
        f"RTPLAN: {plan_path}\nRTSTRUCT: {structure_path}\n"
        f"RPPD: {dose_path}\n"
        f"Patient model: {mode}\n"
        f"Isocenter: {isocenter.tolist()} mm\n"
        f"Patient geometry bounds: {bounds_min.tolist()} .. {bounds_max.tolist()} mm\n"
        f"Patient half lengths / center translation: {half.tolist()} / {translation.tolist()} mm\n"
        + (f"CT voxel count / size: {voxel_count.tolist()} / {voxel_size.tolist()} mm\n" if voxel_count is not None else "")
        + f"RPPD grid half lengths / translation: {grid_half.tolist()} / {grid_translation.tolist()} mm\n"
        f"Beam: HFS, gantry {gantry}, couch {couch}, direction patient +X to -X\n"
        f"Beam model: {beam_model_description}\n"
        f"Beam model audit: {beam_model_audit}\n"
        f"Beam source plane upstream: {source_distance:.10g} mm\n"
        f"Material: {material_summary}\n"
        f"HU table provenance: {table_description}\n"
        "Scope: research physical-dose shape QA only. Patient CT requires institutional HU calibration and full commissioning before clinical claims.\n",
        encoding="utf-8",
    )
    print(summary.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
