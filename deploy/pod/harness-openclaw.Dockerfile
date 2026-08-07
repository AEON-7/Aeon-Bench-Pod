# AEON Bench — OpenClaw harness wrapper image.
#
# OpenClaw (https://github.com/openclaw/openclaw) ships as an npm package. The pod drives the
# agentic suite THROUGH this harness, pointed at the served `model-under-test` alias, and the
# harness's exact release version is captured for disclosure in the report (harnesses.py:
# version_cmd = ["openclaw", "--version"]). Keep this a VANILLA install — no tuned system prompt,
# tool docs, retry policy, or max_steps overrides — or the model×harness comparison stops being
# apples-to-apples (see AGENTS.md + trust-architecture §2.3 "vanilla-ness lives in config").
#
# Pin the version with the OPENCLAW_VERSION build arg so the disclosed harness_version is exact
# and reproducible. Bump deliberately — a harness update can move scores.
FROM node:22-slim

# TODO verify: confirm the published npm package name is `openclaw` and that this pinned version
#   exists on the registry (registry.npmjs.org). harnesses.py declares package="openclaw".
# VERIFIED against the running image. Pin 0.17.0 did not build: 0.17.0 was hermes-agent's version pasted in error; openclaw uses CalVer.
ARG OPENCLAW_VERSION=2026.6.11
RUN npm install -g "openclaw@${OPENCLAW_VERSION}"

# OpenClaw reads its OpenAI-compatible endpoint + model from ~/.openclaw/openclaw.json — NOT from
# env vars. The pod does NOT run this image as a service: per task it mounts a freshly-generated
# openclaw.json (baseUrl → the served alias) at /root/.openclaw and invokes:
#   docker run --rm --network host -v <home>:/root/.openclaw aeon-harness-openclaw \
#     agent --local --json --agent main -m "<prompt>" --model dgx/<alias>
# (see mvp/pod/adapters/openclaw.py:build_config + run_task). So there are no OPENAI_* envs here.

# Sanity: surface the resolved version at build/run time (this is the string the pod discloses).
RUN openclaw --version || true

ENTRYPOINT ["openclaw"]
