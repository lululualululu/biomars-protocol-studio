"""Phase mapping pack for drug repurposing."""

from ..data_type import WorkflowState

DRUG_REPURPOSING_PACK = {
    WorkflowState.GENERATING: "_run_generation_phase",
    WorkflowState.REFLECTING: "_run_reflection_phase",
    WorkflowState.EVOLVING: "_run_evolution_phase",
    WorkflowState.RANKING: "_run_ranking_phase",
    WorkflowState.EXTERNAL_DATA: "_run_external_data_phase",
    WorkflowState.AWAITING_FEEDBACK: "_run_awaiting_feedback_phase",
}
