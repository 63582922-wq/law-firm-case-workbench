from threading import Event
import unittest

from case_kernel.official_source_capture_supervisor import OfficialCaptureSupervisor


class SupervisorTests(unittest.TestCase):
    def test_runs_one_job_then_stops(self):
        stop = Event(); calls = []
        def run_once():
            calls.append(1); stop.set()
        OfficialCaptureSupervisor(run_once=run_once, stop=stop, interval_seconds=1).run()
        self.assertEqual(calls, [1])
