"""Registry for task-type-specific workflow phase handlers."""

from __future__ import annotations

from typing import Callable, Dict, Optional

from .data_type import WorkflowState


class HandlerRegistry:
    """Resolves state handlers from named packs for each task type."""

    def __init__(self) -> None:
        self._packs: Dict[str, Dict[WorkflowState, str]] = {}

    def register_pack(self, pack_name: str, mapping: Dict[WorkflowState, str]) -> None:
        self._packs[pack_name] = dict(mapping)

    def get_handler(
        self,
        orchestrator: object,
        pack_name: str,
        state: WorkflowState,
    ) -> Optional[Callable]:
        mapping = self._packs.get(pack_name)
        if not mapping:
            return None

        method_name = mapping.get(state)
        if not method_name:
            return None

        handler = getattr(orchestrator, method_name, None)
        return handler if callable(handler) else None
