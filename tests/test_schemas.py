"""Offline checks that the tool and final-answer schemas are valid strict schemas, and that representative
arguments (including nulls for optional filters) actually validate against them (review P1).

No jsonschema dependency: schema_errors() implements only the keywords these schemas use
(type, enum, properties, required, additionalProperties, items)."""

import json

import pytest

import agent
import tools
from test_agent import INVESTIGATION, S1_CONTEXT

JSON_TYPES = {"string": str, "integer": int, "array": list, "object": dict, "null": type(None), "boolean": bool}


def schema_errors(schema, value, path="$"):
    types = schema.get("type")
    if types is not None:
        types = types if isinstance(types, list) else [types]
        ok = any(isinstance(value, JSON_TYPES[t]) and not (t == "integer" and isinstance(value, bool)) for t in types)
        if not ok:
            return [f"{path}: {value!r} is not {types}"]
    if "enum" in schema and value not in schema["enum"]:
        return [f"{path}: {value!r} not in enum {schema['enum']}"]
    errors = []
    if isinstance(value, dict):
        props = schema.get("properties", {})
        errors += [f"{path}: missing {key}" for key in schema.get("required", []) if key not in value]
        if schema.get("additionalProperties") is False:
            errors += [f"{path}: unexpected {key}" for key in value if key not in props]
        for key, item in value.items():
            if key in props:
                errors += schema_errors(props[key], item, f"{path}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errors += schema_errors(schema["items"], item, f"{path}[{i}]")
    return errors


def strict_structure_errors(schema, path="$"):
    """Strict function/structured-output rules: every object lists all properties as required and forbids
    extras, and any nullable enum includes null among its values."""
    errors = []
    types = schema.get("type")
    types = types if isinstance(types, list) else [types]
    if "object" in types:
        if schema.get("additionalProperties") is not False:
            errors.append(f"{path}: additionalProperties must be false")
        if sorted(schema.get("required", [])) != sorted(schema.get("properties", {})):
            errors.append(f"{path}: every property must be required")
        for key, sub in schema.get("properties", {}).items():
            errors += strict_structure_errors(sub, f"{path}.{key}")
    if "enum" in schema and "null" in types and None not in schema["enum"]:
        errors.append(f"{path}: nullable enum must include null")
    if "items" in schema:
        errors += strict_structure_errors(schema["items"], f"{path}[]")
    return errors


SCHEMAS = {s["name"]: s["parameters"] for s in tools.TOOL_SCHEMAS}
FINAL_SCHEMA = agent.FINAL_FORMAT["format"]["schema"]


@pytest.mark.parametrize("name", sorted(SCHEMAS))
def test_tool_schemas_follow_strict_rules(name):
    assert strict_structure_errors(SCHEMAS[name]) == []


def test_final_schema_follows_strict_rules():
    assert strict_structure_errors(FINAL_SCHEMA) == []


def test_nullable_enums_accept_null():
    props = SCHEMAS["find_shift"]["properties"]
    assert schema_errors(props["hall"], None) == []
    assert schema_errors(props["shift_name"], None) == []
    assert schema_errors(props["hall"], "Mars") != []


def test_lookup_by_exact_shift_id_with_null_filters_validates_and_works(conn):
    args = {"shift_id": "AND-1012-EVE", "date": None, "hall": None, "shift_name": None,
            "start_time": None, "worker": "Jordan"}
    assert schema_errors(SCHEMAS["find_shift"], args) == []
    assert tools.dispatch(conn, "find_shift", json.dumps(args))["resolution"] == "resolved"


def test_scripted_test_arguments_match_the_real_schemas():
    calls = [(c.name, json.loads(c.arguments)) for reply in INVESTIGATION for c in reply.tool_calls]
    calls += S1_CONTEXT
    for name, args in calls:
        assert schema_errors(SCHEMAS[name], args) == [], name


@pytest.mark.parametrize("args", [
    {"shift_id": None, "date": "2026-10-12", "hall": "Andromeda", "shift_name": "Evening", "start_time": None},  # missing worker
    {"shift_id": None, "date": "2026-10-12", "hall": "Andromeda", "shift_name": "Evening", "start_time": None,
     "worker": "Jordan", "extra": 1},
    {"shift_id": None, "date": "2026-10-12", "hall": "Mars", "shift_name": None, "start_time": None, "worker": None},
])
def test_schema_checker_rejects_bad_arguments(args):
    assert schema_errors(SCHEMAS["find_shift"], args) != []


def test_example_final_answers_match_final_schema():
    from test_agent import GOOD_FINAL
    from scripted import final_reply
    assert schema_errors(FINAL_SCHEMA, json.loads(final_reply(**GOOD_FINAL).text)) == []
    clarification = json.loads(final_reply(status="clarification", rationale="x", clarification_question="Which?").text)
    assert schema_errors(FINAL_SCHEMA, clarification) == []
