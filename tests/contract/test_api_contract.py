from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.src.main import create_app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MESSAGE", "hello from contract")
    monkeypatch.delenv("ALLOW_MESSAGE_FALLBACK", raising=False)
    return TestClient(create_app(), raise_server_exceptions=False)


def test_openapi_contains_documented_paths(client: TestClient) -> None:
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    assert "/" in paths
    assert "/healthz" in paths
    assert "/readyz" in paths
    assert "/metrics" in paths


def test_root_contract(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == "hello from contract"
    assert response.headers["content-type"].startswith("text/plain")


def test_healthz_contract(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.text == "ok"
    assert response.headers["content-type"].startswith("text/plain")


def test_readyz_contract(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 200
    assert response.text == "ok source=configmap"
    assert response.headers["content-type"].startswith("text/plain")


def test_metrics_contract(client: TestClient) -> None:
    client.get("/")
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "http_requests_total" in response.text


def test_not_found_contract(client: TestClient) -> None:
    response = client.get("/does-not-exist")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_internal_error_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MESSAGE", "hello")
    app = create_app()

    @app.get("/contract-boom")
    def contract_boom() -> str:
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    response = client.get("/contract-boom")

    assert response.status_code == 500
    assert response.json() == {
        "error": "internal_server_error",
        "detail": "An unexpected error occurred.",
    }
