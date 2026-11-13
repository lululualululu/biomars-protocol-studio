"""Phase mapping pack for experiment optimization."""

from ..data_type import WorkflowState

EXPERIMENT_OPTIMIZATION_PACK = {
    WorkflowState.OPTIMIZATION_PREPARE: "_run_opt_prepare_phase",
    WorkflowState.OPTIMIZATION: "_run_opt_optimization_phase",
    WorkflowState.OPTIMIZATION_SUMMARY: "_run_opt_summary_phase",
}
