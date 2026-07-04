# AEON Bench — Hermes Agent harness image (`aeon-harness-hermes`).
#
# Hermes Agent (https://github.com/NousResearch/hermes-agent) is NousResearch's agent harness.
# harnesses.py declares deploy="docker"; the pod drives the agentic suite THROUGH this image,
# pointed at the served `model-under-test` alias, and discloses the exact version in the report.
#
# The pod launches this image ONE one-shot container PER TASK (see mvp/pod/adapters/hermes.py):
#
#   docker run --rm --name … --network host -e TERMINAL_CWD=/work \
#     -v <cfg>:/root/.hermes/config.yaml:ro -v <workdir>:/work -w /work \
#     aeon-harness-hermes --query=<prompt> --base_url=<url> --api_key=sk-local \
#     --model=<alias> --max_turns=8 --save_sample [--disabled_toolsets=<csv>]
#
# So this image MUST: (a) ENTRYPOINT `python /app/run_agent.py` so the argv after the image name
# is Hermes' own flag list; (b) bake TERMINAL_ENV=local so the agent's terminal/file tools execute
# INSIDE this container (in the mounted /work) — NOT docker-in-docker; (c) read its config from
# /root/.hermes/config.yaml (the adapter mounts a context_length:65536 config there to pass the
# Hermes tool-calling gate). Keep it VANILLA — no tuned prompt/tool-docs/retry/max_steps — so the
# model×harness comparison stays apples-to-apples (see AGENTS.md).
#
# On a DGX Spark this is built arm64. Pin the source with HERMES_REF so the disclosed version is
# reproducible; bump deliberately — a harness update can move scores.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    TERMINAL_ENV=local

# git to fetch the harness source; the agent's terminal toolset expects a POSIX shell + coreutils
# (present in slim). Add build-essential only if a transitive dep needs to compile.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# TODO verify: confirm the repo URL + that HERMES_REF resolves, and that the installable entry
#   point is `run_agent.py` at the repo root (harnesses.py only declares the GitHub repo; the
#   adapter's contract is ENTRYPOINT `python /app/run_agent.py`). Pin HERMES_REF to a release
#   tag/commit for a reproducible disclosed version.
ARG HERMES_REF=main
RUN git clone --depth 1 --branch "${HERMES_REF}" \
        https://github.com/NousResearch/hermes-agent.git /app \
    || git clone https://github.com/NousResearch/hermes-agent.git /app \
        && git -C /app checkout "${HERMES_REF}"
WORKDIR /app

# Install the harness + its deps. Prefer the repo's own metadata; fall back to requirements.txt.
RUN if [ -f pyproject.toml ] || [ -f setup.py ]; then pip install .; \
    elif [ -f requirements.txt ]; then pip install -r requirements.txt; fi

# The adapter runs `docker run --rm aeon-harness-hermes --version` for version disclosure and
# passes Hermes' own flags (--query=…) for a task. `python /app/run_agent.py` receives those argv.
ENTRYPOINT ["python", "/app/run_agent.py"]
