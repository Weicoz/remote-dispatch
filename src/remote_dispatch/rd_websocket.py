"""Small RFC 6455 server transport, using only the standard library."""
import base64
import hashlib
import struct


# 1 MiB raw input plus its JSON/base64 envelope. Fragment totals share this bound.
MAX_MESSAGE = 4 * ((1024 * 1024 + 2) // 3) + 1024


class WebSocketError(ValueError):
    def __init__(self, code=1002):
        self.code = code


def handshake(handler):
    headers = handler.headers
    try:
        key = headers.get("Sec-WebSocket-Key", "")
        decoded = base64.b64decode(key, validate=True)
        valid = (handler.request_version == "HTTP/1.1"
                 and headers.get("Upgrade", "").lower() == "websocket"
                 and "upgrade" in {part.strip().lower() for part in headers.get("Connection", "").split(",")}
                 and headers.get("Sec-WebSocket-Version") == "13" and len(decoded) == 16
                 and not headers.get("Transfer-Encoding")
                 and headers.get("Content-Length", "0") == "0"
                 and all(len(headers.get_all(name, [])) == 1 for name in (
                     "Sec-WebSocket-Key", "Sec-WebSocket-Version", "Upgrade")))
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise WebSocketError()
    return base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode()


def frame(opcode, payload):
    length = len(payload)
    header = bytes([0x80 | opcode])
    if length < 126:
        header += bytes([length])
    elif length < 65536:
        header += bytes([126]) + struct.pack("!H", length)
    else:
        header += bytes([127]) + struct.pack("!Q", length)
    return header + payload


class Reader:
    def __init__(self, stream):
        self.stream = stream
        self.fragments = bytearray()
        self.fragmented = False

    def exact(self, length):
        result = bytearray()
        while len(result) < length:
            chunk = self.stream.read(length - len(result))
            if not chunk:
                raise EOFError()
            result.extend(chunk)
        return bytes(result)

    def receive(self):
        """Return complete text/control messages; allow control frames mid-message."""
        while True:
            first, second = self.exact(2)
            final, opcode = bool(first & 128), first & 15
            length = second & 127
            if first & 0x70 or not second & 128 or opcode not in (0, 1, 2, 8, 9, 10):
                raise WebSocketError()
            if opcode == 2:
                raise WebSocketError(1003)
            if opcode >= 8 and (not final or length > 125):
                raise WebSocketError()
            if length == 126:
                length = struct.unpack("!H", self.exact(2))[0]
                if length < 126:
                    raise WebSocketError()
            elif length == 127:
                length = struct.unpack("!Q", self.exact(8))[0]
                if length < 65536 or length >= 1 << 63:
                    raise WebSocketError()
            if length > MAX_MESSAGE or (opcode < 8 and len(self.fragments) + length > MAX_MESSAGE):
                raise WebSocketError(1009)
            mask = self.exact(4)
            payload = bytes(value ^ mask[i % 4] for i, value in enumerate(self.exact(length)))
            if opcode == 8:
                if len(payload) == 1:
                    raise WebSocketError()
                if payload:
                    code = struct.unpack("!H", payload[:2])[0]
                    if code not in (1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011, 1012, 1013, 1014) and not 3000 <= code <= 4999:
                        raise WebSocketError()
                    try:
                        payload[2:].decode("utf-8")
                    except UnicodeError:
                        raise WebSocketError(1007) from None
                return opcode, payload
            if opcode >= 8:
                return opcode, payload
            if (opcode == 0) != self.fragmented:
                raise WebSocketError()
            self.fragments.extend(payload)
            if not final:
                self.fragmented = True
                continue
            payload = bytes(self.fragments)
            self.fragments.clear()
            self.fragmented = False
            try:
                return 1, payload.decode("utf-8")
            except UnicodeError:
                raise WebSocketError(1007) from None
