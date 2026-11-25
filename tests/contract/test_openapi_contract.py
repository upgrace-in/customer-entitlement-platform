"""Contract tests validating live API responses against openapi.yaml schemas."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import jsonschema
import yaml

SPEC_PATH = Path(__file__).resolve().parent.parent.parent / "openapi.yaml"


def _load_spec() -> dict[str, Any]:
    with open(SPEC_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _validate_against_component(instance: Any, schema_name: str) -> None:
    spec = _load_spec()
    schema = dict(spec["components"]["schemas"][schema_name])
    # Inject components so internal $refs resolve cleanly
    schema["components"] = spec["components"]
    jsonschema.validate(instance=instance, schema=schema)


def test_openapi_spec_is_valid_structure() -> None:
    spec = _load_spec()
    assert spec["openapi"] == "3.1.0"
    required_paths = {
        "/v1/customers/{id}",
        "/v1/cases",
        "/v1/cases/{id}/comments",
        "/v1/cases/{id}/escalate",
    }
    assert required_paths.issubset(set(spec["paths"].keys()))
    assert "caseEscalated" in spec["webhooks"]


def test_contract_get_customer_matches_openapi(test_client, make_token) -> None:
    token = make_token(["customers:read", "finance:read"])
    resp = test_client.get(
        "/v1/customers/ACC-007",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    _validate_against_component(resp.json(), "CustomerSummary")


def test_contract_list_cases_matches_openapi(test_client, make_token) -> None:
    token = make_token(["cases:read"])
    resp = test_client.get(
        "/v1/cases?limit=5",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    _validate_against_component(resp.json(), "CaseListResponse")


def test_contract_add_comment_matches_openapi(test_client, make_token) -> None:
    token = make_token(["cases:write"])
    resp = test_client.post(
        "/v1/cases/CS-10231/comments",
        json={"body": "Contract test comment"},
        headers={
            "Authorization": f"Bearer {token}",
            "Idempotency-Key": "contract-key-001",
        },
    )
    assert resp.status_code == 201
    _validate_against_component(resp.json(), "CommentCreatedResponse")


def test_contract_escalate_case_matches_openapi(test_client, make_token) -> None:
    read_token = make_token(["cases:read"])
    case_resp = test_client.get(
        "/v1/cases/CS-10232",
        headers={"Authorization": f"Bearer {read_token}"},
    )
    etag = case_resp.headers["ETag"]

    write_token = make_token(["cases:write"])
    resp = test_client.post(
        "/v1/cases/CS-10232/escalate",
        json={"reason": "Contract test escalation", "notify_customer": True},
        headers={
            "Authorization": f"Bearer {write_token}",
            "If-Match": etag,
        },
    )
    assert resp.status_code == 200
    _validate_against_component(resp.json(), "CaseResponse")


def test_contract_error_responses_match_problem_detail(test_client, make_token) -> None:
    # 401 Unauthorized
    r401 = test_client.get("/v1/cases")
    assert r401.status_code == 401
    assert r401.headers["content-type"].startswith("application/problem+json")
    _validate_against_component(r401.json(), "ProblemDetail")

    # 403 Forbidden
    bad_scope = make_token(["cases:read"])
    r403 = test_client.get(
        "/v1/customers/ACC-007",
        headers={"Authorization": f"Bearer {bad_scope}"},
    )
    assert r403.status_code == 403
    _validate_against_component(r403.json(), "ProblemDetail")

    # 404 Not Found
    good_scope = make_token(["cases:read"])
    r404 = test_client.get(
        "/v1/cases/CS-99999",
        headers={"Authorization": f"Bearer {good_scope}"},
    )
    assert r404.status_code == 404
    _validate_against_component(r404.json(), "ProblemDetail")
