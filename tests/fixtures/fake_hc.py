"""假 hc（herdr-core CLI）：只回放 FAKE_HC 指向的 JSON 夹具，不接触真实 core。

支持 events --json、ls --json、get <t> --json、read <t> --json；每次调用写入 FAKE_HC_LOG。
夹具字段：
  terminals     快照与 get 使用的终端记录列表
  ls_extra      只被 ls 与 get 看到、不在快照里的终端（测发现）
  seq           快照 seq
  lines         快照之后逐行输出的原始文本
  gate          存在该路径后才输出 lines（让测试控制增量时机）
  hold          true 时输出完 lines 后保持连接，否则立即退出（模拟断流）
  screens       地址 -> read 返回的文本
  unavailable   true 时所有命令以 1 退出（模拟 core 未运行）
"""
import json
import os
import sys
import time
from pathlib import Path

fixture = json.loads(Path(os.environ["FAKE_HC"]).read_text())
args = sys.argv[1:]
with open(os.environ["FAKE_HC_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
if fixture.get("unavailable"):
    sys.exit(1)
known = {row["address"]: row for row in fixture.get("terminals", []) + fixture.get("ls_extra", [])}
if args[:1] == ["events"]:
    print(json.dumps({"channel": None, "seq": fixture.get("seq", 1), "terminals": fixture["terminals"]}), flush=True)
    gate = fixture.get("gate")
    while gate and not Path(gate).exists():
        time.sleep(0.01)
    for line in fixture.get("lines", []):
        print(line, flush=True)
    while fixture.get("hold"):
        time.sleep(0.05)
elif args[:1] == ["ls"]:
    print(json.dumps({"hidden_finished": 0, "machines": [], "terminals": [
        {"address": a, "agent": r.get("foreground", {}).get("name"), "cwd": r.get("cwd"),
         "state": r.get("state", "live"), "turn": 0} for a, r in known.items()]}))
elif args[:1] == ["get"]:
    row = known.get(args[1])
    if row is None:
        print(json.dumps({"error": {"kind": "not_found"}}))
        sys.exit(3)
    print(json.dumps(row))
elif args[:1] == ["read"]:
    if args[1] not in known:
        print(json.dumps({"error": {"kind": "not_found"}}))
        sys.exit(3)
    print(json.dumps({"complete": True, "how": "screen", "text": fixture.get("screens", {}).get(args[1], "")}))
else:
    sys.exit(2)
