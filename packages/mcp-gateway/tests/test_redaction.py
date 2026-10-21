import random
import string
import time

import pytest

from gateway.redaction import REDACTED_EMAIL, redact


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("mail jane.doe@example.com now", "mail [REDACTED:email] now", id="dotted-local"),
        pytest.param("a+tag@sub.example.co.uk", "[REDACTED:email]", id="plus-and-subdomains"),
        pytest.param("x@a.io, y@b.io", "[REDACTED:email], [REDACTED:email]", id="several"),
        pytest.param("ping @ops.team on chat", "ping @ops.team on chat", id="handle-not-email"),
        pytest.param("", "", id="empty"),
    ],
)
def test_redacts_email_addresses_in_strings(text: str, expected: str) -> None:
    assert redact(text) == expected


def test_recurses_through_dicts_and_lists_and_keeps_other_types() -> None:
    value = {
        "primary": "ops@example.com",
        "nested": [{"who": "a@b.io"}, "c@d.io", 3, None, True, 1.5],
        "count": 2,
    }
    assert redact(value) == {
        "primary": REDACTED_EMAIL,
        "nested": [{"who": REDACTED_EMAIL}, REDACTED_EMAIL, 3, None, True, 1.5],
        "count": 2,
    }


def test_is_pure() -> None:
    original = {"k": ["a@b.io"]}
    redact(original)
    assert original == {"k": ["a@b.io"]}


def test_dict_keys_are_left_alone() -> None:
    assert redact({"a@b.io": "x"}) == {"a@b.io": "x"}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param("ask o'brien@example.com", "ask [REDACTED:email]", id="apostrophe-local"),
        pytest.param(
            "x!#$%&'*+/=?^_`{|}~-y@example.com", "[REDACTED:email]", id="every-atext-symbol-in-local"
        ),
        pytest.param("JANE.DOE@EXAMPLE.COM", "[REDACTED:email]", id="uppercase"),
        pytest.param("Ops@Example.Org", "[REDACTED:email]", id="mixed-case"),
        pytest.param("josé.müller@example.de", "[REDACTED:email]", id="unicode-local"),
        pytest.param("<a href='mailto:jane@example.com'>", "<a href='mailto:[REDACTED:email]'>", id="mailto"),
        pytest.param("see ?to=jane@example.com&x=1", "see [REDACTED:email]&x=1", id="url-query"),
        pytest.param("Escalate to lead@example.com.", "Escalate to [REDACTED:email].", id="trailing-period"),
        pytest.param("npm i lodash@4.17.21", "npm i lodash@4.17.21", id="npm-version"),
        pytest.param("pkg@1.2.3", "pkg@1.2.3", id="version-spec"),
        pytest.param("react@18.3.1-rc.1", "react@18.3.1-rc.1", id="prerelease-version"),
        pytest.param("x@a.b", "x@a.b", id="one-letter-tld"),
    ],
)
def test_redaction_edge_cases(text: str, expected: str) -> None:
    assert redact(text) == expected


@pytest.mark.parametrize(
    "address",
    [
        "jane.doe@example.com",
        "ops-lead@example.com",
        "lead@example.com",
        "dana.lee@example.com",
        "sam.ortiz@example.com",
        "priya.n@example.com",
    ],
)
def test_plan_fixture_addresses_are_fully_redacted(address: str) -> None:
    assert redact(f"contact {address}: now") == "contact [REDACTED:email]: now"


_N = 200_000
_ADVERSARIAL = {
    "no-at": "a" * _N,
    "dots-no-at": "a." * (_N // 2),
    "only-at": "@" * _N,
    "a-at": "a@" * (_N // 2),
    "a-at-dot": "a@a." * (_N // 4),
    "one-at-long-domain": "x@" + "a." * (_N // 2),
    "long-labels": ("x@" + "b" * 70 + ".") * (_N // 73),
    "digit-tlds": "p@1.2.3 " * (_N // 8),
    "hyphen-labels": ("a@" + "a-" * 40 + " ") * (_N // 83),
}


def test_adversarial_inputs_run_in_linear_time() -> None:
    for name, text in _ADVERSARIAL.items():
        started = time.perf_counter()
        redact(text)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.5, f"{name}: {elapsed:.2f}s for {len(text)} characters"


_LOCAL_ALPHABET = string.ascii_letters + string.digits + "!#$%&'*+/=?^_`{|}~-." + "éüßжΩ"
_LABEL_ALPHABET = string.ascii_lowercase + string.digits + "-"
_SEPARATORS = ' \n\t<>()[],;:"'
_FILLER = string.ascii_letters + string.digits + " .,;:!?-_()[]<>/\"'\n" + "éü"


def test_random_addresses_in_random_text_are_fully_redacted() -> None:
    rng = random.Random(20261014)
    for _ in range(400):
        local = "".join(rng.choices(_LOCAL_ALPHABET, k=rng.randint(1, 64)))
        labels = [
            "".join(rng.choices(_LABEL_ALPHABET, k=rng.randint(1, 20))) for _ in range(rng.randint(1, 3))
        ]
        tld = "".join(rng.choices(string.ascii_letters, k=rng.randint(2, 6)))
        address = f"{local}@{'.'.join(labels)}.{tld}"
        before = "".join(rng.choices(_FILLER, k=rng.randint(0, 40))) + rng.choice(_SEPARATORS)
        after = rng.choice(_SEPARATORS) + "".join(rng.choices(_FILLER, k=rng.randint(0, 40)))
        assert redact(before + address + after) == before + REDACTED_EMAIL + after, address
