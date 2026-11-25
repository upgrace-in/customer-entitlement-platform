"""Unit tests for Pydantic v2 schemas and validation rules."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from src.models.schemas import (
    Case,
    CaseFilter,
    CreateCommentRequest,
    EscalateRequest,
    ProblemDetail,
)


def test_case_model_validates_enums() -> None:
    case = Case(
        id="CS-10231",
        account_id="ACC-007",
        priority="P1",
        status="open",
        opened_at=datetime.now(timezone.utc),
        subject="DB replication lag",
    )
    assert case.priority == "P1"

    with pytest.raises(ValidationError):
        Case(
            id="CS-10231",
            account_id="ACC-007",
            priority="P9",  # type: ignore[arg-type]
            status="open",
            opened_at=datetime.now(timezone.utc),
            subject="Invalid priority",
        )


def test_comment_body_rejects_blank_whitespace() -> None:
    req = CreateCommentRequest(body="  Investigating replication lag.  ")
    assert req.body == "Investigating replication lag."

    with pytest.raises(ValidationError):
        CreateCommentRequest(body="    \n\t  ")


def test_escalate_request_rejects_blank_reason() -> None:
    with pytest.raises(ValidationError):
        EscalateRequest(reason="   ")


def test_case_filter_normalizes_search_whitespace() -> None:
    flt = CaseFilter(search="  replication   lag  ")
    assert flt.search == "replication lag"


def test_problem_detail_rfc9457_shape() -> None:
    err = ProblemDetail(
        type="https://api.example.com/problems/validation-error",
        title="Validation Error",
        status=400,
        detail="Invalid field",
        instance="/v1/cases",
        errors=[{"field": "/priority", "message": "Invalid", "code": "invalid_enum"}],
    )
    dumped = err.model_dump()
    assert dumped["status"] == 400
    assert dumped["errors"][0]["field"] == "/priority"
