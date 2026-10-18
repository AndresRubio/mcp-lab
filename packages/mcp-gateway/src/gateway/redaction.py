"""PII redaction for everything the gateway returns: a pure function over JSON-shaped values."""

import re
from typing import Any, cast

REDACTED_EMAIL = "[REDACTED:email]"
EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def redact(value: Any) -> Any:
    """Return a copy of `value` with e-mail addresses replaced by `[REDACTED:email]`.

    Recurses through str, dict values and list items; every other type is returned unchanged.
    Dict keys are schema, not data, so they are left alone.
    """
    if isinstance(value, str):
        return EMAIL.sub(REDACTED_EMAIL, value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in cast(dict[Any, Any], value).items()}
    if isinstance(value, list):
        return [redact(v) for v in cast(list[Any], value)]
    return value
