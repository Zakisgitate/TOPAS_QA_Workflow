from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from gui.case_results import (
    analysis_run_dir,
    case_identity,
    require_identified_case,
    update_run_manifest,
)
from gui.web_app import APP_ROOT, validate_case_root


class CaseRootGuardTest(unittest.TestCase):
    """Reject folders that cannot be a case root without corrupting something.

    Deliberately narrower than "must not live under the project's dicom/": a
    well-formed case folder that happens to sit there works correctly and
    several already do. See OPTIMIZATION_REPORT item 4.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="plan1699-caseroot-")
        self.base = Path(self.temporary.name)
        self.case = self.base / "a-case"
        for name in ("dicom/CT", "dicom/RTPLAN", "analysis", "topas_output", "plan_parsed"):
            (self.case / name).mkdir(parents=True, exist_ok=True)
        (self.case / "case_config.json").write_text("{}", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_a_well_formed_case_folder_is_accepted(self) -> None:
        self.assertEqual(validate_case_root(str(self.case)), self.case.resolve())

    def test_a_brand_new_empty_folder_is_accepted(self) -> None:
        fresh = self.base / "fresh"
        fresh.mkdir()
        self.assertEqual(validate_case_root(str(fresh)), fresh.resolve())

    def test_a_folder_holding_loose_dicom_is_rejected(self) -> None:
        images = self.base / "loose-images"
        images.mkdir()
        (images / "CT_00001.dcm").write_bytes(b"\x00")
        with self.assertRaises(RuntimeError) as caught:
            validate_case_root(str(images))
        self.assertIn("contains DICOM files directly", str(caught.exception))

    def test_case_data_subdirectories_are_rejected(self) -> None:
        for name in ("dicom", "analysis", "topas_output", "plan_parsed"):
            with self.subTest(name=name), self.assertRaises(RuntimeError) as caught:
                validate_case_root(str(self.case / name))
            self.assertIn("data folder of the case", str(caught.exception))

    def test_any_folder_nested_in_a_case_is_rejected(self) -> None:
        nested = self.case / "measurement"
        nested.mkdir(exist_ok=True)
        with self.assertRaises(RuntimeError) as caught:
            validate_case_root(str(nested))
        self.assertIn("inside the existing case", str(caught.exception))

    def test_application_directories_are_rejected(self) -> None:
        for name in ("gui", "scripts", "topas"):
            with self.subTest(name=name), self.assertRaises(RuntimeError) as caught:
                validate_case_root(str(APP_ROOT / name))
            self.assertIn("belongs to the application", str(caught.exception))

    def test_the_app_root_itself_stays_usable(self) -> None:
        # APP_ROOT is the template case every other case is created from.
        self.assertEqual(validate_case_root(str(APP_ROOT)), APP_ROOT)

    def test_existing_project_cases_are_not_broken(self) -> None:
        existing = [
            path
            for path in (APP_ROOT / "dicom").glob("*")
            if path.is_dir() and (path / "case_config.json").is_file()
        ]
        for case in existing:
            with self.subTest(case=case.name):
                self.assertEqual(validate_case_root(str(case)), case.resolve())

    def test_unsafe_shallow_paths_are_still_rejected(self) -> None:
        for value in ("/", "/tmp"):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                validate_case_root(value)


class CaseIdentityGuardTest(unittest.TestCase):
    """A placeholder identity must never become a cache directory.

    `patient-anonymous--study-.../plan-plan--.../` collected runs from unrelated
    plans into one tree: on disk, one case's anonymous folder held a run tagged
    for a different case entirely.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="plan1699-identity-")
        self.root = Path(self.temporary.name) / "case"
        (self.root / "dicom" / "CT").mkdir(parents=True)
        (self.root / "dicom" / "RTPLAN").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_identity_of_an_empty_case_is_flagged_unidentified(self) -> None:
        identity = case_identity(self.root)
        self.assertFalse(identity.identified)
        # Reading it must still work: the DICOM import path compares the
        # previous identity against incoming files before anything exists.
        self.assertTrue(identity.patient_key.startswith("patient-anonymous"))

    def test_require_identified_case_refuses_a_placeholder(self) -> None:
        with self.assertRaises(RuntimeError) as caught:
            require_identified_case(self.root)
        self.assertIn("no readable RTPLAN or CT", str(caught.exception))

    def test_cache_creation_is_blocked_and_writes_nothing(self) -> None:
        with self.assertRaises(RuntimeError):
            analysis_run_dir(self.root, "tag", create=True)
        with self.assertRaises(RuntimeError):
            update_run_manifest(self.root, "tag")
        self.assertFalse(
            (self.root / "analysis").exists(),
            "a blocked cache write must not leave a placeholder directory behind",
        )

    def test_read_only_path_lookup_still_works(self) -> None:
        # Resolving a path without create=True stays available for callers that
        # only report where results *would* go.
        self.assertIn("patient-anonymous", str(analysis_run_dir(self.root, "tag")))

    def test_a_real_case_is_identified(self) -> None:
        real = [
            path
            for path in (APP_ROOT / "dicom").glob("*")
            if path.is_dir() and list((path / "dicom" / "RTPLAN").glob("*.dcm"))
        ]
        if not real:
            self.skipTest("no imported case available in this checkout")
        identity = case_identity(real[0])
        self.assertTrue(identity.identified)
        self.assertNotIn("anonymous", identity.patient_key)


if __name__ == "__main__":
    sys.exit(unittest.main())
