# One image for both processes: the worker with the pipeline on the card, and the
# web process, which needs none of it but FastAPI. CUDA comes from wheels only, the driver
# from the NVIDIA runtime, so the base is plain Ubuntu. torch brings CUDA 13;
# llama.cpp and CTranslate2 are built for 12 and need its cudart and cuBLAS beside
# (NCCL keeps one soname for both). torch on CUDA 12 instead makes the image about 1 GB
# larger: its own wheels for 12 are bigger than these two.

FROM ghcr.io/ggml-org/llama.cpp:server-cuda-b12063 AS llama

# ffmpeg with its own codecs only: the m4b wants aac, loudnorm and the mp4 muxer.
# Ubuntu's ffmpeg brings SDL, Mesa and LLVM along (some 200 MB) for playback.
FROM ubuntu:24.04 AS ffmpeg
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

FROM ubuntu:24.04

ARG NVIDIA=/opt/venv/lib/python3.12/site-packages/nvidia
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:/opt/llama:$PATH \
    LD_LIBRARY_PATH=/opt/llama:$NVIDIA/cuda_runtime/lib:$NVIDIA/cublas/lib:$NVIDIA/nccl/lib \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    AUDIOBUX_DATA=/data \
    HF_HOME=/data/models/hf \
    AUDIOBUX_VOICE=/app/voice/m1.wav

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv djvulibre-bin libgomp1 tzdata \
 && rm -rf /var/lib/apt/lists/* \
 && python3 -m venv /opt/venv

COPY --from=ffmpeg /out/usr/local/bin/ffmpeg /out/usr/local/bin/ffprobe /usr/local/bin/
COPY --from=ffmpeg /out/usr/local/lib/ /usr/local/lib/
COPY --from=llama /app/llama-server /app/libllama-server-impl.so /app/libllama-common.so.0 /app/libmtmd.so.0 \
     /app/libllama.so.0 /app/libggml.so.0 /app/libggml-base.so.0 /app/libggml-cuda.so /app/libggml-cpu-*.so /opt/llama/
RUN ldconfig

WORKDIR /app
COPY pyproject.toml ./
# F5-TTS without its own requirements: they are the trainer's and the demo's (gradio,
# wandb, datasets, accelerate, bitsandbytes); what inference imports is in `.[f5]`.
# Its model package imports the trainer on load; that line goes. Triton is for
# torch.compile, which nothing here calls.
RUN mkdir audiobux && touch audiobux/__init__.py \
 && pip install --no-cache-dir ".[f5,web]" torch==2.15.0 torchaudio==2.11.1 torchcodec==0.17.0 \
      nvidia-cuda-runtime-cu12==12.9.* nvidia-cublas-cu12==12.9.* \
 && pip install --no-cache-dir --no-deps f5-tts==1.1.22 \
 && sed -i '/^from f5_tts.model.trainer import Trainer$/d' /opt/venv/lib/python3.12/site-packages/f5_tts/model/__init__.py \
 && pip uninstall -y triton
# RUAccent fetches its parser into its own package folder on first load, and the
# processes do not run as root: the parser comes with the image instead.
RUN python -c "import pathlib, ruaccent; from huggingface_hub import snapshot_download; \
snapshot_download('ruaccent/accentuator', revision='b78ae5ea1e62beaf138bed1865cd8c3b0b5ca855', allow_patterns=['koziev/*'], local_dir=pathlib.Path(ruaccent.__file__).parent)"
COPY voice ./voice
COPY audiobux ./audiobux
RUN pip install --no-cache-dir --no-deps .
COPY --from=frontend /frontend/dist ./static
ENV AUDIOBUX_FRONTEND=/app/static

VOLUME /data
CMD ["audiobux", "worker"]
