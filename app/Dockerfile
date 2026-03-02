# --- builder stage ---
FROM python:3.14.3-slim@sha256:6a27522252aef8432841f224d9baaa6e9fce07b07584154fa0b9a96603af7456 AS builder

ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

COPY app/requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /build/requirements.txt

# --- runtime stage ---
FROM python:3.14.3-slim@sha256:6a27522252aef8432841f224d9baaa6e9fce07b07584154fa0b9a96603af7456

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN addgroup --gid 10001 --system app && adduser --uid 10001 --system --ingroup app app

WORKDIR /workspace
RUN chown app:app /workspace

# Copy only installed packages from builder
COPY --from=builder /install /usr/local

COPY --chown=app:app app/__init__.py /workspace/app/__init__.py
COPY --chown=app:app app/src /workspace/app/src

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

USER app

CMD ["uvicorn", "app.src.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
