"""Model adapter protocol for the Phase 9 bounded agent loop (increment 1).

`agent_service.py`'s loop talks to a `ModelAdapter`, never directly to the
`openai` package - so a test can swap in a `ScriptedModelAdapter` that
returns pre-written, deterministic responses with no network call and no
API key, while production uses `OpenAIModelAdapter` unchanged. This is the
one seam that makes the loop provider-independent.

`ModelTurn` is the one shape both adapters return: an optional list of
requested tool calls, and/or a final visible text message. A real model can
return both at once (a short message plus one or more tool calls) or either
alone; the loop handles all three cases identically regardless of which
adapter produced them.
"""

import json
from dataclasses import dataclass, field

from ai_config import AIConfigurationError, resolve_ai_config


@dataclass
class ToolCallRequest:
    """One tool call the model asked for. `arguments` is already parsed
    from JSON when possible; if the model's own JSON was malformed,
    `arguments` is `None` and `raw_arguments` holds the unparsed text so the
    loop can report a controlled "malformed tool arguments" result instead
    of crashing on `json.loads`."""

    id: str
    name: str
    arguments: dict | None
    raw_arguments: str = ""


@dataclass
class ModelTurn:
    """One reply from the model: zero or more tool calls, and/or a final
    visible text message. Never carries hidden reasoning - only what the
    model actually said and actually asked to call."""

    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    message: str | None = None


SYSTEM_PROMPT = (
    "You are ShiftOps AI's scheduling assistant. You investigate coverage "
    "questions and call-outs for a university housing operation using ONLY "
    "the provided tools - never invent an employee, shift, date, eligibility "
    "result, or ranking; every fact must come from a tool result. If a "
    "worker or shift name is ambiguous, ask the supervisor to clarify rather "
    "than guessing which one they meant. You may propose a replacement or a "
    "fill for an uncovered shift with propose_replacement, but you cannot "
    "approve or execute it yourself - a supervisor must do that separately. "
    "Keep your visible replies short and factual."
)


class OpenAIModelAdapter:
    """Talks to the real OpenAI API using the Chat Completions tool-calling
    contract (`client.chat.completions.create(..., tools=..., tool_choice="auto")`),
    the current, fully supported tool-calling interface in the official
    `openai` Python SDK.

    Configuration (`OPENAI_API_KEY`/`OPENAI_MODEL`) is resolved lazily, on
    the FIRST actual call, not at construction time or import time - so
    constructing this adapter never requires a key, and a test can import
    this module freely without one.
    """

    def __init__(self):
        self._client = None
        self._model = None

    def _ensure_client(self):
        if self._client is None:
            config = resolve_ai_config()  # raises AIConfigurationError if unset
            # Imported here, not at module level, so importing this module
            # (and therefore agent_service.py, and therefore the test
            # suite) never requires the `openai` package's own import-time
            # environment probing to succeed under test conditions.
            from openai import OpenAI

            self._client = OpenAI(api_key=config.api_key)
            self._model = config.model
        return self._client

    def complete(self, messages, tools):
        client = self._ensure_client()
        full_messages = [{"role": "system", "content": SYSTEM_PROMPT}, *messages]
        try:
            response = client.chat.completions.create(
                model=self._model,
                messages=full_messages,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as error:  # noqa: BLE001 - deliberately broad: any
            # provider-side failure (auth, rate limit, network, timeout) is
            # reported the same controlled way, never a raw traceback. The
            # raised message deliberately does NOT interpolate `error`
            # itself (Codex review: the original message embedded the raw
            # provider exception text verbatim, which an HTTP client would
            # then see in the 503 `detail` field - a provider error can
            # include request/response internals that have no business
            # leaving this process). `from error` still preserves the real
            # exception in this process's own traceback/logs.
            raise AIConfigurationError(
                "The AI provider request failed. This may be an authentication, "
                "network, rate-limit, or provider-side issue - check the server's "
                "OPENAI_API_KEY/OPENAI_MODEL configuration and server logs."
            ) from error

        choice = response.choices[0].message
        tool_calls = []
        for call in choice.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments)
            except (TypeError, ValueError):
                arguments = None
            tool_calls.append(
                ToolCallRequest(
                    id=call.id,
                    name=call.function.name,
                    arguments=arguments,
                    raw_arguments=call.function.arguments or "",
                )
            )
        return ModelTurn(tool_calls=tool_calls, message=choice.content)


class ScriptedModelAdapter:
    """A fake model for tests: returns pre-written `ModelTurn`s in sequence,
    one per call to `complete()`, ignoring its `messages`/`tools`
    arguments entirely. No network call, no API key, fully deterministic.

    Raises `StopIteration`-derived `AssertionError` if the loop asks for
    more turns than the script provides, so a test's step-limit
    expectations are explicit rather than silently reusing the last turn.
    """

    def __init__(self, turns):
        self._turns = list(turns)
        self._index = 0
        self.calls = []  # every (messages, tools) pair received, for assertions

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        if self._index >= len(self._turns):
            raise AssertionError(
                f"ScriptedModelAdapter exhausted after {self._index} turn(s); "
                "the test script needs another one."
            )
        turn = self._turns[self._index]
        self._index += 1
        return turn
