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

## Three-engine toolbox

The install script puts all three engine CLIs and these wrappers on PATH by
default. Set `FC_APPS=film,effect,sound` on Linux/macOS or pass `-Apps
film,effect,sound` on Windows to select which engines to install. Use
`fcedit --app effect ...`, `fcedit --app sound ...`, or the `ecedit` and
`scedit` shims. An omitted recipe `"app"` means `"film"`.

Every app supports recipe `ops`:

```json
{"op":"raw","id":"engine.command","params":{}}
{"op":"tool","name":"mcp_tool_name","arguments":{}}
```

For full engine access, inspect MCP tools and call them directly:
`fcedit --app effect tools`, `fcedit --app effect tool list_effects
'{"filter":"blur"}'`, and `fcedit --app sound describe mix.insert`. `fcedit
mcp-config` prints all three stdio servers.

### EffectCraft composition recipe

Times are seconds. Named layers may be referenced as `parent` or as a matte
layer; tool calls, raw engine commands, and script strings run after layer
creation.

```json
{
  "app":"effect",
  "comp":{"name":"Title","width":1920,"height":1080,"fps":30,"duration":5,"background":"#101018"},
  "media":{"logo":"/ABSOLUTE/PATH/TO/logo.png"},
  "layers":[
    {"type":"solid","name":"background","solid":"#101018","out":5},
    {"type":"text","name":"title","text":"A NEW STORY","size":120,"fill":"#ffffff",
     "transform":{"position":[960,540],"opacity":100},
     "keys":{
       "transform/position":[{"time":0,"value":[960,620]},{"time":1,"value":[960,540]}],
       "transform/opacity":[{"time":0,"value":0},{"time":1,"value":100}]
     },
     "ease":"easyEase","expressions":{"transform/rotation":"time*2"},
     "effects":[{"effect":"Gaussian Blur","values":{"blurriness":4}}],
     "masks":[]}
  ],
  "script":"// optional JSX-style script",
  "ops":[{"op":"tool","name":"get_state","arguments":{}}],
  "preview":[{"time":1,"path":"/ABSOLUTE/PATH/TO/title-1.png"},
             {"time":3,"path":"/ABSOLUTE/PATH/TO/title-3.png"}],
  "save":"/ABSOLUTE/PATH/TO/title.ecproj",
  "render":{"path":"/ABSOLUTE/PATH/TO/title.mp4","format":"h264","start":0,"end":5}
}
```

Layer types are `solid`, `text`, `shape`, `null`, `adjustment`, `camera`,
`light`, and `footage`. Common fields include `in`, `out`, `start`, `blend`,
`parent`, `matte`, `threeD`, `transform`, `keys`, `ease`, `expressions`,
`effects` (with parameter values and keyframes), and `masks`. Shapes use
`rectangle`, `ellipse`, `polygon`, or `star`; footage `src` names an entry in
`media`. A `script` may be a JSX string or a list of strings. Rendering waits
for the output file before returning.

### SoundCraft session recipe

Times are seconds in recipes and are converted to SoundCraft time-position
objects. Plugin identifiers and parameter names come from SoundCraft's live
plugin/command discovery; pass any additional operation through `tool` or
`raw`.

```json
{
  "app":"sound",
  "session":{"name":"Mix","sample_rate":48000,"template":"blank"},
  "tracks":[
    {"name":"music","src":"/ABSOLUTE/PATH/TO/music.wav","at":0,"gain":-6,"pan":-0.2,
     "mute":false,"solo":false,
     "inserts":[{"plugin":"PLUGIN_ID","params":{}}],
     "automation":{"volume":[{"time":0,"value":-12},{"time":4,"value":0}]}}
  ],
  "markers":[{"at":4,"name":"drop"}],
  "ops":[{"op":"tool","name":"inspect_session","arguments":{"detail":"full"}}],
  "save":"/ABSOLUTE/PATH/TO/mix.scraft",
  "bounce":{"path":"/ABSOLUTE/PATH/TO/mix.wav","format":"wav","bit_depth":"24",
            "normalize":true,"start":0,"end":10}
}
```

Track fields support audio import, gain, pan, mute/solo, plugin inserts and
parameter automation. Bounce results include the engine's peak dBFS and LUFS
measurements. `frame` is not supported for the audio-only engine.

### Pipelines

An ordered pipeline keeps one live session per engine and substitutes an
earlier step's render, bounce, or export path wherever `{{step-id}}` occurs:

```json
{
  "app":"pipeline",
  "steps":[
    {"id":"title","app":"effect","comp":{"name":"Title","width":1920,"height":1080,"fps":30,"duration":5},
     "layers":[{"type":"text","name":"title","text":"TITLE","size":100}],
     "render":{"path":"/ABSOLUTE/PATH/TO/title.mp4","format":"h264"}},
    {"id":"mix","app":"sound","session":{"name":"Mix","sample_rate":48000},
     "tracks":[{"name":"music","src":"/ABSOLUTE/PATH/TO/music.wav"}],
     "bounce":{"path":"/ABSOLUTE/PATH/TO/mix.wav","format":"wav","start":0,"end":5}},
    {"id":"final","app":"film","sequence":{"width":1920,"height":1080,"fps":30},
     "media":{"title":"{{title}}","music":"{{mix}}","clip":"/ABSOLUTE/PATH/TO/clip.mp4"},
     "timeline":[{"op":"place","src":"title","at":0,"dur":5},
                 {"op":"place","src":"clip","at":0,"dur":5},
                 {"op":"audio","src":"music","at":0,"dur":5}],
     "export":{"path":"/ABSOLUTE/PATH/TO/final.mp4","format":"h264"}}
  ]
}
```

Build this with `fcedit build pipeline.json`. The same recipes can be submitted
to `POST /pipeline`.

### Multi-app HTTP and container security

`fcedit serve --apps film,effect,sound` enables app-prefixed routes such as
`GET /effect/tools`, `POST /effect/tool`, and `POST /sound/recipe`. Unprefixed
routes remain aliases for the selected default app. Engines start lazily;
`GET /health` reports which sessions have started. `POST /pipeline` runs app
steps in a consistent lock order.

The service binds to loopback by default. A non-loopback `--host` requires
`--token TOKEN` or `FCEDIT_TOKEN`, unless `--insecure-no-auth` is explicitly
given. Except for `GET /health`, requests must carry
`Authorization: Bearer TOKEN`. CORS is disabled by default; allow specific
origins with repeatable `--cors ORIGIN` or `FCEDIT_CORS`.

The Docker image listens on `0.0.0.0`, so run it with `-e FCEDIT_TOKEN=...`.
Only use `--insecure-no-auth` on an isolated, trusted network.
