# Harness skill: OpenClaw — self-configuration

You are configuring the **OpenClaw** agent CLI to use YOU as its model. The pod runs
OpenClaw as a one-shot Docker container (`--network host`); its home directory is seeded at
`/root/.openclaw` and the pod invokes:

    openclaw agent --local --json --agent main -m "<task prompt>" --model dgx/<ALIAS>

Placeholders in this document: `<BASE_URL>` is the served OpenAI-compatible endpoint,
`<ALIAS>` is the served model name. The pod substitutes the real values before handing you
this document.

## Config surface (what you author)

One JSON file: `openclaw.json`, placed by the pod at `/root/.openclaw/openclaw.json`.
Strict JSON — no comments, no trailing commas. The provider id MUST be `dgx`, because the
pod invokes `--model dgx/<ALIAS>`. The exact structure the harness reads:

```json
{
  "models": {
    "providers": {
      "dgx": {
        "baseUrl": "<BASE_URL>",
        "apiKey": "sk-local",
        "api": "openai-completions",
        "models": [
          {"id": "<ALIAS>", "name": "<ALIAS>", "contextWindow": 131072, "maxTokens": 8192}
        ]
      }
    }
  },
  "agents": {"defaults": {"model": {"primary": "dgx/<ALIAS>"}}}
}
```

Field notes (every field above is part of the harness's real schema — emit them all):

* `models.providers.dgx.baseUrl` — the OpenAI-compatible endpoint serving you
  (PROTECTED — must be exactly `<BASE_URL>`).
* `models.providers.dgx.apiKey` — the local server does not validate it; use `sk-local`.
* `models.providers.dgx.api` — `"openai-completions"`, the OpenAI-compatible driver.
* `models.providers.dgx.models[]` — must contain an entry whose `id` (and `name`) is
  `<ALIAS>`; `contextWindow` / `maxTokens` are the serving limits (131072 / 8192).
* `agents.defaults.model.primary` — `"dgx/<ALIAS>"` (PROTECTED — the default agent must
  point at the dgx provider and the served alias).

The same directory is also OpenClaw's session store; the pod seeds a fresh one per
model-run so no state leaks between models. You author only `openclaw.json`.

## Endpoint facts

* Protocol: OpenAI-compatible chat completions (`POST <BASE_URL>/chat/completions`).
* base_url: `<BASE_URL>` — use this EXACT value.
* model alias: `<ALIAS>` — use this EXACT value.
* Auth: any bearer token is accepted; use `sk-local`.

## Output contract

Reply with ONLY the config file content inside ONE fenced code block tagged with its
filename (```openclaw.json). No other fenced blocks, no commentary outside the block.
Any URL other than the given base_url anywhere in the file, or a primary model other than
`dgx/<ALIAS>`, fails setup.
