"""Request-id correlation.

Regression test: the middleware once reset the context variable before
emitting the access log, so every access line recorded `request_id: "-"` and
could not be joined to the handler logs it belonged to.
"""

from __future__ import annotations

import json
import logging

from app.logging_config import JsonFormatter


def test_access_log_carries_the_request_id(client, caplog):
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())

    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            records.append(JsonFormatter().format(record))

    app_logger = logging.getLogger("app.main")
    capture = Capture()
    app_logger.addHandler(capture)
    app_logger.setLevel(logging.INFO)
    try:
        client.get("/healthz", headers={"X-Request-ID": "corr-1234"})
    finally:
        app_logger.removeHandler(capture)

    access_lines = [json.loads(line) for line in records]
    access = [line for line in access_lines if line["message"] == "http_request"]
    assert access, "no access log emitted"
    assert access[0]["request_id"] == "corr-1234"
    assert access[0]["path"] == "/healthz"
    assert access[0]["status"] == 200


def test_error_response_carries_the_same_request_id(client):
    response = client.get("/no-such-route", headers={"X-Request-ID": "corr-err"})
    assert response.headers["X-Request-ID"] == "corr-err"
