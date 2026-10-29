"""Shared target-host and session-access configuration reads and writes.

The serve HTTP routes and the CLI setting commands call these functions so
validation, backups and atomic replacement stay identical on both paths.
"""
import configparser
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
import uuid

from . import cli


SESSION_ACCESS = "/v1/config/session-access"
COLLECTION = "/v1/config/hosts"
ROUTE = re.compile(r"/v1/config/hosts(?:/([A-Za-z0-9._-]+))?")
PUBLIC_FIELDS = set(cli.DEFAULTS) - {"env_file", "codex_auth_file", "dsh_auth_file", "agy_auth_file", "ssh_opts"}
PUBLIC_FIELDS.add("ssh")
NAME = re.compile(r"[A-Za-z0-9._-]{1,128}")
FIELD = re.compile(r"[a-z][a-z0-9_]*")
TERMINAL_STATES = ("done", "dead", "missing")


class InvalidField(ValueError):
    def __init__(self, field, reason="invalid_host_field"):
        self.field, self.reason = field, reason


def validate_name(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise InvalidField("name")


def validate_field(field, value):
    if not FIELD.fullmatch(field):
        raise ValueError("invalid_field")
    if value is not None and (not isinstance(value, str) or any(c in value for c in "\n\r\x00")):
        raise InvalidField(field)


def validate(values):
    for field in ("ssh", "workdir"):
        if not values.get(field, "").strip():
            raise InvalidField(field, "host_field_required")
    for field, allowed in (("sync", ("origin", "direct")),
                           ("platform", ("posix", "macos", "linux", "windows")),
                           ("enabled", ("1", "0", "true", "false", "yes", "no")),
                           ("launcher", ("nohup", "tmux"))):
        value = values[field].lower() if field == "enabled" else values[field]
        if value not in allowed:
            raise InvalidField(field)
    if values["ssh"] == "local":
        if values["platform"] == "windows":
            raise InvalidField("platform")
        if not values["workdir"].startswith(("/", "~", "$HOME")):
            raise InvalidField("workdir")
    try:
        cli.parse_cache_dirs(values.get("cache_dirs", ""), values["workdir"])
    except ValueError:
        raise InvalidField("cache_dirs") from None
    for field in cli.DEFAULTS:
        if field.startswith(("max_", "min_", "warn_", "swap_")) or field == "timeout":
            try:
                number = float(values[field])
                if field in ("max_heavy", "timeout"):
                    int(values[field])  # Match the launcher's integer-string parser.
            except (ValueError, OverflowError):
                raise InvalidField(field) from None
            if (not math.isfinite(number) or number < 0
                    or field.endswith("_pct") and number > 100
                    or field in ("max_heavy", "timeout") and number < 1):
                raise InvalidField(field)


def parse(text):
    cp = configparser.ConfigParser(interpolation=None)
    cp.read_string(text)
    return cp


def effective(cp, name):
    values = dict(cli.DEFAULTS)
    if cp.has_section("defaults"):
        values.update(cp["defaults"])
    values.update(cp["host:" + name])
    return values


def edit(text, name, fields, *, delete=False, section_name=None):
    """Follow ConfigParser's indentation rules, retaining untouched raw lines."""
    newline = "\r\n" if "\r\n" in text else "\n"
    target = section_name or "host:" + name
    section, option_indent = None, None
    found, dropping = False, False
    result, pending = [], dict(fields)

    def append_pending():
        if pending and result and not result[-1].endswith("\n"):
            result[-1] += newline
        result.extend(f"{key} = {value}{newline}" for key, value in pending.items() if value is not None)
        pending.clear()

    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped or stripped.startswith((";", "#")):
            result.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        # Continuations take precedence over both section and option syntax.
        if option_indent is not None and indent > option_indent:
            if not dropping:
                result.append(line)
            continue
        header = configparser.ConfigParser.SECTCRE.match(line.lstrip())
        if header:
            if section == target:
                append_pending()
            section, option_indent, dropping = header.group("header"), None, False
            if section == target:
                found = True
            if section != target or not delete:
                result.append(line)
            else:
                annotation = line.lstrip()[header.end():].strip()
                if annotation.startswith((";", "#")):
                    result.append(annotation + newline)
            continue
        option = configparser.ConfigParser.OPTCRE.match(line.lstrip())
        option_indent = indent if option else None
        field = option.group("option").rstrip().lower() if option else None
        dropping = section == target and (delete or field in pending)
        if dropping:
            if not delete:
                value = pending.pop(field)
                if value is not None:
                    prefix = line[:indent] + option.group("option") + option.group("vi")
                    result.append(prefix + value + newline)
        else:
            result.append(line)
    if not found:
        if result and not result[-1].endswith("\n"):
            result[-1] += newline
        result.append("[" + target + "]" + newline)
    if not found or section == target:
        append_pending()
    return "".join(result)


def session_access(cp):
    try:
        return cp.getboolean("serve", "session_access", fallback=True)
    except ValueError:
        raise OSError("invalid_session_access") from None


def session_access_candidate(raw, enabled):
    if type(enabled) is not bool:
        raise ValueError("invalid_session_access")
    text = edit(raw.decode("utf-8"), "", {"session_access": str(enabled).lower()},
                section_name="serve")
    return parse(text), text.encode("utf-8")


def host_candidate(raw, name, fields, *, delete=False):
    text = edit(raw.decode("utf-8"), name, fields, delete=delete)
    return parse(text), text.encode("utf-8")


def config_digest(path):
    """Stable ASCII header value for a possibly non-ASCII config path."""
    return hashlib.sha256(os.fsencode(str(Path(path).absolute()))).hexdigest()


def load(path):
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise OSError("invalid_config_path")
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    return raw, parse(raw.decode("utf-8"))


def replace(path, raw, updated):
    """Durably back up the original bytes, then atomically replace the file."""
    path = Path(path)
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise OSError("invalid_config_path")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = path.with_name(path.name + ".bak." + uuid.uuid4().hex)
    backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(backup_fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def active_job_ids(records, states):
    """Unknown or unconfirmed execution still blocks deleting its host."""
    return sorted({record["job"] for record in records
                   if states.get(record["job"], {}).get("state") not in TERMINAL_STATES})
