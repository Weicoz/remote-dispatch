"""定时进度摘要接缝：reports.tick 驱动，真实快照文件、真实汇报存储与事件。"""
import unittest

from remote_dispatch import cli
from remote_dispatch import rd_events as events
from remote_dispatch.rd_serve import Service
from tests import test_rd_reports as base


class DigestTests(unittest.TestCase):
    setUp = base.ReportTests.setUp
    agent = base.ReportTests.agent
    save_fixture = base.ReportTests.save_fixture
    saved_session = base.ReportTests.saved_session
    transcript = base.ReportTests.transcript
    assert_structure = base.ReportTests.assert_structure
    service = base.ReportTests.service
    job = base.ReportTests.job
    reports = base.ReportTests.reports

    def running(self, name, summary, owner="session-one"):
        rec = self.job(name, initiator_session=owner, title="任务" + name)
        cli.messages.atomic_json(self.state / "serve-snapshots" / (name + ".json"), dict(
            user_id="fixture", generation=[1, "target", None],
            snapshot=dict(state="running", round="1", snapshot_at=cli.messages.utc_now()),
            progress=dict(summary=summary, phase="build")))
        return rec

    def digests(self, service, owner="session-one"):
        status, body = service.read("/v1/reports?initiator_session=" + owner)
        self.assertEqual(status, 200)
        return [r for r in body["reports"] if r.get("kind") == "digest"]

    def test_change_pushes_and_unchanged_does_not(self):
        self.running("a", "编译中")
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=800)
        self.assertEqual(self.digests(service), [])
        service.reports.tick(now=901)
        first = self.digests(service)
        self.assertEqual(len(first), 1)
        self.assertEqual([e["job"] for e in first[0]["entries"]], ["a"])
        self.assertIn("编译中", first[0]["entries"][0]["summary"])
        service.reports.tick(now=1900)
        self.assertEqual(len(self.digests(service)), 1)
        service.close()
        self.running("a", "测试中")  # serve 的进度缓存在内存里，换新进度需重启
        service = self.service()
        service.reports.tick(now=2900)
        self.assertEqual(len(self.digests(service)), 2)
        created = [e for e in events.read_after(self.state, 0) if e["type"] == "report.created"]
        self.assertEqual(len(created), 2)

    def test_restart_does_not_repeat_last_digest(self):
        self.running("a", "编译中")
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=901)
        service.close()
        service = self.service()
        service.reports.tick(now=1900)
        service.reports.tick(now=2900)
        self.assertEqual(len(self.digests(service)), 1)

    def test_each_initiator_session_gets_its_own_digest(self):
        self.running("a", "甲进展", "session-one")
        self.running("b", "乙进展", "session-two")
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=901)
        one, two = self.digests(service), self.digests(service, "session-two")
        self.assertEqual(([e["job"] for e in one[0]["entries"]], [e["job"] for e in two[0]["entries"]]),
                         (["a"], ["b"]))

    def test_features_switch_turns_digest_off(self):
        self.config.write_text("[features]\nprogress_digest=false\n")
        self.running("a", "编译中")
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=901)
        self.assertEqual(self.digests(service), [])

    def test_interval_follows_report_interval_seconds(self):
        self.config.write_text("[monitor]\nreport_interval_seconds=60\n")
        self.running("a", "编译中")
        service = self.service()
        service.reports.tick(now=0)
        service.reports.tick(now=61)
        self.assertEqual(len(self.digests(service)), 1)


if __name__ == "__main__":
    unittest.main()
