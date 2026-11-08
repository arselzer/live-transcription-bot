# A real KVM guest; the browser never runs in this container's userspace.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
ARG APP_UID=10001
ARG APP_GID=10001
RUN apt-get update && apt-get install -y --no-install-recommends \
    qemu-system-x86 qemu-utils cloud-image-utils openssh-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN (getent group "$APP_GID" >/dev/null || groupadd --gid "$APP_GID" transcript) \
    && (getent passwd "$APP_UID" >/dev/null || useradd --uid "$APP_UID" --gid "$APP_GID" --home-dir /vm --no-create-home transcript)
COPY *.whl /tmp/application/
RUN python3 -m pip install --no-cache-dir --no-deps /tmp/application/*.whl && rm -rf /tmp/application
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONSAFEPATH=1 HOME=/vm
WORKDIR /vm
USER 10001:10001
ENTRYPOINT ["python3", "-m", "live_transcript.runtime.qemu"]
