"""LLM access, behind one narrow interface.

Two implementations, and the difference between them is only who answers:

  * `FakeLLMClient`  -- deterministic heuristics over the diff. Default, so the whole
                        spine runs, tests and demos with no key and no network.
  * `OpenAILLMClient` -- real structured output.

Cost is computed here, at the boundary, from the token counts the provider reports.
Attributing cost anywhere else means reconciling two numbers that must agree, which
is a bug generator.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from backend.models.enums import AgentType
from backend.models.findings import FindingsPayload
from backend.reliability.circuit_breaker import CircuitOpenError, get_breaker
from backend.reliability.retry import RetryPolicy, call_with_retry
from backend.settings import Settings, get_settings
from backend.tools.fake_llm import agent_for_marker, review_diff

logger = logging.getLogger(__name__)

ModelT = TypeVar("ModelT", bound=BaseModel)

#: The fake client finds the asking agent from this marker in the system prompt.
AGENT_MARKER = "AGENT:"


class LLMResponse(BaseModel):
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    finish_reason: str | None = None
    simulated: bool = False


def estimate_cost(tokens_in: int, tokens_out: int, settings: Settings | None = None) -> float:
    resolved = settings or get_settings()
    cost = (
        tokens_in / 1000.0 * resolved.cost_per_1k_input_tokens
        + tokens_out / 1000.0 * resolved.cost_per_1k_output_tokens
    )
    return round(cost, 6)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the first JSON object out of a model response, tolerating code fences
    and trailing prose. Raises ValueError when there is nothing to parse."""
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        payload = json.loads(cleaned)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        payload = json.loads(cleaned[start : end + 1])
        if isinstance(payload, dict):
            return payload
    raise ValueError(f"no JSON object found in model response: {text[:200]!r}")


class LLMClient(Protocol):
    name: str

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
    ) -> LLMResponse: ...

    async def complete_structured(
        self,
        schema: type[ModelT],
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
    ) -> tuple[ModelT, LLMResponse]: ...


class BaseLLMClient:
    """Shared structured-output handling: ask for JSON, parse it, validate it."""

    name = "base"

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
    ) -> LLMResponse:  # pragma: no cover - implemented by subclasses
        raise NotImplementedError

    async def complete_structured(
        self,
        schema: type[ModelT],
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
    ) -> tuple[ModelT, LLMResponse]:
        response = await self.complete(
            model=model, system=system, user=user, max_tokens=max_tokens, temperature=0.0
        )
        try:
            payload = extract_json(response.text)
            return schema.model_validate(payload), response
        except (ValueError, ValidationError) as exc:
            logger.warning("structured output rejected, treating pass as empty: %s", exc)
            return schema.model_validate(_empty_for(schema)), response


def _empty_for(schema: type[BaseModel]) -> dict[str, Any]:
    if schema is FindingsPayload:
        return {"findings": []}
    return {}


class FakeLLMClient(BaseLLMClient):
    """Deterministic reviewer. Every finding it returns is derived from the diff it
    was handed, with the correct new-file line numbers, so downstream anchoring and
    dedup are exercised for real."""

    name = "fake"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
    ) -> LLMResponse:
        agent = self._agent_from(system)
        diff = _extract_section(user, "DIFF:")
        findings = review_diff(diff, agent) if agent else []
        text = json.dumps({"findings": findings}, indent=2)
        return LLMResponse(
            text=text,
            model=f"{model}+fake",
            tokens_in=estimate_tokens(system) + estimate_tokens(user),
            tokens_out=estimate_tokens(text),
            cost_usd=estimate_cost(
                estimate_tokens(system) + estimate_tokens(user),
                estimate_tokens(text),
                self.settings,
            ),
            finish_reason="stop",
            simulated=True,
        )

    @staticmethod
    def _agent_from(system: str) -> AgentType | None:
        for line in system.splitlines():
            if line.strip().startswith(AGENT_MARKER):
                return agent_for_marker(line.split(AGENT_MARKER, 1)[1].strip())
        return None


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _extract_section(prompt: str, marker: str) -> str:
    start = prompt.find(marker)
    if start == -1:
        return prompt
    remainder = prompt[start + len(marker) :]
    for terminator in ("\nRETRIEVED_CONTEXT:", "\nINSTRUCTIONS:", "\nOUTPUT:"):
        index = remainder.find(terminator)
        if index != -1:
            remainder = remainder[:index]
    return remainder.strip()


class OpenAILLMClient(BaseLLMClient):
    """Chat completions with json_schema response format, behind a circuit breaker
    and bounded retries."""

    name = "openai"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._breaker = get_breaker(f"llm:{self.settings.openai_base_url}", failure_threshold=5)
        self._policy = RetryPolicy(
            attempts=self.settings.llm_max_retries,
            base_delay=0.5,
            max_delay=10.0,
            label="llm",
        )

    async def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
        temperature: float = 0.0,
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        settings = self.settings
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
        }
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "schema": schema.model_json_schema(),
                    "strict": False,
                },
            }

        async def _call() -> LLMResponse:
            async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
                response = await client.post(
                    f"{settings.openai_base_url.rstrip('/')}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.openai_api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                response.raise_for_status()
                payload = response.json()
            choice = payload["choices"][0]
            usage = payload.get("usage") or {}
            tokens_in = int(usage.get("prompt_tokens") or 0)
            tokens_out = int(usage.get("completion_tokens") or 0)
            return LLMResponse(
                text=choice["message"].get("content") or "",
                model=payload.get("model", model),
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=estimate_cost(tokens_in, tokens_out, settings),
                finish_reason=choice.get("finish_reason"),
            )

        try:
            return await self._breaker.call(lambda: call_with_retry(_call, policy=self._policy))
        except CircuitOpenError as exc:
            logger.error("llm circuit open, failing this pass fast: %s", exc)
            raise

    async def complete_structured(
        self,
        schema: type[ModelT],
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 2000,
    ) -> tuple[ModelT, LLMResponse]:
        response = await self.complete(
            model=model, system=system, user=user, max_tokens=max_tokens, schema=schema
        )
        try:
            payload = extract_json(response.text)
            return schema.model_validate(payload), response
        except (ValueError, ValidationError) as exc:
            logger.warning("structured output rejected, treating pass as empty: %s", exc)
            return schema.model_validate(_empty_for(schema)), response


def get_llm_client(settings: Settings | None = None) -> LLMClient:
    resolved = settings or get_settings()
    if resolved.llm_provider == "openai":
        return OpenAILLMClient(resolved)
    return FakeLLMClient(resolved)
