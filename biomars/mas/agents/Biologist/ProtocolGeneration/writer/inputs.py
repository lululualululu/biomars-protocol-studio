"""Resolve Writer evidence inputs and honor the Plan's existing scope decisions."""

from typing import Dict, List, Optional, Tuple
from logging import Logger
import os
import json

from ..protocol_schemas import ProtocolPlan, ProtocolStepSkeleton
from .. import protocol_scope


def load_anchor_catalog(manifest_path: Optional[str]) -> Dict[str, Dict[str, str]]:
    if not manifest_path:
        return {}
    mp = os.path.abspath(manifest_path)
    if not os.path.exists(mp):
        return {}
    try:
        with open(mp, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, list):
        return {}
    catalog: Dict[str, Dict[str, str]] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        evid = entry.get("evidence_id")
        if not evid:
            continue
        title = entry.get("title") or ""
        url = entry.get("url") or ""
        selection = entry.get("anchor_selection") or {}
        catalog[str(evid)] = {
            "title": str(title),
            "url": str(url),
            "anchor_selection_reason": str(selection.get("reason") or ""),
            "anchor_query_focus": str(entry.get("anchor_query_focus") or ""),
        }
    return catalog


def resolve_anchor_paths(
    session_id: str,
    anchor_paths: Dict[str, str],
    manifest_path: Optional[str],
    *,
    agent_name: str,
    logger: Logger,
) -> Tuple[Dict[str, str], int, int]:
    valid_paths: Dict[str, str] = {}
    total = len(anchor_paths)
    for evid, path in anchor_paths.items():
        if not path:
            continue
        abs_path = os.path.abspath(path)
        if os.path.exists(abs_path):
            valid_paths[evid] = abs_path
    valid_count = len(valid_paths)

    if valid_count:
        logger.info(
            f"{agent_name}: Using {valid_count}/{total} anchor_methods_paths from context for session {session_id}."
        )
        return valid_paths, valid_count, total

    manifest_candidates: List[str] = []
    if manifest_path:
        manifest_candidates.append(manifest_path)
    manifest_candidates.append(
        os.path.join(
            "results",
            "AutoProtocol",
            "anchors",
            session_id,
            "anchors_manifest.json",
        )
    )

    rebuilt: Dict[str, str] = {}
    for mp in manifest_candidates:
        try:
            mp_abs = os.path.abspath(mp)
            if not os.path.exists(mp_abs):
                continue
            with open(mp_abs, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)
            for entry in manifest_data:
                evid = entry.get("evidence_id")
                fp = entry.get("methods_md_path") or entry.get("file_path")
                if not evid or not fp:
                    continue
                abs_fp = os.path.abspath(fp)
                if os.path.exists(abs_fp):
                    rebuilt[evid] = abs_fp
            if rebuilt:
                break
        except Exception as e:
            logger.warning(
                f"{agent_name}: Failed to read anchors manifest '{mp}': {e}"
            )

    rebuilt_total = len(rebuilt)
    if rebuilt_total:
        logger.warning(
            f"{agent_name}: Loaded anchor_methods_paths from manifest for session {session_id}: "
            f"{rebuilt_total}/{rebuilt_total} valid paths"
        )
        return rebuilt, rebuilt_total, rebuilt_total

    logger.warning(
        f"{agent_name}: Unable to load any valid anchor_methods_paths for session {session_id}."
    )
    return {}, 0, total


def derive_evidence_scope_notes(
    allowed_evidence_ids: List[str],
    anchor_catalog: Optional[Dict[str, Dict[str, str]]] = None,
) -> List[str]:
    catalog = anchor_catalog or {}
    notes: List[str] = []

    for evid in allowed_evidence_ids or []:
        meta = catalog.get(str(evid)) or {}
        reason = (meta.get("anchor_selection_reason") or "").strip()
        if not reason:
            continue

        if any(
            marker in reason.lower()
            for marker in [
                "surrogate",
                "mismatch",
                "indirect",
                "partial match",
                "transfer",
            ]
        ):
            notes.append(
                "The selected evidence has an applicability boundary recorded "
                f"during anchor selection: {reason}"
            )

    seen = set()
    unique_notes: List[str] = []
    for note in notes:
        if note in seen:
            continue
        seen.add(note)
        unique_notes.append(note)
    return unique_notes


def plan_preserves_assessment_readouts(plan: ProtocolPlan) -> bool:
    return protocol_scope.plan_preserves_assessment_readouts(plan)


def step_is_renderable_for_query(step: ProtocolStepSkeleton, include_readouts: bool) -> bool:
    return protocol_scope.step_is_renderable_for_query(step, include_readouts)
