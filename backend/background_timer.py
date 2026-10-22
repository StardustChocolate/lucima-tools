"""Native background timer used by the background restriction test.

The old test measured a browser heartbeat.  That value stops being meaningful
as soon as the WebView is throttled or frozen.  This module keeps the two
clocks in the backend instead:

* ``realElapsedMs`` is the wall clock interval between start and stop.
* ``appElapsedMs`` is progress on the backend's planned time axis.  For an
  explicit task deadline it is capped at that deadline; for an open-ended
  probe it is the sum of completed *planned* ``Event.wait`` intervals.

The latter detail is intentional.  If a process was asleep when a scheduled
11 hour wait became due and the thread was only resumed an hour later, the
wait still contributes 11 hours to the application clock; the extra hour is
visible only in the wall clock.  For an open-ended probe, a late wake only
commits the wait interval that had been scheduled before suspension.
"""

from __future__ import annotations

import threading
import time
from typing import Any


JITTER_SECONDS = 0.1
DEFAULT_TICK_SECONDS = 1.0


def scheduled_elapsed_ms(planned_ms: float, observed_ms: float,
                         jitter_ms: float = JITTER_SECONDS * 1000) -> int:
    """Return the application time represented by one scheduled wait.

    A scheduler wake that is a little early is counted as observed time.  A
    normal wake (including the usual scheduling jitter) counts the planned
    interval exactly.  A wake that is much later still counts only the planned
    interval, so a frozen process cannot manufacture application time.

    This small pure function also makes the 2:00 -> 13:00 -> 14:00 example
    explicit: ``scheduled_elapsed_ms(11h, 12h)`` is 11 hours.
    """
    planned = max(0.0, float(planned_ms))
    observed = max(0.0, float(observed_ms))
    if observed + max(0.0, float(jitter_ms)) < planned:
        return int(round(observed))
    return int(round(planned))


def planned_elapsed_ms(planned_ms: float | None, observed_ms: float) -> int:
    """Return progress on a finite scheduled time axis.

    ``planned_ms`` is the interval from the start to the task's scheduled
    wake.  Once the process is resumed after that wake, progress is capped at
    the scheduled interval, so a task planned for 13:00 and observed at 14:00
    contributes 11 hours to the app clock while wall time contributes 12.
    ``None`` is useful for an open ended test and leaves the caller's observed
    value unchanged.
    """
    observed = max(0.0, float(observed_ms))
    if planned_ms is None:
        return int(round(observed))
    return int(round(min(observed, max(0.0, float(planned_ms)))))


class BackgroundTimer:
    """A restartable, thread-backed pair of application and wall clocks."""

    def __init__(self, tick_seconds: float = DEFAULT_TICK_SECONDS):
        self.tick_seconds = max(0.01, float(tick_seconds))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._planned_duration_ms: int | None = None
        self._reset_locked()

    def _reset_locked(self) -> None:
        self._state = "idle"
        self._started_at_ms: int | None = None
        self._stopped_at_ms: int | None = None
        self._final_real_elapsed_ms = 0
        self._app_elapsed_ms = 0
        self._planned_duration_ms = None

    def _now_ms(self) -> int:
        return int(round(time.time() * 1000))

    def _snapshot_locked(self, captured_at_ms: int | None = None) -> dict[str, Any]:
        captured = self._now_ms() if captured_at_ms is None else int(captured_at_ms)
        if self._started_at_ms is None:
            real_elapsed = 0
        elif self._state == "completed":
            real_elapsed = self._final_real_elapsed_ms
        else:
            # Wall time is deliberately independent of the monotonic wait
            # loop.  Clamp clock corrections so a wall clock adjustment does
            # not expose a negative duration to the UI.
            real_elapsed = max(0, captured - self._started_at_ms)
        return {
            "state": self._state,
            "startedAtMs": self._started_at_ms,
            "stoppedAtMs": self._stopped_at_ms,
            "capturedAtMs": captured,
            "appElapsedMs": int(self._app_elapsed_ms),
            "realElapsedMs": int(real_elapsed),
        }

    def snapshot(self) -> dict[str, Any]:
        """Read the current clocks without advancing either one."""
        with self._lock:
            return self._snapshot_locked()

    def start(self, planned_duration_ms: float | None = None,
              planned_wake_at_ms: float | None = None) -> dict[str, Any]:
        """Start the timer; a repeated call while running is idempotent.

        Starting after a completed run creates a fresh run, which is what the
        UI's "重新开始" action needs.  No account, login, or Android service
        state is consulted.
        """
        with self._lock:
            if self._state == "running":
                return self._snapshot_locked()
            self._reset_locked()
            self._state = "running"
            self._started_at_ms = self._now_ms()
            if planned_wake_at_ms is not None:
                self._planned_duration_ms = max(0, int(round(float(planned_wake_at_ms) - self._started_at_ms)))
            elif planned_duration_ms is not None:
                self._planned_duration_ms = max(0, int(round(float(planned_duration_ms))))
            self._stop = threading.Event()
            stop_event = self._stop
            thread = threading.Thread(target=self._run, name="background-test-clock",
                                       args=(stop_event,), daemon=True)
            self._thread = thread
            thread.start()
            return self._snapshot_locked()

    def stop(self) -> dict[str, Any]:
        """Stop the wait loop and freeze the completed result.

        An in-progress wait is intentionally discarded.  It has not reached
        its scheduled point yet, so counting it would make a short stop look
        like a completed task.  The Event wakes the thread immediately and the
        bounded join keeps API shutdown responsive.
        """
        with self._lock:
            if self._state != "running":
                return self._snapshot_locked()
            stopped = self._now_ms()
            self._stop.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.25, self.tick_seconds + 0.25))
        with self._lock:
            self._state = "completed"
            self._stopped_at_ms = stopped
            self._final_real_elapsed_ms = max(0, stopped - (self._started_at_ms or stopped))
            return self._snapshot_locked(captured_at_ms=self._now_ms())

    def _commit_planned_elapsed(self, monotonic_now: float | None = None) -> None:
        """Commit time on the backend schedule, only from the wait thread."""
        with self._lock:
            if self._state != "running" or self._started_at_ms is None:
                return
            # A finite plan is an absolute wall-clock schedule (the same
            # basis as account task timestamps).  ``time.monotonic`` is kept
            # for measuring an individual Event.wait, but must not be used to
            # calculate progress across Android deep sleep.
            observed_ms = max(0.0, self._now_ms() - self._started_at_ms)
            self._app_elapsed_ms = max(
                self._app_elapsed_ms,
                planned_elapsed_ms(self._planned_duration_ms, observed_ms),
            )

    def _record_wait(self, planned_seconds: float, observed_seconds: float) -> None:
        """Commit one completed backend wait (kept separate for unit tests)."""
        planned_ms = max(0.0, float(planned_seconds)) * 1000
        observed_ms = max(0.0, float(observed_seconds)) * 1000
        increment = scheduled_elapsed_ms(planned_ms, observed_ms)
        with self._lock:
            if self._state == "running":
                self._app_elapsed_ms += increment

    def _run(self, stop_event: threading.Event) -> None:
        # Keep the Event local to this run.  If a caller starts a new run after
        # a stop, the old daemon cannot accidentally observe the replacement
        # Event and continue indefinitely.
        while not stop_event.is_set():
            wait_started = time.monotonic()
            # Event.wait is the same primitive used by account scheduling.
            # It returns immediately when stop() asks the thread to finish.
            if self._planned_duration_ms is None:
                wait_seconds = self.tick_seconds
            else:
                with self._lock:
                    with_started = self._started_at_ms or self._now_ms()
                    elapsed_ms = max(0, self._now_ms() - with_started)
                    remaining_ms = self._planned_duration_ms - elapsed_ms
                if remaining_ms <= 0:
                    self._commit_planned_elapsed(wait_started)
                    return
                # A finite plan represents one real scheduler deadline.  Wait
                # for that deadline in one Event.wait instead of waking every
                # second for hours; stop() can still interrupt it immediately.
                wait_seconds = remaining_ms / 1000
            if stop_event.wait(wait_seconds):
                # stop() can be the first backend code to run after a long
                # suspension.  Let this worker commit the schedule progress
                # before it exits, while still capping it at the plan.
                self._commit_planned_elapsed()
                return
            now = time.monotonic()
            if self._planned_duration_ms is None:
                observed = max(0.0, now - wait_started)
                self._record_wait(wait_seconds, observed)
            else:
                self._commit_planned_elapsed(now)


_TIMER = BackgroundTimer()


def runtime_diagnostics(phase: str | None = None,
                        planned_duration_ms: float | None = None,
                        planned_wake_at_ms: float | None = None) -> dict[str, Any]:
    """Handle the credential-free global diagnostics endpoint."""
    if phase == "start":
        return _TIMER.start(planned_duration_ms, planned_wake_at_ms)
    if phase == "stop":
        return _TIMER.stop()
    return _TIMER.snapshot()


def reset_for_tests() -> None:
    """Reset the process singleton without leaving a daemon test thread."""
    _TIMER.stop()
    with _TIMER._lock:
        _TIMER._reset_locked()
