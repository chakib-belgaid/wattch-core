"""Canonical local-host identity evidence shared by capture and analysis."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
HOST_IDENTITY_SCHEME = "OS_UNAME_AND_OPTIONAL_MACHINE_ID_V1"


def _canonical_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def local_host_provenance() -> dict[str, str]:
    """Fingerprint the executing host without storing its raw machine id."""

    uname = os.uname()
    identity: dict[str, str] = {
        "sysname": uname.sysname,
        "nodename": uname.nodename,
        "release": uname.release,
        "version": uname.version,
        "machine": uname.machine,
    }
    for candidate in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            machine_id = candidate.read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            continue
        if machine_id:
            identity["machine_id_sha256"] = hashlib.sha256(
                machine_id.encode("utf-8")
            ).hexdigest()
            break
    return {
        "identity_scheme": HOST_IDENTITY_SCHEME,
        "identity_sha256": _canonical_sha256(identity),
    }


def validate_host_provenance(value: object) -> dict[str, str]:
    """Return the canonical shape or reject forged/free-form host evidence."""

    if not isinstance(value, Mapping) or set(value) != {
        "identity_scheme",
        "identity_sha256",
    }:
        raise ValueError("host provenance has a non-canonical shape")
    scheme = value.get("identity_scheme")
    digest = value.get("identity_sha256")
    if scheme != HOST_IDENTITY_SCHEME or not isinstance(digest, str) or not _SHA256.fullmatch(
        digest
    ):
        raise ValueError("host provenance identity is invalid")
    return {"identity_scheme": scheme, "identity_sha256": digest}


def host_provenance_sha256(value: Mapping[str, Any]) -> str:
    return _canonical_sha256(validate_host_provenance(value))


__all__ = [
    "HOST_IDENTITY_SCHEME",
    "host_provenance_sha256",
    "local_host_provenance",
    "validate_host_provenance",
]
