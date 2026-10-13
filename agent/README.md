# Multi-Craft for AI agents

One stdlib-only Python agent layer drives three storytold engines headlessly on
Linux, Windows, and macOS:

| App | CLI | Recipe type | Shortcut |
|---|---|---|---|
| FilmCraft | `filmcraft-cli` | video edits, titles, captions, export | `fcedit` |
| EffectCraft | `effectcraft-cli` | compositions, layers, animation, effects, render | `ecedit` |
| SoundCraft | `soundcraft-cli` | sessions, tracks, mix, automation, bounce | `scedit` |

`fcedit` also provides full MCP tool passthrough, a persistent multi-app HTTP
service, and ordered pipelines that pass one engine's output into another.

## Install

Install all engines by default:

```sh
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/jokedud/filmcraft-agent/main/agent/install.sh | sh

# Windows PowerShell
irm https://raw.githubusercontent.com/jokedud/filmcraft-agent/main/agent/install.ps1 | iex
```

Select a subset with `FC_APPS=film,effect,sound` (comma-separated; default is
all three), or on Windows use `-Apps film,effect,sound`. The installers fetch
this distribution's release assets first, then fall back to the corresponding
upstream release. `fcedit.py` requires Python 3 and no third-party modules.

## Build recipes

```sh
fcedit build agent/examples/montage-recipe.json
ecedit build agent/examples/vfx-title-recipe.json
scedit build agent/examples/audio-mix-recipe.json
fcedit build agent/examples/pipeline-recipe.json
```

Set `"app":"film"|"effect"|"sound"` at the top of a recipe; omitted `app`
defaults to FilmCraft. Each recipe accepts `ops` with `raw` engine commands and
`tool` MCP calls. `pipeline` recipes run ordered app recipes and replace
`{{step-id}}` paths in later inputs with earlier render, bounce, or export
outputs.

The effect recipe supports comps, media import, solid/text/shape/null/
adjustment/camera/light/footage layers, timing, transforms, keyframes,
expressions, effects, masks, JSX-style scripts, previews, project save and
blocking render. The sound recipe supports session setup, tracks, audio import,
gain, pan, mute/solo, inserts, automation, markers, session save and bounce
(including peak and loudness results). For unsupported specialized operations,
call any MCP tool directly:

```sh
fcedit --app effect tools
fcedit --app effect tool list_effects '{"filter":"blur"}'
fcedit --app sound describe mix.insert
fcedit --app film tool command_run '{"id":"timeline.place","params":{}}'
```

## HTTP service and security

```sh
fcedit serve --apps film,effect,sound --port 8321
curl http://127.0.0.1:8321/health
curl http://127.0.0.1:8321/effect/tools
```

App-prefixed routes include `/run`, `/exec`, `/batch`, `/inspect`, `/commands`,
`/frame`, `/recipe`, `/tools`, `/tool`, and `/describe`; unprefixed routes alias
the selected default app. `POST /pipeline` accepts a pipeline recipe. Sessions
start lazily, so an unavailable CLI affects only its own app routes.

Only loopback listeners are allowed without authentication. For a non-loopback
listener, set `FCEDIT_TOKEN` or pass `--token`; requests other than `GET
/health` must send `Authorization: Bearer <token>`. Browser origins are denied
by default; add specific trusted origins with repeatable `--cors ORIGIN` or
`FCEDIT_CORS`. Do not expose an unauthenticated service to an untrusted network.

For Docker, supply a token because the container listens on `0.0.0.0`:

```sh
docker build -f agent/Dockerfile -t fcedit .
docker run -p 8321:8321 -e FCEDIT_TOKEN=replace-me -v "$PWD/media:/media" fcedit
```

## Files

| File | Purpose |
|---|---|
| `fcedit.py` | MCP sessions, engine adapters, recipes, pipeline, HTTP server |
| `install.sh`, `install.ps1` | Cross-platform multi-engine installers |
| `mcp.json` | MCP config for FilmCraft, EffectCraft, SoundCraft |
| `SKILL.md` | Detailed recipe schemas, workflow and tool reference |
| `Dockerfile` | Multi-engine image serving the authenticated HTTP API |
| `examples/` | Film, VFX title, audio mix, pipeline and command examples |
