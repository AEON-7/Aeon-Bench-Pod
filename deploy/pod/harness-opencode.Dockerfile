# AEON Bench — OpenCode harness wrapper image.
#
# OpenCode (https://github.com/anomalyco/opencode) ships as an npm package. The pod drives the
# agentic suite THROUGH this harness, pointed at the served `model-under-test` alias, and the
# harness's exact release version is captured for disclosure in the report (harnesses.py:
# version_cmd = ["opencode", "--version"], package = "opencode-ai"). Keep this a VANILLA install
# — no tuned prompt/tool-docs/retry/max_steps — so the model×harness delta stays apples-to-apples.
#
# Pin the version with the OPENCODE_VERSION build arg so the disclosed harness_version is exact.
FROM node:22-slim

# NOTE: the package name is `opencode-ai` but the CLI binary it installs is `opencode`
# (harnesses.py: package="opencode-ai", version_cmd=["opencode", ...]).
# TODO verify: confirm `opencode-ai@<version>` is the correct published package and that this
#   pinned version exists on the npm registry.
ARG OPENCODE_VERSION=0.3.0
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}"

# TODO verify the exact env/flag OpenCode uses to point at an OpenAI-compatible endpoint + model
#   + key. The compose wires the common OPENAI_* envs as a default; adjust once confirmed.
ENV OPENAI_BASE_URL="" OPENAI_API_KEY="" OPENCODE_MODEL="model-under-test"

RUN opencode --version || true

ENTRYPOINT ["opencode"]
