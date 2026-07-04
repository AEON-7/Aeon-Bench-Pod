# AEON Bench — POD control-plane image (the `aeon-pod` runner).
#
# This is the portable, testable A→B control plane (modelhost → runner → submit). It does NOT
# serve the model or run a harness — those are their own containers in docker-compose.yml. The
# pod talks to the served model alias over the network and submits the signed bundle upstream.
#
#   pulls/verifies HF weights  (modelhost.py: resolve→fetch_ref→pull→verify→derive_recipe)
#   benchmarks the served model (aeon_pod.py → aeon.runner, into ~/.aeon/pod.db SQLite)
#   submits the ed25519-signed bundle to the mothership (aeon_submit.py)
#
# Build context is the REPO ROOT (so it can COPY mvp/), matching deploy/Dockerfile's convention.
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PYTHONUTF8=1
WORKDIR /app

# nvidia-smi is invoked best-effort for the hardware/GPU profile (aeon_pod._hardware_profile,
# modelhost._gpu_names). It is NOT installed here — on a GPU host run this container with the
# NVIDIA Container Toolkit so the host's nvidia-smi/driver is visible; absent, the profile just
# omits the GPU block. curl is for the healthcheck/diagnostics.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Pod runtime deps. The pod itself is stdlib + cryptography; modelhost needs huggingface_hub
# (+ safetensors to read tensor metadata on verify); requests is a common HF transitive dep we
# pin explicitly so the image is self-contained.
RUN pip install \
    "huggingface_hub>=0.24" \
    "safetensors>=0.4" \
    "cryptography>=42" \
    "requests>=2.31"

# Copy the control plane. We copy the whole mvp/ tree because the runner's import closure pulls
# in aeon.{db,runner,scoring,suite,evaluators,targets,judge_policy,agentic,blobstore,capabilities,
# vram,...}; copying the package wholesale is simpler and correct than enumerating it. The pod
# uses ONLY the stdlib-sqlite path of aeon.db (AEON_DB_URL is unset in aeon_pod.run_pod).
COPY mvp/ /app/mvp/

# If a generated suite corpus exists at mvp/suites/cases.json it is picked up automatically
# (suite._CASES_FILE); absent, the suite falls back to the built-in cases. You can also mount a
# corpus over /app/mvp/suites at run time (see docker-compose.yml).
WORKDIR /app/mvp

# Device key + local pod dashboard live under ~/.aeon (HOME=/root here). Mount a named volume
# over /root/.aeon to persist the enrolled ed25519 device key across runs (see compose).
ENV HOME=/root AEON_DB=/root/.aeon/pod.db

# The pod is a one-shot job: it benchmarks, submits, and exits. The real flags are injected by
# docker-compose.yml's entrypoint from the .env vars. This default just prints the CLI help.
ENTRYPOINT ["python", "-m", "pod.aeon_pod"]
CMD ["--help"]
