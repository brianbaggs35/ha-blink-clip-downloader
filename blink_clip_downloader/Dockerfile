ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

# BUILD_ARCH is injected by the HA build system for multi-arch images.
ARG BUILD_ARCH
ARG BUILD_VERSION=2.8.0

# Install C build tools needed by some Python wheels.
# jpeg-dev + zlib-dev are required by Pillow.
# make + cmake are needed by moondream's kestrel/TVM dependency (moondream_local provider).
RUN apk add --no-cache gcc g++ musl-dev libffi-dev openssl-dev ffmpeg jpeg-dev zlib-dev make cmake

# Install Python dependencies first (Docker layer cache)
COPY requirements.txt /tmp/requirements.txt
RUN pip3 install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# Optional: moondream local model support (moondream_local provider).
# Requires building kestrel/TVM from source – may take several minutes.
# Non-fatal: if build fails the moondream_local provider reports "not installed"
# at runtime and the image still starts normally.
RUN pip3 install --no-cache-dir moondream 2>&1 | tail -5 || \
    echo "INFO: moondream could not be built on this platform; moondream_local provider will be unavailable"

# Install the app package so `python3 -m blink_downloader` works anywhere
COPY pyproject.toml /app/pyproject.toml
COPY blink_downloader/ /app/blink_downloader/
RUN pip3 install --no-cache-dir /app \
    && rm -rf /app

# Copy rootfs overlay (services.d service definition)
COPY rootfs /
# Belt-and-suspenders chmod — git already tracks 100755, but some CI/CD
# pipelines don't preserve git file modes through COPY.
RUN chmod a+x /etc/services.d/blink-downloader/run \
    && chmod a+x /etc/services.d/blink-downloader/finish

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

