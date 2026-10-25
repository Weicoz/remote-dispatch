"""Canonical request fingerprints; never persist request bodies."""
import hashlib
import json


def fingerprint(method, path, body):
    payload = json.dumps([method.upper(), path, body], sort_keys=True,
                         ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
