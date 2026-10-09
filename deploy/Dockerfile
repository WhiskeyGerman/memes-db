# syntax=docker/dockerfile:1.7
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

FROM ${PYTHON_IMAGE} AS python-runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH=/opt/venv/bin:${PATH} VIRTUAL_ENV=/opt/venv \
    OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && apt-get install --no-install-recommends -y \
    ffmpeg util-linux libgomp1 libglib2.0-0 libgl1 libsndfile1 ca-certificates
RUN groupadd --gid 10001 memes && \
    useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin memes && \
    mkdir -p /var/cache/memes-worker /models && \
    chown memes:memes /var/cache/memes-worker /models
WORKDIR /app

FROM python-runtime AS poetry-build
ARG POETRY_VERSION=2.2.1
ENV POETRY_NO_INTERACTION=1 POETRY_VIRTUALENVS_CREATE=false
RUN python -m venv /opt/venv && \
    /usr/local/bin/python -m pip install --no-cache-dir poetry==${POETRY_VERSION}
FROM poetry-build AS project-build
COPY pyproject.toml poetry.lock /app/
COPY packages /app/packages
COPY services /app/services
COPY deploy/scripts /app/deploy/scripts

FROM project-build AS backend-build
RUN --mount=type=cache,target=/root/.cache/pypoetry \
    poetry install --only main --no-root

FROM poetry-build AS worker-project-build
COPY packages/kernel /app/packages/kernel
COPY services/worker /app/services/worker

FROM worker-project-build AS worker-client-build
RUN --mount=type=cache,target=/root/.cache/pypoetry \
    poetry -C services/worker install --only main

FROM worker-project-build AS worker-ml-build
ENV POETRY_REQUESTS_TIMEOUT=120 POETRY_REQUESTS_MAX_RETRIES=3 POETRY_INSTALLER_MAX_WORKERS=4
RUN --mount=type=cache,target=/root/.cache/pypoetry \
    poetry -C services/worker install --only main,ml,ocr

FROM python-runtime AS backend
ARG RELEASE_ID
RUN test -n "${RELEASE_ID}"
LABEL org.opencontainers.image.title="Memes DB backend" \
      org.opencontainers.image.revision="${RELEASE_ID}"
COPY --from=backend-build /opt/venv /opt/venv
COPY --from=backend-build /app /app
USER 10001:10001
EXPOSE 8080
STOPSIGNAL SIGTERM
CMD ["memes-catalog"]

FROM python-runtime AS worker-client
ARG RELEASE_ID
RUN test -n "${RELEASE_ID}"
LABEL org.opencontainers.image.title="Memes DB standalone worker client" \
      org.opencontainers.image.revision="${RELEASE_ID}"
COPY --from=worker-client-build /opt/venv /opt/venv
COPY --from=worker-client-build /app /app
USER 10001:10001
STOPSIGNAL SIGTERM
CMD ["memes-worker"]

FROM python-runtime AS worker-ml
ENV HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    HF_HOME=/tmp/huggingface HF_MODULES_CACHE=/tmp/huggingface/modules \
    XDG_CACHE_HOME=/tmp/xdg-cache PADDLE_HOME=/tmp/paddle \
    PADDLE_OCR_BASE_DIR=/tmp/paddleocr TORCH_HOME=/tmp/torch HF_HUB_DISABLE_TELEMETRY=1 \
    LD_LIBRARY_PATH=/opt/venv/lib/python3.12/site-packages/nvidia/cublas/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cuda_runtime/lib:/opt/venv/lib/python3.12/site-packages/nvidia/cuda_nvrtc/lib:/opt/venv/lib/python3.12/site-packages/nvidia/nvjitlink/lib:/usr/local/nvidia/lib:/usr/local/nvidia/lib64
ARG RELEASE_ID
RUN test -n "${RELEASE_ID}"
LABEL org.opencontainers.image.title="Memes DB offline ML worker" \
      org.opencontainers.image.revision="${RELEASE_ID}"
COPY --from=worker-ml-build /opt/venv /opt/venv
COPY --from=worker-ml-build /app /app
USER 10001:10001
STOPSIGNAL SIGTERM
CMD ["memes-worker"]
