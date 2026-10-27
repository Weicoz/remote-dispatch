"""Incremental, fail-closed ANSI terminal scrubber; never stores a transcript."""
import codecs
import re


CREDENTIAL_LABEL = re.compile(r"(?i)(?:auth_file|env_file|ssh(?:_opts)?|password|secret|token|api_key|private_?key)[\"']?\s*$")
CREDENTIAL = re.compile(r"(?i)(?:auth_file|env_file|ssh(?:_opts)?|password|secret|token|api_key|private_?key)[\"']?\s*[:=]$")


class TerminalRedactor:
    def __init__(self, service):
        self.service = service
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.tail = ""
        self.secret_tail = ""
        self.escape = ""
        self.string_escape = False
        self.string_end = False
        self.value = None
        self.value_escape = False
        self.credential_label = False
        self.pem = False

    def feed(self, data):
        output, visible, positions = [], [], []
        secrets = tuple(secret for secret in self.service.sensitive if secret)
        text = self.decoder.decode(data)
        if (not secrets and not self.escape and not self.string_escape and not self.value and not self.pem
                and not any(char in text for char in "\x1b=:-")):
            if text and text.strip():
                self.credential_label = bool(CREDENTIAL_LABEL.search((self.tail + text.rstrip())[-256:]))
            elif text:
                self.credential_label = self.credential_label or bool(CREDENTIAL_LABEL.search(self.tail))
            self.tail = (self.tail + text)[-256:]
            return text.encode("utf-8")
        for char in text:
            if self.string_escape:
                # Drop OSC/DCS/APC/PM/SOS bodies, including split terminators.
                if char == "\x07" or (self.string_end and char == "\\"):
                    self.string_escape = self.string_end = False
                else:
                    self.string_end = char == "\x1b"
                continue
            if self.escape:
                self.escape += char
                if len(self.escape) == 2 and char in "]PX^_":
                    self.escape = ""
                    self.string_escape = True
                elif self.escape.startswith("\x1b["):
                    if len(self.escape) > 4096:
                        raise ValueError("terminal_unavailable")
                    if len(self.escape) > 2 and "@" <= char <= "~":
                        output.append(self.escape)
                        self.escape = ""
                elif "0" <= char <= "~":
                    output.append(self.escape)
                    self.escape = ""
                continue
            if char == "\x1b":
                self.escape = char
                continue
            self.tail = (self.tail + char)[-256:]
            masked = self.pem
            if self.value == "waiting":
                if not char.isspace():
                    self.value = char if char in "\"'" else "unquoted"
                    masked = True
            elif self.value:
                masked = True
                if self.value == "unquoted" and (char.isspace() or char in ",}"):
                    masked = False
                    self.value = None
                elif self.value in ("\"", "'"):
                    if self.value_escape:
                        self.value_escape = False
                    elif char == "\\":
                        self.value_escape = True
                    elif char == self.value:
                        self.value = None
            if CREDENTIAL.search(self.tail) or (self.credential_label and char in ":="):
                self.value = "waiting"
                self.value_escape = False
            if char.isspace():
                self.credential_label = self.credential_label or bool(CREDENTIAL_LABEL.search(self.tail))
            else:
                self.credential_label = False
            if re.search(r"-----BEGIN [^-]*PRIVATE KEY-----$", self.tail):
                self.pem = True
            if self.pem and re.search(r"-----END [^-]*PRIVATE KEY-----$", self.tail):
                self.pem = False
            positions.append(len(output))
            visible.append(char)
            output.append("*" if masked else char)
        # Match over visible text, ignoring ANSI style/position codes. Hold no
        # output: an unfinished secret prefix at a paint boundary is masked now.
        # Later paints can complete it, but cannot expose its earlier prefix.
        previous = len(self.secret_tail)
        combined = self.secret_tail + "".join(visible)
        masked = bytearray(len(visible))
        for secret in secrets:
            start = combined.find(secret)
            while start >= 0:
                left, right = max(0, start - previous), start + len(secret) - previous
                if right > 0:
                    masked[left:right] = b"\x01" * (right - left)
                start = combined.find(secret, start + 1)
            for length in range(1, min(len(secret), len(combined)) + 1):
                if combined.endswith(secret[:length]):
                    left = max(0, len(combined) - length - previous)
                    masked[left:] = b"\x01" * (len(visible) - left)
        keep = max([1] + [len(secret) for secret in secrets]) - 1
        self.secret_tail = combined[-keep:] if keep else ""
        for index, hide in enumerate(masked):
            if hide:
                output[positions[index]] = "*"
        return "".join(output).encode("utf-8")
