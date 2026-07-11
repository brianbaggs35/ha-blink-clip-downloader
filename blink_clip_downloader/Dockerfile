# Declared before any FROM (required so its value reaches the *second*
# stage's FROM below — an ARG declared between stages instead of at the top
# of the file isn't visible to a later FROM under the legacy (non-BuildKit)
# builder, which some CI/local Docker setups still default to).
ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-debian:trixie

# Separate build stage so the Node toolchain (and its node_modules) never
# lands in the final image — only the static build output (frontend/dist,
# written to blink_downloader/static/ per vite.config.ts) is copied over
# below. Pinned to the Node LTS matching this repo's engines; bumping this
# needs no coordination with the Python base image argument above.
FROM node:22-bookworm-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM ${BUILD_FROM}

# BUILD_ARCH is injected by the HA build system for multi-arch images.
ARG BUILD_ARCH
ARG BUILD_VERSION=2.8.1

# Base image switched from Alpine to Debian (trixie, glibc) in 4.1.0 — the
# optional computer-vision pipeline (ai_object_detection_enabled and friends,
# see config.yaml) depends on torch, which has no official wheels for
# musl/Alpine on any architecture. This is the same class of problem already
# documented below for moondream's TVM build on aarch64 musllinux, just
# affecting a much larger and more central set of dependencies. Debian's
# "*-base-debian" images ship no interpreter at all (unlike the old
# "*-base-python" Alpine images), so Python itself is installed explicitly
# below. Trixie (Debian 13) ships Python 3.13 by default, satisfying this
# project's requires-python = ">=3.12".
#
# PIP_BREAK_SYSTEM_PACKAGES: Debian's system pip refuses "externally managed
# environment" installs (PEP 668) by default. This container is single
# purpose and never shares its Python environment with the host OS package
# manager, so that protection is unnecessary here — set globally rather than
# passing --break-system-packages on every pip3 invocation below.
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# python3-dev + build-essential: compile any C-extension wheel without a
# prebuilt manylinux wheel for this Python version.
# libjpeg-dev + zlib1g-dev: required by Pillow.
# libgl1 + libglib2.0-0: required by opencv (cv2) at import time, even with
# the "headless" wheel — several of its dynamic library dependencies are
# still linked against these at runtime.
# cmake: only used by moondream on amd64 but small enough to install
# unconditionally, matching the previous Alpine image's behavior.
# postgresql-17: the add-on's clip library database (see database.py) — a
# real Postgres server bundled and supervised in this same container (see
# rootfs/etc/services.d/postgresql), not a user-configured external
# database. Directly available in Debian trixie's own archive (verified at
# https://packages.debian.org/trixie/postgresql-17), so no extra apt
# repository is needed to pin this exact major version. Was postgresql-16
# through 5.0.0; bumped because Debian trixie's archive stopped carrying
# postgresql-16 (this stage started failing with "Unable to locate package
# postgresql-16" — trixie now only ships -17). No data-migration step needed
# since the data directory path itself is versioned
# (/data/postgresql/<major>/main, see 01-postgres-init.sh) and this had not
# shipped in a tagged release yet.
RUN apt-get update && apt-get install --no-install-recommends -y \
        python3 python3-pip python3-dev build-essential \
        libffi-dev libssl-dev ffmpeg libjpeg-dev zlib1g-dev cmake \
        libgl1 libglib2.0-0 ca-certificates postgresql-17 \
    && rm -rf /var/lib/apt/lists/* \
    # postgresql-17's postinst auto-creates a default "17 main" cluster under
    # /var/lib/postgresql via Debian's own pg_createcluster/postgresql-common
    # machinery. This add-on manages its own data directory under /data
    # instead (see rootfs/etc/cont-init.d/01-postgres-init.sh), started
    # directly rather than through that wrapper, so the auto-created default
    # cluster is dead weight — drop it to keep the image smaller. Non-fatal
    # if it wasn't created in the first place (e.g. a base image that
    # already suppresses service auto-start on package install).
    && (pg_dropcluster --stop 17 main || echo "INFO: no default postgresql cluster to drop")

# Install Python dependencies first (Docker layer cache)
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Optional: computer-vision enhancement pipeline (object detection/tracking,
# depth estimation, contact segmentation, face recognition — see vision.py
# and the ai_enhanced_detection_enabled/ai_face_recognition_enabled config
# options, both off by default). These packages are large (order of 1-2 GB combined,
# mostly torch itself) and are installed unconditionally on both
# architectures so the config toggles work out of the box, but nothing in
# this block is imported or loaded into memory unless a feature is actually
# enabled — see vision.py's lazy-import pattern.
#
# torch is installed from PyTorch's dedicated CPU-only wheel index first, as
# a separate step, per PyTorch's own install guidance — the default PyPI
# index resolves torch's GPU/CUDA build, which pulls several additional
# gigabytes of NVIDIA runtime libraries that are useless here (this add-on
# never has GPU access) and would roughly double the final image size.
# Installing the CPU wheel first means the packages below (which merely
# require *some* compatible torch) reuse it instead of pulling the CUDA
# build in as a transitive dependency.
#
# torchvision must *also* come from the CPU wheel index, not default PyPI —
# ultralytics depends on it, and a torchvision built against mainline
# (CUDA-targeted) torch is ABI-incompatible with the "+cpu" torch build
# above despite both satisfying the same version range on paper. Installing
# it here first, from the matching index, means ultralytics's own resolver
# finds a compatible torchvision already present instead of pulling in a
# mismatched one — verified by hand: without this, `import torchvision`
# raises "RuntimeError: operator torchvision::nms does not exist" at runtime.
#
# facenet-pytorch is installed separately with --no-deps: its pinned
# `Pillow<10.3.0,>=10.2.0` predates Python 3.13 wheel support, so pip falls
# back to building Pillow 10.2.0 from source, which fails outright under
# current setuptools (KeyError: '__version__' in Pillow's legacy setup.py) —
# and 10.2.0 conflicts with this project's own Pillow>=12.3.0 requirement
# regardless. --no-deps skips that broken/conflicting pin; facenet-pytorch's
# actual runtime code (MTCNN + InceptionResnetV1) only needs torch,
# torchvision, numpy, and Pillow — all already installed above by the time
# this line runs — and was confirmed by hand to work correctly against
# Pillow 12.3.0.
#
# Entirely non-fatal, matching the moondream block above: if this fails for
# any reason (network hiccup, a wheel temporarily missing for one
# architecture, disk pressure on an emulated aarch64 CI runner, etc.) the
# add-on still builds and runs — the affected config toggles simply report
# themselves unavailable at runtime (see vision.py's is_*_available() checks).
RUN ( \
        pip3 install --no-cache-dir "torch>=2.4,<3" "torchvision>=0.19,<1" \
            --index-url https://download.pytorch.org/whl/cpu \
        && pip3 install --no-cache-dir \
            "ultralytics>=8.3,<9" \
            "opencv-python-headless>=4.10,<5" \
            "transformers>=4.56,<5" \
        && pip3 install --no-cache-dir --no-deps "facenet-pytorch>=2.6,<3" \
        2>&1 | tail -20 \
    ) || echo "INFO: computer-vision pipeline install failed; ai_object_detection_enabled \
and related toggles will report unavailable"

# Optional: moondream local model support (moondream_local provider).
# Attempted on both architectures as of the 4.1.0 Debian base image switch —
# the historical blocker was musllinux (Alpine) having no wheels for
# moondream's torch/kestrel dependencies, not the architecture itself; torch
# has published manylinux aarch64 wheels for a long time. Deliberately placed
# *after* the computer-vision block above so moondream's own (unpinned)
# torch requirement is satisfied by the CPU-only build already installed
# there instead of pulling in a second, much larger CUDA-enabled torch as a
# fresh transitive dependency.
# Local ("Photon") inference itself still requires an NVIDIA GPU (relevant
# for Jetson-class aarch64 boards, not typical Pi-class ones) or Apple
# Silicon regardless of architecture — see analyzer.py's
# MoondreamLocalAnalyzer._load_model_sync — so most hosts on either
# architecture without a GPU will still see this provider report itself
# unavailable at runtime, exactly as before this change.
# Pinned to the >=1.3,<2 range: the moondream package rewrote its local-
# inference architecture between the 0.x and 1.x lines (0.x: CPU-only ONNX;
# 1.x: GPU-only "Photon" engine via kestrel/torch, requires an API key even
# for local mode) without a matching major-version bump at the time, and an
# unpinned install here previously let that rewrite silently break this
# provider.
# Non-fatal: if the install fails the moondream_local provider reports "not
# installed" at runtime and the image still starts normally.
RUN pip3 install --no-cache-dir "moondream>=1.3,<2" 2>&1 | tail -5 || \
    echo "INFO: moondream install failed; moondream_local provider will be unavailable"

# Install the app package so `python3 -m blink_downloader` works anywhere.
# Ordering note: this must stay the *last* install step. moondream pins
# pillow<11.0.0 and silently downgrades the Pillow>=12.3.0 already installed
# from requirements.txt above (pip's default resolver applies a later
# package's pin even when it conflicts with one installed earlier, only
# warning "incompatible" rather than failing). Installing this project's own
# package last — with its own Pillow>=12.3.0 requirement — re-upgrades
# Pillow back to the version this codebase actually needs. Verified by hand
# that moondream still imports fine against Pillow 12.3.0 despite its
# (overly conservative) pin. If a future dependency gets added after this
# block, re-verify Pillow's final installed version doesn't regress.
COPY pyproject.toml /app/pyproject.toml
COPY blink_downloader/ /app/blink_downloader/
# Built Vue app (see frontend-builder stage above) — media_server.py's
# _handle_index/_STATIC_DIR serve this instead of an embedded HTML string.
COPY --from=frontend-builder /blink_downloader/static /app/blink_downloader/static
RUN pip3 install --no-cache-dir /app \
    && rm -rf /app

# Copy rootfs overlay (services.d service definitions + cont-init.d)
COPY rootfs /
# Belt-and-suspenders chmod — git already tracks 100755, but some CI/CD
# pipelines don't preserve git file modes through COPY.
RUN chmod a+x /etc/services.d/blink-downloader/run \
    && chmod a+x /etc/services.d/blink-downloader/finish \
    && chmod a+x /etc/services.d/postgresql/run \
    && chmod a+x /etc/cont-init.d/01-postgres-init.sh

# No ENTRYPOINT override — the base image's ENTRYPOINT ["/init"] starts
# s6-overlay exactly once.  Any override here (including a wrapper that
# exec-s /init) causes s6-svscan to be started a second time, producing
# the "another instance already running" crash loop.

LABEL \
    org.opencontainers.image.title="Blink Clip Downloader" \
    org.opencontainers.image.description="Continuously downloads Blink camera clips to local HA storage" \
    org.opencontainers.image.source="https://github.com/brianbaggs35/ha-blink-clip-downloader" \
    org.opencontainers.image.authors="Brian Baggs <brianbaggs@hotmail.com>" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version="${BUILD_VERSION}" \
    io.hass.name="Blink Clip Downloader" \
    io.hass.description="Download, browse and stream Blink camera clips in Home Assistant" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}"
