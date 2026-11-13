"""Base workflow data models shared across task types."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


class WorkflowState(Enum):
    """Common workflow state enum kept for orchestrator compatibility."""

    INITIAL = "initial"
    GENERATING = "generating"
    REFLECTING = "reflecting"
    EVOLVING = "evolving"
    KG_PREDICTING = "kg_predicting"
    DTI_SCORING = "dti_scoring"
    RANKING = "ranking"
    AWAITING_FEEDBACK = "awaiting_feedback"
    EXTERNAL_DATA = "external_data"
    PLANNING = "planning"
    WRITING = "writing"
    CRITIC = "critic"
    FINALIZING = "finalizing"
    PROTOCOL_DOMAIN_PACK = "protocol_domain_pack"
    PROTOCOL_RESEARCH = "protocol_research"
    PROTOCOL_PDF_INGEST = "protocol_pdf_ingest"
    PROTOCOL_PLANNING = "protocol_planning"
    PROTOCOL_CURATOR = "protocol_curator"
    PROTOCOL_GROUNDED_WRITER = "protocol_grounded_writer"
    PROTOCOL_CRITIC = "protocol_critic"
    PROTOCOL_ACTION = "protocol_action"
    OPTIMIZATION_PREPARE = "optimization_prepare"
    OPTIMIZATION = "optimization"
    OPTIMIZATION_SUMMARY = "optimization_summary"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class Idea:
    """Data class for research ideas/hypotheses."""

    id: str
    text: str
    drug_name: str = ""
    score: float = 0.0
    rationale: str = ""
    critiques: List[str] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    iteration: int = 0
    scores: Dict[str, float] = field(default_factory=dict)
    references: List[Dict[str, Any]] = field(default_factory=list)
    kg_detailed_ideas: Dict[str, Any] = field(default_factory=dict)
    parent_id: Optional[str] = None
    search_queries: List[str] = field(default_factory=list)
    search_metadata: Dict[str, Any] = field(default_factory=dict)
    relevance_summary: str = ""
    ranking_rationale: str = ""
    ranking_explanation: str = ""
    generated_at: datetime = field(default_factory=datetime.now)

    def __post_init__(self):
        self.drug_name = (self.drug_name or "").strip().lower()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "drug_name": self.drug_name,
            "text": self.text,
            "score": self.score,
            "rationale": self.rationale,
            "critiques": self.critiques,
            "evidence": self.evidence,
            "iteration": self.iteration,
            "scores": self.scores,
            "references": self.references,
            "kg_detailed_ideas": self.kg_detailed_ideas,
            "parent_id": self.parent_id,
            "search_queries": self.search_queries,
            "search_metadata": self.search_metadata,
            "relevance_summary": self.relevance_summary,
            "ranking_rationale": self.ranking_rationale,
            "ranking_explanation": self.ranking_explanation,
            "generated_at": self.generated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Idea":
        payload = dict(data)
        if isinstance(payload.get("generated_at"), str):
            payload["generated_at"] = datetime.fromisoformat(payload["generated_at"])
        allowed_fields = set(cls.__dataclass_fields__.keys())
        payload = {k: v for k, v in payload.items() if k in allowed_fields}
        return cls(**payload)


@dataclass
class BaseTask:
    """Base task model shared by all task types."""

    id: str
    description: str
    domain: str
    task_type: str = "drug_repurposing"
    constraints: List[str] = field(default_factory=list)
    background: str = ""
    ref_code_path: str = ""
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "domain": self.domain,
            "task_type": self.task_type,
            "constraints": self.constraints,
            "background": self.background,
            "ref_code_path": self.ref_code_path,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BaseTask":
        payload = dict(data)
        if isinstance(payload.get("created_at"), str):
            payload["created_at"] = datetime.fromisoformat(payload["created_at"])
        payload.setdefault("task_type", "drug_repurposing")
        return cls(**payload)


@dataclass
class BaseWorkflowSession:
    """Base workflow session shared by all task types."""

    id: str
    task: BaseTask
    task_type: str = "drug_repurposing"
    task_state: str = "initial"

    ideas: List[Idea] = field(default_factory=list)
    iterations_completed: int = 0
    max_iterations: int = 4
    state: WorkflowState = WorkflowState.INITIAL
    feedback_history: List[Dict[str, Any]] = field(default_factory=list)
    top_ideas: List[str] = field(default_factory=list)
    tool_usage: Dict[str, int] = field(default_factory=dict)

    started_at: datetime = field(default_factory=datetime.now)
    completed_at: Optional[datetime] = None

    method_phase: bool = False
    context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "task": self.task.to_dict(),
            "task_type": self.task_type,
            "task_state": self.task_state,
            "ideas": [idea.to_dict() for idea in self.ideas],
            "iterations_completed": self.iterations_completed,
            "max_iterations": self.max_iterations,
            "state": self.state.value,
            "feedback_history": self.feedback_history,
            "top_ideas": self.top_ideas,
            "tool_usage": self.tool_usage,
            "method_phase": self.method_phase,
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BaseWorkflowSession":
        payload = dict(data)
        from .factory import task_from_dict  # lazy import to avoid circulars

        task_data = payload.get("task", payload.get("goal", {}))
        task = task_data if isinstance(task_data, BaseTask) else task_from_dict(task_data)

        ideas_payload = payload.get("ideas", payload.get("hypotheses", [])) or []
        ideas = [item if isinstance(item, Idea) else Idea.from_dict(item) for item in ideas_payload]

        state_payload = payload.get("state", WorkflowState.INITIAL.value)
        state = state_payload if isinstance(state_payload, WorkflowState) else WorkflowState(state_payload)

        started_at = payload.get("started_at")
        completed_at = payload.get("completed_at")
        if isinstance(started_at, str):
            started_at = datetime.fromisoformat(started_at)
        if isinstance(completed_at, str):
            completed_at = datetime.fromisoformat(completed_at)

        return cls(
            id=payload.get("id", ""),
            task=task,
            task_type=payload.get("task_type", getattr(task, "task_type", "drug_repurposing")),
            task_state=payload.get("task_state", "initial"),
            ideas=ideas,
            iterations_completed=payload.get("iterations_completed", 0),
            max_iterations=payload.get("max_iterations", 4),
            state=state,
            feedback_history=payload.get("feedback_history", []),
            top_ideas=payload.get("top_ideas", payload.get("top_hypotheses", [])),
            tool_usage=payload.get("tool_usage", {}),
            started_at=started_at or datetime.now(),
            completed_at=completed_at,
            method_phase=payload.get("method_phase", False),
            context=payload.get("context", {}),
        )
