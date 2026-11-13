"""Protocol generation Agents and their business modules.

Load Agent classes only when requested so schemas and leaf modules can be
imported independently, including through historical compatibility paths.
"""

from importlib import import_module

_AGENT_MODULES = {
    "ProtocolDomainPackAgent": "protocol_domain_pack_agent",
    "ProtocolResearchAgent": "protocol_research_agent",
    "ProtocolPlanningAgent": "protocol_planning_agent",
    "ProtocolGroundedWriterAgent": "protocol_grounded_writer_agent",
    "ProtocolIntegrityValidator": "protocol_integrity_validator",
}
__all__ = list(_AGENT_MODULES)


def __getattr__(name):
    if name not in _AGENT_MODULES:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{_AGENT_MODULES[name]}", __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
