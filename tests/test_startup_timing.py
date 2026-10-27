import io
import json
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

from src.agent.startup_timing import StartupTiming


class StartupTimingTests(unittest.TestCase):
    def test_monotonic_durations_end_at_native_start_and_emit_once(self):
        output = io.StringIO()
        with patch("src.agent.startup_timing.time.perf_counter", side_effect=[10, 11, 13, 15]), redirect_stderr(output):
            timing = StartupTiming()
            with timing.phase("runtime_factory"):
                pass
            timing.turn_started()
            timing.emit()
        self.assertEqual(json.loads(output.getvalue()), {"startup_timing_ms": {
            "runtime_factory": 2000, "uncached_to_turn_started": 5000,
        }})
        self.assertEqual(len(output.getvalue().splitlines()), 1)

    def test_failed_phase_preserves_original_exception_and_has_no_success_total(self):
        output = io.StringIO()
        failure = RuntimeError("synthetic-private-failure")
        with redirect_stderr(output):
            timing = StartupTiming()
            with self.assertRaises(RuntimeError) as caught:
                with timing.phase("thread_resume"):
                    raise failure
            timing.emit()
        self.assertIs(caught.exception, failure)
        values = json.loads(output.getvalue())["startup_timing_ms"]
        self.assertEqual(set(values), {"thread_resume"})
        self.assertGreaterEqual(values["thread_resume"], 0)
        self.assertNotIn("synthetic-private", output.getvalue())

    def test_unknown_names_clock_and_output_failures_are_nonfatal(self):
        output = io.StringIO()
        with redirect_stderr(output):
            timing = StartupTiming()
            with timing.phase("synthetic-private-phase"):
                pass
            timing.emit()
        self.assertEqual(json.loads(output.getvalue()), {"startup_timing_ms": {}})
        with patch("src.agent.startup_timing.time.perf_counter", side_effect=RuntimeError("private-clock")):
            timing = StartupTiming()
            with timing.phase("turn_start"):
                pass
            with patch("src.agent.startup_timing.print", side_effect=OSError("private-output")):
                timing.turn_started()
        self.assertEqual(timing.durations, {})

    def test_nonfinite_durations_are_omitted(self):
        with patch("src.agent.startup_timing.time.perf_counter", side_effect=[0, 1, float("nan")]):
            timing = StartupTiming()
            with timing.phase("account"):
                pass
        self.assertEqual(timing.durations, {})
