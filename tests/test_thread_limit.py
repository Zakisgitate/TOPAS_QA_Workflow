from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest

from gui.batch_queue import BatchQueueManager
from gui.runtime_monitor import clamp_threads, logical_cpu_count
from gui.web_app import HTML, resolve_threads


APP_ROOT = Path(__file__).resolve().parents[1]


class ThreadLimitTest(unittest.TestCase):
    """Optimization item 1: Geant4 workers never exceed the local core count."""

    def test_clamp_converges_on_logical_cpus(self) -> None:
        limit = logical_cpu_count()
        self.assertGreaterEqual(limit, 1)
        value, note = clamp_threads(limit)
        self.assertEqual(value, limit)
        self.assertEqual(note, "")
        value, note = clamp_threads(limit + 1)
        self.assertEqual(value, limit)
        self.assertIn(str(limit), note)
        value, note = clamp_threads(64 if limit < 64 else limit * 4)
        self.assertEqual(value, limit)
        self.assertTrue(note)

    def test_clamp_accepts_strings_and_rejects_non_positive(self) -> None:
        limit = logical_cpu_count()
        self.assertEqual(clamp_threads("64")[0], min(64, limit))
        self.assertEqual(clamp_threads(0)[0], 1)
        self.assertEqual(clamp_threads(-5)[0], 1)

    def test_payload_resolver_reports_requested_and_effective(self) -> None:
        limit = logical_cpu_count()
        effective, requested, note = resolve_threads({"threads": str(limit + 49)})
        self.assertEqual(effective, limit)
        self.assertEqual(requested, limit + 49)
        self.assertTrue(note)
        effective, requested, note = resolve_threads({"threads": "1"})
        self.assertEqual((effective, requested, note), (1, 1, ""))

    def test_page_does_not_hardcode_a_thread_ceiling(self) -> None:
        self.assertNotIn('max="64"', HTML)
        self.assertIn('max="__MAX_THREADS__"', HTML)

    def test_prepare_script_writes_the_clamped_thread_count(self) -> None:
        source = (APP_ROOT / "scripts" / "09_prepare_topas_run.py").read_text(encoding="utf-8")
        self.assertIn("i:Ts/NumberOfThreads = {threads}", source)
        self.assertNotIn("i:Ts/NumberOfThreads = {args.threads}", source)

    def test_queue_job_records_the_effective_thread_count(self) -> None:
        limit = logical_cpu_count()
        with tempfile.TemporaryDirectory(prefix="plan1699-threads-test-") as temporary:
            base = Path(temporary)
            case = base / "case"
            case.mkdir()
            manager = BatchQueueManager(
                base / "queue.json",
                build_action=lambda action, payload: (action, []),
                initialize_case=lambda root: None,
                can_start=lambda: False,
            )
            job = manager.enqueue(
                case,
                {
                    "root": str(case),
                    "histories": 1000,
                    "threads": limit,
                    "requested_threads": limit + 49,
                    "output_tag": "thread_cap",
                    "beam_model_mode": "baseline",
                },
                label="thread cap",
                estimate={},
            )
            self.assertEqual(job["threads"], limit)
            self.assertEqual(job["requested_threads"], limit + 49)


if __name__ == "__main__":
    sys.exit(unittest.main())
