"""SCRIPTED provider for deterministic tests. These replies are pre-written, not real model output."""

import copy
import json

from agent import ModelReply, ToolCall


class ScriptedProvider:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.counts = 0

    def count_input_tokens(self, request, timeout):
        self.counts += 1
        return len(json.dumps(request)) // 4  # rough stand-in for the provider's counter

    def create(self, request, max_output_tokens, timeout):
        self.requests.append(copy.deepcopy(request))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def tools_reply(*calls, input_tokens=800, output_tokens=60, cached_tokens=0, cache_write_tokens=0):
    return ModelReply(status="completed", input_tokens=input_tokens, output_tokens=output_tokens,
                      cached_tokens=cached_tokens, cache_write_tokens=cache_write_tokens,
                      tool_calls=[ToolCall(f"call_{name}_{i}", name, json.dumps(args))
                                  for i, (name, args) in enumerate(calls)])


def final_reply(**fields):
    final = {"status": "recommendation", "shift_id": None, "outgoing_worker_id": None, "recommended_worker_id": None,
             "backup_worker_id": None, "rationale": "", "tradeoff": "", "clarification_question": None,
             "evidence_refs": []}
    final.update(fields)
    return ModelReply(status="completed", text=json.dumps(final), input_tokens=1500, output_tokens=150,
                      cached_tokens=0, cache_write_tokens=0)
