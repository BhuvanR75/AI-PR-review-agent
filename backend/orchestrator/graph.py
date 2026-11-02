"""The workflow graph.

    START -> prepare -> [Send x4] -> specialist x4 -> aggregate -> gate -> END

The join is the interesting part. `aggregate` has four incoming branches and LangGraph
runs it once, after every task in the fan-out super-step has finished -- so the barrier
is expressed in the graph rather than orchestrated by hand. That is also the
orchestration-deadlock defense: every specialist is bounded by a timeout inside
`BaseAgent.run`, so a stalled provider produces an incomplete result rather than a join
that never fires.
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, START, StateGraph

from backend.orchestrator.nodes import ReviewNodes
from backend.orchestrator.state import ReviewState

logger = logging.getLogger(__name__)

NODE_PREPARE = "prepare"
NODE_SPECIALIST = "specialist"
NODE_AGGREGATE = "aggregate"
NODE_GATE = "gate"


def build_graph(
    nodes: ReviewNodes,
    *,
    checkpointer: Any = None,
    debug: bool = False,
) -> Any:
    graph = StateGraph(ReviewState)

    graph.add_node(NODE_PREPARE, nodes.prepare)
    # The specialist node's input is the `Send` payload rather than the graph state, so
    # its signature is `dict -> dict`. LangGraph's node typing is invariant over the
    # state type and cannot express that, hence the ignore.
    graph.add_node(NODE_SPECIALIST, nodes.specialist)  # type: ignore[arg-type]
    graph.add_node(NODE_AGGREGATE, nodes.aggregate)
    graph.add_node(NODE_GATE, nodes.gate)

    graph.add_edge(START, NODE_PREPARE)
    graph.add_conditional_edges(NODE_PREPARE, nodes.dispatch, [NODE_SPECIALIST])
    graph.add_edge(NODE_SPECIALIST, NODE_AGGREGATE)
    graph.add_edge(NODE_AGGREGATE, NODE_GATE)
    graph.add_edge(NODE_GATE, END)

    return graph.compile(checkpointer=checkpointer, debug=debug)


def graph_diagram() -> str:
    """A text rendering of the topology, for the docs and for a quick sanity check."""
    return (
        "START -> prepare -> Send(specialist) x N -> aggregate -> gate -> END\n"
        "  prepare    : fetch PR, supersede older heads, retrieve context once\n"
        "  specialist : one grounded pass per concern, bounded by a timeout\n"
        "  aggregate  : merge, dedup, score (deterministic, no LLM)\n"
        "  gate       : critical escalation, confidence threshold, or publish"
    )
