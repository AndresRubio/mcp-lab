"""Property test: `_UniqueKeyLoader` rejects exactly the documents a composed-tree reference flags.

The loader hooks PyYAML's flatten and construct steps, and each earlier fix found a path those hooks
missed. The reference here reads the composed node tree instead, so it shares none of that logic: it
knows nothing about flatten order, construction order or how `<<` rewrites a mapping.
"""

from dataclasses import dataclass, field
from functools import cache
from typing import Literal

import pytest
import yaml
from hypothesis import example, given, settings
from hypothesis import strategies as st

from gateway.registry import _UniqueKeyLoader

_MERGE_TAG = "tag:yaml.org,2002:merge"
_VALUE_TAG = "tag:yaml.org,2002:value"  # a plain `=` key in YAML 1.1


def reference_duplicates(text: str) -> list[str]:
    """Every duplicate in `text`'s mappings, as written, with each mapping node checked once.

    A key that is a `<<` merge (by tag) is skipped, except that two of them in one mapping are a
    duplicate. Keys are built with `SafeLoader`'s constructor, so YAML 1.1 equalities such as
    `yes`/`true`/`1`/`0x1` count the way PyYAML counts them. A `=` key is the string "=", as
    PyYAML's flatten makes it. A sequence or mapping key is reported as unhashable, since
    `SafeLoader` rejects it too.
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
        if key_node.tag == _VALUE_TAG and isinstance(key_node, yaml.ScalarNode):
            # PyYAML's flatten retags a YAML 1.1 `=` key as a string, so `=` is the key "=".
            key: object = key_node.value
        else:
            key = constructor.construct_object(key_node, deep=True)
        try:
            repeated = key in seen
            seen.add(key)
        except TypeError:  # a sequence or mapping key; SafeLoader rejects it as unhashable
            found.append(f"unhashable key {key!r}")
            continue
        if repeated:
            found.append(repr(key))
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
        pytest.param("a: 1\n=: 2\n", [], id="value-key-is-the-string-equals"),
        pytest.param("=: 1\n=: 2\n", ["'='"], id="value-key-repeated"),
        pytest.param("'=': 1\n=: 2\n", ["'='"], id="value-key-equals-quoted-equals"),
        pytest.param("? [a]\n: 1\n", ["unhashable key ['a']"], id="sequence-key"),
        pytest.param("? {a: 1}\n: 1\n", ["unhashable key {'a': 1}"], id="mapping-key"),
        pytest.param("&k a: 1\n*k : 2\n", ["'a'"], id="alias-key-repeats-its-anchor"),
        pytest.param("&k a: 1\nb: {*k : 2}\n", [], id="alias-key-in-another-mapping"),
        pytest.param("s: &s [{k: 1}, {k: 2}]\nuse: {<<: *s}\n", [], id="merge-an-anchored-sequence"),
        pytest.param("s: &s [{k: 1, k: 2}]\nuse: {<<: *s}\n", ["'k'"], id="duplicate-inside-merged-sequence"),
    ],
)
def test_reference_finds_exactly_the_duplicates_as_written(text: str, expected: list[str]) -> None:
    assert reference_duplicates(text) == expected


# --- the generator -------------------------------------------------------------------------------

# A small alphabet, so keys collide often. `yes`, `true`, `1` and `0x1` all construct equal to True;
# `'yes'` and `'<<'` are quoted strings, so neither is a bool nor a merge key; `=` is the string "=".
KEYS = ("a", "a", "b", "b", "c", "c", "d", "e", "yes", "true", "'yes'", "1", "0x1", "~", "'<<'", "=")
SCALARS = ("0", "1", "x", "true", "~")
MERGE_SPELLINGS = ("<<", "<<", "<<", "!!merge '<<'")
MAX_DEPTH = 3
MAX_PAIRS = 3


@cache
def _key_value(spelling: str) -> object:
    """What a key spelled `spelling` constructs to, so the generator can tell which keys collide."""
    loaded: dict[object, object] = yaml.load("{" + spelling + ": 0}", Loader=yaml.SafeLoader)
    return next(iter(loaded))


@dataclass
class _Doc:
    """One document's plan, and the anchors defined so far in text order.

    In a free document anything goes, so a mapping may hold several duplicates and `<<` keys. Other
    documents keep every mapping's keys distinct and merge at most once per mapping, except that the
    mapping numbered `defect_at`, in text order, gets the one `defect`. A document with one defect
    stops a loader that rejects it for some other reason from hiding a missed class.
    """

    free: bool
    defect: Literal["none", "key", "merge"]
    defect_at: int
    mappings: int = 0
    mapping_anchors: list[str] = field(default_factory=list[str])
    sequence_anchors: list[str] = field(default_factory=list[str])  # sequences of mappings only
    scalar_anchors: dict[str, object] = field(default_factory=dict[str, object])  # name -> value
    next_anchor: int = 0

    def new_anchor(self) -> str:
        self.next_anchor += 1
        return f"a{self.next_anchor}"

    def any_anchor(self) -> list[str]:
        return self.mapping_anchors + self.sequence_anchors + list(self.scalar_anchors)


def _key_spellings(doc: _Doc) -> list[tuple[str, object]]:
    """Every way to write a key here: the alphabet and aliases to the scalar anchors defined so far."""
    spellings = [(key, _key_value(key)) for key in KEYS]
    return spellings + [(f"*{name}", value) for name, value in doc.scalar_anchors.items()]


def _mapping(draw: st.DrawFn, doc: _Doc, depth: int) -> str:
    """A flow mapping, possibly anchored, whose entries are drawn in the order they are written."""
    defect = doc.defect if doc.mappings == doc.defect_at and not doc.free else "none"
    doc.mappings += 1
    anchor = doc.new_anchor() if draw(st.integers(0, 2)) == 2 else None
    if doc.free:
        # Nine in twenty mappings merge once and one in twenty merges twice.
        merges = draw(st.sampled_from((0,) * 10 + (1,) * 9 + (2,)))
    else:
        merges = 2 if defect == "merge" else draw(st.integers(0, 1))
    pairs = draw(st.integers(0, MAX_PAIRS))
    repeats = 1 if defect == "key" else 0
    kinds = draw(st.permutations(["merge"] * merges + ["pair"] * pairs + ["repeat"] * repeats))
    used: list[object] = []  # constructed keys so far; a list, since `1` and `True` must both count
    entries: list[str] = []
    for kind in kinds:
        if kind == "merge":
            merge = _merge_value(draw, doc, depth)
            if merge is not None:
                entries.append(f"{draw(st.sampled_from(MERGE_SPELLINGS))}: {merge}")
            continue
        # A repeat with nothing to repeat yet writes a fresh key first.
        if kind == "repeat" and not used and (fresh := _pair(draw, doc, depth, used, repeat=False)):
            entries.append(fresh)
        entry = _pair(draw, doc, depth, used, repeat=kind == "repeat")
        if entry:
            entries.append(entry)
    text = "{" + ", ".join(entries) + "}"
    if anchor is None:
        return text
    doc.mapping_anchors.append(anchor)  # usable only after the mapping is complete
    return f"&{anchor} {text}"


def _pair(draw: st.DrawFn, doc: _Doc, depth: int, used: list[object], *, repeat: bool) -> str:
    """One `key: value` entry. A repeat reuses a key in `used`; outside a free document, others don't."""
    spellings = _key_spellings(doc)
    if repeat:
        spellings = [(text, value) for text, value in spellings if value in used]
    elif not doc.free:
        spellings = [(text, value) for text, value in spellings if value not in used]
    if not spellings:
        return ""
    key, value = draw(st.sampled_from(spellings))
    used.append(value)
    # A `=` key stays unanchored: only flatten makes it a string, so SafeLoader fails on an alias
    # to it that is built as a value before its own mapping is flattened.
    if not key.startswith("*") and key != "=" and draw(st.integers(0, 4)) == 4:
        name = doc.new_anchor()
        doc.scalar_anchors[name] = value  # a later key in this mapping may already alias it
        key = f"&{name} {key}"
    return f"{key}: {_value(draw, doc, depth)}"


def _merge_source(draw: st.DrawFn, doc: _Doc, depth: int) -> str | None:
    """One mapping a `<<` may merge: an alias to a mapping anchor or an inline mapping."""
    can_inline = depth < MAX_DEPTH
    if doc.mapping_anchors and (not can_inline or draw(st.booleans())):
        return "*" + draw(st.sampled_from(doc.mapping_anchors))
    return _mapping(draw, doc, depth + 1) if can_inline else None


def _source_list(draw: st.DrawFn, doc: _Doc, depth: int) -> str | None:
    """A sequence of merge sources, which is sometimes anchored so a later `<<` can alias it."""
    anchor = doc.new_anchor() if draw(st.integers(0, 2)) == 2 else None
    sources = [_merge_source(draw, doc, depth) for _ in range(draw(st.integers(1, 3)))]
    present = [source for source in sources if source is not None]
    if not present:
        return None
    text = "[" + ", ".join(present) + "]"
    if anchor is None:
        return text
    doc.sequence_anchors.append(anchor)
    return f"&{anchor} {text}"


def _merge_value(draw: st.DrawFn, doc: _Doc, depth: int) -> str | None:
    # Each draw that adds structure shrinks toward the simpler choice, so a failure shrinks well.
    forms = ["one", "list"] + (["sequence alias"] if doc.sequence_anchors else [])
    match draw(st.sampled_from(forms)):
        case "one":
            return _merge_source(draw, doc, depth)
        case "list":
            return _source_list(draw, doc, depth)
        case _:
            return "*" + draw(st.sampled_from(doc.sequence_anchors))


def _value(draw: st.DrawFn, doc: _Doc, depth: int) -> str:
    choices = ["scalar", "scalar"]
    if doc.any_anchor():
        choices.append("alias")
    if depth < MAX_DEPTH:
        choices += ["mapping", "mapping", "sequence", "sources"]
    match draw(st.sampled_from(choices)):
        case "alias":
            return "*" + draw(st.sampled_from(doc.any_anchor()))
        case "mapping":
            return _mapping(draw, doc, depth + 1)
        case "sequence":
            items = [_value(draw, doc, depth + 1) for _ in range(draw(st.integers(0, 2)))]
            return "[" + ", ".join(items) + "]"
        case "sources":  # a sequence of mappings defined as a value, which a later `<<` may alias
            return _source_list(draw, doc, depth + 1) or "[]"
        case _:
            scalar = draw(st.sampled_from(SCALARS))
            if draw(st.integers(0, 4)) == 4:
                anchor = doc.new_anchor()
                doc.scalar_anchors[anchor] = _key_value(scalar)
                return f"&{anchor} {scalar}"
            return scalar


@st.composite
def yaml_documents(draw: st.DrawFn) -> str:
    """Small flow-style YAML documents full of anchors, aliases, merges and colliding keys.

    One document in four is free. The rest hold at most one defect, a repeated key or a repeated
    `<<`, in a mapping chosen by its position, and a third of them hold none.
    """
    free = draw(st.integers(0, 3)) == 3
    defect = draw(st.sampled_from(("none", "key", "merge")))
    doc = _Doc(free=free, defect=defect, defect_at=draw(st.integers(0, 4)))
    return _mapping(draw, doc, 0) + "\n"


# --- the property --------------------------------------------------------------------------------


# One pin per historical bug class, so each stays covered whatever the generator draws.
@example("{<<: {a: 0, a: 0}}\n")  # a duplicate inside a mapping reached only through `<<`
@example("{<<: {a: 0}, a: 0}\n")  # an explicit key overriding a merged one is legal
@example("{<<: {<<: {}, <<: {}}}\n")  # a repeated `<<` inside a merged mapping
@example("{a: &x {<<: {}, <<: {}}, <<: *x}\n")  # an anchor flattened before it is built
@example("{<<: &a1 {}, !!merge '<<': *a1}\n")  # a repeated `<<` in its tagged spelling
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
