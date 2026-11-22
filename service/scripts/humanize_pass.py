#!/usr/bin/env python3
"""Deterministic humanizer pass for the Layer B ``humanize`` tactic.

The humanize tactic asks a model to "write like a human"; this module enforces
the mechanical, context-free subset of the humanizer skill (Wikipedia's "Signs
of AI writing") that is safe to apply without reading the text: straight quotes,
no em/en dashes or double hyphens, common filler-phrase collapses, and the
unambiguous ``utilize`` -> ``use`` swap. Everything that needs judgment (voice,
rhythm, promotional language, passive voice, rule-of-three) lives in the
rewrite prompt; this pass only changes text where the edit is always correct.
"""

from __future__ import annotations

import re

# Lowercase replacements; capitalization is preserved from the matched text.
_PHRASE_SWAPS: tuple[tuple[str, str], ...] = (
    (r"\bin order to\b", "to"),
    (r"\bdue to the fact that\b", "because"),
    (r"\bat this point in time\b", "now"),
    (r"\bin the event that\b", "if"),
    (r"\bhas the ability to\b", "can"),
    (r"\bhave the ability to\b", "can"),
    (r"\bit is important to note that\b", ""),
    (r"\bit is worth noting that\b", ""),
    (r"\bdelve into\b", "explore"),
    (r"\bdelves into\b", "explores"),
    (r"\bdelved into\b", "explored"),
    (r"\bdelving into\b", "exploring"),
    # Trim stock filler and empty transitions; retain the substantive claim.
    (r"^\s*in today's fast-paced world,?\s*", ""),
    (r"^\s*in today’s fast-paced world,?\s*", ""),
    (r"\bit is essential to remember that\s*", ""),
    (r"\bit's essential to remember that\s*", ""),
    (r"\bit is worth noting that\s*", ""),
    (r"\bit's worth noting that\s*", ""),
    (r"\bplays a crucial role in\b", "is important to"),
    (r"\bplays an important role in\b", "is important to"),
    (r"\ba wide range of\b", "many"),
    (r"\bin conclusion,?\s*", ""),
    (r"\bmoreover,?\s*", ""),
    (r"\bfurthermore,?\s*", ""),
    (r"\badditionally,?\s*", ""),
    (r"\bin the realm of\b", "in"),
    (r"\bit is important to note that\b", ""),
    (r"\bit's important to note that\b", ""),
    (r"\bas an AI language model,?\s*", ""),
    (r"\bcertainly!?\s*here'?s\s*", ""),
    (r"\bi hope this helps!?\s*", ""),
    (r"\blet me know if you'd like me to expand\.?\s*", ""),
)

_WORD_SWAPS: dict[str, str] = {
    "utilize": "use",
    "utilizes": "uses",
    "utilized": "used",
    "utilizing": "using",
    "leverage": "use",
    "leverages": "uses",
    "leveraged": "used",
    "leveraging": "using",
}

_WORD_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in _WORD_SWAPS) + r")\b",
    re.IGNORECASE,
)


def _capitalize_like(matched: str, replacement: str) -> str:
    """Match the capitalization of *matched*'s first character."""
    if matched.isupper():
        return replacement.upper()
    return replacement.capitalize() if matched[0].isupper() else replacement


def _straighten_quotes(text: str) -> str:
    for ch in ("\u2018", "\u2019"):
        text = text.replace(ch, "'")
    for ch in ("\u201c", "\u201d"):
        text = text.replace(ch, '"')
    return text


def _replace_dashes(text: str) -> str:
    _DASH = re.compile(r"\s*[\u2014\u2013]\s*")
    _DBL_HYPHEN = re.compile(r"(\s*)--(\s*)")

    def _dash_sub(m: re.Match[str]) -> str:
        # Neighbors just beyond the matched whitespace: if both are digits the
        # dash is a numeric range (e.g. "2019 - 2020") and stays untouched.
        left = text[m.start() - 1] if m.start() > 0 else ""
        right = text[m.end()] if m.end() < len(text) else ""
        if left.isdigit() and right.isdigit():
            return m.group(0)
        return ", "

    def _dbl_sub(m: re.Match[str]) -> str:
        leading, trailing = m.group(1), m.group(2)
        hyphen_start = m.start() + len(leading)
        hyphen_end = m.end() - len(trailing)
        before = text[hyphen_start - 1] if hyphen_start > 0 else ""
        after = text[hyphen_end] if hyphen_end < len(text) else ""
        # A command-line option is `--word` where `--` directly precedes a word
        # char after a delimiter (or string start); never a prose double hyphen.
        if after.isalnum() and (not before or not before.isalnum()):
            return m.group(0)
        return ", "

    text = _DASH.sub(_dash_sub, text)
    text = _DBL_HYPHEN.sub(_dbl_sub, text)
    text = re.sub(r",\s*,\s*", ", ", text)  # collapse adjacent commas
    text = re.sub(r"\s*,\s*([.!?])", r"\1", text)  # "word, ." -> "word."
    return text


def _collapse_phrases(text: str) -> str:
    for pattern, repl in _PHRASE_SWAPS:
        text = re.sub(
            pattern,
            lambda m, r=repl: _capitalize_like(m.group(0), r),
            text,
            flags=re.IGNORECASE,
        )
    return text


def _swap_words(text: str) -> str:
    def _sub(match: re.Match[str]) -> str:
        word = match.group(0)
        return _capitalize_like(word, _WORD_SWAPS[word.lower()])

    return _WORD_RE.sub(_sub, text)


def humanize_pass(text: str) -> str:
    """Apply the deterministic humanizer transforms to *text*."""
    text = _straighten_quotes(text)
    text = _replace_dashes(text)
    text = _collapse_phrases(text)
    text = _swap_words(text)
    # Clean spacing left by removed filler and restore paragraph-start capitals.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"(?m)(^|[.!?][ \t]+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), text)
    return text
