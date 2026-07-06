# AEON Bench — the POD image (`ghcr.io/aeon-7/aeon-pod`): dashboard + control plane, prebuilt
# and multi-platform (linux/amd64 + linux/arm64).
#
# DEFAULT = the POD DASHBOARD: `docker run` this image and open http://localhost:8091 — the Run
# tab does everything (validate a model by hash, pick an engine container, launch, watch Live).
# The same image is the one-shot CONTROL PLANE when compose overrides the entrypoint (see
# docker-compose.yml: `pull` and `aeon-pod` services).
#
#   dashboard        serve.py (AEON_ROLE=pod) — Run tab, Live view, local boards
#   validate/pull    modelhost.py: resolve → fetch_ref → [pull] → sha256-verify vs the HF manifest
#   serve            engines.py: the user's chosen engine CONTAINER (aeon-vllm-ultimate / vLLM /
#                    SGLang / llama.cpp / ROCm — launched as SIBLINGS via the mounted docker
#                    socket; Apple MLX is the documented bare-metal exception)
#   bench + submit   aeon_pod.py → runner → ed25519-signed bundle → mothership
#
# Build context is the REPO ROOT (so it can COPY mvp/):
#   docker build -f deploy/pod/Dockerfile -t aeon-pod .
# Multi-arch publishing runs in CI (.github/workflows/pod-image.yml).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONUTF8=1
WORKDIR /app

# curl: healthcheck + diagnostics. The docker CLI (static binary, arch-matched) lets the pod
# launch engine/harness containers through the HOST's mounted /var/run/docker.sock — the daemon
# itself is never in this image. nvidia-smi is best-effort via the NVIDIA Container Toolkit.
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && DOCKER_ARCH=$([ "$TARGETARCH" = "arm64" ] && echo aarch64 || echo x86_64) \
    && curl -fsSL "https://download.docker.com/linux/static/stable/${DOCKER_ARCH}/docker-27.3.1.tgz" \
       | tar -xz --strip-components=1 -C /usr/local/bin docker/docker

# Full dashboard deps (fastapi/uvicorn/crypto/argon2/Pillow + the pod's HF toolchain). psycopg
# and boto3 ship for import-safety; a pod never touches Postgres/S3 (local SQLite + fs blobs).
COPY mvp/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt \
    "huggingface_hub>=0.24" \
    "safetensors>=0.4" \
    "requests>=2.31"

# The whole control plane + dashboard (aeon.* + pod.* + web/ + suites/).
COPY mvp/ /app/mvp/
WORKDIR /app/mvp

# ~/.aeon (device key + pod.db) persists via a volume; /models holds pulled/validated weights.
# AEON_MODELS_HOST_DIR must be set at `docker run` when serve containers need the HOST path of
# the models volume (sibling-container -v mounts) — see the quickstart.
ENV HOME=/root AEON_ROLE=pod AEON_DB=/root/.aeon/pod.db AEON_PORT=8091 AEON_MODELS_DIR=/models
VOLUME ["/root/.aeon", "/models"]
EXPOSE 8091

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD curl -fsS "http://127.0.0.1:${AEON_PORT}/healthz" || exit 1

# DEFAULT: the dashboard. Compose services override this for the one-shot control-plane jobs.
ENTRYPOINT ["python"]
CMD ["serve.py"]
