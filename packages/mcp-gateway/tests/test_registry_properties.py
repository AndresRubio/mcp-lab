"""Property test: `_UniqueKeyLoader` rejects exactly the documents a composed-tree reference flags.

The loader hooks PyYAML's flatten and construct steps, and each earlier fix found a path those hooks
missed. The reference here reads the composed node tree instead, so it shares none of that logic: it
knows nothing about flatten order, construction order or how `<<` rewrites a mapping.
"""

from dataclasses import dataclass, field

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from gateway.registry import _UniqueKeyLoader

_MERGE_TAG = "tag:yaml.org,2002:merge"


def reference_duplicates(text: str) -> list[str]:
    """Every duplicate in `text`'s mappings, as written, with each mapping node checked once.

    A key that is a `<<` merge (by tag) is skipped, except that two of them in one mapping are a
    duplicate. Keys are built with `SafeLoader`'s constructor, so YAML 1.1 equalities such as
    `yes`/`true`/`1`/`0x1` count the way PyYAML counts them.
    """
    root = yaml.compose(text, Loader=yaml.SafeLoader)
    if root is None:
        return []
    constructor = yaml.SafeLoader("")
    found: list[str] = []
    visited: set[yaml.Node] = set()
    pending: list[yaml.Node] = [root]
    try:
        while pending:
            node = pending.pop()
            if node in visited:  # an anchor reached again through an alias
                continue
            visited.add(node)
            if isinstance(node, yaml.MappingNode):
                pairs: list[tuple[yaml.Node, yaml.Node]] = node.value
                found.extend(_duplicates_in(constructor, pairs))
                pending.extend(child for pair in pairs for child in pair)
            elif isinstance(node, yaml.SequenceNode):
                items: list[yaml.Node] = node.value
                pending.extend(items)
    finally:
        constructor.dispose()
    return found


def _duplicates_in(constructor: yaml.SafeLoader, pairs: list[tuple[yaml.Node, yaml.Node]]) -> list[str]:
    found: list[str] = []
    if sum(key_node.tag == _MERGE_TAG for key_node, _ in pairs) > 1:
        found.append("<<")
    seen: set[object] = set()
    for key_node, _ in pairs:
        if key_node.tag == _MERGE_TAG:
            continue
        key = constructor.construct_object(key_node, deep=True)
        if key in seen:
            found.append(repr(key))
        seen.add(key)
    return found


# --- the reference on its own --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("a: 1\nb: 2\n", [], id="no-duplicate"),
        pytest.param("a: 1\na: 2\n", ["'a'"], id="plain"),
        pytest.param("x:\n  y: {a: 1, a: 2}\n", ["'a'"], id="nested"),
        pytest.param("x: [0, {a: 1, a: 2}]\n", ["'a'"], id="inside-a-sequence"),
        pytest.param("x: {<<: [{}, {a: 1, a: 2}]}\n", ["'a'"], id="inside-a-merge-list"),
        pytest.param("yes: 1\ntrue: 2\n", ["True"], id="yes-true"),
        pytest.param("0x1: 1\n1: 2\n", ["1"], id="hex-int"),
        pytest.param("1: 1\ntrue: 2\n", ["True"], id="int-bool"),
        pytest.param("'yes': 1\nyes: 2\n", [], id="quoted-yes-is-a-string"),
        pytest.param("~: 1\nnull: 2\n", ["None"], id="null-spellings"),
        pytest.param(
            "base: &b {k: 1, k: 2}\nuse: {<<: *b}\n",
            ["'k'"],
            id="duplicate-only-inside-merged-anchor",
        ),
        pytest.param("use: {<<: {k: 1, k: 2}}\n", ["'k'"], id="duplicate-only-inside-inline-merge"),
        pytest.param(
            "base: &b {k: 1, k: 2}\none: *b\ntwo: *b\nthree: {<<: [*b, *b]}\n",
            ["'k'"],
            id="shared-anchor-reported-once",
        ),
        pytest.param("base: &b {k: 1}\none: *b\ntwo: {<<: *b}\n", [], id="shared-anchor-clean"),
        pytest.param("base: &b {k: 1}\nuse: {<<: *b, k: 2}\n", [], id="explicit-override-of-merged-key"),
        pytest.param(
            "base: &b {k: 1}\nmid: &m {<<: *b, k: 2}\nuse: {<<: *m, k: 3}\n",
            [],
            id="override-through-merge-of-merge",
        ),
        pytest.param("a: &a {k: 1}\nb: {<<: *a, <<: *a}\n", ["<<"], id="repeated-merge"),
        pytest.param("a: &a {k: 1}\nb: {<<: *a, !!merge '<<': *a}\n", ["<<"], id="tagged-merge-counts"),
        pytest.param("a: &a {k: 1}\nb: {<<: *a, '<<': 2}\n", [], id="quoted-merge-is-a-plain-key"),
        pytest.param("'<<': 1\n'<<': 2\n", ["'<<'"], id="quoted-merge-repeated"),
        pytest.param("", [], id="empty-document"),
    ],
)
def test_reference_finds_exactly_the_duplicates_as_written(text: str, expected: list[str]) -> None:
    assert reference_duplicates(text) == expected


# --- the generator -------------------------------------------------------------------------------

# A small alphabet, so keys collide often. `yes`, `true`, `1` and `0x1` all construct equal to True;
# `'yes'` and `'<<'` are quoted strings, so neither is a bool nor a merge key.
KEYS = ("a", "a", "b", "b", "c", "c", "d", "e", "yes", "true", "'yes'", "1", "0x1", "~", "'<<'")
SCALARS = ("0", "1", "x", "true", "~")
MAX_DEPTH = 3


@dataclass
class _Doc:
    """Anchors defined so far, in text order, so an alias always follows its anchor."""

    mapping_anchors: list[str] = field(default_factory=list[str])
    other_anchors: list[str] = field(default_factory=list[str])
    next_anchor: int = 0

    def new_anchor(self) -> str:
        self.next_anchor += 1
        return f"a{self.next_anchor}"


def _mapping(draw: st.DrawFn, doc: _Doc, depth: int) -> str:
    """A flow mapping, possibly anchored, whose entries are drawn in the order they are written."""
    anchor = doc.new_anchor() if draw(st.integers(0, 2)) == 2 else None
    # Nine in twenty mappings merge once and one in twenty merges twice, so most documents with a merge
    # are legal and a repeated `<<` still turns up.
    merges = draw(st.sampled_from((0,) * 10 + (1,) * 9 + (2,)))
    pairs = draw(st.integers(0, 3))
    kinds = draw(st.permutations(["merge"] * merges + ["pair"] * pairs))
    entries: list[str] = []
    for kind in kinds:
        merge = _merge_value(draw, doc, depth) if kind == "merge" else None
        if merge is not None:
            spelling = draw(st.sampled_from(("<<", "<<", "<<", "!!merge '<<'")))
            entries.append(f"{spelling}: {merge}")
        else:
            entries.append(f"{draw(st.sampled_from(KEYS))}: {_value(draw, doc, depth)}")
    text = "{" + ", ".join(entries) + "}"
    if anchor is None:
        return text
    doc.mapping_anchors.append(anchor)  # usable only after the mapping is complete
    return f"&{anchor} {text}"


def _merge_source(draw: st.DrawFn, doc: _Doc, depth: int) -> str | None:
    """One mapping a `<<` may merge: an alias to a mapping anchor or an inline mapping."""
    can_inline = depth < MAX_DEPTH
    if doc.mapping_anchors and (not can_inline or draw(st.booleans())):
        return "*" + draw(st.sampled_from(doc.mapping_anchors))
    return _mapping(draw, doc, depth + 1) if can_inline else None


def _merge_value(draw: st.DrawFn, doc: _Doc, depth: int) -> str | None:
    # Each draw that adds structure shrinks toward the simpler choice, so a failure shrinks well.
    if not draw(st.booleans()):
        return _merge_source(draw, doc, depth)
    sources = [_merge_source(draw, doc, depth) for _ in range(draw(st.integers(1, 3)))]
    present = [source for source in sources if source is not None]
    return "[" + ", ".join(present) + "]" if present else None


def _value(draw: st.DrawFn, doc: _Doc, depth: int) -> str:
    choices = ["scalar", "scalar"]
    if doc.mapping_anchors or doc.other_anchors:
        choices.append("alias")
    if depth < MAX_DEPTH:
        choices += ["mapping", "mapping", "sequence"]
    match draw(st.sampled_from(choices)):
        case "alias":
            return "*" + draw(st.sampled_from(doc.mapping_anchors + doc.other_anchors))
        case "mapping":
            return _mapping(draw, doc, depth + 1)
        case "sequence":
            items = [_value(draw, doc, depth + 1) for _ in range(draw(st.integers(0, 2)))]
            return "[" + ", ".join(items) + "]"
        case _:
            scalar = draw(st.sampled_from(SCALARS))
            if draw(st.integers(0, 4)) == 4:
                anchor = doc.new_anchor()
                doc.other_anchors.append(anchor)
                return f"&{anchor} {scalar}"
            return scalar


@st.composite
def yaml_documents(draw: st.DrawFn) -> str:
    """Small flow-style YAML documents full of anchors, aliases, merges and colliding keys."""
    return _mapping(draw, _Doc(), 0) + "\n"


# --- the property --------------------------------------------------------------------------------


@settings(max_examples=400, derandomize=True, database=None, deadline=None)
@given(yaml_documents())
def test_loader_rejects_exactly_what_the_reference_flags(text: str) -> None:
    expected = yaml.load(text, Loader=yaml.SafeLoader)  # the generator emits only loadable YAML
    duplicates = reference_duplicates(text)
    try:
        loaded = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as err:
        assert duplicates, f"rejected a document without duplicates: {err}"
        return
    assert not duplicates, f"accepted a document with duplicate keys {duplicates}"
    assert loaded == expected
