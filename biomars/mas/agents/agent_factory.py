"""
Agent Factory for InternAgent

This module provides functionality for registering and creating specialized
agent instances based on configuration.
"""
import importlib
import hashlib
import json
import logging
from typing import Dict, Any, Type, Union

from ..models.model_factory import ModelFactory
from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class AgentFactory:
    """
    Factory for creating agent instances based on configuration.
    
    This factory maintains a registry of available agent types
    and creates properly configured instances as needed.
    """
    
    # Registry of agent types
    _agent_registry: Dict[str, Union[Type[BaseAgent], str]] = {'protocol_domain_pack': '.Biologist.ProtocolGeneration.protocol_domain_pack_agent:ProtocolDomainPackAgent', 'protocol_research': '.Biologist.ProtocolGeneration.protocol_research_agent:ProtocolResearchAgent', 'protocol_planning': '.Biologist.ProtocolGeneration.protocol_planning_agent:ProtocolPlanningAgent', 'protocol_grounded_writer': '.Biologist.ProtocolGeneration.protocol_grounded_writer_agent:ProtocolGroundedWriterAgent'}
    
    # Cache of created agent instances
    _agent_cache: Dict[str, BaseAgent] = {}
    
    @classmethod
    def register_agent_type(cls, agent_type: str, agent_class: Type[BaseAgent]) -> None:
        """
        Register a new agent type.
        
        Args:
            agent_type: Type identifier for the agent
            agent_class: Agent class to register
        """
        if agent_type in cls._agent_registry:
            logger.warning(f"Overriding existing agent type: {agent_type}")
            
        cls._agent_registry[agent_type] = agent_class
        logger.info(f"Registered agent type: {agent_type}")
    
    @classmethod
    def create_agent(cls, 
                  agent_type: str, 
                  config: Dict[str, Any],
                  model_factory: 'ModelFactory') -> BaseAgent:
        """
        Create an agent instance of the specified type.
        
        Args:
            agent_type: Type of agent to create
            config: Configuration for the agent
            model_factory: ModelFactory instance for creating models
            
        Returns:
            Configured agent instance
            
        Raises:
            ValueError: If the agent type is not registered
        """
        # Check if agent type is registered
        if agent_type not in cls._agent_registry:
            raise ValueError(f"Agent type not registered: {agent_type}")
        
        # Create a cache key
        cache_key = cls._create_cache_key(agent_type, config)
        
        # Check if we have a cached instance
        if cache_key in cls._agent_cache:
            logger.debug(f"Using cached agent instance for {agent_type}")
            return cls._agent_cache[cache_key]
        
        # Resolve class lazily to avoid import-time dependency coupling across workflows.
        agent_class = cls._resolve_agent_class(agent_type)
        
        # Create the model instance using model_provider from agent config
        model_provider = config.get("model_provider", "default")
        
        try:
            # Use the model factory to create a model for this agent
            domain = agent_class.__module__.partition(".Biologist.")[2].split(".")[0]
            model_config = config
            if domain in {"DrugRepurposing", "ExperimentOptimization"}:
                model_config = dict(config, _model_domain=domain)
            model = model_factory.create_model_for_agent(agent_type, model_config)
            
            # Create the agent instance
            agent = agent_class(model, config)
            
            # Cache the instance
            cls._agent_cache[cache_key] = agent
            
            logger.info(f"Created agent instance of type: {agent_type} with provider: {model_provider}")
            return agent
        except Exception as e:
            logger.error(f"Error creating agent {agent_type}: {e}")
            raise
    
    @classmethod
    def create_all_agents(cls, 
                       config: Dict[str, Any],
                       model_factory: 'ModelFactory') -> Dict[str, BaseAgent]:
        """
        Create all configured agent instances.
        
        Args:
            config: Configuration dictionary with agent configurations
            model_factory: ModelFactory instance for creating models
            
        Returns:
            Dictionary mapping agent types to agent instances
        """
        agents = {}
        agent_configs = config.get("agents", {})
        for agent_type, agent_config in agent_configs.items():
            if agent_type in cls._agent_registry:
                try:
                    # Create a merged config with agent-specific settings
                    merged_config = agent_config.copy()
                    
                    # Add reference to global config sections that might be needed
                    merged_config["_global_config"] = config
                    
                    agents[agent_type] = cls.create_agent(
                        agent_type=agent_type,
                        config=merged_config,
                        model_factory=model_factory
                    )
                except Exception as e:
                    logger.error(f"Error creating agent {agent_type}: {str(e)}")
        
        return agents
    
    @classmethod
    def get_available_agent_types(cls) -> Dict[str, str]:
        """
        Get available agent types.
        
        Returns:
            Dictionary mapping agent types to their class names
        """
        available: Dict[str, str] = {}
        for agent_type, entry in cls._agent_registry.items():
            if isinstance(entry, str):
                _, _, class_name = entry.partition(":")
                available[agent_type] = class_name or "UnknownAgent"
            else:
                available[agent_type] = entry.__name__
        return available
    
    @classmethod
    def clear_cache(cls) -> None:
        """Clear the agent cache."""
        cls._agent_cache.clear()
        logger.info("Agent cache cleared")
    
    @staticmethod
    def _create_cache_key(agent_type: str, config: Dict[str, Any]) -> str:
        """
        Create a cache key for an agent configuration.
        
        Args:
            agent_type: Type of agent
            config: Agent configuration
            
        Returns:
            Cache key string
        """
        # Include effective model and agent configuration. Provider-only caching
        # reused stale models/catalog paths across interfaces in the same process.
        fingerprint = hashlib.sha256(
            json.dumps(config, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
        return f"{agent_type}_{fingerprint}"

    @classmethod
    def _resolve_agent_class(cls, agent_type: str) -> Type[BaseAgent]:
        """
        Resolve an agent class from registry entry.

        Supports two entry forms:
        - class object
        - "<module_path>:<ClassName>" string for lazy import
        """
        entry = cls._agent_registry.get(agent_type)
        if entry is None:
            raise ValueError(f"Agent type not registered: {agent_type}")
        if isinstance(entry, type):
            return entry
        if not isinstance(entry, str):
            raise TypeError(f"Unsupported registry entry for {agent_type}: {type(entry).__name__}")

        module_path, sep, class_name = entry.partition(":")
        if not sep or not module_path or not class_name:
            raise ValueError(f"Invalid agent registry entry for {agent_type}: {entry}")
        module = importlib.import_module(module_path, package=__package__)
        agent_class = getattr(module, class_name)
        if not isinstance(agent_class, type):
            raise TypeError(f"Resolved object is not a class for {agent_type}: {class_name}")
        # Cache resolved class for subsequent use.
        cls._agent_registry[agent_type] = agent_class
        return agent_class
