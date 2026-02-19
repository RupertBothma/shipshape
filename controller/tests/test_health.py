from __future__ import annotations

import threading
import time
import urllib.request

import pytest

from controller.src.health import start_health_server


def _get(url: str, timeout: float = 2) -> tuple[int, str]:
    """Helper to make a GET request and return (status_code, body)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


class TestHealthServerWithLeadership:
    """Tests for the health server's leadership-aware readiness probe."""

    def setup_method(self) -> None:
        self.ready = threading.Event()
        self.leader = threading.Event()
        self.server = start_health_server(
            ready=self.ready, port=0, leader=self.leader
        )
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

    def teardown_method(self) -> None:
        self.server.shutdown()

    def test_healthz_always_returns_200(self) -> None:
        status, body = _get(f"{self.base_url}/healthz")
        assert status == 200
        assert body == "ok"

    def test_readyz_returns_503_when_not_ready(self) -> None:
        status, body = _get(f"{self.base_url}/readyz")
        assert status == 503
        assert "ready=false" in body

    def test_readyz_returns_503_when_ready_but_not_leader(self) -> None:
        self.ready.set()
        status, body = _get(f"{self.base_url}/readyz")
        assert status == 503
        assert "leader=false" in body

    def test_readyz_returns_200_when_ready_and_leader(self) -> None:
        self.ready.set()
        self.leader.set()
        status, body = _get(f"{self.base_url}/readyz")
        assert status == 200
        assert "leader=true" in body

    def test_readyz_returns_503_after_leadership_lost(self) -> None:
        self.ready.set()
        self.leader.set()
        status, _ = _get(f"{self.base_url}/readyz")
        assert status == 200

        self.leader.clear()
        status, body = _get(f"{self.base_url}/readyz")
        assert status == 503
        assert "leader=false" in body

    def test_leadz_returns_503_when_not_leader(self) -> None:
        status, body = _get(f"{self.base_url}/leadz")
        assert status == 503
        assert body == "not leader"

    def test_leadz_returns_200_when_leader(self) -> None:
        self.leader.set()
        status, body = _get(f"{self.base_url}/leadz")
        assert status == 200
        assert body == "ok"

    def test_404_for_unknown_path(self) -> None:
        status, _ = _get(f"{self.base_url}/unknown")
        assert status == 404

    def test_healthz_stays_responsive_during_slow_metrics_scrape(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import prometheus_client

        self.ready.set()
        self.leader.set()

        original_generate_latest = prometheus_client.generate_latest
        metrics_started = threading.Event()

        def slow_generate_latest() -> bytes:
            metrics_started.set()
            time.sleep(1.2)
            return original_generate_latest()

        monkeypatch.setattr(prometheus_client, "generate_latest", slow_generate_latest)

        metrics_result: dict[str, object] = {}

        def _scrape_metrics() -> None:
            try:
                status, _ = _get(f"{self.base_url}/metrics", timeout=3)
                metrics_result["status"] = status
            except Exception as exc:
                metrics_result["error"] = exc

        metrics_thread = threading.Thread(target=_scrape_metrics)
        metrics_thread.start()

        assert metrics_started.wait(timeout=1)
        status, body = _get(f"{self.base_url}/healthz", timeout=1)

        metrics_thread.join(timeout=4)
        assert not metrics_thread.is_alive()
        assert "error" not in metrics_result
        assert metrics_result.get("status") == 200
        assert status == 200
        assert body == "ok"


class TestHealthServerWithoutLeaderElection:
    """When leader election is disabled, readiness should only depend on ready_event."""

    def setup_method(self) -> None:
        self.ready = threading.Event()
        # leader=None simulates leader election disabled
        self.server = start_health_server(ready=self.ready, port=0, leader=None)
        self.port = self.server.server_address[1]
        self.base_url = f"http://127.0.0.1:{self.port}"

    def teardown_method(self) -> None:
        self.server.shutdown()

    def test_readyz_returns_200_when_ready_without_leader_election(self) -> None:
        self.ready.set()
        status, body = _get(f"{self.base_url}/readyz")
        assert status == 200
        assert "leader=true" in body

    def test_readyz_returns_503_when_not_ready(self) -> None:
        status, _ = _get(f"{self.base_url}/readyz")
        assert status == 503
