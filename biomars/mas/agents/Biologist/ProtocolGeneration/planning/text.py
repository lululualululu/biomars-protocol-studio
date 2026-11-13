"""Text helpers shared only within protocol planning. No model calls or I/O."""

import re
from typing import Any, Dict, List, Optional, Tuple


def format_segment_analysis_for_prompt(
    segment_analysis_bundle: Optional[Dict[str, Any]],
) -> str:
    """Render the existing segment-summary format used by both preparation paths."""
    segment_analysis_text = ""
    if segment_analysis_bundle and segment_analysis_bundle.get("analyses"):
        analysis_lines = []
        for a in segment_analysis_bundle["analyses"]:
            content_types_str = ", ".join(a.get("content_types", []))
            suggested_uses_str = "; ".join(a.get("suggested_uses", [])[:3])
            key_entities_str = ", ".join(a.get("key_entities", [])[:5])
            analysis_lines.append(
                f"- [{a.get('order', 0):02d}] **{a.get('title', 'Untitled')}** (Level {a.get('level', 0)})\n"
                f"  Types: {content_types_str}\n"
                f"  Summary: {a.get('content_summary', '')}\n"
                f"  Uses: {suggested_uses_str}\n"
                f"  Entities: {key_entities_str}"
            )
        segment_analysis_text = "\n".join(analysis_lines)
    return segment_analysis_text


def locate_semantic_quote(
    source_chunk: str,
    quote: str,
    start_at: int = 0,
) -> Optional[Tuple[int, int, str]]:
    """Accept only text that can be mapped back to an exact source span."""
    candidate = str(quote or "").strip()
    if not candidate:
        return None
    start_at = max(0, int(start_at or 0))

    start = source_chunk.find(candidate, start_at)
    if start >= 0:
        end = start + len(candidate)
        return start, end, source_chunk[start:end]

    tokens = [token for token in re.split(r"\s+", candidate) if token]
    if not tokens:
        return None
    match = re.search(
        r"\s+".join(re.escape(token) for token in tokens),
        source_chunk[start_at:],
    )
    if not match:
        return None
    match_start = start_at + match.start()
    match_end = start_at + match.end()
    return match_start, match_end, source_chunk[match_start:match_end]


def markdown_heading_count(text: str) -> int:
    if not text:
        return 0
    return len(re.findall(r"(?m)^#{1,6}\s+\S", text))


def extract_view_headings(text: str, limit: int = 12) -> List[str]:
    headings: List[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#"):
            line = line.lstrip("#").strip()
        elif line.startswith("- **") and line.endswith("**"):
            line = line.strip("- ").strip("*").strip()
        else:
            continue
        if not line:
            continue
        if len(line) > 140:
            continue
        if any(token in line.lower() for token in ["figure", "reference", "copyright"]):
            continue
        if line not in headings:
            headings.append(line)
        if len(headings) >= limit:
            break
    return headings


def extract_selected_segment_ids_from_view(text: str) -> List[str]:
    ids: List[str] = []
    for comment in re.findall(r"<!--[^>]+-->", text or "", flags=re.IGNORECASE):
        match = re.search(r"segment_id:([^|>]+)", comment)
        if not match:
            continue
        seg_id = match.group(1).strip().strip('"')
        if seg_id and seg_id not in ids:
            ids.append(seg_id)
    return ids


def compact_segment_catalog(analyses: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    catalog: List[Dict[str, Any]] = []
    for entry in analyses:
        if not isinstance(entry, dict):
            continue
        catalog.append(
            {
                "segment_id": str(entry.get("segment_id") or ""),
                "order": entry.get("order"),
                "title": str(entry.get("title") or ""),
                "content_summary": str(entry.get("content_summary") or ""),
                "content_types": [str(x) for x in (entry.get("content_types") or []) if x][:5],
                "primary_view": str(entry.get("primary_view") or ""),
                "query_relevance": str(entry.get("query_relevance") or ""),
                "suggested_uses": [str(x) for x in (entry.get("suggested_uses") or []) if x][:4],
            }
        )
    return [item for item in catalog if item.get("segment_id")]


def extract_markdown_sections(text: str) -> List[Tuple[str, str]]:
    sections: List[Tuple[str, str]] = []
    current_heading: Optional[str] = None
    buffer: List[str] = []

    def flush() -> None:
        nonlocal current_heading, buffer
        body = "\n".join(buffer).strip()
        if current_heading or body:
            sections.append((current_heading or "Preamble", body))
        buffer = []

    for raw_line in (text or "").splitlines():
        stripped = raw_line.strip()
        if stripped.startswith("<!--"):
            continue
        if re.match(r"^#{2,6}\s+", stripped):
            flush()
            current_heading = re.sub(r"^#{2,6}\s*", "", stripped).strip()
            continue
        buffer.append(raw_line)

    flush()
    return [(heading, body) for heading, body in sections if heading or body]


def normalize_planner_input(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u00ad", "")
    text = re.sub(r"^\s*\d+\|\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"[]", "", text)
    text = text.replace("–", "-")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def normalize_for_match(text: str) -> str:
    if not text:
        return ""
    text = text.replace("–", "-")
    text = text.lower()
    text = re.sub(r"\s*-\s*", "-", text)
    text = re.sub(r"\s*,\s*", ",", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def strip_code_fences(text: str) -> str:
    """
    Robustly extract JSON from text, handling markdown fences and extra commentary.
    """
    text = text.strip()

    json_fence_pattern = r"```(?:json)?\s*(\{.*?\})\s*```"
    match = re.search(json_fence_pattern, text, re.DOTALL)
    if match:
        return match.group(1)

    start = text.find("{")
    end = text.rfind("}")

    if start != -1 and end != -1 and end > start:
        return text[start:end+1]

    return text
