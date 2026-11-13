"""Shared Plan-to-text scope rules for Writer and deterministic validation."""

from typing import Collection

from .protocol_schemas import ProtocolPlan, ProtocolStepSkeleton


NON_RENDERABLE_ROLES = frozenset({"context_only", "timing_only", "structural_annotation"})


def plan_preserves_assessment_readouts(plan: ProtocolPlan) -> bool:
    profile = plan.query_focus_profile or {}
    if str(profile.get("scope_mode") or "").strip().lower() == "full_workflow":
        return True
    if "must_preserve_assessment_readouts" in profile:
        return bool(profile.get("must_preserve_assessment_readouts"))
    # Writer does not reinterpret the query. When Planning has not supplied a
    # semantic decision, preserving readouts is the non-destructive default.
    return True


def step_is_renderable_for_query(
    step: ProtocolStepSkeleton,
    include_readouts: bool,
    *,
    non_renderable_roles: Collection[str] = NON_RENDERABLE_ROLES,
) -> bool:
    role = str(
        (step.meta or {}).get("execution_role") or "operation"
    ).strip().lower()
    if role in non_renderable_roles:
        return False
    if role == "operation_constraint":
        return True
    if role == "evaluation_only":
        return include_readouts
    return True
