# Harness skill: Hermes Agent — self-configuration

You are configuring the **Hermes agent harness** (NousResearch/hermes-agent) to use YOU as
its model. The pod runs Hermes as a one-shot Docker container (`--network host`, task files
at `/work`); the container entrypoint is `python /app/run_agent.py` and its endpoint
configuration is passed ENTIRELY through command-line flags.

Placeholders in this document: `<BASE_URL>` is the served OpenAI-compatible endpoint,
`<ALIAS>` is the served model name. The pod substitutes the real values before handing you
this document.

## Config surface (what you author)

Author a flag file named `hermes.flags`: plain text, exactly one `--flag=value` line per
flag, nothing else. The pod parses it, whitelists the flags, and rebuilds the container
argv from it (your text is never executed and never passed through a shell).

Allowed flags — these are the ONLY flags you may emit:

| flag | required | value |
|---|---|---|
| `--base_url` | yes | the OpenAI-compatible endpoint serving you: `<BASE_URL>` (PROTECTED — must be exactly this value) |
| `--model` | yes | the served model alias: `<ALIAS>` (PROTECTED — must be exactly this value) |
| `--api_key` | no (defaults to `sk-local`) | any bearer token; the local server does not validate it — use `sk-local` |
| `--max_turns` | no (defaults to 8) | integer 1–32; the pod uses 8 |

Do NOT emit any other flag. In particular `--query`, `--save_sample` and
`--disabled_toolsets` are owned by the pod and appended per task — a flag file containing
them (or any unknown flag) fails setup.

## What the pod stages for you (not yours to author)

* `/root/.hermes/config.yaml` containing `model:\n  context_length: 65536`. This is an
  honest serve-window disclosure workaround: the bench serve caps max-model-len at 32K for
  GPU memory while the model's true window is larger, and Hermes refuses models reporting
  a window under 64K. The pod always stages this file itself.
* Per-task flags: `--query=<task prompt>` and `--save_sample` (Hermes then writes the
  ShareGPT transcript `sample_<uuid>.json` into `/work`).

## Endpoint facts

* Protocol: OpenAI-compatible chat completions (`POST <BASE_URL>/chat/completions`).
* base_url: `<BASE_URL>` — use this EXACT value.
* model alias: `<ALIAS>` — use this EXACT value.
* Auth: any bearer token is accepted; use `sk-local`.

## Output contract

Reply with ONLY the flag file content inside ONE fenced code block tagged with its
filename:

```hermes.flags
--base_url=<BASE_URL>
--api_key=sk-local
--model=<ALIAS>
--max_turns=8
```

No other fenced blocks, no commentary outside the block. Any URL other than the given
base_url, or any model name other than the given alias, fails setup.
