"""The orchestration seam (ADR-001).

Three methods, and nothing in the application imports LangGraph directly. If
sustained concurrent workflows outgrow what a Redis-checkpointed in-process engine
can carry, a Temporal implementation of this same protocol is a new file and one line
in the bootstrap -- not a rewrite.

`core` depends on nothing else in the package. That is the dependency rule from
ADR-002, and it is why this module holds only a protocol and the data shapes it
needs, with the concrete engine living in `orchestrator/`.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from backend.models.review import ReviewOutcome, ReviewRequest


@runtime_checkable
class WorkflowEngine(Protocol):
    """What the rest of the system is allowed to assume about orchestration."""

    name: str

    async def setup(self) -> None:
        """Prepare the engine: checkpointer, pools, compiled graph."""
        ...

    async def teardown(self) -> None:
        """Release whatever setup acquired."""
        ...

    async def run(self, request: ReviewRequest, *, thread_id: str | None = None) -> ReviewOutcome:
        """Execute a full review. `thread_id` isolates a re-run from the previous
        attempt's checkpoint; the default thread is the review id."""
        ...

    async def resume(self, request: ReviewRequest) -> ReviewOutcome:
        """Continue a workflow from its last checkpoint, for a worker that died
        mid-review. Resuming must not re-do completed nodes."""
        ...

    async def get_state(self, thread_id: str) -> dict[str, Any] | None:
        """Read the checkpointed state, if any. Used by the trace/DIAGNOSTICS path."""
        ...
