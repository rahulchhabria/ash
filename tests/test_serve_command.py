from __future__ import annotations

import socket

from ash.cli.commands.serve import _address_in_use


def test_address_in_use_detects_bound_loopback_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = int(sock.getsockname()[1])

        assert _address_in_use("127.0.0.1", port) is True


def test_address_in_use_returns_false_for_free_loopback_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    assert _address_in_use("127.0.0.1", port) is False
