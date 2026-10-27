import json
import math
import sys
import time
from contextlib import contextmanager


class StartupTiming:
    # Only fixed phase names and durations may cross the diagnostic boundary.
    PHASES = frozenset({
        "instructions_load", "profile_prepare", "runtime_factory",
        "runtime_enter", "workspace_status", "account", "thread_resume", "thread_start",
        "instruction_refresh", "turn_start", "uncached_to_turn_started",
    })

    def __init__(self):
        self.durations = {}
        self.started = self._now()
        self.emitted = False

    @staticmethod
    def _now():
        try:
            return time.perf_counter()
        except Exception:
            return None

    def _record(self, phase, started):
        try:
            duration = (self._now() - started) * 1000
            if phase in self.PHASES and math.isfinite(duration) and duration >= 0:
                self.durations[phase] = round(duration, 3)
        except Exception:
            pass

    @contextmanager
    def phase(self, phase):
        started = self._now()
        try:
            yield
        finally:
            self._record(phase, started)

    def turn_started(self):
        self._record("uncached_to_turn_started", self.started)
        self.emit()

    def emit(self):
        if self.emitted:
            return
        self.emitted = True
        try:
            print(json.dumps({"startup_timing_ms": self.durations}, allow_nan=False), file=sys.stderr, flush=True)
        except Exception:
            # Diagnostics must never replace a reply or the original failure.
            pass
