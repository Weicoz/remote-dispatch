elif a[:3] == ["terminal", "session", "control"]:
    import base64, time
    def record(value):
        with open(os.environ["FAKE_WRITES"], "a") as f:
            f.write(json.dumps(value) + "\n")
    cols, rows = int(a[a.index("--cols") + 1]), int(a[a.index("--rows") + 1])
    record(dict(start=True, cols=cols, rows=rows, takeover="--takeover" in a))
    if v.get("control_pid"):
        Path(v["control_pid"]).write_text(str(os.getpid()))
    if v.get("control_busy") and "--takeover" not in a:
        print(json.dumps(dict(type="terminal.closed", reason="already has an attached client; retry with --takeover")), flush=True)
        sys.exit(0)
    frames = v.get("control_frames", [dict(type="terminal.frame", seq=1, width=cols, height=rows,
        full=True, bytes=base64.b64encode(b"\x1b[2Jhello\x1b[3;5H\x1b[?25h").decode())])
    for frame in frames:
        print(json.dumps(frame), flush=True)
    if v.get("flood"):
        # A finite burst can block stdout; after it drains the controller must
        # still consume resize/release. Infinite output cannot acknowledge them.
        for _ in range(512):
            print(json.dumps(frames[0]), flush=True)
    prefix = b""
    if v.get("control_gate"):
        prefix = os.read(sys.stdin.fileno(), 4096)
        record(dict(partial_input_bytes=len(prefix)))
        while not Path(v["control_gate"]).exists():
            time.sleep(0.01)
    import itertools
    lines = sys.stdin.buffer
    if prefix:
        lines = itertools.chain([prefix + lines.readline()], lines)
    for line in lines:
        command = json.loads(line)
        record(command)
        if command["type"] == "terminal.release":
            break
