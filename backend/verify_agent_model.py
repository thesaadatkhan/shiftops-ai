"""Focused checks for provider request options. No network or database."""

from types import SimpleNamespace

from agent_model import OpenAIModelAdapter


passed = 0
failed = 0


def check(condition, description):
    global passed, failed
    if condition:
        passed += 1
        print(f"PASS  {description}")
    else:
        failed += 1
        print(f"FAIL  {description}")


class FakeCompletions:
    def __init__(self):
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        message = SimpleNamespace(content="Done.", tool_calls=[])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeCompletions())


def adapter_for(model):
    adapter = OpenAIModelAdapter()
    adapter._client = FakeClient()
    adapter._model = model
    return adapter


luna = adapter_for("gpt-5.6-luna")
luna.complete([{"role": "user", "content": "test"}], [])
luna_request = luna._client.chat.completions.requests[0]
check(
    luna_request.get("reasoning_effort") == "none",
    "GPT-5.6 Luna disables reasoning so Chat Completions accepts function tools",
)
check(luna_request["tool_choice"] == "auto", "the existing automatic tool-choice contract is preserved")

terra = adapter_for("gpt-5.6-terra")
terra.complete([{"role": "user", "content": "test"}], [])
check(
    terra._client.chat.completions.requests[0].get("reasoning_effort") == "none",
    "the GPT-5.6 compatibility rule applies consistently across the family",
)

legacy = adapter_for("gpt-4o-mini")
legacy.complete([{"role": "user", "content": "test"}], [])
check(
    "reasoning_effort" not in legacy._client.chat.completions.requests[0],
    "non-GPT-5.6 models keep their prior request shape",
)

print(f"\n{passed} passed, {failed} failed.")
raise SystemExit(1 if failed else 0)
