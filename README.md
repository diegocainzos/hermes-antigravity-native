# hermes-antigravity-native

**Provider nativo de Google Antigravity / Cloud Code Assist para Hermes Agent.**
Llamada directa in-process a la API privada de Cloud Code Assist: sin lanzar el CLI
`agy` como subproceso y **sin** el monkey-patch/interceptor del plugin antiguo
`hermes-antigravity-auth` (v1.8.0). Portado de **pi-antigravity**.

## Cómo funciona

```
Hermes (agent loop, cliente OpenAI)
  └─ provider "antigravity-native" (ProviderProfile, plugin model-provider)
       └─ gateway OpenAI-compatible en un hilo del propio proceso: http://127.0.0.1:51789/v1
            └─ httpx → https://daily-cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse
               (+ fallbacks sandbox/prod, discovery de proyecto, catálogo, routing de effort, cuotas)
```

- El gateway escucha **solo en loopback** y exige un bearer aleatorio por usuario
  (`$HERMES_HOME/auth/antigravity_native_gateway.key`, 0600). El plugin lo exporta como
  `ANTIGRAVITY_NATIVE_API_KEY` al registrarse, así que Hermes lo usa sin tocar `.env`.
  Otros procesos/usuarios locales no pueden gastar tu cuota por ese puerto.
- Modelos: `gemini-3.8-flash-low|medium|high`, `claude-sonnet-4-6`,
  `claude-opus-4-6-thinking`, `gpt-oss-120b-medium` (además de los ids públicos
  `gemini-3.8-flash` + `reasoning_effort` low/medium/high/xhigh, como el `ANTIGRAVITY_ROUTING` de pi).

## Credenciales (sin pasos manuales recurrentes)

Gana la primera fuente usable (forzable con `ANTIGRAVITY_NATIVE_SOURCE=<nombre>`):

| # | Fuente (`source`) | Ruta | Cliente OAuth con el que se refresca |
|---|---|---|---|
| 1 | `env` | `ANTIGRAVITY_REFRESH_TOKEN` | `ANTIGRAVITY_CLIENT_ID` o el público |
| 2 | `native` | `$HERMES_HOME/auth/antigravity_native.json` — **solo** lo escribe `hermes antigravity-native login` | el guardado en el store (`clientId`) |
| 3 | `agy-cli-token` | `~/.gemini/antigravity-cli/antigravity-oauth-token` (solo lectura) | el del `id_token` (`aud`): el cliente público de Antigravity `1071006060591…` |
| 4 | `hermes-antigravity-accounts` | `$HERMES_HOME/antigravity-accounts.json` (plugin viejo, solo lectura) | `1052827114944…` de `antigravity-credentials.json` |
| 5 | `hermes-google-oauth` | `$HERMES_HOME/auth/google_oauth.json` (solo lectura) | el público |

Datos verificados en vivo (14-oct-2026):

- El token de agy lo emitió el **cliente público de Antigravity** (el mismo que usa pi)
  y **se refresca in-process** con él → el access token caducado ya no requiere ejecutar `agy`.
  (La prueba anterior que daba `invalid_client` usaba un secret mal copiado.)
- El cliente `1052827114944…` del plugin viejo refresca, pero su proyecto GCP tiene la
  Cloud Code Private API deshabilitada → **403** en inferencia. Por eso esa fuente va detrás
  de agy, y el doctor avisa (WARN) si es la que se usaría.
- `login` ahora usa el cliente público (antes cogía por error el `1052…` del plugin viejo).

Los tokens refrescados de fuentes ajenas **nunca** se copian al store propio: van a una
caché indexada por huella del refresh token (`antigravity_native_cache.json`, 0600). Así
una copia vieja no puede tapar a una fuente mejor (el bug que re-rompía el streaming).
Los stores propios anteriores a v2 (sin `clientId`) se ignoran; puedes borrarlos.

Si el access token caduca y el refresh falla, el error es accionable (fuente + causa +
`hermes antigravity-native login` / `agy`). Si Google devuelve un 401 a mitad de sesión,
el gateway fuerza un refresh y reintenta una vez antes de rendirse.

## Instalar / habilitar

```bash
cd /root/hermes-antigravity-provider && bash scripts/install.sh
hermes plugins enable antigravity-native          # provider (modelo)
hermes plugins enable antigravity-native-cli      # comandos `hermes antigravity-native ...`
hermes antigravity-native doctor                  # todo PASS = listo
```

`install.sh` instala el paquete editable con `uv pip` en el venv de Hermes y copia los
wrappers. El wrapper del provider se instala en
`~/.hermes/plugins/model-providers/0-antigravity-native` **a propósito**: Hermes importa los
directorios de providers por orden alfabético, y el plugin viejo `antigravity` importa
`hermes_cli.auth` al cargarse, lo que congela el registro de providers. Cargando antes,
`--provider antigravity-native` resuelve sin parchear Hermes (antes daba `Unknown provider`).

Login propio (opcional; solo si no tienes token de agy o quieres independencia de él):

```bash
hermes antigravity-native login   # abre la URL; en un servidor headless pega la URL de callback
```

## Uso

```bash
hermes -z "hola" --provider antigravity-native -m gemini-3.8-flash-low
hermes chat --provider antigravity-native -m claude-sonnet-4-6
hermes model          # elige "Antigravity (native)" en el selector → queda como provider principal
```

Dentro de una sesión con `antigravity-native` activo, `/model gemini-3.8-flash-high` cambia
de modelo. **Limitación del core de Hermes:** `/model <m> --provider antigravity-native` desde
*otro* provider responde `Unknown provider`, porque `resolve_provider_full` no consulta los
providers de plugins (le pasa igual al `google-gemini-cli` incluido en Hermes). Usa
`hermes model` o arranca con `--provider`.

Comandos: `hermes antigravity-native status|doctor|models [--live]|usage|login|serve`.

## Known limitation: Hermes identity is rejected upstream

Verificado en vivo el 14-oct-2026 y de forma determinista (3/3, Gemini y Claude): el backend
de Antigravity responde **`429 RESOURCE_EXHAUSTED`** a cualquier petición cuyo system
prompt contenga la frase de identidad por defecto de Hermes
(`You are Hermes Agent, an intelligent AI assistant created by Nous Research`). La misma
cuenta, proyecto, modelo y las 17 tools de Hermes funcionan sin esa frase, y la cuota está
al ~100%. No es un rate limit: parece un **bloqueo deliberado de Google** a este agente
de terceros.

Consecuencia: el pipeline nativo funciona de extremo a extremo (doctor todo PASS, incluida la
llamada SSE por el gateway que usa Hermes, y llamadas a tools), pero un turno normal de Hermes
con su prompt por defecto recibe ese 429. El plugin **no** reescribe ni oculta el system prompt
para esquivarlo, porque eso sería evadir un control de acceso del proveedor (y probablemente
sus términos de uso). Solo lo diagnostica: el error dice explícitamente que no es cuota y
remite aquí. Qué hacer con esto es decisión tuya.

## Coexistencia con el plugin viejo (`antigravity` / `antigravity-cli`)

- El plugin viejo reemplaza `httpx.Client.send` en todo el proceso y reescribe cualquier
  petición a `cloudcode-pa` (quita nuestro `Authorization` y pone su cuenta, que hoy está en
  cooldown → 401). Las peticiones nativas llevan sus propios marcadores de "ya procesado"
  (`antigravity_request_hook_processed` / `antigravity_response_hook_processed`), así que las
  deja intactas. Sin parches; si el plugin viejo no existe, no hace nada.
- Para retirarlo cuando quieras (el nativo ya no depende de él):

  ```bash
  hermes plugins disable antigravity-cli
  hermes plugins disable antigravity
  # los model-providers se importan aunque estén "disabled": aparta su directorio
  mv ~/.hermes/plugins/model-providers/antigravity ~/.hermes/antigravity-provider.disabled
  ```

  Y si `config.yaml` sigue con `model.base_url: cloudcode-pa://google`, elige otro provider con
  `hermes model` (ese esquema solo lo entiende el plugin viejo).

## Variables de entorno

`ANTIGRAVITY_NATIVE_SOURCE` (forzar fuente), `ANTIGRAVITY_REFRESH_TOKEN`,
`ANTIGRAVITY_CLIENT_ID`/`_SECRET`, `ANTIGRAVITY_PROJECT_ID`, `ANTIGRAVITY_BASE_URL`
(solo `*.googleapis.com`), `ANTIGRAVITY_NATIVE_PORT`, `ANTIGRAVITY_NATIVE_API_KEY`
(bearer del gateway), `ANTIGRAVITY_AGY_TOKEN_FILE`, `ANTIGRAVITY_RUNTIME_MODEL`.

## Tests

```bash
/root/.hermes/hermes-agent/venv/bin/python -m pytest tests/ -q   # 145 passed, offline
```

Incluyen: orden de fuentes y cliente por fuente, caché por huella, fallback de refresh y
mensajes de expiry, reintento ante 401, bearer del gateway, marcadores anti-interceptor,
diagnóstico del 429 de identidad y un test de integración (subproceso con el core de Hermes)
que reproduce el `Unknown provider` con el nombre de directorio viejo y lo verifica resuelto
con `0-antigravity-native`.
