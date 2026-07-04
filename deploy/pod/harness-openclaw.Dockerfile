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
ARG OPENCLAW_VERSION=0.17.0
RUN npm install -g "openclaw@${OPENCLAW_VERSION}"

# OpenClaw reads an OpenAI-compatible endpoint from its config. harnesses.py records
# config_file="~/.openclaw/openclaw.json"; the pod/compose points base_url at the served alias.
# TODO verify the exact env/flag OpenClaw uses to set the OpenAI base URL + model + api key
#   (e.g. OPENAI_BASE_URL / OPENAI_API_KEY, or a `--config` / `--base-url` flag). Until verified,
#   the compose wires the common OPENAI_* envs and mounts a config file.
ENV OPENAI_BASE_URL="" OPENAI_API_KEY="" OPENCLAW_MODEL="model-under-test"

# Sanity: surface the resolved version at build/run time (this is the string the pod discloses).
RUN openclaw --version || true

ENTRYPOINT ["openclaw"]
