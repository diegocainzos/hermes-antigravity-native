# Project: hermes-antigravity-provider

Build a **native Hermes Agent model-provider plugin** that lets Hermes use Google
**Antigravity / Cloud Code Assist** models by making a **direct in-process API call**
(a "quick call") to the Cloud Code Assist private API — **never shelling out to the
external Antigravity CLI (`agy`)**. It is modeled on the `pi-antigravity` extension
(which works flawlessly) and is meant to become the clean, working Antigravity
integration for Hermes, replacing the current broken monkey-patch plugin.

You are working in a fresh, independent git repo. **Do NOT modify the Hermes core
repo** (`/root/.hermes/hermes-agent/`). Everything you build lives in this repo and
gets installed as a standalone Hermes plugin.

## The reference that works PERFECTLY (your main porting target)

**pi-antigravity** extension (TypeScript) — fully installed on disk:

```
/root/.pi/agent/npm/node_modules/pi-antigravity/
  src/index.ts                 # entrypoint: registers provider (PROVIDER_ID="antigravity"), commands, oauth
  src/auth/oauth.ts            # Google OAuth2: AUTH_URL=accounts.google.com/o/oauth2/v2/auth,
                               #   TOKEN_URL=oauth2.googleapis.com/token, REDIRECT_URI=http://localhost:51121/oauth-callback,
                               #   scopes: aicode, cloud-platform, userinfo.email/userinfo.profile, cclog, experimentsandconfigs
  src/client/client.ts         # endpoints: DEFAULT_ENDPOINT=https://daily-cloudcode-pa.googleapis.com
                               #   (sandbox + prod fallbacks), project discovery
                               #   (listCloudAICompanionProjects), model discovery, headers, project cache
  src/stream/stream.ts         # native SSE streaming (streamGenerateContent-style) — largest file, study carefully
  src/models/models.ts         # ANTIGRAVITY_ROUTING map: public model id -> backend request id per thinking effort
  src/models/grouping.ts       # catalog / grouping helpers
  src/models/discovery.ts      # model catalog refresh
  src/usage/usage.ts           # shared quota pools (Gemini vs Claude+GPT), 5h + weekly
  src/diagnostics/diagnostics.ts
  src/image/image.ts           # image generation (optional for this port)
  src/utils/http.ts            # HTTP helpers (fetch w/ timeout, retry)
  src/types/types.ts, enums.ts
```

Read `src/index.ts` first to see exactly how it registers a provider + oauth +
commands. Port its behavior to Python.

## The current BROKEN Hermes plugin (what you are replacing)

Pip package `hermes-antigravity-auth` v1.8.0 installed at:

```
/root/.hermes/hermes-agent/venv/lib/python3.12/site-packages/antigravity_auth/
```

Thin file-plugin wrappers that delegate to it:

```
/root/.hermes/plugins/antigravity-cli/{plugin.yaml,__init__.py}         # kind: standalone
/root/.hermes/plugins/model-providers/antigravity/{plugin.yaml,__init__.py}  # kind: model-provider
```

`antigravity_auth/plugin_contract.py` defines the file-plugin wrapper contract:
`load_cli_register()` returns `antigravity_auth.hermes_plugin.register`; the
model-provider wrapper exposes `antigravity_auth.hermes_provider_plugin`.

**Why it's broken ("es una guarrada"):** it monkey-patches the running Hermes
runtime — installs an HTTP request **interceptor** (`interceptor.py`,
`transform/`), patches the model picker, auth registry, provider grouping aliases,
runtime provider factory hooks, and Gemini native adapter. `hermes antigravity
doctor` reports an INFO that a core module `agent.gemini_cloudcode_adapter` is
missing (`No module named 'agent.gemini_cloudcode_adapter'`). The user wants a
clean native provider instead of fragile monkey-patching.

Study `plugin_contract.py` + `hermes_provider_plugin.py` ONLY to learn the Hermes
plugin contract. Do NOT copy the interceptor/monkey-patch design.

## Hermes core architecture (read-only reference — DO NOT EDIT)

```
/root/.hermes/hermes-agent/
  AGENTS.md                     # READ THE RULES. Plugins are standalone dirs/pip pkgs; never touch core from a plugin.
  agent/gemini_native_adapter.py  # pattern for an OpenAI-compatible facade over a native API (uses httpx, streaming)
  agent/gemini_schema.py
  agent/agent_runtime_helpers.py
  hermes_cli/plugins.py         # how Hermes discovers/enables file plugins (~/.hermes/plugins)
  hermes_cli/config.py          # DEFAULT_CONFIG: model.provider/base_url/api_key, providers.*
  run_agent.py                  # agent loop (OpenAI-format messages + tool schemas)
```

Hermes Python venv + runtime:

```
Python: /root/.hermes/hermes-agent/venv/bin/python
Hermes CLI: /root/.hermes/bin/hermes  (symlink to venv bin/hermes)
Config: /root/.hermes/config.yaml
Env:    /root/.hermes/.env
```

Note the current config routes through Antigravity:
`model.base_url: cloudcode-pa://google` (scheme handled by the existing plugin).

## Credentials available to REUSE (secrets — never commit them)

- `/root/.hermes/auth/google_oauth.json` → JSON keys `access`, `refresh`,
  `email` (`gaiosoxd@gmail.com`), `expires` (epoch ms). This **refresh token is
  valid and functional** (the current plugin refreshed it successfully:
  `hermes antigravity doctor` PASS "active token refresh"). Reuse it for OAuth.
- `/root/.gemini/antigravity-cli/antigravity-oauth-token` → the Antigravity OAuth
  token that the `agy` CLI uses for native calls to `daily-cloudcode-pa.googleapis.com`.
  Read it only at runtime; never write it into source files.

Design the provider to obtain credentials at runtime (env var / `~/.hermes/auth/`
/ antigravity credential store), with the Google OAuth refresh flow ported from
pi-antigravity's `oauth.ts`.

## Models to expose (current `agy models` output)

```
gemini-3.8-flash-high       Gemini 3.8 Flash (High)
gemini-3.8-flash-medium     Gemini 3.8 Flash (Medium)
gemini-3.8-flash-low        Gemini 3.8 Flash (Low)
claude-sonnet-4-6           Claude Sonnet 4.6 (Thinking)
claude-opus-4-6-thinking    Claude Opus 4.6 (Thinking)
gpt-oss-120b-medium         GPT-OSS 120B (Medium)
```

Mirror pi-antigravity's `ANTIGRAVITY_ROUTING` approach: expose the public model id
and map optional thinking-effort selection (low/medium/high/xhigh) to the backend
request id the API expects. Prefer an in-process model catalog that matches what
`agy models` reports, with a discovery/refresh path like `fetchAvailableModels`.

## Design constraints (non-negotiable)

1. **Native, in-process, direct API call** to the Cloud Code Assist private API.
   Streaming (SSE) must work. **No** launching `agy` or any external CLI as a subprocess.
2. Registers as a proper **Hermes model-provider** so it shows up in
   `hermes model` / `/model` and can serve as the main agent-loop provider.
   Design to **coexist** with the existing `antigravity` plugin (distinct provider
   id, e.g. `antigravity-native`, unless you find a clean takeover) — do not break
   the user's current setup. Provide clear flip/enable instructions.
3. **Port pi-antigravity's client behavior**: project discovery
   (`listCloudAICompanionProjects` → projectId), model discovery, auth headers,
   native SSE parsing, quota-pool usage if reasonable.
4. OAuth: port pi-antigravity's flow; **reuse existing stored refresh token** so
   the user does not have to log in again if possible. On a headless server the
   OAuth callback listener binds localhost; support pasting a callback URL
   (pi-antigravity supports this for remote/headless).
5. **Tests**: ship pytest tests. They must run against Hermes' Python
   (`/root/.hermes/hermes-agent/venv/bin/python -m pytest ...`). Use temp
   `HERMES_HOME` and mock network/HTTP for the transport layer; write pure-logic
   unit tests (routing map, URL/project parsing, token/refresh helpers, SSE chunk
   parser, redaction) that run offline. Follow Hermes' testing convention
   (tests auto-redirect HERMES_HOME). Make offline tests pass.
6. **No secrets in the repo** (add `.gitignore`). No modification of the Hermes
   core repo.

## Deliverables

1. The standalone plugin (a Python package installable into Hermes' venv, plus
   file-plugin wrappers for `~/.hermes/plugins/` — model the `plugin.yaml`
   `kind: model-provider` + thin `__init__.py` delegate pattern from the existing
   plugin, but wrapping YOUR native package, not the interceptor).
2. Install + enable steps (use `uv pip` into Hermes' python
   `/root/.hermes/hermes-agent/venv/bin/python`, consistent with how
   hermes-antigravity-auth is installed; `hermes plugins list` to confirm).
3. A `doctor`/`check` entrypoint (port pi-antigravity's diagnostics) and/or
   Hermes CLI command to verify auth + model connectivity with secrets redacted.
4. A README explaining what it is, how it differs from the old plugin, install,
   enable, and usage (`hermes model`, `/model`, e.g. passing
   `--provider <your_id> --model gemini-3.8-flash-high`).
5. Offline tests passing. A final summary of: what is where, how to enable, the
   exact test command+result, and any open questions/risks.

Keep the code idiomatic for Hermes (httpx for HTTP, async where the codebase is
async, `get_hermes_home()` for paths, secrets only from env/auth store).