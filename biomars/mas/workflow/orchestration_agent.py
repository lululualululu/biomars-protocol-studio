"""
Orchestration Agent for InternAgent

Implements central workflow orchestration coordinating multi-agent idea generation,
refinement, and selection. Manages iterative flow between specialized agents (generation,
reflection, evolution, ranking, method development, refinement), literature sources,
and scientist feedback. Controls state transitions through workflow phases, handles
concurrent task execution with semaphores, and maintains session persistence via memory
manager. Supports configurable iteration limits, top-N idea selection, and dynamic
agent routing based on workflow state.
"""

import asyncio
import logging
import time
import os
import json
import re
import random
import shutil
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable, Tuple

from ..agents.base_agent import BaseAgent
from ..agents.Biologist.ProtocolGeneration.protocol_integrity_validator import (
    ProtocolIntegrityValidator,
)
from ..memory.memory_manager import MemoryManager
from ..models.model_factory import ModelFactory
from .data_type import Idea, Task, WorkflowSession, WorkflowState
from .data_types.factory import create_session as create_typed_session, create_task
from .handler_registry import HandlerRegistry
from .phase_packs import (
    DRUG_REPURPOSING_PACK,
    EXPERIMENT_OPTIMIZATION_PACK,
    PROTOCOL_GENERATION_PACK,
)
from ..utils.execution_logger import execution_logger

logger = logging.getLogger(__name__)


class OrchestrationAgent:
    """
    Central coordinator for multi-agent research workflow.

    Orchestrates complete research pipeline from idea generation through method
    refinement. Manages workflow sessions with state machines (INITIAL → GENERATING →
    REFLECTING → EXTERNAL_DATA → EVOLVING → RANKING → COMPLETED), coordinates
    specialized agents, handles scientist feedback integration,
    and ensures proper sequencing of research phases. Supports concurrent task execution,
    session persistence, state change callbacks, and iterative refinement cycles.

    Attributes:
        config (Dict[str, Any]): Configuration dictionary
        memory_manager (MemoryManager): Persistent storage manager
        model_factory (ModelFactory): Model instance creator
        agent_registry (Dict[str, BaseAgent]): Registered specialized agents
        max_iterations (int): Maximum refinement iterations
        top_ideas_count (int): Number of top ideas to select
        graph_max_candidates (int): Max graph hypotheses to keep after LLM ranking
        top_ideas_evo (bool): Evolve only top ideas in iterations
        max_concurrent_tasks (int): Concurrent task limit
        active_sessions (Dict): Currently active workflow sessions
        session_callbacks (Dict): State change callback functions
    """

    def __init__(self,
                config: Dict[str, Any],
                memory_manager: MemoryManager,
                model_factory: ModelFactory = None,
                agent_registry: Dict[str, BaseAgent] = None):
        """
        Initialize orchestration agent with configuration and dependencies.

        Loads workflow configuration (max iterations, top ideas count, concurrency limits),
        initializes agent registry and model factory, and prepares session tracking structures.

        Args:
            config (Dict[str, Any]): Configuration with workflow settings
            memory_manager (MemoryManager): Manager for session persistence
            model_factory (ModelFactory): Factory for model creation (optional)
            agent_registry (Dict[str, BaseAgent]): Specialized agents by type (optional)
        """
        self.config = config
        self.memory_manager = memory_manager
        self.model_factory = model_factory or ModelFactory()
        self.agent_registry = agent_registry or {}

        # Load workflow configuration
        workflow_config = config.get("workflow", {})
        self.max_iterations = workflow_config.get("max_iterations", 2)
        global_max = config.get("graph_max_candidates")
        self.top_ideas_count = workflow_config.get("top_ideas_count", global_max or 3)
        max_graph = workflow_config.get("max_graph_hypotheses")
        if max_graph is None:
            max_graph = global_max
        if max_graph is None:
            max_graph = (config.get("disease_intent") or {}).get("max_graph_hypotheses")
        try:
            self.graph_max_candidates = int(max_graph) if max_graph is not None else None
        except (TypeError, ValueError):
            self.graph_max_candidates = None
        self.top_ideas_evo = workflow_config.get("top_ideas_evo", False)
        self.max_concurrent_tasks = workflow_config.get("max_concurrent_tasks", 5)
        self.disable_kg = config.get("disable_kg", workflow_config.get("disable_kg", False))
        self.exploration_mode = workflow_config.get("exploration_mode", "conservative")
        self.pipeline_states = self._load_pipeline(workflow_config)
        self.task_pipelines = workflow_config.get("task_pipelines", {}) or {}
        self.task_handler_packs = workflow_config.get("handler_packs", {}) or {}
        self.default_handler_pack = workflow_config.get("default_handler_pack", "drug_repurposing")
        self.use_pipeline = bool(self.pipeline_states or self.task_pipelines)
        self.handler_registry = HandlerRegistry()
        self._register_default_packs()

        # Active session tracking
        self.active_sessions = {}
        self.session_callbacks = {}

        logger.info(
            "OrchestrationAgent initialized with max_iterations=%s, top_ideas_count=%s, graph_max_candidates=%s",
            self.max_iterations,
            self.top_ideas_count,
            self.graph_max_candidates,
        )

    def _resolve_protocol_pdf_llm_model(self) -> Optional[str]:
        models_cfg = self.config.get("models", {}) or {}
        default_provider = models_cfg.get("default_provider")
        if isinstance(default_provider, str):
            provider_cfg = models_cfg.get(default_provider) or {}
            if isinstance(provider_cfg, dict):
                model_name = provider_cfg.get("model_name")
                if isinstance(model_name, str) and model_name.strip():
                    return model_name.strip()
        openrouter_cfg = models_cfg.get("openrouter") or {}
        if isinstance(openrouter_cfg, dict):
            model_name = openrouter_cfg.get("model_name")
            if isinstance(model_name, str) and model_name.strip():
                return model_name.strip()
        return None

    def _protocol_pdf_ingest_scope(self) -> str:
        """Return which manually provided PDFs should be parsed for protocol generation."""
        scope = os.getenv("PROTOCOL_PDF_INGEST_SCOPE")
        if not scope:
            workflow_cfg = self.config.get("workflow", {}) or {}
            scope = workflow_cfg.get("protocol_pdf_ingest_scope", "primary")
        scope = str(scope or "primary").strip().lower()
        aliases = {
            "primary_anchor": "primary",
            "used_anchor": "primary",
            "single_source": "primary",
            "selected_anchors": "all",
            "selected": "all",
        }
        return aliases.get(scope, scope)

    def _protocol_pdf_max_pages(self) -> int:
        value = os.getenv("PROTOCOL_PDF_MAX_PAGES")
        if value is None:
            workflow_cfg = self.config.get("workflow", {}) or {}
            value = workflow_cfg.get("protocol_pdf_max_pages", 30)
        try:
            return max(1, int(value))
        except (TypeError, ValueError):
            return 30

    def _protocol_pdf_prefer_pymupdf(self) -> bool:
        value: Any = os.getenv("PROTOCOL_PDF_PREFER_PYMUPDF")
        if value is None:
            workflow_cfg = self.config.get("workflow", {}) or {}
            value = workflow_cfg.get("protocol_pdf_prefer_pymupdf", True)
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
            "disabled",
        }

    @staticmethod
    def _filter_protocol_manual_pdf_requests_for_scope(
        manual_pdf_requests: List[Dict[str, Any]],
        primary_anchor_id: Optional[str],
        scope: str = "primary",
    ) -> List[Dict[str, Any]]:
        """Keep only PDF requests that are actually consumed downstream.

        Protocol generation currently runs in single-source mode after PDF ingest:
        planning/writing are narrowed to the primary anchor. Parsing supporting-anchor
        PDFs therefore adds latency without changing the generated protocol.
        """
        requests = [req for req in (manual_pdf_requests or []) if isinstance(req, dict)]
        normalized_scope = str(scope or "primary").strip().lower()
        if normalized_scope in {"all", "selected", "selected_anchors"}:
            return requests
        if normalized_scope in {"none", "off", "disabled"}:
            return []
        if not primary_anchor_id:
            return requests
        primary = str(primary_anchor_id)
        return [req for req in requests if str(req.get("evidence_id")) == primary]

    async def create_session(self,
                           goal_description: str,
                           domain: str,
                           task_type: str = "drug_repurposing",
                           background: str = "",
                           ref_code_path: str = None,
                           constraints: List[str] = None,
                           payload: Any = None) -> str:
        """
        Create new workflow session for research task.

        Initializes workflow session with research goal, domain context, and constraints.
        Creates task and session objects, stores in memory, and activates for execution.

        Args:
            goal_description (str): Research objective description
            domain (str): Research domain (e.g., "machine learning", "NLP")
            task_type (str): Task type key used for selecting pipeline/handlers
            background (str): Additional context (optional)
            ref_code_path (str): Path to reference code baseline (optional)
            constraints (List[str]): Research constraints (optional)
            payload (Any): Optional arbitrary payload for specialized tasks (e.g. ProtocolRequest)

        Returns:
            str: Unique session identifier for tracking
        """
        task_id = f"task_{int(time.time())}"
        task = create_task(
            task_type=task_type,
            id=task_id,
            description=goal_description,
            domain=domain,
            background=background,
            ref_code_path=ref_code_path,
            constraints=constraints or [],
        )
        # Monkey-patch task with payload if needed, or rely on session level
        # Ideally Task should have a payload field, but for minimal changes we attach it to session context later
        # OR we can just attach it to the task instance dynamically if dataclass allows, but it's frozen by default? No.

        session_id = f"session_{int(time.time())}"
        session = create_typed_session(
            task_type=task_type,
            id=session_id,
            task=task,
            max_iterations=self.max_iterations,
        )
        if payload is not None:
            # Ensure payload is serializable
            # We just added 'context' field to WorkflowSession in data_type.py
            if hasattr(payload, "model_dump"):
                session.context["payload"] = payload.model_dump()
            elif hasattr(payload, "dict"):
                session.context["payload"] = payload.dict()
            else:
                session.context["payload"] = payload

        await self.memory_manager.store_session(session)
        self.active_sessions[session_id] = session

        logger.info(
            "Created new session %s for task: %s (task_type=%s)",
            session_id,
            goal_description,
            task_type,
        )
        return session_id

    async def run_session(self,
                        session_id: str,
                        on_state_change: Optional[Callable] = None) -> WorkflowSession:
        """
        Execute workflow session from current state to completion.

        Runs session through workflow phases using state machine, executing appropriate
        agent tasks at each phase. Continues until completion, error, or awaiting feedback
        state. Stores session state after each phase and triggers callbacks on transitions.

        Args:
            session_id (str): Session identifier to execute
            on_state_change (Optional[Callable]): Callback for state transitions (optional)

        Returns:
            WorkflowSession: Updated session with execution results

        Raises:
            Exception: If phase execution fails or session not found
        """
        session = await self._get_session(session_id)

        if on_state_change:
            self.session_callbacks[session_id] = on_state_change

        pipeline_states = self._get_pipeline_states_for_task(session.task_type)
        if session.state == WorkflowState.INITIAL:
            if pipeline_states:
                await self._update_session_state(session, pipeline_states[0])
            else:
                await self._update_session_state(session, WorkflowState.GENERATING)

        try:
            while session.state not in [WorkflowState.COMPLETED, WorkflowState.ERROR]:
                await self._execute_current_phase(session)
                if pipeline_states:
                    await self._advance_pipeline(session)
                await self.memory_manager.store_session(session)

                if session.state == WorkflowState.AWAITING_FEEDBACK:
                    logger.info(f"Session {session_id} is awaiting feedback")
                    break

            if session.state == WorkflowState.COMPLETED:
                session.completed_at = datetime.now()
                await self.memory_manager.store_session(session)
                logger.info(f"System: Session {session_id} completed successfully after {session.iterations_completed} iterations")

            return session

        except Exception as e:
            logger.error(f"Error running session {session_id}: {str(e)}")
            await self._update_session_state(session, WorkflowState.ERROR)
            await self.memory_manager.store_session(session)
            raise

    async def add_feedback(self,
                         session_id: str,
                         feedback: dict,
                         target_idea_ids: List[str] = None) -> str:
        """
        Add scientist feedback to workflow session.

        Incorporates external feedback into session history, optionally targeting specific
        ideas. If session is awaiting feedback, transitions to reflection phase. Stores
        updated session state with feedback entry including timestamp and iteration context.

        Args:
            session_id (str): Target session identifier
            feedback (dict): Feedback content and metadata
            target_idea_ids (List[str]): Specific ideas to address (optional)

        Returns:
            str: Confirmation message with session ID
        """
        session = await self._get_session(session_id)

        feedback_entry = {
            "text": feedback,
            "timestamp": datetime.now().isoformat(),
            "target_ideas": target_idea_ids,
            "iteration": session.iterations_completed
        }
        session.feedback_history.append(feedback_entry)

        if session.state in {WorkflowState.AWAITING_FEEDBACK, WorkflowState.ERROR}:
            awaiting_kind = session.context.get("awaiting_kind")
            awaiting_return_state = session.context.get("awaiting_return_state")
            if awaiting_kind == "manual_pdf_upload" or session.context.get("manual_pdf_requests"):
                if isinstance(feedback, dict):
                    manual_pdf_dir = feedback.get("manual_pdf_dir")
                    if manual_pdf_dir:
                        session.context["manual_pdf_dir"] = str(manual_pdf_dir)
                if awaiting_return_state:
                    await self._update_session_state(session, WorkflowState(awaiting_return_state))
                else:
                    await self._update_session_state(session, WorkflowState.PROTOCOL_PDF_INGEST)
            else:
                await self._update_session_state(session, WorkflowState.REFLECTING)

        await self.memory_manager.store_session(session)
        return f"Feedback added to session {session_id}"

    async def get_session_status(self, session_id: str) -> Dict[str, Any]:
        """
        Retrieve current session status and metadata.

        Returns comprehensive session information including current state, iteration progress,
        idea counts, timestamps, and top-ranked ideas with scores.

        Args:
            session_id (str): Session identifier to query

        Returns:
            Dict[str, Any]: Status dictionary with keys: id, task, state, iterations_completed,
                max_iterations, idea_count, started_at, age_hours, top_ideas (list of dicts)
        """
        session = await self._get_session(session_id)

        status = {
            "id": session.id,
            "task": session.task.description,
            "task_type": session.task_type,
            "task_state": getattr(session, "task_state", session.state.value),
            "state": session.state.value,
            "iterations_completed": session.iterations_completed,
            "max_iterations": session.max_iterations,
            "idea_count": len(session.ideas),
            "started_at": session.started_at.isoformat(),
            "age_hours": (datetime.now() - session.started_at).total_seconds() / 3600,
            "top_ideas": [],
            "context": session.context,
        }

        if session.top_ideas:
            for idea_id in session.top_ideas:
                idea = next((i for i in session.ideas if i.id == idea_id), None)
                if idea:
                    status["top_ideas"].append({
                        "id": idea.id,
                        "text": idea.text,
                        "score": idea.score
                    })

        return status

    async def get_top_ideas(self, session_id: str, include_all: bool = False) -> List[Dict[str, Any]]:
        """
        Retrieve top-ranked ideas from session.

        Returns ideas marked as top performers by ranking agent. Optionally includes
        ideas from all iterations or only the latest iteration.

        Args:
            session_id (str): Session identifier to query
            include_all (bool): Include ideas from all iterations if True (default: False)

        Returns:
            List[Dict[str, Any]]: Top ideas as dictionaries with all idea attributes
        """
        session = await self._get_session(session_id)

        if include_all:
            ideas = session.ideas
        else:
            latest_iter = max(i.iteration for i in session.ideas) if session.ideas else 0
            ideas = [i for i in session.ideas if i.iteration == latest_iter]

        ideas = [i for i in ideas if i.id in session.top_ideas]
        return [i.to_dict() for i in ideas]

    async def get_ideas(self, session_id: str, limit: int = 10, include_all: bool = False) -> List[Dict[str, Any]]:
        """
        Retrieve ideas from session with optional filtering.

        Returns ideas sorted by score, optionally limited to top N and filtered by iteration.
        Useful for accessing all generated ideas beyond just top-ranked selections.

        Args:
            session_id (str): Session identifier to query
            limit (int): Maximum number of ideas to return (default: 10)
            include_all (bool): Include all iterations if True, else latest only (default: False)

        Returns:
            List[Dict[str, Any]]: Ideas as dictionaries sorted by score (highest first)
        """
        session = await self._get_session(session_id)

        if include_all:
            ideas = session.ideas
        else:
            latest_iter = max(i.iteration for i in session.ideas) if session.ideas else 0
            ideas = [i for i in session.ideas if i.iteration == latest_iter]

        ideas = sorted(ideas, key=lambda i: i.score, reverse=True)[:limit]
        return [i.to_dict() for i in ideas]

    # Private methods
    async def _get_session(self, session_id: str) -> WorkflowSession:
        """
        Retrieve session from active cache or memory storage.

        Checks active sessions first for performance, falls back to memory manager
        if not cached. Loads and caches session if found in storage.

        Args:
            session_id (str): Session identifier to retrieve

        Returns:
            WorkflowSession: Session object with current state

        Raises:
            ValueError: If session not found in active sessions or memory
        """
        if session_id in self.active_sessions:
            return self.active_sessions[session_id]

        session = await self.memory_manager.load_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        self.active_sessions[session_id] = session
        return session

    def _get_agent_info_for_state(self, state: WorkflowState) -> str:
        """
        Map workflow state to responsible agent name.

        Returns human-readable agent name for logging and display purposes.
        Maps workflow states to their corresponding agent handlers.

        Args:
            state (WorkflowState): Current workflow state

        Returns:
            str: Agent name or "Unknown Agent" if unmapped
        """
        agent_mapping = {
            WorkflowState.GENERATING: "Idea Innovation Agent",
            WorkflowState.EVOLVING: "Idea Innovation Agent",
            WorkflowState.KG_PREDICTING: "KG Predicting Agent",
            WorkflowState.DTI_SCORING: "DTI Scoring Agent",
            WorkflowState.REFLECTING: "Assessment Agent",
            WorkflowState.RANKING: "Assessment Agent",
            WorkflowState.EXTERNAL_DATA: "Survey Agent",
            WorkflowState.PROTOCOL_DOMAIN_PACK: "Protocol Domain Pack Agent",
            WorkflowState.PROTOCOL_RESEARCH: "Protocol Research Agent",
            WorkflowState.PROTOCOL_PDF_INGEST: "Protocol PDF Ingest",
            WorkflowState.PROTOCOL_PLANNING: "Protocol Planning Agent",
            WorkflowState.PROTOCOL_GROUNDED_WRITER: "Protocol Grounded Writer Agent",
            WorkflowState.PROTOCOL_CRITIC: "Protocol Integrity Validator",
            WorkflowState.PROTOCOL_ACTION: "Protocol Action Agent",
            WorkflowState.OPTIMIZATION_PREPARE: "Optimization Agent",
            WorkflowState.OPTIMIZATION: "Optimization Agent",
            WorkflowState.OPTIMIZATION_SUMMARY: "Optimization Agent",
            WorkflowState.FINALIZING: "System",
            WorkflowState.AWAITING_FEEDBACK: "System",
            WorkflowState.INITIAL: "System",
            WorkflowState.COMPLETED: "System",
            WorkflowState.ERROR: "System"
        }
        return agent_mapping.get(state, "Unknown Agent")

    async def _update_session_state(self, session: WorkflowSession, new_state: WorkflowState) -> None:
        """
        Transition session to new workflow state.

        Updates session state, sets method phase flag if entering method development,
        logs state transition with agent context, and triggers registered callbacks.

        Args:
            session (WorkflowSession): Session to update
            new_state (WorkflowState): Target workflow state
        """
        old_state = session.state
        session.state = new_state
        session.task_state = getattr(new_state, "value", str(new_state))

        # Log state transition with agent context
        old_agent_info = self._get_agent_info_for_state(old_state)
        new_agent_info = self._get_agent_info_for_state(new_state)
        logger.info(f"Session {session.id}: {old_agent_info} -> {new_agent_info}")
        
        # Log via ExecutionLogger
        execution_logger.log_event(
            event_type="STATE_CHANGE",
            component="OrchestrationAgent",
            phase="TRANSITION",
            data={
                "session_id": session.id,
                "old_state": getattr(old_state, 'value', str(old_state)),
                "new_state": getattr(new_state, 'value', str(new_state)),
                "old_agent": old_agent_info,
                "new_agent": new_agent_info
            }
        )

        if session.id in self.session_callbacks:
            try:
                callback = self.session_callbacks[session.id]
                await callback(session, old_state, new_state)
            except Exception as e:
                logger.error(f"Error in state change callback: {str(e)}")

    async def _execute_current_phase(self, session: WorkflowSession) -> None:
        """
        Route session to appropriate phase handler based on current state.

        Dispatches execution to specialized phase methods (generation, reflection, evolution,
        ranking, method development, refinement, literature, awaiting feedback). Transitions
        to error state if handler not found.

        Args:
            session (WorkflowSession): Session to execute
        """
        handler = self._get_handler_by_task_type(session.task_type, session.state)
        if handler is None:
            handler = self._default_phase_handlers().get(session.state)
        
        execution_logger.log_event(
            event_type="PHASE_EXECUTION",
            component="OrchestrationAgent",
            phase="SCHEDULING",
            data={
                "session_id": session.id,
                "state": getattr(session.state, 'value', str(session.state)),
                "handler": handler.__name__ if handler else "None"
            }
        )
        
        if handler:
            await handler(session)
        else:
            logger.error(f"Unknown state: {session.state}")
            await self._update_session_state(session, WorkflowState.ERROR)

    async def _handle_protocol_domain_pack(self, session: WorkflowSession) -> None:
        """
        Handle the protocol domain pack phase.
        
        Reads ProtocolRequest from session payload, invokes the protocol_domain_pack agent
        to build a DomainPack, stores it in session context, and transitions to PROTOCOL_RESEARCH.
        """
        logger.info(f"Protocol Domain Pack Agent: Starting for session {session.id}")
        
        agent = self._get_agent("protocol_domain_pack")
        if not agent:
            logger.error("Protocol domain pack agent not found")
            await self._update_session_state(session, WorkflowState.ERROR)
            return

        # Retrieve ProtocolRequest from session context
        payload_data = session.context.get("payload")
        if not payload_data:
            logger.error(f"No payload (ProtocolRequest) found in session {session.id} context")
            await self._update_session_state(session, WorkflowState.ERROR)
            return

        # Reconstruct ProtocolRequest object if payload is a dict
        if isinstance(payload_data, dict):
            # Avoid top-level import to prevent circular dependency
            from biomars.mas.agents.Biologist.ProtocolGeneration.protocol_schemas import ProtocolRequest
            try:
                request = ProtocolRequest(**payload_data)
            except Exception as e:
                logger.error(f"Failed to reconstruct ProtocolRequest: {e}")
                await self._update_session_state(session, WorkflowState.ERROR)
                return
        else:
            request = payload_data

        context = {"request": request}
        
        try:
            result = await agent.run_with_logging(context, {})
            domain_pack = result.get("domain_pack")
            
            if not domain_pack:
                raise ValueError("Protocol domain pack agent did not return a domain_pack")
                
            # Store in session context - use model_dump if Pydantic model
            if hasattr(domain_pack, "model_dump"):
                session.context["domain_pack"] = domain_pack.model_dump()
            elif hasattr(domain_pack, "dict"):
                session.context["domain_pack"] = domain_pack.dict()
            else:
                session.context["domain_pack"] = domain_pack
            
            # Persist session state immediately
            await self.memory_manager.store_session(session)
                
            logger.info(f"Protocol Domain Pack Agent: Successfully built DomainPack for session {session.id}")
            
            if not self._uses_pipeline(session):
                # Default transition for protocol workflow
                await self._update_session_state(session, WorkflowState.PROTOCOL_RESEARCH)
            
            # Explicitly complete if this is the last step in a single-step run
            # The main loop checks for COMPLETED state, but if we are just running this one step and it's not in pipeline...
            # Actually, the main loop logic (run_session) handles pipeline advancement.
            # If use_pipeline is true, it advances to next step or COMPLETED.
            # If use_pipeline is false, we set state manually. 
            
            # Ensure we don't overwrite the state if we are about to advance
            # But we MUST ensure the data is saved.
                
        except Exception as e:
            logger.error(f"Error in protocol domain pack phase: {str(e)}")
            await self._update_session_state(session, WorkflowState.ERROR)

    async def _handle_protocol_research(self, session: WorkflowSession) -> None:
        """
        Handle the protocol research phase.
        
        Reads DomainPack from session context, invokes protocol_research agent,
        stores resulting EvidenceBundle in session context, and transitions to PROTOCOL_PLANNING.
        """
        logger.info(f"Protocol Research Agent: Starting for session {session.id}")
        
        agent = self._get_agent("protocol_research")
        if not agent:
            logger.error("Protocol research agent not found")
            await self._update_session_state(session, WorkflowState.ERROR)
            return

        # Retrieve DomainPack from session context
        domain_pack_data = session.context.get("domain_pack")
        if not domain_pack_data:
            # Fallback to nested structure if present (though we flattened it earlier)
            domain_pack_data = session.context.get("protocol", {}).get("domain_pack")
            
        if not domain_pack_data:
            logger.error(f"No domain_pack found in session {session.id} context")
            await self._update_session_state(session, WorkflowState.ERROR)
            return

        # We pass the data directly; the agent handles reconstruction
        context = {
            "domain_pack": domain_pack_data,
            "session_id": session.id
        }
        
        try:
            result = await agent.run_with_logging(context, {})
            evidence_bundle = result.get("evidence_bundle")
            
            if not evidence_bundle:
                raise ValueError("Protocol research agent did not return an evidence_bundle")
            
            anchor_methods_paths = result.get("anchor_methods_paths") or {}
            anchor_raw_paths = result.get("anchor_raw_paths") or {}
            anchors_manifest_path = result.get("anchors_manifest_path")
            anchor_ids = result.get("anchor_ids") or getattr(evidence_bundle, "anchor_ids", [])
            anchor_contract_status = str(
                result.get("anchor_contract_status") or "validation_unavailable"
            )
            if anchor_contract_status not in {"accept", "needs_fulltext"}:
                anchor_ids = []
                anchor_methods_paths = {}
                anchor_raw_paths = {}
            analysis_markers_by_anchor = result.get("analysis_markers_by_anchor") or {}
                
            # Store in session context - use model_dump if Pydantic model
            if hasattr(evidence_bundle, "model_dump"):
                session.context["evidence_bundle"] = evidence_bundle.model_dump()
            elif hasattr(evidence_bundle, "dict"):
                session.context["evidence_bundle"] = evidence_bundle.dict()
            else:
                session.context["evidence_bundle"] = evidence_bundle

            session.context["anchor_methods_paths"] = anchor_methods_paths
            session.context["anchor_raw_paths"] = anchor_raw_paths
            session.context["anchors_manifest_path"] = anchors_manifest_path
            session.context["anchor_ids"] = anchor_ids
            session.context["anchor_contract_status"] = anchor_contract_status
            if anchor_ids:
                session.context["primary_anchor_id"] = str(
                    result.get("primary_anchor_id") or anchor_ids[0]
                )
            else:
                session.context.pop("primary_anchor_id", None)
            if analysis_markers_by_anchor:
                session.context["analysis_markers_by_anchor"] = analysis_markers_by_anchor

            if not anchor_ids:
                logger.error(
                    "Research produced no contract-validated primary anchor for "
                    "session %s; stopping before planning (contract_status=%s).",
                    session.id,
                    anchor_contract_status,
                )
                await self._update_session_state(session, WorkflowState.ERROR)
                return

            manual_pdf_requests_all = result.get("manual_pdf_requests") or []
            manual_pdf_requests = self._filter_protocol_manual_pdf_requests_for_scope(
                manual_pdf_requests_all,
                session.context.get("primary_anchor_id") or (anchor_ids[0] if anchor_ids else None),
                self._protocol_pdf_ingest_scope(),
            )
            if len(manual_pdf_requests) != len(manual_pdf_requests_all):
                logger.info(
                    "Manual PDF gate filtered requests for session %s: %s -> %s (scope=%s)",
                    session.id,
                    len(manual_pdf_requests_all),
                    len(manual_pdf_requests),
                    self._protocol_pdf_ingest_scope(),
                )
            if manual_pdf_requests and os.getenv("PROTOCOL_MANUAL_PDF_GATE", "1") == "1":
                session.context["manual_pdf_requests"] = manual_pdf_requests
                session.context["awaiting_kind"] = "manual_pdf_upload"
                session.context["awaiting_return_state"] = WorkflowState.PROTOCOL_PDF_INGEST.value
                session.context["pipeline_manual_override"] = True
                await self._update_session_state(session, WorkflowState.AWAITING_FEEDBACK)
                await self.memory_manager.store_session(session)
                logger.warning(
                    f"Manual PDF gate triggered for session {session.id}: "
                    f"{len(manual_pdf_requests)} anchor(s) require user-provided PDFs."
                )
                return
            
            # Persist session state immediately
            await self.memory_manager.store_session(session)
                
            logger.info(f"Protocol Research Agent: Successfully gathered evidence for session {session.id}")
            
            if not self._uses_pipeline(session):
                # Default transition for protocol workflow
                await self._update_session_state(session, WorkflowState.PROTOCOL_PLANNING)
                
        except Exception as e:
            logger.error(f"Error in protocol research phase: {str(e)}")
            await self._update_session_state(session, WorkflowState.ERROR)

    async def handle_protocol_pdf_ingest(self, session: WorkflowSession) -> None:
        manual_pdf_requests_all = session.context.get("manual_pdf_requests") or []
        manual_pdf_requests = self._filter_protocol_manual_pdf_requests_for_scope(
            manual_pdf_requests_all,
            session.context.get("primary_anchor_id"),
            self._protocol_pdf_ingest_scope(),
        )
        if len(manual_pdf_requests) != len(manual_pdf_requests_all):
            logger.info(
                "Protocol PDF Ingest: filtered requests for session %s: %s -> %s (scope=%s)",
                session.id,
                len(manual_pdf_requests_all),
                len(manual_pdf_requests),
                self._protocol_pdf_ingest_scope(),
            )
            session.context["manual_pdf_requests"] = manual_pdf_requests
        if not manual_pdf_requests:
            session.context.pop("manual_pdf_missing", None)
            session.context.pop("awaiting_kind", None)
            session.context.pop("awaiting_return_state", None)
            logger.info(f"Protocol PDF Ingest: No manual PDFs required for session {session.id}")
            return

        manual_pdf_dir = session.context.get("manual_pdf_dir")
        if not manual_pdf_dir:
            session.context["awaiting_kind"] = "manual_pdf_upload"
            session.context["awaiting_return_state"] = WorkflowState.PROTOCOL_PDF_INGEST.value
            session.context["pipeline_manual_override"] = True
            await self._update_session_state(session, WorkflowState.AWAITING_FEEDBACK)
            await self.memory_manager.store_session(session)
            logger.warning(f"Protocol PDF Ingest: Waiting for manual_pdf_dir for session {session.id}")
            return

        folder = os.path.abspath(str(manual_pdf_dir))
        from ..agents.Biologist.ProtocolGeneration.sources.protocol_pdf_ingest import (
            PdfPageLimitExceeded,
            find_reusable_anchor_cache,
            ingest_pdf_to_anchor,
            recover_methods_markdown_with_llm,
            resolve_manual_pdf_path,
            verify_anchor_pdf_identity_with_llm,
        )

        anchor_dir = os.path.join("results", "AutoProtocol", "anchors", session.id)
        os.makedirs(anchor_dir, exist_ok=True)
        manifest_path = session.context.get("anchors_manifest_path") or os.path.join(anchor_dir, "anchors_manifest.json")

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
            if not isinstance(manifest, list):
                manifest = []
        except Exception:
            manifest = []

        manifest_by_id = {}
        for entry in manifest:
            if isinstance(entry, dict) and entry.get("evidence_id"):
                manifest_by_id[str(entry.get("evidence_id"))] = entry

        evidence_items = (session.context.get("evidence_bundle") or {}).get("items") or []
        evidence_meta_by_id = {
            str(item.get("id")): item.get("meta") or {}
            for item in evidence_items
            if isinstance(item, dict) and item.get("id")
        }
        for req in manual_pdf_requests:
            if not isinstance(req, dict):
                continue
            evidence_meta = evidence_meta_by_id.get(str(req.get("evidence_id") or ""), {})
            external_ids = evidence_meta.get("externalIds") or {}
            if not isinstance(external_ids, dict):
                external_ids = {}
            req.setdefault("doi", external_ids.get("DOI") or evidence_meta.get("doi") or "")
            req.setdefault("pmid", external_ids.get("PubMed") or evidence_meta.get("pmid") or "")
            req.setdefault(
                "pmcid",
                external_ids.get("PubMedCentral") or evidence_meta.get("pmcid") or "",
            )
        session.context["manual_pdf_requests"] = manual_pdf_requests

        updated_methods_paths = dict(session.context.get("anchor_methods_paths") or {})
        updated_raw_paths = dict(session.context.get("anchor_raw_paths") or {})
        pdf_llm_model = self._resolve_protocol_pdf_llm_model()
        task_query = ""
        payload = session.context.get("payload")
        if isinstance(payload, dict):
            task_query = str(payload.get("query") or "").strip()
        if not task_query:
            domain_pack = session.context.get("domain_pack") or {}
            if isinstance(domain_pack, dict):
                task_query = str((domain_pack.get("task") or {}).get("query") or "").strip()
        identity_failures: List[Dict[str, Any]] = []
        page_limit_failures: List[Dict[str, Any]] = []
        resolved_pdf_paths: Dict[str, str] = {}
        reused_cache_records: List[Dict[str, Any]] = []
        missing: List[Dict[str, Any]] = []

        for req in manual_pdf_requests:
            evidence_id = str(req.get("evidence_id") or "").strip()
            expected = str(req.get("expected_pdf_filename") or "").strip()
            if not evidence_id or not expected:
                continue

            resolved_pdf = resolve_manual_pdf_path(folder, req)
            if resolved_pdf:
                resolved_pdf_paths[evidence_id] = resolved_pdf
                continue

            cached = find_reusable_anchor_cache(
                anchors_root=os.path.dirname(anchor_dir),
                request=req,
                current_manifest_path=manifest_path,
            )
            if cached:
                identity = await verify_anchor_pdf_identity_with_llm(
                    raw_fulltext_md_path=cached["raw_fulltext_md_path"],
                    methods_md_path=cached["methods_md_path"],
                    expected_title=str(req.get("title") or ""),
                    task_query=task_query,
                    semantic_contract=req.get("semantic_contract") or {},
                    global_config=self.config,
                    llm_model=pdf_llm_model,
                )
                if identity.get("decision") == "accept":
                    safe_evidence_id = "".join(
                        character
                        for character in evidence_id
                        if character.isalnum() or character in "_-"
                    ) or "unknown"
                    methods_path = os.path.abspath(
                        os.path.join(anchor_dir, f"{safe_evidence_id}.md")
                    )
                    raw_path = os.path.abspath(
                        os.path.join(anchor_dir, f"{safe_evidence_id}__raw.md")
                    )
                    shutil.copyfile(cached["methods_md_path"], methods_path)
                    shutil.copyfile(cached["raw_fulltext_md_path"], raw_path)
                    updated_methods_paths[evidence_id] = methods_path
                    updated_raw_paths[evidence_id] = raw_path
                    cache_entry = dict(cached.get("entry") or {})
                    entry = manifest_by_id.get(evidence_id, {})
                    entry.update(
                        {
                            "evidence_id": evidence_id,
                            "title": entry.get("title") or req.get("title"),
                            "url": entry.get("url") or req.get("url"),
                            "doi": req.get("doi") or cache_entry.get("doi"),
                            "pmid": req.get("pmid") or cache_entry.get("pmid"),
                            "pmcid": req.get("pmcid") or cache_entry.get("pmcid"),
                            "methods_md_path": methods_path,
                            "raw_fulltext_md_path": raw_path,
                            "file_path": methods_path,
                            "raw_file_path": raw_path,
                            "fulltext_strategy": "validated_cross_session_cache",
                            "fulltext_quality": cache_entry.get("fulltext_quality") or "HIGH",
                            "methods_extract_mode": cache_entry.get("methods_extract_mode") or "cached",
                            "pdf_identity_decision": "accept",
                            "pdf_identity_report_path": identity.get("report_path"),
                            "cache_source_manifest": cached.get("manifest_path"),
                            "cache_match_kind": cached.get("match_kind"),
                        }
                    )
                    manifest_by_id[evidence_id] = entry
                    reused_cache_records.append(
                        {
                            "evidence_id": evidence_id,
                            "match_kind": cached.get("match_kind"),
                            "source_manifest": cached.get("manifest_path"),
                        }
                    )
                    logger.info(
                        "Protocol PDF Ingest: reused validated cache for %s via %s",
                        evidence_id,
                        cached.get("match_kind"),
                    )
                    continue

            missing.append(
                {
                    "evidence_id": evidence_id,
                    "expected_pdf_filename": expected,
                    "doi": req.get("doi"),
                }
            )

        if reused_cache_records:
            session.context["manual_pdf_cache_reuse"] = reused_cache_records
        else:
            session.context.pop("manual_pdf_cache_reuse", None)

        if missing:
            with open(manifest_path, "w", encoding="utf-8") as f:
                json.dump(list(manifest_by_id.values()), f, indent=2, ensure_ascii=False)
            session.context["anchors_manifest_path"] = os.path.abspath(manifest_path)
            session.context["anchor_methods_paths"] = updated_methods_paths
            session.context["anchor_raw_paths"] = updated_raw_paths
            session.context["manual_pdf_missing"] = missing
            session.context["awaiting_kind"] = "manual_pdf_upload"
            session.context["awaiting_return_state"] = WorkflowState.PROTOCOL_PDF_INGEST.value
            session.context["pipeline_manual_override"] = True
            await self._update_session_state(session, WorkflowState.AWAITING_FEEDBACK)
            await self.memory_manager.store_session(session)
            logger.warning(f"Protocol PDF Ingest: Missing {len(missing)} PDFs for session {session.id}")
            return

        for req in manual_pdf_requests:
            evidence_id = str(req.get("evidence_id") or "").strip()
            expected = req.get("expected_pdf_filename")
            if not evidence_id or not expected:
                continue
            if evidence_id in updated_methods_paths and evidence_id not in resolved_pdf_paths:
                continue
            pdf_path = resolved_pdf_paths[evidence_id]
            try:
                ingest = ingest_pdf_to_anchor(
                    pdf_path=pdf_path,
                    session_id=session.id,
                    evidence_id=evidence_id,
                    llm_model=pdf_llm_model,
                    max_pages=self._protocol_pdf_max_pages(),
                    prefer_pymupdf=self._protocol_pdf_prefer_pymupdf(),
                )
            except PdfPageLimitExceeded as exc:
                updated_methods_paths.pop(str(evidence_id), None)
                updated_raw_paths.pop(str(evidence_id), None)
                failure = {
                    "evidence_id": str(evidence_id),
                    "expected_pdf_filename": expected,
                    "title": req.get("title"),
                    "reason": "pdf_page_limit_exceeded",
                    "page_count": exc.page_count,
                    "max_pages": exc.max_pages,
                    "message": (
                        f"PDF has {exc.page_count} pages; provide an article or chapter PDF "
                        f"with at most {exc.max_pages} pages."
                    ),
                }
                page_limit_failures.append(failure)
                entry = manifest_by_id.get(str(evidence_id), {})
                entry.update(
                    {
                        "evidence_id": str(evidence_id),
                        "expected_pdf_filename": expected,
                        "title": req.get("title"),
                        "pdf_page_limit_status": "rejected",
                        "pdf_page_count": exc.page_count,
                        "pdf_max_pages": exc.max_pages,
                    }
                )
                manifest_by_id[str(evidence_id)] = entry
                logger.warning(
                    "Protocol PDF Ingest: rejected oversized PDF for %s (%s pages; limit=%s)",
                    evidence_id,
                    exc.page_count,
                    exc.max_pages,
                )
                continue
            if ingest.get("methods_extract_mode") == "fallback_full":
                try:
                    recovery = await recover_methods_markdown_with_llm(
                        raw_fulltext_md_path=ingest["raw_fulltext_md_path"],
                        methods_md_path=ingest["methods_md_path"],
                        global_config=self.config,
                        llm_model=pdf_llm_model,
                        task_query=task_query,
                    )
                    ingest["methods_recovery_report_path"] = recovery.get("report_path")
                    if recovery.get("success"):
                        ingest["methods_extract_mode"] = recovery.get("methods_extract_mode") or "llm_block_recovery"
                        logger.info(
                            "Protocol PDF Ingest: recovered structured markdown for %s in session %s",
                            evidence_id,
                            session.id,
                        )
                    else:
                        logger.warning(
                            "Protocol PDF Ingest: LLM recovery did not produce a usable procedure section for %s in session %s",
                            evidence_id,
                            session.id,
                        )
                except Exception as recovery_error:
                    logger.warning(
                        "Protocol PDF Ingest: LLM recovery failed for %s in session %s: %s",
                        evidence_id,
                        session.id,
                        recovery_error,
                    )

            identity = await verify_anchor_pdf_identity_with_llm(
                raw_fulltext_md_path=ingest["raw_fulltext_md_path"],
                methods_md_path=ingest["methods_md_path"],
                expected_title=str(req.get("title") or ""),
                task_query=task_query,
                semantic_contract=req.get("semantic_contract") or {},
                global_config=self.config,
                llm_model=pdf_llm_model,
            )
            ingest["pdf_identity_report_path"] = identity.get("report_path")
            ingest["pdf_identity_decision"] = identity.get("decision")
            if identity.get("decision") == "reject":
                updated_methods_paths.pop(str(evidence_id), None)
                updated_raw_paths.pop(str(evidence_id), None)
                identity_failures.append(
                    {
                        "evidence_id": str(evidence_id),
                        "expected_pdf_filename": expected,
                        "title": req.get("title"),
                        "reason": identity.get("reason"),
                        "contradictions": identity.get("contradictions") or [],
                        "report_path": identity.get("report_path"),
                    }
                )
            else:
                updated_methods_paths[str(evidence_id)] = ingest["methods_md_path"]
                updated_raw_paths[str(evidence_id)] = ingest["raw_fulltext_md_path"]

            entry = manifest_by_id.get(str(evidence_id), {})
            entry.update(
                {
                    "evidence_id": str(evidence_id),
                    "title": entry.get("title") or req.get("title"),
                    "url": entry.get("url") or req.get("url"),
                    "doi": entry.get("doi") or req.get("doi"),
                    "pmid": entry.get("pmid") or req.get("pmid"),
                    "pmcid": entry.get("pmcid") or req.get("pmcid"),
                    "methods_md_path": ingest["methods_md_path"],
                    "raw_fulltext_md_path": ingest["raw_fulltext_md_path"],
                    "file_path": ingest["methods_md_path"],
                    "raw_file_path": ingest["raw_fulltext_md_path"],
                    "fulltext_strategy": ingest["fulltext_strategy"],
                    "fulltext_quality": ingest["fulltext_quality"],
                    "methods_extract_mode": ingest["methods_extract_mode"],
                    "methods_recovery_report_path": ingest.get("methods_recovery_report_path"),
                    "pdf_identity_decision": ingest.get("pdf_identity_decision"),
                    "pdf_identity_report_path": ingest.get("pdf_identity_report_path"),
                    "pdf_page_count": ingest.get("pdf_page_count"),
                    "pdf_max_pages": ingest.get("pdf_max_pages"),
                    "pymupdf_preferred": ingest.get("pymupdf_preferred"),
                    "pymupdf_candidate_decision": ingest.get(
                        "pymupdf_candidate_decision"
                    ),
                    "pymupdf_candidate_stats": ingest.get(
                        "pymupdf_candidate_stats"
                    ),
                    "resolved_pdf_path": os.path.abspath(pdf_path),
                    "pdf_resolution_strategy": (
                        "expected_filename"
                        if os.path.basename(pdf_path) == os.path.basename(str(expected))
                        else "identifier_alias"
                    ),
                }
            )
            manifest_by_id[str(evidence_id)] = entry

        new_manifest = list(manifest_by_id.values())
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(new_manifest, f, indent=2, ensure_ascii=False)

        session.context["anchors_manifest_path"] = os.path.abspath(manifest_path)
        session.context["anchor_methods_paths"] = updated_methods_paths
        session.context["anchor_raw_paths"] = updated_raw_paths
        if page_limit_failures:
            session.context["manual_pdf_page_limit_failures"] = page_limit_failures
        else:
            session.context.pop("manual_pdf_page_limit_failures", None)
        if identity_failures or page_limit_failures:
            if identity_failures:
                session.context["manual_pdf_identity_failures"] = identity_failures
            else:
                session.context.pop("manual_pdf_identity_failures", None)
            session.context["awaiting_kind"] = "manual_pdf_upload"
            session.context["awaiting_return_state"] = WorkflowState.PROTOCOL_PDF_INGEST.value
            session.context["pipeline_manual_override"] = True
            await self._update_session_state(session, WorkflowState.AWAITING_FEEDBACK)
            await self.memory_manager.store_session(session)
            logger.warning(
                "Protocol PDF Ingest: rejected %s identity-mismatched and %s oversized PDF(s) for session %s",
                len(identity_failures),
                len(page_limit_failures),
                session.id,
            )
            return

        session.context.pop("manual_pdf_identity_failures", None)
        session.context.pop("manual_pdf_page_limit_failures", None)
        session.context.pop("manual_pdf_missing", None)
        session.context.pop("awaiting_kind", None)
        session.context.pop("awaiting_return_state", None)

        primary_anchor_id = session.context.get("primary_anchor_id")
        if primary_anchor_id:
            session.context["anchor_ids"] = [str(primary_anchor_id)]
            ev = session.context.get("evidence_bundle")
            if isinstance(ev, dict):
                ev["anchor_ids"] = [str(primary_anchor_id)]
                session.context["evidence_bundle"] = ev
            if str(primary_anchor_id) in updated_methods_paths and str(primary_anchor_id) in updated_raw_paths:
                session.context["anchor_methods_paths"] = {str(primary_anchor_id): updated_methods_paths[str(primary_anchor_id)]}
                session.context["anchor_raw_paths"] = {str(primary_anchor_id): updated_raw_paths[str(primary_anchor_id)]}

        await self.memory_manager.store_session(session)
        logger.info(f"Protocol PDF Ingest: Completed for session {session.id}")

    @staticmethod
    def _to_int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_opt_candidates(raw_candidates: Any) -> List[Dict[str, Any]]:
        if raw_candidates is None:
            return []
        if isinstance(raw_candidates, dict):
            for key in ("candidates", "points", "warm_start_points"):
                if isinstance(raw_candidates.get(key), list):
                    raw_candidates = raw_candidates.get(key)
                    break
            else:
                raw_candidates = []

        if not isinstance(raw_candidates, list):
            return []

        normalized: List[Dict[str, Any]] = []
        for item in raw_candidates:
            if not isinstance(item, dict):
                continue
            params = item.get("params") or item.get("config")
            if not isinstance(params, dict):
                params = {
                    k: v
                    for k, v in item.items()
                    if k not in {"score", "source", "id", "meta", "metadata", "params", "config"}
                }
            if not params:
                continue
            normalized.append(
                {
                    "params": params,
                    "score": item.get("score"),
                    "source": item.get("source", "warm_start"),
                }
            )
        return normalized

    @staticmethod
    def _sample_candidate_from_space(search_space: Dict[str, Any], rng: random.Random) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if not isinstance(search_space, dict):
            return params

        for name, spec in search_space.items():
            if isinstance(spec, dict):
                choices = spec.get("choices") or spec.get("values")
                if isinstance(choices, list) and choices:
                    params[name] = rng.choice(choices)
                    continue

                bounds = spec.get("bounds")
                if isinstance(bounds, list) and len(bounds) == 2:
                    low, high = bounds
                else:
                    low, high = spec.get("min"), spec.get("max")

                low_f = OrchestrationAgent._to_float(low)
                high_f = OrchestrationAgent._to_float(high)
                if low_f is not None and high_f is not None:
                    if str(spec.get("type", "")).lower() in {"int", "integer"}:
                        params[name] = rng.randint(int(low_f), int(high_f))
                    else:
                        params[name] = round(rng.uniform(low_f, high_f), 6)
                    continue

                if "default" in spec:
                    params[name] = spec.get("default")
            elif isinstance(spec, list) and spec:
                params[name] = rng.choice(spec)
            elif isinstance(spec, tuple) and spec:
                params[name] = rng.choice(list(spec))
        return params

    @staticmethod
    def _evaluate_optimization_candidate(
        params: Dict[str, Any],
        objective: Dict[str, Any],
        step_idx: int,
    ) -> float:
        numeric_values: List[float] = []
        categorical_hash = 0.0
        for value in (params or {}).values():
            if isinstance(value, (int, float)):
                numeric_values.append(float(value))
            else:
                categorical_hash += (abs(hash(str(value))) % 17) / 1000.0

        base = (sum(numeric_values) / max(1, len(numeric_values))) + categorical_hash
        target = OrchestrationAgent._to_float((objective or {}).get("target"))
        if target is not None:
            base -= abs(base - target)

        # Small deterministic decay to avoid identical ties across many generated points.
        base -= step_idx * 1e-6
        return round(base, 6)

    @staticmethod
    def _is_better_optimization_score(
        new_score: float,
        best_score: Optional[float],
        maximize: bool,
    ) -> bool:
        if best_score is None:
            return True
        return new_score > best_score if maximize else new_score < best_score

    async def _run_opt_prepare_phase(self, session: WorkflowSession) -> None:
        """Prepare optimization payload and initialize candidate/session buffers."""
        logger.info("Optimization Agent: preparing optimization session %s", session.id)

        payload = session.context.get("payload")
        payload_dict = payload if isinstance(payload, dict) else {}

        search_space = payload_dict.get("search_space") or {}
        objective = payload_dict.get("objective") or {"metric": "score", "direction": "maximize"}
        budget = payload_dict.get("budget") or {}
        warm_start = payload_dict.get("warm_start") or {}
        runtime = payload_dict.get("runtime") or {}

        # Keep task fields synchronized for downstream consumers.
        if hasattr(session.task, "search_space"):
            session.task.search_space = search_space
        if hasattr(session.task, "objective"):
            session.task.objective = objective
        if hasattr(session.task, "budget"):
            session.task.budget = budget

        warm_start_candidates = self._normalize_opt_candidates(warm_start)
        if not warm_start_candidates and isinstance(payload_dict.get("warm_start"), list):
            warm_start_candidates = self._normalize_opt_candidates(payload_dict.get("warm_start"))

        rng = random.Random(abs(hash(f"{session.id}:prepare")) % (2**32))
        if not warm_start_candidates and isinstance(search_space, dict) and search_space:
            seed_params = self._sample_candidate_from_space(search_space, rng)
            if seed_params:
                warm_start_candidates.append(
                    {"params": seed_params, "score": None, "source": "generated_seed"}
                )

        session.candidate_pool = warm_start_candidates
        session.optimization_trace = []
        session.best_params = {}
        session.iterations_completed = 0
        session.context["optimization_input"] = {
            "search_space": search_space,
            "objective": objective,
            "budget": budget,
            "warm_start": warm_start,
            "runtime": runtime,
            "prepared_at": datetime.now().isoformat(),
        }

        logger.info(
            "Optimization Agent: prepared %d warm-start candidates for session %s",
            len(session.candidate_pool),
            session.id,
        )

        if not self._uses_pipeline(session):
            await self._update_session_state(session, WorkflowState.OPTIMIZATION)

    async def _run_opt_optimization_phase(self, session: WorkflowSession) -> None:
        """
        Execute experiment-optimization loop and update:
        - candidate_pool
        - optimization_trace
        - best_params
        """
        logger.info("Optimization Agent: optimizing session %s", session.id)

        opt_input = session.context.get("optimization_input") or {}
        search_space = opt_input.get("search_space") or {}
        objective = opt_input.get("objective") or {"metric": "score", "direction": "maximize"}
        budget = opt_input.get("budget") or {}

        direction = str(objective.get("direction", "maximize")).lower()
        maximize = direction not in {"min", "minimize", "minimum"}

        num_rounds = self._to_int(budget.get("num_rounds") or budget.get("rounds"), 5)
        num_experiments = self._to_int(budget.get("num_experiments") or budget.get("experiments"), 1)
        total_steps = max(1, num_rounds) * max(1, num_experiments)

        rng = random.Random(abs(hash(f"{session.id}:optimize")) % (2**32))
        candidate_pool = list(session.candidate_pool or [])
        trace = list(session.optimization_trace or [])
        best = dict(session.best_params or {})
        best_score = self._to_float(best.get("score"))

        while len(trace) < total_steps:
            if candidate_pool:
                candidate = candidate_pool.pop(0)
            else:
                sampled = self._sample_candidate_from_space(search_space, rng)
                if not sampled:
                    sampled = {"step": len(trace) + 1}
                candidate = {"params": sampled, "score": None, "source": "generated"}

            params = candidate.get("params") if isinstance(candidate.get("params"), dict) else {}
            raw_score = self._to_float(candidate.get("score"))
            score = (
                raw_score
                if raw_score is not None
                else self._evaluate_optimization_candidate(params, objective, len(trace) + 1)
            )

            step_record = {
                "step": len(trace) + 1,
                "params": params,
                "score": score,
                "source": candidate.get("source", "generated"),
            }
            trace.append(step_record)

            if self._is_better_optimization_score(score, best_score, maximize):
                best = {
                    "step": step_record["step"],
                    "params": params,
                    "score": score,
                    "source": step_record["source"],
                    "direction": "maximize" if maximize else "minimize",
                    "metric": objective.get("metric", "score"),
                }
                best_score = score

        session.optimization_trace = trace
        session.candidate_pool = candidate_pool
        session.best_params = best
        session.iterations_completed = len(trace)
        session.context["optimization_runtime"] = {
            "executed_steps": len(trace),
            "target_steps": total_steps,
            "direction": "maximize" if maximize else "minimize",
            "updated_at": datetime.now().isoformat(),
        }

        logger.info(
            "Optimization Agent: completed %d optimization steps for session %s",
            len(trace),
            session.id,
        )

        if not self._uses_pipeline(session):
            await self._update_session_state(session, WorkflowState.OPTIMIZATION_SUMMARY)

    async def _run_opt_summary_phase(self, session: WorkflowSession) -> None:
        """Write optimization summary into session context."""
        logger.info("Optimization Agent: summarizing session %s", session.id)

        trace = list(session.optimization_trace or [])
        best = dict(session.best_params or {})
        opt_input = session.context.get("optimization_input") or {}

        session.context["optimization_summary"] = {
            "session_id": session.id,
            "task_type": session.task_type,
            "steps": len(trace),
            "best_params": best.get("params", {}),
            "best_score": best.get("score"),
            "direction": best.get("direction", "maximize"),
            "metric": best.get("metric", "score"),
            "objective": opt_input.get("objective", {}),
            "budget": opt_input.get("budget", {}),
            "completed_at": datetime.now().isoformat(),
        }

        if not self._uses_pipeline(session):
            await self._update_session_state(session, WorkflowState.COMPLETED)

    async def _run_generation_phase(self, session: WorkflowSession) -> None:
        """
        Execute idea generation phase.

        Invokes generation agent to create initial hypothesis candidates. Optionally runs
        survey agent for literature if configured. Creates idea objects from agent responses
        and adds to session. Transitions to reflection phase on success.

        Args:
            session (WorkflowSession): Session to process
        """
        logger.info(f"Idea Innovation Agent: Starting idea generation for session {session.id}")

        if not self.disable_kg:
            await self._ensure_graph_hypotheses(session)
        else:
            session.graph_hypotheses = []

        # If we already have graph-derived drug hypotheses, rank them via LLM then attach as ideas
        if session.graph_hypotheses and not self.disable_kg:
            ranked = await self._rank_graph_hypotheses(session)
            max_graph = self.graph_max_candidates
            if not (isinstance(max_graph, int) and max_graph > 0):
                max_graph = len(ranked or session.graph_hypotheses)
            subset = (ranked or session.graph_hypotheses)[:max_graph]
            ideas_added = self._attach_graph_hypotheses_as_ideas(
                subset, session, target_iteration=session.iterations_completed + 1
            )
            logger.info(
                "Graph hypotheses ranked and attached as ideas (count=%d, truncated to graph_max_candidates=%s); skipping GenerationAgent.",
                ideas_added,
                self.graph_max_candidates,
            )
            if not self._uses_pipeline(session):
                await self._update_session_state(session, WorkflowState.REFLECTING)
            return

        generation_agent = self._get_agent("generation")
        if not generation_agent:
            raise ValueError("Generation agent initialization failed")

        paper_lst = None
        if generation_agent.config.get("do_survey", False):
            logger.info(f"Survey Agent: Conduct in-depth literature research on task {session.id}")
            survey_agent = self._get_agent("survey")
            if survey_agent:
                paper_lst = await survey_agent.run_with_logging(session.task.to_dict(), {})

        context = {
            "goal": session.task.to_dict(),
            "iteration": session.iterations_completed,
            "feedback": session.feedback_history,
            "paper_lst": paper_lst,
            "graph_hypotheses": session.graph_hypotheses,
        }
        gen_params: Dict[str, Any] = {}
        if session.graph_hypotheses and not self.disable_kg:
            gen_params["count"] = len(session.graph_hypotheses)

        try:
            response = await generation_agent.execute(context, gen_params)

            for idx, idea_data in enumerate(response.get("hypotheses", [])):
                idea = Idea(
                    id=f"idea_{int(time.time())}_{idx}",
                    drug_name=idea_data.get("drug_name", ""),
                    text=idea_data.get("text", ""),
                    rationale=idea_data.get("rationale", ""),
                    iteration=session.iterations_completed
                )
                session.ideas.append(idea)

            logger.info(f"Idea Innovation Agent: Generated {len(response.get('hypotheses', []))} ideas for session {session.id}")
            if not self._uses_pipeline(session):
                await self._update_session_state(session, WorkflowState.REFLECTING)

        except Exception as e:
            logger.error(f"Error in generation phase: {str(e)}")
            await self._update_session_state(session, WorkflowState.ERROR)

    async def _run_reflection_phase(self, session: WorkflowSession) -> None:
        """
        Execute reflection and critique phase.

        Invokes reflection agent to critically evaluate current iteration ideas. Processes
        ideas concurrently, extracts feedback based on type (global/local), and attaches
        critiques to ideas. Transitions to literature gathering phase on success.

        Args:
            session (WorkflowSession): Session to process
        """
        logger.info(f"Assessment Agent: Starting reflection and critique for session {session.id}")

        reflection_agent = self._get_agent("reflection")
        if not reflection_agent:
            raise ValueError("Reflection agent initialization failed")

        current_iter = session.iterations_completed
        # Ideas attached in generation phase use iteration = current_iter + 1
        ideas = self._get_current_ideas(session, current_iter + 1)

        feedback_content = self._extract_feedback_content(session, ideas)

        tasks = []
        for idea in ideas:
            context = {
                "goal": session.task.to_dict(),
                "hypothesis": idea.to_dict(),
                "iteration": current_iter,
                "feedback": feedback_content if isinstance(feedback_content, str)
                          else feedback_content.get('comment', '') if feedback_content.get("id") == idea.id else ""
            }
            tasks.append(reflection_agent.run_with_logging(context, {}))

        try:
            results = await asyncio.gather(*tasks)

            for idx, idea in enumerate(ideas):
                critiques = results[idx].get("critiques", [])
                idea.critiques = critiques
                if session.task.task_type == "drug_repurposing":
                    issues = results[idx].get("metadata", {}).get("critique_format_issues")
                    if issues:
                        idea.search_metadata["critique_format_issues"] = issues

            logger.info(f"Assessment Agent: Completed reflection and critique for {len(ideas)} ideas in session {session.id}")
            if not self._uses_pipeline(session):
                await self._update_session_state(session, WorkflowState.EXTERNAL_DATA)

        except Exception as e:
            logger.error(f"Error in reflection phase: {str(e)}")
            await self._update_session_state(session, WorkflowState.ERROR)

    async def _run_external_data_phase(self, session: WorkflowSession) -> None:
        """
        Execute paper gathering phase.

        Invokes scholar agent to retrieve supporting literature and evidence for current
        ideas. Uses semaphore for concurrency control. Attaches evidence and references
        to ideas. Transitions to refinement phase if in method phase, else evolution phase.

        Args:
            session (WorkflowSession): Session to process
        """
        logger.info(f"Scholar Agent: Starting literature gathering for session {session.id}")

        scholar_agent = self._get_agent("scholar")
        if not scholar_agent:
            raise ValueError("Scholar agent initialization failed")
        isolate_candidates = session.task.task_type == "drug_repurposing"
        checkpoint_lock = asyncio.Lock()

        current_iter = session.iterations_completed
        # Ideas attached in generation phase use iteration = current_iter + 1
        ideas = self._get_current_ideas(session, current_iter + 1)

        # Use semaphore for concurrency control
        semaphore = asyncio.Semaphore(self.max_concurrent_tasks)
        tasks = []

        # If no ideas are ready (happens in later iterations because evolution runs after this phase),
        # skip quietly to evolution so evidence can be gathered for newly evolved ideas there.
        if not ideas and current_iter > 0:
            logger.info(
                "Scholar Agent: No ideas to gather evidence for in iteration %d; skipping to evolution",
                current_iter,
            )
            if not self._uses_pipeline(session):
                await self._update_session_state(session, WorkflowState.EVOLVING)
            return

        async def execute_with_semaphore(idea):
            async with semaphore:
                parent_idea = None
                if idea.parent_id:
                    parent_idea = next((i for i in session.ideas if i.id == idea.parent_id), None)
                prev_evidence = idea.evidence or (parent_idea.evidence if parent_idea else [])
                prev_references = idea.references or (parent_idea.references if parent_idea else [])
                context = {
                    "goal": session.task.to_dict(),
                    "hypothesis": idea.to_dict(),
                    "prev_evidence": prev_evidence,
                    "prev_references": prev_references,
                    "iteration": current_iter,
                    "gaps": session.gap_reports or [],
                    "graph_constraints": session.graph_constraints or {},
                }
                return await scholar_agent.run_with_logging(context, {})

        async def collect_drug_evidence(idea):
            # Persist each candidate independently, including empty but completed
            # searches. A later candidate must not erase already collected work.
            metadata = idea.search_metadata if isinstance(idea.search_metadata, dict) else {}
            if (metadata.get("literature_status") == "completed"
                    and metadata.get("literature_iteration") == current_iter + 1):
                return
            try:
                result = await execute_with_semaphore(idea)
                evidence = result.get("evidence", [])
                references = result.get("references", [])
                summary = result.get("relevance_summary", "")
                search_metadata = dict(metadata)
                search_metadata.update(result.get("metadata", {}))
                if not isinstance(evidence, list) or not isinstance(references, list) or not isinstance(summary, str):
                    raise ValueError("Invalid literature result fields")
                search_metadata.update(literature_status="completed", literature_iteration=current_iter + 1)
                search_metadata.pop("literature_error", None)
                idea.evidence = evidence
                idea.references = references
                idea.search_queries = search_metadata.get("search_queries", [])
                idea.search_metadata = search_metadata
                idea.relevance_summary = summary
            except Exception as exc:
                idea.search_metadata = dict(metadata, literature_status="failed",
                                            literature_iteration=current_iter + 1,
                                            literature_error={"type": type(exc).__name__, "message": str(exc)})
                logger.error("Literature search failed for candidate %s: %s", idea.id, exc)
            async with checkpoint_lock:
                await self.memory_manager.store_session(session)

        for idea in ideas:
            tasks.append(collect_drug_evidence(idea) if isolate_candidates else execute_with_semaphore(idea))

        try:
            results = await asyncio.gather(*tasks)

            if not isolate_candidates:
                for idx, idea in enumerate(ideas):
                    idea.evidence = results[idx].get("evidence", [])
                    idea.references = results[idx].get("references", [])
                    idea.search_queries = results[idx].get("metadata", {}).get("search_queries", [])
                    idea.search_metadata = results[idx].get("metadata", {})
                    idea.relevance_summary = results[idx].get("relevance_summary", "")

            next_state = WorkflowState.EVOLVING
            # Collect current-iteration gaps (prefer relevance_summary, fallback to critiques), keep concise
            from ..agents.Biologist.DrugRepurposing.evidence.summaries import extract_gap_text as _extract_gap_text

            gap_entries = []
            for idea in ideas:
                if idea.relevance_summary:
                    gap_entries.append({"idea_id": idea.id, "gap": _extract_gap_text(idea.relevance_summary)})
                elif idea.critiques:
                    if isolate_candidates:
                        from ..agents.Biologist.DrugRepurposing.critique_records import split_critique_records
                        records, _ = split_critique_records(idea.critiques)
                        if records:
                            gap_entries.append({"idea_id": idea.id, "gap": "; ".join(c["point"] for c in records[:3])})
                    else:
                        gap_entries.append({"idea_id": idea.id, "gap": "; ".join(idea.critiques[:3])})
            # Limit to avoid oversized context
            session.prev_gap_reports = session.gap_reports
            session.gap_reports = gap_entries[:5]

            logger.info(f"Survey Agent: Completed literature gathering for {len(ideas)} ideas in session {session.id}")
            if not self._uses_pipeline(session):
                await self._update_session_state(session, next_state)

        except Exception as e:
            logger.error(f"Error in literature survey phase: {str(e)}")
            await self._update_session_state(session, WorkflowState.ERROR)

    async def _run_evolution_phase(self, session: WorkflowSession) -> None:
        """
        Execute idea evolution phase.

        Invokes evolution agent to refine current iteration ideas by addressing critiques
        and incorporating evidence. Creates evolved idea variants with parent tracking,
        adds to session for next iteration. Transitions to ranking phase on success.

        Args:
            session (WorkflowSession): Session to process
        """
        logger.info(f"Idea Innovation Agent: Starting idea evolution for session {session.id}")

        evolution_agent = self._get_agent("evolution")
        if not evolution_agent:
            raise ValueError("Evolution agent initialization failed")

        current_iter = session.iterations_completed
        ideas = self._get_current_ideas(session, current_iter)

        evolved_ideas = []
        for idea in ideas:
            context = {
                "goal": session.task.to_dict(),
                "hypothesis": idea.to_dict(),
                "critiques": idea.critiques,
                "evidence": idea.evidence,
                "feedback": session.feedback_history,
                "iteration": current_iter
            }

            try:
                response = await evolution_agent.run_with_logging(context, {})

                evolutions = response.get("evolved_hypotheses", [])
                for idx, evolution in enumerate(evolutions):
                    drug_name = evolution.get("drug_name") or idea.drug_name
                    evolved_idea = Idea(
                        id=f"idea_{int(time.time())}_{idx}_{idea.id}",
                        drug_name=drug_name,
                        text=evolution.get("text", ""),
                        rationale=evolution.get("rationale", ""),
                        iteration=current_iter + 1,
                        parent_id=idea.id
                    )
                    evolved_ideas.append(evolved_idea)

            except Exception as e:
                logger.error(f"Error evolving idea {idea.id}: {str(e)}")

        session.ideas.extend(evolved_ideas)
        logger.info(f"Idea Innovation Agent: Evolved {len(evolved_ideas)} ideas from {len(ideas)} parent ideas in session {session.id}")

        # If evolution produced new ideas (common in later iterations), immediately gather evidence
        # for them so downstream ranking/lineage keeps references populated.
        if evolved_ideas:
            scholar_agent = self._get_agent("scholar")
            if scholar_agent:
                semaphore = asyncio.Semaphore(self.max_concurrent_tasks)
                gather_tasks = []

                async def gather_evidence(idea_obj):
                    async with semaphore:
                        ctx = {
                            "goal": session.task.to_dict(),
                            "hypothesis": idea_obj.to_dict(),
                            "iteration": current_iter + 1,
                            "gaps": session.gap_reports or [],
                            "graph_constraints": session.graph_constraints or {},
                        }
                        return await scholar_agent.execute(ctx, {})

                for evo in evolved_ideas:
                    gather_tasks.append(gather_evidence(evo))

                try:
                    gather_results = await asyncio.gather(*gather_tasks)
                    for idx, evo in enumerate(evolved_ideas):
                        res = gather_results[idx] or {}
                        evo.evidence = res.get("evidence", [])
                        evo.references = res.get("references", [])
                        evo.search_queries = res.get("metadata", {}).get("search_queries", [])
                        evo.search_metadata = res.get("metadata", {})
                        evo.relevance_summary = res.get("relevance_summary", "")
                    # Update gaps based on the newest ideas
                    from ..agents.Biologist.DrugRepurposing.evidence.summaries import extract_gap_text as _extract_gap_text

                    gap_entries = []
                    for evo in evolved_ideas:
                        if evo.relevance_summary:
                            gap_entries.append({"idea_id": evo.id, "gap": _extract_gap_text(evo.relevance_summary)})
                        elif evo.critiques:
                            gap_entries.append({"idea_id": evo.id, "gap": "; ".join(evo.critiques[:3])})
                    session.prev_gap_reports = session.gap_reports
                    session.gap_reports = gap_entries[:5]
                except Exception as exc:
                    logger.error(f"Error gathering evidence for evolved ideas: {exc}")
            else:
                logger.warning("Scholar agent unavailable; evolved ideas have no evidence attached")

        if not self._uses_pipeline(session):
            await self._update_session_state(session, WorkflowState.RANKING)

    async def _ensure_graph_hypotheses(self, session: WorkflowSession) -> None:
        """
        Ensure graph-derived one-hop drug candidates are available on the session.

        Uses the disease_intent agent to parse user intent, standardize targets,
        and pull one-hop drug neighbors from the KG.

        # Historical path (now replaced but retained for context):
        # Use kg_predicting (GNN) + optional bio_filter to produce candidates.
        # context = {"goal": session.task.to_dict(), "iteration": session.iterations_completed, "diseases": [...]}
        # result = await kg_predicting_agent.execute(context, {})
        # predictions = result.get("candidates", [])
        # session.kg_predictions = predictions
        """
        if session.graph_hypotheses:
            return

        agent = self._get_agent("disease_intent")
        if not agent:
            logger.info("DiseaseIntent agent not configured; skipping graph hypothesis fetch.")
            return

        user_input = session.task.description or session.task.goal or ""
        context = {
            "user_input": user_input,
            "goal": session.task.to_dict(),
        }

        try:
            result = await agent.execute(context, {})
        except Exception as exc:
            logger.error("DiseaseIntent agent failed: %s", exc)
            return
        session.graph_targets = result.get("standardized_targets", []) or []
        # Record KG constraints: keyword/target and target-drug edges for downstream hard constraints
        target_constraints = []
        for t in session.graph_targets:
            label = t.get("matched_label") or t.get("name")
            if label and t.get("matched_id"):
                target_constraints.append(
                    {
                        "target_id": t.get("matched_id"),
                        "target_label": label,
                        "evidence": t.get("evidence"),
                    }
                )

        # Apply relation filter at orchestration level to keep key targets
        rel_agent = self._get_agent("relation_filter")
        filtered_targets = session.graph_targets
        if rel_agent and session.graph_targets:
            try:
                rel_res = await rel_agent.execute(
                    {"goal": context.get("goal") or {}, "diseases": [result.get("disease")]},
                    {"targets": session.graph_targets, "diseases": [result.get("disease")]},
                )
                filtered_targets = rel_res.get("filtered_targets") or session.graph_targets
                
                if not filtered_targets:
                    filtered_targets = session.graph_targets
            except Exception as exc:
                logger.warning("Relation filter agent failed, using unfiltered targets: %s", exc)
        
        filtered_targets = [t for t in filtered_targets if t.get("matched_id")]
        all_targets_for_known_filter = [
            t for t in (session.graph_targets or []) if isinstance(t, dict) and t.get("matched_id")
        ]

        exploration_mode = (self.exploration_mode or "conservative").lower()
        use_prediction = exploration_mode == "innovative"
        use_one_hop = exploration_mode == "conservative"

        if use_prediction and filtered_targets:
            try:
                logger.info(
                    "Innovative mode: filtered_targets=%s",
                    [
                        {
                            "name": t.get("matched_label") or t.get("name"),
                            "matched_id": t.get("matched_id"),
                            "reason": t.get("reason"),
                            "evidence": t.get("evidence"),
                        }
                        for t in filtered_targets
                    ],
                )
            except Exception:
                logger.info("Innovative mode: filtered_targets logging failed")

        # Optional: predict drug candidates from filtered targets (innovative mode only)
        predicted_hypotheses = []
        pred_agent = self._get_agent("drug_target_prediction") if use_prediction else None
        if pred_agent and filtered_targets:
            try:
                pred_res = await pred_agent.execute(
                    {"goal": context.get("goal") or {}, "disease": result.get("disease")},
                    {
                        "targets": filtered_targets,
                        "known_filter_targets": all_targets_for_known_filter,
                    },
                )
                predicted_hypotheses = pred_res.get("predicted_hypotheses") or []
                session.tool_usage["drug_target_prediction"] = (
                    session.tool_usage.get("drug_target_prediction", 0) + 1
                )
            except Exception as exc:
                logger.warning("Drug-target prediction agent failed, skipping predicted candidates: %s", exc)

        # Compute one-hop hypotheses from filtered targets (conservative mode only)
        try:
            disease_for_graph = result.get("disease") or session.task.id or getattr(session.task, "domain", None) or session.task.description
            graph_hypotheses_raw = []
            if use_one_hop:
                graph_res = await agent._graph_one_hop_hypotheses(filtered_targets, disease_for_graph)
                graph_hypotheses_raw = graph_res[0] if isinstance(graph_res, tuple) else graph_res
                graph_hypotheses_raw = [
                    {**gh, "source": gh.get("source") or "kg_one_hop"} for gh in (graph_hypotheses_raw or [])
                ]
            predicted_hypotheses = [
                {**gh, "source": gh.get("source") or "drug_protein_prediction"}
                for gh in (predicted_hypotheses or [])
            ]

            combined_raw = (graph_hypotheses_raw or []) if use_one_hop else (predicted_hypotheses or [])
            total_targets = len(filtered_targets)
            session.graph_hypotheses = self._aggregate_hypotheses_by_drug(
                combined_raw, total_targets=total_targets
            )
            if use_prediction:
                try:
                    logger.info(
                        "Innovative mode: aggregated_candidates=%s",
                        [
                            {
                                "drug": gh.get("drug_label") or gh.get("drug_id"),
                                "score": gh.get("score"),
                                "hard_score": gh.get("hard_score"),
                                "coverage": gh.get("coverage_count"),
                                "targets": gh.get("target_labels") or gh.get("target_label"),
                                "source": gh.get("source"),
                            }
                            for gh in (session.graph_hypotheses or [])[:30]
                        ],
                    )
                except Exception:
                    logger.info("Innovative mode: aggregated_candidates logging failed")
            # Target-drug edge constraints
            edge_constraints = []
            for gh in combined_raw:
                if gh.get("drug_id") and gh.get("target_id"):
                    edge_constraints.append(
                        {
                            "drug_id": gh.get("drug_id"),
                            "drug_label": gh.get("drug_label"),
                            "target_id": gh.get("target_id"),
                            "target_label": gh.get("target_label"),
                            "path": gh.get("path"),
                        }
                    )
            session.graph_constraints = {
                "targets": target_constraints,
                "edges": edge_constraints,
            }
        except Exception as exc:
            logger.warning("Failed to compute graph hypotheses: %s", exc)
            combined_raw = predicted_hypotheses or []
            total_targets = len(filtered_targets)
            session.graph_hypotheses = self._aggregate_hypotheses_by_drug(
                combined_raw, total_targets=total_targets
            )

        session.tool_usage["disease_intent"] = session.tool_usage.get("disease_intent", 0) + 1

    @staticmethod
    def _aggregate_hypotheses_by_drug(
        hypotheses: List[Dict[str, Any]], total_targets: int = 0
    ) -> List[Dict[str, Any]]:
        if not hypotheses:
            return []

        # Normalize scores per target to avoid target-specific score scale bias
        per_target_scores: Dict[str, List[float]] = {}
        for gh in hypotheses:
            target_id = gh.get("target_id") or gh.get("target_label")
            if not target_id:
                continue
            score = gh.get("score")
            if isinstance(score, (int, float)):
                per_target_scores.setdefault(str(target_id), []).append(float(score))

        per_target_minmax: Dict[str, Tuple[float, float]] = {}
        for tid, vals in per_target_scores.items():
            if not vals:
                continue
            per_target_minmax[tid] = (min(vals), max(vals))

        def _safe_score(item: Dict[str, Any]) -> float:
            score = item.get("score")
            if isinstance(score, (int, float)):
                return float(score)
            return 1.0

        def _norm_score(item: Dict[str, Any]) -> float:
            target_id = item.get("target_id") or item.get("target_label")
            if not target_id:
                return _safe_score(item)
            minmax = per_target_minmax.get(str(target_id))
            raw = _safe_score(item)
            if not minmax:
                return raw
            min_v, max_v = minmax
            if max_v <= min_v:
                return 1.0
            return (raw - min_v) / (max_v - min_v)

        grouped: Dict[str, Dict[str, Any]] = {}
        for gh in hypotheses:
            drug_id = gh.get("drug_id") or gh.get("drug_label") or gh.get("drug_name")
            if not drug_id:
                continue
            entry = grouped.setdefault(
                drug_id,
                {
                    "drug_id": gh.get("drug_id"),
                    "drug_label": gh.get("drug_label") or gh.get("drug_name") or drug_id,
                    "target_ids": [],
                    "target_labels": [],
                    "scores": [],
                    "sources": set(),
                    "known_any": False,
                },
            )
            target_id = gh.get("target_id") or gh.get("target_label")
            target_label = gh.get("target_label") or gh.get("target_id") or ""
            if target_id and target_id not in entry["target_ids"]:
                entry["target_ids"].append(target_id)
            if target_label and target_label not in entry["target_labels"]:
                entry["target_labels"].append(target_label)
            entry["scores"].append(_safe_score(gh))
            entry.setdefault("scores_norm", []).append(_norm_score(gh))
            if gh.get("source"):
                entry["sources"].add(gh.get("source"))
            if gh.get("known") is True:
                entry["known_any"] = True

        aggregated: List[Dict[str, Any]] = []
        for drug_id, entry in grouped.items():
            scores = entry["scores"] or [1.0]
            score_mean = sum(scores) / len(scores)
            score_max = max(scores)
            scores_norm = entry.get("scores_norm") or scores
            score_norm_mean = sum(scores_norm) / len(scores_norm)
            score_norm_max = max(scores_norm)
            coverage_count = len(entry["target_ids"])
            coverage_ratio = (coverage_count / total_targets) if total_targets else 0.0
            # Hard score: use per-target normalized scores to avoid target scale bias
            hard_score = (0.6 * score_norm_mean) + (0.3 * score_norm_max) + (0.1 * coverage_ratio)
            targets = entry["target_labels"]
            if len(targets) > 3:
                target_label = "; ".join(targets[:3]) + "; ..."
            else:
                target_label = "; ".join(targets) if targets else "unknown_target"

            sources = entry["sources"]
            source = "mixed" if len(sources) > 1 else (next(iter(sources)) if sources else None)

            aggregated.append(
                {
                    "drug_id": entry["drug_id"] or drug_id,
                    "drug_label": entry["drug_label"],
                    "target_id": entry["target_ids"][0] if entry["target_ids"] else None,
                    "target_label": target_label,
                    "target_ids": entry["target_ids"],
                    "target_labels": entry["target_labels"],
                    "score_mean": score_mean,
                    "score_max": score_max,
                    "score_norm_mean": score_norm_mean,
                    "score_norm_max": score_norm_max,
                    "coverage_count": coverage_count,
                    "coverage_ratio": coverage_ratio if total_targets else None,
                    "hard_score": hard_score,
                    "score": hard_score,
                    "source": source,
                    "known": entry["known_any"],
                    "path": f"{entry['drug_label']} -> {target_label}",
                    "note": "Aggregated across targets",
                }
            )

        aggregated.sort(
            key=lambda x: (x.get("hard_score", 0.0), x.get("coverage_count", 0), x.get("score_mean", 0.0)),
            reverse=True,
        )
        return aggregated

    async def _run_ranking_phase(self, session: WorkflowSession) -> None:
        """
        Execute idea ranking and scoring phase.

        Invokes ranking agent to evaluate and score ideas from current iteration. Updates
        idea scores and identifies top performers. Increments iteration counter. Transitions
        based on phase (method phase → awaiting feedback, max iterations → method development,
        else → awaiting feedback).

        Args:
            session (WorkflowSession): Session to process
        """
        logger.info(f"Assessment Agent: Starting idea ranking and scoring for session {session.id}")

        ranking_agent = self._get_agent("ranking")
        if not ranking_agent:
            raise ValueError("Ranking agent initialization failed")

        current_iter = session.iterations_completed
        ideas = [i for i in session.ideas if i.iteration == (current_iter + 1)]

        if not ideas:
            logger.warning(f"No ideas found for iteration {current_iter}")
            await self._update_session_state(session, WorkflowState.ERROR)
            return

        context = {
            "goal": session.task.to_dict(),
            "hypotheses": [i.to_dict() for i in ideas],
            "iteration": current_iter + 1,
            "feedback": session.feedback_history
        }

        try:
            response = await ranking_agent.run_with_logging(context, {})

            # Update idea scores
            ranked_ideas = response.get("ranked_hypotheses", [])
            for ranked in ranked_ideas:
                idea_id = ranked.get("id")
                if idea_id:
                    idea = next((i for i in ideas if i.id == idea_id), None)
                    if idea:
                        idea.score = ranked.get("overall_score", 0.0)
                        idea.scores = ranked.get("criteria_scores", {})
                        idea.ranking_rationale = ranked.get("scoring_rationale", "")
                        idea.ranking_explanation = response.get("scoring_explanation", "")

            session.top_ideas = response.get("top_hypotheses", [])
            session.iterations_completed += 1

            logger.info(f"Assessment Agent: Completed ranking of {len(ideas)} ideas, selected top {len(session.top_ideas)} for session {session.id}")
            # Determine next state
            if not self._uses_pipeline(session):
                if session.iterations_completed >= session.max_iterations:
                    await self._update_session_state(session, WorkflowState.COMPLETED)
                else:
                    await self._update_session_state(session, WorkflowState.AWAITING_FEEDBACK)

        except Exception as e:
            logger.error(f"Error in ranking phase: {str(e)}")
            await self._update_session_state(session, WorkflowState.ERROR)

    async def _run_awaiting_feedback_phase(self, session: WorkflowSession) -> None:
        """
        Handle awaiting feedback state.

        Passive phase where session waits for external scientist feedback via add_feedback().
        Does not execute agent tasks. External systems detect this state and provide input.
        Session automatically transitions to reflection phase when feedback is added.

        Args:
            session (WorkflowSession): Session in waiting state
        """
        logger.info(f"System: Session {session.id} is awaiting external feedback")

        # This phase doesn't do anything - it just waits for external feedback
        # The external IdeaGenerator (stage.py) will detect this state and provide feedback
        # Once feedback is added via add_feedback(), the session will transition to REFLECTING

        # For safety, if we've been waiting too long or something goes wrong,
        # we should have a timeout or fallback mechanism
        pass

    # Helper methods

    def _register_default_packs(self) -> None:
        """Register built-in task phase packs."""
        self.handler_registry.register_pack("drug_repurposing", DRUG_REPURPOSING_PACK)
        self.handler_registry.register_pack("experiment_optimization", EXPERIMENT_OPTIMIZATION_PACK)
        self.handler_registry.register_pack("protocol_generation", PROTOCOL_GENERATION_PACK)

    def _default_phase_handlers(self) -> Dict[WorkflowState, Callable]:
        """Fallback handlers used when a task-specific pack does not define a state."""
        return {
            WorkflowState.GENERATING: self._run_generation_phase,
            WorkflowState.REFLECTING: self._run_reflection_phase,
            WorkflowState.EVOLVING: self._run_evolution_phase,
            WorkflowState.RANKING: self._run_ranking_phase,
            WorkflowState.EXTERNAL_DATA: self._run_external_data_phase,
            WorkflowState.AWAITING_FEEDBACK: self._run_awaiting_feedback_phase,
            WorkflowState.PROTOCOL_DOMAIN_PACK: self._handle_protocol_domain_pack,
            WorkflowState.PROTOCOL_RESEARCH: self._handle_protocol_research,
            WorkflowState.PROTOCOL_PDF_INGEST: self.handle_protocol_pdf_ingest,
            WorkflowState.PROTOCOL_PLANNING: self.handle_protocol_planning,
            WorkflowState.PROTOCOL_CURATOR: self.handle_legacy_protocol_curator,
            WorkflowState.PROTOCOL_GROUNDED_WRITER: self.handle_protocol_grounded_writer,
            WorkflowState.PROTOCOL_CRITIC: self.handle_protocol_critic,
            WorkflowState.PROTOCOL_ACTION: self.handle_protocol_action,
            WorkflowState.OPTIMIZATION_PREPARE: self._run_opt_prepare_phase,
            WorkflowState.OPTIMIZATION: self._run_opt_optimization_phase,
            WorkflowState.OPTIMIZATION_SUMMARY: self._run_opt_summary_phase,
        }

    def _get_pack_name_for_task_type(self, task_type: str) -> str:
        if task_type in self.task_handler_packs:
            return self.task_handler_packs[task_type]
        return task_type or self.default_handler_pack

    def _get_handler_by_task_type(
        self, task_type: str, state: WorkflowState
    ) -> Optional[Callable]:
        pack_name = self._get_pack_name_for_task_type(task_type)
        handler = self.handler_registry.get_handler(self, pack_name, state)
        if handler:
            return handler
        if pack_name != self.default_handler_pack:
            return self.handler_registry.get_handler(self, self.default_handler_pack, state)
        return None

    def _get_pipeline_states_for_task(self, task_type: str) -> List[WorkflowState]:
        """Resolve task-specific pipeline, falling back to workflow.pipeline."""
        pipeline_cfg = self.task_pipelines.get(task_type)
        if pipeline_cfg is None:
            return self.pipeline_states
        parsed = self._parse_pipeline_cfg(pipeline_cfg, f"workflow.task_pipelines.{task_type}")
        if not parsed:
            return self.pipeline_states
        return parsed

    def _uses_pipeline(self, session: WorkflowSession) -> bool:
        return bool(self._get_pipeline_states_for_task(session.task_type))

    def _load_pipeline(self, workflow_config: Dict[str, Any]) -> List[WorkflowState]:
        """
        Parse workflow.pipeline from config into a list of WorkflowState.

        Returns empty list if not provided, which falls back to the default
        state machine.
        """
        return self._parse_pipeline_cfg(workflow_config.get("pipeline"), "workflow.pipeline")

    def _parse_pipeline_cfg(self, pipeline_cfg: Any, cfg_name: str) -> List[WorkflowState]:
        if not pipeline_cfg:
            return []
        if not isinstance(pipeline_cfg, list):
            logger.warning("%s must be a list of state names; ignoring invalid value", cfg_name)
            return []

        parsed_states: List[WorkflowState] = []
        for state_name in pipeline_cfg:
            try:
                parsed_states.append(WorkflowState(state_name))
            except ValueError:
                logger.warning("Unknown workflow state in %s: %s", cfg_name, state_name)

        if parsed_states:
            logger.info("Using pipeline from %s: %s", cfg_name, [s.value for s in parsed_states])
        return parsed_states

    async def _advance_pipeline(self, session: WorkflowSession) -> None:
        """
        Move session to next state defined by custom pipeline. If already at the
        end of the pipeline, mark the session completed. No-op when not in
        pipeline mode.
        """
        pipeline_states = self._get_pipeline_states_for_task(session.task_type)
        if not pipeline_states:
            return

        if session.state in [WorkflowState.ERROR, WorkflowState.COMPLETED]:
            return

        if session.context.get("pipeline_manual_override"):
            session.context.pop("pipeline_manual_override", None)
            logger.info(
                f"Pipeline advancement skipped for session {session.id} due to manual override."
            )
            return

        try:
            current_idx = pipeline_states.index(session.state)
        except ValueError:
            logger.warning(f"Current state {session.state} not in pipeline; stopping pipeline advancement")
            return

        if current_idx >= len(pipeline_states) - 1:
            await self._update_session_state(session, WorkflowState.COMPLETED)
            return

        await self._update_session_state(session, pipeline_states[current_idx + 1])

    async def handle_protocol_planning(self, session: WorkflowSession) -> None:
        """
        Execute Protocol Planning Agent.
        Phase 3: LLM generates a structured plan skeleton from DomainPack and EvidenceBundle.
        """
        logger.info(f"Protocol Planning Agent: Starting for session {session.id}")
        
        agent = self._get_agent("protocol_planning")
        if not agent:
            logger.warning("Agent type protocol_planning initialization failed")
            logger.error("Protocol Planning Agent not found in registry")
            await self._update_session_state(session, WorkflowState.ERROR)
            return

        # Build context for run_with_logging (similar to other phases)
        ctx = {
            "session_id": session.id,
            "payload": session.context.get("payload"),
            "domain_pack": session.context.get("domain_pack"),
            "evidence_bundle": session.context.get("evidence_bundle"),
            "anchor_methods_paths": session.context.get("anchor_methods_paths", {}) or {},
            "anchor_raw_paths": session.context.get("anchor_raw_paths", {}) or {},
            "anchors_manifest_path": session.context.get("anchors_manifest_path"),
            "anchor_ids": session.context.get("anchor_ids", []) or [],
            "primary_anchor_id": session.context.get("primary_anchor_id"),
            "structure_first_artifacts_dir": session.context.get("structure_first_artifacts_dir"),
        }
        
        try:
            # Use standard execution path
            result = await agent.run_with_logging(ctx, {})
            plan = result.get("protocol_plan")
            
            # [NEW] Extract primary_anchor_id from the plan dict if available
            primary_anchor_id = plan.get("primary_anchor_id")
            if primary_anchor_id:
                session.context["primary_anchor_id"] = primary_anchor_id
                logger.info(f"Captured primary_anchor_id from plan: {primary_anchor_id}")
            
            # Store in session context
            session.context["protocol_plan"] = plan
            
            # DEBUG: Save to var/results/AutoProtocol
            try:
                import os, json
                out_dir = os.path.join("var", "results", "AutoProtocol")
                os.makedirs(out_dir, exist_ok=True)
                with open(os.path.join(out_dir, f"protocol_plan_{session.id}.json"), "w", encoding="utf-8") as f:
                    json.dump(plan, f, indent=2, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"Failed to save protocol plan debug artifact: {e}")

            # Persist session state
            await self.memory_manager.store_session(session)
            
            logger.info(f"Protocol Planning Agent: Successfully generated plan for session {session.id}")
            
            if not self._uses_pipeline(session):
                # Manual transition if not using pipeline
                await self._update_session_state(session, WorkflowState.PROTOCOL_GROUNDED_WRITER)
            
        except Exception as e:
            logger.error(f"Error in Protocol Planning Agent: {e}")
            await self._update_session_state(session, WorkflowState.ERROR)

    async def handle_legacy_protocol_curator(self, session: WorkflowSession) -> None:
        """Resume a pre-retirement Curator session at the Writer without mutation."""
        logger.info(
            "Session %s uses retired protocol_curator state; continuing at protocol_grounded_writer.",
            session.id,
        )
        await self._update_session_state(session, WorkflowState.PROTOCOL_GROUNDED_WRITER)

    async def handle_protocol_grounded_writer(self, session: WorkflowSession) -> None:
        """
        Execute Protocol Grounded Writer Agent.
        Phase 4: Render the source-grounded hierarchical Plan as a final protocol.
        """
        logger.info(f"Protocol Grounded Writer Agent: Starting for session {session.id}")
        
        agent = self._get_agent("protocol_grounded_writer")
        if not agent:
            logger.warning("Agent type protocol_grounded_writer initialization failed")
            await self._update_session_state(session, WorkflowState.ERROR)
            return

        protocol_plan = session.context.get("protocol_plan")
        anchor_methods_paths = session.context.get("anchor_methods_paths", {})
        anchor_raw_paths = session.context.get("anchor_raw_paths", {})
        anchors_manifest_path = session.context.get("anchors_manifest_path")
        anchor_ids = session.context.get("anchor_ids", [])
        primary_anchor_id = session.context.get("primary_anchor_id")
        domain_pack = session.context.get("domain_pack")
        validation_feedback = session.context.get("protocol_validation_result")
        if validation_feedback is None:
            validation_feedback = session.context.get("critic_result")
        
        if not protocol_plan:
            logger.error("No protocol_plan found in session context")
            await self._update_session_state(session, WorkflowState.ERROR)
            return

        ctx = {
            "session_id": session.id,
            "protocol_plan": protocol_plan,
            "anchor_methods_paths": anchor_methods_paths,
            "anchor_raw_paths": anchor_raw_paths,
            "anchors_manifest_path": anchors_manifest_path,
            "anchor_ids": anchor_ids,
            "primary_anchor_id": primary_anchor_id,
            "domain_pack": domain_pack,
            "validation_feedback": validation_feedback,
        }
        
        try:
            result = await agent.run_with_logging(ctx, {})
            
            updated_plan = result.get("protocol_plan")
            final_markdown = result.get("final_markdown")
            grounding_input_empty = result.get("grounding_input_empty")
            writer_render_trace = result.get("writer_render_trace")
            
            if updated_plan:
                session.context["protocol_plan"] = updated_plan
            if final_markdown:
                session.context["final_protocol_markdown"] = final_markdown
            if grounding_input_empty is not None:
                session.context["grounding_input_empty"] = bool(grounding_input_empty)
            if writer_render_trace is not None:
                session.context["writer_render_trace"] = writer_render_trace
                
            # Persist session
            await self.memory_manager.store_session(session)
            
            logger.info(f"Protocol Grounded Writer Agent: Successfully generated protocol for session {session.id}")
            
            if not self._uses_pipeline(session):
                await self._update_session_state(session, WorkflowState.PROTOCOL_CRITIC)
            
        except Exception as e:
            logger.error(f"Error in Protocol Grounded Writer Agent: {e}")
            await self._update_session_state(session, WorkflowState.ERROR)

    async def handle_protocol_critic(self, session: WorkflowSession) -> None:
        """Run the deterministic protocol integrity gate.

        The historical workflow state remains ``protocol_critic`` for persisted
        session compatibility, but no Critic Agent or model call is involved.
        """
        logger.info("Protocol Integrity Validator: Starting for session %s", session.id)

        final_markdown = session.context.get("final_protocol_markdown")
        protocol_plan = session.context.get("protocol_plan")
        refinement_used = bool(session.context.get("grounded_refine_used"))
        grounding_input_empty = bool(session.context.get("grounding_input_empty"))
        
        try:
            result = ProtocolIntegrityValidator().validate(
                protocol_plan,
                final_markdown or "",
                grounding_input_empty=grounding_input_empty,
            )
            
            session.context["critic_result"] = result
            session.context["protocol_validation_result"] = result
            approved = bool(result.get("approved"))
            issue_codes = result.get("issue_codes") or []
            issues = result.get("issues") or []
            
            # Render and Export Artifacts (always, approved or not)
            if final_markdown:
                try:
                    from ..agents.Biologist.ProtocolGeneration.renderers.protocol_renderer import (
                        render_markdown,
                        export_pdf,
                    )
                    
                    # 1. Render/Beautify
                    rendered_md = render_markdown(
                        final_markdown,
                        metadata={
                            "session_id": session.id,
                            "task_query": session.task.description or "Unknown Task",
                            "validation_approved": approved,
                            "validation_issues": issues,
                            "validation_issue_codes": issue_codes,
                        },
                    )
                    
                    # 2. Save Artifacts
                    out_root = os.path.join("var", "results", "AutoProtocol")
                    out_rendered = os.path.join(out_root, "rendered")
                    os.makedirs(out_rendered, exist_ok=True)
                    
                    raw_path = os.path.join(out_root, f"protocol_{session.id}.md")
                    rendered_path = os.path.join(out_rendered, f"protocol_rendered_{session.id}.md")
                    pdf_path = os.path.join(out_rendered, f"protocol_{session.id}.pdf")
                    
                    with open(raw_path, "w", encoding="utf-8") as f:
                        f.write(final_markdown)
                        
                    with open(rendered_path, "w", encoding="utf-8") as f:
                        f.write(rendered_md)
                        
                    # 3. Export PDF
                    if os.getenv("PROTOCOL_EXPORT_PDF", "1") == "1":
                        export_pdf(rendered_path, pdf_path)
                        
                    logger.info(
                        f"Saved protocol artifacts: raw_md={raw_path}, "
                        f"rendered_md={rendered_path}, pdf={pdf_path}"
                    )
                    session.context["protocol_artifacts"] = {
                        "raw_md": raw_path,
                        "rendered_md": rendered_path,
                        "pdf": pdf_path,
                    }
                    
                except Exception as render_err:
                    logger.error(f"Failed to render/export protocol: {render_err}")

            await self.memory_manager.store_session(session)
            
            logger.info(
                "Protocol Integrity Validator: Completed session %s, approved=%s",
                session.id,
                approved,
            )
            
            if (
                (not approved)
                and (not refinement_used)
                and bool(result.get("retryable"))
            ):
                session.context["grounded_refine_used"] = True
                session.context["pipeline_manual_override"] = True
                await self._update_session_state(session, WorkflowState.PROTOCOL_GROUNDED_WRITER)
            elif not self._uses_pipeline(session):
                await self._update_session_state(session, WorkflowState.COMPLETED)
            
        except Exception as e:
            logger.error(f"Error in Protocol Integrity Validator: {e}")
            await self._update_session_state(session, WorkflowState.ERROR)

    async def handle_protocol_action(self, session: WorkflowSession) -> None:
        """Compile action proposals and persist them without dispatching hardware calls."""
        agent = self._get_agent("protocol_action")
        if agent is None:
            raise ValueError("protocol_action must be configured in agents")
        validation = session.context.get("protocol_validation_result") or session.context.get("critic_result")
        if validation is not None and not validation.get("approved"):
            raise ValueError("Protocol integrity validation failed; action compilation is blocked")
        result = await agent.run_with_logging(dict(session.context), {})
        session.context["action_plan"] = result["action_plan"]
        await self.memory_manager.store_session(session)
        if not self._uses_pipeline(session):
            await self._update_session_state(session, WorkflowState.COMPLETED)

    def _get_agent(self, agent_type: str) -> Optional[BaseAgent]:
        """
        Retrieve agent from registry by type.

        Looks up specialized agent instance for given type. Logs warning if not found.

        Args:
            agent_type (str): Agent type identifier (e.g., "generation", "reflection")

        Returns:
            Optional[BaseAgent]: Agent instance or None if not registered
        """
        if agent_type not in self.agent_registry:
            logger.warning(f"Agent type {agent_type} initialization failed")
            return None
        return self.agent_registry[agent_type]

    def set_debug_hook(self, fn: Optional[Callable[[str, Dict[str, Any], Dict[str, Any]], None]] = None) -> None:
        self._debug_hook = fn


    def _get_current_ideas(self, session: WorkflowSession, current_iter: int) -> List[Idea]:
        """
        Retrieve ideas for current iteration with optional filtering.

        Returns ideas from specified iteration. If top_ideas_evo is enabled and not first
        iteration, filters to only top-ranked ideas for focused evolution.

        Args:
            session (WorkflowSession): Session containing ideas
            current_iter (int): Iteration number to filter by

        Returns:
            List[Idea]: Filtered idea list for current iteration
        """
        ideas = [i for i in session.ideas if i.iteration == current_iter]

        if current_iter > 0 and self.top_ideas_evo and session.top_ideas:
            ideas = [i for i in ideas if i.id in session.top_ideas]

        return ideas

    def _extract_feedback_content(self, session: WorkflowSession, ideas: List[Idea]):
        """
        Extract and format feedback content based on feedback type.

        Retrieves most recent feedback and processes based on type (global applies to all
        ideas, local targets specific ideas). Filters idea list for local feedback.

        Args:
            session (WorkflowSession): Session with feedback history
            ideas (List[Idea]): Ideas to filter (modified in place for local feedback)

        Returns:
            str or dict: Feedback content (str for global, dict for local) or empty string
        """
        if not session.feedback_history:
            logger.info("No feedback needed in the initial iteration")
            return ""

        feedback = sorted(
            session.feedback_history,
            key=lambda x: x.get("iteration", 0),
            reverse=True
        )[-1]

        fb_type = feedback['text'].get("type", "global")
        if fb_type == "global":
            return feedback['text'].get("content", "")
        elif fb_type == "local":
            feedback_content = feedback['text'].get("content", [])
            feedback_id_list = [fb_i.get("id", "") for fb_i in feedback_content]
            # Filter ideas based on feedback IDs
            ideas[:] = [i for i in ideas if i.id in feedback_id_list]
            return feedback_content
        else:
            logger.warning("Invalid feedback type, using empty string")
            return ""



    async def _rank_graph_hypotheses(self, session: WorkflowSession) -> List[Dict[str, Any]]:
        """Rank graph-derived hypotheses using the graph_hypothesis_ranking agent."""
        candidates = session.graph_hypotheses
        max_graph = self.graph_max_candidates
        agent = self._get_agent("graph_hypothesis_ranking")
        if not agent:
            if isinstance(max_graph, int) and max_graph > 0:
                return candidates[:max_graph]
            return candidates
        try:
            res = await agent.execute(
                {
                    "goal": session.task.to_dict(),
                    "disease": session.task.description,
                    "graph_hypotheses": candidates,
                },
                {"rerank_top_n": self.graph_max_candidates},
            )
            ranked = res.get("ranked") or []

            # Merge back structural fields (target/path/disease) from original hypotheses
            orig_map: Dict[str, Dict[str, Any]] = {}
            for gh in candidates:
                drug_id = gh.get("drug_id")
                drug_label = gh.get("drug_label")
                for k in [drug_id, drug_label, drug_label.lower() if isinstance(drug_label, str) else None]:
                    if k:
                        orig_map[k] = gh

            merged: List[Dict[str, Any]] = []
            for r in ranked:
                key_candidates = [
                    r.get("drug_id"),
                    r.get("drug_name"),
                    r.get("drug_label"),
                    (r.get("drug_name") or "").lower() if r.get("drug_name") else None,
                    (r.get("drug_label") or "").lower() if r.get("drug_label") else None,
                ]
                base: Dict[str, Any] = {}
                for k in key_candidates:
                    if k and k in orig_map:
                        base = orig_map[k]
                        break
                merged_item = {**base, **r}
                # Ensure labels/ids propagate if missing from LLM response
                if not merged_item.get("drug_label") and base.get("drug_label"):
                    merged_item["drug_label"] = base["drug_label"]
                if not merged_item.get("drug_id") and base.get("drug_id"):
                    merged_item["drug_id"] = base["drug_id"]
                if not merged_item.get("target_label") and base.get("target_label"):
                    merged_item["target_label"] = base["target_label"]
                if not merged_item.get("target_id") and base.get("target_id"):
                    merged_item["target_id"] = base["target_id"]
                if not merged_item.get("path") and base.get("path"):
                    merged_item["path"] = base["path"]
                if not merged_item.get("disease") and base.get("disease"):
                    merged_item["disease"] = base["disease"]
                merged.append(merged_item)

            # If merged is empty or fewer than originals, fall back to originals to avoid loss
            if not merged or len(merged) < len(candidates):
                result = candidates
            else:
                result = merged
            if isinstance(max_graph, int) and max_graph > 0:
                return result[:max_graph]
            return result
        except Exception as exc:
            logger.warning("Graph hypothesis ranking failed: %s", exc)
            if isinstance(max_graph, int) and max_graph > 0:
                return candidates[:max_graph]
            return candidates

    def _attach_graph_hypotheses_as_ideas(
        self,
        graph_hypotheses: List[Dict[str, Any]],
        session: WorkflowSession,
        target_iteration: Optional[int] = None,
    ) -> int:
        """
        Convert one-hop graph hypotheses into Idea objects for downstream ranking.
        """
        if not graph_hypotheses:
            return 0

        current_iter = session.iterations_completed
        target_iter = target_iteration if target_iteration is not None else current_iter + 1
        added = 0
        for idx, gh in enumerate(graph_hypotheses):
            drug_label = gh.get("drug_label") or gh.get("drug_id") or "unknown_drug"
            target_label = gh.get("target_label") or gh.get("target_id") or "unknown_target"
            # Prefer explicit disease; else use stable task id, then description
            disease = gh.get("disease") or getattr(session.task, "domain", None) or session.task.id or session.task.description
            path = gh.get("path") or gh.get("graph_path") or f"{drug_label} -> {target_label}"
            source = gh.get("source") or ""
            note = gh.get("note") or ""
            if source in {"drug_protein_prediction", "mixed", "drug_target_prediction"}:
                text = f"{drug_label} may modulate {target_label} to impact {disease} (target→drug prediction)."
                rationale = note or f"Predicted candidate via target neighborhood: {path}"
            else:
                text = f"{drug_label} may modulate {target_label} to impact {disease} (KG one-hop)."
                rationale = f"Derived from KG one-hop path: {path}"
            idea = Idea(
                # Use the idea_* ID prefix consistently
                id=f"idea_{session.id}_{current_iter}_{idx}",
                drug_name=drug_label,
                text=text,
                rationale=rationale,
                iteration=target_iter,
                parent_id=disease,
                kg_detailed_ideas={"graph_one_hop": gh},
            )
            session.ideas.append(idea)
            added += 1
        return added
