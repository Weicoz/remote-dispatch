"""serve 看板接口：与 CLI board --json 输出相同，ADR 0004 下不要求原发起会话。"""
import json
import os
import unittest
from unittest.mock import patch

from tests import test_rd_serve as support


class ServeBoardTests(unittest.TestCase):
    setUp = support.ServeTests.setUp
    seed = support.ServeTests.seed
    start = support.ServeTests.start
    request = support.ServeTests.request
    eventually = support.ServeTests.eventually
    command = support.ServeTests.command

    def cli_board(self, *argv):
        result = self.command("board", "--json", *argv)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_board_endpoint_matches_cli_json_for_all_and_session_scope(self):
        self.seed(poll_interval=3600)
        self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                        lambda rows: any(r["job"] == "fixture-job" and r["execution_state"] == "done" for r in rows))
        plan = self.command("plan", "--project", "demo", "add", "later", "--title", "后续")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        status, served = self.request("/v1/board?all=1")
        self.assertEqual(status, 200)
        self.assertEqual(served, self.cli_board("--all"))
        self.assertEqual([row["job"] for row in served["rows"]], ["fixture-job"])
        self.assertEqual((served["summary"]["dispatched"], served["summary"]["pending"]), (1, 1))
        status, scoped = self.request("/v1/board?initiator_session=original-session")
        self.assertEqual(scoped, self.cli_board("--initiator-session", "original-session"))
        self.assertEqual(self.request("/v1/board?initiator_session=nobody")[1]["rows"], [])

    def test_board_endpoint_uses_configured_stale_days_like_cli(self):
        with patch.dict(os.environ, {"RD_LINE_STALE_DAYS": "0"}):
            self.seed(poll_interval=3600)
            self.eventually(lambda: self.request("/v1/jobs")[1]["jobs"],
                            lambda rows: any(r["job"] == "fixture-job" and r["execution_state"] == "done" for r in rows))
            plan = self.command("plan", "--project", "demo", "add", "later", "--title", "后续", "--after", "fixture-job")
            self.assertEqual(plan.returncode, 0, plan.stderr)
            _, served = self.request("/v1/board?all=1")
            self.assertEqual(served, self.cli_board("--all"))
            self.assertTrue(served["rows"][0].get("stale"), served["rows"][0])

    def test_board_endpoint_requires_a_scope(self):
        self.seed(poll_interval=3600)
        self.assertEqual(self.request("/v1/board")[0], 400)


if __name__ == "__main__":
    unittest.main()
