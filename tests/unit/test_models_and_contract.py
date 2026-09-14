"""Model mapping, structured logging, and validation of openapi.yaml."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest
import yaml

from app.logging_config import JsonFormatter, request_id_var
from app.models import Comment, Issue, IssueUpdate, _label_names

SPEC_PATH = Path(__file__).resolve().parents[2] / "openapi.yaml"


# --------------------------------------------------------------------------
# Model mapping
# --------------------------------------------------------------------------
def test_labels_flattened_from_objects():
    assert _label_names([{"name": "bug"}, {"name": "ui"}]) == ["bug", "ui"]


def test_labels_accept_bare_strings():
    assert _label_names(["bug", "ui"]) == ["bug", "ui"]


@pytest.mark.parametrize("value", [None, [], [{}], [{"color": "red"}]])
def test_labels_degrade_to_empty_list(value):
    assert _label_names(value) == []


def test_issue_maps_minimal_github_payload():
    """GitHub omits fields; the mapper must not KeyError."""
    issue = Issue.from_github({"number": 7})
    assert issue.number == 7
    assert issue.state == "open"
    assert issue.labels == []
    assert issue.body is None


def test_issue_maps_missing_user_object():
    assert Issue.from_github({"number": 7, "user": None}).user is None


def test_comment_maps_minimal_payload():
    comment = Comment.from_github({"id": 1})
    assert comment.id == 1 and comment.user is None


def test_issue_update_rejects_empty_patch():
    with pytest.raises(ValueError):
        IssueUpdate()


@pytest.mark.parametrize(
    "kwargs", [{"title": "t"}, {"body": "b"}, {"state": "closed"}, {"title": "t", "state": "open"}]
)
def test_issue_update_accepts_any_single_field(kwargs):
    assert IssueUpdate(**kwargs)


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------
def _record(**extra) -> logging.LogRecord:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "an_event", (), None)
    record.__dict__.update(extra)
    return record


def test_log_output_is_valid_json_with_request_id():
    token = request_id_var.set("req-42")
    try:
        payload = json.loads(JsonFormatter().format(_record()))
    finally:
        request_id_var.reset(token)

    assert payload["message"] == "an_event"
    assert payload["request_id"] == "req-42"
    assert payload["level"] == "INFO"
    assert "ts" in payload


def test_log_includes_structured_extras():
    payload = json.loads(JsonFormatter().format(_record(issue_number=42, delivery_id="d-1")))
    assert payload["issue_number"] == 42
    assert payload["delivery_id"] == "d-1"


def test_log_serialises_exceptions():
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        import sys

        record = _record()
        record.exc_info = sys.exc_info()
        payload = json.loads(JsonFormatter().format(record))
    assert "RuntimeError" in payload["exception"]


# --------------------------------------------------------------------------
# The contract document
# --------------------------------------------------------------------------
@pytest.fixture(scope="module")
def spec() -> dict:
    return yaml.safe_load(SPEC_PATH.read_text())


def test_spec_is_openapi_31(spec):
    assert spec["openapi"].startswith("3.1")


def test_spec_validates_against_the_openapi_metaschema(spec):
    from openapi_spec_validator import validate

    validate(spec)


def test_spec_covers_every_required_route(spec):
    required = {
        "/issues": {"get", "post"},
        "/issues/{number}": {"get", "patch", "delete"},
        "/issues/{number}/comments": {"get", "post"},
        "/webhook": {"post"},
        "/events": {"get"},
        "/healthz": {"get"},
    }
    for path, methods in required.items():
        assert path in spec["paths"], f"missing path {path}"
        assert methods <= set(spec["paths"][path]), f"missing methods on {path}"


def test_spec_defines_the_reusable_schemas(spec):
    schemas = spec["components"]["schemas"]
    for name in ("Issue", "Comment", "Error", "IssueCreate", "IssueUpdate",
                 "CommentCreate", "WebhookEvent", "ErrorDetail"):
        assert name in schemas


def test_spec_declares_a_bearer_security_scheme(spec):
    scheme = spec["components"]["securitySchemes"]["githubToken"]
    assert scheme["type"] == "http" and scheme["scheme"] == "bearer"


def test_create_issue_documents_201_with_location(spec):
    created = spec["paths"]["/issues"]["post"]["responses"]["201"]
    assert "Location" in created["headers"]
    assert created["content"]["application/json"]["examples"]


def test_webhook_documents_204_401_and_400(spec):
    responses = spec["paths"]["/webhook"]["post"]["responses"]
    assert {"204", "400", "401"} <= set(responses)


def test_error_responses_carry_examples(spec):
    for name in ("BadRequest", "Unauthorized", "NotFound", "RateLimited"):
        content = spec["components"]["responses"][name]["content"]["application/json"]
        assert content["examples"], f"{name} has no example"


def test_rate_limited_response_documents_retry_after(spec):
    assert "Retry-After" in spec["components"]["responses"]["RateLimited"]["headers"]


def test_list_issues_documents_link_header(spec):
    assert "Link" in spec["paths"]["/issues"]["get"]["responses"]["200"]["headers"]


def test_every_operation_has_an_operation_id(spec):
    seen = set()
    for path, item in spec["paths"].items():
        for method, op in item.items():
            if method in ("get", "post", "patch", "put", "delete"):
                op_id = op.get("operationId")
                assert op_id, f"{method} {path} lacks operationId"
                assert op_id not in seen, f"duplicate operationId {op_id}"
                seen.add(op_id)
