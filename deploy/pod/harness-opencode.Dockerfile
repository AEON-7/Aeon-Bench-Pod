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

# OpenCode reads its OpenAI-compatible provider + model from `opencode.json` in its cwd — NOT from
# env vars. The pod does NOT run this image as a service: per task it drops a freshly-generated
# opencode.json into the workdir and invokes:
#   docker run --rm --network host -v <workdir>:/work -w /work aeon-harness-opencode \
#     run --format json --auto -m dgx/<alias> "<prompt>"
# (see mvp/pod/adapters/opencode.py:build_config + run_task). So there are no OPENAI_* envs here.

RUN opencode --version || true

ENTRYPOINT ["opencode"]
