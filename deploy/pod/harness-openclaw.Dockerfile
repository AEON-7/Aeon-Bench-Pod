# AEON Bench — OpenClaw harness wrapper image.
#
# OpenClaw (https://github.com/openclaw/openclaw) ships as an npm package. The pod drives the
# agentic suite THROUGH this harness, pointed at the served `model-under-test` alias, and the
# harness's exact release version is captured for disclosure in the report (harnesses.py:
# version_cmd = ["openclaw", "--version"]). Keep this a VANILLA install — no tuned system prompt,
# tool docs, retry policy, or max_steps overrides — or the model×harness comparison stops being
# apples-to-apples (see AGENTS.md + trust-architecture §2.3 "vanilla-ness lives in config").
#
# Tracks the LATEST openclaw release. The version actually installed is queried from this
# container at run time (run_harness2.discover -> `docker run --rm <image> --version`) and
# disclosed as `harness_version` on the submission, so the result stays self-describing even
# though the tag is floating. Set OPENCLAW_VERSION to reproduce an older result exactly.
# node 24: openclaw 2026.7.1-2 requires Node >=22.22.3 and node:22-slim ships v22.22.2, so
# the 22 line silently produced an image whose CLI refused to start.
FROM node:24-slim

# openclaw uses CalVer (2026.6.11, 2026.7.1-2, ...), not semver — do not assume 0.x ordering.
# It is published on npm as `openclaw`; harnesses.py declares package="openclaw".
ARG OPENCLAW_VERSION=latest
RUN npm install -g "openclaw@${OPENCLAW_VERSION}"

# OpenClaw reads its OpenAI-compatible endpoint + model from ~/.openclaw/openclaw.json — NOT from
# env vars. The pod does NOT run this image as a service: per task it mounts a freshly-generated
# openclaw.json (baseUrl → the served alias) at /root/.openclaw and invokes:
#   docker run --rm --network host -v <home>:/root/.openclaw aeon-harness-openclaw \
#     agent --local --json --agent main -m "<prompt>" --model dgx/<alias>
# (see mvp/pod/adapters/openclaw.py:build_config + run_task). So there are no OPENAI_* envs here.

# GATE, not a sanity print: the build must FAIL if the CLI cannot start. Tracking `latest`
# means upstream can raise its Node floor (or anything else) under us, and a harness that
# installs but won't run scores 0 on every agentic task with no error anywhere. This one line
# is what converts that into a build failure CI catches. Never add `|| true` here.
# It also surfaces the resolved version - the string the pod discloses as harness_version.
RUN openclaw --version

ENTRYPOINT ["openclaw"]
