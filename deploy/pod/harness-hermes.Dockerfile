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
# On a DGX Spark this is built arm64. HERMES_REF defaults to the default branch, i.e. the latest
# source; set a tag or commit to reproduce an older result. Either way the version disclosed in the
# report is read back out of the built container, not assumed from this arg.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 \
    TERMINAL_ENV=local

# git to fetch the harness source; the agent's terminal toolset expects a POSIX shell + coreutils
# (present in slim). Add build-essential only if a transitive dep needs to compile.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# VERIFIED: the repo clones, and `run_agent.py` sits at the repo root - that path is the adapter's
# contract (ENTRYPOINT `python /app/run_agent.py`). HERMES_REF takes a branch, tag, or commit.
ARG HERMES_REF=main
RUN git clone --depth 1 --branch "${HERMES_REF}" \
        https://github.com/NousResearch/hermes-agent.git /app \
    || git clone https://github.com/NousResearch/hermes-agent.git /app \
        && git -C /app checkout "${HERMES_REF}"
WORKDIR /app

# Install the harness + its deps. Prefer the repo's own metadata; fall back to requirements.txt.
# `pip install .` FAILS here - hermes-agent's wheel build errors out ("Failed building wheel
# for hermes-agent"). The image that actually works installs it EDITABLE via uv, which skips the
# wheel build, plus the runtime deps its pyproject does not pull in. Verified against the running
# aeon-harness-hermes image.
RUN pip install --no-cache-dir uv \
    && uv pip install --system --no-cache -e "." \
    && uv pip install --system --no-cache fire rich openai pyyaml tenacity httpx

# GATE: the build must fail if the entrypoint or its deps are broken. Tracking the default
# branch means upstream can change its dependencies under us, and a harness that installs but
# won't start scores 0 on every agentic task with no error anywhere.
RUN python -c "import fire, openai, tenacity, yaml, httpx" && test -f /app/run_agent.py

# The adapter runs `docker run --rm aeon-harness-hermes --version` for version disclosure and
# passes Hermes' own flags (--query=…) for a task. `python /app/run_agent.py` receives those argv.
ENTRYPOINT ["python", "/app/run_agent.py"]
