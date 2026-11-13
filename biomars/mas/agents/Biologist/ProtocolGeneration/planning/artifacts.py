"""Planning artifact I/O and trace summaries, without content-selection policy."""

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from ..protocol_schemas import ProtocolPlan
from .text import extract_selected_segment_ids_from_view

logger = logging.getLogger(__name__)


def log_view_selection(label: str, path: str, *, log: logging.Logger = logger) -> None:
    """Log selected source IDs, including the historical ``segment=`` markers."""
    if not path or not os.path.exists(path):
        log.info(f"View {label}: file not found")
        return
    try:
        with open(path, "r", encoding="utf-8") as vf:
            text = vf.read()
    except Exception:
        log.info(f"View {label}: unable to read file")
        return
    comments = re.findall(r"<!--[^>]+?-->", text, flags=re.IGNORECASE)
    segment_ids = []
    seen_ids = set()
    for c in comments:
        m = re.search(r"segment_id:([^|>]+)", c)
        if not m:
            m = re.search(r"\bsegment=([^\s>]+)", c)
        if m:
            seg_id = m.group(1).strip().strip('"')
            if seg_id and seg_id not in seen_ids:
                seen_ids.add(seg_id)
                segment_ids.append(seg_id)
    log.info(f"View {label}: selected_segments={segment_ids} path={path}")


def read_optional_text(path: Optional[str]) -> str:
    if not path:
        return ""
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                return handle.read()
    except Exception:
        return ""
    return ""


def read_optional_json(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
    except Exception:
        return {}
    return {}


def planning_view_trace(
    text: str,
    *,
    extract_segment_ids: Callable[[str], List[str]] = extract_selected_segment_ids_from_view,
) -> Dict[str, Any]:
    source = str(text or "")
    selected_ids = extract_segment_ids(source)
    rescue_ids: List[str] = []
    for match in re.finditer(
        r"<!--[^>]*segment_id:([^|>]+)[^>]*semantic_rescue:true[^>]*-->",
        source,
        flags=re.IGNORECASE,
    ):
        segment_id = match.group(1).strip()
        if segment_id and segment_id not in rescue_ids:
            rescue_ids.append(segment_id)
    return {
        "characters": len(source),
        "selected_segment_ids": selected_ids,
        "semantic_rescue_segment_ids": rescue_ids,
        "semantic_span_additions": source.count("semantic_coverage_add"),
        "contains_truncation_marker": "[TRUNCATED]" in source,
    }


def planning_plan_trace(plan: ProtocolPlan) -> Dict[str, Any]:
    return {
        "step_count": plan.step_count(),
        "block_count": len(plan.blocks or []),
        "timeline_node_count": sum(
            len(block.timeline_nodes or []) for block in (plan.blocks or [])
        ),
        "step_ids": [str(step.step_id or "") for step in plan.iter_steps()],
    }


def compact_view_verification_trace(report: Dict[str, Any]) -> Dict[str, Any]:
    data = report or {}
    return {
        "required_structures": list(data.get("required_structures") or []),
        "missing_in_view": list(data.get("missing_in_view") or []),
        "rescue_segment_ids": list(data.get("rescue_segment_ids") or []),
        "empty_view_bootstrap": bool(data.get("empty_view_bootstrap")),
        "verification_error": str(data.get("verification_error") or ""),
    }


def compact_structure_guard_trace(report: Dict[str, Any]) -> Dict[str, Any]:
    data = report or {}
    return {
        "should_reparse": bool(data.get("should_reparse")),
        "missing_structures": list(data.get("missing_structures") or []),
        "collapsed_branches": list(data.get("collapsed_branches") or []),
        "collapsed_cycles": list(data.get("collapsed_cycles") or []),
        "rescue_segment_ids": list(data.get("rescue_segment_ids") or []),
    }


def persist_planning_trace(path: str, trace: Dict[str, Any]) -> None:
    if not path:
        return
    try:
        Path(path).write_text(
            json.dumps(trace, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Unable to persist Planning trace (non-blocking): %s", exc)


def persist_effective_views(
    artifacts: Dict[str, Any],
    view_texts: Dict[str, str],
) -> None:
    """Keep persisted View artifacts identical to the texts sent to Planning."""
    for view_name, text in (view_texts or {}).items():
        path = str((artifacts or {}).get(f"view_{view_name}_path") or "")
        if not path:
            continue
        try:
            Path(path).write_text(
                str(text or "").rstrip() + ("\n" if text else ""),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(
                "Unable to persist effective %s View (non-blocking): %s",
                view_name,
                exc,
            )


def resolve_structure_first_artifacts_dir(
    context: Dict[str, Any],
    methods_md_path: str,
    session_id: str,
) -> Path:
    """
    Choose where structure-first intermediate artifacts should live.

    Default legacy behavior wrote outline/segment/view files beside the
    source anchor markdown. That creates cross-session collisions when the
    same cached anchor is replayed concurrently by multiple models.

    Replays can provide `structure_first_artifacts_dir` in context so each
    session gets its own isolated artifact directory.
    """
    override = str(context.get("structure_first_artifacts_dir") or "").strip()
    if override:
        out_dir = Path(override).expanduser()
    else:
        out_dir = Path(methods_md_path).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def read_segment_text_map(analyses: List[Dict[str, Any]], segments_dir: str) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for entry in analyses or []:
        seg_id = str(entry.get("segment_id") or "")
        seg_file = str(entry.get("segment_file") or "")
        if not seg_id or not seg_file:
            continue
        if not os.path.isabs(seg_file):
            seg_file = os.path.join(segments_dir, seg_file)
        try:
            with open(seg_file, "r", encoding="utf-8") as handle:
                mapping[seg_id] = handle.read()
        except Exception:
            continue
    return mapping
