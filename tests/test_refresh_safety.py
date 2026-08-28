from __future__ import annotations

import json
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import urlopen

from gui import web_app


APP_ROOT = Path(__file__).resolve().parents[1]


def wait_for(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for task state")


class RefreshSafetyTest(unittest.TestCase):
    """Reloading the page must never interrupt a running calculation.

    The task is a daemon thread plus a detached process group inside the server
    process; nothing about it is owned by the browser connection. These tests
    hold that property in place.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="plan1699-refresh-test-")
        self.root = Path(self.temporary.name)
        self.previous_state = web_app.STATE
        self.previous_batch = web_app.BATCH
        web_app.STATE = web_app.WorkflowState(self.root)
        web_app.BATCH = None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), web_app.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        try:
            if web_app.STATE.running:
                try:
                    web_app.stop_active_task()
                except RuntimeError:
                    pass
                wait_for(lambda: not web_app.STATE.running, timeout=10.0)
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5.0)
            web_app.STATE = self.previous_state
            web_app.BATCH = self.previous_batch
            self.temporary.cleanup()

    def get(self, path: str) -> bytes:
        with urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as response:
            self.assertEqual(response.status, 200)
            return response.read()

    def start_long_task(self, seconds: float = 6.0, form_state: dict | None = None) -> None:
        command = (
            "long task",
            [sys.executable, "-c", f"import time; time.sleep({seconds})"],
            self.root,
            None,
        )
        web_app.start_commands("long task", [command], self.root, None, form_state)
        wait_for(lambda: web_app.STATE.process is not None)

    def test_page_reloads_do_not_interrupt_a_running_task(self) -> None:
        self.start_long_task()
        pid = web_app.STATE.process.pid
        group_id = web_app.STATE.process_group_id

        for _ in range(5):
            body = self.get("/")
            self.assertIn(b"runLockNote", body)
            snapshot = json.loads(self.get("/api/log?after=0"))
            self.assertTrue(snapshot["running"])

        self.assertTrue(web_app.STATE.running)
        self.assertIsNotNone(web_app.STATE.process)
        self.assertEqual(web_app.STATE.process.pid, pid)
        self.assertEqual(web_app.STATE.process_group_id, group_id)
        self.assertIsNone(web_app.STATE.process.poll(), "the child process was terminated")

    def test_reload_replays_the_whole_log_from_cursor_zero(self) -> None:
        self.start_long_task()
        web_app.STATE.append("marker-line\n")
        first = json.loads(self.get("/api/log?after=0"))
        self.assertIn("marker-line\n", first["lines"])
        # A reloaded page starts at cursor 0 again and must still see everything.
        second = json.loads(self.get("/api/log?after=0"))
        self.assertIn("marker-line\n", second["lines"])
        self.assertGreaterEqual(second["cursor"], first["cursor"])

    def test_snapshot_carries_the_form_state_for_reattachment(self) -> None:
        self.start_long_task(
            form_state=web_app.gui_form_state(
                {
                    "histories": "1000000",
                    "threads": "5",
                    "seed": "4242",
                    "output_tag": "full_plan_1000000",
                    "beam_model_mode": "commissioned",
                    "beam_override_enabled": False,
                    "energy_layer_indices": "11,22,47",
                    "action": "run_topas",
                }
            )
        )
        state = json.loads(self.get("/api/log?after=0"))["form_state"]
        self.assertEqual(state["histories"], "1000000")
        self.assertEqual(state["seed"], "4242")
        self.assertEqual(state["beam_model_mode"], "commissioned")
        self.assertEqual(state["beam_override_enabled"], "false")
        self.assertEqual(state["energy_layer_indices"], "11,22,47")
        self.assertNotIn("action", state, "only browser form fields are snapshotted")

    def test_form_state_records_the_clamped_thread_count(self) -> None:
        from gui.runtime_monitor import logical_cpu_count

        state = web_app.gui_form_state({"histories": "100000", "threads": "64"})
        self.assertEqual(state["threads"], str(min(64, logical_cpu_count())))

    def test_a_second_action_is_refused_while_a_task_runs(self) -> None:
        self.start_long_task()
        with self.assertRaises(RuntimeError):
            web_app.start_commands("second task", [], self.root)


if __name__ == "__main__":
    sys.exit(unittest.main())
