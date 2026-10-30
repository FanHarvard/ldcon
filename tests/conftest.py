"""Paths for explicit offline fixture imports, never production services."""
from pathlib import Path
import sys
import ipaddress
import socket

import pytest

ROOT = Path(__file__).resolve().parent
for directory in (ROOT.parent, ROOT, ROOT / 'support', ROOT / 'support/owner/tests', ROOT / 'support/jobs/tests'):
    sys.path.insert(0, str(directory))


@pytest.fixture(autouse=True)
def offline_network_only(monkeypatch):
    """Regression tests may use loopback servers but never a live instrument."""
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def check(sock, address):
        if sock.family == socket.AF_UNIX:
            return
        host = address[0]
        if host == 'localhost':
            return
        try:
            allowed = ipaddress.ip_address(host).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise AssertionError('Offline test attempted a non-loopback connection')

    def guarded(sock, address):
        check(sock, address)
        return connect(sock, address)

    def guarded_ex(sock, address):
        check(sock, address)
        return connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded)
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded_ex)
