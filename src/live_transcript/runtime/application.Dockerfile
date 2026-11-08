# Controller, UI, retrieval and final CPU processing; browsers remain in VMs.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build
RUN apt-get update && apt-get install -y --no-install-recommends git cmake g++ make ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/ggml-org/whisper.cpp.git /build \
    && git -C /build checkout --detach d1be6fde11ac6e0407606b4e42fe72d34add8037 \
    && cmake -S /build -B /build/out -DCMAKE_BUILD_TYPE=Release \
       -DGGML_NATIVE=OFF -DGGML_CUDA=OFF -DGGML_HIP=OFF -DWHISPER_CURL=OFF -DBUILD_SHARED_LIBS=OFF \
    && cmake --build /build/out --target whisper-server whisper-cli --parallel 2
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS runtime
ARG APP_UID=10001
ARG APP_GID=10001
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg openssh-client rsync netcat-openbsd util-linux fonts-dejavu-core libgomp1 \
    && rm -rf /var/lib/apt/lists/*
RUN (getent group "$APP_GID" >/dev/null || groupadd --gid "$APP_GID" transcript) \
    && (getent passwd "$APP_UID" >/dev/null || useradd --uid "$APP_UID" --gid "$APP_GID" --home-dir /data --no-create-home transcript)
COPY --from=build /build/out/bin/whisper-cli /usr/local/bin/whisper-cli
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONSAFEPATH=1 HOME=/data LIVE_TRANSCRIPT_CONFIG=/config/config.toml
WORKDIR /data
USER 10001:10001
ENTRYPOINT ["live-transcript"]
CMD ["serve", "app"]

# Package reviewed source. Exported wheel deployments replace this stage.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS package
WORKDIR /source
COPY . /source
RUN python3 -m pip wheel --no-deps --wheel-dir /wheels /source

FROM runtime AS application
USER 0:0
COPY --from=package /wheels/ /tmp/application/
RUN python3 -m pip install --no-cache-dir Pillow==13.0.0 /tmp/application/*.whl \
    && rm -rf /tmp/application
USER 10001:10001
