from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from gui.batch_queue import BatchQueueManager, DEFAULT_PHASES, POST_TRANSPORT_ACTIONS


APP_ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for queue state")


class QueuePipelineTest(unittest.TestCase):
    """A queued case must run the same sequence the Workflow tab does by hand."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="plan1699-queue-phase-")
        self.base = Path(self.temporary.name)
        self.case = self.base / "case"
        self.case.mkdir()
        self.seen: list[str] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_queue(self, failing: dict[str, str] | None = None, payload_extra=None):
        failing = failing or {}

        def build(action: str, payload: dict):
            self.seen.append(action)
            mode = failing.get(action)
            if mode == "build":
                raise RuntimeError(f"simulated build failure in {action}")
            argv = [sys.executable, "-c", "pass"]
            if mode == "exit":
                argv = [sys.executable, "-c", "import sys; sys.exit(1)"]
            return action, [(action, argv, Path(payload["root"]), None)]

        manager = BatchQueueManager(
            self.base / "queue.json",
            initialize_case=lambda root: None,
            build_action=build,
            can_start=lambda: True,
        )
        payload = {
            "root": str(self.case),
            "histories": 10,
            "threads": 2,
            "output_tag": "tag",
            "beam_model_mode": "baseline",
        }
        payload.update(payload_extra or {})
        manager.enqueue(self.case, payload, label="case", estimate={})
        manager.set_enabled(True)
        wait_for(
            lambda: manager.snapshot()["jobs"][0]["status"]
            in {"completed", "completed_with_warnings", "failed"}
        )
        return manager, manager.snapshot()["jobs"][0]

    def test_default_phases_continue_past_the_transport(self) -> None:
        self.assertEqual(DEFAULT_PHASES, ("pipeline", "preflight", "run_topas", "analyze", "gamma"))
        self.assertEqual(POST_TRANSPORT_ACTIONS, {"analyze", "gamma"})

    def test_a_clean_case_runs_every_phase_and_completes(self) -> None:
        _, job = self.run_queue()
        self.assertEqual(self.seen, list(DEFAULT_PHASES))
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["error"], "")

    def test_a_failed_transport_is_still_a_hard_failure(self) -> None:
        _, job = self.run_queue(failing={"run_topas": "exit"})
        self.assertEqual(job["status"], "failed")
        self.assertNotIn("analyze", self.seen, "analysis must not run on a lost transport")
        self.assertNotIn("gamma", self.seen)

    def test_post_transport_failures_degrade_instead_of_discarding_the_dose(self) -> None:
        _, job = self.run_queue(failing={"analyze": "exit", "gamma": "build"})
        # Both post-transport stages are attempted even though the first failed.
        self.assertEqual(self.seen, list(DEFAULT_PHASES))
        self.assertEqual(job["status"], "completed_with_warnings")
        self.assertIn("analyze", job["error"])
        self.assertIn("gamma", job["error"])

    def test_completed_with_warnings_can_be_retried(self) -> None:
        manager, job = self.run_queue(failing={"gamma": "build"})
        self.assertEqual(job["status"], "completed_with_warnings")
        manager.set_enabled(False)
        manager.control(job["id"], "retry")
        self.assertEqual(manager.snapshot()["jobs"][0]["status"], "queued")

    def test_foreign_case_paths_in_the_snapshot_are_cleared(self) -> None:
        # The queue snapshots the Workflow form, which points at whichever case
        # the operator had open. Analysing another patient's binary would be far
        # worse than skipping the analysis.
        other = self.base / "other-case" / "topas_output" / "production" / "dose.bin"
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b"\x00")
        seen_payloads: list[dict] = []

        def build(action: str, payload: dict):
            seen_payloads.append(dict(payload))
            return action, [(action, [sys.executable, "-c", "pass"], self.case, None)]

        manager = BatchQueueManager(
            self.base / "queue.json",
            initialize_case=lambda root: None,
            build_action=build,
            can_start=lambda: True,
        )
        manager.enqueue(
            self.case,
            {
                "root": str(self.case),
                "histories": 10,
                "threads": 2,
                "output_tag": "tag",
                "beam_model_mode": "baseline",
                "mc_binary": str(other),
                "tps_dose_uid": "1.2.3.from.another.case",
            },
            label="case",
            estimate={},
        )
        manager.set_enabled(True)
        wait_for(
            lambda: manager.snapshot()["jobs"][0]["status"]
            in {"completed", "completed_with_warnings", "failed"}
        )
        self.assertTrue(seen_payloads)
        for payload in seen_payloads:
            self.assertEqual(payload["mc_binary"], "")
            self.assertEqual(payload["tps_dose_uid"], "")


class CasePackageShadowingTest(unittest.TestCase):
    """An empty `gui/` in a case root shadowed the real `gui` package.

    `scripts/14_calibrate_mc_dose.py` put the case root on sys.path ahead of the
    app root, so `gui` resolved to the case's empty namespace package and the
    import of `gui.case_results` died after the transport had already finished.
    """

    def test_case_scaffold_does_not_create_a_gui_package(self) -> None:
        source = (APP_ROOT / "scripts" / "10_initialize_case.py").read_text(encoding="utf-8")
        directories = source.split("DIRECTORIES = (", 1)[1].split(")", 1)[0]
        self.assertNotIn('"gui"', directories)

    def test_no_case_root_shadows_the_gui_package(self) -> None:
        strays = [
            path
            for path in APP_ROOT.glob("*/**/gui")
            if path.is_dir() and ".venv" not in path.parts and path.parent != APP_ROOT
        ]
        self.assertEqual(strays, [], f"these directories shadow the gui package: {strays}")

    def test_calibration_script_imports_from_the_app_root(self) -> None:
        source = (APP_ROOT / "scripts" / "14_calibrate_mc_dose.py").read_text(encoding="utf-8")
        self.assertIn("APP_ROOT = Path(__file__).resolve().parents[1]", source)
        self.assertNotIn("sys.path.insert(0, str(root))", source)

    def test_calibration_script_runs_with_a_case_root_argument(self) -> None:
        # Importing is the part that broke; --help exercises it without needing
        # a dose binary.
        result = subprocess.run(
            [sys.executable, str(APP_ROOT / "scripts" / "14_calibrate_mc_dose.py"), "--help"],
            capture_output=True,
            text=True,
            cwd=tempfile.gettempdir(),
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    sys.exit(unittest.main())
