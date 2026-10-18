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
