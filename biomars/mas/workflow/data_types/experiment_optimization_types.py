"""Experiment optimization specific task/session/state types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

from .base import BaseTask, BaseWorkflowSession


class ExperimentOptimizationState(Enum):
    INITIAL = "initial"
    OPTIMIZATION_PREPARE = "optimization_prepare"
    OPTIMIZATION = "optimization"
    OPTIMIZATION_SUMMARY = "optimization_summary"
    # Legacy states kept for compatibility with old sessions.
    GENERATING = "generating"
    REFLECTING = "reflecting"
    EXTERNAL_DATA = "external_data"
    EVOLVING = "evolving"
    RANKING = "ranking"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class ExperimentOptimizationTask(BaseTask):
    task_type: str = "experiment_optimization"
    search_space: Dict[str, Any] = field(default_factory=dict)
    objective: Dict[str, Any] = field(default_factory=dict)
    budget: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExperimentOptimizationSession(BaseWorkflowSession):
    task_type: str = "experiment_optimization"
    task_state: str = ExperimentOptimizationState.INITIAL.value

    # Kept for current shared handlers (generation/external_data/evolution) compatibility.
    graph_hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    graph_targets: List[Dict[str, Any]] = field(default_factory=list)
    graph_constraints: Dict[str, Any] = field(default_factory=dict)
    gap_reports: List[Dict[str, Any]] = field(default_factory=list)
    prev_gap_reports: List[Dict[str, Any]] = field(default_factory=list)

    optimization_trace: List[Dict[str, Any]] = field(default_factory=list)
    candidate_pool: List[Dict[str, Any]] = field(default_factory=list)
    best_params: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "graph_hypotheses": self.graph_hypotheses,
                "graph_targets": self.graph_targets,
                "graph_constraints": self.graph_constraints,
                "gap_reports": self.gap_reports,
                "prev_gap_reports": self.prev_gap_reports,
                "optimization_trace": self.optimization_trace,
                "candidate_pool": self.candidate_pool,
                "best_params": self.best_params,
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExperimentOptimizationSession":
        base = BaseWorkflowSession.from_dict(data)
        return cls(
            **base.__dict__,
            graph_hypotheses=(data or {}).get("graph_hypotheses", []),
            graph_targets=(data or {}).get("graph_targets", []),
            graph_constraints=(data or {}).get("graph_constraints", {}),
            gap_reports=(data or {}).get("gap_reports", []),
            prev_gap_reports=(data or {}).get("prev_gap_reports", []),
            optimization_trace=(data or {}).get("optimization_trace", []),
            candidate_pool=(data or {}).get("candidate_pool", []),
            best_params=(data or {}).get("best_params", {}),
        )
