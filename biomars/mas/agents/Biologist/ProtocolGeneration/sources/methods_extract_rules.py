"""
Deterministic Methods/Protocol extraction helpers.

This module provides a fast, rule-based extractor for Methods/Protocol sections
without using any LLM. It is intended to be used by agents that can optionally
fallback to an LLM if the rules fail.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple


STOP_HEADINGS = {
    "representative results",
    "anticipated results",
    "results",
    "result",
    "discussion",
    "conclusion",
    "conclusions",
    "acknowledgments",
    "acknowledgements",
    "funding",
    "references",
    "bibliography",
    "supplementary material",
    "supplementary materials",
}

START_HEADINGS = {
    "materials and methods",
    "material and methods",
    "materials & methods",
    "materials",
    "reagents",
    "equipment",
    "methods",
    "method",
    "procedure",
    "procedures",
    "experimental procedures",
    "experimental procedure",
    "experimental protocol",
    "study protocol",
    "study design",
    "laboratory methods",
    "laboratory protocol",
    "lab protocol",
}


def _normalize_heading_line(line: str) -> str:
    """
    Normalize a potential heading line for matching.
    """
    s = line.strip()
    while s.startswith("#"):
        s = s[1:].lstrip()
    # Strip common markdown emphasis/code markers so headings like "**PROCEDURE**"
    # match the canonical heading vocabulary.
    s = re.sub(r"[`*_]+", "", s)
    while s and (s[0].isdigit() or s[0] in " .:-–—)*•]("):
        s = s[1:]
    s = s.strip()
    s = re.sub(r"[`*_]+", "", s).strip()
    while s and s[-1] in ":. ":
        s = s[:-1]
    return " ".join(s.lower().split())


def normalize_markdown(raw: str) -> str:
    """
    Deterministic normalization:
    - unify newlines
    - strip BOM/zero-width chars
    - join broken lines inside paragraphs
    - keep heading-like lines separate
    """
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = raw.replace("\u200b", "").replace("\ufeff", "")

    lines = raw.split("\n")
    out: List[str] = []
    buf: List[str] = []

    def flush_buf():
        if buf:
            out.append(" ".join(buf).strip())
            buf.clear()

    n = len(lines)
    for i, line in enumerate(lines):
        s = line.rstrip()
        if s.strip() == "":
            flush_buf()
            out.append("")
            continue

        prev_blank = True if i == 0 else (lines[i - 1].strip() == "")
        next_blank = True if i == n - 1 else (lines[i + 1].strip() == "")

        stripped = s.strip()
        is_heading_like = False
        norm = _normalize_heading_line(stripped)
        
        if stripped.startswith("#"):
            is_heading_like = True
        elif norm in START_HEADINGS or norm in STOP_HEADINGS or norm == "introduction":
            is_heading_like = True
        elif len(stripped) <= 90 and (prev_blank or next_blank):
            if not (stripped.endswith(".") and len(stripped) > 25):
                is_heading_like = True

        if is_heading_like:
            flush_buf()
            out.append(stripped)
        else:
            t = stripped
            if not buf:
                buf.append(t)
            else:
                prev = buf[-1]
                if prev.endswith("-") and t and t[0].isalnum():
                    buf[-1] = prev[:-1] + t
                elif t and t[0].islower() and prev and prev[-1].islower() and len(t) < 20:
                    buf[-1] = prev + t
                else:
                    buf.append(t)

    flush_buf()

    cleaned: List[str] = []
    blank_run = 0
    for line in out:
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 1:
                cleaned.append("")
        else:
            blank_run = 0
            cleaned.append(line)

    return "\n".join(cleaned).strip() + "\n"


def find_methods_by_headings(normalized: str) -> Optional[Tuple[int, int]]:
    """
    Find Methods/Protocol region using only headings.

    - Prefer headings after "Introduction" if present.
    - Choose candidate with the largest span before next STOP heading.
    - If span too small (<3 lines), return None.
    """
    lines = normalized.splitlines()
    n = len(lines)
    if n == 0:
        return None

    intro_idx: Optional[int] = None
    for i, line in enumerate(lines):
        norm = _normalize_heading_line(line)
        if norm == "introduction":
            intro_idx = i
            break

    candidates: List[int] = []
    for i, line in enumerate(lines):
        norm = _normalize_heading_line(line)
        if norm in START_HEADINGS:
            candidates.append(i)

    if not candidates:
        return None

    if intro_idx is not None:
        post_intro = [i for i in candidates if i >= intro_idx]
        if post_intro:
            candidates = post_intro

    best_start: Optional[int] = None
    best_stop: Optional[int] = None
    best_span: int = 0

    for start_idx in candidates:
        stop_idx = n
        for j in range(start_idx + 1, n):
            norm_j = _normalize_heading_line(lines[j])
            if norm_j in STOP_HEADINGS:
                stop_idx = j
                break
        span = stop_idx - start_idx
        if span > best_span:
            best_span = span
            best_start = start_idx
            best_stop = stop_idx

    if best_start is None or best_stop is None:
        return None

    if best_span < 3:
        return None

    return best_start, best_stop


def extract_methods_region(markdown_text: str) -> Tuple[str, str]:
    normalized = normalize_markdown(markdown_text or "")
    region = find_methods_by_headings(normalized)
    if not region:
        return normalized, "fallback_full"
    start_idx, end_idx = region
    lines = normalized.splitlines()
    start_idx = max(0, min(start_idx, len(lines)))
    end_idx = max(start_idx, min(end_idx, len(lines)))
    extracted = "\n".join(lines[start_idx:end_idx]).strip() + "\n"
    if extracted.strip():
        return extracted, "heading_heuristic"
    return normalized, "fallback_full"
