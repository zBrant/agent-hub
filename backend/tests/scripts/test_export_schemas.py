"""Generated contracts stay pinned to their canonical Python sources."""

import json

from scripts import export_schemas


def test_committed_schemas_are_current() -> None:
    assert export_schemas.export(check=True)


def test_openapi_contains_the_phase_1_resource_contract() -> None:
    document = json.loads(export_schemas.OPENAPI_PATH.read_text(encoding="utf-8"))
    paths = document["paths"]
    assert "/api/sessions" in paths
    assert "/api/sessions/{session_id}/node" in paths
    assert "/api/sessions/{session_id}/runs" in paths
    assert "/api/sessions/{session_id}/diff" in paths


def test_agent_event_schema_is_the_discriminated_union() -> None:
    document = json.loads(export_schemas.AGENT_EVENT_PATH.read_text(encoding="utf-8"))
    assert len(document["oneOf"]) == 11
    assert document["discriminator"]["propertyName"] == "type"
