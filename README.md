# hermes-antigravity-native

**Provider nativo de Google Antigravity / Cloud Code Assist para Hermes Agent.**
Llamada directa in-process a la API privada de Cloud Code Assist — sin lanzar el
CLI de Antigravity (`agy`) y **sin** el monkey-patch/interceptor del plugin antiguo.

Portado del modelo de **pi-antigravity** (la extensión que Diego usa en `pi` y que
funciona de maravilla). Sustituye al plugin `hermes-antigravity-auth` (v1.8.0), que
parcheaba el runtime y rompía (le faltaba el módulo `agent.gemini_cloudcode_adapter`
y caía en la API legacy de Gemini).

## Qué hace

- Registra el provider `antigravity-native` en Hermes (`hermes model` / `/model`,
  y viaja como provider del bucle principal).
- Endpoint OpenAI-compatible en loopback (`127.0.0.1:51789/v1`), servido por un
  gateway en un hilo **dentro del proceso de Hermes** (sin subprocesos, sin CLI).
- El gateway llama directo a `https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse`
  (streaming SSE nativo), con fallbacks de endpoint, discovery de proyecto
  (`loadCodeAssist` → `gen-lang-client-*`), catálogo de modelos, ruteo de
  razonamiento (low/medium/high) y cuotas.
- Modelos: `gemini-3.8-flash-low/medium/high`, `claude-sonnet-4-6`,
  `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`.
- Reutiliza credenciales OAuth existentes **sin mutarlas**:
  1. `$HERMES_HOME/auth/antigravity_native.json` (store propio, tras un refresh)
  2. `$HERMES_HOME/antigravity-accounts.json` (store del plugin antiguo; fuente válida)
  3. `~/.gemini/antigravity-cli/antigravity-oauth-token` (token de agy)
  4. `$HERMES_HOME/auth/google_oauth.json` (último recurso)
  Los client_id/client_secret se resuelven: env → `$HERMES_HOME/antigravity-credentials.json` → default.

## Instalar / habilitar

```bash
cd /root/hermes-antigravity-provider && bash scripts/install.sh
hermes plugins enable antigravity-native
hermes plugins enable antigravity-native-cli
hermes antigravity-native doctor   # verifica auth + proyecto + catálogo + streaming
```

## Diagnóstico y estado real (verificado en vivo, 14-Oct-2026)

```
$ hermes antigravity-native doctor
[PASS] credentials      source=hermes-antigravity-accounts account=g***d@gmail.com
[PASS] catalog          6 modelos
[PASS] token refresh    access token válido (~54 min)
[PASS] project discovery  project=gen-lang-client-0222290829
[INFO] gateway          127.0.0.1:51789/v1
```

**Streaming verificado de extremo a extremo** con un token válido (el de agy):
`streamed OK, "OK"` a través del código nativo (test `probe_stream.py`).
**112 tests pytest pasan offline** en el venv de Hermes.

### Único bloqueo pendiente (acción de usuario)

Con el token del client `1052827114944` (el del plugin antiguo) el streaming da:

```
403 Cloud Code Private API has not been used in project 1052827114944 before or it is disabled
```

Ese número es la parte numérica del **OAuth client_id** del plugin antiguo; su
proyecto GCP no tiene habilitado el API. Es la causa raíz de por qué "el de
Hermes no funciona". Para desbloquearlo **en la consola de Google de Diego**:

```
https://console.developers.google.com/apis/api/cloudcode-pa.googleapis.com/overview?project=1052827114944
```

Habilitar `Cloud Code Private API` (y esperar propagación). Alternativa inmediata
sin tocar consola: que `ANTIGRAVITY_NATIVE` use el token de agy (ya autorizado,
proyecto `aicode-consumers`) — ver nota en el código de `auth.py`.

## Test

```bash
/root/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q   # → 112 passed
```