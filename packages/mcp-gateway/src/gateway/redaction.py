"""PII (personally identifiable information) redaction: a pure function over JSON-shaped values.

The gateway applies it to tool results, resource reads and prompts (see `gateway.policies`).

E-mail matching rules:

- Local part: the whole run of RFC 5322 (Internet Message Format) atext characters (letters, digits,
  `!#$%&'*+/=?^_`{|}~-`, Unicode letters through `\\w`) and dots that ends at the `@`. The run is matched
  from its start, so the regex never starts inside it, which keeps matching linear. A run longer than the
  RFC's 64 characters is still redacted in full, and so are atext neighbours such as `?to=` in
  `?to=jane@example.com`: the redaction errs toward hiding too much of the local part.
- Domain: one or more labels of 1-63 word characters or hyphens, each followed by a dot, then a top-level
  domain (TLD) of 2-63 letters with no digits. Version specs such as `lodash@4.17.21` are therefore left
  alone.

Out of scope:

- IP-literal domains (`user@[10.0.0.1]`) and dotless hosts (`user@localhost`).
- Quoted local parts, labels longer than 63 characters, and percent-encoded `@` (`user%40example.com`).
- Punycode TLD tails: in `jane@example.xn--p1ai` the TLD match stops at `xn`, so `--p1ai` stays visible.
- TLDs glued to digits: `jane@example.com2` keeps the trailing `2`, and `jane@example.c0m` is not
  redacted at all.

`redact` recurses through str, dict values and list items. Dict keys are schema, not data, so the plan
leaves them alone. Tuples, sets and other containers are not recursed: the input is assumed JSON-shaped.
"""

import re
from typing import Any, cast

REDACTED_EMAIL = "[REDACTED:email]"

_ATEXT = r"\w!#$%&'*+/=?^`{|}~.-"
EMAIL = re.compile(
    rf"(?<![{_ATEXT}])[{_ATEXT}]++"  # local part: the full atext run, entered only at its start
    r"@(?:[\w-]{1,63}+\.)+"  # domain labels, each followed by a dot
    r"[^\W\d_]{2,63}+"  # TLD: letters only, so `pkg@1.2.3` does not match
)


def redact(value: Any) -> Any:
    """Return a copy of `value` with e-mail addresses replaced by `[REDACTED:email]`.

    Recurses through str, dict values and list items; every other type is returned unchanged.
    Dict keys are left alone.
    """
    if isinstance(value, str):
        return EMAIL.sub(REDACTED_EMAIL, value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in cast(dict[Any, Any], value).items()}
    if isinstance(value, list):
        return [redact(v) for v in cast(list[Any], value)]
    return value
