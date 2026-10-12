---
name: filmcraft
description: Edit video, audio, titles and captions on any machine with the FilmCraft headless engine — recipes, JSON commands, MCP, or HTTP. Use when asked to cut, join, caption, transition, speed up, gain, overlay text, or export media files.
---

# FilmCraft agent skill

FilmCraft is a real non-linear editor (a clean-room Premiere-class NLE in pure
Rust) with a headless engine. Everything is a named command taking JSON params;
this skill is the shortest path from "edit this video" to a finished file.

## Install (once per machine)

```sh
# Linux / macOS
curl -fsSL https://raw.githubusercontent.com/jokedud/filmcraft-agent/main/agent/install.sh | sh
# Windows (PowerShell)
irm https://raw.githubusercontent.com/jokedud/filmcraft-agent/main/agent/install.ps1 | iex
```

This puts `filmcraft-cli` (the engine) and `fcedit` (the agent wrapper) on PATH.
No other dependencies; `fcedit` is a single Python 3 stdlib file.

## Three ways to drive it

1. **Recipe (preferred)** — describe the whole edit in one JSON file:

   ```sh
   fcedit build recipe.json --verbose
   ```

2. **Commands** — run engine commands one at a time or as a JSONL script:

   ```sh
   filmcraft-cli exec timeline.place '{"item":8,"track":"V1","time":0}'
   filmcraft-cli run script.jsonl --save-as project.fcproj   # persists!
   ```

   Sessions do not persist across CLI calls — pass `--project p.fcproj`
   and `--save-as p.fcproj`, or use `fcedit serve`/`mcp` for a live session.

3. **HTTP service** — for agents on other devices or web UIs:

   ```sh
   fcedit serve --port 8321        # POST /recipe, /run, /batch; GET /inspect, /commands; POST /frame
   ```

   MCP clients (Claude Code, Devin) can instead attach to `filmcraft-cli mcp`
   on stdio — tools: `command_run`, `command_batch`, `command_list`,
   `doc_inspect`, `media_import`, `render_frame`.

## Recipe reference

```jsonc
{
  "sequence": {"name": "edit", "width": 1920, "height": 1080, "fps": 30},
  "media": {"a": "/abs/path/clip.mp4", "song": "/abs/path/music.mp3"},
  "captions": {"format": "Subtitle", "style": {"size": 42, "anchor": "bottom"}},
  "timeline": [
    // media
    {"op": "place", "src": "a", "in": 2, "out": 8, "name": "intro"},   // trim 2s→8s
    {"op": "place", "src": "a", "in": 10, "dur": 4},                   // auto-appends after intro
    {"op": "audio", "src": "song", "at": 0, "track": "A2", "dur": 30}, // audio-only
    {"op": "image", "src": "logo", "at": 0, "dur": 5},
    {"op": "matte", "color": "#101018", "at": 30, "dur": 2},
    {"op": "bars", "at": 0, "dur": 1},
    // graphics & captions
    {"op": "text", "content": "TITLE", "at": 0.5, "dur": 4, "position": [960, 540],
     "size": 120, "font": "Inter", "props": {"fill": "#ffd700"}},
    {"op": "shape", "shape": "rectangle", "position": [960, 540], "size": [400, 100], "dur": 5},
    {"op": "caption", "text": "hello", "at": 1, "dur": 2.5},
    {"op": "importCaptions", "path": "/abs/subs.srt"},
    // edits on clips (by "name", "#index", or {"id": n}; "name.audio" = its audio)
    {"op": "transition", "clip": "intro", "effect": "cross_dissolve", "frames": 24, "edge": "out"},
    {"op": "transition", "clip": "intro.audio", "effect": "constant_power", "frames": 24},
    {"op": "effect", "clip": "#0", "effect": "gaussian_blur", "params": {"blurriness": 30}},
    {"op": "param", "clip": "intro", "effect": "motion", "param": "scale", "value": 120,
     "keyframe": true, "time": 1.0},                                    // keyframes!
    {"op": "speed", "clip": "intro", "speed": 200, "interpolation": "opticalFlow"},
    {"op": "gain", "clip": "intro.audio", "db": -6},
    {"op": "trim", "clip": "intro", "edge": "out", "delta": -0.5, "mode": "ripple"},
    {"op": "move", "clip": "intro", "to": 5, "track": "V2"},
    {"op": "razor", "at": 6, "track": "V1"},
    {"op": "marker", "at": 4, "name": "beat drop"},
    {"op": "preview", "at": 2, "path": "check.png"},                    // PNG of program frame
    {"op": "transcribe", "language": "en"},                             // speech→text if a model is present
    {"op": "raw", "id": "any.engine.command", "params": {}}
  ],
  "project": {"save": "edit.fcproj"},
  "export": {"path": "out.mp4", "format": "h264", "crf": 20, "burnCaptions": true,
             "captionSidecar": "srt"}
}
```

Times are seconds (floats) everywhere; the engine's own commands also take
`seconds`, `frame`, or raw `ticks` (254016000000/s).

## Discovering commands (700+)

```sh
fcedit commands blur                  # ids + param schemas
filmcraft-cli describe effects.apply  # one command's schema
fcedit inspect                        # live project/sequence as JSON
```

Frequent ids: `file.newSequence` `file.import` `timeline.place`
`timeline.trim` `timeline.move` `timeline.razor` `sequence.applyVideoTransition`
`effects.apply` `effects.setParam` `clip.speedDuration` `clip.audioGain`
`graphics.newText` `captions.add` `captions.import` `markers.add`
`file.exportMedia` `file.exportFrame` `sequence.transcribe`
`clip.automateToSequence` (auto-assembly) `edit.undo`.

## Workflow tips

- Media paths must be absolute (the engine resolves like the project does).
- Preview before/after edits with `fcedit frame --seconds T --out t.png` or the
  `preview` op — look at the frame, don't guess.
- `timeline.place` returns `{"clips": [videoId, audioId]}`; `transition`,
  `effect`, `speed`, `gain`, `trim`, `move` need those ids (or recipe names).
- Exports: `format` picks the codec (`h264` mp4, `prores` mov, `gif`, `wav`…);
  `preset` names a Media-Encoder-style preset (`export --list-presets`).
- Batch many tweaks in `POST /batch` or one `run` script — each `run` call is
  one undoable transaction per line.
- If a command errors, the session is fine: fix params and retry, or
  `{"id":"edit.undo"}`.
