"""Extract source Methods and organize bounded evidence chunks."""
from biomars.mas.assets.protocol_generation.prompts.research_source_content import SEG_SYSTEM_25, SEG_USER_TEMPLATE_33

from typing import Any, Callable, Dict, List, Tuple, Optional
from logging import Logger
import json
import os
import re

from ..protocol_schemas import DomainPack, EvidenceItem


FULLTEXT_EVIDENCE_CHUNK_SELECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "selected_chunk_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "reason": {"type": "string"},
    },
    "required": ["selected_chunk_ids", "reason"],
}


SEG_SYSTEM = (
    SEG_SYSTEM_25
)


SEG_USER_TEMPLATE = SEG_USER_TEMPLATE_33


async def extract_methods_text(
    raw_fulltext_md: str,
    use_llm_fallback: bool,
    max_lines: int,
    *,
    segment_methods_with_llm: Callable[..., Any],
    find_methods_by_headings: Any,
    normalize_markdown: Any,
) -> Tuple[str, Dict[str, Any], str]:
    normalized = normalize_markdown(raw_fulltext_md)
    lines = normalized.splitlines()
    n = len(lines)

    region = find_methods_by_headings(normalized)
    mode: str
    start_idx: int
    end_idx: int

    if region is not None:
        start_idx, end_idx = region
        mode = "heading_heuristic"
    else:
        if use_llm_fallback:
            start_idx, end_idx = await segment_methods_with_llm(
                normalized,
                max_lines=max_lines,
            )
            mode = "llm_line_segmentation"
        else:
            start_idx, end_idx = 0, n
            mode = "fallback_full"

    if n == 0 or start_idx >= end_idx:
        extracted = normalized
    else:
        extracted = "\n".join(lines[start_idx:end_idx]).strip() + "\n"

    report: Dict[str, Any] = {
        "mode": mode,
        "line_start": start_idx,
        "line_end_exclusive": end_idx,
        "total_lines": n,
    }
    return extracted, report, normalized


async def segment_methods_with_llm(
    normalized: str,
    max_lines: int = 1200,
    *,
    call_model: Callable[..., Any],
    SEG_SYSTEM: Any,
    SEG_USER_TEMPLATE: Any,
) -> Tuple[int, int]:
    lines = normalized.splitlines()
    n = len(lines)
    if n == 0:
        return 0, 0

    use_n = min(n, max_lines)
    numbered_lines = [f"L{i+1}: {lines[i]}" for i in range(use_n)]
    numbered_doc = "\n".join(numbered_lines)
    user = SEG_USER_TEMPLATE.format(numbered_doc=numbered_doc)

    response = await call_model(
        system_prompt=SEG_SYSTEM,
        prompt=user,
    )

    m_start = re.search(r"START\s*=\s*(\d+)", response or "", re.IGNORECASE)
    m_end = re.search(r"END\s*=\s*(\d+)", response or "", re.IGNORECASE)

    if not (m_start and m_end):
        start_line = 1
        end_line = use_n
    else:
        start_line = int(m_start.group(1))
        end_line = int(m_end.group(1))

    if start_line < 1:
        start_line = 1
    if end_line < start_line:
        end_line = start_line
    if end_line > use_n:
        end_line = use_n

    start_idx = start_line - 1
    end_idx = end_line
    return start_idx, end_idx


async def build_methods_chunks(
    methods_text: str,
    task_query: str = '',
    *,
    derive_analysis_markers: Callable[..., Any],
    extract_day_ranges: Callable[..., Any],
    split_line_by_analysis_markers: Callable[..., Any],
    normalize_markdown: Any,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    if not methods_text:
        return [], []
    normalized = normalize_markdown(methods_text)
    lines = normalized.splitlines()
    if not lines:
        return [], []

    chunks: List[Dict[str, Any]] = []
    current_lines: List[str] = []
    current_heading: Optional[str] = None
    current_is_analysis: Optional[bool] = None
    analysis_markers = await derive_analysis_markers(
        methods_text,
        task_query=task_query,
    )

    def is_heading_line(idx: int, line: str) -> bool:
        stripped = line.strip()
        if not stripped:
            return False
        if stripped.startswith("#"):
            return True
        if len(stripped) > 90:
            return False
        prev_blank = True if idx == 0 else (lines[idx - 1].strip() == "")
        next_blank = True if idx == len(lines) - 1 else (lines[idx + 1].strip() == "")
        if (prev_blank or next_blank) and not (stripped.endswith(".") and len(stripped) > 25):
            return True
        return False

    def flush_chunk(is_analysis: Optional[bool] = None):
        if not current_lines:
            return
        body = "\n".join(current_lines).strip()
        if not body:
            return
        heading = current_heading or ""
        text = f"{heading}\n{body}" if heading else body
        timepoints = extract_day_ranges(text)
        chunk_id = f"chunk_{len(chunks) + 1:02d}"
        chunks.append(
            {
                "chunk_id": chunk_id,
                "heading": heading,
                "text": text,
                "timepoints": timepoints,
                "is_analysis": bool(is_analysis),
            }
        )

    for i, line in enumerate(lines):
        if is_heading_line(i, line):
            flush_chunk(current_is_analysis)
            current_lines = []
            current_heading = line.strip().lstrip("#").strip()
            current_is_analysis = None
            continue
        if line.strip() == "":
            continue
        for group_text, group_is_analysis in split_line_by_analysis_markers(
            line,
            analysis_markers,
        ):
            if group_is_analysis != current_is_analysis and current_lines:
                flush_chunk(current_is_analysis)
                current_lines = []
            current_is_analysis = group_is_analysis
            if group_text:
                current_lines.append(group_text.rstrip())

    flush_chunk(current_is_analysis)
    return chunks, analysis_markers


async def select_targeted_fulltext_evidence(
    item: EvidenceItem,
    domain_pack: DomainPack,
    recovery_plan: Optional[Dict[str, Any]],
    unresolved_facets: List[str],
    *,
    call_model: Callable[..., Any],
    FULLTEXT_EVIDENCE_CHUNK_SELECTION_SCHEMA: Any,
) -> Tuple[str, Dict[str, Any]]:
    """Select source chunks for one bounded reread before dropping a full-text anchor."""
    meta = item.meta or {}
    chunks = [
        chunk
        for chunk in (meta.get("methods_chunks") or [])
        if isinstance(chunk, dict)
        and str(chunk.get("chunk_id") or "").strip()
        and str(chunk.get("text") or "").strip()
    ]
    if not chunks:
        return "", {"attempted": False, "reason": "no_methods_chunks"}

    def clip(text: str, limit: int) -> str:
        text = str(text or "").strip()
        if len(text) <= limit:
            return text
        head = max(1, int(limit * 0.7))
        return f"{text[:head]}\n...\n{text[-(limit - head):]}"[:limit]

    recovery_plan = recovery_plan or {}
    preview_limit = max(180, min(650, int(9000 / len(chunks))))
    catalogue = [
        {
            "chunk_id": str(chunk.get("chunk_id") or ""),
            "heading": str(chunk.get("heading") or ""),
            "source_preview": clip(chunk.get("text"), preview_limit),
        }
        for chunk in chunks
    ]

    prompt = f"""
        Select source chunks for a targeted reread of one biological protocol paper.

        User request:
        {domain_pack.task.query}

        Central operation:
        {recovery_plan.get("central_operation") or ""}

        Previously unresolved required facets:
        {unresolved_facets}

        Candidate source chunks:
        {catalogue}

        Select at most four chunk IDs whose supplied source previews are most likely to contain
        direct evidence for the unresolved facets and their relationship to the requested workflow.
        Select only IDs present in the candidate list. Do not infer missing facts or write protocol
        content. Return an empty list when none of the source previews is relevant.
        """
    try:
        response = await call_model(
            prompt=prompt,
            system_prompt=(
                "You retrieve evidence spans from supplied scientific source text. "
                "Return only valid JSON matching the requested schema."
            ),
            schema=FULLTEXT_EVIDENCE_CHUNK_SELECTION_SCHEMA,
            temperature=0.0,
        )
    except Exception as exc:
        return "", {
            "attempted": True,
            "reason": f"chunk_selection_failed: {exc}",
        }

    if isinstance(response, str):
        try:
            response = json.loads(response)
        except (TypeError, ValueError):
            response = None
    if isinstance(response, list):
        response = {
            "selected_chunk_ids": response,
            "reason": "model_returned_chunk_id_list",
        }
    if not isinstance(response, dict):
        return "", {
            "attempted": True,
            "reason": f"invalid_chunk_selection:{type(response).__name__}",
        }

    chunk_by_id = {str(chunk.get("chunk_id") or ""): chunk for chunk in chunks}
    selected_ids: List[str] = []
    for raw_id in response.get("selected_chunk_ids") or []:
        chunk_id = str(raw_id).strip()
        if chunk_id in chunk_by_id and chunk_id not in selected_ids:
            selected_ids.append(chunk_id)
        if len(selected_ids) >= 4:
            break
    if not selected_ids:
        return "", {
            "attempted": True,
            "selected_chunk_ids": [],
            "reason": str(response.get("reason") or "no_relevant_chunks"),
        }

    total_limit = 6000
    per_chunk_limit = max(1200, int(total_limit / len(selected_ids)))
    evidence_parts = []
    for chunk_id in selected_ids:
        chunk = chunk_by_id[chunk_id]
        heading = str(chunk.get("heading") or "")
        excerpt = clip(chunk.get("text"), per_chunk_limit)
        evidence_parts.append(f"[{chunk_id}] {heading}\n{excerpt}".strip())

    return "\n\n".join(evidence_parts)[:total_limit], {
        "attempted": True,
        "selected_chunk_ids": selected_ids,
        "reason": str(response.get("reason") or ""),
    }


async def derive_analysis_markers(
    methods_text: str,
    task_query: str = '',
    *,
    call_model: Callable[..., Any],
    derive_analysis_markers_from_text: Callable[..., Any],
    logger: Logger,
) -> List[str]:
    max_chars = int(os.getenv("METHODS_ANALYSIS_MARKERS_MAX_CHARS", "12000"))
    excerpt = methods_text[:max_chars]
    markers: List[str] = []
    prompt = (
        "You are analyzing a Methods section to identify analysis/characterization language.\n"
        "Return a JSON object whose analysis_markers array contains only concise phrases that "
        "signal analysis steps (e.g., specific staining, imaging, assays, sequencing, antibody "
        "incubations). Avoid generic words like 'cells' or 'culture'.\n"
        "Keep phrases short (1-4 words), and include terms that appear in the text.\n\n"
        f"Task context: {task_query}\n\n"
        f"Methods excerpt:\n{excerpt}\n"
    )
    schema = {
        "type": "object",
        "properties": {
            "analysis_markers": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["analysis_markers"],
    }
    try:
        response = await call_model(
            prompt=prompt,
            system_prompt="You are a scientific methods analyzer returning valid JSON.",
            schema=schema,
            temperature=0.0,
        )
        if isinstance(response, dict):
            markers = response.get("analysis_markers", []) or []
    except Exception as e:
        logger.warning(f"Analysis marker inference failed, falling back to regex: {e}")

    if not markers:
        markers = derive_analysis_markers_from_text(methods_text)

    cleaned: List[str] = []
    for m in markers:
        if not m:
            continue
        s = " ".join(str(m).strip().lower().split())
        if len(s) < 3:
            continue
        cleaned.append(s)
    # de-dup while preserving order
    seen = set()
    unique = []
    for m in cleaned:
        if m in seen:
            continue
        seen.add(m)
        unique.append(m)
    return unique[:50]


def derive_analysis_markers_from_text(methods_text: str) -> List[str]:
    if not methods_text:
        return []
    text = methods_text.lower()
    markers: List[str] = []
    # Capture "anti-<target>" style antibodies
    for m in re.finditer(r"\banti[-\s]?([a-z0-9\-]+)\b", text):
        target = m.group(1).strip()
        if target:
            markers.append(f"anti-{target}")
            markers.append(f"{target} antibody")
    # Capture "<X> staining" or "immunostaining"
    for m in re.finditer(r"\b([a-z0-9\-]+)\s+staining\b", text):
        markers.append(f"{m.group(1)} staining")
    for m in re.finditer(r"\b([a-z0-9\-]+)\s+immunostaining\b", text):
        markers.append(f"{m.group(1)} immunostaining")
    # Generic assay patterns (lightweight)
    for m in re.finditer(r"\b([a-z0-9\-]+)\s+pcr\b", text):
        markers.append(f"{m.group(1)} pcr")
    for m in re.finditer(r"\b([a-z0-9\-]+)-seq\b", text):
        markers.append(f"{m.group(1)}-seq")
    return markers


def split_line_by_analysis_markers(
    line: str,
    analysis_markers: List[str],
) -> List[Tuple[str, bool]]:
    import re
    line = line.strip()
    if not line:
        return []
    if not analysis_markers:
        return [(line, False)]
    markers = [m for m in analysis_markers if m]
    if not markers:
        return [(line, False)]

    sentences = re.split(r"(?<=[\.\!\?])\s+", line)
    groups: List[Tuple[str, bool]] = []
    current: List[str] = []
    current_is_analysis: Optional[bool] = None

    def is_analysis_sentence(s: str) -> bool:
        s_low = s.lower()
        for mk in markers:
            if mk in s_low:
                return True
        return False

    for sent in sentences:
        s = sent.strip()
        if not s:
            continue
        flag = is_analysis_sentence(s)
        if current_is_analysis is None:
            current_is_analysis = flag
        elif flag != current_is_analysis:
            groups.append((" ".join(current).strip(), current_is_analysis))
            current = []
            current_is_analysis = flag
        current.append(s)

    if current:
        groups.append((" ".join(current).strip(), bool(current_is_analysis)))

    return groups


def extract_day_ranges(text: str) -> List[Dict[str, Any]]:
    if not text:
        return []
    patterns = [
        r"\bfrom\s*(?:day|d)\s*(\d+)\s*(?:to|[-–])\s*(?:day|d)?\s*(\d+)\b",
        r"\b(?:day|d)\s*(\d+)\s*(?:to|[-–])\s*(?:day|d)?\s*(\d+)\b",
        r"\b(?:day|d)\s*(\d+)\s*\+\b",
        r"\bfrom\s*(?:day|d)\s*(\d+)\s*onward\b",
        r"\b(?:day|d)\s*(\d+)\b",
    ]
    ranges: List[Dict[str, Any]] = []
    used_spans: List[Tuple[int, int]] = []

    def add_range(start: int, end: Optional[int], label: str, span: Tuple[int, int]):
        ranges.append({"start_day": start, "end_day": end, "label": label})
        used_spans.append(span)

    for pat in patterns[:2]:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            start = int(m.group(1))
            end = int(m.group(2))
            add_range(start, end, m.group(0), m.span())

    for pat in patterns[2:4]:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            start = int(m.group(1))
            add_range(start, None, m.group(0), m.span())

    def span_overlaps(span: Tuple[int, int]) -> bool:
        for used in used_spans:
            if not (span[1] <= used[0] or span[0] >= used[1]):
                return True
        return False

    for m in re.finditer(patterns[4], text, flags=re.IGNORECASE):
        if span_overlaps(m.span()):
            continue
        day = int(m.group(1))
        ranges.append({"start_day": day, "end_day": day, "label": m.group(0)})

    return ranges
