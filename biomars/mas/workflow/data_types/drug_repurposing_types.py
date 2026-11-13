"""Drug repurposing specific task/session/state types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

from .base import BaseTask, BaseWorkflowSession


class DrugRepurposingState(Enum):
    INITIAL = "initial"
    GENERATING = "generating"
    REFLECTING = "reflecting"
    EXTERNAL_DATA = "external_data"
    EVOLVING = "evolving"
    RANKING = "ranking"
    AWAITING_FEEDBACK = "awaiting_feedback"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class DrugRepurposingTask(BaseTask):
    task_type: str = "drug_repurposing"
    disease_targets: List[str] = field(default_factory=list)


@dataclass
class DrugRepurposingSession(BaseWorkflowSession):
    task_type: str = "drug_repurposing"
    task_state: str = DrugRepurposingState.INITIAL.value

    kg_predictions: List[Dict[str, Any]] = field(default_factory=list)
    graph_hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    graph_targets: List[Dict[str, Any]] = field(default_factory=list)
    graph_constraints: Dict[str, Any] = field(default_factory=dict)
    dti_scores: List[List[object]] = field(default_factory=list)
    gap_reports: List[Dict[str, Any]] = field(default_factory=list)
    prev_gap_reports: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "kg_predictions": self.kg_predictions,
                "graph_hypotheses": self.graph_hypotheses,
                "graph_targets": self.graph_targets,
                "graph_constraints": self.graph_constraints,
                "dti_scores": self.dti_scores,
                "gap_reports": self.gap_reports,
                "prev_gap_reports": self.prev_gap_reports,
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DrugRepurposingSession":
        base = BaseWorkflowSession.from_dict(data)
        payload = data or {}
        return cls(
            **base.__dict__,
            kg_predictions=payload.get("kg_predictions", []),
            graph_hypotheses=payload.get("graph_hypotheses", []),
            graph_targets=payload.get("graph_targets", []),
            graph_constraints=payload.get("graph_constraints", {}),
            dti_scores=payload.get("dti_scores", []),
            gap_reports=payload.get("gap_reports", []),
            prev_gap_reports=payload.get("prev_gap_reports", []),
        )
