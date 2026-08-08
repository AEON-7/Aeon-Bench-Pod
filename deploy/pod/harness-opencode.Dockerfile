# AEON Bench — OpenCode harness wrapper image.
#
# OpenCode (https://github.com/anomalyco/opencode) ships as an npm package. The pod drives the
# agentic suite THROUGH this harness, pointed at the served `model-under-test` alias, and the
# harness's exact release version is captured for disclosure in the report (harnesses.py:
# version_cmd = ["opencode", "--version"], package = "opencode-ai"). Keep this a VANILLA install
# — no tuned prompt/tool-docs/retry/max_steps — so the model×harness delta stays apples-to-apples.
#
# Tracks the latest release by default; OPENCODE_VERSION pins it when reproducing an old result.
# Either way the disclosed harness_version comes from querying this container, not from the arg.
# node 24: matches the openclaw harness, and stays ahead of the engine floors these CLIs
# raise between releases now that we track `latest`.
FROM node:24-slim

# NOTE: the package name is `opencode-ai` but the CLI binary it installs is `opencode`
# (harnesses.py: package="opencode-ai", version_cmd=["opencode", ...]). A bare `opencode` package
# is NOT the harness.
#
# Tracks the LATEST opencode-ai release; the installed version is queried from this container at
# run time and disclosed as `harness_version`. Set OPENCODE_VERSION to reproduce an older result.
ARG OPENCODE_VERSION=latest
RUN npm install -g "opencode-ai@${OPENCODE_VERSION}"

# OpenCode reads its OpenAI-compatible provider + model from `opencode.json` in its cwd — NOT from
# env vars. The pod does NOT run this image as a service: per task it drops a freshly-generated
# opencode.json into the workdir and invokes:
#   docker run --rm --network host -v <workdir>:/work -w /work aeon-harness-opencode \
#     run --format json --auto -m dgx/<alias> "<prompt>"
# (see mvp/pod/adapters/opencode.py:build_config + run_task). So there are no OPENAI_* envs here.

# GATE, not a sanity print: the build must FAIL if the CLI cannot start - a harness that
# installs but won't run scores 0 on every agentic task silently. Never add `|| true` here.
RUN opencode --version

ENTRYPOINT ["opencode"]
