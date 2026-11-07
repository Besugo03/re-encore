# syntax=docker/dockerfile:1

# =====================================================================
# re-encore: ab-av1 Auto-Encoder Dashboard
#
# Stage 1: pinned, checksum-verified static ab-av1 binary (musl, portable)
# Stage 2: pinned, checksum-verified static ffmpeg
#          (NVENC AV1 + libvmaf with built-in models + libsvtav1)
# Stage 3: slim Python runtime, running as your host user
#
# Why static ffmpeg? Distro ffmpeg builds lack av1_nvenc (Debian 12)
# or libvmaf (Debian 12/13), and ab-av1's --min-vmaf requires BOTH
# libvmaf and libsvtav1 per its README. BtbN's builds provide all of it.
# =====================================================================

# ---------- Stage 1: ab-av1 ----------
FROM alpine:3.20 AS abav1

ARG AB_AV1_VERSION=0.11.7
# sha256 of the official GitHub release asset
ARG AB_AV1_SHA256=40c035df608162fcb1c991d1474bc9bc8817e0354ac7aef5be5314d3efd64069

RUN apk add --no-cache curl zstd \
 && mkdir -p /out \
 && curl -fsSL -o /tmp/ab-av1.tar.zst \
        "https://github.com/alexheretic/ab-av1/releases/download/v${AB_AV1_VERSION}/ab-av1-v${AB_AV1_VERSION}-x86_64-unknown-linux-musl.tar.zst" \
 && echo "${AB_AV1_SHA256}  /tmp/ab-av1.tar.zst" | sha256sum -c - \
 && zstd -d -c /tmp/ab-av1.tar.zst | tar -x -C /tmp \
 && install -m 0755 /tmp/ab-av1 /out/ab-av1

# ---------- Stage 2: ffmpeg ----------
FROM alpine:3.20 AS ffmpeg

# Monthly BtbN build (kept upstream for 2 years). Bump URL + sha together.
# Includes: av1_nvenc/h264_nvenc/hevc_nvenc, libsvtav1, libvmaf (models
# built in via -Dbuilt_in_models=true), libopus, libx264, libx265, ...
ARG FFMPEG_URL=https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-2026-09-30-13-08/ffmpeg-n9.0.2-17-g2a571b6068-linux64-gpl-9.0.tar.xz
ARG FFMPEG_SHA256=68ee646831adaae2495618346f3bba94ff207ff83bbd34d643e7004730d66269

RUN apk add --no-cache curl xz \
 && curl -fsSL -o /tmp/ffmpeg.tar.xz "${FFMPEG_URL}" \
 && echo "${FFMPEG_SHA256}  /tmp/ffmpeg.tar.xz" | sha256sum -c - \
 && mkdir -p /out \
 && tar -xJf /tmp/ffmpeg.tar.xz -C /tmp \
 && install -m 0755 /tmp/ffmpeg-*/bin/ffmpeg  /out/ffmpeg \
 && install -m 0755 /tmp/ffmpeg-*/bin/ffprobe /out/ffprobe

# ---------- Stage 3: runtime ----------
# Same Python minor + package pins as the known-good local venv.
FROM python:3.14-slim-trixie

# Host user to run as (keeps encoded files owned by you, not root)
ARG PUID=1000
ARG PGID=1000
ARG APP_USER=re-encore

ENV DEBIAN_FRONTEND=noninteractive
# tini: proper PID 1 signal handling / zombie reaping for the ab-av1 subprocesses
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini ca-certificates \
 && rm -rf /var/lib/apt/lists/*

COPY --from=abav1  /out/ab-av1  /usr/local/bin/ab-av1
COPY --from=ffmpeg /out/ffmpeg  /usr/local/bin/ffmpeg
COPY --from=ffmpeg /out/ffprobe /usr/local/bin/ffprobe

# Fail the build early if the toolchain ever regresses: ab-av1 needs
# av1_nvenc (GPU), libsvtav1 (CPU fallback) and libvmaf (--min-vmaf).
RUN ffmpeg -hide_banner -encoders | grep -q 'av1_nvenc' \
 && ffmpeg -hide_banner -encoders | grep -q 'libsvtav1' \
 && ffmpeg -hide_banner -filters | grep -q 'libvmaf' \
 && ab-av1 --version

# Non-root user with a real HOME (gradio/ffmpeg caches live there)
RUN groupadd --gid "${PGID}" "${APP_USER}" \
 && useradd --uid "${PUID}" --gid "${PGID}" --create-home --shell /usr/sbin/nologin "${APP_USER}"

WORKDIR /app

# Python deps (fully pinned)
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Application code: runtime state (config/history/thumbnails) is NOT baked
# in; it lives in the mounted /app/data volume.
# NOTE: /app itself must be user-writable because ab-av1 creates its scratch
# `.ab-av1-XXXX` temp dirs in the process working directory (unless --temp-dir
# is passed).
COPY --chown=${PUID}:${PGID} app.py re-encore.py ./
RUN mkdir -p /app/data && chown -R "${PUID}:${PGID}" /app

ENV DATA_DIR=/app/data \
    PYTHONUNBUFFERED=1 \
    GRADIO_ANALYTICS_ENABLED=False \
    HOME=/home/${APP_USER}

USER ${APP_USER}
EXPOSE 7860

# app.py binds 0.0.0.0:7860, matching EXPOSE and the compose port mapping.
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/', timeout=8)"

ENTRYPOINT ["tini", "--"]
# APP_FILE switches to the older UI: APP_FILE=re-encore.py
CMD ["sh", "-c", "exec python \"${APP_FILE:-app.py}\""]
