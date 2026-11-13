from biomars.mas.assets.protocol_generation.prompts.protocol_domain_pack_agent import SYSTEM_PROMPT_90
import json
import logging
import textwrap
from typing import Any, List, Dict

from . import domain_pack as domain_pack_support
from ...base_agent import BaseAgent
from .protocol_schemas import (
    ProtocolRequest,
    LoadedDoc,
    DomainPack,
    DomainPackLLMContent,
)
from ....tools.ProtocolGeneration import domain_doc_loader  # This will need to be verified/implemented


logger = logging.getLogger(__name__)


class ProtocolDomainPackAgent(BaseAgent):
    """LLM-based agent to build a DomainPack for AutoProtocol."""

    agent_type = "protocol_domain_pack"

    async def execute(self, context: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute the domain pack generation.
        
        Args:
            context: Dictionary containing:
                - request: The ProtocolRequest object
            params: Dictionary of execution parameters
            
        Returns:
            Dict containing:
                - domain_pack: The fully constructed DomainPack object
        """
        request = context.get("request")
        if not request:
            raise ValueError("ProtocolRequest is required in context")
            
        # 1. Load domain docs
        loaded_docs = await self._load_domain_docs(request.user_domain_docs)
        
        # 2. Call LLM to synthesize content
        llm_content = await self._call_llm_for_domain_pack(request, loaded_docs)

        # 3. Merge into final DomainPack
        domain_pack = DomainPack(
            task=request,
            lab_profile=llm_content.lab_profile,
            constraints=llm_content.constraints,
            safety_rules=llm_content.safety_rules,
            env_capabilities=llm_content.env_capabilities,
            domain_docs=loaded_docs,
        )
        
        return {"domain_pack": domain_pack}

    async def _load_domain_docs(self, paths: List[str]) -> List[LoadedDoc]:
        return await domain_pack_support.load_domain_docs(paths, loader=domain_doc_loader)

    async def _call_llm_for_domain_pack(
        self,
        request: ProtocolRequest,
        loaded_docs: List[LoadedDoc],
    ) -> DomainPackLLMContent:
        docs_snippets = [
            {
                "title": doc.title or doc.path,
                "summary": doc.summary,
                "content_head": doc.content[:4000],
            }
            for doc in loaded_docs
        ]

        # Use Pydantic schema for structured generation
        # This matches the schema passed to the model
        schema = {
            "type": "object",
            "properties": {
                "lab_profile": {"type": "string"},
                "constraints": {"type": "object"},
                "safety_rules": {"type": "object"},
                "env_capabilities": {"type": "object"}
            },
            "required": ["lab_profile", "constraints", "safety_rules", "env_capabilities"]
        }

        system_prompt = (
            SYSTEM_PROMPT_90
        )

        user_prompt = textwrap.dedent(f"""
        Protocol Request:
        - query: {request.query}
        - title: {request.title}
        - cell_line: {request.cell_line}
        - species: {request.species}
        - scope: {request.scope}

        Domain Docs (summaries and heads):
        {json.dumps(docs_snippets, ensure_ascii=False, indent=2)}

        Your task:
        1. Infer the lab profile: environment, culture conditions, equipment, and practical notes.
        2. Infer constraints: hard limits implied by scope and likely lab capabilities.
        3. Infer safety_rules: biosafety and waste management related to the task and scope.
        4. Infer env_capabilities: what equipment and conditions we can rely on.

        IMPORTANT:
        - If the information is unknown, explicitly write that it is unknown or not guaranteed.
        - Do NOT hallucinate specific brand names or catalog numbers unless they appear in the docs.
        - Be consistent with BSL level and species (e.g., mouse vs human).
        - Consider that the system will use this information to constrain downstream protocol planning.
        """)

        # Using BaseAgent._call_model for consistent error handling and retry logic
        # Passing schema allows for structured output if supported by the model backend
        last_error: Exception | None = None
        for attempt in range(2):
            prompt = user_prompt
            if attempt:
                prompt += (
                    "\n\nRETRY: The previous response did not satisfy the structured-output "
                    "contract. Return one JSON object whose top-level keys are exactly "
                    "lab_profile, constraints, safety_rules, and env_capabilities. "
                    "Do not return a scalar, array, markdown, or commentary."
                )

            response = await self._call_model(
                prompt=prompt,
                system_prompt=system_prompt,
                schema=schema,
            )

            try:
                return self._normalize_domain_pack_response(response)
            except (TypeError, ValueError) as exc:
                last_error = exc
                logger.warning(
                    "ProtocolDomainPackAgent received invalid structured output on "
                    "attempt %d/2: %s",
                    attempt + 1,
                    exc,
                )

        raise ValueError(
            "DomainPack model returned invalid structured output after 2 attempts: "
            f"{last_error}"
        )

    def _normalize_domain_pack_response(self, response: Any) -> DomainPackLLMContent:
        return domain_pack_support.normalize_domain_pack_response(
            response, coerce_object_field=self._coerce_object_field,
        )

    @staticmethod
    def _coerce_object_field(value: Any) -> Dict[str, Any]:
        return domain_pack_support.coerce_object_field(value)
