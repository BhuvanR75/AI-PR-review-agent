"""Checkpoint serialization.

Graph state carries our Pydantic models, and LangGraph's msgpack serializer warns
that deserializing types it does not recognise "will be blocked in a future
version". The supported answer is an explicit allow-list, so we declare exactly the
types that may appear in a checkpoint -- rather than depending on the current
permissive default and discovering the change when a dependency bump breaks resume.

The serializer must match the saver. The Redis savers write through
`JsonPlusRedisSerializer`, whose JSON write path calls `_preprocess_redis_json` --
a method the base `JsonPlusSerializer` does not have. Assigning the base class to a
Redis saver therefore fails every checkpoint write, which is exactly the bug this
module exists to prevent: the graph runs, the specialists run, and the run dies
saving its state. So `attach_serializer` picks by saver: the Redis class for Redis
savers (it accepts the same allow-list), the base class elsewhere.

`AsyncRedisSaver` does not accept a `serde` argument, but `BaseCheckpointSaver`
stores one as a public attribute, so it is assigned after construction. That is the
same slot the constructor would set.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

#: (module, qualname) pairs permitted inside a checkpoint.
ALLOWED_CHECKPOINT_TYPES: list[tuple[str, ...]] = [
    ("backend.models.findings", "Finding"),
    ("backend.models.findings", "SpecialistResult"),
    ("backend.models.review", "ChangedFile"),
    ("backend.models.enums", "AgentType"),
    ("backend.models.enums", "Severity"),
    ("backend.models.enums", "Category"),
    ("backend.memory.context_retriever", "RetrievedChunk"),
]


def build_serializer(*, for_redis: bool = False) -> Any:
    """The allow-listed serializer. `for_redis=True` yields the Redis-native class,
    whose write path the Redis savers require."""
    if for_redis:
        try:
            from langgraph.checkpoint.redis.base import JsonPlusRedisSerializer
        except ImportError:  # pragma: no cover - redis extra absent
            return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_CHECKPOINT_TYPES)
        return JsonPlusRedisSerializer(allowed_msgpack_modules=ALLOWED_CHECKPOINT_TYPES)
    return JsonPlusSerializer(allowed_msgpack_modules=ALLOWED_CHECKPOINT_TYPES)


def attach_serializer(saver: Any) -> Any:
    """Give a checkpointer our allow-list, with the class its write path needs.
    Returns the saver so it can be used inline."""
    is_redis = type(saver).__module__.startswith("langgraph.checkpoint.redis")
    saver.serde = build_serializer(for_redis=is_redis)
    return saver
