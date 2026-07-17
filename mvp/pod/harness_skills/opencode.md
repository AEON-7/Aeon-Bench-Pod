# Harness skill: OpenCode — self-configuration

You are configuring the **OpenCode** CLI (sst/opencode) to use YOU as its model. The pod
runs OpenCode as a one-shot Docker container (`--network host`) with the task workdir at
`/work` (also the cwd) and invokes:

    opencode run --format json --auto -m dgx/<ALIAS> "<task prompt>"

Placeholders in this document: `<BASE_URL>` is the served OpenAI-compatible endpoint,
`<ALIAS>` is the served model name. The pod substitutes the real values before handing you
this document.

## Config surface (what you author)

One JSON file: `opencode.json`. The CLI reads it from its cwd (`/work`). Strict JSON — no
comments, no trailing commas. The provider id MUST be `dgx`, because the pod invokes
`-m dgx/<ALIAS>`. The exact structure the harness reads:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "dgx": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "DGX",
      "options": {"baseURL": "<BASE_URL>", "apiKey": "sk-local"},
      "models": {"<ALIAS>": {"name": "<ALIAS>", "tool_call": true}}
    }
  },
  "model": "dgx/<ALIAS>"
}
```

Field notes (every field above is part of the harness's real schema — emit them all):

* `$schema` — optional; if present it must be exactly `https://opencode.ai/config.json`
  (the only URL other than the endpoint that is permitted in the file).
* `provider.dgx.npm` — `"@ai-sdk/openai-compatible"`, the provider implementation package
  the CLI loads for a custom OpenAI-compatible endpoint.
* `provider.dgx.name` — display name; the pod uses `"DGX"`.
* `provider.dgx.options.baseURL` — the OpenAI-compatible endpoint serving you
  (PROTECTED — must be exactly `<BASE_URL>`).
* `provider.dgx.options.apiKey` — the local server does not validate it; use `sk-local`.
* `provider.dgx.models` — an object keyed by model id; it must contain the key `<ALIAS>`
  with `{"name": "<ALIAS>", "tool_call": true}` (tool_call enables function calling).
* `model` — the default model, `"dgx/<ALIAS>"` (PROTECTED).

## Endpoint facts

* Protocol: OpenAI-compatible chat completions (`POST <BASE_URL>/chat/completions`).
* base_url: `<BASE_URL>` — use this EXACT value.
* model alias: `<ALIAS>` — use this EXACT value.
* Auth: any bearer token is accepted; use `sk-local`.

## Output contract

Reply with ONLY the config file content inside ONE fenced code block tagged with its
filename (```opencode.json). No other fenced blocks, no commentary outside the block.
Any URL other than the given base_url (except the `$schema` value above), or a default
model other than `dgx/<ALIAS>`, fails setup.
