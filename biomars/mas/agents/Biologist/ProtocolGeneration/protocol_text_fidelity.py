"""Shared text-fidelity helpers for protocol Writer and Critic.

These helpers parse document structure and numeric notation only. They must not
contain query-, paper-, or experiment-specific correction rules.
"""

from __future__ import annotations

import re
from typing import List


_SUPERSCRIPT_TRANSLATION = str.maketrans(
    {
        "⁰": "0",
        "¹": "1",
        "²": "2",
        "³": "3",
        "⁴": "4",
        "⁵": "5",
        "⁶": "6",
        "⁷": "7",
        "⁸": "8",
        "⁹": "9",
        "⁺": "+",
        "⁻": "-",
    }
)


def strip_leading_source_ordinal(text: str) -> str:
    """Remove a source list label while preserving decimal quantities.

    Examples removed: ``9.``, ``1.1.4.``, ``**9.**`` and ``Step 9:``.
    A value such as ``1.5 mL`` is retained because it has no terminal list
    punctuation or explicit ``Step`` label.
    """

    value = str(text or "").strip()
    patterns = (
        r"^\s*\*\*\s*\d+(?:\.\d+)*[.)]?\s*\*\*\s+",
        r"^\s*(?:step\s+)?\d+(?:\.\d+)*[.):]\s+",
    )
    for pattern in patterns:
        cleaned = re.sub(pattern, "", value, count=1, flags=re.IGNORECASE)
        if cleaned != value:
            return cleaned.strip()
    return value


def _expand_superscript_exponents(text: str) -> str:
    superscript_run = re.compile(r"(?<=[0-9)])([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)")
    return superscript_run.sub(
        lambda match: "^" + match.group(1).translate(_SUPERSCRIPT_TRANSLATION),
        text or "",
    )


def _canonical_numeric_token(token: str) -> str:
    compact = re.sub(r"\s+", "", token or "").replace("×", "x").replace(",", "")
    compact = compact.replace("**", "^")

    scientific = re.fullmatch(
        r"(?P<mantissa>\d+(?:\.\d+)?)x10\^(?P<exponent>[+-]?\d+)",
        compact,
        flags=re.IGNORECASE,
    )
    if scientific:
        exponent = int(scientific.group("exponent"))
        return f"{scientific.group('mantissa')}e{exponent}"

    power = re.fullmatch(r"10\^(?P<exponent>[+-]?\d+)", compact)
    if power:
        return f"1e{int(power.group('exponent'))}"

    e_notation = re.fullmatch(
        r"(?P<mantissa>\d+(?:\.\d+)?)[eE](?P<exponent>[+-]?\d+)",
        compact,
    )
    if e_notation:
        return f"{e_notation.group('mantissa')}e{int(e_notation.group('exponent'))}"

    return compact


def numeric_fact_tokens(text: str) -> List[str]:
    """Return comparable numeric facts while ignoring citations/list ordinals.

    Scientific-notation spellings such as ``10⁴``, ``10^4`` and ``1e4`` are
    canonicalized. Compact ``104`` remains distinct because treating it as an
    exponent requires semantic evidence rather than typography parsing.
    """

    value = strip_leading_source_ordinal(text)
    value = re.sub(
        r"\[\s*\d+(?:\s*[-–,]\s*\d+)*\s*\]",
        "",
        value,
    )
    value = _expand_superscript_exponents(value)
    pattern = re.compile(
        r"\d+(?:\.\d+)?\s*[x×]\s*10\s*(?:\^|\*\*)\s*[+-]?\d+"
        r"|\d+(?:\.\d+)?[eE][+-]?\d+"
        r"|10\s*(?:\^|\*\*)\s*[+-]?\d+"
        r"|(?<![A-Za-z])\d+(?:[.,]\d+)*"
    )
    return [_canonical_numeric_token(match.group(0)) for match in pattern.finditer(value)]
