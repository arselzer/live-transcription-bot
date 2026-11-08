# Caption processing only. Browser, media devices and originals stay in the VM.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
WORKDIR /app
COPY --chmod=0444 backend.pyz /app/backend.pyz
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LIVE_TRANSCRIPT_CONFIG=/config/backend.toml
WORKDIR /tmp
USER 10001:10001
ENTRYPOINT ["python3", "/app/backend.pyz", "captions"]
