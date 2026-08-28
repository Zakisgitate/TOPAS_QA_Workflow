from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from gui.machine_models import (
    extract_package,
    import_inspected_package,
    inspect_extracted_package,
    list_machine_models,
    set_model_active,
)
from scripts.utils.commissioned_beam import resolve_profile


PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / "machine_model" / "beam_commissioning" / "hzRoom1_90_RF4_250701"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_package(parent: Path, *, corrupt_hash: bool = False) -> Path:
    package = parent / "supplier-package"
    package.mkdir(parents=True)
    names = {
        "profile": "profile.json",
        "particle_calibration": "particle_calibration.json",
        "energy_spectrum": "energy_spectrum.json",
        "phase_space": "phase_space.json",
        "number_per_mu": "number_per_mu.txt",
        "measured_idd": "measured_pristine_bragg_peaks.csv",
        "measured_spot_sigma": "measured_spot_sigma.csv",
        "energy_list": "commissioned_energy_list.txt",
    }
    for filename in names.values():
        shutil.copy2(SOURCE / filename, package / filename)
    hashes = {key: digest(package / filename) for key, filename in names.items()}
    if corrupt_hash:
        hashes["measured_idd"] = "0" * 64
    manifest = {
        "schema_version": 1,
        "package_kind": "beam_commissioning",
        "package_version": "2026.08-test",
        "subject": {"treatment_machine_name": "hzRoom1_90_RF4_250701"},
        "units": {
            "energy_spectrum": "total MeV per carbon ion",
            "phase_space_position_sigma": "mm",
            "phase_space_angular_sigma": "rad",
            "number_per_mu": "primary carbon ions per MU",
            "measured_idd_depth": "mm",
            "measured_spot_sigma": "mm",
            "commissioned_energy": "MeV/u",
        },
        "files": names,
        "sha256": hashes,
        "provenance": {"source": "unit-test copy of existing commissioned package"},
        "approval": {
            "status": "approved",
            "approved_by": "Test reviewer",
            "approved_at": "2026-08-21T00:00:00+08:00",
            "evidence": "TEST-ONLY",
        },
    }
    (package / "machine_package.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return package


class MachineModelPackageTests(unittest.TestCase):
    def test_inspect_import_immutable_deactivate_and_historical_reference(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "case"
            root.mkdir()
            package = make_package(Path(temporary))
            inspection = inspect_extracted_package(root, package)
            self.assertTrue(inspection["importAllowed"])
            self.assertEqual(inspection["summary"]["block"], 0)
            self.assertTrue(any(row["level"] == "WARN" for row in inspection["report"]))

            imported = import_inspected_package(root, inspection)
            self.assertFalse(imported["alreadyImported"])
            model = imported["model"]
            profile = Path(model["profile"])
            self.assertTrue(profile.is_file())
            self.assertEqual(resolve_profile(root, treatment_machine_name=model["machineName"]), profile)

            repeat = import_inspected_package(root, inspection)
            self.assertTrue(repeat["alreadyImported"])
            self.assertEqual(repeat["model"]["id"], model["id"])

            cached = root / "analysis" / "patient" / "plan" / "run" / "manifest.json"
            cached.parent.mkdir(parents=True)
            cached.write_text(
                json.dumps({"model_fingerprint": model["modelFingerprint"]}), encoding="utf-8"
            )
            listed = list_machine_models(root)
            registered = next(item for item in listed["models"] if item["id"] == model["id"])
            self.assertEqual(registered["referenceCount"], 1)

            inactive = set_model_active(root, model["id"], False)
            self.assertFalse(inactive["active"])
            with self.assertRaisesRegex(RuntimeError, "deactivated"):
                resolve_profile(root, explicit=profile, treatment_machine_name=model["machineName"])
            with self.assertRaisesRegex(RuntimeError, "No commissioned beam profile"):
                resolve_profile(root, treatment_machine_name=model["machineName"])
            self.assertTrue(profile.is_file(), "deactivation must never remove immutable content")

    def test_bad_declared_hash_blocks_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "case"
            root.mkdir()
            inspection = inspect_extracted_package(root, make_package(base, corrupt_hash=True))
            self.assertFalse(inspection["importAllowed"])
            self.assertGreater(inspection["summary"]["block"], 0)
            with self.assertRaisesRegex(RuntimeError, "BLOCK"):
                import_inspected_package(root, inspection)

    def test_legacy_model_can_be_deactivated_without_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "case"
            destination = root / "machine_model" / "beam_commissioning" / SOURCE.name
            shutil.copytree(SOURCE, destination)
            listed = list_machine_models(root)
            legacy = next(item for item in listed["models"] if item["legacy"])
            changed = set_model_active(root, legacy["id"], False)
            self.assertFalse(changed["active"])
            self.assertTrue(Path(changed["profile"]).is_file())
            self.assertEqual(list_machine_models(root)["compatibleBeamProfiles"], [])

    def test_zip_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            archive = base / "bad.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../outside.txt", "unsafe")
                output.writestr("machine_package.json", "{}")
            with self.assertRaisesRegex(RuntimeError, "Unsafe path"):
                extract_package(archive, base / "extract")


if __name__ == "__main__":
    unittest.main()
