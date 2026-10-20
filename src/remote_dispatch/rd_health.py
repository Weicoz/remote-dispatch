"""可随请求传到 worker 的只读网络探针与有界日志摘要（不输出凭据或正文）。"""
import concurrent.futures
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit


def positive_seconds(value, label):
    try:
        seconds = float(value)
        if math.isfinite(seconds) and seconds > 0:
            return seconds
    except (ValueError, TypeError):
        pass
    raise ValueError(label + " 必须是大于零的有限秒数。")


def https_url(value):
    """不允许通过探针发送 URL 凭据、查询参数或非 HTTPS 请求。"""
    try:
        url = urlsplit(value)
        if url.scheme == "wss":
            url = url._replace(scheme="https")
        if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        _ = url.port
        return urlunsplit(url)
    except ValueError:
        raise ValueError("网络探测地址必须是无凭据、查询参数及片段的 HTTPS/WSS URL。") from None


def network_options(host):
    options = {key: os.environ.get("RD_" + key.upper()) or host.get(key, "")
               for key in ("network_urls", "network_model_url", "network_websocket_url", "network_timeout_seconds")}
    options["network_timeout_seconds"] = positive_seconds(options["network_timeout_seconds"] or 5,
                                                         "RD_NETWORK_TIMEOUT_SECONDS / network_timeout_seconds")
    for key in ("network_model_url", "network_websocket_url"):
        if options[key]:
            options[key] = https_url(options[key])
    options["network_urls"] = [https_url(v) for v in re.split(r"[,\s]+", options["network_urls"].strip()) if v]
    return options


def model_url(root, uid, version=None):
    """只读取该员工已同步配置中的活动 provider，不读取宿主个人配置或认证文件。"""
    directory = Path(root)
    if uid:
        directory = directory / "users" / uid
    current = directory / "profiles/current.json"
    if version or current.exists():
        try:
            import tomllib
            version = version or json.loads(current.read_text(encoding="utf-8"))["fingerprint"]
            if not re.fullmatch(r"[a-f0-9]{64}", version):
                raise ValueError()
            config = tomllib.loads((directory / "profiles" / version / "config.toml").read_text(encoding="utf-8"))
            provider = config.get("model_provider", "openai")
            base = config.get("model_providers", {}).get(provider, {}).get("base_url")
            if base:
                return https_url(base)
            if provider != "openai":
                raise ValueError()
        except (OSError, ValueError, KeyError, TypeError, ImportError):
            raise ValueError("无法识别员工模型服务地址；请设置 RD_NETWORK_MODEL_URL 或 network_model_url。") from None
    return https_url(os.environ.get("OPENAI_BASE_URL") or "https://chatgpt.com")


def network_probe(options, root, uid="", agent="codex"):
    started = time.monotonic()
    budget = options["network_timeout_seconds"]
    try:
        model = options["network_model_url"] or model_url(root, uid, options.get("profile_fingerprint"))
        # 列表可覆盖通用探测地址；模型端点始终是必需项。
        urls = options["network_urls"] or ["https://pypi.org"]
        parsed = urlsplit(model)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        targets = [(url, False) for url in dict.fromkeys([*urls, origin])]
        if agent == "codex":
            ws = options["network_websocket_url"] or (origin.rstrip("/") + "/backend-api/codex/responses"
                    if parsed.hostname == "chatgpt.com" else model.rstrip("/") + ("" if model.rstrip("/").endswith("/responses") else "/responses"))
            targets.append((ws, True))
    except ValueError:
        return {"ok": False, "checks": [], "error": "无法识别模型 HTTPS 地址；配置 network_model_url。"}

    def check(target):
        url, websocket = target
        seconds = max(0.01, budget - (time.monotonic() - started))
        cmd = ["curl", "--disable", "--silent", "--show-error", "--output", os.devnull,
               "--max-time", str(seconds), "--connect-timeout", str(seconds),
               "--proto", "=https", "--write-out", "%{http_code}"]
        if websocket:
            cmd += ["--http1.1", "--header", "Connection: Upgrade", "--header", "Upgrade: websocket",
                    "--header", "Sec-WebSocket-Version: 13", "--header", "Sec-WebSocket-Key: cmQtbmV0d29yay1wcm9iZQ=="]
        try:
            result = subprocess.run([*cmd, url], capture_output=True, text=True, timeout=seconds + 0.2)
            status = result.stdout.strip()
            ok = (result.returncode == 0 and status.isdigit() and status != "000") or (websocket and status == "101" and result.returncode == 28)
            # HTTP 401/403 等仍证明 HTTPS/TLS 可达；不尝试使用模型凭据。
            error = "" if ok else {35: "TLS handshake failed", 60: "TLS certificate failed", 28: "timeout", 5: "proxy DNS failed", 6: "DNS failed", 7: "connect failed"}.get(result.returncode, "HTTPS probe failed")
        except FileNotFoundError:
            ok, status, error = False, "", "curl unavailable"
        except (OSError, subprocess.TimeoutExpired):
            ok, status, error = False, "", "probe timeout or tool failed"
        return {"target": url, "kind": "websocket_tls" if websocket else "https", "ok": ok,
                "http_status": status if status.isdigit() else "", "error": error}

    # 所有地址共享预算，并行执行；不随地址数串行累计超时。
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(targets)) as executor:
        checks = list(executor.map(check, targets))
    return {"ok": all(item["ok"] for item in checks), "checks": checks,
            "elapsed_seconds": round(time.monotonic() - started, 2)}


def reconnect_line(line):
    value = line.strip()
    try:
        event = json.loads(value)
        if not isinstance(event, dict) or event.get("type") != "error":
            return False
        value = event.get("message", "")
    except ValueError:
        pass
    return isinstance(value, str) and bool(re.match(r"^(?:\[[^\]\n]+\]\s*)?Reconnecting\b.*waiting for network\b", value, re.I))


def log_sample(directory, number):
    """每个流最多读 64KiB；仅回传偏移量和分类，不回传日志内容。"""
    streams = {}
    for name in ("stdout", "stderr"):
        path = Path(directory) / (name + ".r" + str(number) + ".log")
        try:
            with path.open("rb") as stream:
                size = stream.seek(0, 2)
                base = max(0, size - 65536)
                stream.seek(base)
                data = stream.read(65536)
            if base:
                skipped, sep, data = data.partition(b"\n")
                base += len(skipped) + len(sep)
            offset, progress, reconnect = base, 0, 0
            for line in data.splitlines(keepends=True):
                offset += len(line)
                if line.strip():
                    if reconnect_line(line.decode("utf-8", errors="replace")):
                        reconnect = offset
                    else:
                        progress = offset
            streams[name] = dict(size=size, base=base, progress=progress, reconnect=reconnect)
        except FileNotFoundError:
            streams[name] = dict(size=0, base=0, progress=0, reconnect=0)
    return streams
