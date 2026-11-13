"""Backward-compatible entrypoint for workflow data models.

New task-specialized models live under `biomars.mas.workflow.data_types`.
This module re-exports the commonly used symbols to avoid breaking existing imports.
"""

from .data_types import (  # noqa: F401
    Idea,
    WorkflowState,
    BaseTask,
    BaseWorkflowSession,
    DrugRepurposingTask,
    DrugRepurposingSession,
    DrugRepurposingState,
    ExperimentOptimizationTask,
    ExperimentOptimizationSession,
    ExperimentOptimizationState,
    ProtocolGenerationTask,
    ProtocolGenerationSession,
    ProtocolGenerationState,
)

# Compatibility aliases expected by legacy code.
Task = BaseTask
WorkflowSession = BaseWorkflowSession

__all__ = [
    "Idea",
    "WorkflowState",
    "Task",
    "WorkflowSession",
    "BaseTask",
    "BaseWorkflowSession",
    "DrugRepurposingTask",
    "DrugRepurposingSession",
    "DrugRepurposingState",
    "ExperimentOptimizationTask",
    "ExperimentOptimizationSession",
    "ExperimentOptimizationState",
    "ProtocolGenerationTask",
    "ProtocolGenerationSession",
    "ProtocolGenerationState",
]
