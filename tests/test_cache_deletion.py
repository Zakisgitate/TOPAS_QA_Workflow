from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from gui.case_results import analysis_run_dir, discover_cached_runs, trash_cached_run
import gui.web_app as web_app


def write_plan(root: Path) -> None:
    destination = root / "dicom" / "RTPLAN" / "RTPLAN_00001.dcm"
    destination.parent.mkdir(parents=True)
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = generate_uid()
    meta.MediaStorageSOPInstanceUID = generate_uid()
    dataset = FileDataset(str(destination), {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.is_little_endian = True
    dataset.is_implicit_VR = False
    dataset.Modality = "RTPLAN"
    dataset.PatientID = "CACHE-TEST"
    dataset.StudyInstanceUID = generate_uid()
    dataset.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    dataset.RTPlanLabel = "plan-a"
    dataset.save_as(destination)


class CacheDeletionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="plan1699-cache-test-")
        self.root = Path(self.temporary.name) / "case"
        self.root.mkdir()
        write_plan(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_cached_run_is_moved_to_recoverable_trash(self) -> None:
        run = analysis_run_dir(self.root, "test_run", create=True)
        (run / "figures" / "result.txt").write_text("result", encoding="utf-8")
        self.assertEqual(len(discover_cached_runs(self.root)), 1)

        record = trash_cached_run(self.root, run)

        trash = Path(record["trash_directory"])
        self.assertFalse(run.exists())
        self.assertTrue((trash / "figures" / "result.txt").is_file())
        self.assertTrue((trash / "deletion.json").is_file())
        self.assertEqual(discover_cached_runs(self.root), [])

    def test_path_outside_standardized_plan_cache_is_rejected(self) -> None:
        outside = self.root / "analysis" / "run-not-a-plan-cache"
        outside.mkdir(parents=True)
        (outside / "manifest.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "direct standardized run"):
            trash_cached_run(self.root, outside)

    def test_shared_queue_blocks_cache_mutation_for_other_gui_instances(self) -> None:
        original_app_root = web_app.APP_ROOT
        try:
            web_app.APP_ROOT = Path(self.temporary.name) / "app"
            storage = web_app.APP_ROOT / "analysis" / "_batch_queue" / "queue.json"
            storage.parent.mkdir(parents=True)
            storage.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "123456789abc",
                                "case_root": str(self.root),
                                "status": "running",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            self.assertIn("123456789abc", web_app.active_case_work(self.root))
        finally:
            web_app.APP_ROOT = original_app_root


if __name__ == "__main__":
    unittest.main()
