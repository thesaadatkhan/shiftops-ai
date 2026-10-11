"""SCRIPTED provider for offline tests. These replies are pre-written, not real model output."""

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
    answer = {"status": "recommendation", "shift_id": None, "ask_first": None, "backup": None,
              "explanation": None, "question": None}
    answer.update(fields)
    return ModelReply(status="completed", text=json.dumps(answer), input_tokens=1500, output_tokens=150,
                      cached_tokens=0, cache_write_tokens=0)


# ----- the Andromeda Oct 12, 6 PM-11 PM scenario (Jordan calls out), as scripted model replies -----

S1 = "AND-1012-EVE"
S1_IDS = ["W05", "W09", "W14", "W21"]
S1_QUESTION = "Jordan called out for Andromeda's October 12 evening shift. Who should I ask first?"


def find_args(**fields):
    args = {"shift_id": None, "date": None, "hall": None, "shift_name": None, "start_time": None, "worker": None}
    args.update(fields)
    return args


S1_FIND = tools_reply(("find_shift", find_args(date="2026-10-12", hall="Andromeda", shift_name="Evening", worker="Jordan")))
S1_ELIGIBLE = tools_reply(("get_eligible_candidates", {"shift_id": S1}))
S1_CONTEXT = tools_reply(("get_preferences", {"shift_id": S1, "worker_ids": S1_IDS}),
                         ("get_coverage_history", {"shift_id": S1, "worker_ids": S1_IDS, "lookback_days": 14}),
                         ("get_nearby_classes", {"shift_id": S1, "worker_ids": S1_IDS, "window_minutes": 120}))
S1_INVESTIGATION = [S1_FIND, S1_ELIGIBLE, S1_CONTEXT]


def s1_answer(**fields):
    answer = {"shift_id": S1, "ask_first": "W14", "backup": "W21",
              "explanation": "Skyler has no recent emergency coverage and the most spare hours; Riley prefers "
                             "the shift but completed three emergency covers recently."}
    answer.update(fields)
    return final_reply(**answer)
