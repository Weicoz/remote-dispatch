"""POSIX 作业资源上限：运行真实生成脚本，不连接设备或模型。"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from test_rd import rd


@unittest.skipUnless(os.name == "posix", "POSIX runner only")
class RunnerLimitsTests(unittest.TestCase):
    def run_worker(self, soft, hard, expected):
        import resource

        with tempfile.TemporaryDirectory(prefix="rd-limits-") as directory:
            root = Path(directory)
            job = root / "jobs" / "fixture"
            worktree = root / "wt" / "fixture"
            job.mkdir(parents=True)
            worktree.mkdir(parents=True)
            host = dict(rd.DEFAULTS, workdir=str(root))
            rec = dict(job="fixture", sync="direct")
            probe = worktree / "probe.py"
            probe.write_text(
                "import json, os, resource\n"
                "print(json.dumps(resource.getrlimit(resource.RLIMIT_NOFILE)))\n"
                "handles = [open(os.devnull) for _ in range(300)]\n"
                "for handle in handles: handle.close()\n"
            )
            command = f"{shlex.quote(sys.executable)} {shlex.quote(str(probe))}\n"
            (job / "setup.sh").write_text(command)
            (job / "cmd.r1.sh").write_text(command)
            script = job / "run.r1.sh"
            script.write_text(rd.build_run_script(host, rec, 1, 10))

            def set_limits():
                resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))

            process = subprocess.run(
                ["sh", str(script)], capture_output=True, text=True,
                preexec_fn=set_limits, timeout=15,
            )
            self.assertEqual(process.returncode, 0,
                             process.stderr + (job / "setup.log").read_text())
            self.assertEqual((job / "exit_code").read_text().strip(), "0",
                             (job / "setup.log").read_text())
            for name in ("setup.log", "stdout.r1.log"):
                self.assertEqual(json.loads((job / name).read_text()), [expected, hard])

    def test_low_ssh_limit_is_raised_before_setup_and_command(self):
        self.run_worker(256, 32768, 10240)

    def test_existing_higher_soft_limit_is_preserved(self):
        self.run_worker(16384, 32768, 16384)

    def test_lower_hard_limit_is_respected(self):
        self.run_worker(256, 512, 512)


if __name__ == "__main__":
    unittest.main()
