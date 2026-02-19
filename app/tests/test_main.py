from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from app.src.config import ConfigError
from app.src.main import JSONFormatter, create_app


def test_root_returns_message_from_configmap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGE", "hello from configmap")
    monkeypatch.delenv("ALLOW_MESSAGE_FALLBACK", raising=False)

    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    assert response.text == "hello from configmap"


def test_health_and_readiness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGE", "hello")

    client = TestClient(create_app())

    assert client.get("/healthz").text == "ok"
    assert client.get("/readyz").text == "ok source=configmap"


def test_fallback_message_for_local_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESSAGE", raising=False)
    monkeypatch.setenv("ALLOW_MESSAGE_FALLBACK", "true")

    client = TestClient(create_app())

    response = client.get("/")

    assert response.status_code == 200
    assert response.text == "hello from local dev"


def test_readyz_shows_fallback_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESSAGE", raising=False)
    monkeypatch.setenv("ALLOW_MESSAGE_FALLBACK", "true")
    client = TestClient(create_app())
    assert client.get("/readyz").text == "ok source=fallback"


def test_missing_message_without_fallback_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESSAGE", raising=False)
    monkeypatch.delenv("ALLOW_MESSAGE_FALLBACK", raising=False)

    with pytest.raises(ConfigError):
        create_app()


def test_metrics_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGE", "hello")

    client = TestClient(create_app())

    # Make a request first to generate some metrics
    client.get("/")

    response = client.get("/metrics")
    assert response.status_code == 200
    assert "http_requests_total" in response.text
    assert "http_request_duration_seconds" in response.text
    assert "http_in_flight_requests" in response.text
    assert "app_config_loaded_timestamp_seconds" in response.text
    assert "app_config_loaded_info{" in response.text
    assert 'source="configmap"' in response.text


def test_metrics_normalize_unknown_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGE", "hello")

    client = TestClient(create_app())

    client.get("/missing-one")
    client.get("/missing-two")
    response = client.get("/metrics")

    assert response.status_code == 200
    assert 'path="other"' in response.text
    assert "/missing-one" not in response.text
    assert "/missing-two" not in response.text


def test_config_info_metric_uses_fallback_source(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MESSAGE", raising=False)
    monkeypatch.setenv("ALLOW_MESSAGE_FALLBACK", "true")

    client = TestClient(create_app())
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "app_config_loaded_info{" in response.text
    assert 'source="fallback"' in response.text


def test_unhandled_exception_returns_standard_error_json_and_logs_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("MESSAGE", "hello")
    monkeypatch.setattr("app.src.main.configure_logging", lambda: None)
    app = create_app()

    @app.get("/boom")
    def boom() -> str:
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    with caplog.at_level(logging.ERROR):
        response = client.get("/boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": "internal_server_error",
        "detail": "An unexpected error occurred.",
    }

    exception_logs = [
        record
        for record in caplog.records
        if record.name == "app.src.main" and record.getMessage() == "Unhandled error for GET /boom"
    ]
    assert len(exception_logs) == 1


def test_json_formatter_redacts_sensitive_values() -> None:
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="app.test",
        level=logging.INFO,
        pathname="test_main.py",
        lineno=1,
        msg=(
            "token=abc123 password=hunter2 Authorization: Bearer abc.def.ghi "
            "url=/readyz?access_token=qwerty"
        ),
        args=(),
        exc_info=None,
    )

    parsed = json.loads(formatter.format(record))
    message = parsed["msg"]

    assert "[REDACTED]" in message
    assert "abc123" not in message
    assert "hunter2" not in message
    assert "abc.def.ghi" not in message
    assert "access_token=qwerty" not in message
