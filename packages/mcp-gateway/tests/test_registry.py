import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from gateway.registry import (
    HttpSource,
    OpenAPISource,
    Registry,
    RegistryError,
    RegistryHolder,
    StdioSource,
    _UniqueKeyLoader,
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


def test_non_utf8_reload_keeps_last_good_registry(write_registry: WriteRegistry, registry_path: Path) -> None:
    holder = RegistryHolder(write_registry({"servers": {"opsdesk": OPSDESK}}))
    good = holder.current
    registry_path.write_bytes(b"\xff\xfe\x00")
    stamp = registry_path.stat().st_mtime_ns + 1_000_000_000
    os.utime(registry_path, ns=(stamp, stamp))
    assert holder.refresh() is False
    assert holder.current is good
    assert holder.last_error is not None


def test_restart_only_edits_log_a_warning(
    write_registry: WriteRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    remote = {"owner": "x", "source": {"kind": "http", "url": "http://127.0.0.1:9/mcp"}}
    holder = RegistryHolder(write_registry({"servers": {"opsdesk": OPSDESK, "remote": remote}}))
    moved = {**OPSDESK, "source": {"kind": "stdio", "command": ["other-server"]}}
    write_registry({"servers": {"opsdesk": moved, "added": remote}})
    with caplog.at_level(logging.INFO, logger="gateway.registry"):
        assert holder.refresh() is True
    [warning] = [r for r in caplog.records if r.levelno == logging.WARNING]
    message = warning.getMessage()
    assert "restart" in message
    assert "source changed: opsdesk" in message
    assert "added: added" in message
    assert "removed: remote" in message


def test_live_edits_log_no_restart_warning(
    write_registry: WriteRegistry, caplog: pytest.LogCaptureFixture
) -> None:
    holder = RegistryHolder(write_registry({"servers": {"opsdesk": OPSDESK}}))
    flipped = {**OPSDESK, "owner": "someone-else", "tools": {"restart_service": {"enabled": True}}}
    write_registry({"servers": {"opsdesk": flipped}})
    with caplog.at_level(logging.INFO, logger="gateway.registry"):
        assert holder.refresh() is True
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert "reloaded" in caplog.text


@pytest.mark.parametrize("name", ["opsdesk", "pager-2", "a", "0ps_desk"])
def test_server_names_that_fit_a_mount_path_are_accepted(name: str) -> None:
    registry = Registry.model_validate({"servers": {name: OPSDESK}})
    assert list(registry.servers) == [name]


@pytest.mark.parametrize(
    "name", ["", "OpsDesk", "-pager", "_pager", "a/b", "a b", "..", "a.b", "pager\n", "café"]
)
def test_server_names_that_would_break_the_mount_path_are_rejected(name: str) -> None:
    with pytest.raises(ValidationError):
        Registry.model_validate({"servers": {name: OPSDESK}})


DUPLICATE_TOOL = """\
servers:
  opsdesk:
    owner: oncall-team
    source: {kind: stdio, command: [opsdesk-server]}
    tools:
      restart_service: {enabled: false}
      restart_service: {enabled: true}
"""

DUPLICATE_SERVER = """\
servers:
  opsdesk: {owner: a, source: {kind: http, url: "http://127.0.0.1:9/mcp"}}
  opsdesk: {owner: b, source: {kind: http, url: "http://127.0.0.1:9/mcp"}}
"""


@pytest.mark.parametrize(
    ("text", "key"),
    [
        pytest.param(DUPLICATE_TOOL, "restart_service", id="tool"),
        pytest.param(DUPLICATE_SERVER, "opsdesk", id="server"),
    ],
)
def test_duplicate_keys_are_rejected(write_registry: WriteRegistry, text: str, key: str) -> None:
    # Otherwise the last duplicate wins, and a reviewer could approve `enabled: false` above a `true`.
    with pytest.raises(RegistryError, match=rf"duplicate key '{key}'"):
        load_registry(write_registry(text))


def _merge_registry(tool_body: str) -> str:
    return f"""\
servers:
  opsdesk:
    owner: oncall-team
    source: {{kind: stdio, command: [opsdesk-server]}}
    tools:
      muted: &off {{enabled: false}}
      owned_by: &owned {{owner: oncall-team}}
{tool_body}
"""


def test_repeated_merge_key_is_rejected(write_registry: WriteRegistry) -> None:
    # Guards the `<<` handling in `_UniqueKeyLoader`: two merges in one mapping let one anchor
    # silently override the other, with nothing a reviewer would see as a duplicate.
    text = _merge_registry("      restart_service:\n        <<: *off\n        <<: *owned")
    with pytest.raises(RegistryError, match=r"duplicate key '<<'"):
        load_registry(write_registry(text))


def test_repeated_merge_key_at_top_level_is_rejected(write_registry: WriteRegistry) -> None:
    # Guards the same rule at the document root. Both merges pull in an empty mapping, so the
    # duplicate `<<` is the only thing wrong with this otherwise valid registry.
    text = """\
servers:
  opsdesk:
    owner: oncall-team
    source: {kind: stdio, command: [opsdesk-server]}
    tools: &empty {}
<<: *empty
<<: *empty
"""
    with pytest.raises(RegistryError, match=r"duplicate key '<<'"):
        load_registry(write_registry(text))


def test_repeated_merge_key_in_an_anchor_merged_by_an_earlier_node_is_rejected(
    write_registry: WriteRegistry,
) -> None:
    # Guards counting `<<` before the base class flattens: a shallower `<<: *a` is built first and
    # strips the anchor's own `<<` pairs, so counting only in `construct_mapping` saw none.
    text = """\
servers:
  e0:
    owner: o
    source: {kind: stdio, command: [x]}
    tools: &e {}
  y:
    owner: o
    source: {kind: stdio, command: [x]}
    tools:
      t: &a
        <<: *e
        <<: *e
  x:
    owner: o
    source: {kind: stdio, command: [x]}
    tools: {<<: *a}
"""
    with pytest.raises(RegistryError, match=r"duplicate key '<<'"):
        load_registry(write_registry(text))


def test_explicit_merge_tag_counts_as_a_merge_key(write_registry: WriteRegistry) -> None:
    # Guards the tag-based count: `!!merge '<<'` is a merge key even though it is quoted.
    text = _merge_registry("      restart_service:\n        <<: *off\n        !!merge '<<': *owned")
    with pytest.raises(RegistryError, match=r"duplicate key '<<'"):
        load_registry(write_registry(text))


def test_quoted_merge_key_is_a_plain_key_next_to_a_real_merge() -> None:
    # Guards the tag-based count: a quoted '<<' is a string key, so it is not a second merge.
    text = "a: &a {k: 1}\nb:\n  <<: *a\n  '<<': 2\n"
    assert yaml.load(text, Loader=_UniqueKeyLoader)["b"] == {"k": 1, "<<": 2}


def test_single_merge_still_loads(write_registry: WriteRegistry) -> None:
    # Guards against the new rule rejecting a legitimate single `<<`.
    text = _merge_registry("      restart_service:\n        <<: *off\n        owner: oncall-team")
    policy = load_registry(write_registry(text)).servers["opsdesk"].tools["restart_service"]
    assert (policy.enabled, policy.owner) == (False, "oncall-team")


def test_merge_list_still_loads(write_registry: WriteRegistry) -> None:
    # Guards `<<: [*a, *b]`, the supported way to merge several anchors.
    text = _merge_registry("      restart_service:\n        <<: [*off, *owned]")
    policy = load_registry(write_registry(text)).servers["opsdesk"].tools["restart_service"]
    assert (policy.enabled, policy.owner) == (False, "oncall-team")


def test_explicit_key_overrides_merged_one(write_registry: WriteRegistry) -> None:
    # Guards standard YAML semantics: an explicit key beats a merged one, without an error.
    text = _merge_registry("      restart_service:\n        <<: *off\n        enabled: true")
    policy = load_registry(write_registry(text)).servers["opsdesk"].tools["restart_service"]
    assert policy.enabled is True


def _servers_with_tools(tools_body: str) -> str:
    return f"""\
servers:
  opsdesk:
    owner: oncall-team
    source: {{kind: stdio, command: [opsdesk-server]}}
    tools:
{tools_body}
"""


DUPLICATE_IN_MERGED_MAPPING = _servers_with_tools(
    "      <<: &policies\n        restart_service: {enabled: false}\n        restart_service: {enabled: true}"
)

DUPLICATE_IN_MERGE_LIST = _servers_with_tools(
    "      <<: [{list_incidents: {enabled: true}},"
    " {restart_service: {enabled: false}, restart_service: {enabled: true}}]"
)

DUPLICATE_IN_NESTED_MERGE = _servers_with_tools(
    "      <<:\n"
    "        <<: {restart_service: {enabled: false}, restart_service: {enabled: true}}\n"
    "        list_incidents: {enabled: true}"
)

DUPLICATE_IN_ANCHOR_ONLY_MERGED_ELSEWHERE = """\
servers:
  pager:
    owner: oncall-team
    source: {kind: stdio, command: [pager-server]}
    tools:
      <<: &policies {restart_service: {enabled: false}, restart_service: {enabled: true}}
  opsdesk:
    owner: oncall-team
    source: {kind: stdio, command: [opsdesk-server]}
    tools: {<<: *policies}
"""


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(DUPLICATE_IN_MERGED_MAPPING, id="merged-mapping"),
        pytest.param(DUPLICATE_IN_MERGE_LIST, id="merge-list"),
        pytest.param(DUPLICATE_IN_NESTED_MERGE, id="nested-merge"),
        pytest.param(DUPLICATE_IN_ANCHOR_ONLY_MERGED_ELSEWHERE, id="anchor-merged-twice"),
    ],
)
def test_duplicate_key_inside_a_merged_mapping_is_rejected(write_registry: WriteRegistry, text: str) -> None:
    # A mapping reached only through `<<` is flattened into its consumer without being constructed,
    # so its own duplicates must be caught there; otherwise `enabled: true` silently wins.
    with pytest.raises(RegistryError, match=r"duplicate key 'restart_service'"):
        load_registry(write_registry(text))


def test_override_in_an_anchor_merged_by_a_shallower_node_still_loads() -> None:
    # Guards standard YAML semantics when a shallower `<<: *a` flattens the deeper anchor before
    # the anchor is built: its merged `k` and its explicit `k` are an override, not a duplicate.
    text = "deep:\n  deeper:\n    anchor: &a {<<: {k: 1}, k: 2}\nshallow: {<<: *a}\n"
    data = yaml.load(text, Loader=_UniqueKeyLoader)
    assert data["shallow"] == {"k": 2}
    assert data["deep"]["deeper"]["anchor"] == {"k": 2}
