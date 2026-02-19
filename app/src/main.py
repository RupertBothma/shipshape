from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from prometheus_client import Counter, Gauge, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.src.config import load_config, parse_bool

APP_VERSION = "0.2.2"
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


def configure_logging() -> None:
    """Configure structured JSON logging with a level from ``LOG_LEVEL`` env var."""
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.root.handlers.clear()
    logging.root.addHandler(handler)
    logging.root.setLevel(getattr(logging, log_level, logging.INFO))

REQUEST_COUNT = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
)
REQUEST_IN_FLIGHT = Gauge(
    "http_in_flight_requests",
    "Current number of HTTP requests being processed",
)
CONFIG_LOADED_TIMESTAMP = Gauge(
    "app_config_loaded_timestamp_seconds",
    "Unix timestamp when application config was loaded at startup",
)
CONFIG_LOADED_INFO = Gauge(
    "app_config_loaded_info",
    "Startup app config metadata",
    ["source", "config_fingerprint", "app_version"],
)
KNOWN_METRIC_PATHS = {"/", "/healthz", "/readyz", "/metrics"}
_TRACING_INITIALIZED = False


class MetricsMiddleware(BaseHTTPMiddleware):
    """ASGI middleware that records per-request Prometheus counters and histograms.

    Skips the ``/metrics`` endpoint itself to avoid self-referential inflation.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path == "/metrics":
            return await call_next(request)
        REQUEST_IN_FLIGHT.inc()
        start = time.monotonic()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            duration = time.monotonic() - start
            metric_path = self._normalize_metric_path(request)
            REQUEST_COUNT.labels(method=request.method, path=metric_path, status=status_code).inc()
            REQUEST_DURATION.labels(method=request.method, path=metric_path).observe(duration)
            REQUEST_IN_FLIGHT.dec()
        return response

    @staticmethod
    def _normalize_metric_path(request: Request) -> str:
        route = request.scope.get("route")
        route_path = getattr(route, "path", None)
        if isinstance(route_path, str) and route_path in KNOWN_METRIC_PATHS:
            return route_path
        if request.url.path in KNOWN_METRIC_PATHS:
            return request.url.path
        return "other"


def _config_fingerprint(message: str) -> str:
    return hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]


def configure_tracing(app: FastAPI, logger: logging.Logger) -> None:
    """Enable OpenTelemetry tracing when ``OTEL_ENABLED=true``.

    The app stays fully functional when OpenTelemetry packages are absent;
    tracing is then skipped with a warning.
    """
    global _TRACING_INITIALIZED

    if not parse_bool(os.getenv("OTEL_ENABLED")):
        return

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import (
            FastAPIInstrumentor,
        )
        from opentelemetry.instrumentation.requests import (
            RequestsInstrumentor,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
        )
    except ImportError:
        logger.warning(
            "OTEL_ENABLED=true but OpenTelemetry packages are not installed; tracing disabled"
        )
        return

    if not _TRACING_INITIALIZED:
        endpoint_base = os.getenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "http://otel-collector.monitoring.svc:4318",
        )
        endpoint = os.getenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or (
            endpoint_base
            if endpoint_base.endswith("/v1/traces")
            else endpoint_base.rstrip("/") + "/v1/traces"
        )

        resource = Resource.create({
            "service.name": os.getenv("OTEL_SERVICE_NAME", "helloworld"),
            "service.namespace": os.getenv("OTEL_SERVICE_NAMESPACE", "shipshape"),
        })

        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)
        RequestsInstrumentor().instrument()
        _TRACING_INITIALIZED = True
        logger.info("OpenTelemetry tracing enabled (OTLP endpoint=%s)", endpoint)

    FastAPIInstrumentor.instrument_app(app)


def create_app() -> FastAPI:
    """Create and configure the helloworld FastAPI application.

    Reads the ``MESSAGE`` value from the environment (injected by a
    Kubernetes ConfigMap via ``envFrom``) and serves it on ``GET /``.

    Endpoints:
        ``GET /``        — Returns the ConfigMap message as plain text.
        ``GET /healthz`` — Liveness probe (always ``200 ok``).
        ``GET /readyz``  — Readiness probe; includes the config source
                           (``configmap`` or ``fallback``) for diagnostics.
        ``GET /metrics`` — Prometheus metrics in text exposition format.
    """
    configure_logging()
    config = load_config()
    logger = logging.getLogger(__name__)
    logger.info("Starting helloworld app (config source=%s)", config.source)

    app = FastAPI(title="helloworld", version=APP_VERSION)
    CONFIG_LOADED_TIMESTAMP.set_to_current_time()
    CONFIG_LOADED_INFO.clear()
    CONFIG_LOADED_INFO.labels(
        source=config.source,
        config_fingerprint=_config_fingerprint(config.message),
        app_version=app.version,
    ).set(1)
    app.state.message_source = config.source
    app.add_middleware(MetricsMiddleware)
    configure_tracing(app, logger)

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Return a standardized JSON error body for unhandled exceptions."""
        logger.exception("Unhandled error for %s %s", request.method, request.url.path)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "detail": "An unexpected error occurred."},
        )

    @app.get("/", response_class=PlainTextResponse)
    def read_message() -> str:
        return config.message

    @app.get("/healthz", response_class=PlainTextResponse)
    def healthz() -> str:
        return "ok"

    @app.get("/readyz", response_class=PlainTextResponse)
    def readyz() -> str:
        return f"ok source={config.source}"

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> bytes:
        return generate_latest()

    return app
