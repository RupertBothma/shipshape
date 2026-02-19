from __future__ import annotations

import json
import logging
import signal
import threading
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from controller.src.__main__ import JSONFormatter, _parse_bool_env, main


class TestJSONFormatter:
    """Tests for the structured JSON log formatter."""

    def _make_record(
        self,
        msg: str = "test message",
        level: int = logging.INFO,
        exc_info: object = None,
    ) -> logging.LogRecord:
        record = logging.LogRecord(
            name="test.logger",
            level=level,
            pathname="test.py",
            lineno=1,
            msg=msg,
            args=(),
            exc_info=exc_info,  # type: ignore[arg-type]
        )
        return record

    def test_format_produces_valid_json(self) -> None:
        formatter = JSONFormatter()
        record = self._make_record()

        output = formatter.format(record)
        parsed = json.loads(output)

        assert parsed["msg"] == "test message"
        assert parsed["level"] == "INFO"
        assert parsed["logger"] == "test.logger"
        assert "ts" in parsed

    def test_format_includes_error_on_exception(self) -> None:
        formatter = JSONFormatter()
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            record = self._make_record(exc_info=sys.exc_info())

        output = formatter.format(record)
        parsed = json.loads(output)

        assert "error" in parsed
        assert "ValueError" in parsed["error"]
        assert "boom" in parsed["error"]

    def test_format_omits_error_when_no_exception(self) -> None:
        formatter = JSONFormatter()
        record = self._make_record()

        output = formatter.format(record)
        parsed = json.loads(output)

        assert "error" not in parsed

    def test_format_is_single_line(self) -> None:
        formatter = JSONFormatter()
        record = self._make_record(msg="line one\nline two")

        output = formatter.format(record)

        # The JSON itself should be a single line (no embedded newlines
        # outside of JSON string escaping).
        assert output.count("\n") == 0

    def test_format_redacts_sensitive_values(self) -> None:
        formatter = JSONFormatter()
        record = self._make_record(
            msg=(
                "token=abc123 password=hunter2 Authorization: Bearer abc.def.ghi "
                "url=/healthz?access_token=qwerty"
            )
        )

        output = formatter.format(record)
        parsed = json.loads(output)
        message = parsed["msg"]

        assert "[REDACTED]" in message
        assert "abc123" not in message
        assert "hunter2" not in message
        assert "abc.def.ghi" not in message
        assert "access_token=qwerty" not in message

    def test_format_redacts_sensitive_values_in_exception_text(self) -> None:
        formatter = JSONFormatter()
        try:
            raise ValueError("token=abc123")
        except ValueError:
            import sys

            record = self._make_record(exc_info=sys.exc_info())

        output = formatter.format(record)
        parsed = json.loads(output)

        assert "error" in parsed
        assert "[REDACTED]" in parsed["error"]
        assert "abc123" not in parsed["error"]


class TestParseBoolEnv:
    """Tests for the _parse_bool_env helper."""

    def test_returns_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEST_BOOL_VAR", raising=False)
        assert _parse_bool_env("TEST_BOOL_VAR", default=False) is False
        assert _parse_bool_env("TEST_BOOL_VAR", default=True) is True

    @pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "on"])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("TEST_BOOL_VAR", value)
        assert _parse_bool_env("TEST_BOOL_VAR") is True

    @pytest.mark.parametrize("value", ["false", "False", "0", "no", "off", ""])
    def test_falsy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv("TEST_BOOL_VAR", value)
        assert _parse_bool_env("TEST_BOOL_VAR") is False

    def test_whitespace_is_stripped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_BOOL_VAR", "  true  ")
        assert _parse_bool_env("TEST_BOOL_VAR") is True


class TestMainEntrypoint:
    """Integration-style tests for the main() function wiring."""

    def test_main_without_leader_election(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify main() wires components correctly when leader election is disabled."""
        monkeypatch.setenv("LEADER_ELECTION_ENABLED", "false")
        monkeypatch.setenv("LOG_LEVEL", "WARNING")

        mock_controller = MagicMock()
        mock_controller.ready = threading.Event()

        # run_forever should set the shutdown event to exit immediately
        def fake_run_forever(shutdown_event: threading.Event | None = None) -> None:
            if shutdown_event is not None:
                shutdown_event.set()

        mock_controller.run_forever.side_effect = fake_run_forever

        with (
            patch("controller.src.__main__.load_kube_configuration"),
            patch(
                "controller.src.__main__.build_clients",
                return_value=(SimpleNamespace(), SimpleNamespace()),
            ),
            patch(
                "controller.src.__main__.build_controller_from_env",
                return_value=mock_controller,
            ),
            patch("controller.src.__main__.start_health_server") as mock_health,
        ):
            mock_health.return_value = MagicMock()
            main()

        mock_controller.run_forever.assert_called_once()
        assert mock_health.call_args.kwargs["leader"] is None
        mock_health.return_value.shutdown.assert_called_once()

    def test_main_passes_leader_event_when_election_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEADER_ELECTION_ENABLED", "true")
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        monkeypatch.setenv("LEADER_ELECTION_LEASE_DURATION_SECONDS", "20")
        monkeypatch.setenv("LEADER_ELECTION_RENEW_DEADLINE_SECONDS", "12")
        monkeypatch.setenv("LEADER_ELECTION_RETRY_PERIOD_SECONDS", "3")

        mock_controller = MagicMock()
        mock_controller.ready = threading.Event()

        def fake_run_forever(shutdown_event: threading.Event | None = None) -> None:
            if shutdown_event is not None:
                shutdown_event.set()

        mock_controller.run_forever.side_effect = fake_run_forever

        mock_elector = MagicMock()

        def fake_elector_run(
            *,
            on_started_leading: Callable[[], None],
            on_stopped_leading: Callable[[], None],
            stop_event: threading.Event,
        ) -> None:
            on_started_leading()
            on_stopped_leading()
            stop_event.set()

        mock_elector.run.side_effect = fake_elector_run

        with (
            patch("controller.src.__main__.load_kube_configuration"),
            patch(
                "controller.src.__main__.build_clients",
                return_value=(SimpleNamespace(), SimpleNamespace()),
            ),
            patch(
                "controller.src.__main__.build_controller_from_env",
                return_value=mock_controller,
            ),
            patch("controller.src.__main__.start_health_server") as mock_health,
            patch("controller.src.leader.default_identity", return_value="controller-0"),
            patch(
                "controller.src.leader.LeaseLeaderElector",
                return_value=mock_elector,
            ) as mock_elector_cls,
            patch("kubernetes.client.CoordinationV1Api", return_value=SimpleNamespace()),
        ):
            mock_health.return_value = MagicMock()
            main()

        assert isinstance(mock_health.call_args.kwargs["leader"], threading.Event)
        assert mock_elector.run.call_count == 1
        assert mock_health.call_args.kwargs["port"] == 8080
        ctor_kwargs = mock_elector_cls.call_args.kwargs
        assert ctor_kwargs["lease_duration_seconds"] == 20
        assert ctor_kwargs["renew_deadline_seconds"] == 12
        assert ctor_kwargs["retry_period_seconds"] == 3
        mock_health.return_value.shutdown.assert_called_once()

    def test_main_registers_signal_handlers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Verify SIGTERM and SIGINT handlers are registered."""
        monkeypatch.setenv("LEADER_ELECTION_ENABLED", "false")

        mock_controller = MagicMock()
        mock_controller.ready = threading.Event()

        def fake_run_forever(shutdown_event: threading.Event | None = None) -> None:
            if shutdown_event is not None:
                shutdown_event.set()

        mock_controller.run_forever.side_effect = fake_run_forever

        registered_signals: list[int] = []
        original_signal = signal.signal

        def tracking_signal(signum: int, handler: object) -> object:
            registered_signals.append(signum)
            return original_signal(signum, signal.SIG_DFL)

        with (
            patch("controller.src.__main__.load_kube_configuration"),
            patch(
                "controller.src.__main__.build_clients",
                return_value=(SimpleNamespace(), SimpleNamespace()),
            ),
            patch(
                "controller.src.__main__.build_controller_from_env",
                return_value=mock_controller,
            ),
            patch("controller.src.__main__.start_health_server") as mock_health,
            patch("controller.src.__main__.signal.signal", side_effect=tracking_signal),
        ):
            mock_health.return_value = MagicMock()
            main()

        assert signal.SIGTERM in registered_signals
        assert signal.SIGINT in registered_signals

    def test_main_rejects_invalid_health_port(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LEADER_ELECTION_ENABLED", "false")
        monkeypatch.setenv("HEALTH_PORT", "70000")

        with (
            patch("controller.src.__main__.load_kube_configuration"),
            patch(
                "controller.src.__main__.build_clients",
                return_value=(SimpleNamespace(), SimpleNamespace()),
            ),
            patch(
                "controller.src.__main__.build_controller_from_env",
                return_value=SimpleNamespace(ready=threading.Event()),
            ),
            pytest.raises(ValueError, match="HEALTH_PORT must be <= 65535, got: 70000"),
        ):
            main()

    def test_main_rejects_invalid_leader_timing_relationship(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEADER_ELECTION_ENABLED", "true")
        monkeypatch.setenv("LEADER_ELECTION_LEASE_DURATION_SECONDS", "10")
        monkeypatch.setenv("LEADER_ELECTION_RENEW_DEADLINE_SECONDS", "10")
        monkeypatch.setenv("LEADER_ELECTION_RETRY_PERIOD_SECONDS", "2")

        with (
            patch("controller.src.__main__.load_kube_configuration"),
            patch(
                "controller.src.__main__.build_clients",
                return_value=(SimpleNamespace(), SimpleNamespace()),
            ),
            patch(
                "controller.src.__main__.build_controller_from_env",
                return_value=SimpleNamespace(ready=threading.Event()),
            ),
            patch("controller.src.__main__.start_health_server") as mock_health,
        ):
            mock_health.return_value = MagicMock()
            with pytest.raises(
                ValueError,
                match=(
                    "LEADER_ELECTION_RENEW_DEADLINE_SECONDS must be smaller than "
                    "LEADER_ELECTION_LEASE_DURATION_SECONDS"
                ),
            ):
                main()

    def test_main_leadership_handoff_does_not_set_global_shutdown(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEADER_ELECTION_ENABLED", "true")
        monkeypatch.setenv("LOG_LEVEL", "WARNING")

        mock_controller = MagicMock()
        mock_controller.ready = threading.Event()
        controller_started = threading.Event()

        def fake_run_forever(shutdown_event: threading.Event | None = None) -> None:
            assert shutdown_event is not None
            controller_started.set()
            shutdown_event.wait(timeout=1.0)

        mock_controller.run_forever.side_effect = fake_run_forever
        mock_elector = MagicMock()

        def fake_elector_run(
            *,
            on_started_leading: Callable[[], None],
            on_stopped_leading: Callable[[], None],
            stop_event: threading.Event,
        ) -> None:
            on_started_leading()
            assert controller_started.wait(timeout=1.0)
            assert not stop_event.is_set()
            on_stopped_leading()
            assert not stop_event.is_set()
            stop_event.set()

        mock_elector.run.side_effect = fake_elector_run

        with (
            patch("controller.src.__main__.load_kube_configuration"),
            patch(
                "controller.src.__main__.build_clients",
                return_value=(SimpleNamespace(), SimpleNamespace()),
            ),
            patch(
                "controller.src.__main__.build_controller_from_env",
                return_value=mock_controller,
            ),
            patch("controller.src.__main__.start_health_server") as mock_health,
            patch("controller.src.leader.default_identity", return_value="controller-0"),
            patch("controller.src.leader.LeaseLeaderElector", return_value=mock_elector),
            patch("kubernetes.client.CoordinationV1Api", return_value=SimpleNamespace()),
        ):
            mock_health.return_value = MagicMock()
            main()

        mock_controller.run_forever.assert_called_once()
        mock_health.return_value.shutdown.assert_called_once()

    def test_main_sets_global_shutdown_when_controller_exits_unexpectedly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEADER_ELECTION_ENABLED", "true")
        monkeypatch.setenv("LOG_LEVEL", "WARNING")

        mock_controller = MagicMock()
        mock_controller.ready = threading.Event()

        def fake_run_forever(shutdown_event: threading.Event | None = None) -> None:
            return None

        mock_controller.run_forever.side_effect = fake_run_forever
        mock_elector = MagicMock()

        def fake_elector_run(
            *,
            on_started_leading: Callable[[], None],
            on_stopped_leading: Callable[[], None],
            stop_event: threading.Event,
        ) -> None:
            on_started_leading()
            assert stop_event.wait(timeout=1.0)

        mock_elector.run.side_effect = fake_elector_run

        with (
            patch("controller.src.__main__.load_kube_configuration"),
            patch(
                "controller.src.__main__.build_clients",
                return_value=(SimpleNamespace(), SimpleNamespace()),
            ),
            patch(
                "controller.src.__main__.build_controller_from_env",
                return_value=mock_controller,
            ),
            patch("controller.src.__main__.start_health_server") as mock_health,
            patch("controller.src.leader.default_identity", return_value="controller-0"),
            patch("controller.src.leader.LeaseLeaderElector", return_value=mock_elector),
            patch("kubernetes.client.CoordinationV1Api", return_value=SimpleNamespace()),
        ):
            mock_health.return_value = MagicMock()
            main()

        mock_controller.run_forever.assert_called_once()
        mock_health.return_value.shutdown.assert_called_once()

    def test_main_prevents_new_leader_watch_when_previous_thread_is_stuck(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LEADER_ELECTION_ENABLED", "true")
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        monkeypatch.setenv("LEADER_ELECTION_CONTROLLER_STOP_TIMEOUT_SECONDS", "1")

        mock_controller = MagicMock()
        mock_controller.ready = threading.Event()
        thread_started = threading.Event()

        def fake_run_forever(shutdown_event: threading.Event | None = None) -> None:
            thread_started.set()
            # Simulate a stuck watch loop that ignores stop events.
            threading.Event().wait(timeout=2.0)

        mock_controller.run_forever.side_effect = fake_run_forever
        mock_elector = MagicMock()

        def fake_elector_run(
            *,
            on_started_leading: Callable[[], None],
            on_stopped_leading: Callable[[], None],
            stop_event: threading.Event,
        ) -> None:
            on_started_leading()
            assert thread_started.wait(timeout=1.0)
            on_stopped_leading()
            # If the previous thread is still alive, a second start should
            # trigger global shutdown instead of creating an overlap.
            on_started_leading()
            assert stop_event.wait(timeout=2.0)

        mock_elector.run.side_effect = fake_elector_run

        with (
            patch("controller.src.__main__.load_kube_configuration"),
            patch(
                "controller.src.__main__.build_clients",
                return_value=(SimpleNamespace(), SimpleNamespace()),
            ),
            patch(
                "controller.src.__main__.build_controller_from_env",
                return_value=mock_controller,
            ),
            patch("controller.src.__main__.start_health_server") as mock_health,
            patch("controller.src.leader.default_identity", return_value="controller-0"),
            patch("controller.src.leader.LeaseLeaderElector", return_value=mock_elector),
            patch("kubernetes.client.CoordinationV1Api", return_value=SimpleNamespace()),
        ):
            mock_health.return_value = MagicMock()
            main()

        mock_controller.run_forever.assert_called_once()
        assert mock_controller.request_stop.call_count >= 1
        mock_health.return_value.shutdown.assert_called_once()
