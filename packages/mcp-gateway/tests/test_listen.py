"""`gateway.listen`: the one bind step every CLI in this package shares."""

import socket

import pytest

from gateway.listen import BindError, base_url, listen
from gateway.pager import main as pager_main


def _ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
    except OSError:
        return False
    return True


@pytest.mark.skipif(not _ipv6_loopback_available(), reason="no IPv6 loopback on this host")
def test_listens_on_an_ipv6_host() -> None:
    with listen("::1", 0) as sock:
        assert sock.family == socket.AF_INET6
        port = sock.getsockname()[1]
        assert base_url("::1", port) == f"http://[::1]:{port}"


def test_listens_on_an_ipv4_host() -> None:
    with listen("127.0.0.1", 0) as sock:
        assert sock.family == socket.AF_INET
        port = sock.getsockname()[1]
        assert port > 0
        assert base_url("127.0.0.1", port) == f"http://127.0.0.1:{port}"


def test_a_port_in_use_is_a_bind_error_naming_the_address() -> None:
    with socket.create_server(("127.0.0.1", 0)) as taken:
        port = taken.getsockname()[1]
        with pytest.raises(
            BindError, match=rf"^cannot listen on 127\.0\.0\.1:{port}: .*[Aa]ddress already in use"
        ):
            listen("127.0.0.1", port)


def test_pager_reports_a_port_in_use_without_a_traceback() -> None:
    with socket.create_server(("127.0.0.1", 0)) as taken:
        port = taken.getsockname()[1]
        with pytest.raises(SystemExit, match=r"^pager: cannot listen on .*[Aa]ddress already in use"):
            pager_main(["--port", str(port)])
