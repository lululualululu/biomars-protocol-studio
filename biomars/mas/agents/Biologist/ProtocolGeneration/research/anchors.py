"""Rank, validate, and annotate anchor candidates without changing acceptance policy."""
from biomars.mas.assets.protocol_generation.prompts.research_anchors import UNIVERSAL_RELEVANCE_PROMPT_13, SYSTEM_PROMPT_843

from typing import Any, Callable, Dict, List, Tuple, Optional
from logging import Logger
import asyncio
import json
import os
import re

from ..protocol_schemas import DomainPack, EvidenceItem


UNIVERSAL_RELEVANCE_PROMPT = UNIVERSAL_RELEVANCE_PROMPT_13


ANCHOR_SELECTION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query_focus": {"type": "string"},
        "primary_id": {"type": "string"},
        "selected_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "ranked_candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "role": {"type": "string"},
                    "score": {"type": "number"},
                    "reason": {"type": "string"},
                },
                "required": ["id", "role", "score", "reason"],
            },
        },
    },
    "required": ["query_focus", "selected_ids", "ranked_candidates"],
}


ANCHOR_CONTRACT_VALIDATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": ["accept", "needs_fulltext", "reject"],
        },
        "primary_id": {"type": "string"},
        "accepted_ids": {
            "type": "array",
            "items": {"type": "string"},
        },
        "required_facets": {
            "type": "array",
            "items": {"type": "string"},
        },
        "covered_required_facets": {
            "type": "array",
            "items": {"type": "string"},
        },
        "missing_required_facets": {
            "type": "array",
            "items": {"type": "string"},
        },
        "unsupported_required_facets": {
            "type": "array",
            "items": {"type": "string"},
        },
        "conflicting_required_facets": {
            "type": "array",
            "items": {"type": "string"},
        },
        "document_role": {
            "type": "string",
            "enum": [
                "primary_protocol",
                "methods_capable_application",
                "supporting_evidence",
                "review",
                "conference_abstract",
                "unrelated",
            ],
        },
        "reason": {"type": "string"},
    },
    "required": [
        "decision",
        "primary_id",
        "accepted_ids",
        "required_facets",
        "covered_required_facets",
        "missing_required_facets",
        "unsupported_required_facets",
        "conflicting_required_facets",
        "document_role",
        "reason",
    ],
}


ANCHOR_PROTOCOL_TERMS = (
    "protocol",
    "methods",
    "method",
    "procedure",
    "procedures",
    "workflow",
    "sop",
    "standard operating procedure",
    "current protocols",
    "nature protocols",
    "jove",
    "manual",
    "step-by-step",
    "stepwise",
)


ANCHOR_COMPARATIVE_TERMS = (
    "comparative",
    "comparison",
    "compare",
    "screening",
    "screen",
    "optimization",
    "optimisation",
    "evaluation",
    "evaluating",
    "programmed",
    "toxicity",
    "results",
    "discussion",
)


ANCHOR_BACKGROUND_TERMS = (
    "review",
    "perspective",
    "opinion",
    "editorial",
    "commentary",
    "overview",
    "introduction",
)


def preferred_anchor_id(items: List[EvidenceItem]) -> str:
    for item in items:
        if (item.meta or {}).get("anchor_stability_preference"):
            return str(item.id)
    return ""


async def validate_anchor_with_stability_preference(
    candidate_items: List[EvidenceItem],
    candidate_anchor_ids: List[str],
    domain_pack: DomainPack,
    retrieval_plan: Optional[Dict[str, Any]],
    preferred_primary_id: str,
    validation_stage: str = 'abstract',
    *,
    validate_anchor_contract_with_llm: Callable[..., Any],
) -> Tuple[List[str], Dict[str, Any]]:
    """Keep a prior primary if it still passes today's semantic contract."""
    candidate_ids = list(dict.fromkeys(str(value) for value in candidate_anchor_ids))
    if not preferred_primary_id or preferred_primary_id not in candidate_ids:
        if validation_stage == "abstract":
            return await validate_anchor_contract_with_llm(
                candidate_items,
                candidate_ids,
                domain_pack,
                retrieval_plan,
            )
        return await validate_anchor_contract_with_llm(
            candidate_items,
            candidate_ids,
            domain_pack,
            retrieval_plan,
            validation_stage=validation_stage,
        )

    stable_ids, stable_audit = await validate_anchor_contract_with_llm(
        candidate_items,
        [preferred_primary_id],
        domain_pack,
        retrieval_plan,
        allow_reselection=False,
        validation_stage=validation_stage,
    )
    stable_audit["stability_preference_evaluated"] = True
    stable_audit["preferred_primary_id"] = preferred_primary_id
    if stable_ids:
        stable_audit["stability_preserved"] = True
        return stable_ids, stable_audit
    if stable_audit.get("decision") == "validation_unavailable":
        stable_audit["stability_preserved"] = False
        stable_audit["stability_reason"] = "current_validation_unavailable"
        return [], stable_audit

    remaining_ids = [
        evidence_id for evidence_id in candidate_ids if evidence_id != preferred_primary_id
    ]
    replacement_ids, replacement_audit = await validate_anchor_contract_with_llm(
        candidate_items,
        remaining_ids,
        domain_pack,
        retrieval_plan,
        validation_stage=validation_stage,
    )
    replacement_audit["stability_preference_evaluated"] = True
    replacement_audit["stability_preserved"] = False
    replacement_audit["rejected_preferred_primary_id"] = preferred_primary_id
    replacement_audit["preferred_validation"] = stable_audit
    return replacement_ids, replacement_audit


async def validate_anchor_contract_with_llm(
    candidate_items: List[EvidenceItem],
    candidate_anchor_ids: List[str],
    domain_pack: DomainPack,
    recovery_plan: Optional[Dict[str, Any]] = None,
    allow_reselection: bool = True,
    validation_stage: str = 'abstract',
    allow_fulltext_evidence_retry: bool = True,
    evidence_overrides: Optional[Dict[str, str]] = None,
    *,
    call_model: Callable[..., Any],
    select_targeted_fulltext_evidence: Callable[..., Any],
    validate_anchor_contract_with_llm: Callable[..., Any],
    ANCHOR_CONTRACT_VALIDATION_SCHEMA: Any,
    logger: Logger,
) -> Tuple[List[str], Dict[str, Any]]:
    """Validate anchors and, once at most, reconsider the remaining candidate pool."""
    recovery_plan = recovery_plan or {}
    evidence_overrides = evidence_overrides or {}
    candidate_by_id = {
        str(item.id): item for item in candidate_items if getattr(item, "id", None)
    }
    candidate_budget = max(
        4000,
        int(os.getenv("RECOVERY_VALIDATION_CHAR_BUDGET", "16000")),
    )
    candidate_ids: List[str] = []
    candidate_payload = []
    payload_chars = 0
    ordered_candidate_ids: List[str] = []
    for raw_evidence_id in candidate_anchor_ids:
        evidence_id = str(raw_evidence_id)
        if evidence_id in candidate_by_id and evidence_id not in ordered_candidate_ids:
            ordered_candidate_ids.append(evidence_id)

    scope_priority = {
        "exact": 0,
        "uncertain": 1,
        "narrower": 2,
        "broader": 3,
        "conflict": 4,
    }
    suitability_priority = {
        "primary_protocol": 0,
        "methods_capable_application": 1,
        "supporting_only": 2,
        "reject": 3,
    }

    def _assessment_priority(evidence_id: str) -> Tuple[Any, ...]:
        meta = candidate_by_id[evidence_id].meta or {}
        assessment = meta.get("relevance_verification")
        if not isinstance(assessment, dict):
            return (0, 0, 1, 1, 0.0)
        try:
            confidence = float(assessment.get("match_confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return (
            0 if assessment.get("is_relevant", True) else 1,
            0 if assessment.get("core_workflow_relation_match", True) else 1,
            suitability_priority.get(str(assessment.get("primary_suitability") or ""), 1),
            scope_priority.get(str(assessment.get("scope_fit") or ""), 1),
            -confidence,
        )

    ordered_candidate_ids.sort(key=_assessment_priority)

    validation_stage = "fulltext" if validation_stage == "fulltext" else "abstract"
    fulltext_candidate_count = max(1, min(len(ordered_candidate_ids), 3))
    fulltext_excerpt_limit = max(
        3500,
        min(8000, int(candidate_budget / fulltext_candidate_count) - 500),
    )

    for evidence_id in ordered_candidate_ids:
        if evidence_id in candidate_ids or evidence_id not in candidate_by_id:
            continue
        item = candidate_by_id[evidence_id]
        meta = item.meta or {}
        if validation_stage == "fulltext":
            override = evidence_overrides.get(evidence_id)
            snippet = str(override or item.markdown or meta.get("snippet") or "")
            snippet_limit = (
                min(14500, candidate_budget - 800)
                if override
                else fulltext_excerpt_limit
            )
        else:
            snippet = str(meta.get("snippet") or item.markdown or "")
            snippet_limit = 1800
        payload_item = {
            "id": evidence_id,
            "title": item.title,
            "source": item.source,
            "evidence_excerpt": " ".join(snippet.split())[:snippet_limit],
        }
        independent_assessment = meta.get("relevance_verification")
        if isinstance(independent_assessment, dict):
            payload_item["independent_assessment"] = independent_assessment
        item_chars = len(json.dumps(payload_item, ensure_ascii=False))
        if candidate_payload and payload_chars + item_chars > candidate_budget:
            break
        candidate_ids.append(evidence_id)
        candidate_payload.append(payload_item)
        payload_chars += item_chars

    if not candidate_ids:
        return [], {"accepted": False, "reason": "no_candidate_anchor"}

    prompt = f"""
        Validate shortlisted anchors for a biological protocol request.

        User request:
        {domain_pack.task.query}

        Proposed central operation, if available:
        {recovery_plan.get('central_operation') or 'not provided'}

        Proposed required facets, if available:
        {recovery_plan.get('required_facets') or []}

        Candidate anchors:
        {candidate_payload}

        Validation stage: {validation_stage}

        An independent per-document assessment may be included with a candidate. Treat it as
        advisory evidence, reconcile it against the title/evidence excerpt yourself, and explicitly
        resolve disagreements before selecting the primary.

        First derive the minimum semantic contract from the literal user request. A required
        facet must be explicitly stated or be an unavoidable part of the requested central
        operation. Do not invent a reagent, purity requirement, species, cell type, material
        composition, assay, endpoint, or standalone-publication requirement that the user did
        not request.

        Validate relationships, not a bag of matching words. A reagent, material, cell type, or
        endpoint counts as covered only when the candidate uses it in the role requested by the
        user. For example, a factor used only in a later stimulation or readout does not support
        a claim that the factor is used in the requested generation workflow.

        Then choose one decision:
        - accept: the title/evidence excerpt directly supports the requested operation, biological
          entities, and every essential facet, with no explicit conflict;
        - needs_fulltext: the candidate is aligned to the requested operation and entities, but
          the snippet does not expose enough procedural detail or a requested endpoint. This is
          uncertainty, not a contradiction. A methods-capable applied paper may use this state;
        - reject: the candidate explicitly conflicts with an essential facet, covers only a
          neighboring operation/system, or merely mentions the topic without performing it.

        A missing detail in an abstract is not an explicit conflict. Do not require every
        requested endpoint or validation readout to appear in the abstract when the core method
        is directly aligned; use needs_fulltext. needs_fulltext may defer procedural detail, but
        it must not relax the requested entities, operation, scope, or relationships. A review,
        conference abstract without an executable source, or an unrelated application cannot be
        accepted as a primary.

        Treat the named durable platform, biological system, and source-to-product relationship
        as core semantic evidence. If the title/excerpt does not affirm those relationships, put
        them in unsupported_required_facets and reject at the abstract stage. A specifically named
        delivery reagent may remain a missing_required_facet only when the same candidate already
        affirms the requested durable platform, biological system, stable-engineering operation,
        and has no conflicting delivery evidence. In that narrow case use needs_fulltext, never
        accept; the full-text gate must confirm the reagent before planning. A generic paper that
        does not establish the named platform and system cannot use this exception.

        At the fulltext stage, the evidence excerpts come from the recovered Methods/full text.
        Resolve earlier abstract uncertainty from that evidence. Use needs_fulltext only when the
        supplied full-text evidence is itself incomplete or truncated; do not defer a requested
        workflow relationship that the supplied Methods contradicts or fails to perform.

        Compare all candidates before choosing primary_id. Among candidates that satisfy the
        contract, select the executable source with the smallest semantic distance to the exact
        requested target and scope. Do not prefer a broader umbrella workflow merely because it
        appears earlier, has a longer snippet, or has higher general authority when a narrower
        directly aligned protocol is available.

        Preserve molecular architecture relationships literally. If the request specifies
        separate donor and transposase plasmids, an all-in-one vector is a conflicting architecture
        even when its inducible transgene or downstream application looks more specific. Prefer
        the source that performs the requested upstream operation over a source that merely shares
        the reporter, inducible system, or downstream lineage.

        Supporting candidates may be retained only when genuinely relevant. If no candidate is
        at least needs_fulltext, return reject with empty primary_id and accepted_ids. Do not
        infer unsupported experimental details.

        The required/covered/missing/unsupported/conflicting facet arrays must describe only the
        proposed primary_id. Evaluate other candidates separately when deciding accepted_ids. A
        missing or conflicting facet in an excluded supporting candidate must not invalidate a
        primary candidate that independently satisfies the query contract.

        Return JSON with exactly these keys:
        decision, primary_id, accepted_ids, required_facets,
        covered_required_facets, missing_required_facets, unsupported_required_facets,
        conflicting_required_facets, document_role, reason.

        unsupported_required_facets contains a requested facet for which no candidate evidence excerpt
        supports the required entity-operation relationship, including a factor used only in a
        different experimental phase. Do not speculate that unsupported evidence may exist in
        full text. missing_required_facets is only for lower-level execution or validation
        details when a candidate already affirmatively establishes the requested workflow. A
        candidate set with any unsupported_required_facets cannot be accept or needs_fulltext.

        Consistency requirements:
        - primary_id must be included in accepted_ids.
        - primary_id must be empty when accepted_ids is empty.
        - accept requires empty missing_required_facets and conflicting_required_facets.
        - needs_fulltext may have missing_required_facets but cannot have an explicit conflict.
        - reject must use empty primary_id and accepted_ids.
        """
    try:
        response = await call_model(
            prompt=prompt,
            system_prompt=(
                "You are a conservative senior biologist validating protocol anchors. "
                "Return only valid JSON matching the requested schema."
            ),
            schema=ANCHOR_CONTRACT_VALIDATION_SCHEMA,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("Anchor-contract validation failed: %s", exc)
        return [], {
            "accepted": False,
            "decision": "validation_unavailable",
            "reason": str(exc),
        }

    if not isinstance(response, dict):
        return [], {
            "accepted": False,
            "decision": "validation_unavailable",
            "reason": "invalid_validation_response",
        }

    required_keys = {
        "decision",
        "primary_id",
        "accepted_ids",
        "required_facets",
        "covered_required_facets",
        "missing_required_facets",
        "unsupported_required_facets",
        "conflicting_required_facets",
        "document_role",
        "reason",
    }
    response_primary = str(response.get("primary_id") or "").strip()
    response_accepted = [str(value) for value in (response.get("accepted_ids") or [])]
    response_decision = str(response.get("decision") or "").strip().lower()
    inconsistent = (
        not required_keys.issubset(response)
        or (response_primary and response_primary not in response_accepted)
        or (not response_primary and bool(response_accepted))
        or (
            response_decision in {"accept", "needs_fulltext"}
            and bool(response.get("unsupported_required_facets"))
        )
    )
    consistency_repair = None
    if inconsistent:
        consistency_repair = {"initial_response": response, "outcome": "unavailable"}
        repair_prompt = f"""
            Re-evaluate a protocol source decision whose JSON fields are inconsistent.
            Judge from the request and source excerpts below, not from the earlier rationale.

            User request: {domain_pack.task.query}
            Validation stage: {validation_stage}
            Candidate evidence: {candidate_payload}
            Inconsistent fields: {{
                "decision": {response.get('decision')!r},
                "primary_id": {response_primary!r},
                "accepted_ids": {response_accepted!r},
                "unsupported_required_facets": {response.get('unsupported_required_facets')!r}
            }}

            Derive required facets from the user's literal request without adding constraints.
            A source using the requested input for the requested operation supports that
            relationship even if it also uses other inputs. Do not require exclusivity unless
            the user requests it. Equivalent scientific wording counts; an exact phrase match
            is not required. Independent assessments are advisory, not replacement evidence.
            For each required entity-operation relationship, check what the proposed primary's
            excerpt actually says. Cite decisive source wording in reason.

            Return JSON with exactly these keys: decision, primary_id, accepted_ids,
            required_facets, covered_required_facets, missing_required_facets,
            unsupported_required_facets, conflicting_required_facets, document_role, reason.
            All facet fields and accepted_ids are arrays of strings; the other fields are strings.
            Put directly supported facets in covered_required_facets, unconfirmed lower-level
            execution details in missing_required_facets, absent core relationships in
            unsupported_required_facets, and explicit mismatches in conflicting_required_facets.
            At the abstract stage,
            needs_fulltext is appropriate only if the core relationship is already supported.
            At the fulltext stage, do not defer relationships the supplied Methods fail to
            perform. Never invent evidence or move an unsupported relationship into missing.
            accept requires no missing, unsupported or conflicting facets. needs_fulltext
            requires no unsupported or conflicting facets. Otherwise reject. Facet lists
            describe only the primary; judge supporting candidates separately.
            primary_id must be in accepted_ids. reject must have empty primary_id and
            accepted_ids. Do not merely clear unsupported facets to make a candidate pass.
            """
        try:
            repaired = await call_model(
                prompt=repair_prompt,
                system_prompt=(
                    "You repair structured biomedical validation output. "
                    "Return only valid JSON matching the requested schema."
                ),
                schema=ANCHOR_CONTRACT_VALIDATION_SCHEMA,
                temperature=0.0,
            )
            if isinstance(repaired, dict):
                response = repaired
                consistency_repair["outcome"] = "response_received"
        except Exception as exc:
            logger.warning("Anchor-contract validation repair failed: %s", exc)

    if not isinstance(response, dict) or not required_keys.issubset(response):
        return [], {
            "accepted": False,
            "decision": "validation_unavailable",
            "reason": "invalid_validation_contract",
        }

    accepted_ids: List[str] = []
    for raw_id in response.get("accepted_ids") or []:
        evidence_id = str(raw_id)
        if evidence_id in candidate_ids and evidence_id not in accepted_ids:
            accepted_ids.append(evidence_id)

    primary_id = str(response.get("primary_id") or "").strip()
    decision = str(response.get("decision") or "").strip().lower()
    missing_facets = [
        str(value).strip()
        for value in (response.get("missing_required_facets") or [])
        if str(value).strip()
    ]
    conflicting_facets = [
        str(value).strip()
        for value in (response.get("conflicting_required_facets") or [])
        if str(value).strip()
    ]
    unsupported_required_facets = [
        str(value).strip()
        for value in (response.get("unsupported_required_facets") or [])
        if str(value).strip()
    ]

    if decision not in {"accept", "needs_fulltext", "reject"}:
        decision = "validation_unavailable"

    if decision == "accept" and missing_facets:
        decision = "needs_fulltext"

    independent_primary_rejection = ""
    retry_candidate_id = primary_id
    if not retry_candidate_id and len(candidate_ids) == 1:
        retry_candidate_id = candidate_ids[0]
    primary_item = candidate_by_id.get(retry_candidate_id)
    primary_assessment = (
        (primary_item.meta or {}).get("relevance_verification")
        if primary_item
        else None
    )
    if isinstance(primary_assessment, dict):
        suitability = str(primary_assessment.get("primary_suitability") or "")
        scope_fit = str(primary_assessment.get("scope_fit") or "")
        if primary_assessment.get("is_relevant") is False:
            independent_primary_rejection = "independent assessment rejected relevance"
        elif primary_assessment.get("core_workflow_relation_match") is False:
            independent_primary_rejection = "independent assessment rejected workflow relations"
        elif suitability in {"supporting_only", "reject"}:
            independent_primary_rejection = f"independent assessment role={suitability}"
        elif scope_fit == "conflict":
            independent_primary_rejection = "independent assessment found a scope conflict"

    targeted_evidence_retry: Optional[Dict[str, Any]] = None
    should_retry_fulltext_evidence = bool(
        validation_stage == "fulltext"
        and allow_fulltext_evidence_retry
        and retry_candidate_id in candidate_by_id
        and not evidence_overrides
        and not conflicting_facets
        and not independent_primary_rejection
        and (
            decision == "reject"
            or unsupported_required_facets
            or (decision == "needs_fulltext" and missing_facets)
        )
    )
    if should_retry_fulltext_evidence:
        unresolved_facets = list(
            dict.fromkeys(unsupported_required_facets + missing_facets)
        )
        if not unresolved_facets:
            unresolved_facets = [
                str(value).strip()
                for value in (response.get("required_facets") or [])
                if str(value).strip()
            ]
        targeted_excerpt, targeted_evidence_retry = (
            await select_targeted_fulltext_evidence(
                candidate_by_id[retry_candidate_id],
                domain_pack,
                recovery_plan,
                unresolved_facets,
            )
        )
        if targeted_excerpt:
            base_context = " ".join(
                str(candidate_by_id[retry_candidate_id].markdown or "").split()
            )[:7800]
            combined_excerpt = (
                f"[previously covered source context]\n{base_context}\n\n"
                f"[targeted source reread]\n{targeted_excerpt}"
            )[:14000]
            targeted_evidence_retry["base_context_chars"] = len(base_context)
            targeted_evidence_retry["combined_excerpt_chars"] = len(
                combined_excerpt
            )
            retry_ids, retry_audit = await validate_anchor_contract_with_llm(
                candidate_items,
                [retry_candidate_id],
                domain_pack,
                recovery_plan,
                allow_reselection=False,
                validation_stage="fulltext",
                allow_fulltext_evidence_retry=False,
                evidence_overrides={retry_candidate_id: combined_excerpt},
            )
            targeted_evidence_retry["validation_decision"] = retry_audit.get(
                "decision"
            )
            targeted_evidence_retry["validation_reason"] = retry_audit.get("reason")
            if retry_ids:
                retry_audit["targeted_evidence_retry"] = targeted_evidence_retry
                return retry_ids, retry_audit

    if unsupported_required_facets:
        decision = "reject"
    if independent_primary_rejection:
        decision = "reject"

    if (
        decision in {"reject", "validation_unavailable"}
        or primary_id not in accepted_ids
        or conflicting_facets
        or unsupported_required_facets
        or independent_primary_rejection
    ):
        accepted_ids = []
    elif accepted_ids and accepted_ids[0] != primary_id:
        accepted_ids = [primary_id] + [x for x in accepted_ids if x != primary_id]
    accepted_ids = accepted_ids[:3]

    audit = dict(response)
    audit["decision"] = decision
    audit["accepted"] = bool(accepted_ids)
    audit["candidate_ids"] = candidate_ids
    if consistency_repair:
        audit["consistency_repair"] = consistency_repair
    if independent_primary_rejection:
        audit["independent_primary_rejection"] = independent_primary_rejection
    if targeted_evidence_retry:
        audit["targeted_evidence_retry"] = targeted_evidence_retry

    if not accepted_ids and decision == "reject" and allow_reselection:
        rejected_primary_id = primary_id or candidate_ids[0]

        def _can_still_be_primary(evidence_id: str) -> bool:
            item = candidate_by_id.get(evidence_id)
            assessment = (item.meta or {}).get("relevance_verification") if item else None
            if not isinstance(assessment, dict):
                return True
            return not (
                assessment.get("is_relevant") is False
                or assessment.get("core_workflow_relation_match") is False
                or str(assessment.get("primary_suitability") or "")
                in {"supporting_only", "reject"}
                or str(assessment.get("scope_fit") or "") == "conflict"
            )

        remaining_ids = [
            evidence_id
            for evidence_id in candidate_ids
            if evidence_id != rejected_primary_id and _can_still_be_primary(evidence_id)
        ]
        if remaining_ids:
            reselected_ids, reselection_audit = await validate_anchor_contract_with_llm(
                candidate_items,
                remaining_ids,
                domain_pack,
                recovery_plan,
                allow_reselection=False,
                validation_stage=validation_stage,
            )
            if reselected_ids:
                reselection_audit["reselection_attempted"] = True
                reselection_audit["rejected_primary_id"] = rejected_primary_id
                reselection_audit["initial_validation"] = {
                    "decision": decision,
                    "reason": str(response.get("reason") or ""),
                }
                return reselected_ids, reselection_audit
            audit["reselection_attempted"] = True
            audit["rejected_primary_id"] = rejected_primary_id
            audit["reselection"] = reselection_audit

    if accepted_ids:
        for index, evidence_id in enumerate(accepted_ids):
            item = candidate_by_id.get(evidence_id)
            if not item:
                continue
            meta = item.meta or {}
            meta["anchor_contract_status"] = decision
            meta["anchor_contract_reason"] = str(response.get("reason") or "")
            meta["anchor_contract_primary"] = index == 0
            item.meta = meta

    return accepted_ids, audit


async def select_anchors_with_llm(
    top_items: List[EvidenceItem],
    domain_pack: DomainPack,
    *,
    apply_anchor_selection_metadata: Callable[..., Any],
    call_model: Callable[..., Any],
    combine_llm_and_heuristic_anchor_order: Callable[..., Any],
    extract_anchor_query_terms: Callable[..., Any],
    summarize_anchor_candidate: Callable[..., Any],
    ANCHOR_SELECTION_SCHEMA: Any,
    logger: Logger,
) -> List[str]:
    """
        Use the workflow LLM to select and order anchor candidates.
        The first returned ID is treated downstream as the primary anchor, so this
        step must distinguish "main executable mother protocol" from supporting evidence.
        """
    candidate_items = top_items[:10]
    query_terms = extract_anchor_query_terms(domain_pack.task.query)
    candidates_str = "\n".join(
        summarize_anchor_candidate(item, query_terms)
        for item in candidate_items
    )

    scope_limitations = ""
    constraints = getattr(domain_pack, "constraints", {}) or {}
    if isinstance(constraints, dict):
        scope_limitations = str(constraints.get("scope_limitations") or "").strip()

    system_prompt = (
        SYSTEM_PROMPT_843
    )

    user_prompt = f"""
        Task Query: "{domain_pack.task.query}"
        Species Constraint: "{domain_pack.task.species or 'Not specified'}"
        Cell Line Constraint: "{domain_pack.task.cell_line or 'Not specified'}"
        Scope Limitation: "{scope_limitations or 'Not explicitly specified'}"

        Candidate Documents:
        {candidates_str}

        Selection Rules:
        1. Infer the central requested biological operation from the query.
        2. Rank documents by how directly they enable that operation to be executed in the lab.
        3. Prefer a narrowly aligned protocol/method/manual/SOP as the PRIMARY anchor.
        4. If a candidate is mainly a comparative study, optimization paper, review, or broad workflow paper,
           it may be supporting evidence but should NOT outrank a more direct executable protocol.
        5. Prefer candidates whose scope matches the requested subworkflow, not just the same biological domain.
        6. Closely related surrogate systems may support the answer when directly transferable, but do not outrank a direct-match protocol.
        7. The PRIMARY anchor should minimize semantic distance to the requested operation, entities, and conditions.
        8. Rank the requested upstream operation and molecular architecture ahead of an example
           transgene, reporter, or downstream application. A paper matching the application but
           using a different architecture is not the primary protocol. For example, a requested
           donor-plus-separate-transposase workflow is not satisfied by an all-in-one vector.

        Output JSON requirements:
        - "query_focus": one short sentence describing the central requested operation.
        - "primary_id": the single best mother-protocol candidate ID.
        - "selected_ids": 1 to 3 IDs ordered from best to worst.
        - "ranked_candidates": ranked assessments for the best few candidates.
        - In ranked_candidates.role use one of:
          "primary_protocol", "supporting_protocol", "supporting_evidence", "reject".
        - Give concise reasons that mention directness, scope fit, and protocol-likeness.
        """

    try:
        logger.info("Asking LLM to select best anchors...")
        response = await call_model(
            prompt=user_prompt,
            system_prompt=system_prompt,
            schema=ANCHOR_SELECTION_SCHEMA,
            temperature=0.1,
        )
        if not isinstance(response, dict):
            logger.error(
                "LLM anchor selection returned an invalid response; "
                "refusing heuristic fallback."
            )
            return []

        valid_ids = {str(item.id) for item in candidate_items if getattr(item, "id", None)}
        primary_id = str(response.get("primary_id") or "").strip()
        selected_ids: List[str] = []
        for evidence_id in response.get("selected_ids") or []:
            evidence_id = str(evidence_id)
            if evidence_id in valid_ids and evidence_id not in selected_ids:
                selected_ids.append(evidence_id)

        ranked_candidates = response.get("ranked_candidates") or []
        ranked_valid_ids: List[str] = []
        ranked_primary_ids: List[str] = []
        ranked_roles: Dict[str, str] = {}
        rejected_ids = set()
        if isinstance(ranked_candidates, list):
            for entry in ranked_candidates:
                if not isinstance(entry, dict):
                    continue
                evidence_id = str(entry.get("id") or "").strip()
                role = str(entry.get("role") or "").strip()
                if evidence_id in valid_ids:
                    ranked_roles[evidence_id] = role
                if evidence_id in valid_ids and role == "reject":
                    rejected_ids.add(evidence_id)
                    continue
                if evidence_id in valid_ids and evidence_id not in ranked_valid_ids:
                    ranked_valid_ids.append(evidence_id)
                if (
                    evidence_id in valid_ids
                    and role == "primary_protocol"
                    and evidence_id not in ranked_primary_ids
                ):
                    ranked_primary_ids.append(evidence_id)

        selected_ids = [
            evidence_id for evidence_id in selected_ids if evidence_id not in rejected_ids
        ]
        if primary_id in rejected_ids:
            logger.warning(
                "Anchor selection response marked primary_id as reject in ranked_candidates; "
                f"discarding primary_id={primary_id}."
            )
            primary_id = ""

        if ranked_primary_ids:
            normalized_primary_id = ranked_primary_ids[0]
            if primary_id and primary_id != normalized_primary_id:
                logger.warning(
                    "Anchor selection response disagreed on primary anchor. "
                    f"Using ranked_candidates primary_protocol={normalized_primary_id} "
                    f"over primary_id={primary_id}."
                )
            primary_id = normalized_primary_id

        if (
            primary_id in ranked_roles
            and ranked_roles[primary_id] != "primary_protocol"
        ):
            logger.warning(
                "Anchor selection response did not classify primary_id=%s as "
                "primary_protocol; declining supporting-only promotion.",
                primary_id,
            )
            primary_id = ""

        if primary_id in valid_ids:
            ordered = [primary_id]
        else:
            logger.info(
                "LLM returned no explicit primary_protocol; leaving the anchor empty "
                "so bounded semantic recovery can run."
            )
            return []

        for evidence_id in selected_ids + ranked_valid_ids:
            if evidence_id in valid_ids and evidence_id not in ordered:
                ordered.append(evidence_id)

        ordered = ordered[:3]
        effective_primary_id = primary_id if primary_id in ordered else (ordered[0] if ordered else "")
        ordered = combine_llm_and_heuristic_anchor_order(
            candidate_items=candidate_items,
            ordered_ids=ordered,
            primary_id=effective_primary_id,
            ranked_candidates=ranked_candidates if isinstance(ranked_candidates, list) else [],
            domain_pack=domain_pack,
        )[:3]
        selection_payload = {
            "query_focus": response.get("query_focus", ""),
            "primary_id": effective_primary_id or (ordered[0] if ordered else primary_id),
            "selected_ids": ordered,
            "ranked_candidates": ranked_candidates,
            "selection_strategy": "llm_hybrid",
        }
        apply_anchor_selection_metadata(top_items, selection_payload)
        return ordered

    except Exception as e:
        logger.error(
            "LLM anchor selection failed: %s. Refusing heuristic fallback.",
            e,
        )
        return []


async def verify_candidates_with_llm(
    candidates: List[EvidenceItem],
    query: str,
    retrieval_plan: Optional[Dict[str, Any]] = None,
    *,
    call_model: Callable[..., Any],
    agent_model: Any,
    UNIVERSAL_RELEVANCE_PROMPT: Any,
    logger: Logger,
) -> List[Tuple[EvidenceItem, Dict]]:
    """
        [NEW] Batch verification of scientific relevance using LLM.
        Uses UNIVERSAL_RELEVANCE_PROMPT to check constraints (Species, Method, Context).
        Returns a list of tuples (item, verification_data).
        """
    import json

    # Limit concurrency to avoid rate limits
    sem = asyncio.Semaphore(5)

    async def verify_one(item: EvidenceItem) -> Tuple[EvidenceItem, Dict]:
        async with sem:
            # Prepare snippet from meta or markdown
            snippet = (item.meta or {}).get("snippet", "")
            if not snippet and item.markdown:
                snippet = item.markdown[:500].replace("\n", " ")

            # Format prompt
            meta = item.meta or {}
            open_access_pdf = meta.get("openAccessPdf") or {}
            prompt = UNIVERSAL_RELEVANCE_PROMPT.format(
                query=query,
                semantic_contract=json.dumps(
                    {
                        "central_operation": (retrieval_plan or {}).get("central_operation") or "",
                        "required_facets": (retrieval_plan or {}).get("required_facets") or [],
                    },
                    ensure_ascii=False,
                ),
                title=item.title,
                snippet=snippet,
                source=item.source,
                bibliographic_context=json.dumps(
                    {
                        "venue": meta.get("venue") or "",
                        "doi": meta.get("doi") or "",
                        "publication_types": meta.get("publicationTypes") or [],
                        "open_access_url": (
                            open_access_pdf.get("url", "")
                            if isinstance(open_access_pdf, dict)
                            else ""
                        ),
                    },
                    ensure_ascii=False,
                ),
            )

            try:
                # Call LLM
                # Depending on model capability, we try to force JSON mode or parse text
                if hasattr(agent_model, "generate_json"):
                    response = await agent_model.generate_json(prompt, schema=None) # Schema implied in prompt
                else:
                    response_text = await call_model(prompt, "") # System prompt inside template
                    # Attempt to parse JSON from text
                    start = response_text.find("{")
                    end = response_text.rfind("}") + 1
                    if start >= 0 and end > start:
                        response = json.loads(response_text[start:end])
                    else:
                        response = {
                            "is_relevant": False,
                            "match_confidence": 0.0,
                            "analysis": {"error": "JSON parse failed"},
                        }

                # Normalize common structured-output wrappers before validation.
                if (
                    isinstance(response, list)
                    and len(response) == 1
                    and isinstance(response[0], dict)
                ):
                    response = response[0]
                if (
                    not isinstance(response, dict)
                    or not isinstance(response.get("is_relevant"), bool)
                ):
                    response = {
                        "is_relevant": False,
                        "match_confidence": 0.0,
                        "analysis": {"error": "Invalid response type"},
                    }

                return (item, response)

            except Exception as e:
                logger.warning(f"Verification failed for {item.id}: {e}")
                return (
                    item,
                    {
                        "is_relevant": False,
                        "match_confidence": 0.0,
                        "analysis": {"error": str(e)},
                    },
                )

    # Run batch
    tasks = [verify_one(item) for item in candidates]
    results = await asyncio.gather(*tasks)
    return results


async def merge_and_rank(
    all_items: List[EvidenceItem],
    domain_pack: DomainPack,
    k: int,
    retrieval_plan: Optional[Dict[str, Any]] = None,
    contract_audit: Optional[Dict[str, Any]] = None,
    *,
    calculate_base_score: Callable[..., Any],
    explicit_document_exclusion_reason: Callable[..., Any],
    preferred_anchor_id: Callable[..., Any],
    select_anchors_with_llm: Callable[..., Any],
    title_supports_central_operation: Callable[..., Any],
    validate_anchor_with_stability_preference: Callable[..., Any],
    verify_candidates_with_llm: Callable[..., Any],
    logger: Logger,
    normalize_doi: Any,
) -> Tuple[List[EvidenceItem], List[str]]:
    """
        Merge results, deduplicate, VALIDATE RELEVANCE with LLM, re-rank, and select top K.
        """
    # 1. Deduplicate equivalent records while preferring the record with the
    # richest stable identifiers. S2 can return duplicate corpus records with
    # different URLs but the same paper title.
    unique_items: List[EvidenceItem] = []
    doi_to_index: Dict[str, int] = {}
    title_to_index: Dict[str, int] = {}

    def _dedupe_identity(item: EvidenceItem) -> Tuple[str, str]:
        meta = item.meta or {}
        external_ids = meta.get("externalIds") or {}
        doi = normalize_doi(external_ids.get("DOI") or meta.get("doi"))
        normalized_title = re.sub(r"[^a-z0-9]+", " ", (item.title or "").lower()).strip()
        return doi, normalized_title

    def _record_richness(item: EvidenceItem) -> Tuple[int, int, int, float]:
        meta = item.meta or {}
        external_ids = meta.get("externalIds") or {}
        stable_id_count = sum(
            bool(external_ids.get(key) or (key == "DOI" and meta.get("doi")))
            for key in ("DOI", "PubMed", "PubMedCentral")
        )
        content_length = len(str(meta.get("snippet") or item.markdown or ""))
        return (
            int(bool(meta.get("anchor_stability_preference"))),
            stable_id_count,
            content_length,
            float(item.authority_score or 0.0),
        )

    for item in all_items:
        doi, normalized_title = _dedupe_identity(item)
        index = doi_to_index.get(doi) if doi else None
        if index is None and normalized_title:
            title_index = title_to_index.get(normalized_title)
            if title_index is not None:
                existing_doi, _ = _dedupe_identity(unique_items[title_index])
                # A title is a safe fallback identity only when at least one
                # record lacks a DOI. Conflicting DOIs remain separate papers.
                if not doi or not existing_doi or doi == existing_doi:
                    index = title_index
        if index is not None:
            existing = unique_items[index]
            if _record_richness(item) > _record_richness(existing):
                unique_items[index] = item
            chosen_doi, chosen_title = _dedupe_identity(unique_items[index])
            if chosen_doi:
                doi_to_index[chosen_doi] = index
            if chosen_title:
                title_to_index[chosen_title] = index
            if doi:
                doi_to_index[doi] = index
            if normalized_title:
                title_to_index[normalized_title] = index
        else:
            index = len(unique_items)
            unique_items.append(item)
            if doi:
                doi_to_index[doi] = index
            if normalized_title:
                title_to_index[normalized_title] = index

    deduped_items = unique_items

    # 2. Base Scoring (Funnel Layer 1)
    scored_items = []
    for item in deduped_items:
        # We still use base score for initial sorting
        base_score = calculate_base_score(item, domain_pack)
        item.authority_score = base_score
        scored_items.append(item)

    # Sort by base score
    scored_items.sort(key=lambda x: x.authority_score, reverse=True)

    # 3. LLM Verification & Re-ranking (Funnel Layer 2)
    # Verify in cumulative batches by default: each next batch is opened only
    # when the current verified pool cannot produce an anchor that passes the
    # semantic contract. Set RESEARCH_VERIFY_BATCHES to an empty value to use
    # one-shot verification up to RESEARCH_VERIFY_TOPN. The final default
    # batch preserves the original k=50 retrieval recall ceiling.
    verify_top_n = int(os.getenv("RESEARCH_VERIFY_TOPN", "50"))
    verify_top_n = max(1, verify_top_n)
    progressive_raw = os.getenv(
        "RESEARCH_VERIFY_BATCHES",
        "8,16,25,40,50",
    ).strip()
    progressive = bool(progressive_raw)
    candidate_limit = min(len(scored_items), verify_top_n)

    if progressive:
        batch_ends: List[int] = []
        for raw_value in progressive_raw.split(","):
            try:
                value = int(raw_value.strip())
            except ValueError:
                continue
            value = min(max(1, value), candidate_limit)
            if value not in batch_ends:
                batch_ends.append(value)
        batch_ends.sort()
        if candidate_limit and candidate_limit not in batch_ends:
            batch_ends.append(candidate_limit)
    else:
        batch_ends = [candidate_limit] if candidate_limit else []

    verified_items: List[EvidenceItem] = []
    verified_assessments: Dict[str, Dict[str, Any]] = {}
    processed_count = 0
    anchor_ids: List[str] = []
    latest_validation: Dict[str, Any] = {}
    ready_confidence = float(
        os.getenv("RESEARCH_PRIMARY_READY_CONFIDENCE", "0.85")
    )

    def _is_primary_ready(result: Dict[str, Any]) -> bool:
        try:
            confidence = float(result.get("match_confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        return bool(
            result.get("is_relevant") is True
            and confidence >= ready_confidence
            and result.get("core_workflow_relation_match") is True
            and str(result.get("primary_suitability") or "")
            == "primary_protocol"
            and str(result.get("scope_fit") or "")
            in {"exact", "narrower"}
        )

    for batch_index, batch_end in enumerate(batch_ends, start=1):
        new_candidates = scored_items[processed_count:batch_end]
        processed_count = batch_end
        if not new_candidates:
            continue
        logger.info(
            "Progressive candidate verification batch %s/%s: checking ranks %s-%s",
            batch_index,
            len(batch_ends),
            batch_end - len(new_candidates) + 1,
            batch_end,
        )
        verified_results = await verify_candidates_with_llm(
            new_candidates,
            domain_pack.task.query,
            retrieval_plan=retrieval_plan,
        )

        for item, result in verified_results:
            is_relevant = result.get("is_relevant", False)
            try:
                confidence = float(result.get("match_confidence", 0.5))
            except (TypeError, ValueError):
                confidence = 0.0
            analysis = result.get("analysis", {})
            if not isinstance(analysis, dict):
                analysis = {}

            explicit_exclusion = explicit_document_exclusion_reason(
                domain_pack.task.query,
                item,
            )
            if explicit_exclusion:
                is_relevant = False
                result = dict(result)
                result["is_relevant"] = False
                result["primary_suitability"] = "reject"
                result["scope_fit"] = "conflict"
                result["core_workflow_relation_match"] = False
                analysis = dict(analysis)
                analysis["conflict_reason"] = explicit_exclusion
                result["analysis"] = analysis

            if not is_relevant:
                logger.info(
                    "Rejected %s (%s): %s",
                    item.id,
                    item.title,
                    analysis.get("conflict_reason") or analysis.get("error"),
                )
                continue

            boost = max(0, (confidence - 0.5) * 0.8)
            item.authority_score = min(1.0, item.authority_score + boost)
            meta = item.meta or {}
            meta["relevance_analysis"] = analysis
            meta["relevance_verification"] = result
            item.meta = meta
            verified_items.append(item)
            verified_assessments[str(item.id)] = result

        # In progressive mode unverified candidates never enter selection.
        # Wait for a strong protocol-like candidate, but always let the last
        # batch make a final contract-validated attempt to protect recall.
        is_last_batch = batch_index == len(batch_ends)
        has_ready_candidate = any(
            _is_primary_ready(result)
            for result in verified_assessments.values()
        )
        if progressive and not has_ready_candidate and not is_last_batch:
            logger.info(
                "No high-confidence primary-protocol candidate after rank %s; "
                "expanding verification.",
                batch_end,
            )
            continue

        selection_pool = list(verified_items)
        selection_pool.sort(key=lambda x: x.authority_score, reverse=True)
        top_items = selection_pool[:k]
        if not top_items:
            continue

        candidate_anchor_ids = await select_anchors_with_llm(
            top_items,
            domain_pack,
        )
        if not candidate_anchor_ids:
            # A short abstract may prove subject/workflow relevance without
            # proving that the paper is an executable mother protocol. On
            # the last batch, let the conservative contract/full-text gate
            # judge those methods-capable candidates. This is not a primary
            # selection fallback: no ID is accepted unless contract-valid.
            provisional_contract_ids: List[str] = []
            if contract_audit is not None and is_last_batch:
                for item in top_items[:10]:
                    assessment = (item.meta or {}).get("relevance_verification")
                    if not isinstance(assessment, dict):
                        continue
                    scope_fit = str(assessment.get("scope_fit") or "")
                    title_supports_operation = title_supports_central_operation(
                        item,
                        retrieval_plan,
                    )
                    if (
                        assessment.get("is_relevant") is True
                        and assessment.get("core_workflow_relation_match") is True
                        and str(assessment.get("primary_suitability") or "")
                        in {"primary_protocol", "methods_capable_application"}
                        and scope_fit != "conflict"
                        and (
                            scope_fit in {"exact", "narrower"}
                            or title_supports_operation
                        )
                    ):
                        provisional_contract_ids.append(str(item.id))
            if provisional_contract_ids:
                logger.info(
                    "Anchor selector found no explicit primary_protocol; sending %s "
                    "verified methods-capable candidate(s) to the contract gate.",
                    len(provisional_contract_ids),
                )
                candidate_anchor_ids = provisional_contract_ids
            elif progressive and not is_last_batch:
                logger.info(
                    "No primary anchor selected after rank %s; expanding verification.",
                    batch_end,
                )
                continue
            else:
                break

        if contract_audit is None:
            anchor_ids = candidate_anchor_ids
            break

        preferred_primary_id = preferred_anchor_id(top_items)
        validation_candidate_ids: List[str] = []
        for evidence_id in candidate_anchor_ids:
            evidence_id = str(evidence_id)
            if evidence_id not in validation_candidate_ids:
                validation_candidate_ids.append(evidence_id)
        anchor_ids, latest_validation = (
            await validate_anchor_with_stability_preference(
                top_items,
                validation_candidate_ids,
                domain_pack,
                retrieval_plan or {},
                preferred_primary_id,
            )
        )
        if anchor_ids and str(latest_validation.get("decision") or "") in {
            "accept",
            "needs_fulltext",
        }:
            logger.info(
                "Anchor contract passed after verifying %s candidate(s); "
                "stopping expansion.",
                batch_end,
            )
            break
        anchor_ids = []
        if progressive and not is_last_batch:
            logger.info(
                "No contract-valid anchor after rank %s; expanding verification.",
                batch_end,
            )

    if contract_audit is not None:
        contract_audit.clear()
        contract_audit.update(latest_validation)

    # Never leak the unverified remainder into anchor selection or downstream
    # planning. If no verified candidate passes, recovery/manual review owns
    # the result instead of silently promoting an unchecked paper.
    final_pool = list(verified_items)
    final_pool.sort(key=lambda x: x.authority_score, reverse=True)
    return final_pool[:k], anchor_ids


def explicit_document_exclusion_reason(query: str, item: EvidenceItem) -> str:
    """Apply only document-type exclusions stated literally in the request."""
    query_lower = str(query or "").lower()
    title_lower = str(item.title or "").lower().strip()
    meta = item.meta or {}
    publication_types = meta.get("publicationTypes") or []
    if isinstance(publication_types, str):
        publication_types = [publication_types]
    publication_blob = " ".join(str(value).lower() for value in publication_types)
    open_access_pdf = meta.get("openAccessPdf") or {}
    open_access_url = (
        str(open_access_pdf.get("url") or "").lower()
        if isinstance(open_access_pdf, dict)
        else ""
    )
    bibliographic_blob = " ".join(
        [
            str(meta.get("venue") or "").lower(),
            str(meta.get("doi") or "").lower(),
            open_access_url,
            publication_blob,
        ]
    )

    excludes_reviews = bool(
        re.search(r"\bexclude(?:s|d|ing)?\b[^.;]{0,80}\breviews?\b", query_lower)
    )
    if excludes_reviews and (
        "review" in publication_blob
        or re.search(
            r"\b(systematic review|scoping review|literature review|meta-analysis|review)\b",
            title_lower,
        )
    ):
        return "Explicit document-type exclusion: review article."

    excludes_conference_abstracts = bool(
        re.search(
            r"\bexclude(?:s|d|ing)?\b[^.;]{0,100}\b(?:conference|meeting)\s+abstracts?\b",
            query_lower,
        )
    )
    if excludes_conference_abstracts:
        explicit_abstract_title = bool(
            re.match(r"^(?:abstract\b|[a-z]{1,3}\s*\d{1,4}\s*[:.])", title_lower)
        )
        numbered_supplement_title = bool(
            re.match(r"^\d{1,4}\b", title_lower)
            and re.search(r"(?:supp(?:l|lement)|meeting|conference|congress)", bibliographic_blob)
        )
        conference_publication = any(
            marker in publication_blob
            for marker in ("conference", "meeting", "abstract")
        )
        if explicit_abstract_title or numbered_supplement_title or conference_publication:
            return "Explicit document-type exclusion: conference abstract."

    return ""


def title_supports_central_operation(
    item: EvidenceItem,
    retrieval_plan: Optional[Dict[str, Any]],
) -> bool:
    """Require title-level operation evidence for broader provisional anchors."""
    central_operation = str((retrieval_plan or {}).get("central_operation") or "")
    if not central_operation:
        return False
    stopwords = {
        "about",
        "after",
        "before",
        "cells",
        "human",
        "mouse",
        "primary",
        "protocol",
        "using",
        "with",
    }
    operation_terms = {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9-]+", central_operation.lower())
        if len(token) >= 5 and token not in stopwords
    }
    title_lower = str(item.title or "").lower()
    return any(term in title_lower for term in operation_terms)


def calculate_base_score(item: EvidenceItem, domain_pack: DomainPack) -> float:
    """
        Calculate a comprehensive score based on authority, keywords, and species match.
        """
    score = item.authority_score

    # Content/Title check string
    title_lower = (item.title or "").lower()
    check_text = title_lower + " " + (item.markdown[:2000] if item.markdown else "").lower()

    # 1. Official Source Boost
    if item.source == "official":
        score += 0.1

    # 2. Species Alignment (Crucial)
    target_species = (domain_pack.task.species or "").lower()
    if target_species:
        if target_species in title_lower:
            score += 0.4  # Big boost for correct species in title
        elif target_species in check_text:
            score += 0.1  # Smaller boost if just in content

    # 3. Keyword Relevance
    # Extract core keywords (len > 4) from query
    raw_words = domain_pack.task.query.lower().split()
    stopwords = {"of", "the", "a", "an", "from", "and", "to", "in", "for", "with", "protocol"}
    keywords = [w for w in raw_words if w not in stopwords and len(w) > 4]

    for kw in keywords:
        if kw in title_lower:
            score += 0.1

    hit_count = sum(1 for kw in keywords if kw in check_text)
    if keywords:
        score += 0.05 * (hit_count / len(keywords)) # Max +0.05 bonus

    # 4. For official sources, penalize if keyword hit is too low
    if item.source == "official":
        if hit_count == 0:
            score -= 0.5  # Heavy penalty for zero relevance
        elif hit_count <= max(1, len(keywords) // 6):
            score -= 0.2  # Moderate penalty for low relevance

    # 5. Negative constraints / Conflict Penalty
    common_species = ["human", "mouse", "rat", "zebrafish", "drosophila"]
    if target_species:
        for s in common_species:
            if s != target_species and s in title_lower and target_species not in title_lower:
                # Found a different species in title but NOT the target
                score -= 0.5
                break

    return max(0.0, min(1.0, score)) # Clamp 0-1


def select_anchors_heuristic(
    top_items: List[EvidenceItem],
    domain_pack: DomainPack,
    *,
    score_anchor_candidate_heuristically: Callable[..., Any],
) -> List[str]:
    """
        Select anchor protocols based on heuristics (direct task fit + protocol-likeness).
        """
    candidates = []
    for item in top_items[:12]:
        candidates.append(
            {
                "id": item.id,
                "score": score_anchor_candidate_heuristically(item, domain_pack),
            }
        )

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return [c["id"] for c in candidates[:3]]


def score_anchor_candidate_heuristically(
    item: EvidenceItem,
    domain_pack: DomainPack,
    *,
    extract_anchor_query_terms: Callable[..., Any],
    extract_core_operation_terms: Callable[..., Any],
    ANCHOR_BACKGROUND_TERMS: Any,
    ANCHOR_COMPARATIVE_TERMS: Any,
    ANCHOR_PROTOCOL_TERMS: Any,
) -> float:
    query_terms = set(extract_anchor_query_terms(domain_pack.task.query))
    operation_terms = set(extract_core_operation_terms(domain_pack.task.query))
    title_lower = (item.title or "").lower()
    content_lower = (item.markdown or "").lower()
    text_lower = f"{title_lower} {content_lower[:2500]}"
    has_protocol_cues = any(term in text_lower for term in ANCHOR_PROTOCOL_TERMS)
    has_comparison_cues = any(term in text_lower for term in ANCHOR_COMPARATIVE_TERMS)
    has_background_cues = any(term in text_lower for term in ANCHOR_BACKGROUND_TERMS)
    title_term_hits = sum(1 for term in query_terms if term in title_lower)
    body_term_hits = sum(1 for term in query_terms if term in text_lower)
    title_operation_hits = sum(1 for term in operation_terms if term in title_lower)
    body_operation_hits = sum(1 for term in operation_terms if term in text_lower)
    is_official = item.source == "official"

    score = float(item.authority_score)
    score += min(0.35, 0.08 * title_term_hits)
    score += min(0.15, 0.03 * body_term_hits)
    score += min(0.45, 0.18 * title_operation_hits)
    score += min(0.16, 0.08 * body_operation_hits)
    if has_protocol_cues:
        score += 0.2
    if is_official and (title_term_hits or body_term_hits):
        score += 0.1
    if has_comparison_cues:
        score -= 0.1
    if has_background_cues:
        score -= 0.2
    if operation_terms and title_operation_hits == 0 and body_operation_hits == 0:
        score -= 0.12
    return score


def extract_anchor_query_terms(query: str) -> List[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "after",
        "before",
        "during",
        "for",
        "from",
        "how",
        "in",
        "into",
        "of",
        "on",
        "or",
        "protocol",
        "protocols",
        "step",
        "steps",
        "the",
        "to",
        "using",
        "with",
    }
    tokens = re.findall(r"[a-z0-9\-\+]+", (query or "").lower())
    terms: List[str] = []
    for token in tokens:
        if len(token) <= 3 or token in stopwords:
            continue
        if token not in terms:
            terms.append(token)
    return terms[:12]


def extract_core_operation_terms(query: str) -> List[str]:
    query_lower = (query or "").strip().lower()
    if not query_lower:
        return []

    delimiters = (" of ", " for ", " from ", " using ", " with ", " after ", " in ")
    prefix = query_lower
    for marker in delimiters:
        idx = query_lower.find(marker)
        if idx > 0:
            prefix = query_lower[:idx]
            break

    tokens = re.findall(r"[a-z0-9\-\+]+", prefix)
    stopwords = {"a", "an", "and", "the", "to"}
    terms = [token for token in tokens if len(token) > 3 and token not in stopwords]
    return terms[:4]


def summarize_anchor_candidate(
    item: EvidenceItem,
    query_terms: List[str],
    *,
    ANCHOR_BACKGROUND_TERMS: Any,
    ANCHOR_COMPARATIVE_TERMS: Any,
    ANCHOR_PROTOCOL_TERMS: Any,
) -> str:
    title = item.title or ""
    source = item.source or "unknown"
    score = getattr(item, "authority_score", 0.0)
    tags = ", ".join(item.tags[:6]) if item.tags else "none"
    snippet = re.sub(r"\s+", " ", (item.markdown or "")[:1200]).strip()
    if len(snippet) > 700:
        snippet = snippet[:700] + "..."

    title_lower = title.lower()
    text_lower = f"{title_lower} {(item.markdown or '')[:2500].lower()}"
    matched_terms = [term for term in query_terms if term in text_lower][:8]
    heuristic_flags = []

    if any(term in text_lower for term in ANCHOR_PROTOCOL_TERMS):
        heuristic_flags.append("protocol_like")
    if any(term in text_lower for term in ANCHOR_COMPARATIVE_TERMS):
        heuristic_flags.append("comparative_or_results_heavy")
    if any(term in text_lower for term in ANCHOR_BACKGROUND_TERMS):
        heuristic_flags.append("background_or_review_like")
    if source == "official":
        heuristic_flags.append("official_source")
    if title_lower.startswith("current protocols") or "nature protocols" in title_lower:
        heuristic_flags.append("high_protocol_authority")
    return (
        f"ID: {item.id}\n"
        f"Title: {title}\n"
        f"Source: {source}\n"
        f"BaseScore: {score:.3f}\n"
        f"Tags: {tags}\n"
        f"MatchedQueryTerms: {matched_terms or ['none']}\n"
        f"HeuristicFlags: {heuristic_flags or ['none']}\n"
        f"Snippet: {snippet or 'none'}\n"
        "---"
    )


def apply_anchor_selection_metadata(
    top_items: List[EvidenceItem],
    selection_payload: Dict[str, Any],
) -> None:
    ranked_candidates = selection_payload.get("ranked_candidates") or []
    by_id = {
        str(entry.get("id")): entry
        for entry in ranked_candidates
        if isinstance(entry, dict) and entry.get("id")
    }
    query_focus = selection_payload.get("query_focus", "")
    primary_id = selection_payload.get("primary_id")
    selected_ids = [str(x) for x in (selection_payload.get("selected_ids") or []) if x]
    selection_strategy = selection_payload.get("selection_strategy")
    final_role_by_id = {
        evidence_id: ("primary_protocol" if idx == 0 else "supporting_protocol")
        for idx, evidence_id in enumerate(selected_ids)
    }

    for item in top_items:
        meta = item.meta or {}
        ranking = by_id.get(str(item.id))
        if ranking:
            meta["anchor_selection"] = ranking
        if query_focus:
            meta["anchor_query_focus"] = query_focus
        if selected_ids:
            meta["anchor_selected"] = str(item.id) in selected_ids
            if str(item.id) in final_role_by_id:
                meta["anchor_selection_final_role"] = final_role_by_id[str(item.id)]
        if primary_id:
            meta["anchor_primary_selected"] = str(item.id) == str(primary_id)
        if selection_strategy:
            meta["anchor_selection_strategy"] = selection_strategy
        item.meta = meta


def apply_contract_anchor_metadata(
    items: List[EvidenceItem],
    anchor_ids: List[str],
    validation: Dict[str, Any],
    domain_pack: DomainPack,
    *,
    apply_anchor_selection_metadata: Callable[..., Any],
) -> None:
    validation_reason = str(validation.get("reason") or "")
    apply_anchor_selection_metadata(
        items,
        {
            "query_focus": str(
                validation.get("central_operation")
                or f"Direct executable protocol for: {domain_pack.task.query}"
            ),
            "primary_id": anchor_ids[0],
            "selected_ids": anchor_ids,
            "ranked_candidates": [
                {
                    "id": evidence_id,
                    "role": "primary_protocol" if index == 0 else "supporting_protocol",
                    "score": max(0.0, 1.0 - (index * 0.1)),
                    "reason": validation_reason,
                }
                for index, evidence_id in enumerate(anchor_ids)
            ],
            "selection_strategy": "llm_contract",
        },
    )


def apply_heuristic_anchor_metadata(
    top_items: List[EvidenceItem],
    selected_ids: List[str],
    query_focus: str = '',
    *,
    apply_anchor_selection_metadata: Callable[..., Any],
) -> None:
    ranked_candidates = []
    for idx, evidence_id in enumerate(selected_ids):
        ranked_candidates.append(
            {
                "id": evidence_id,
                "role": "primary_protocol" if idx == 0 else "supporting_protocol",
                "score": max(0.0, 1.0 - (idx * 0.1)),
                "reason": "Heuristic fallback selected this candidate based on direct query-term overlap and protocol-likeness.",
            }
        )
    apply_anchor_selection_metadata(
        top_items,
        {
            "query_focus": query_focus,
            "primary_id": selected_ids[0] if selected_ids else None,
            "selected_ids": selected_ids,
            "ranked_candidates": ranked_candidates,
            "selection_strategy": "heuristic_fallback",
        },
    )


def combine_llm_and_heuristic_anchor_order(
    candidate_items: List[EvidenceItem],
    ordered_ids: List[str],
    primary_id: Optional[str],
    ranked_candidates: List[Dict[str, Any]],
    domain_pack: DomainPack,
    *,
    score_anchor_candidate_heuristically: Callable[..., Any],
) -> List[str]:
    if not ordered_ids:
        return []

    candidate_by_id = {str(item.id): item for item in candidate_items if getattr(item, "id", None)}
    heuristic_scores: Dict[str, float] = {}
    for evidence_id in ordered_ids:
        item = candidate_by_id.get(str(evidence_id))
        if item:
            heuristic_scores[str(evidence_id)] = score_anchor_candidate_heuristically(item, domain_pack)

    if not heuristic_scores:
        return ordered_ids

    original_index = {str(evidence_id): idx for idx, evidence_id in enumerate(ordered_ids)}
    heuristic_order = sorted(
        heuristic_scores.keys(),
        key=lambda evidence_id: (-heuristic_scores[evidence_id], original_index.get(str(evidence_id), 999)),
    )

    # LLM decides which documents are relevant enough to be anchors.
    # Preserve the LLM-designated primary anchor; use heuristics only to make the
    # remaining shortlisted supporting anchors deterministic.
    if primary_id and primary_id in heuristic_order:
        remainder = [evidence_id for evidence_id in heuristic_order if evidence_id != primary_id]
        return [primary_id] + remainder
    return heuristic_order
