"""Protocol generation specific task/session/state types."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

from .base import BaseTask, BaseWorkflowSession


class ProtocolGenerationState(Enum):
    INITIAL = "initial"
    PROTOCOL_DOMAIN_PACK = "protocol_domain_pack"
    PROTOCOL_RESEARCH = "protocol_research"
    PROTOCOL_PDF_INGEST = "protocol_pdf_ingest"
    PROTOCOL_PLANNING = "protocol_planning"
    PROTOCOL_CURATOR = "protocol_curator"
    PROTOCOL_GROUNDED_WRITER = "protocol_grounded_writer"
    PROTOCOL_CRITIC = "protocol_critic"
    PROTOCOL_ACTION = "protocol_action"
    COMPLETED = "completed"
    ERROR = "error"


@dataclass
class ProtocolGenerationTask(BaseTask):
    task_type: str = "protocol_generation"
    query: str = ""
    scope: str = "full_protocol"
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ProtocolGenerationSession(BaseWorkflowSession):
    task_type: str = "protocol_generation"
    task_state: str = ProtocolGenerationState.INITIAL.value

    domain_pack: Dict[str, Any] = field(default_factory=dict)
    evidence_bundle: List[Dict[str, Any]] = field(default_factory=list)
    protocol_plan: Dict[str, Any] = field(default_factory=dict)
    protocol_doc: Dict[str, Any] = field(default_factory=dict)
    protocol_candidates: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        payload = super().to_dict()
        payload.update(
            {
                "domain_pack": self.domain_pack,
                "evidence_bundle": self.evidence_bundle,
                "protocol_plan": self.protocol_plan,
                "protocol_doc": self.protocol_doc,
                "protocol_candidates": self.protocol_candidates,
            }
        )
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ProtocolGenerationSession":
        base = BaseWorkflowSession.from_dict(data)
        payload = data or {}
        return cls(
            **base.__dict__,
            domain_pack=payload.get("domain_pack", {}),
            evidence_bundle=payload.get("evidence_bundle", []),
            protocol_plan=payload.get("protocol_plan", {}),
            protocol_doc=payload.get("protocol_doc", {}),
            protocol_candidates=payload.get("protocol_candidates", []),
        )
