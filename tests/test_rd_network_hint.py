"""派单前网络探针拒绝与失败回执提示：network_unreachable / auth_failed。"""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from remote_dispatch import rd_messages as messages
from remote_dispatch import rd_query as query
from test_rd import rd
from test_rd_artifact_fingerprint import Fixture


def row(kind, message):
    return json.dumps({"type": kind, "message": message})


def info(network_ok=True, error="TLS handshake failed"):
    check = {"kind": "websocket_tls", "target": "https://chatgpt.com/backend-api/codex/responses",
             "ok": network_ok, "error": "" if network_ok else error}
    return {"reachable": True, "load1": "0.1", "ncpu": "8", "memfree": "80", "disk": "100", "running": "0",
            "network": {"ok": network_ok, "checks": [check]}}


class NetworkGateTests(Fixture):
    def setUp(self):
        super().setUp()
        self.second_host()

    def pick(self, probes, name="auto", **kwargs):
        with patch.object(rd, "probe", side_effect=lambda host: probes[host["name"]]), \
             contextlib.redirect_stdout(io.StringIO()):
            return rd.pick_hosts(self.t.cfg, name, [], **kwargs)

    def test_explicit_host_with_failing_network_is_refused_with_reason_and_advice(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as stop:
            self.pick({"fixture": info(False), "fixture2": info()}, name="fixture")
        self.assertEqual(stop.exception.code, 3)
        text = errors.getvalue()
        self.assertIn("TLS 握手失败", text)
        self.assertIn("换主机", text)
        self.assertIn("--force-host", text)

    def test_timeout_and_dns_reasons_are_named(self):
        for error, chinese in (("timeout", "超时"), ("DNS failed", "DNS 解析失败")):
            with self.subTest(error=error), contextlib.redirect_stderr(io.StringIO()) as errors, \
                    self.assertRaises(SystemExit):
                self.pick({"fixture": info(False, error), "fixture2": info(False, error)})
            self.assertIn(chinese, errors.getvalue())

    def test_auto_skips_the_host_with_failing_network(self):
        chosen = self.pick({"fixture": info(False), "fixture2": info()})
        self.assertEqual([h["name"] for h in chosen], ["fixture2"])

    def test_force_host_skips_the_probe_entirely(self):
        with patch.object(rd, "probe") as probe, patch.object(rd, "pick_host") as pick:
            rd.cmd_run(self.t.args(force_host=True, host="fixture"), self.t.cfg)
        probe.assert_not_called()
        pick.assert_not_called()

    def test_non_network_refusal_has_no_network_advice(self):
        busy = dict(info(), load1="99")
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit):
            self.pick({"fixture": busy, "fixture2": busy})
        self.assertNotIn("网络不可达", errors.getvalue())


class FailureHintTests(unittest.TestCase):
    def test_workspace_routing_failure_is_network(self):
        text = row("error", "workspace routing discovery failed: error sending request")
        self.assertEqual(messages.failure_hint("codex", text, 1)[0], "network_unreachable")

    def test_tls_and_connection_errors_in_turn_failed_are_network(self):
        text = json.dumps({"type": "turn.failed", "error": {"message": "TLS handshake failed"}})
        self.assertEqual(messages.failure_hint("codex", text, 1)[0], "network_unreachable")
        self.assertIsNone(messages.failure_hint("codex", "boot\nERROR: connection timed out\n", 1))  # 纯文本不标

    def test_authentication_failure_wins_over_network_wording(self):
        text = row("error", "401 Unauthorized while sending request; error sending request")
        self.assertEqual(messages.failure_hint("codex", text, 1)[0], "auth_failed")

    def test_unknown_failures_success_and_agent_chatter_are_not_marked(self):
        self.assertIsNone(messages.failure_hint("codex", row("error", "disk quota exceeded"), 1))
        self.assertIsNone(messages.failure_hint("codex", row("error", "workspace routing discovery failed"), 0))
        chatter = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "tls handshake docs"}})
        self.assertIsNone(messages.failure_hint("codex", chatter, 1))
        self.assertIsNone(messages.failure_hint(None, row("error", "tls handshake"), 1))

    def test_transient_reconnect_followed_by_other_final_failure_is_not_network(self):
        text = "\n".join([row("error", "Reconnecting... 1/5 (error sending request) waiting for network"),
                          json.dumps({"type": "turn.failed", "error": {"message": "context window exceeded"}})])
        self.assertIsNone(query.Query.failure_hint_for("codex", text, 1))

    def test_only_the_last_failure_event_decides(self):
        old = row("error", "tls handshake failed")
        final = json.dumps({"type": "turn.failed", "error": {"message": "disk quota exceeded"}})
        self.assertIsNone(query.Query.failure_hint_for("codex", old + "\n" + final, 1))
        later = row("error", "workspace routing discovery failed")
        self.assertEqual(query.Query.failure_hint_for("codex", final + "\n" + later, 1)[0], "network_unreachable")

    def test_reconnect_lines_are_ignored_even_when_last(self):
        text = row("error", "workspace routing discovery failed") + "\n" + \
            row("error", "Reconnecting... 2/5 (error sending request) waiting for network")
        self.assertEqual(query.Query.failure_hint_for("codex", text, 1)[0], "network_unreachable")

    def test_error_followed_by_progress_events_is_ignored(self):
        text = "\n".join([row("error", "Reconnecting... 1/5 (error sending request for url)"),
                          json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}})])
        self.assertIsNone(query.Query.failure_hint_for("codex", text, 124))
        text = row("error", "workspace routing discovery failed") + "\n" + json.dumps({"type": "turn.completed"})
        self.assertIsNone(query.Query.failure_hint_for("codex", text, 1))

    def test_reconnecting_n_of_m_wording_is_not_a_failure(self):
        for message in ("Reconnecting... 1/5 (stream disconnected: error sending request)", "[x] Reconnecting… 2/5 (tls handshake)"):
            self.assertIsNone(query.Query.failure_hint_for("codex", row("error", message), 1), message)

    def test_plain_text_is_not_used_except_strong_dsh_error_lines(self):
        self.assertIsNone(messages.failure_hint("agy", "connection refused on 5432\n", 1))
        self.assertIsNone(messages.failure_hint("dsh", "talking about tls handshake in docs\n", 1))
        self.assertEqual(messages.failure_hint("dsh", "start\nError: tls handshake failed\n", 1)[0], "network_unreachable")

    def test_credential_detection_is_unchanged(self):
        self.assertTrue(messages.credential_suspect("codex", row("error", "401 Unauthorized"), 1))
        self.assertFalse(messages.credential_suspect("codex", row("error", "401 Unauthorized"), 0))


class ReceiptHintTests(Fixture):
    def receipt(self, text, code="1", agent="codex"):
        q = query.Query(rd.STATE, states=lambda recs: {}, report=lambda *a: None, receiver=lambda rec: None,
                        output=lambda rec, number: text)
        rec = dict(job="job-a", host="h", rounds=1, user_id="u", agent=agent, dispatch_state="started")
        return q.result(rec, 1, {"state": "done", "round": "1", "exit": code})

    def test_receipt_carries_hint_and_summary_shows_it(self):
        receipt = self.receipt(row("error", "workspace routing discovery failed"))
        self.assertEqual(receipt["failure_hint"], "network_unreachable")
        self.assertIn("网络", receipt["failure_note"])
        summary = messages.result_summary(dict(receipt, delivery_state="pending"))
        self.assertEqual(summary["failure_hint"], "network_unreachable")

    def test_auth_receipt_has_both_credential_and_failure_hint(self):
        receipt = self.receipt(row("error", "401 Unauthorized"))
        self.assertTrue(receipt["credential_suspect"])
        self.assertEqual(receipt["failure_hint"], "auth_failed")

    def test_unrecognised_or_successful_runs_have_no_hint(self):
        self.assertNotIn("failure_hint", self.receipt(row("error", "something odd")))
        self.assertNotIn("failure_hint", self.receipt(row("error", "workspace routing discovery failed"), code="0"))


if __name__ == "__main__":
    unittest.main()
