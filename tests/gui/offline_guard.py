"""Opt-in pytest network tripwire for local GUI regression runs, not production."""
from __future__ import annotations

import ipaddress
import socket

import pytest


@pytest.fixture(autouse=True)
def forbid_external_network(monkeypatch):
    original = socket.socket.connect

    def connect(sock, address):
        host = address[0] if isinstance(address, tuple) else ""
        try:
            allowed = host == "localhost" or ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise AssertionError("Offline regression run blocked external networking")
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
