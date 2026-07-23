from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Any

from langchain.agents import create_agent
from langchain_core.callbacks.base import BaseCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver

from .config import DEFAULT_MODEL
from .monitor import report_tokens


@dataclass(slots=True)
class AgentSpec:
    name: str
    description: str
    system_prompt: str


@dataclass(slots=True)
class TokenUsage:
    """Cumulative LLM token usage. One instance lives on each ``BaseAgent``
    for its whole process lifetime, so ``str(agent.token_usage)`` always
    reflects everything that agent has spent so far -- exactly what you
    want printed "at the end" of a run, regardless of which architecture
    or how many turns/tool calls happened along the way."""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    llm_calls: int = 0

    def add(self, input_tokens: int, output_tokens: int, total_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens += total_tokens
        self.llm_calls += 1

    def __str__(self) -> str:
        return (
            f"{self.llm_calls} LLM call(s) -> "
            f"{self.input_tokens} input + {self.output_tokens} output = {self.total_tokens} total tokens"
        )


class _TokenUsageCallback(BaseCallbackHandler):
    """Reads ``usage_metadata`` off every chat model response as it comes
    back. This fires exactly once per actual LLM call (including the
    intermediate calls an agentic tool-calling loop makes within a single
    ``invoke()``), so usage is counted correctly no matter how many tool
    round-trips a request needed -- unlike summing over the checkpointer's
    message history, which would double-count on every later call."""

    def __init__(self, usage: TokenUsage):
        self._usage = usage
        self._lock = threading.Lock()

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        for generations in getattr(response, "generations", []) or []:
            for generation in generations:
                message = getattr(generation, "message", None)
                usage = getattr(message, "usage_metadata", None) if message is not None else None
                if not usage:
                    continue
                with self._lock:
                    self._usage.add(
                        int(usage.get("input_tokens", 0) or 0),
                        int(usage.get("output_tokens", 0) or 0),
                        int(usage.get("total_tokens", 0) or 0),
                    )


class BaseAgent:
    def __init__(self, spec: AgentSpec, model: str | None = None, *, architecture: str = "unknown"):
        self.spec = spec
        self.model = model or DEFAULT_MODEL
        self.architecture = architecture
        self._checkpointer = InMemorySaver()
        self.token_usage = TokenUsage()
        self.last_call_usage = TokenUsage()
        self._token_callback = _TokenUsageCallback(self.token_usage)

    @cached_property
    def agent(self):
        return create_agent(
            model=self.model,
            tools=self._retrieve_tools(),
            system_prompt=self.spec.system_prompt,
            name=self.spec.name,
            checkpointer=self._checkpointer,
        )

    def _retrieve_tools(self) -> list:
        return []

    def invoke(self, message: str, thread_id: str = "default") -> str:
        config = {
            "configurable": {"thread_id": self.thread_key(thread_id)},
            "callbacks": [self._token_callback],
        }

        async def _run() -> dict[str, Any]:
            return await self.agent.ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config,
            )

        before = replace(self.token_usage)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            result = asyncio.run(_run())
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(lambda: asyncio.run(_run())).result()

        self.last_call_usage = TokenUsage(
            input_tokens=self.token_usage.input_tokens - before.input_tokens,
            output_tokens=self.token_usage.output_tokens - before.output_tokens,
            total_tokens=self.token_usage.total_tokens - before.total_tokens,
            llm_calls=self.token_usage.llm_calls - before.llm_calls,
        )
        report_tokens(
            agent=self.spec.name,
            architecture=self.architecture,
            input_tokens=self.token_usage.input_tokens,
            output_tokens=self.token_usage.output_tokens,
            total_tokens=self.token_usage.total_tokens,
            llm_calls=self.token_usage.llm_calls,
        )
        return self._extract_text(result)

    def token_usage_line(self, *, cumulative: bool = True) -> str:
        usage = self.token_usage if cumulative else self.last_call_usage
        label = "total" if cumulative else "this turn"
        return f"[{self.spec.name}] tokens ({label}): {usage}"

    def print_token_usage(self) -> None:
        print(f"=== {self.token_usage_line()} ===")

    def run_persistent_chat(
        self,
        thread_id: str = "default",
        *,
        prompt: str = "You: ",
        exit_commands: frozenset[str] | None = None,
    ) -> None:
        exits = exit_commands or frozenset({"quit", "exit", "q", "/quit", "/exit"})
        lowered = {e.lower() for e in exits}
        print(
            "Persistent chat on thread %r. Commands: %s (or Ctrl+D / Ctrl+C)."
            % (thread_id, ", ".join(sorted(exits)))
        )
        print("Token usage and message counts are reported to the usage monitor, not printed here.")
        while True:
            try:
                line = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            if line.lower() in lowered:
                break
            reply = self.invoke(line, thread_id=thread_id)
            print(reply)

    def thread_key(self, thread_id: str) -> str:
        return f"{self.spec.name}:{thread_id}"

    def _extract_text(self, result: dict[str, Any]) -> str:
        messages = result.get("messages", [])
        if not messages:
            return ""
        last_message = messages[-1]
        return (
            last_message.content
            if isinstance(last_message.content, str)
            else str(last_message.content)
            if isinstance(last_message.content, list)
            else str(last_message.content)
        )
