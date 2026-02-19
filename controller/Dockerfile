# --- builder stage ---
FROM python:3.14.3-slim@sha256:486b8092bfb12997e10d4920897213a06563449c951c5506c2a2cfaf591c599f AS builder

ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY controller/requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /build/requirements.txt

# --- runtime stage ---
FROM python:3.14.3-slim@sha256:486b8092bfb12997e10d4920897213a06563449c951c5506c2a2cfaf591c599f

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN addgroup --gid 10001 --system app && adduser --uid 10001 --system --ingroup app app

WORKDIR /workspace
RUN chown app:app /workspace

# Copy only installed packages from builder
COPY --from=builder /install /usr/local

COPY --chown=app:app controller/__init__.py /workspace/controller/__init__.py
COPY --chown=app:app controller/src /workspace/controller/src

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

USER app

CMD ["python", "-m", "controller.src"]
