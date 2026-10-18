import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from gateway.registry import (
    HttpSource,
    OpenAPISource,
    Registry,
    RegistryError,
    RegistryHolder,
    StdioSource,
    load_registry,
)

WriteRegistry = Callable[[Any], Path]

OPSDESK = {
    "owner": "oncall-team",
    "source": {"kind": "stdio", "command": ["uv", "run", "opsdesk-server"]},
    "rate_limit": {"calls_per_minute": 30},
    "tools": {
        "list_incidents": {"enabled": True, "owner": "oncall-team"},
        "restart_service": {"enabled": False, "owner": "oncall-team"},
    },
}


def test_parses_every_source_kind(write_registry: WriteRegistry) -> None:
    path = write_registry(
        {
            "servers": {
                "opsdesk": OPSDESK,
                "remote": {"owner": "x", "source": {"kind": "http", "url": "http://127.0.0.1:9/mcp"}},
                "pager": {
                    "owner": "y",
                    "source": {
                        "kind": "openapi",
                        "spec": "gateway.pager:SPEC",
                        "base_url": "http://p",
                        "mcp_names": {"listPages": "list_pages"},
                    },
                },
            }
        }
    )
    registry = load_registry(path)
    servers = registry.servers
    assert servers["opsdesk"].source == StdioSource(kind="stdio", command=["uv", "run", "opsdesk-server"])
    assert servers["remote"].source == HttpSource(kind="http", url="http://127.0.0.1:9/mcp")
    assert servers["pager"].source == OpenAPISource(
        kind="openapi", spec="gateway.pager:SPEC", base_url="http://p", mcp_names={"listPages": "list_pages"}
    )
    assert servers["opsdesk"].rate_limit is not None
    assert servers["opsdesk"].rate_limit.calls_per_minute == 30


def test_defaults_are_closed() -> None:
    registry = Registry.model_validate(
        {
            "servers": {
                "s": {
                    "owner": "team",
                    "source": {"kind": "http", "url": "http://x/mcp"},
                    "tools": {"listed_without_flag": {}},
                }
            }
        }
    )
    entry = registry.servers["s"]
    assert entry.rate_limit is None
    assert entry.tools["listed_without_flag"].enabled is False
    assert entry.tools["listed_without_flag"].owner is None


def test_only_listed_and_enabled_tools_are_enabled() -> None:
    entry = Registry.model_validate({"servers": {"opsdesk": OPSDESK}}).servers["opsdesk"]
    assert entry.is_enabled("list_incidents") is True
    assert entry.is_enabled("restart_service") is False
    assert entry.is_enabled("not_in_registry") is False


@pytest.mark.parametrize(
    "server",
    [
        pytest.param({"owner": "t", "source": {"kind": "ftp", "url": "x"}}, id="unknown-kind"),
        pytest.param({"owner": "t", "source": {"kind": "stdio", "command": []}}, id="empty-command"),
        pytest.param(
            {"owner": "t", "source": {"kind": "http", "url": "x"}, "rate_limit": {"calls_per_minute": 0}},
            id="zero-rate",
        ),
        pytest.param({"owner": "t", "source": {"kind": "http", "url": "x"}, "tols": {}}, id="typo-field"),
    ],
)
def test_invalid_entries_are_rejected(server: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Registry.model_validate({"servers": {"s": server}})


def test_refresh_reloads_when_mtime_changes(write_registry: WriteRegistry) -> None:
    holder = RegistryHolder(write_registry({"servers": {"opsdesk": OPSDESK}}))
    assert holder.current.servers["opsdesk"].is_enabled("restart_service") is False

    flipped = {**OPSDESK, "tools": {"restart_service": {"enabled": True}}}
    write_registry({"servers": {"opsdesk": flipped}})
    assert holder.refresh() is True
    assert holder.current.servers["opsdesk"].is_enabled("restart_service") is True
    assert holder.refresh() is False


def test_refresh_ignores_content_change_with_same_mtime(write_registry: WriteRegistry) -> None:
    path = write_registry({"servers": {"opsdesk": OPSDESK}})
    holder = RegistryHolder(path)
    stamp = path.stat().st_mtime_ns
    path.write_text("servers: {}\n")
    os.utime(path, ns=(stamp, stamp))
    assert holder.refresh() is False
    assert "opsdesk" in holder.current.servers


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param("servers: [unclosed\n", id="yaml-syntax"),
        pytest.param("servers:\n  s: {owner: t}\n", id="schema"),
        pytest.param("- just\n- a list\n", id="not-a-mapping"),
    ],
)
def test_invalid_reload_keeps_last_good_registry(
    write_registry: WriteRegistry, bad: str, caplog: pytest.LogCaptureFixture
) -> None:
    holder = RegistryHolder(write_registry({"servers": {"opsdesk": OPSDESK}}))
    good = holder.current
    write_registry(bad)
    with caplog.at_level(logging.ERROR, logger="gateway.registry"):
        assert holder.refresh() is False
    assert holder.current is good
    assert holder.last_error is not None
    assert "keeping the last good registry" in caplog.text

    write_registry({"servers": {"opsdesk": OPSDESK, "x": {**OPSDESK, "owner": "z"}}})
    assert holder.refresh() is True
    assert holder.last_error is None
    assert set(holder.current.servers) == {"opsdesk", "x"}


def test_deleted_file_keeps_last_good_registry(write_registry: WriteRegistry) -> None:
    path = write_registry({"servers": {"opsdesk": OPSDESK}})
    holder = RegistryHolder(path)
    path.unlink()
    assert holder.refresh() is False
    assert "opsdesk" in holder.current.servers
    assert holder.last_error is not None


def test_initial_load_failure_raises(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text("servers: [unclosed\n")
    with pytest.raises(RegistryError, match=r"bad\.yaml"):
        RegistryHolder(path)
    with pytest.raises(RegistryError, match=r"missing\.yaml"):
        RegistryHolder(tmp_path / "missing.yaml")
