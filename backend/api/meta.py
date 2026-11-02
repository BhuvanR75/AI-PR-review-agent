"""Transparency endpoints: what agents exist, what they may do, which prompts ran.

The console shows this so a reviewer can answer "who is asking me to approve this, and
what were they allowed to do" without reading the source. Given that specialists are
explicitly denied the ability to post, that boundary should be visible from outside.
"""

from __future__ import annotations

from fastapi import APIRouter

from backend.agents import AGENT_CONCERNS
from backend.database.pg import read_capabilities
from backend.models.enums import SPECIALIST_AGENTS
from backend.orchestrator.graph import graph_diagram
from backend.prompts.registry import get_prompt_registry
from backend.security.threat_model import as_dict as threat_model
from backend.tools.capability_scope import Capability, scope_for
from backend.tools.model_router import get_model_router
from backend.tools.tool_registry import get_tool_registry

router = APIRouter(prefix="/api/meta", tags=["meta"])


@router.get("/agents")
async def agents() -> dict:
    registry = get_prompt_registry()
    router_ = get_model_router()
    return {
        "specialists": [
            {
                "agent": agent.value,
                "concern": AGENT_CONCERNS.get(agent),
                "capabilities": scope_for(agent).describe(),
                "may_post_to_github": scope_for(agent).allows(Capability.POST_REVIEW),
                "model": router_.model_for(agent),
                "max_tokens": router_.max_tokens_for(agent),
                "prompt": {
                    "version": registry.get(agent).version,
                    "hash": registry.get(agent).short_hash,
                },
            }
            for agent in SPECIALIST_AGENTS
        ],
        "orchestration": {
            "engine": "langgraph",
            "topology": graph_diagram(),
        },
    }


@router.get("/threat-model")
async def threats() -> dict:
    """The threat model, served rather than filed.

    A security document nobody opens is not a control. Each entry names the module that
    implements it, so an operator can check the claim against the code in one hop.
    """
    return threat_model()


@router.get("/tools")
async def tools() -> dict:
    return {
        "tools": get_tool_registry().describe(),
        "note": "each tool declares the capability it needs; invocation is scope-checked",
    }


@router.get("/capabilities")
async def capabilities() -> dict:
    available = await read_capabilities()
    return {
        "storage_lanes": {
            "timescale_hypertables_and_rollups": bool(available.get("timescale", False)),
            "vectorscale_diskann": bool(available.get("vectorscale", False)),
            "portable_fallback": True,
        },
        "note": (
            "without TimescaleDB the time lane is a plain table read with plain SQL; "
            "without pgvectorscale the vector lane uses HNSW. Both are correct, just slower."
        ),
    }
