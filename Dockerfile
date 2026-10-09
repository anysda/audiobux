# Two images, one per process. The web one is FastAPI and the built interface. The
# worker one is the pipeline with what it imports to start, ffmpeg and djvulibre:
# torch with CUDA, F5-TTS, faster-whisper, RUAccent and llama-server, some 4 GB, it
# installs into the data folder on its first start (pylock.runtime.toml, runtime.py),
# the way it fetches the models. The driver comes from the NVIDIA runtime.

FROM ghcr.io/astral-sh/uv:0.12.7 AS uv

# ffmpeg with its own codecs only: the m4b wants aac, loudnorm and the mp4 muxer.
# Debian's ffmpeg brings SDL, Mesa and LLVM along (some 200 MB) for playback.
FROM python:3.12.16-slim-trixie AS ffmpeg
ARG FFMPEG=7.1.5
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential ca-certificates curl nasm xz-utils zlib1g-dev \
 && curl -fsSL https://ffmpeg.org/releases/ffmpeg-$FFMPEG.tar.xz | tar -xJ -C /tmp \
 && cd /tmp/ffmpeg-$FFMPEG \
 && ./configure --prefix=/usr/local --enable-shared --disable-static --disable-autodetect --enable-zlib \
      --disable-network --disable-doc --disable-debug --disable-ffplay \
 && make -j"$(nproc)" \
 && make install DESTDIR=/out

# The interface: built here, served by the web process as plain files.
FROM node:22.22.3-slim AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM python:3.12.16-alpine3.24 AS web-build
COPY --from=uv /uv /usr/local/bin/
WORKDIR /src
COPY pyproject.toml pylock.web.toml ./
COPY audiobux ./audiobux
RUN uv venv -q /opt/venv \
 && uv pip install -q -p /opt/venv --no-cache -r pylock.web.toml \
 && uv pip install -q -p /opt/venv --no-cache --no-deps . \
 && find /opt/venv -name __pycache__ -prune -exec rm -rf {} +

FROM python:3.12.16-alpine3.24 AS web
COPY --from=web-build /opt/venv /opt/venv
COPY --from=frontend /frontend/dist /app/static
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AUDIOBUX_DATA=/data \
    AUDIOBUX_FRONTEND=/app/static
CMD ["audiobux", "web"]

# PyMuPDF's layout analysis is half the package, and nothing here calls it, nor its
# headers or NumPy's own tests.
# EnCodec, which the vocoder imports, comes as source only: it is built here, its
# requirements are the runtime's.
FROM python:3.12.16-slim-trixie AS worker-build
COPY --from=uv /uv /usr/local/bin/
WORKDIR /src
COPY pyproject.toml pylock.worker.toml ./
COPY audiobux ./audiobux
RUN uv venv -q /opt/venv \
 && uv pip install -q -p /opt/venv --no-cache -r pylock.worker.toml \
 && uv pip install -q -p /opt/venv --no-cache --no-deps encodec==0.1.1 . \
 && rm -rf /opt/venv/lib/python3.12/site-packages/pymupdf/layout /opt/venv/lib/python3.12/site-packages/pymupdf/mupdf-devel \
 && find /opt/venv/lib/python3.12/site-packages/numpy -name tests -prune -exec rm -rf {} + \
 && find /opt/venv -name __pycache__ -prune -exec rm -rf {} +

FROM python:3.12.16-slim-trixie AS worker
RUN apt-get update \
 && apt-get install -y --no-install-recommends djvulibre-bin libgomp1 tzdata \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ffmpeg /out/usr/local/bin/ffmpeg /out/usr/local/bin/ffprobe /usr/local/bin/
COPY --from=ffmpeg /out/usr/local/lib/ /usr/local/lib/
RUN ldconfig
COPY --from=worker-build /opt/venv /opt/venv
COPY voice /app/voice
COPY pylock.runtime.toml /app/
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    AUDIOBUX_DATA=/data \
    AUDIOBUX_RUNTIME=/app/pylock.runtime.toml \
    AUDIOBUX_VOICE=/app/voice/m1.wav \
    HF_HOME=/data/models/hf
VOLUME /data
CMD ["audiobux", "worker"]
