# Speech services only. The browser and original capture remain in a separate VM.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS build
RUN apt-get update && apt-get install -y --no-install-recommends git cmake g++ make ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN git clone https://github.com/ggml-org/whisper.cpp.git /build \
    && git -C /build checkout --detach d1be6fde11ac6e0407606b4e42fe72d34add8037 \
    && cmake -S /build -B /build/out -DCMAKE_BUILD_TYPE=Release \
       -DGGML_NATIVE=OFF -DGGML_CUDA=OFF -DGGML_HIP=OFF -DWHISPER_CURL=OFF -DBUILD_SHARED_LIBS=OFF \
    && cmake --build /build/out --target whisper-server --parallel 2
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/out/bin/whisper-server /usr/local/bin/whisper-server
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["/usr/local/bin/whisper-server"]
