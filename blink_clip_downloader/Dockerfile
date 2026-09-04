# Separate build stage so the Node toolchain (and its node_modules) never
# lands in the final image — only the static build output (frontend/dist,
# written to blink_downloader/static/ per vite.config.ts) is copied over
# below. Pinned to the same Node LTS already used everywhere else in CI
# (ci.yaml's lint/test/smoke-test jobs all set node-version: "24"); bumping
# this needs no coordination with the base image below. Those CI jobs run
# eslint/vue-tsc/vitest/playwright directly on the runner under Node 24,
# but never `npm run build` itself (the actual Vite production bundle,
# which only ever runs inside this Docker stage) — verified separately by
# hand via `docker build --target frontend-builder`: type-checks and
# bundles identically to Node 22, same asset output shape/sizes.
FROM node:24-bookworm-slim AS frontend-builder
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# A single multi-platform manifest, not a per-arch ARG BUILD_FROM — HA's
# base images now publish one tag covering both linux/amd64 and
# linux/arm64 (verified: `docker buildx imagetools inspect
# ghcr.io/home-assistant/base-debian:trixie` lists both platforms under
# the same tag), and BuildKit automatically pulls the manifest entry
# matching whatever --platform the build is invoked with. This replaces
# the old build.yaml + `ARG BUILD_FROM`/`FROM ${BUILD_FROM}` pattern,
# which Supervisor now logs as deprecated (see the official migration
# guide: https://developers.home-assistant.io/blog/2026/04/02/builder-migration/)
# — Supervisor still reads build.yaml if present for backward
# compatibility, but there's no reason to keep it once nothing needs a
# per-arch mapping. Both CI workflows (.github/workflows/build.yaml's
# release publish, .github/workflows/ci.yaml's build job) drive the
# target architecture via their own --platform/platforms input, same as
# Supervisor's own local build does — nothing else needed to change.
FROM ghcr.io/home-assistant/base-debian:trixie

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

# TORCH_HOME / HF_HOME: without these, torch.hub (facenet-pytorch's
# InceptionResnetV1) and the transformers/huggingface_hub library (used by
# both DepthEstimator and ContactSegmenter in vision.py) each cache their
# one-time pretrained-weight downloads under $HOME/.cache by default —
# $HOME here is /root, which is ordinary container filesystem, not the
# /data volume. That cache is lost every time the container is recreated
# (an add-on update, not just a restart — verified by hand: a plain
# `docker restart` keeps it, but removing and recreating the container
# does not), so every one of these optional CV features would silently
# redo a many-second, tens-of-MB download the first time it's used after
# every single update. Redirecting both into /data makes the download
# genuinely one-time, matching the persistence already given to moondream's
# own downloaded packages (see __main__.py's /data/moondream_packages
# handling) and to Ultralytics' YOLO weights (see vision.py's
# ObjectDetector, which resolves its model filename against this same
# directory for the same reason — YOLO doesn't consult either of these
# env vars, it downloads to whatever path it's given).
ENV TORCH_HOME=/data/model_cache/torch
ENV HF_HOME=/data/model_cache/hf

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
RUN apt-get update \
    # Patch already-installed base-image packages (e.g. curl, libtasn1-6) to
    # whatever the base image's own preconfigured apt sources currently
    # offer, before installing anything new below. The base image is built
    # and published on its own schedule, so by the time this Dockerfile
    # builds, security patches for packages it already contains may already
    # be sitting in the same suite it's already pointed at — no new apt
    # source/repo is added here, this just catches the base image up to
    # what it's already configured to see. Safe on a stable release
    # (trixie): apt-get upgrade never adds/removes packages, only bumps
    # existing ones within the same suite.
    && apt-get upgrade -y \
    && apt-get install --no-install-recommends -y \
        python3 python3-pip python3-dev build-essential \
        libffi-dev libssl-dev ffmpeg libjpeg-dev zlib1g-dev cmake \
        libgl1 libglib2.0-0 ca-certificates postgresql-17 \
    && rm -rf /var/lib/apt/lists/* \
    # The base image ships /usr/bin/tempio (HA's config-templating helper).
    # Nothing in this add-on uses it, and its statically-linked Go runtime
    # accumulates CVEs (Go stdlib + golang.org/x/crypto) that a container
    # scan flags — remove it rather than carry unused attack surface.
    && rm -f /usr/bin/tempio \
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
        # torch's own install pulls in a pinned, older setuptools/wheel as a
        # transitive build dependency (visible via `pip show`, not declared
        # by this project) — re-upgrading them here closes known CVEs in
        # those specific pinned versions. Neither is imported by this
        # project's own runtime code (they're packaging tools, not
        # application dependencies), so this is a pure vulnerability-surface
        # reduction with no functional effect.
        #
        # setuptools has a proper pip RECORD file, so a plain --upgrade
        # cleanly uninstalls the old version first. That matters here beyond
        # just the CVE fix itself: setuptools vendors its own bundled copies
        # of jaraco.context/wheel/packaging under setuptools/_vendor, and a
        # clean uninstall removes the *old* vendor bundle along with the
        # rest of the old version's files — an --ignore-installed install
        # (as used for wheel below) would instead leave the old vendor
        # bundle on disk alongside the new one, still on disk and still
        # flagged by a CVE scan even though nothing imports it anymore.
        && pip3 install --no-cache-dir --upgrade "setuptools>=78.1.1" \
        # wheel is different: Debian's python3-pip apt package (installed
        # above) hard-depends on python3-wheel, which ships to
        # /usr/lib/python3/dist-packages with no RECORD file (dpkg tracks
        # its files itself, not pip) — a plain --upgrade can't cleanly
        # uninstall it first ("Cannot uninstall wheel: no RECORD file"), and
        # that failure isn't caught by this block's own `|| echo` fallback
        # below since it's chained with `&&` *inside* the parens, so it
        # silently skipped every package after it (ultralytics/opencv/
        # transformers/facenet-pytorch) the first time this was tried.
        # --ignore-installed installs the patched version into /usr/local's
        # site-packages without needing to remove the apt-owned one first;
        # Python resolves /usr/local before /usr/lib so the new version is
        # what actually runs, but the old copy still physically exists and
        # would still surface as a CVE finding (Docker Scout's Python-
        # ecosystem detection walks for *.dist-info regardless of whether
        # apt or pip put it there), so it's deleted directly below instead
        # of just being shadowed. Removing apt's copy of a *file*, not the
        # apt *package* (python3-wheel is never `apt-get remove`d — that
        # would take python3-pip down with it, since pip hard-depends on
        # it): safe because nothing after this point in the build re-runs
        # apt-get against it.
        && pip3 install --no-cache-dir --upgrade --ignore-installed "wheel>=0.46.2" \
        && rm -rf /usr/lib/python3/dist-packages/wheel \
            /usr/lib/python3/dist-packages/wheel-*.dist-info \
        && pip3 install --no-cache-dir \
            "ultralytics>=8.4.140,<9" \
            # lap isn't in ultralytics' own declared dependencies (`pip show
            # ultralytics` doesn't list it) — it's an optional extra
            # ultralytics only imports inside YOLO.track() (the Hungarian-
            # algorithm assignment step ByteTrack/BoT-SORT need), which is
            # exactly the method ObjectDetector in vision.py uses. Without
            # it installed ahead of time, the first real .track() call
            # triggers ultralytics' own "AutoUpdate" fallback — a live pip
            # install from inside the running container, verified by hand
            # to work but undesirable in production: it depends on outbound
            # network access being available at that exact moment (which a
            # user's network policy may not guarantee), and its own log
            # output warns the *current* process may need restarting for
            # the newly installed package to actually take effect.
            "lap>=0.5.12" \
            "opencv-python-headless>=5.0.0.93,<6" \
            # Cap raised from <5 to <6: verified by hand in isolated venvs
            # that both cv2 call sites this add-on actually uses
            # (FramePreprocessor.enhance's imdecode/cvtColor/CLAHE/
            # fastNlMeansDenoisingColored/imencode chain, and
            # ContactDetector's cv2.dilate) produce identical shapes/dtypes
            # and identical behaviorally-significant output under
            # opencv-python-headless 5.0.0 versus the previous 4.10.0 floor.
            # No known CVEs in either line at the time of this bump (checked
            # via pip-audit) — this one's just staying current, unlike the
            # transformers bump below.
            # Floor raised from 4.56 to 5.5 to clear two HIGH-severity CVEs
            # (2026-4372 RCE, 2026-5241 arbitrary code execution via
            # trust_remote_code) that the 4.x line never fixed — found via a
            # trivy image scan. Verified by hand before bumping: the pinned
            # depth-estimation model (VisionPipeline._DepthEstimator in
            # vision.py, "depth-anything/Depth-Anything-V2-Small-hf") loads
            # and runs identically under transformers 5.14 in an isolated
            # venv, producing the same predicted_depth/depth pipeline()
            # output shape this code already expects — the 5.x line's
            # high-level pipeline() API is HuggingFace's most
            # backward-compatible surface across major versions, and this
            # add-on's usage here is exactly that surface, never
            # trust_remote_code (which stays at its safe default of False).
            "transformers>=5.16.1,<6" \
        && pip3 install --no-cache-dir --no-deps "facenet-pytorch>=2.6.0,<3" \
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

# Keep release metadata out of the expensive dependency-install layers above.
# Docker includes in-scope build arguments in cache keys, so declaring these
# here lets code-only releases reuse the large Python/CV layers and lets
# Supervisor pull only the changed application layer.
ARG BUILD_ARCH
ARG BUILD_VERSION=5.4.7
LABEL \
    org.opencontainers.image.title="Blink Clip Downloader" \
    org.opencontainers.image.description="Home Assistant add-on for saving and viewing Blink camera clips, with AI analysis and facial recognition" \
    org.opencontainers.image.source="https://github.com/brianbaggs35/ha-blink-clip-downloader" \
    org.opencontainers.image.authors="Brian Baggs <brianbaggs@hotmail.com>" \
    org.opencontainers.image.licenses="MIT" \
    org.opencontainers.image.version="${BUILD_VERSION}" \
    io.hass.name="Blink Clip Downloader" \
    io.hass.description="Download, browse and stream Blink camera clips in Home Assistant" \
    io.hass.arch="${BUILD_ARCH}" \
    io.hass.type="addon" \
    io.hass.version="${BUILD_VERSION}"
