from __future__ import annotations

import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from functools import cached_property
from typing import Any

from langchain.agents import create_agent
from langchain_core.callbacks.base import BaseCallbackHandler
from langgraph.checkpoint.memory import InMemorySaver

from .config import DEFAULT_MODEL
from .monitor import report_tokens, report_trace
from .timing import format_elapsed, record_timing


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


def _content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text" and "text" in block:
                    parts.append(str(block["text"]))
                else:
                    parts.append(json.dumps(block, default=str))
            else:
                text = getattr(block, "text", None)
                parts.append(str(text) if text is not None else str(block))
        return " ".join(p for p in parts if p)
    return str(content)


def _format_tool_calls(tool_calls: Any) -> str:
    if not tool_calls:
        return ""
    bits: list[str] = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            name = tc.get("name") or tc.get("function", {}).get("name") or "?"
            args = tc.get("args")
            if args is None and isinstance(tc.get("function"), dict):
                args = tc["function"].get("arguments")
        else:
            name = getattr(tc, "name", None) or "?"
            args = getattr(tc, "args", None)
        if not isinstance(args, str):
            try:
                args = json.dumps(args, default=str)
            except Exception:
                args = str(args)
        bits.append(f"{name}({args})")
    return "; ".join(bits)


class _AgentTelemetryCallback(BaseCallbackHandler):
    """Token accounting plus live LLM/tool traces for the Agent Trace window."""

    def __init__(self, usage: TokenUsage, *, agent: str, architecture: str):
        self._usage = usage
        self._agent = agent
        self._architecture = architecture
        self._lock = threading.Lock()
        self.thread_id: str | None = None

    def _emit(self, kind: str, text: str = "", *, tool: str | None = None) -> None:
        try:
            report_trace(
                agent=self._agent,
                architecture=self._architecture,
                kind=kind,
                text=text,
                tool=tool,
                thread_id=self.thread_id,
            )
        except Exception:
            pass

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        for generations in getattr(response, "generations", []) or []:
            for generation in generations:
                message = getattr(generation, "message", None)
                if message is None:
                    continue
                usage = getattr(message, "usage_metadata", None)
                if usage:
                    with self._lock:
                        self._usage.add(
                            int(usage.get("input_tokens", 0) or 0),
                            int(usage.get("output_tokens", 0) or 0),
                            int(usage.get("total_tokens", 0) or 0),
                        )
                text = _content_to_text(getattr(message, "content", None))
                tool_bits = _format_tool_calls(getattr(message, "tool_calls", None))
                parts = [p for p in (text, f"tool_calls: {tool_bits}" if tool_bits else "") if p]
                if parts:
                    self._emit("llm", " | ".join(parts))

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        **kwargs: Any,
    ) -> None:
        name = (
            (serialized or {}).get("name")
            or kwargs.get("name")
            or "?"
        )
        inputs = kwargs.get("inputs", input_str)
        if not isinstance(inputs, str):
            try:
                inputs = json.dumps(inputs, default=str)
            except Exception:
                inputs = str(inputs)
        self._emit("tool_start", str(inputs), tool=str(name))

    def on_tool_end(self, output: Any, **kwargs: Any) -> None:
        name = kwargs.get("name") or "?"
        if hasattr(output, "content"):
            text = _content_to_text(getattr(output, "content", output))
        elif not isinstance(output, str):
            try:
                text = json.dumps(output, default=str)
            except Exception:
                text = str(output)
        else:
            text = output
        self._emit("tool_end", text, tool=str(name))


class BaseAgent:
    def __init__(self, spec: AgentSpec, model: str | None = None, *, architecture: str = "unknown"):
        self.spec = spec
        self.model = model or DEFAULT_MODEL
        self.architecture = architecture
        self._checkpointer = InMemorySaver()
        self.token_usage = TokenUsage()
        self.last_call_usage = TokenUsage()
        self._token_callback = _AgentTelemetryCallback(
            self.token_usage,
            agent=spec.name,
            architecture=architecture,
        )

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
        self._token_callback.thread_id = self.thread_key(thread_id)
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
        text = self._extract_text(result)
        report_trace(
            agent=self.spec.name,
            architecture=self.architecture,
            kind="turn_end",
            text=text,
            thread_id=self.thread_key(thread_id),
        )
        return text

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
        timing_label: str | None = None,
    ) -> None:
        """Interactive REPL. When ``timing_label`` is set, wall-clock from user
        message until the agent returns its final reply (system thinks done)
        is printed and appended to ``timings.log`` if an experiment session
        is active.
        """
        exits = exit_commands or frozenset({"quit", "exit", "q", "/quit", "/exit"})
        lowered = {e.lower() for e in exits}
        print(
            "Persistent chat on thread %r. Commands: %s (or Ctrl+D / Ctrl+C)."
            % (thread_id, ", ".join(sorted(exits)))
        )
        print(
            "Token usage → usage monitor; LLM/tool traces → agent trace window "
            "(not printed here)."
        )
        if timing_label:
            print(
                "Timer: wall-clock from each message until this agent finishes "
                "(final reply = system thinks done)."
            )
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
            t0 = time.perf_counter() if timing_label else None
            reply = self.invoke(line, thread_id=thread_id)
            print(reply)
            if timing_label and t0 is not None:
                elapsed = time.perf_counter() - t0
                print(
                    f"--- elapsed until done: {format_elapsed(elapsed)} "
                    f"({elapsed:.1f}s) ---"
                )
                record_timing(
                    timing_label,
                    elapsed,
                    extra=f"agent={self.spec.name}",
                )

    def thread_key(self, thread_id: str) -> str:
        return f"{self.spec.name}:{thread_id}"

    def _extract_text(self, result: dict[str, Any]) -> str:
        messages = result.get("messages", [])
        if not messages:
            return ""
        last_message = messages[-1]
        return _content_to_text(getattr(last_message, "content", last_message))
