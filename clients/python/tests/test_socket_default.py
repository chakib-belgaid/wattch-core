"""Tests for the default Wattch daemon socket-path resolution.

Mirrors the Rust resolution order in wattch_protocol::socket: automatic
discovery uses only the root-owned runtime directory and $XDG_RUNTIME_DIR.
Fine-grained RAPL counters are a documented side channel (PLATYPUS,
CVE-2020-8694), so predictable sockets in attacker-writable /tmp require an
explicit path and are never auto-discovered.
"""

from __future__ import annotations

import os

from pywattch.client import (
    RUN_WATTCH_SOCKET,
    _first_existing,
    default_socket_candidates,
    default_socket_path,
)


def test_first_existing_picks_the_first_path_that_exists():
    calls = []

    def exists(path: str) -> bool:
        calls.append(path)
        return path == "b"

    assert _first_existing(["a", "b", "c"], exists=exists) == "b"
    # short-circuits: "c" is never probed once "b" matches.
    assert calls == ["a", "b"]


def test_first_existing_falls_back_to_first_candidate_when_none_exist():
    assert _first_existing(["a", "b", "c"], exists=lambda _p: False) == "a"


def test_default_socket_candidates_use_only_trusted_runtime_dirs(monkeypatch, tmp_path):
    xdg = tmp_path / "run-user-501"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    monkeypatch.setattr(os, "geteuid", lambda: 501)

    candidates = default_socket_candidates()

    assert candidates == [
        RUN_WATTCH_SOCKET,
        str(xdg / "wattch" / "wattch-powerd.sock"),
    ]


def test_default_socket_candidates_skips_xdg_when_unset(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 501)

    candidates = default_socket_candidates()

    assert candidates == [RUN_WATTCH_SOCKET]


def test_default_socket_candidates_skips_xdg_when_empty(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "")
    monkeypatch.setattr(os, "geteuid", lambda: 501)

    candidates = default_socket_candidates()

    assert candidates == [RUN_WATTCH_SOCKET]


def test_default_socket_path_prefers_existing_xdg_socket(monkeypatch, tmp_path):
    xdg = tmp_path / "run-user-501"
    wattch_dir = xdg / "wattch"
    wattch_dir.mkdir(parents=True)
    socket_path = wattch_dir / "wattch-powerd.sock"
    socket_path.touch()

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    monkeypatch.setattr(os, "geteuid", lambda: 501)

    assert default_socket_path() == str(socket_path)


def test_default_socket_path_falls_back_to_first_candidate_when_none_exist(
    monkeypatch, tmp_path
):
    xdg = tmp_path / "does-not-exist"
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    monkeypatch.setattr(os, "geteuid", lambda: 999999)

    # Neither trusted candidate exists in a throwaway runtime directory, so the
    # highest-priority candidate is returned anyway -- the caller (WattchClient
    # connect) is responsible for surfacing a WattchConnectionError that
    # includes this path.
    assert default_socket_path() == RUN_WATTCH_SOCKET
