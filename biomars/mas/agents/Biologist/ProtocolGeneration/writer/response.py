"""Parse Writer responses and preserve source facts through normalization audits."""

from typing import Any, Callable, Dict, List, Optional, Set
from logging import Logger
import json
import re
from collections import Counter

from ..protocol_schemas import ProtocolPlan


def safe_parse_json(
    text: str,
    *,
    _extract_balanced_json_substring: Any,
    logger: Logger,
) -> Any:
    """
        Robustly extracts and parses JSON from LLM output, handling:
        1) Markdown code blocks (```json ... ```)
        2) Extra prose before/after JSON
        3) NBSP / zero-width chars / smart quotes
        4) Trailing commas and JS-style comments (light cleanup)
        Returns {} or [] on failure (never None).
        """
    if not text:
        return {}

    t = text.strip()

    # 1) Strip Markdown code fences
    import re
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", t, flags=re.DOTALL | re.IGNORECASE)
    if m:
        t = m.group(1).strip()

    # 2) Normalize common bad characters
    t = (
        t.replace("\u00a0", " ")   # NBSP -> normal space
         .replace("\u200b", "")   # zero-width space
         .replace("\ufeff", "")   # BOM
         .replace("“", '"').replace("”", '"')
         .replace("’", "'")
    )

    # 3) Extract balanced JSON substring (object or array)
    t = _extract_balanced_json_substring(t).strip()

    # 4) Remove JS-style comments + trailing commas
    t = re.sub(r"//.*?$", "", t, flags=re.MULTILINE)
    t = re.sub(r"/\*.*?\*/", "", t, flags=re.DOTALL)
    t = re.sub(r",\s*([}\]])", r"\1", t)

    # 5) Parse
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON Parse Error: {e}. Raw head: {t[:120]!r}")
        # Heuristic empty type
        if t.lstrip().startswith("["):
            return []
        return {}


def parse_writer_payload(
    response: str,
    expected_step_ids: Set[str],
    render_trace: Optional[Dict[str, Any]] = None,
    *,
    safe_parse_json: Callable[..., Any],
    sanitize_step_rewrite: Callable[..., Any],
    logger: Logger,
) -> Dict[str, Any]:
    """Parse the bounded Writer response and discard out-of-contract nodes."""
    payload = safe_parse_json(response)
    if not isinstance(payload, dict):
        if render_trace is not None:
            render_trace["llm_response_valid"] = False
        return {}

    raw_step_map = payload.get("step_text_by_id") or {}
    if isinstance(raw_step_map, list):
        raw_step_map = {
            str(item.get("step_id") or "").strip(): item.get("rendered_text")
            for item in raw_step_map
            if isinstance(item, dict)
        }
    if not isinstance(raw_step_map, dict):
        raw_step_map = {}

    raw_step_ids = [
        str(step_id or "").strip()
        for step_id in raw_step_map
        if str(step_id or "").strip()
    ]
    unexpected_step_ids = [
        step_id for step_id in raw_step_ids if step_id not in expected_step_ids
    ]
    leaked_plan_id_step_ids: List[str] = []

    step_text_by_id: Dict[str, str] = {}
    for raw_step_id, raw_text in raw_step_map.items():
        step_id = str(raw_step_id or "").strip()
        if step_id not in expected_step_ids or not isinstance(raw_text, str):
            continue
        cleaned = sanitize_step_rewrite(raw_text)
        leaks_plan_id = any(
            re.search(
                rf"(?<![A-Za-z0-9_.:-]){re.escape(expected_id)}"
                rf"(?![A-Za-z0-9_.:-])",
                cleaned,
            )
            for expected_id in expected_step_ids
        )
        if leaks_plan_id:
            leaked_plan_id_step_ids.append(step_id)
            logger.warning(
                "Discarding Writer prose for %s because it exposes an internal Plan ID.",
                step_id,
            )
            continue
        if cleaned:
            step_text_by_id[step_id] = cleaned

    normalization_candidates: List[Dict[str, str]] = []
    raw_candidates = payload.get("normalization_candidates") or []
    if isinstance(raw_candidates, list):
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            step_id = str(item.get("step_id") or "").strip()
            if step_id not in expected_step_ids:
                continue
            normalization_candidates.append(
                {
                    "step_id": step_id,
                    "source_fragment": str(item.get("source_fragment") or "").strip(),
                    "normalized_fragment": str(item.get("normalized_fragment") or "").strip(),
                    "category": str(item.get("category") or "").strip(),
                    "reason": str(item.get("reason") or "").strip(),
                }
            )

    if render_trace is not None:
        render_trace.update(
            {
                "llm_response_valid": True,
                "llm_raw_step_ids": raw_step_ids,
                "unexpected_step_ids": unexpected_step_ids,
                "leaked_plan_id_step_ids": leaked_plan_id_step_ids,
                "llm_returned_step_ids": list(step_text_by_id),
            }
        )

    return {
        "overview": payload.get("overview"),
        "materials_reagents": payload.get("materials_reagents"),
        "equipment": payload.get("equipment"),
        "safety_sterility": payload.get("safety_sterility"),
        "step_text_by_id": step_text_by_id,
        "normalization_candidates": normalization_candidates,
    }


def sanitize_step_rewrite(text: str, *, strip_markdown_fences: Callable[..., Any]) -> str:
    """Remove structural syntax that belongs to the deterministic renderer."""
    value = strip_markdown_fences(str(text or "").strip())
    value = re.sub(r"<!--\s*protocol-step:[^>]+-->", "", value)
    value = re.sub(r"\[evidence_id:\s*[^\]]+\]", "", value)
    value = re.sub(r"^\s*(?:\d+[.)]|[-*+])\s+", "", value)
    value = re.sub(r"(?m)^\s*#{1,6}\s+", "", value)
    return value.strip()


def numeric_tokens(text: str, *, numeric_fact_tokens: Any) -> List[str]:
    return numeric_fact_tokens(text)


def select_faithful_step_text(
    step: Dict[str, Any],
    rewrite: Optional[str],
    *,
    numeric_tokens: Callable[..., Any],
    strip_leading_source_ordinal: Any,
) -> str:
    """Accept prose rewriting only when its numeric facts match the source."""
    description = strip_leading_source_ordinal(
        str(step.get("description") or "").strip()
    )
    source_sentence = strip_leading_source_ordinal(
        str(step.get("source_sentence") or "").strip()
    )
    canonical = description or source_sentence
    rewrite = str(rewrite or "").strip()
    if not rewrite:
        return canonical

    audit = step.get("writer_normalization_audit") or {}
    if (
        isinstance(audit, dict)
        and bool(audit.get("applied"))
        and str(audit.get("candidate_text") or "").strip() == rewrite
    ):
        return rewrite

    source_numbers = Counter(numeric_tokens(source_sentence or canonical))
    rewrite_numbers = Counter(numeric_tokens(rewrite))
    if rewrite_numbers == source_numbers:
        return rewrite

    description_numbers = Counter(numeric_tokens(description))
    if description and description_numbers == source_numbers:
        return description
    return source_sentence or canonical


def collect_normalization_candidates(
    block_summaries: List[Dict[str, Any]],
    writer_payload: Dict[str, Any],
    *,
    numeric_tokens: Callable[..., Any],
    strip_leading_source_ordinal: Any,
) -> List[Dict[str, Any]]:
    """Collect generic factual deltas and Writer-reported source anomalies."""

    contextual_steps: List[Dict[str, Any]] = []
    for block in block_summaries:
        for node in block.get("timeline_nodes") or []:
            node_steps = [
                step
                for step in (node.get("steps") or [])
                if str(step.get("step_id") or "").strip()
            ]
            for index, step in enumerate(node_steps):
                context = []
                for neighbor_index in range(
                    max(0, index - 2), min(len(node_steps), index + 3)
                ):
                    if neighbor_index == index:
                        continue
                    neighbor = node_steps[neighbor_index]
                    context.append(
                        {
                            "relative_position": neighbor_index - index,
                            "source_text": strip_leading_source_ordinal(
                                str(
                                    neighbor.get("source_sentence")
                                    or neighbor.get("description")
                                    or ""
                                )
                            ),
                        }
                    )
                contextual_steps.append(
                    {
                        "step": step,
                        "source_context": context,
                    }
                )
    rewrites = writer_payload.get("step_text_by_id") or {}
    reported = {
        str(item.get("step_id") or "").strip(): item
        for item in (writer_payload.get("normalization_candidates") or [])
        if isinstance(item, dict) and str(item.get("step_id") or "").strip()
    }

    candidates: List[Dict[str, Any]] = []
    for item in contextual_steps:
        step = item["step"]
        step_id = str(step.get("step_id") or "").strip()
        source_text = strip_leading_source_ordinal(
            str(step.get("source_sentence") or step.get("description") or "")
        )
        candidate_text = str(rewrites.get(step_id) or "").strip()
        report = reported.get(step_id)
        numeric_delta = bool(candidate_text) and Counter(
            numeric_tokens(source_text)
        ) != Counter(numeric_tokens(candidate_text))
        if not numeric_delta and report is None:
            continue

        candidates.append(
            {
                "step_id": step_id,
                "source_text": source_text,
                "candidate_text": candidate_text or source_text,
                "source_context": item["source_context"],
                "numeric_fact_delta": numeric_delta,
                "writer_report": report or {},
            }
        )
    return candidates


async def audit_normalization_candidates(
    candidates: List[Dict[str, Any]],
    *,
    call_model: Callable[..., Any],
    safe_parse_json: Callable[..., Any],
    logger: Logger,
) -> List[Dict[str, Any]]:
    """Use one bounded LLM call to judge only source/candidate factual deltas."""

    if not candidates:
        return []
    audit_inputs = [
        {
            key: value
            for key, value in candidate.items()
            if key != "writer_report"
        }
        for candidate in candidates
    ]
    prompt = (
        "You are an evidence-fidelity auditor for scientific protocol text.\n"
        "Review each source/candidate pair without using query-specific rules. "
        "Treat every candidate as an untrusted proposal, not as evidence. The only evidence is "
        "source_text and source_context. The candidate may repair PDF/OCR typography, or it may "
        "alter a scientific fact.\n"
        "Classify every item using exactly one verdict:\n"
        "- FORMAT_ONLY: notation/typography changed but the scientific value and meaning are identical.\n"
        "- EVIDENCE_SUPPORTED_CORRECTION: supplied source context directly supports the candidate.\n"
        "- LIKELY_OCR_CORRUPTION: the source is probably damaged and the candidate is a highly likely recovery, "
        "but no clean duplicate is present.\n"
        "- SOURCE_CONTRADICTION: the supplied source conflicts with itself.\n"
        "- UNSUPPORTED_CHANGE: the candidate changes a fact without support.\n"
        "- UNCERTAIN: evidence is insufficient.\n"
        "A SOURCE_CONTRADICTION requires conflicting statements about the same entity and operation; "
        "neighboring steps about different reagents or stages do not contradict each other.\n"
        "Do not invent a third wording or fill missing protocol facts. `use_candidate` may be true only for "
        "FORMAT_ONLY, EVIDENCE_SUPPORTED_CORRECTION, or LIKELY_OCR_CORRUPTION when the visible corruption "
        "mechanism admits one clear reconstruction. If multiple values are plausible, return UNCERTAIN. "
        "For contradictions and uncertainty, preserve the source and explain the problem.\n\n"
        "Return exactly one JSON object and no Markdown using this shape:\n"
        "{\"decisions\":[{\"step_id\":\"...\",\"verdict\":\"FORMAT_ONLY|"
        "EVIDENCE_SUPPORTED_CORRECTION|LIKELY_OCR_CORRUPTION|SOURCE_CONTRADICTION|"
        "UNSUPPORTED_CHANGE|UNCERTAIN\",\"use_candidate\":true,"
        "\"reason\":\"...\"}]}\n"
        "Return one decision for every supplied step_id.\n\n"
        "Candidates (JSON):\n"
        + json.dumps(audit_inputs, ensure_ascii=False, indent=2)
    )
    try:
        for attempt in range(2):
            response = await call_model(
                prompt=prompt,
                system_prompt="You are a precise scientific evidence-fidelity auditor.",
                temperature=0.1,
            )
            if isinstance(response, str):
                response = safe_parse_json(response)
            decisions = (
                response.get("decisions")
                if isinstance(response, dict)
                else []
            )
            valid = [
                item for item in (decisions or []) if isinstance(item, dict)
            ]
            if valid:
                return valid
            logger.warning(
                "Writer normalization audit returned no decisions on attempt %d; "
                "response type=%s payload=%r",
                attempt + 1,
                type(response).__name__,
                response,
            )
        logger.warning(
            "Writer normalization audit exhausted one bounded output retry; "
            "preserving Plan text."
        )
    except Exception as exc:
        logger.warning("Writer normalization audit failed; preserving Plan text: %s", exc)
    return []


def apply_normalization_audit(
    plan: ProtocolPlan,
    block_summaries: List[Dict[str, Any]],
    writer_payload: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    decisions: List[Dict[str, Any]],
) -> None:
    """Apply only policy-compliant decisions and persist an auditable trace."""

    decision_map = {
        str(item.get("step_id") or "").strip(): item
        for item in decisions
        if str(item.get("step_id") or "").strip()
    }
    step_lookup = plan.step_index()
    summary_lookup = {
        str(step.get("step_id") or "").strip(): step
        for block in block_summaries
        for node in (block.get("timeline_nodes") or [])
        for step in (node.get("steps") or [])
        if str(step.get("step_id") or "").strip()
    }
    rewrites = writer_payload.get("step_text_by_id") or {}

    for candidate in candidates:
        step_id = str(candidate.get("step_id") or "").strip()
        decision = decision_map.get(step_id) or {}
        verdict = str(decision.get("verdict") or "UNCERTAIN").strip().upper()
        requested = bool(decision.get("use_candidate"))
        accepted_verdict = verdict in {
            "FORMAT_ONLY",
            "EVIDENCE_SUPPORTED_CORRECTION",
            "LIKELY_OCR_CORRUPTION",
        }
        applied = bool(requested and accepted_verdict and rewrites.get(step_id))
        audit_record = {
            "verdict": verdict,
            "applied": applied,
            "source_text": candidate.get("source_text") or "",
            "candidate_text": candidate.get("candidate_text") or "",
            "reason": str(decision.get("reason") or "Audit unavailable; source preserved."),
            "numeric_fact_delta": bool(candidate.get("numeric_fact_delta")),
            "writer_report": candidate.get("writer_report") or {},
        }
        if step_id in summary_lookup:
            summary_lookup[step_id]["writer_normalization_audit"] = audit_record
        step_obj = step_lookup.get(step_id)
        if step_obj is not None:
            step_obj.meta = dict(step_obj.meta or {})
            step_obj.meta["writer_normalization_audit"] = audit_record


def strip_markdown_fences(md: str) -> str:
    """
        Remove leading/trailing markdown code fences.
        """
    md = md.strip()
    if md.startswith("```markdown"):
        md = md[11:]
    elif md.startswith("```"):
        md = md[3:]
        
    if md.endswith("```"):
        md = md[:-3]
            
    return md.strip()


def _extract_balanced_json_substring(text: str) -> str:
    """
    Extract the first balanced JSON object/array substring using a small state machine.
    Safer than rfind('}') when trailing braces appear in prose.
    """
    if not text:
        return text

    s = text.strip()

    # Find first '{' or '['
    first_obj = s.find("{")
    first_arr = s.find("[")
    starts = [i for i in [first_obj, first_arr] if i != -1]
    if not starts:
        return s

    start = min(starts)
    opener = s[start]
    closer = "}" if opener == "{" else "]"

    depth = 0
    in_str = False
    esc = False

    for i in range(start, len(s)):
        ch = s[i]

        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue

        # not in string
        if ch == '"':
            in_str = True
            continue

        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return s[start : i + 1]

    # fallback: return from first opener
    return s[start:]
