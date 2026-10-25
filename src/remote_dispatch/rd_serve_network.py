"""HTTPS/device boundary for serve; local administration stays on its Unix socket."""
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from time import monotonic
from urllib.parse import urlsplit

from . import rd_events as events
from . import rd_messages as messages
from .rd_lock import JobLock
from .rd_push import PushDispatcher
from .rd_idempotency import fingerprint


class NetworkLock(JobLock):
    def __init__(self, state):
        super().__init__(state, "network")
        self.path = Path(state) / "locks" / "network" / "state.lock"

    def acquire(self):
        return super().acquire(blocking=True)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def error(status, reason):
    return status, {"error": {"reason": reason}}


class Network:
    def __init__(self, service, *, push_sender=None):
        self.service = service
        self.path = service.state / "network" / "devices.json"
        self.lock = threading.RLock()
        self.window = None
        self.servers = []
        self.connections = {}
        self.fingerprint = None
        self.candidates = []
        self.listen_addresses = self.configured_listeners()
        self.push = PushDispatcher(service.state, service.user_id, push_sender,
                                   stop=service.stop, is_active=self.push_active) if push_sender is not None else None
        self.push_thread = None

    def configured_listeners(self):
        data = self.read()
        rows = data.get("listen_addresses")
        if rows is None:
            try:
                rows = json.loads(self.service.config.cp.get("serve", "listen_addresses", fallback="[]"))
            except ValueError:
                raise ValueError("invalid_listen_address: select explicit LAN or VPN IP addresses and valid ports") from None
        return self.validate_listeners(rows)

    @staticmethod
    def validate_listeners(rows):
        try:
            if not isinstance(rows, list):
                raise ValueError()
            result, seen = [], set()
            allowed = [ipaddress.ip_network(value) for value in (
                "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                "100.64.0.0/10", "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10")]
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("address"), str)
                        or type(row.get("enabled", True)) is not bool):
                    raise ValueError()
                address = ipaddress.ip_address(row["address"])
                port = row.get("port", 8443)
                if (not any(address in net for net in allowed) or address.is_unspecified
                        or type(port) is not int or not 0 <= port <= 65535):
                    raise ValueError()
                item = str(address), port
                if item in seen:
                    raise ValueError()
                seen.add(item)
                result.append(dict(address=str(address), port=port, enabled=row.get("enabled", True)))
            return result
        except (ValueError, TypeError, KeyError):
            raise ValueError("invalid_listen_address: select explicit LAN or VPN IP addresses and valid ports") from None

    def certificate(self):
        directory = events.physical(self.service.state / "network")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        cert, key = directory / "server.pem", directory / "server.key"
        events.physical(cert)
        events.physical(key)
        if cert.exists() != key.exists():
            raise ValueError("certificate_incomplete: restore the existing certificate and key")
        try:
            if not cert.exists():
                with tempfile.TemporaryDirectory(prefix=".tls-", dir=directory) as staging:
                    output = Path(staging)
                    subprocess.run(["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt",
                        "ec_paramgen_curve:prime256v1", "-nodes", "-days", "3650", "-subj", "/CN=rdispatch",
                        "-out", str(output / "cert"), "-keyout", str(output / "key")],
                        check=True, capture_output=True)
                    for source, target in ((output / "key", key), (output / "cert", cert)):
                        os.chmod(source, 0o600)
                        os.replace(source, target)
            public = subprocess.run(["openssl", "x509", "-in", str(cert), "-pubkey", "-noout"],
                                    check=True, capture_output=True).stdout
            spki = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "DER"], input=public,
                                  check=True, capture_output=True).stdout
            self.fingerprint = hashlib.sha256(spki).hexdigest()
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(cert, key)
            return context
        except (OSError, subprocess.SubprocessError) as exc:
            raise ValueError("certificate_unavailable: openssl and a valid certificate/key are required") from exc

    def bind_listeners(self, rows, handler):
        selected, created, candidates = [], [], []
        try:
            enabled = [row for row in rows if row["enabled"]]
            context = self.certificate() if enabled else None
            existing = {server.listen_address: (server, thread) for server, thread in self.servers}
            for row in enabled:
                address, port = row["address"], row["port"]
                current = existing.get((address, port))
                if current is None:
                    server_type = type("SelectedAddressServer", (TLSServer,),
                                       {"address_family": socket.AF_INET6 if ":" in address else socket.AF_INET})
                    server = server_type((address, port), handler, bind_and_activate=False)
                    created.append((server, None))
                    server.service, server.network, server.context = self.service, self, context
                    cp = self.service.config.cp
                    server.handshake_timeout = positive_setting(cp, "tls_handshake_timeout", 5)
                    server.request_timeout = positive_setting(cp, "request_timeout", 15)
                    server.slots = threading.BoundedSemaphore(int(positive_setting(cp, "max_connections", 32)))
                    server.listen_address = address, port
                    server.server_bind()
                    server.server_activate()
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    created[-1] = current = server, thread
                selected.append(current)
                candidates.append(dict(address=address, port=current[0].server_address[1]))
            return selected, created, candidates
        except BaseException:
            self.close_listeners(created)
            raise

    @staticmethod
    def close_listeners(servers):
        for server, thread in servers:
            if thread is not None:
                server.shutdown()
                thread.join()
            server.server_close()

    def start(self, handler):
        # Unix-only installations remain usable without openssl. HTTPS and pairing
        # always require the persistent certificate; no ephemeral pin is issued.
        if shutil.which("openssl"):
            self.certificate()
        # Scrub caches written by older versions before serving any request.
        with self.lock, NetworkLock(self.service.state):
            if self.path.exists():
                messages.atomic_json(self.path, self.read(), limit=None)
        self.handler = handler
        self.servers, _, self.candidates = self.bind_listeners(self.listen_addresses, handler)
        log = events.EventLog(self.service.state)
        data = dict(user_id=self.service.user_id, spki_sha256=self.fingerprint, candidates=self.candidates)
        if self.candidates or log.latest("network.addresses_changed", data) is not None:
            log.publish("network.addresses_changed", data, only_if_changed=True)
        if self.push is not None:
            self.push_thread = threading.Thread(target=self.push_loop, daemon=True)
            self.push_thread.start()

    def write_listeners(self, data, identity, body):
        try:
            rows = self.validate_listeners(body.get("listen_addresses"))
        except ValueError:
            reply = error(400, "invalid_listen_address")
            data["requests"][identity]["reply"] = reply
            messages.atomic_json(self.path, data, limit=None)
            return reply
        try:
            selected, created, candidates = self.bind_listeners(rows, self.handler)
        except OSError:
            reply = error(409, "listen_address_unavailable")
            data["requests"][identity]["reply"] = reply
            messages.atomic_json(self.path, data, limit=None)
            return reply
        reply = 200, dict(spki_sha256=self.fingerprint, candidates=candidates, listen_addresses=rows)
        data["listen_addresses"] = rows
        data["requests"][identity]["reply"] = reply
        try:
            with events.write_transaction([self.path]):
                messages.atomic_json(self.path, data, limit=None)
                if candidates != self.candidates:
                    events.emit(self.service.state, "network.addresses_changed", dict(
                        user_id=self.service.user_id, spki_sha256=self.fingerprint, candidates=candidates))
        except BaseException:
            self.close_listeners(created)
            raise
        retired = [item for item in self.servers if item not in selected]
        self.servers, self.candidates, self.listen_addresses = selected, candidates, rows
        self.close_listeners(retired)
        return reply

    def push_loop(self):
        reader = events.EventCursor(self.service.state)
        pending = None
        delay = 0.1
        while not self.service.stop.is_set():
            try:
                if pending is None:
                    pending = reader.read()
                with self.lock:
                    devices = list(self.read()["devices"].values())
                self.push.dispatch([row for row in pending if self.service.visible(row)], devices, now=time.time())
                pending = None
                delay = 0.1
            except Exception:
                # Keep the pending batch in memory; never reread history on retries.
                if not self.service.stop.is_set():
                    print("rdispatch: push_unavailable: cannot deliver notifications", file=sys.stderr)
                delay = min(delay * 2, 30.0)
            self.service.stop.wait(delay)

    def push_active(self, device):
        # Recheck immediately before a send without holding the lock during I/O.
        # A delivery already in flight may finish; revocation never waits for it.
        with self.lock:
            current = self.read()["devices"].get(device["device_id"])
            return bool(current and not current["revoked"] and current.get("push_token") == device.get("push_token"))

    def info(self):
        with self.lock:
            return dict(spki_sha256=self.fingerprint, candidates=list(self.candidates),
                        listen_addresses=list(self.listen_addresses))

    def track(self, connection, device_id=None):
        with self.lock:
            if self.service.stop.is_set():
                return False
            self.connections[connection] = device_id
            return True

    def untrack(self, connection):
        with self.lock:
            self.connections.pop(connection, None)

    def authorize(self, handler):
        if not secrets.compare_digest(handler.headers.get("X-Rdispatch-SPKI", "").encode("utf-8"), (self.fingerprint or "").encode("ascii")):
            return error(403, "fingerprint_mismatch")
        if urlsplit(handler.path).path == "/v1/pair":
            return None
        token = handler.headers.get("Authorization", "")
        with self.lock:
            for row in self.read()["devices"].values():
                if (token.startswith("Bearer ") and not row["revoked"]
                        and secrets.compare_digest(row["token_hash"], digest(token[7:]))):
                    handler.device_id = row["device_id"]
                    handler.session_access_enabled = row["session_access_enabled"]
                    handler.session_device = dict(id=row["device_id"], session_access=row["session_access_enabled"])
                    self.connections[handler.connection] = row["device_id"]
                    return None
        return error(401, "device_unauthorized")

    def read(self):
        try:
            data = json.loads(events.physical(self.path).read_text(encoding="utf-8"))
            for identity, entry in data["requests"].items():
                if not isinstance(entry, dict):
                    # Legacy caches have no request binding and may contain secrets.
                    data["requests"][identity] = dict(fingerprint=None, reply=error(403, "pairing_closed"))
            return data
        except FileNotFoundError:
            return {"devices": {}, "requests": {}}

    def devices(self):
        with self.lock:
            data = self.read()
            return {"devices": [{key: row[key] for key in
                     ("device_id", "name", "revoked", "session_access_enabled")}
                    for row in data["devices"].values()]}

    def write(self, path, body, key, *, device_id=None, method="POST"):
        if not isinstance(key, str) or not key.strip() or len(key) > 256:
            return error(400, "idempotency_key_required")
        if not isinstance(body, dict):
            return error(400, "invalid_request")
        with self.lock, NetworkLock(self.service.state):
            if self.service.stop.is_set():
                return error(503, "serve_stopping")
            data = self.read()
            principal = "pair" if path == "/v1/pair" else device_id or "local"
            identity = digest(principal + ":" + key)
            if identity in data["requests"]:
                entry = data["requests"][identity]
                if entry["fingerprint"] != fingerprint(method, path, body):
                    return error(422, "idempotency_key_reused")
                status, result = entry["reply"]
                return status, result
            data["requests"][identity] = dict(fingerprint=fingerprint(method, path, body))
            if path == "/v1/network":
                return self.write_listeners(data, identity, body)
            changed = None
            next_window = self.window
            if path == "/v1/pairing-window":
                if self.fingerprint is None:
                    self.certificate()
                next_window = (secrets.token_urlsafe(32), monotonic() + 300)
                reply = 200, dict(code=next_window[0], expires_in=300, **self.info())
            elif path == "/v1/pair":
                if (self.window is None or monotonic() >= self.window[1]
                        or not secrets.compare_digest(str(body.get("code", "")).encode("utf-8"), self.window[0].encode("ascii"))):
                    return error(403, "pairing_closed")
                else:
                    device_id, token = secrets.token_hex(16), secrets.token_urlsafe(32)
                    data["devices"][device_id] = dict(device_id=device_id, name=str(body.get("name", "Device"))[:128],
                        token_hash=digest(token), revoked=False, session_access_enabled=False)
                    reply = 200, dict(device_id=device_id, device_token=token, session_access_enabled=False)
                    changed = data["devices"][device_id]
                    next_window = None
            elif path.startswith("/v1/devices/") and path.endswith("/session-access"):
                target = path[len("/v1/devices/"):-len("/session-access")]
                if target not in data["devices"] or data["devices"][target]["revoked"]:
                    reply = error(404, "device_not_found")
                elif type(body.get("enabled")) is not bool:
                    reply = error(400, "invalid_request")
                else:
                    changed = data["devices"][target]
                    changed["session_access_enabled"] = body["enabled"]
                    reply = 200, dict(device_id=target, session_access_enabled=body["enabled"])
            elif path == "/v1/push-tokens":
                target = device_id or body.get("device_id")
                token = body.get("token")
                if target not in data["devices"] or data["devices"][target]["revoked"]:
                    reply = error(401, "device_unauthorized")
                elif not isinstance(token, str) or not token.strip() or len(token) > 4096:
                    reply = error(400, "invalid_push_token")
                else:
                    changed = data["devices"][target]
                    changed["push_token"] = token
                    reply = 200, dict(device_id=target, registered=True)
            elif path.startswith("/v1/devices/") and method == "DELETE":
                target = path.removeprefix("/v1/devices/")
                if target not in data["devices"]:
                    reply = error(404, "device_not_found")
                else:
                    data["devices"][target]["revoked"] = True
                    changed = data["devices"][target]
                    reply = 200, dict(device_id=target, revoked=True)
            else:
                return error(404, "not_found")
            # Pairing secrets are returned once, and only live in process memory.
            data["requests"][identity]["reply"] = (error(403, "pairing_closed")
                if path in ("/v1/pair", "/v1/pairing-window") else reply)
            with events.write_transaction([self.path]):
                messages.atomic_json(self.path, data, limit=None)
                if changed is not None:
                    events.emit(self.service.state, "device.changed", dict(user_id=self.service.user_id,
                        **{field: changed[field] for field in ("device_id", "revoked", "session_access_enabled")}))
            self.window = next_window
            if changed is not None and changed["revoked"]:
                for connection, owner in list(self.connections.items()):
                    if owner == changed["device_id"]:
                        disconnect(connection)
                        self.connections.pop(connection, None)
            return reply

    def close(self):
        if self.push_thread is not None:
            self.push_thread.join()
            self.push_thread = None
        with self.lock:
            self.close_listeners(self.servers)
            self.servers.clear()
            for connection in list(self.connections):
                disconnect(connection)
            self.connections.clear()


def positive_setting(cp, name, default):
    value = float(cp.getfloat("serve", name, fallback=default))
    if not 0 < value < float("inf") or (name == "max_connections" and not value.is_integer()):
        raise ValueError("invalid_" + name)
    return value


class TLSServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        # Handshakes run in request threads, so an idle peer cannot stall accept
        # or shutdown. Track sockets before handshaking so shutdown can cancel.
        try:
            request.settimeout(self.handshake_timeout)
            connection = self.context.wrap_socket(request, server_side=True, do_handshake_on_connect=False)
            with connection:
                if not self.network.track(connection):
                    return
                try:
                    connection.do_handshake()
                    connection.settimeout(self.request_timeout)
                    super().process_request_thread(connection, client_address)
                finally:
                    self.network.untrack(connection)
        except OSError:
            request.close()
        finally:
            self.slots.release()


def disconnect(connection):
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    connection.close()


def stream_allowed(handler):
    device_id = getattr(handler, "device_id", None)
    if device_id is None:
        return True
    network = handler.server.service.network
    with network.lock:
        row = network.read()["devices"].get(device_id)
        handler.session_device = dict(id=device_id, session_access=bool(row and row["session_access_enabled"]))
        return bool(row and not row["revoked"])


def handler_class(base):
    """Register one adapter without spreading network routes through rd_serve."""
    class NetworkHandler(base):
        device_id = None

        @property
        def remote(self):
            return isinstance(self.server, TLSServer)

        def authorized(self):
            if not self.remote:
                return True
            network = self.server.service.network
            try:
                result = network.authorize(self)
            except (OSError, ValueError, TypeError, KeyError):
                result = error(503, "network_unavailable")
            path = urlsplit(self.path).path
            if result is None and (path.startswith("/_") or path in ("/v1/pairing-window", "/v1/devices", "/v1/network")
                    or path.startswith("/v1/devices/")):
                result = error(403, "local_only")
            if result is None and (path == "/v1/sessions" or path.startswith("/v1/sessions/")):
                if not getattr(self, "session_access_enabled", False):
                    result = error(403, "session_access_disabled")
            if result is not None:
                self.json(*result)
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            try:
                path = urlsplit(self.path).path
                if path == "/v1/devices":
                    return self.json(200, self.server.service.network.devices())
                if path == "/v1/network":
                    return self.json(200, self.server.service.network.info())
                return super().do_GET()
            finally:
                if self.remote:
                    self.server.service.network.untrack(self.connection)

        def do_POST(self):
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            if (path not in ("/v1/pair", "/v1/pairing-window", "/v1/push-tokens", "/v1/network")
                    and not (path.startswith("/v1/devices/") and path.endswith("/session-access"))):
                return super().do_POST()
            try:
                if self.headers.get("Transfer-Encoding") or self.headers.get_content_type() != "application/json":
                    raise ValueError("invalid_request")
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 8 * 1024 * 1024:
                    raise ValueError("invalid_request")
                body = json.loads(self.rfile.read(length))
                self.json(*self.server.service.network.write(urlsplit(self.path).path, body,
                           self.headers.get("Idempotency-Key"), device_id=self.device_id))
            except ValueError as exc:
                status = 503 if str(exc).startswith(("event_write_failed:", "certificate_")) else 400
                self.json(*error(status, "network_unavailable" if status == 503 else "invalid_request"))
            except (TypeError, UnicodeError):
                self.json(*error(400, "invalid_request"))
            except OSError:
                self.json(*error(503, "network_unavailable"))

        def do_DELETE(self):
            if not self.authorized():
                return
            path = urlsplit(self.path).path
            target = path[len("/v1/devices/"):] if path.startswith("/v1/devices/") else ""
            if not target or "/" in target:
                return self.json(*error(404, "not_found"))
            try:
                self.json(*self.server.service.network.write(path, {}, self.headers.get("Idempotency-Key"), method="DELETE"))
            except (ValueError, OSError):
                self.json(*error(503, "network_unavailable"))
    return NetworkHandler
