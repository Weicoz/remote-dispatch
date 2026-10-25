"""Recorded target RPC boundary shared by serve and its isolated CLI children."""
import json
import os
from pathlib import Path

from remote_dispatch import cli


def request(host, operation, data=None, **kwargs):
    path = Path(os.environ["RD_TEST_TARGET"])
    saved = json.loads(path.read_text())
    if operation == saved.get("fail"):
        raise ValueError("fixture target unavailable")
    if operation == "state":
        return {job: saved["states"].get(job, dict(state="missing")) for job in data["jobs"]}
    if operation == "probe":
        return saved.get("probe", dict(memfree="90", disk="100", ncpu="8", load1="1"))
    if operation == "report-read":
        if data["kind"] != "result":
            return None
        return dict(schema=1, job=data["job"], round=data["round"], kind="result",
                    outcome=saved.get("outcome", "completed"), checks="passed", summary="完成",
                    updated_at="2026-10-25T00:00:00+00:00")
    if operation == "sessions":
        return '{"session_id":"receiver"}'
    if operation == "resume-checkpoint":
        return dict(state=saved["states"][data["job"]])
    if operation == "resume-rollback":
        saved["states"][data["job"]] = data["checkpoint"]["state"]
    elif operation == "launch":
        spec = data["spec"]
        saved["states"][spec["job"]] = dict(state="running", round=str(spec["round"]))
        saved.setdefault("launches", []).append(dict(job=spec["job"], round=spec["round"],
                                                    prompt=data["prompt"], model=spec.get("model")))
    elif operation == "stop":
        saved["states"][data["job"]].update(state="done", exit="143")
    elif operation in ("upload", "controller-register"):
        return None
    elif operation == "init":
        return dict(root=host["workdir"])
    elif operation == "profile-read":
        return dict(root=host["workdir"], manifest=saved["profile"])
    elif operation == "doctor":
        return dict(ok=True, features=True)
    else:
        raise AssertionError(operation)
    cli.messages.atomic_json(path, saved, limit=None)


if __name__ == "__main__":
    cli.native.request = request
    from remote_dispatch.rd_serve import execute_cli
    execute_cli()
