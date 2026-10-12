# agent/ — FilmCraft for AI agents

Everything an AI agent (or a script, or a CI job) needs to edit video on
Linux, Windows, or macOS — no window, no ffmpeg, no GPU required.

| File | What it is |
|---|---|
| `install.sh` | One-line installer (Linux/macOS): `filmcraft-cli` + `fcedit` on PATH |
| `install.ps1` | One-line installer (Windows PowerShell) |
| `fcedit.py` | The agent tool: recipe builder + HTTP service + helpers (stdlib only) |
| `SKILL.md` | Drop-in skill file — give this to an agent and it knows the whole workflow |
| `mcp.json` | MCP client config snippet for `filmcraft-cli mcp` |
| `Dockerfile` | Container image serving `POST /recipe` on :8321 |
| `examples/` | Recipes and a raw JSONL command script to copy |

## Quick start

```sh
# 1. install (Linux/macOS)
curl -fsSL https://raw.githubusercontent.com/jokedud/filmcraft-agent/main/agent/install.sh | sh

# 2. edit the example
fcedit build agent/examples/montage-recipe.json --verbose
# → montage.mp4
```

## The moving parts

- **`filmcraft-cli`** — the headless engine. `exec`/`run`/`mcp`/`export`/
  `probe`/`render`/`commands`. Every engine command is one JSON call.
- **`fcedit`** — Python wrapper. `build` compiles a declarative recipe to live
  commands over a persistent MCP session (named clips, `.audio` refs, `#index`,
  auto-append). `serve` exposes the session over HTTP for remote agents.

See [SKILL.md](SKILL.md) for the full recipe schema and command cookbook, or
the site: https://jokedud.github.io/filmcraft-agent/
