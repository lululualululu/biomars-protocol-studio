"""Factories for task-specific Task/Session data models."""

from __future__ import annotations

from typing import Any, Dict, Type

from .base import BaseTask, BaseWorkflowSession
from .drug_repurposing_types import DrugRepurposingTask, DrugRepurposingSession
from .experiment_optimization_types import (
    ExperimentOptimizationTask,
    ExperimentOptimizationSession,
)
from .protocol_generation_types import ProtocolGenerationTask, ProtocolGenerationSession

TASK_CLASS_BY_TYPE: Dict[str, Type[BaseTask]] = {
    "drug_repurposing": DrugRepurposingTask,
    "experiment_optimization": ExperimentOptimizationTask,
    "protocol_generation": ProtocolGenerationTask,
}

SESSION_CLASS_BY_TYPE: Dict[str, Type[BaseWorkflowSession]] = {
    "drug_repurposing": DrugRepurposingSession,
    "experiment_optimization": ExperimentOptimizationSession,
    "protocol_generation": ProtocolGenerationSession,
}


def get_task_class(task_type: str) -> Type[BaseTask]:
    return TASK_CLASS_BY_TYPE.get(task_type, BaseTask)


def get_session_class(task_type: str) -> Type[BaseWorkflowSession]:
    return SESSION_CLASS_BY_TYPE.get(task_type, BaseWorkflowSession)


def task_from_dict(data: Dict[str, Any]) -> BaseTask:
    task_type = (data or {}).get("task_type", "drug_repurposing")
    cls = get_task_class(task_type)
    return cls.from_dict(data or {})


def session_from_dict(data: Dict[str, Any]) -> BaseWorkflowSession:
    payload = data or {}
    task_payload = payload.get("task") or payload.get("goal") or {}
    task_type = payload.get("task_type") or task_payload.get("task_type") or "drug_repurposing"
    cls = get_session_class(task_type)
    return cls.from_dict(payload)


def create_task(
    *,
    task_type: str,
    id: str,
    description: str,
    domain: str,
    background: str = "",
    ref_code_path: str = "",
    constraints: Any = None,
) -> BaseTask:
    cls = get_task_class(task_type)
    return cls(
        id=id,
        task_type=task_type,
        description=description,
        domain=domain,
        background=background,
        ref_code_path=ref_code_path,
        constraints=list(constraints or []),
    )


def create_session(
    *,
    task_type: str,
    id: str,
    task: BaseTask,
    max_iterations: int,
) -> BaseWorkflowSession:
    cls = get_session_class(task_type)
    return cls(
        id=id,
        task=task,
        task_type=task_type,
        max_iterations=max_iterations,
    )
