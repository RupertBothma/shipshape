from __future__ import annotations

import json
import logging
import os
import re
import signal
import threading

from controller.src.controller import build_controller_from_env, env_int
from controller.src.health import start_health_server
from controller.src.kube import build_clients, load_kube_configuration
from controller.src.metrics import METRICS

RUNTIME_VERSION = "0.2.2"
_REDACTION_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]+)"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)(\b(?:authorization|token|password|passwd|secret|api[_-]?key)\b\s*[:=]\s*)([^\s,;]+)"
        ),
        r"\1[REDACTED]",
    ),
    (
        re.compile(r"(?i)([?&](?:token|access_token|api_key|password)=)([^&\s]+)"),
        r"\1[REDACTED]",
    ),
)


def redact_sensitive_text(value: str) -> str:
    redacted = value
    for pattern, replacement in _REDACTION_RULES:
        redacted = pattern.sub(replacement, redacted)
    return redacted


class JSONFormatter(logging.Formatter):
    """Emit logs as single-line JSON objects for structured log aggregation."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact_sensitive_text(record.getMessage()),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["error"] = redact_sensitive_text(self.formatException(record.exc_info))
        return json.dumps(log_entry)


def _parse_bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    """Controller entrypoint: configure logging, start leader election, and run the watch loop."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_handler = logging.StreamHandler()
    log_handler.setFormatter(JSONFormatter())
    logging.root.addHandler(log_handler)
    logging.root.setLevel(getattr(logging, log_level, logging.INFO))
    METRICS.build_info.info(
        {
            "version": os.getenv("APP_VERSION", RUNTIME_VERSION),
            "revision": os.getenv("GIT_SHA", "unknown"),
        }
    )

    load_kube_configuration()
    core_api, apps_api = build_clients()

    controller = build_controller_from_env(core_api=core_api, apps_api=apps_api)

    leader_election_enabled = _parse_bool_env("LEADER_ELECTION_ENABLED", default=True)
    leader_ready = threading.Event() if leader_election_enabled else None
    health_port = env_int("HEALTH_PORT", 8080, minimum=1, maximum=65535)
    health_server = start_health_server(
        ready=controller.ready,
        port=health_port,
        leader=leader_ready,
    )

    shutdown_event = threading.Event()

    def _handle_signal(signum: int, frame: object) -> None:
        logging.getLogger(__name__).info("Received signal %d, shutting down", signum)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if leader_election_enabled:
        from kubernetes.client import CoordinationV1Api

        from controller.src.leader import LeaseLeaderElector, default_identity

        namespace = os.getenv("WATCH_NAMESPACE", "shipshape")
        lease_name = os.getenv("LEADER_ELECTION_LEASE_NAME", "helloworld-controller-leader")
        identity = os.getenv("LEADER_ELECTION_IDENTITY", default_identity())
        lease_duration_seconds = env_int("LEADER_ELECTION_LEASE_DURATION_SECONDS", 15, minimum=1)
        renew_deadline_seconds = env_int("LEADER_ELECTION_RENEW_DEADLINE_SECONDS", 10, minimum=1)
        retry_period_seconds = env_int("LEADER_ELECTION_RETRY_PERIOD_SECONDS", 2, minimum=1)

        if renew_deadline_seconds >= lease_duration_seconds:
            raise ValueError(
                "LEADER_ELECTION_RENEW_DEADLINE_SECONDS must be smaller than "
                "LEADER_ELECTION_LEASE_DURATION_SECONDS"
            )
        if retry_period_seconds >= renew_deadline_seconds:
            raise ValueError(
                "LEADER_ELECTION_RETRY_PERIOD_SECONDS must be smaller than "
                "LEADER_ELECTION_RENEW_DEADLINE_SECONDS"
            )

        elector = LeaseLeaderElector(
            coordination_api=CoordinationV1Api(),
            namespace=namespace,
            lease_name=lease_name,
            identity=identity,
            lease_duration_seconds=lease_duration_seconds,
            renew_deadline_seconds=renew_deadline_seconds,
            retry_period_seconds=retry_period_seconds,
        )

        controller_thread: threading.Thread | None = None
        controller_stop = threading.Event()
        controller_state_lock = threading.Lock()
        # Must exceed typical watch timeout so leadership handoff does not create
        # overlapping watch loops under transient API stalls.
        controller_stop_join_timeout_seconds = env_int(
            "LEADER_ELECTION_CONTROLLER_STOP_TIMEOUT_SECONDS",
            45,
            minimum=1,
        )

        def on_started_leading() -> None:
            nonlocal controller_thread, controller_stop
            with controller_state_lock:
                if shutdown_event.is_set():
                    return
                if controller_thread is not None and controller_thread.is_alive():
                    logging.getLogger(__name__).error(
                        "Refusing to start a new watch loop while previous "
                        "controller thread is still running"
                    )
                    shutdown_event.set()
                    return

                controller_stop = threading.Event()
                if leader_ready is not None:
                    leader_ready.set()

                def _run_controller() -> None:
                    unexpected_exit = False
                    try:
                        controller.run_forever(shutdown_event=controller_stop)
                        unexpected_exit = (
                            not controller_stop.is_set() and not shutdown_event.is_set()
                        )
                        if unexpected_exit:
                            logging.getLogger(__name__).error(
                                "Controller thread exited without a stop signal; "
                                "terminating process"
                            )
                    except Exception:
                        unexpected_exit = True
                        logging.getLogger(__name__).exception("Controller thread crashed")
                    finally:
                        if unexpected_exit:
                            shutdown_event.set()

                controller_thread = threading.Thread(target=_run_controller, daemon=True)
                controller_thread.start()

        def on_stopped_leading() -> None:
            nonlocal controller_thread
            with controller_state_lock:
                if leader_ready is not None:
                    leader_ready.clear()

                controller.request_stop()
                controller_stop.set()
                if controller_thread is None:
                    return

                controller_thread.join(timeout=controller_stop_join_timeout_seconds)
                if controller_thread.is_alive():
                    logging.getLogger(__name__).error(
                        "Controller thread did not stop within %ss during leadership "
                        "handoff; forcing process shutdown",
                        controller_stop_join_timeout_seconds,
                    )
                    shutdown_event.set()
                    return

                controller_thread = None

        elector.run(
            on_started_leading=on_started_leading,
            on_stopped_leading=on_stopped_leading,
            stop_event=shutdown_event,
        )
        on_stopped_leading()
    else:
        controller.run_forever(shutdown_event=shutdown_event)

    health_server.shutdown()
    logging.getLogger(__name__).info("Controller stopped")


if __name__ == "__main__":
    main()
