"""Select source views, repair their coverage, and prepare grounded parser inputs."""

import hashlib
import os
from logging import Logger
from pathlib import Path
from typing import Any, Dict, Optional

from ..protocol_schemas import DomainPack, EvidenceBundle
from . import artifacts as planning_artifacts
from . import text as planning_text
from .interfaces import PlanningHooks
from .state import PlanningInputs


async def prepare_inputs(
    planner: PlanningHooks,
    context: Dict[str, Any],
    domain_pack: DomainPack,
    evidence_bundle: EvidenceBundle,
    *,
    logger: Logger,
) -> PlanningInputs:
    """Select source views, repair their coverage, and prepare grounded parser inputs."""

    anchor_methods_paths = context.get("anchor_methods_paths", {}) or {}
    anchor_raw_paths = context.get("anchor_raw_paths", {}) or {}
    if not anchor_methods_paths:
        raise ValueError("No anchor methods paths available for parsing.")

    primary_id = context.get("primary_anchor_id")
    if not primary_id and evidence_bundle.anchor_ids:
        primary_id = evidence_bundle.anchor_ids[0]

    if not primary_id or primary_id not in anchor_methods_paths:
        if anchor_methods_paths:
            primary_id = list(anchor_methods_paths.keys())[0]
        else:
            raise ValueError("No valid anchor ID found in paths.")

    methods_md_path = anchor_methods_paths[primary_id]
    if not os.path.exists(methods_md_path):
        raise FileNotFoundError(f"Anchor file not found: {methods_md_path}")

    with open(methods_md_path, "r", encoding="utf-8") as f:
        methods_md = f.read()

    logger.info(f"Parsing method text from anchor {primary_id} ({len(methods_md)} chars)...")

    sid = str(context.get("session_id") or context.get("sid") or "unknown")
    planning_trace: Dict[str, Any] = {
        "version": "1.0",
        "session_id": sid,
        "task_query": domain_pack.task.query,
        "primary_anchor_id": primary_id,
        "source": {
            "methods_path": methods_md_path,
            "methods_characters": len(methods_md),
        },
        "views": {},
        "semantic_view_verification": {},
        "plan": {},
        "checks": {},
        "repairs": {},
    }
    planning_trace_path = ""

    # 2. Segment the source, analyze sections, and persist the initial views.
    view_materials_text = ""
    view_reagent_setup_text = ""
    view_procedure_text = ""
    view_timing_text = ""
    view_troubleshooting_text = ""
    segment_index_text = ""
    artifacts: Dict[str, Any] = {}
    segment_analysis_bundle: Optional[Dict[str, Any]] = None
    try:
        from ..sources.protocol_outline_segmenter import persist_outline_artifacts

        artifacts_dir = planner._resolve_structure_first_artifacts_dir(context, methods_md_path, sid)

        raw_fulltext_md_path = anchor_raw_paths.get(primary_id) or ""
        raw_fulltext_md = methods_md
        if raw_fulltext_md_path and os.path.exists(raw_fulltext_md_path):
            with open(raw_fulltext_md_path, "r", encoding="utf-8") as rf:
                raw_fulltext_md = rf.read()
        segmentation_source, segmentation_text = planner._select_segmentation_source(
            methods_md_path=methods_md_path,
            methods_md=methods_md,
            raw_fulltext_md_path=raw_fulltext_md_path,
            raw_fulltext_md=raw_fulltext_md,
        )

        artifacts = persist_outline_artifacts(
            session_id=sid,
            evidence_id=primary_id,
            anchor_file_path=segmentation_source,
            markdown_text=segmentation_text,
            output_dir=str(artifacts_dir),
        )
        planning_trace_path = str(
            Path(artifacts.get("anchors_dir") or artifacts_dir)
            / f"{primary_id}__planning_trace.json"
        )
        artifacts["planning_trace_path"] = planning_trace_path
        planning_trace["source"].update(
            {
                "segmentation_path": segmentation_source,
                "segmentation_characters": len(segmentation_text),
                "source_kind": (
                    "methods"
                    if os.path.abspath(segmentation_source) == os.path.abspath(methods_md_path)
                    else "raw"
                ),
            }
        )
        planner._persist_planning_trace(planning_trace_path, planning_trace)
        logger.info(
            f"Saved outline artifacts for {primary_id}: outline={artifacts.get('outline_path')}, "
            f"segments={artifacts.get('segments_dir')}, sections={artifacts.get('sections_count')}"
        )

        try:
            segment_analysis_bundle = await planner._analyze_segments(
                outline_path=artifacts.get('outline_path'),
                evidence_id=primary_id,
                source_file=segmentation_source,
                segments_dir=artifacts.get('segments_dir'),
                task_query=domain_pack.task.query,
            )
            if segment_analysis_bundle:
                logger.info(
                    f"Completed segment analysis for {primary_id}: "
                    f"{len(segment_analysis_bundle.get('analyses', []))} segments analyzed"
                )
        except Exception as seg_e:
            logger.warning(f"Segment analysis failed (non-blocking): {seg_e}")

        try:
            if segment_analysis_bundle:
                anchors_dir = Path(artifacts.get("anchors_dir") or Path(artifacts["outline_path"]).parent)
                segments_dir = Path(artifacts["segments_dir"])
                analyses = segment_analysis_bundle.get("analyses", []) or []
                views = await planner.build_and_persist_views(
                    analyses=analyses,
                    anchors_dir=str(anchors_dir),
                    evidence_id=primary_id,
                    segments_dir=str(segments_dir),
                    task_query=domain_pack.task.query,
                )
                artifacts["view_procedure_path"] = str(views.get("procedure") or "")
                artifacts["view_materials_path"] = str(views.get("materials") or "")
                artifacts["view_reagent_setup_path"] = str(views.get("reagent_setup") or "")
                artifacts["view_timing_path"] = str(views.get("timing") or "")
                artifacts["view_troubleshooting_path"] = str(views.get("troubleshooting") or "")
                artifacts["segment_index_path"] = str(views.get("segment_index") or "")

                planning_artifacts.log_view_selection("procedure", artifacts["view_procedure_path"], log=logger)
                planning_artifacts.log_view_selection("materials", artifacts["view_materials_path"], log=logger)
                planning_artifacts.log_view_selection("reagent_setup", artifacts["view_reagent_setup_path"], log=logger)
                planning_artifacts.log_view_selection("timing", artifacts["view_timing_path"], log=logger)
                planning_artifacts.log_view_selection("troubleshooting", artifacts["view_troubleshooting_path"], log=logger)
                logger.info(f"Segment index path: {artifacts.get('segment_index_path')}")
        except Exception as view_e:
            logger.warning(f"View build failed (non-blocking): {view_e}")

        view_materials_text = planning_artifacts.read_optional_text(artifacts.get("view_materials_path"))
        view_reagent_setup_text = planning_artifacts.read_optional_text(artifacts.get("view_reagent_setup_path"))
        view_procedure_text = planning_artifacts.read_optional_text(artifacts.get("view_procedure_path"))
        view_timing_text = planning_artifacts.read_optional_text(artifacts.get("view_timing_path"))
        view_troubleshooting_text = planning_artifacts.read_optional_text(artifacts.get("view_troubleshooting_path"))
        segment_index_text = planning_artifacts.read_optional_text(artifacts.get("segment_index_path"))
    except Exception as e:
        logger.warning(f"Failed to build/persist outline artifacts for {primary_id}: {e}")
        segment_analysis_bundle = None  # Ensure variable exists even if artifacts fail

    if not view_procedure_text.strip():
        view_procedure_text = methods_md
    if not view_timing_text.strip():
        view_timing_text = ""
    if not view_troubleshooting_text.strip():
        view_troubleshooting_text = ""
    if not segment_index_text.strip():
        segment_index_text = ""

    analyses = (segment_analysis_bundle or {}).get("analyses", []) if segment_analysis_bundle else []
    segments_dir = str(artifacts.get("segments_dir") or "")
    initial_view_texts = {
        "procedure": view_procedure_text,
        "materials": view_materials_text,
        "reagent_setup": view_reagent_setup_text,
        "timing": view_timing_text,
        "troubleshooting": view_troubleshooting_text,
    }
    planning_trace["views"]["initial"] = {
        name: planner._planning_view_trace(text)
        for name, text in initial_view_texts.items()
    }
    # 3. Extract source evidence, then restore missing content in the views.
    semantic_cache_path = str(
        Path(
            artifacts.get("anchors_dir")
            or Path(artifacts.get("outline_path") or methods_md_path).parent
        )
        / f"{primary_id}__semantic_operation_units.json"
    )
    artifacts["semantic_operation_units_path"] = semantic_cache_path
    semantic_operation_bundle: Dict[str, Any] = {}
    try:
        semantic_operation_bundle = await planner._load_or_extract_semantic_operation_units(
            task_query=domain_pack.task.query,
            view_texts={
                "procedure": planner._normalize_planner_input(view_procedure_text or ""),
                "reagent_setup": planner._normalize_planner_input(view_reagent_setup_text or ""),
            },
            cache_path=semantic_cache_path,
        )
    except Exception as exc:
        logger.warning(
            "Initial semantic inventory unavailable; semantic View coverage will use the segment catalog only: %s",
            exc,
        )
    procedure_view_report: Dict[str, Any] = {}
    reagent_view_report: Dict[str, Any] = {}
    try:
        procedure_view_report = await planner._verify_and_repair_procedure_view(
            task_query=domain_pack.task.query,
            procedure_view_text=view_procedure_text,
            analyses=analyses,
            segments_dir=segments_dir,
            semantic_operation_bundle=semantic_operation_bundle,
        )
        if procedure_view_report.get("procedure_view_text"):
            view_procedure_text = procedure_view_report["procedure_view_text"]
    except Exception as e:
        logger.warning(f"Procedure-view verification failed (non-blocking): {e}")
        procedure_view_report = {"verification_error": str(e)}

    try:
        reagent_view_report = await planner._verify_and_repair_reagent_setup_view(
            task_query=domain_pack.task.query,
            reagent_setup_view_text=view_reagent_setup_text,
            analyses=analyses,
            segments_dir=segments_dir,
            semantic_operation_bundle=semantic_operation_bundle,
        )
        if reagent_view_report.get("reagent_setup_view_text"):
            view_reagent_setup_text = reagent_view_report[
                "reagent_setup_view_text"
            ]
    except Exception as e:
        logger.warning(f"Reagent-view verification failed (non-blocking): {e}")
        reagent_view_report = {"verification_error": str(e)}

    final_view_texts = {
        "procedure": view_procedure_text,
        "materials": view_materials_text,
        "reagent_setup": view_reagent_setup_text,
        "timing": view_timing_text,
        "troubleshooting": view_troubleshooting_text,
    }
    planner._persist_effective_views(artifacts, final_view_texts)
    planning_trace["views"]["after_semantic_coverage"] = {
        name: planner._planning_view_trace(text)
        for name, text in final_view_texts.items()
    }
    planning_trace["semantic_view_verification"] = {
        "procedure": planner._compact_view_verification_trace(
            procedure_view_report
        ),
        "reagent_setup": planner._compact_view_verification_trace(
            reagent_view_report
        ),
    }
    planner._persist_planning_trace(planning_trace_path, planning_trace)

    # 4. Prepare final inputs; refresh evidence when view text has changed.
    parse_max_procedure = int(os.getenv("PLANNER_PARSE_MAX_CHARS_PROCEDURE", "50000"))
    parse_max_materials = int(os.getenv("PLANNER_PARSE_MAX_CHARS_MATERIALS", "20000"))
    parse_max_reagent_setup = int(os.getenv("PLANNER_PARSE_MAX_CHARS_REAGENT_SETUP", "20000"))

    prompt_path = str(planner.prompts_dir / "planner_block_parsing_v1.md")
    try:
        with open(prompt_path, "r", encoding="utf-8") as f:
            template = f.read()
    except FileNotFoundError:
        logger.error(f"Prompt file not found at {prompt_path}")
        raise

    segment_analysis_text = planning_text.format_segment_analysis_for_prompt(
        segment_analysis_bundle
    )

    cleaned_procedure = planner._normalize_planner_input(view_procedure_text or "")
    cleaned_materials = planner._normalize_planner_input(view_materials_text or "")
    cleaned_reagent_setup = planner._normalize_planner_input(view_reagent_setup_text or "")
    cleaned_timing = planner._normalize_planner_input(view_timing_text or "")
    cleaned_troubleshooting = planner._normalize_planner_input(view_troubleshooting_text or "")
    grounding_support_views = {
        "materials": cleaned_materials,
        "reagent_setup": cleaned_reagent_setup,
        "timing": cleaned_timing,
        "troubleshooting": cleaned_troubleshooting,
    }
    cleaned_segment_index = (segment_index_text or "").strip()
    final_source_sha256 = {
        view_type: hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()
        for view_type, text in {
            "procedure": cleaned_procedure,
            "reagent_setup": cleaned_reagent_setup,
        }.items()
    }
    if semantic_operation_bundle.get("source_sha256") != final_source_sha256:
        try:
            semantic_operation_bundle = await planner._load_or_extract_semantic_operation_units(
                task_query=domain_pack.task.query,
                view_texts={
                    "procedure": cleaned_procedure,
                    "reagent_setup": cleaned_reagent_setup,
                },
                cache_path=semantic_cache_path,
            )
        except Exception as exc:
            logger.warning(
                "Final semantic inventory refresh failed; retaining the source views without stale semantic constraints: %s",
                exc,
            )
            semantic_operation_bundle = {}
    planning_trace["parser_inputs"] = {
        "procedure_characters": min(len(cleaned_procedure), parse_max_procedure),
        "materials_characters": min(len(cleaned_materials), parse_max_materials),
        "reagent_setup_characters": min(
            len(cleaned_reagent_setup), parse_max_reagent_setup
        ),
        "timing_characters": min(len(cleaned_timing), parse_max_procedure),
        "troubleshooting_characters": min(
            len(cleaned_troubleshooting), parse_max_procedure
        ),
        "budgets": {
            "procedure": parse_max_procedure,
            "materials": parse_max_materials,
            "reagent_setup": parse_max_reagent_setup,
        },
    }
    shape_contract = await planner._infer_protocol_shape_contract_semantic(
        domain_pack.task.query,
        cleaned_procedure,
    )
    semantic_operation_checklist = planner._format_semantic_operation_checklist(
        semantic_operation_bundle
    )
    semantic_timing_inventory = planner._format_semantic_timing_inventory(
        semantic_operation_bundle
    )
    try:
        units = [
            item
            for item in (semantic_operation_bundle.get("units") or [])
            if isinstance(item, dict)
        ]
        planning_trace["semantic_operation_inventory"] = {
            "procedure_units": sum(
                1 for item in units if item.get("view_type") == "procedure"
            ),
            "reagent_setup_units": sum(
                1
                for item in units
                if item.get("view_type") == "reagent_setup"
            ),
            "error_count": len(semantic_operation_bundle.get("errors") or []),
            "time_anchor_count": len(
                semantic_operation_bundle.get("time_anchors") or []
            ),
            "cache_path": semantic_cache_path,
        }
    except Exception as exc:
        logger.warning(
            "Semantic operation inventory unavailable; continuing with source views: %s",
            exc,
        )
        planning_trace["semantic_operation_inventory"] = {
            "procedure_units": 0,
            "reagent_setup_units": 0,
            "time_anchor_count": 0,
            "error_count": 1,
            "error": str(exc),
            "cache_path": semantic_cache_path,
        }

    return PlanningInputs(
        domain_pack=domain_pack,
        evidence_bundle=evidence_bundle,
        primary_id=primary_id,
        planning_trace=planning_trace,
        planning_trace_path=planning_trace_path,
        view_procedure_text=view_procedure_text,
        analyses=analyses,
        segments_dir=segments_dir,
        segment_analysis_bundle=segment_analysis_bundle,
        template=template,
        segment_analysis_text=segment_analysis_text,
        cleaned_segment_index=cleaned_segment_index,
        cleaned_procedure=cleaned_procedure,
        cleaned_materials=cleaned_materials,
        cleaned_reagent_setup=cleaned_reagent_setup,
        cleaned_timing=cleaned_timing,
        cleaned_troubleshooting=cleaned_troubleshooting,
        grounding_support_views=grounding_support_views,
        parse_max_procedure=parse_max_procedure,
        parse_max_materials=parse_max_materials,
        parse_max_reagent_setup=parse_max_reagent_setup,
        semantic_operation_bundle=semantic_operation_bundle,
        shape_contract=shape_contract,
        semantic_operation_checklist=semantic_operation_checklist,
        semantic_timing_inventory=semantic_timing_inventory,
    )
