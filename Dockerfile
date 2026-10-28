FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_DIR=/data

WORKDIR /app
RUN groupadd --gid 10001 notifier \
    && useradd --uid 10001 --gid 10001 --no-create-home notifier \
    && mkdir /data \
    && chown notifier:notifier /data

# Explicit source allowlist: no config, credentials, database, or Git history.
COPY --chown=10001:10001 notifier.py configuration.py destinations.py message_content.py codex_resets.py reset_events.py /app/
USER 10001:10001

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "/app/notifier.py", "--health-check", "ready"]
ENTRYPOINT ["python", "/app/notifier.py"]
