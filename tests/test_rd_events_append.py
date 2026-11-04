"""事件存储：追加写、崩溃尾行容忍、去重索引、归档上限、按派单读取。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from remote_dispatch import rd_events as events


class AppendOnlyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-events-append-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve() / "state"
        self.path = self.state / "events.jsonl"

    def test_append_keeps_inode_and_existing_bytes(self):
        log = events.EventLog(self.state)
        log.append("job.created", dict(job="a", round=1))
        inode, before = self.path.stat().st_ino, self.path.read_bytes()
        log.append("job.state_changed", dict(job="a", round=1, execution_state="running"))
        self.assertEqual(self.path.stat().st_ino, inode)
        self.assertTrue(self.path.read_bytes().startswith(before))
        self.assertEqual([row["seq"] for row in log.read_after(0)], [1, 2])

    def test_append_does_not_read_whole_active_segment(self):
        log = events.EventLog(self.state)
        log.append_batch([("job.created", dict(job=str(number)), False) for number in range(300)])
        real = Path.read_bytes
        sizes = []

        def spy(path):
            data = real(path)
            sizes.append(len(data))
            return data
        with patch.object(Path, "read_bytes", spy):
            log.append("job.created", dict(job="new"))
        self.assertEqual(sizes, [])

    def test_torn_tail_after_crash_is_ignored_and_repaired_by_next_append(self):
        log = events.EventLog(self.state)
        log.append("job.created", dict(job="a", round=1))
        with self.path.open("ab") as stream:
            stream.write(b'{"seq":2,"time":"x","type":"job.created","data":{"job":"b"')
        reader = events.EventCursor(self.state)
        self.assertEqual([row["seq"] for row in reader.read()], [1])
        self.assertEqual([row["seq"] for row in log.read_after(0)], [1])
        # 新进程（无内存索引）与已有索引的进程都要能越过尾行继续写。
        log.append("receipt.ready", dict(job="a", round=1))
        self.assertEqual([row["seq"] for row in log.read_after(0)], [1, 2])
        self.assertTrue(self.path.read_bytes().endswith(b"\n"))
        self.assertEqual([row["seq"] for row in reader.read()], [2])

    def test_failed_write_leaves_no_partial_batch(self):
        log = events.EventLog(self.state)
        log.append("job.created", dict(job="a", round=1))
        before = self.path.read_bytes()

        def partial(path, body):
            with open(path, "ab") as stream:
                stream.write(body[:len(body) // 2])
            raise OSError("disk full")
        with patch.object(events, "write_active", partial), self.assertRaisesRegex(ValueError, "event_write_failed"):
            log.append_batch([("receipt.ready", dict(job="a"), False), ("receipt.acked", dict(job="a"), False)])
        self.assertEqual(self.path.read_bytes(), before)
        log.append("receipt.ready", dict(job="a"))
        # 失败批次的 seq 可能已被读者在 fsync 前读到，不复用：重试从高水位之后编号。
        self.assertEqual([row["seq"] for row in log.read_after(0)], [1, 4])


class UncommittedVisibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-events-visible-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve() / "state"

    def test_seq_seen_by_a_reader_before_failed_fsync_is_never_reused(self):
        log = events.EventLog(self.state)
        cursor = events.EventCursor(self.state)
        log.publish("host.changed", dict(host="a", load1=1))
        self.assertEqual([row["seq"] for row in cursor.read()], [1])
        seen = []

        def bad(fd):
            seen.extend(row["seq"] for row in cursor.read())  # 另一进程的 SSE 在 fsync 前读到
            raise OSError(5, "EIO")
        with patch.object(events.os, "fsync", bad), self.assertRaises(ValueError):
            log.publish("host.changed", dict(host="a", load1=2))
        self.assertEqual(seen, [2])
        # 紧接一条更长的写入，读者的旧偏移会落在行中间：必须容错并拿到新行。
        log.publish("host.changed", dict(host="a", load1=3, ncpu="x" * 200))
        rows = cursor.read()
        self.assertEqual([row["seq"] for row in rows], [3])
        self.assertEqual(rows[0]["data"]["load1"], 3)
        self.assertEqual([row["seq"] for row in log.read_after(0)], [1, 3])

    def test_reader_survives_corrupt_line_in_active_segment(self):
        log = events.EventLog(self.state)
        log.publish("job.created", dict(job="a"))
        cursor = events.EventCursor(self.state)
        self.assertEqual(len(cursor.read()), 1)
        with (self.state / "events.jsonl").open("ab") as stream:
            stream.write(b"garbage-not-json\n")
        self.assertEqual(cursor.read(), [])  # 不抛异常

    def test_index_does_not_keep_retracted_data_as_latest(self):
        log = events.EventLog(self.state)
        rec = dict(job="j", user_id="u", rounds=1, host="h")
        log.observe_job(rec, dict(state="running", round="1"))
        log.warm()

        def bad(fd):
            raise OSError(5, "EIO")
        with patch.object(events.os, "fsync", bad), self.assertRaises(ValueError):
            log.observe_job(rec, dict(state="done", round="1", exit="0"))
        log.observe_job(rec, dict(state="done", round="1", exit="0"))
        states = [row["data"]["execution_state"] for row in log.read_after(0)]
        self.assertEqual(states, ["running", "done"])


class IndexTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-events-index-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name).resolve() / "state"

    def observe(self, log, state="running", **extra):
        rec = dict(job="j", user_id="u", rounds=1, host="h")
        log.observe_job(rec, dict(state=state, round="1"), **extra)

    def test_dedup_sees_other_writers_and_survives_rotation_without_scanning(self):
        log = events.EventLog(self.state, max_bytes=300)
        self.observe(log)
        self.assertEqual(len(log.read_after(0)), 1)
        # 另一个 EventLog 实例（等同另一个进程内的写者）写入后，本实例的去重要能看到。
        other = events.EventLog(self.state, max_bytes=300)
        self.observe(other, "done")
        self.observe(log, "done")
        self.assertEqual([row["data"]["execution_state"] for row in log.read_after(0)], ["running", "done"])
        for number in range(6):
            log.append("job.created", dict(job="filler%d" % number, round=1))
        self.assertGreater(len(log.archives()), 0)
        # 轮转后旧状态仍在归档里，去重不得重复发布。
        self.observe(log, "done")
        self.assertEqual(len([row for row in log.read_after(0) if row["type"] == "job.state_changed"]), 2)

    def test_index_is_rebuilt_when_active_segment_is_replaced(self):
        log = events.EventLog(self.state)
        self.observe(log)
        path = self.state / "events.jsonl"
        data = path.read_bytes()
        path.unlink()
        path.write_bytes(data)  # inode 变化
        self.observe(log)
        self.assertEqual(len(log.read_after(0)), 1)

    def test_archive_total_is_capped_and_cursor_gap_is_reported_by_oldest_seq(self):
        log = events.EventLog(self.state, max_bytes=1, max_archives=2)
        for number in range(8):
            log.append("job.created", dict(job="j%d" % number))
        self.assertEqual(len(log.archives()), 2)
        self.assertGreater(log.oldest_seq(), 1)
        rows = log.read_after(0)
        self.assertEqual(rows[0]["seq"], log.oldest_seq())
        self.assertEqual(rows[-1]["seq"], 8)
        # 游标仍在保留范围内时无缺口。
        self.assertEqual([row["seq"] for row in log.read_after(7)], [8])

    def test_index_drops_entries_of_pruned_segments(self):
        log = events.EventLog(self.state, max_bytes=300, max_archives=1)
        rec = dict(job="old", user_id="u", rounds=1, host="h")
        log.observe_job(rec, dict(state="running", round="1"), dict(phase="p", summary="s", updated_at="t"))
        log.warm()
        self.assertIn("old", log.index().jobs)
        for number in range(30):
            log.append("job.created", dict(job="filler%d" % number))
        self.assertEqual(log.read_job("old"), [])
        index = log.index()
        with index.lock:
            index.sync(log)
            self.assertNotIn("old", index.jobs)
            self.assertNotIn(events.identity("job.state_changed", dict(job="old", user_id="u")), index.latest)

    def test_archive_limit_is_configurable_and_validated(self):
        with patch.dict(os.environ, {"RD_EVENT_LOG_MAX_ARCHIVES": "3"}):
            self.assertEqual(events.EventLog(self.state).max_archives, 3)
        for value in ("0", "-1", "x"):
            with patch.dict(os.environ, {"RD_EVENT_LOG_MAX_ARCHIVES": value}):
                with self.assertRaisesRegex(ValueError, "RD_EVENT_LOG_MAX_ARCHIVES"):
                    events.EventLog(self.state)

    def test_read_job_returns_only_that_job_across_segments_without_full_scan(self):
        log = events.EventLog(self.state, max_bytes=500, max_archives=50)
        for number in range(12):
            for job in ("one", "two"):
                rec = dict(job=job, user_id="u", rounds=1, host="h")
                log.observe_job(rec, dict(state="running", round="1"),
                                dict(phase="p", summary="s%d" % number, updated_at="t%02d" % number))
        self.assertGreater(len(log.archives()), 1)
        fresh = events.EventLog(self.state, max_bytes=500, max_archives=50)
        with patch.object(events.EventLog, "read_after", side_effect=AssertionError("full scan")):
            rows = fresh.read_job("one")
        self.assertEqual([row["data"]["summary"] for row in rows], ["s%d" % number for number in range(12)])
        self.assertTrue(all(row["data"]["job"] == "one" for row in rows))
        self.assertEqual(rows, sorted(rows, key=lambda row: row["seq"]))
        self.assertEqual(fresh.read_job("missing"), [])

    def test_read_job_skips_segments_removed_by_cap(self):
        log = events.EventLog(self.state, max_bytes=400, max_archives=1)
        for number in range(10):
            log.observe_job(dict(job="one", user_id="u", rounds=1, host="h"), dict(state="running", round="1"),
                            dict(phase="p", summary="s%d" % number, updated_at="t%02d" % number))
        rows = log.read_job("one")
        self.assertTrue(rows)
        self.assertLess(len(rows), 10)
        self.assertEqual(rows[-1]["data"]["summary"], "s9")
        self.assertEqual(json.loads(json.dumps(rows)), rows)


if __name__ == "__main__":
    unittest.main()
