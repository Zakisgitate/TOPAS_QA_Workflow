from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import time
import unittest

from gui.batch_queue import BatchQueueManager


def wait_for(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for queue state")


class BatchQueueManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="plan1699-queue-test-")
        self.base = Path(self.temporary.name)
        self.storage = self.base / "queue.json"
        self.cases = [self.base / f"case-{index}" for index in range(4)]
        for case in self.cases:
            case.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def initialize_case(root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def payload(root: Path, tag: str) -> dict:
        return {
            "root": str(root),
            "histories": 1000,
            "threads": 2,
            "output_tag": tag,
            "beam_model_mode": "baseline",
        }

    def test_two_slots_auto_advance_and_persist(self) -> None:
        def build(action: str, payload: dict):
            root = Path(payload["root"])
            return action, [
                (
                    action,
                    [sys.executable, "-c", "import time; time.sleep(0.08)"],
                    root,
                    None,
                )
            ]

        manager = BatchQueueManager(
            self.storage,
            initialize_case=self.initialize_case,
            build_action=build,
            can_start=lambda: True,
        )
        manager.set_max_parallel(2)
        for index, root in enumerate(self.cases):
            manager.enqueue(root, self.payload(root, f"run_{index}"), label=root.name)
        manager.set_enabled(True)
        maximum_active = 0

        def complete() -> bool:
            nonlocal maximum_active
            maximum_active = max(maximum_active, manager.active_count())
            return all(job["status"] == "completed" for job in manager.snapshot()["jobs"])

        wait_for(complete)
        self.assertEqual(maximum_active, 2)
        self.assertTrue(all(Path(job["log_path"]).is_file() for job in manager.snapshot()["jobs"]))

        reloaded = BatchQueueManager(
            self.storage,
            initialize_case=self.initialize_case,
            build_action=build,
            can_start=lambda: True,
        )
        self.assertFalse(reloaded.snapshot()["enabled"])
        self.assertEqual([job["status"] for job in reloaded.snapshot()["jobs"]], ["completed"] * 4)

    def test_pause_resume_and_failure_retry(self) -> None:
        should_fail = {"value": True}

        def build(action: str, payload: dict):
            root = Path(payload["root"])
            if action == "pipeline" and should_fail["value"]:
                return action, [(action, [sys.executable, "-c", "raise SystemExit(3)"], root, None)]
            return action, [
                (
                    action,
                    [sys.executable, "-c", "import time; time.sleep(0.12)"],
                    root,
                    None,
                )
            ]

        manager = BatchQueueManager(
            self.storage,
            initialize_case=self.initialize_case,
            build_action=build,
            can_start=lambda: True,
        )
        job = manager.enqueue(
            self.cases[0], self.payload(self.cases[0], "retry"), label="retry"
        )
        manager.set_enabled(True)
        wait_for(lambda: manager.snapshot()["jobs"][0]["status"] == "failed")
        should_fail["value"] = False
        manager.control(job["id"], "retry")
        wait_for(
            lambda: manager.snapshot()["jobs"][0]["status"] == "running"
            and manager.runtimes[job["id"]].process_group_id is not None
        )
        manager.control(job["id"], "pause")
        self.assertEqual(manager.snapshot()["jobs"][0]["status"], "paused")
        time.sleep(0.08)
        manager.control(job["id"], "resume")
        wait_for(lambda: manager.snapshot()["jobs"][0]["status"] == "completed")
        self.assertEqual(manager.snapshot()["jobs"][0]["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
