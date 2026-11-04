import threading
import unittest
from unittest.mock import patch

from remote_dispatch import cli as rd


class JobStatesParallelTest(unittest.TestCase):
    def test_hosts_are_queried_concurrently(self):
        recs = [dict(job="a", host="h1"), dict(job="b", host="h2"), dict(job="c", host="h1")]
        barrier = threading.Barrier(2, timeout=5)
        calls = []

        def fake_remote_state(host, jobs):
            calls.append((host["name"], tuple(jobs)))
            barrier.wait()  # 串行执行时第一台会在这里超时
            return {job: dict(state="running") for job in jobs}

        with patch.object(rd, "job_host", side_effect=lambda cfg, rec: dict(name=rec["host"])), \
                patch.object(rd, "remote_state", side_effect=fake_remote_state):
            states = rd.job_states(None, recs)
        self.assertEqual(set(states), {"a", "b", "c"})
        self.assertEqual(sorted(calls), [("h1", ("a", "c")), ("h2", ("b",))])


if __name__ == "__main__":
    unittest.main()
