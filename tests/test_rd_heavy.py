import os
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest

from remote_dispatch import cli as rd


def rd_heavy_path():
    return Path(rd.HERE, "bin")



class HeavyReceiptTests(unittest.TestCase):
    def run_with_dead_holder(self, supervised):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slot = root / "heavy.1"
            slot.mkdir()
            dead = subprocess.Popen(["sh", "-c", "exit 0"])
            dead.wait()
            (slot / "pid").write_text(str(dead.pid))
            if supervised:
                (slot / "process-receipt.json").write_text('{"tree_exit_confirmed":false}')
            script = Path(rd_heavy_path(), "rd-heavy")
            process = subprocess.Popen([str(script), "sh", "-c", 'printf acquired > "$RD_LOCK_DIR/acquired"'],
                env=dict(os.environ, RD_LOCK_DIR=directory, RD_MAX_HEAVY="1"),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                if supervised:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.communicate(timeout=0.3)
                    self.assertTrue((slot / "process-receipt.json").exists())
                    self.assertFalse((root / "acquired").exists())
                else:
                    process.communicate(timeout=3)
                    self.assertEqual(process.returncode, 0)
                    self.assertTrue((root / "acquired").exists())
                    self.assertFalse(slot.exists())
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGTERM)
                    process.communicate(timeout=3)

    def test_dead_wrapper_does_not_reap_supervised_receipt(self):
        self.run_with_dead_holder(True)

    def test_unsupervised_dead_slot_keeps_legacy_recovery(self):
        self.run_with_dead_holder(False)


if __name__ == "__main__":
    unittest.main()
