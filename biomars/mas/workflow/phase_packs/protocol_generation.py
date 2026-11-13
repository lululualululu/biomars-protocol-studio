"""Phase mapping pack for protocol generation."""

from ..data_type import WorkflowState

PROTOCOL_GENERATION_PACK = {
    WorkflowState.PROTOCOL_DOMAIN_PACK: "_handle_protocol_domain_pack",
    WorkflowState.PROTOCOL_RESEARCH: "_handle_protocol_research",
    WorkflowState.PROTOCOL_PDF_INGEST: "handle_protocol_pdf_ingest",
    WorkflowState.PROTOCOL_PLANNING: "handle_protocol_planning",
    # Compatibility redirect for sessions persisted before Curator was retired.
    WorkflowState.PROTOCOL_CURATOR: "handle_legacy_protocol_curator",
    WorkflowState.PROTOCOL_GROUNDED_WRITER: "handle_protocol_grounded_writer",
    WorkflowState.PROTOCOL_CRITIC: "handle_protocol_critic",
    WorkflowState.PROTOCOL_ACTION: "handle_protocol_action",
}
