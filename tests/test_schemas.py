"""The tool and answer schemas follow the provider's strict rules, and real arguments validate against them.
A tiny built-in checker covers only the keywords these schemas use (no extra dependency)."""

import json

import pytest

import agent
import tools
from scripted import S1_INVESTIGATION, find_args, s1_answer

JSON_TYPES = {"string": str, "integer": int, "array": list, "object": dict, "null": type(None)}


def schema_errors(schema, value, path="$"):
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        if not any(isinstance(value, JSON_TYPES[t]) and not (t == "integer" and isinstance(value, bool)) for t in types):
            return [f"{path}: {value!r} is not {types}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} not allowed"]
    errors = []
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [f"{path}: missing {k}" for k in schema.get("required", []) if k not in value]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: unexpected {k}" for k in value if k not in props]
        for key, item in value.items():
            if key in props:
                errors += schema_errors(props[key], item, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += schema_errors(schema["items"], item, f"{path}[{i}]")
    return errors


def strict_errors(schema, path="$"):
    """Strict mode: every object lists all properties as required and forbids extras; nullable enums include null."""
    errors = []
    types = schema.get("type") if isinstance(schema.get("type"), list) else [schema.get("type")]
    if "object" in types:
        if schema.get("additionalProperties") is not False:
            errors.append(f"{path}: additionalProperties must be false")
        if sorted(schema.get("required", [])) != sorted(schema.get("properties", {})):
            errors.append(f"{path}: every property must be required")
        for key, sub in schema.get("properties", {}).items():
            errors += strict_errors(sub, f"{path}.{key}")
    if "enum" in schema and "null" in types and None not in schema["enum"]:
        errors.append(f"{path}: nullable enum must include null")
    return errors


SCHEMAS = {s["name"]: s["parameters"] for s in tools.TOOL_SCHEMAS}
ANSWER = agent.ANSWER_FORMAT["format"]["schema"]


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_tool_schemas_are_strict(name):
    assert strict_errors(SCHEMAS[name]) == []


def test_answer_schema_is_strict_and_matches_python_fields():
    assert strict_errors(ANSWER) == []
    assert tuple(ANSWER["required"]) == agent.ANSWER_FIELDS
    assert ANSWER["properties"]["status"]["enum"] == list(agent.STATUSES)


def test_scripted_arguments_and_answers_match_the_real_schemas():
    for reply in S1_INVESTIGATION:
        for call in reply.tool_calls:
            assert schema_errors(SCHEMAS[call.name], json.loads(call.arguments)) == [], call.name
    assert schema_errors(ANSWER, json.loads(s1_answer().text)) == []


def test_lookup_by_shift_id_with_null_filters_is_valid(conn):
    args = find_args(shift_id="AND-1012-EVE", worker="Jordan")
    assert schema_errors(SCHEMAS["find_shift"], args) == []
    assert tools.run_tool(conn, "find_shift", json.dumps(args))["status"] == "resolved"


@pytest.mark.parametrize("args", [
    {k: v for k, v in find_args(date="2026-10-12").items() if k != "worker"},   # missing field
    {**find_args(date="2026-10-12"), "extra": 1},                               # unexpected field
    find_args(date="2026-10-12", hall="Mars"),                                   # not an allowed hall
])
def test_checker_rejects_bad_arguments(args):
    assert schema_errors(SCHEMAS["find_shift"], args) != []
