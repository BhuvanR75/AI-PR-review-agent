"""Workflow context.

A node deep in the agent stack should not have to be handed the review id and the
parent span at every call site just so its events can be correlated. These
contextvars carry that identity, and the observability layer reads it when
emitting, so correlation is a property of the work rather than a parameter of it.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_current_review: ContextVar[str | None] = ContextVar("current_review", default=None)
_current_span: ContextVar[str | None] = ContextVar("current_span", default=None)
_current_agent: ContextVar[str | None] = ContextVar("current_agent", default=None)


@contextmanager
def bind_review(review_id: str) -> Iterator[None]:
    token = _current_review.set(review_id)
    try:
        yield
    finally:
        _current_review.reset(token)


@contextmanager
def bind_span(span_id: str, agent: str | None = None) -> Iterator[None]:
    span_token = _current_span.set(span_id)
    agent_token = _current_agent.set(agent) if agent is not None else None
    try:
        yield
    finally:
        _current_span.reset(span_token)
        if agent_token is not None:
            _current_agent.reset(agent_token)


def current_review_id() -> str | None:
    return _current_review.get()


def current_span_id() -> str | None:
    return _current_span.get()


def current_agent() -> str | None:
    return _current_agent.get()
