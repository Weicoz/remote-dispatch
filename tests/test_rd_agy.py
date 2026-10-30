"""AGY 派单：登记、命令构建、隔离 HOME、终态解析与本机端到端。用假 agy，不调用模型。"""
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from remote_dispatch import cli as rd
from remote_dispatch import rd_agents as agents
from remote_dispatch import rd_messages as messages
from remote_dispatch import rd_profiles as profiles


def agy():
    return dict(agents.AGENTS["agy"], kind="agy")


class AgentRegistryTests(unittest.TestCase):
    def test_only_isolated_agents_are_dispatchable(self):
        for name in ("codex", "dsh", "agy"):
            self.assertIn("platforms", agents.spec(name))
        for name in ("kimi", "claude", "nope"):
            with self.assertRaises(ValueError):
                agents.spec(name)

    def test_agy_requires_explicit_permission(self):
        with self.assertRaises(ValueError):
            agents.command(agy(), None, None)
        with self.assertRaises(ValueError):
            agents.command(agy(), None, None, permission="yolo")
        command = agents.command(agy(), None, None, permission="skip")
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertIn("--mode=plan", agents.command(agy(), None, None, permission="plan"))

    def test_permission_mode_is_rejected_for_profile_agents(self):
        with self.assertRaises(ValueError):
            agents.command(dict(agents.AGENTS["codex"], kind="codex"), None, None, permission="skip")

    def test_start_resume_model_and_effort(self):
        start = agents.command(agy(), "gemini-3.8-flash-high", "max", permission="skip")
        self.assertIn("--model gemini-3.8-flash-high", start)
        self.assertIn("--effort max", start)
        self.assertIn("--input-format stream-json --output-format stream-json", start)
        self.assertNotIn("--conversation", start)
        resume = agents.command(agy(), None, None, session="conv-1", permission="skip")
        self.assertIn("--conversation conv-1", resume)

    def test_session_regex_reads_conversation_id(self):
        import re
        regex = agents.AGENTS["agy"]["session_regex"]
        self.assertEqual(re.search(regex, '{"event":"init","conversation_id":"abc-1"}').group(1), "abc-1")
        self.assertIsNone(re.search(regex, '{"result":{"conversation_id":""}}'))

    def test_prompt_is_encoded_without_shell_interpolation(self):
        with tempfile.TemporaryDirectory(prefix="rd-agy-prompt-") as temporary:
            root = Path(temporary)
            prompt = root / "prompt.md"
            text = '中文 "引号" \'单引号\' $(echo pwned) `id`\n第二行\n'
            prompt.write_text(text, encoding="utf-8")
            fake = root / "agy"
            fake.write_text('#!/bin/sh\ncat\n')
            fake.chmod(0o755)
            command = agents.command(agy(), None, None, permission="skip")
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", RD_PROMPT=str(prompt))
            out = subprocess.run(["sh", "-c", command], capture_output=True, text=True, env=env, check=True).stdout
        message = json.loads(out)
        self.assertEqual(message, {"event": "user", "message": {"role": "user", "content": text}})


class AgyRuntimeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-agy-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.job = self.root / "jobs" / "employee_a-demo"
        self.job.mkdir(parents=True)
        self.token = self.root / "token"
        self.token.write_text("fixture-token", encoding="utf-8")

    def test_prepare_copies_token_into_isolated_home(self):
        home = profiles.prepare_agy_runtime(str(self.job), str(self.token))
        self.assertEqual(home, (self.job / "agy-home").resolve())
        self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        copied = home / profiles.AGY_TOKEN
        self.assertEqual(copied.read_text(encoding="utf-8"), "fixture-token")
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o600)
        self.assertTrue((home / "rd-isolation.json").exists())

    def test_resume_keeps_task_token_unless_refresh_requested(self):
        home = profiles.prepare_agy_runtime(str(self.job), str(self.token))
        self.token.write_text("refreshed", encoding="utf-8")
        profiles.prepare_agy_runtime(str(self.job), str(self.token))
        self.assertEqual((home / profiles.AGY_TOKEN).read_text(encoding="utf-8"), "fixture-token")
        profiles.prepare_agy_runtime(str(self.job), "")  # 续跑不再要求认证文件
        profiles.prepare_agy_runtime(str(self.job), str(self.token), refresh=True)
        copied = home / profiles.AGY_TOKEN
        self.assertEqual(copied.read_text(encoding="utf-8"), "refreshed")
        self.assertEqual(stat.S_IMODE(copied.stat().st_mode), 0o600)

    def test_refresh_without_auth_file_is_refused_and_keeps_old_token(self):
        home = profiles.prepare_agy_runtime(str(self.job), str(self.token))
        with self.assertRaises(ValueError):
            profiles.prepare_agy_runtime(str(self.job), str(self.root / "absent"), refresh=True)
        self.assertEqual((home / profiles.AGY_TOKEN).read_text(encoding="utf-8"), "fixture-token")

    def test_missing_token_and_unmarked_home_are_refused(self):
        with self.assertRaises(ValueError):
            profiles.prepare_agy_runtime(str(self.job), str(self.root / "absent"))
        with self.assertRaises(ValueError):
            profiles.prepare_agy_runtime(str(self.job), "")
        (self.job / "agy-home").mkdir()
        with self.assertRaises(ValueError):
            profiles.prepare_agy_runtime(str(self.job), str(self.token))

    def test_environment_is_whitelisted_and_keeps_git_config(self):
        real_home = self.root / "real-home"
        real_home.mkdir()
        (real_home / ".gitconfig").write_text("[user]\n\tname = X\n", encoding="utf-8")
        home = profiles.prepare_agy_runtime(str(self.job), str(self.token))
        env = profiles.agy_environment(home, {"HOME": str(real_home), "PATH": "/usr/bin", "RD_JOB": "j",
                                              "HTTPS_PROXY": "http://127.0.0.1:7890", "AWS_SECRET_ACCESS_KEY": "nope",
                                              "CODEX_HOME": "/x/.codex", "GEMINI_API_KEY": "nope"})
        self.assertEqual(env["HOME"], str(home))
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:7890")
        self.assertEqual(env["RD_JOB"], "j")
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], str(real_home / ".gitconfig"))
        for key in ("AWS_SECRET_ACCESS_KEY", "CODEX_HOME", "GEMINI_API_KEY"):
            self.assertNotIn(key, env)

    def test_dsh_environment_keeps_proxy(self):
        home = profiles.prepare_dsh_runtime(str(self.job), str(self.token))
        env = profiles.dsh_environment(home, {"PATH": "/usr/bin", "ALL_PROXY": "socks5://h:1", "no_proxy": "localhost"})
        self.assertEqual(env["ALL_PROXY"], "socks5://h:1")
        self.assertEqual(env["no_proxy"], "localhost")


class AgyFinishTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-agy-finish-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()

    def stdout(self, result, extra=()):
        rows = [{"event": "init", "conversation_id": "c1", "init": {}}, *extra, {"event": "result", "result": result}]
        (self.root / "stdout.r1.log").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        (self.root / "stderr.r1.log").write_text("", encoding="utf-8")

    def test_success_writes_last_reply(self):
        self.stdout({"conversation_id": "c1", "status": "SUCCESS", "response": "完成\n", "num_turns": 2})
        data = messages.finish(self.root, "job-a", 1, 0, agent="agy")
        self.assertEqual(data["outcome"], "completed")
        self.assertEqual((self.root / "last.r1.txt").read_text(encoding="utf-8"), "完成\n")
        self.assertEqual(data["summary"], "完成\n")

    def test_denied_actions_downgrade_to_partial(self):
        self.stdout({"conversation_id": "c1", "status": "SUCCESS", "response": "", "num_turns": 1,
                     "denied_actions": [{"action": "command", "display_name": "RunCommand"}]})
        data = messages.finish(self.root, "job-a", 1, 0, agent="agy")
        self.assertEqual(data["outcome"], "partial")
        self.assertEqual(data["denied_actions"], ["command"])
        self.assertEqual(messages.result_summary({"report": data})["denied_actions"], ["command"])

    def test_authentication_failure_is_classified(self):
        self.stdout({"conversation_id": "", "status": "ERROR", "response": "", "num_turns": 0,
                     "error": "authentication failed or timed out"})
        data = messages.finish(self.root, "job-a", 1, 1, agent="agy")
        self.assertEqual(data["outcome"], "failed")
        self.assertEqual(data["runtime_failure"]["kind"], "authentication")
        self.assertIs(data["runtime_failure"]["work_observed"], False)

    def test_receipt_wrapping_a_full_report_fits(self):
        path = self.root / "receipt.json"
        receipt = {"report": {"summary": "x" * (messages.MAX_BYTES - 200)}, "job": "j"}
        messages.atomic_json(path, receipt, limit=messages.RECEIPT_BYTES)
        self.assertEqual(messages.read_json(path), receipt)


class AgyPreflightTests(unittest.TestCase):
    def host(self, **overrides):
        value = dict(rd.DEFAULTS, name="fixture", ssh="u@h", workdir="~/rd", shared_workdir="~/rd")
        value.update(overrides)
        return value

    def preflight(self, host, results):
        calls = []

        def fake_ssh(h, script, **kwargs):
            calls.append(script)
            return subprocess.CompletedProcess(["sh"], results.pop(0), "", "")
        with patch.object(rd, "ssh", side_effect=fake_ssh), contextlib.redirect_stderr(io.StringIO()):
            return rd.agent_preflight(host, "employee_a", "agy", agy()), calls

    def test_windows_target_is_rejected_before_any_remote_call(self):
        with self.assertRaises(SystemExit):
            self.preflight(self.host(platform="windows"), [])

    def test_missing_cli_or_login_stops_dispatch(self):
        with self.assertRaises(SystemExit):
            self.preflight(self.host(), [1])
        with self.assertRaises(SystemExit):
            self.preflight(self.host(), [0, 1])
        with self.assertRaises(SystemExit):
            self.preflight(self.host(agy_auth_file=""), [])

    def test_ready_target_passes_and_checks_token_on_worker(self):
        profile, calls = self.preflight(self.host(), [0, 0])
        self.assertIsNone(profile)
        self.assertIn("--input-format", calls[0])
        self.assertIn('"$HOME"/.gemini/antigravity-cli/antigravity-oauth-token', calls[1])

    def test_dsh_accepts_absolute_credentials(self):
        host = self.host(dsh_auth_file="/srv/rd/dsh.yaml")
        with patch.object(rd, "ssh", return_value=subprocess.CompletedProcess(["sh"], 0, "", "")):
            self.assertIsNone(rd.agent_preflight(host, "employee_a", "dsh", dict(agents.AGENTS["dsh"], kind="dsh")))

    def test_doctor_reports_agy_install_and_login_separately(self):
        captured = {}

        def fake_ssh(host, script, **kwargs):
            captured["script"] = script
            return subprocess.CompletedProcess(["sh"], 0, "", "")
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "config.ini"
            config.write_text("[host:self]\nssh = local\nworkdir = ~/rd-test\n", encoding="utf-8")
            info = dict(reachable=True, memfree="50", disk="100", load1="0", ncpu="1", running="0", heavy="0")
            with patch.object(rd, "CONFIG", str(config)), patch.object(rd, "ssh", side_effect=fake_ssh), \
                 patch.object(rd, "probe", return_value=info), contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaises(SystemExit):
                rd.cmd_doctor(argparse.Namespace(host="self", agent=["agy"], dir=None, top_dir=None), rd.Config())
        self.assertIn("AGY print/stream-json runtime", captured["script"])
        self.assertIn("AGY 登录凭据", captured["script"])


FAKE_AGY = r"""#!/bin/sh
if [ "$1" = --help ]; then echo "  --input-format"; exit 0; fi
printf '%s\n' "$@" > "$RD_JOB_DIR/agy-args.r$RD_ROUND"
printf '%s' "$HOME" > "$RD_JOB_DIR/agy-home.r$RD_ROUND"
cat > "$RD_JOB_DIR/agy-stdin.r$RD_ROUND"
echo "round $RD_ROUND" >> agy-work.txt
git add agy-work.txt && git commit -qm "agy round $RD_ROUND"
printf '%s\n' '{"event":"init","conversation_id":"conv-fixture","init":{}}'
printf '%s\n' '{"event":"result","result":{"conversation_id":"conv-fixture","status":"SUCCESS","response":"done\n","num_turns":1}}'
"""


class AgyLocalEndToEndTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rd-agy-e2e-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (("init", "-q"), ("config", "user.name", "Fixture"), ("config", "user.email", "fixture@example.invalid")):
            self.git(*args)
        (self.repo / "tracked.txt").write_text("base\n")
        self.git("add", ".")
        self.git("commit", "-qm", "fixture base")
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "agy").write_text(FAKE_AGY)
        (bin_dir / "agy").chmod(0o755)
        token = self.root / "token"
        token.write_text("fixture-token")
        self.prompt = self.root / "prompt.md"
        self.prompt.write_text("改一下\n", encoding="utf-8")
        config = self.root / "config.ini"
        config.write_text(
            f"[host:self]\nssh = local\nworkdir = {self.root / 'worker'}\nsync = direct\n"
            f"path = {bin_dir}:{os.environ['PATH']}\ntimeout = 30\nagy_auth_file = {token}\n", encoding="utf-8")
        # 任务内 git 提交需要身份；隔离 HOME 后由 GIT_CONFIG_GLOBAL 指回这里。
        fake_home = self.root / "home"
        fake_home.mkdir()
        (fake_home / ".gitconfig").write_text("[user]\n\tname = Worker\n\temail = worker@example.invalid\n")
        for mock in (patch.object(rd, "CONFIG", str(config)), patch.object(rd, "STATE", str(self.root / "state")),
                     patch.dict(os.environ, {"RD_USER_ID": "employee_a", "HOME": str(fake_home)}),
                     contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO())):
            mock.__enter__()
            self.addCleanup(mock.__exit__, None, None, None)
        self.cfg = rd.Config()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def wait_done(self, rec, round_no):
        deadline = time.time() + 30
        while time.time() < deadline:
            state = rd.job_states(self.cfg, [rec])[rec["job"]]
            if state.get("state") == "done" and state.get("round") == str(round_no):
                return state
            time.sleep(0.2)
        self.fail("AGY 本机派单未在 30 秒内结束")

    def test_run_result_and_resume_same_conversation(self):
        args = argparse.Namespace(dir=str(self.repo), agent="agy", prompt_file=str(self.prompt), cmd=None, test=False,
                                  heavy=False, host="self", force_host=True, tag=[], ref="HEAD", wip=False, branch=None,
                                  name="agy", project=None, include=[], model=None, effort=None, timeout=20,
                                  initiator_session=None, workdir=None, permission_mode="skip")
        rd.cmd_run(args, self.cfg)
        rec = rd.all_jobs()[0]
        self.assertEqual(rec["permission_mode"], "skip")
        state = self.wait_done(rec, 1)
        self.assertEqual(state["exit"], "0")
        job_dir = Path(rec["worker_root"], "jobs", rec["job"])
        self.assertEqual((job_dir / "agy-home.r1").read_text(), str(job_dir / "agy-home"))
        self.assertIn("--dangerously-skip-permissions", (job_dir / "agy-args.r1").read_text())
        stdin = json.loads((job_dir / "agy-stdin.r1").read_text())
        self.assertTrue(stdin["message"]["content"].startswith("改一下\n"))
        self.assertEqual((job_dir / "last.r1.txt").read_text(), "done\n")
        self.assertNotEqual(state["head"], rec["base"])
        receipt = rd.collect_result(self.cfg, rec, state)
        self.assertEqual(receipt["receiver_session"], "conv-fixture")
        self.assertEqual(receipt["report"]["outcome"], "completed")

        rd.cmd_resume(argparse.Namespace(job=rec["job"], prompt_file=str(self.prompt), session=None, model=None,
                                         effort=None, timeout=20, force_host=True, initiator_session=None,
                                         new_context=False, retry_after_repair=None, permission_mode=None), self.cfg)
        rec = rd.load_job(rec["job"])
        self.wait_done(rec, 2)
        resumed = (job_dir / "agy-args.r2").read_text().split("\n")
        self.assertIn("--conversation", resumed)
        self.assertEqual(resumed[resumed.index("--conversation") + 1], "conv-fixture")
        self.assertIn("--dangerously-skip-permissions", resumed)


if __name__ == "__main__":
    unittest.main()
