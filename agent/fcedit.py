#!/usr/bin/env python3
"""fcedit — FilmCraft for AI agents.

One file, stdlib only. Drives a headless FilmCraft engine session through
`filmcraft-cli mcp` (MCP over stdio) so any agent on Linux, Windows or macOS
can edit video with plain JSON.

  fcedit build recipe.json           declarative recipe -> exported video
  fcedit serve [--port 8321]         persistent session as an HTTP service
  fcedit exec <id> '<json params>'   one engine command, result as JSON
  fcedit commands [filter]           list engine commands
  fcedit inspect                     project + sequence as JSON
  fcedit frame --seconds 2 --out f.png   render one program frame
  fcedit mcp-config                  print an MCP client config snippet

Environment: FILMCRAFT_CLI = path to filmcraft-cli when it is not on PATH.
"""

import argparse
import base64
import contextlib
import hmac
import ipaddress
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TICKS = 254_016_000_000  # FilmCraft time base (ticks per second)


def ticks(seconds):
    return int(round(float(seconds) * TICKS))


_OUTPUT_PATH_FIELDS = {
    "file.exportMedia": ("path",),
    "file.exportFrame": ("path",),
    "file.saveAs": ("path",),
    "project.save": ("path",),
    "renderQueue.add": ("output", "path"),
    "renderQueue.setOutput": ("output", "path"),
    "save_project": ("path",),
    "save_session": ("path",),
    "bounce_mix": ("path",),
    "render_frame": ("path",),
}


def _absolute_output_path(path):
    if isinstance(path, str) and path and not ("{{" in path and "}}" in path):
        return os.path.abspath(path)
    return path


def _normalize_output_args(command, args):
    normalized = dict(args or {})
    for field in _OUTPUT_PATH_FIELDS.get(command, ()):
        if field in normalized:
            normalized[field] = _absolute_output_path(normalized[field])
    return normalized


class FcError(Exception):
    pass


APP_CONFIG = {
    "film": ("FILMCRAFT_CLI", "filmcraft-cli"),
    "effect": ("EFFECTCRAFT_CLI", "effectcraft-cli"),
    "sound": ("SOUNDCRAFT_CLI", "soundcraft-cli"),
}


def choose_app(cli=None, app=None):
    if app:
        if app not in APP_CONFIG:
            raise FcError(f"unknown app {app!r} (choose film, effect, or sound)")
        return app
    configured = os.environ.get("FCEDIT_APP")
    if configured:
        if configured not in APP_CONFIG:
            raise FcError(f"FCEDIT_APP must be film, effect, or sound, got {configured!r}")
        return configured
    basename = os.path.basename(cli or "").lower()
    if "effectcraft" in basename:
        return "effect"
    if "soundcraft" in basename:
        return "sound"
    return "film"


class FcSession:
    """A live headless engine session behind the MCP stdio server.

    Commands are serialized: one request in flight at a time.
    """

    def __init__(self, app=None, cli=None, mcp_args=None, cwd=None):
        self.app = choose_app(cli, app)
        env_name, binary = APP_CONFIG[self.app]
        self.cli = cli or os.environ.get(env_name) or binary
        args = [self.cli, "mcp"] + list(mcp_args or [])
        try:
            self.proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=cwd,
            )
        except FileNotFoundError:
            raise FcError(
                f"{binary} not found ({self.cli!r}). Install it (agent/install.sh) "
                f"or set {env_name} to the binary path."
            )
        self._id = 0
        self._lock = threading.Lock()
        self._init()

    # -- MCP plumbing -------------------------------------------------------

    def _send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _recv(self, want_id):
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise FcError("filmcraft-cli mcp exited")
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # stray output is not a protocol message
            if msg.get("id") == want_id:
                return msg
            # notifications (progress, etc.) and unrelated ids are skipped

    def _rpc(self, method, params=None):
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
            reply = self._recv(rid)
        if "error" in reply:
            e = reply["error"]
            raise FcError(f"{method}: {e.get('message', e)}")
        return reply.get("result", {})

    def _init(self):
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "fcedit", "version": "0.1.0"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

    def tool(self, name, arguments=None):
        result = self._rpc("tools/call", {"name": name, "arguments": arguments or {}})
        if result.get("isError"):
            text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
            raise FcError(text.strip() or f"{name} failed")
        content = result.get("content", [])
        images = [{"mimeType": c.get("mimeType", "image/png"), "data": c.get("data", "")}
                  for c in content if isinstance(c, dict) and c.get("type") == "image"]
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text", "")
                try:
                    value = json.loads(text)
                except json.JSONDecodeError:
                    value = text
                if images:
                    if isinstance(value, dict):
                        value["images"] = images
                    else:
                        value = {"result": value, "images": images}
                return value
        if images:
            return {"images": images}
        return result

    # -- convenience wrappers -------------------------------------------------

    def run(self, command_id, params=None):
        if self.app == "film":
            return self.tool("command_run", {"id": command_id, "params": params or {}})
        if self.app == "effect":
            return self.tool("execute_command", {"command": command_id, "params": params or {}})
        return self.tool("execute", {"command": command_id, "params": params or {}})

    def batch(self, steps, stop_on_error=True):
        if self.app == "film":
            return self.tool("command_batch", {"steps": steps, "stop_on_error": stop_on_error})
        if self.app == "effect":
            converted = [{"command": x.get("id"), "params": x.get("params") or {}} for x in steps]
            return self.tool("batch", {"steps": converted, "atomic": stop_on_error})
        calls = [{"command": x.get("command", x.get("id")), "params": x.get("params") or {}}
                 for x in steps]
        return self.tool("batch", {"calls": calls, "keep_going": not stop_on_error})

    def inspect(self):
        if self.app == "film":
            return self.tool("doc_inspect")
        if self.app == "effect":
            return self.tool("get_project")
        return self.tool("inspect_session", {"detail": "full"})

    def commands(self, filter_text=None):
        args = {"filter": filter_text} if filter_text else {}
        return self.tool({"film": "command_list", "effect": "list_commands",
                          "sound": "list_commands"}[self.app], args)

    def tools(self):
        return self._rpc("tools/list").get("tools", [])

    def describe(self, command):
        if self.app == "effect":
            return self.tool("describe_command", {"command": command})
        if self.app == "sound":
            return self.tool("list_commands", {"filter": command, "schemas": True})
        proc = subprocess.run([self.cli, "describe", command], capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
        if proc.returncode:
            raise FcError(proc.stderr.strip() or proc.stdout.strip())
        return proc.stdout.strip()

    def frame(self, seconds=0.0, scale=None):
        if self.app == "sound":
            raise FcError("not supported")
        if self.app == "effect":
            args = {"time": seconds}
            if scale:
                args["max_side"] = int(1920 / scale) if scale < 1 else int(scale)
            result = self.tool("render_preview", args)
            images = result.get("images") if isinstance(result, dict) else None
            if images:
                return base64.b64decode(images[0]["data"])
            raise FcError("render_preview returned no image")
        args = {"seconds": seconds}
        if scale:
            args["scale"] = scale
        result = self._rpc("tools/call", {"name": "render_frame", "arguments": args})
        if result.get("isError"):
            text = "".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict))
            raise FcError(text.strip() or "render_frame failed")
        for c in result.get("content", []):
            if isinstance(c, dict) and c.get("type") == "image":
                return base64.b64decode(c.get("data", ""))
            if isinstance(c, dict) and c.get("type") == "text":
                try:
                    v = json.loads(c.get("text", "{}"))
                except json.JSONDecodeError:
                    continue
                path = v.get("path") if isinstance(v, dict) else None
                if path and os.path.exists(path):
                    with open(path, "rb") as f:
                        return f.read()
        raise FcError("render_frame returned no image")

    def close(self):
        try:
            if self.proc.poll() is None:
                self.proc.terminate()
                self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


# ---------------------------------------------------------------------------
# Recipe engine


class RecipeError(FcError):
    pass


def _need(op, key):
    if key not in op:
        raise RecipeError(f"{op.get('op', '?')}: missing `{key}`")
    return op[key]


class RecipeBuilder:
    """Compiles a declarative recipe into live engine commands.

    Clip references: a `place`-family op may carry `"name": "x"`; later ops use
    `"clip": "x"` (video clip), `"clip": "x.audio"` (its linked audio clip) or
    `"clip": "#3"` (the Nth place/text/shape op, 0-based). A bare integer is an
    engine clip id. `sequence.inspect` ids are printed in the build log.
    """

    def __init__(self, recipe, sess, log=None):
        self.r = recipe
        self.s = sess
        self.items = {}   # media name -> project item id
        self.vclips = {}  # name -> video/timeline clip id
        self.aclips = {}  # name -> audio clip id
        self.order = []   # placed clip ids in op order
        self.cursors = {}  # track name -> next free tick on it
        self.caption_track = None
        self.log = log if log is not None else (lambda m: None)

    # -- helpers -------------------------------------------------------------

    def _at(self, track, op):
        """Timeline time for an op: explicit `at`/`time` (seconds) or the
        running cursor for that track (auto-append)."""
        if "at" in op:
            return ticks(op["at"])
        if "time" in op:
            return ticks(op["time"])
        return self.cursors.get(track, 0)

    def _advance(self, track, start_ticks, dur_ticks):
        self.cursors[track] = max(self.cursors.get(track, 0), start_ticks + dur_ticks)

    def _item(self, src):
        if isinstance(src, (int, float)):
            return int(src)
        if src not in self.items:
            raise RecipeError(f"unknown media `{src}` (declare it under \"media\")")
        return self.items[src]

    def _clip(self, ref, audio=False):
        """Resolve a clip reference to an engine clip id."""
        if isinstance(ref, dict) and "id" in ref:
            return int(ref["id"])
        if isinstance(ref, bool):
            raise RecipeError("clip must be a name, #index, or {\"id\": n}")
        if isinstance(ref, (int, float)):
            return int(ref)
        if not isinstance(ref, str):
            raise RecipeError(f"bad clip ref {ref!r}")
        want_audio = audio
        name = ref
        if name.endswith(".audio"):
            name, want_audio = name[:-6], True
        elif name.endswith(".video"):
            name, want_audio = name[:-6], False
        if name.startswith("#") and name[1:].isdigit():
            i = int(name[1:])
            if i >= len(self.order):
                raise RecipeError(f"clip ref {ref}: only {len(self.order)} clips placed")
            cid = self.order[i]
            return cid[1] if (want_audio and len(cid) > 1) else cid[0]
        table = self.aclips if want_audio else self.vclips
        if name in table:
            return table[name]
        if not want_audio and name in self.aclips:
            return self.aclips[name]
        raise RecipeError(f"unknown clip `{ref}` (name a place/text/shape op or use #n)")

    def _register(self, op, clips):
        ids = list(clips or [])
        if not ids:
            return
        self.order.append(ids)
        name = op.get("name")
        if name:
            self.vclips[name] = ids[0]
            if len(ids) > 1:
                self.aclips[name] = ids[1]

    # -- phases --------------------------------------------------------------

    def build(self):
        r = self.r
        project = r.get("project") or {}
        if project.get("open"):
            self.s.run("file.open", {"path": project["open"]})
            self.log(f"opened {project['open']}")
        else:
            seq = {"name": "Sequence 01", "width": 1920, "height": 1080, "fps": 30,
                   "sampleRate": 48000, "video": 3, "audio": 3}
            seq.update(r.get("sequence") or {})
            self.s.run("file.newSequence", seq)
            self.log(f"sequence {seq['width']}x{seq['height']} @{seq['fps']}fps")

        self._import_media(r.get("media") or {})
        if r.get("captions"):
            self._caption_setup(r["captions"])
        for i, op in enumerate(r.get("timeline") or []):
            try:
                self._op(op)
            except FcError as e:
                raise RecipeError(f"timeline[{i}] {op.get('op', '?')}: {e}")
        if project.get("save"):
            save_path = _absolute_output_path(project["save"])
            self.s.run("file.saveAs", {"path": save_path})
            self.log(f"saved {save_path}")
        return self._export(r.get("export") or {})

    def _import_media(self, media):
        if isinstance(media, dict):
            media = [{"name": k, "path": v} for k, v in media.items()]
        plain, plain_names = [], []
        for m in media:
            if isinstance(m, str):
                m = {"path": m}
            name = m.get("name") or os.path.splitext(os.path.basename(str(m["path"])))[0]
            if m.get("imageSequence"):
                res = self.s.run("file.importImageSequence", {"path": m["path"]})
                item = res.get("item") or (res.get("items") or [None])[0]
                if item is None:
                    raise RecipeError(f"importImageSequence {m['path']}: no item id in result")
                self.items[name] = int(item)
            else:
                plain.append(str(m["path"]))
                plain_names.append(name)
        if plain:
            res = self.s.run("file.import", {"paths": plain})
            items = res.get("items") or []
            if len(items) != len(plain_names):
                raise RecipeError(f"import returned {len(items)} items for {len(plain_names)} paths: {res}")
            for name, item in zip(plain_names, items):
                self.items[name] = int(item)
            self.log(f"imported {len(items)} item(s): {self.items}")

    def _clip_info(self, clip_id):
        doc = self.s.inspect()
        sequence = doc.get("sequence") or {}
        for media_type in ("video", "audio"):
            for track in sequence.get(media_type) or []:
                for clip in track.get("items") or []:
                    if clip.get("clip") == clip_id:
                        return track, clip
        return None, None

    def _clip_track(self, clip_id):
        track, _ = self._clip_info(clip_id)
        if not track:
            raise RecipeError(f"cannot find current track for clip {clip_id}")
        return track.get("id", track.get("name"))

    def _placed_duration(self, clips, item):
        doc = self.s.inspect()
        sequence = doc.get("sequence") or {}
        for clip_id in clips:
            if clip_id is None:
                continue
            for media_type in ("video", "audio"):
                for track in sequence.get(media_type) or []:
                    for clip in track.get("items") or []:
                        if clip.get("clip") == clip_id and clip.get("duration") is not None:
                            return int(clip["duration"])
        root = (doc.get("project") or {}).get("root") or {}
        for child in root.get("children") or []:
            if child.get("item") == item and child.get("duration") is not None:
                return int(child["duration"])
        return 0

    def _caption_setup(self, cfg):
        params = {"format": cfg.get("format", "Subtitle")}
        if cfg.get("name"):
            params["name"] = cfg["name"]
        if cfg.get("language"):
            params["language"] = cfg["language"]
        res = self.s.run("captions.newTrack", params)
        self.caption_track = res.get("track")
        if cfg.get("style"):
            st = dict(cfg["style"])
            if self.caption_track is not None:
                st["track"] = self.caption_track
            self.s.run("captions.setStyle", st)
        self.log(f"caption track {self.caption_track}")

    # -- ops -----------------------------------------------------------------

    def _place(self, op, audio_only=False):
        item = self._item(_need(op, "src"))
        if audio_only:
            track = op.get("audioTrack") or op.get("track") or "A1"
            params = {"item": item, "audioTrack": track, "time": self._at(track, op)}
        else:
            track = op.get("track") or "V1"
            params = {"item": item, "track": track, "time": self._at(track, op)}
            if op.get("audioTrack"):
                params["audioTrack"] = op["audioTrack"]
        dur = None
        if "dur" in op:
            dur = ticks(op["dur"])
        elif "in" in op and "out" in op:
            dur = ticks(op["out"] - op["in"])
        elif "out" in op:
            dur = ticks(op["out"])
        if "in" in op:
            params["sourceIn"] = ticks(op["in"])
        if dur is not None:
            params["duration"] = dur
        if audio_only:
            params["video"], params["audio"] = False, True
        else:
            params["video"] = op.get("video", True)
            params["audio"] = op.get("audio", True)
        if op.get("insert"):
            params["insert"] = True
        res = self.s.run("timeline.place", params)
        clips = res.get("clips") or [res.get("clip")]
        if dur is None:
            for key in ("duration", "durationTicks"):
                if res.get(key) is not None:
                    dur = int(res[key])
                    break
            if dur is None:
                dur = self._placed_duration(clips, item)
        self._register(op, clips)
        self._advance(track, params["time"], dur or 0)
        if params.get("audioTrack") and params.get("audio"):
            self._advance(params["audioTrack"], params["time"], dur or 0)
        self.log(f"place {op.get('src')} -> clips {clips} at {params['time'] / TICKS:.2f}s")
        return res

    def _op(self, op):
        kind = op.get("op")
        if not kind:
            raise RecipeError("timeline entry missing \"op\"")

        if kind == "place":
            return self._place(op)

        if kind == "audio":
            return self._place(op, audio_only=True)

        if kind == "image":
            op = dict(op)
            op.setdefault("dur", 5)
            op.setdefault("audio", False)
            return self._place(op)

        if kind in ("matte", "colorMatte"):
            color = _need(op, "color")
            res = self.s.run("file.newColorMatte", {"color": color, "seconds": op.get("dur", 10)})
            item = res.get("item")
            if item is None:
                raise RecipeError(f"newColorMatte: no item in {res}")
            op = dict(op)
            op["src"] = item
            op.setdefault("audio", False)
            return self._place(op)

        if kind == "bars":
            res = self.s.run("file.newBarsAndTone", {"seconds": op.get("dur", 10)})
            item = res.get("item")
            if item is None:
                raise RecipeError(f"newBarsAndTone: no item in {res}")
            op = dict(op)
            op["src"] = item
            return self._place(op)

        if kind == "text":
            params = {
                "text": _need(op, "content"),
                "seconds": op.get("dur", 3),
                "time": self._at("V*", op),
            }
            for k in ("position", "box", "font", "fontStyle", "size", "vertical", "track"):
                if k in op:
                    params[k] = op[k]
            res = self.s.run("graphics.newText", params)
            clip = res.get("clip")
            if clip is not None:
                self._register(op, [clip])
                if op.get("props"):
                    self.s.run("graphics.set", {"clip": clip, "props": op["props"]})
            self.log(f"text '{str(params['text'])[:40]}' -> clip {clip}")
            return res

        if kind == "shape":
            shape = op.get("shape", "rectangle")
            cmd = {"rectangle": "graphics.newRectangle", "ellipse": "graphics.newEllipse",
                   "polygon": "graphics.newPolygon"}.get(shape)
            if not cmd:
                raise RecipeError(f"shape must be rectangle|ellipse|polygon, got {shape}")
            params = {"seconds": op.get("dur", 5), "time": self._at("V*", op)}
            for k in ("position", "size", "sides", "track", "clip"):
                if k in op:
                    params[k] = op[k]
            res = self.s.run(cmd, params)
            if res.get("clip") is not None:
                self._register(op, [res["clip"]])
            return res

        if kind == "transition":
            ref = _need(op, "clip")
            audio = str(ref).endswith(".audio") or op.get("kind") == "audio"
            params = {"clip": self._clip(ref, audio=audio)}
            for k in ("effect", "frames", "edge", "params", "reverse"):
                if k in op:
                    params[k] = op[k]
            cmd = "sequence.applyAudioTransition" if audio else "sequence.applyVideoTransition"
            return self.s.run(cmd, params)

        if kind == "effect":
            ref = _need(op, "clip")
            cid = self._clip(ref)
            effect = _need(op, "effect")
            res = self.s.run("effects.apply", {"clips": [cid], "effect": effect})
            for pname, value in (op.get("params") or {}).items():
                self.s.run("effects.setParam",
                           {"clip": cid, "effect": effect, "param": pname, "value": value})
            self.log(f"effect {effect} -> clip {cid}")
            return res

        if kind == "param":
            ref = _need(op, "clip")
            params = {"clip": self._clip(ref), "param": _need(op, "param"),
                      "value": _need(op, "value")}
            for k in ("effect", "time", "keyframe", "mask", "merge", "begin"):
                if k in op:
                    params[k] = ticks(op[k]) if k == "time" else op[k]
            params.setdefault("effect", "motion")
            return self.s.run("effects.setParam", params)

        if kind == "speed":
            params = {"clips": [self._clip(_need(op, "clip"))], "speed": _need(op, "speed")}
            for k in ("reverse", "ripple", "interpolation"):
                if k in op:
                    params[k] = op[k]
            return self.s.run("clip.speedDuration", params)

        if kind == "gain":
            params = {"clips": [self._clip(_need(op, "clip"))], "db": _need(op, "db")}
            for k in ("mode", "relative"):
                if k in op:
                    params[k] = op[k]
            return self.s.run("clip.audioGain", params)

        if kind == "trim":
            params = {"clip": self._clip(_need(op, "clip")), "edge": op.get("edge", "out"),
                      "mode": op.get("mode", "regular"), "delta": ticks(_need(op, "delta"))}
            if "deltaFrames" in op:
                params = {k: v for k, v in params.items() if k != "delta"}
                params["deltaFrames"] = op["deltaFrames"]
            return self.s.run("timeline.trim", params)

        if kind == "move":
            clip_id = self._clip(_need(op, "clip"))
            params = {"moves": [{"clip": self._clip(_need(op, "clip")),
                                 "time": ticks(_need(op, "to"))}]}
            params["moves"][0]["track"] = op.get("track") or self._clip_track(clip_id)
            for k in ("insert", "linked", "copy"):
                if k in op:
                    params[k] = op[k]
            return self.s.run("timeline.move", params)

        if kind == "razor":
            params = {"time": ticks(_need(op, "at"))}
            if "track" in op:
                params["track"] = op["track"]
            return self.s.run("timeline.razor", params)

        if kind == "caption":
            params = {"text": _need(op, "text"), "seconds": op.get("at", op.get("seconds", 0)),
                      "durationSeconds": op.get("dur", 3)}
            if self.caption_track is not None:
                params["track"] = self.caption_track
            elif "track" in op:
                params["track"] = op["track"]
            res = self.s.run("captions.add", params)
            if res.get("caption") is not None and op.get("name"):
                self.vclips[op["name"]] = res["caption"]
            return res

        if kind == "importCaptions":
            params = {"path": _need(op, "path")}
            if "name" in op:
                params["name"] = op["name"]
            return self.s.run("captions.import", params)

        if kind == "marker":
            params = {"time": ticks(_need(op, "at")), "target": "program"}
            for k in ("name", "comment", "color", "durationFrames"):
                if k in op:
                    params[k] = op[k]
            return self.s.run("markers.add", params)

        if kind == "preview":
            params = {"target": "program", "time": ticks(op.get("at", 0))}
            if op.get("path"):
                params["path"] = _absolute_output_path(op["path"])
            return self.s.run("file.exportFrame", params)

        if kind == "transcribe":
            params = {}
            for k in ("track", "language", "diarize", "maxSpeakers", "model"):
                if k in op:
                    params[k] = op[k]
            return self.s.run("sequence.transcribe", params)

        if kind == "addTracks":
            params = {}
            for k in ("video", "audio", "submix", "videoAfter", "audioAfter", "submixAfter",
                      "audioType", "submixType"):
                if k in op:
                    params[k] = op[k]
            return self.s.run("sequence.addTracks", params)

        if kind == "raw":
            command = _need(op, "id")
            return self.s.run(command, _normalize_output_args(command, op.get("params") or {}))

        if kind == "tool":
            name = _need(op, "name")
            return self.s.tool(name, _normalize_output_args(name, op.get("arguments") or {}))

        raise RecipeError(f"unknown op `{kind}` (place|audio|image|matte|bars|text|shape|"
                          "transition|effect|param|speed|gain|trim|move|razor|caption|"
                          "importCaptions|marker|preview|transcribe|addTracks|raw|tool)")

    def _export(self, spec):
        path = _absolute_output_path(spec.get("path"))
        if not path:
            self.log("no export block — project only")
            return {"exported": None}
        params = _normalize_output_args("file.exportMedia", {"path": path, "wait": True})
        for k in ("preset", "format", "audioCodec", "flacLevel", "width", "height", "fps",
                  "bitrateKbps", "bitrateMode", "crf", "hardwareEncoding", "scale", "audio",
                  "quality", "burnCaptions", "alpha", "exrCompression", "captionSidecar",
                  "loudnessLufs", "proresProfile", "dnxProfile", "apvProfile", "mxfVideoCodec",
                  "sequence", "range", "startSeconds", "endSeconds"):
            if k in spec:
                params[k] = spec[k]
        res = self.s.run("file.exportMedia", params)
        self.log(f"exported {path}")
        return {"exported": path, "result": res}


def _write_images(result, paths):
    images = result.get("images", []) if isinstance(result, dict) else []
    if not images:
        return []
    if isinstance(paths, str):
        paths = [paths]
    paths = list(paths or [])
    written = []
    for image, path in zip(images, paths):
        data = base64.b64decode(image.get("data", ""))
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        written.append(path)
    return written


class EffectRecipeBuilder:
    def __init__(self, recipe, sess, log=None):
        self.r = recipe
        self.s = sess
        self.log = log or (lambda msg: None)
        self.media = {}
        self.layers = {}

    def _layer(self, ref):
        if isinstance(ref, str) and ref in self.layers:
            return self.layers[ref]
        return ref

    def _call(self, name, arguments):
        return self.s.tool(name, _normalize_output_args(name, arguments))

    def _run(self, command, params):
        return self.s.run(command, _normalize_output_args(command, params))

    def _image(self, args, path):
        result = self._call("render_preview", args)
        paths = ([_absolute_output_path(path)] if isinstance(path, str) else
                 [_absolute_output_path(item) for item in path or []])
        for item in paths:
            os.makedirs(os.path.dirname(os.path.abspath(item)), exist_ok=True)
        written = _write_images(result, paths)
        if not written:
            raise RecipeError("render_preview returned no PNG image")
        return {"path": written[0]}

    def build(self):
        comp = dict(self.r.get("comp") or {})
        params = {k: comp[k] for k in ("name", "width", "height", "duration", "background")
                  if k in comp}
        if "fps" in comp:
            params["frameRate"] = comp["fps"]
        created = self._run("comp.new", params)
        comp_id = created.get("comp", created.get("id", comp.get("name")))

        for name, path in (self.r.get("media") or {}).items():
            result = self._run("file.import", {"paths": [path]})
            items = result.get("items") or []
            self.media[name] = (items[0].get("id", items[0]) if items and isinstance(items[0], dict)
                                else items[0] if items else result.get(
                                    "item", result.get("footage", result.get("id", result))))

        for spec in self.r.get("layers") or []:
            kind = spec.get("type")
            name = spec.get("name")
            if kind == "solid":
                result = self._run("layer.newSolid", {"color": spec.get("solid", spec.get("color", "#000000"))})
            elif kind == "text":
                p = {"text": spec.get("text", ""), "size": spec.get("size", 64)}
                for k in ("fill", "font", "style", "justify"):
                    if k in spec:
                        p[k] = spec[k]
                result = self._run("layer.newText", p)
            elif kind == "shape":
                shape = spec.get("shape", "rectangle")
                p = {"kind": {"rectangle": "rect", "ellipse": "ellipse", "polygon": "polygon",
                              "star": "star"}.get(shape, shape)}
                for k in ("size", "fill", "stroke"):
                    if k in spec:
                        p[k] = spec[k]
                result = self._run("layer.newShape", p)
            elif kind in ("null", "adjustment", "camera", "light"):
                result = self._run("layer.new" + kind.title(), {})
            elif kind == "footage":
                src = spec.get("src")
                if src not in self.media:
                    raise RecipeError(f"unknown footage media `{src}`")
                result = self._run("layer.addItem", {"item": self.media[src]})
            else:
                raise RecipeError(f"unknown effect layer type {kind!r}")
            layer = result.get("layer", result.get("id")) if isinstance(result, dict) else result
            if name:
                self.layers[name] = layer
            if name:
                self._run("layer.rename", {"layer": layer, "name": name})
            timing = {k: spec[k] for k in ("in", "out", "start") if k in spec}
            if timing:
                self._run("layer.timing", dict({"layers": [layer]}, **timing))
            if "blend" in spec:
                self._run("layer.setBlendMode", {"layers": [layer], "mode": spec["blend"]})
            if spec.get("parent"):
                self._run("layer.setParent", {"layers": [layer],
                          "parent": self._layer(spec["parent"])})
            matte = spec.get("matte")
            if matte:
                self._run("layer.setTrackMatte", {"layer": layer,
                         "matte": self._layer(matte["layer"]), "kind": matte.get("mode", "alpha")})
            if spec.get("threeD") is not None:
                self._run("layer.setSwitch", {"layers": [layer], "switch": "threeD",
                         "value": bool(spec["threeD"])})
            for prop, value in (spec.get("transform") or {}).items():
                path = "transform/" + prop
                self._call("set_property", {"layer": layer, "path": path, "value": value})
            for prop, keys in (spec.get("keys") or {}).items():
                self._call("add_keyframe", {"layer": layer, "path": prop, "keys": keys,
                              "interpolation": spec.get("ease", "linear")})
            for prop, expression in (spec.get("expressions") or {}).items():
                self._call("set_property", {"layer": layer, "path": prop,
                              "expression": expression})
            for effect in spec.get("effects") or []:
                applied = self._call("add_effect", {"layer": layer, "effect": effect["effect"],
                                       "values": effect.get("values") or {}})
                prefix = applied.get("path")
                for param, keys in (effect.get("keys") or {}).items():
                    self._call("add_keyframe", {"layer": layer, "path": f"{prefix}/{param}",
                                  "keys": keys, "interpolation": spec.get("ease", "linear")})
            for mask in spec.get("masks") or []:
                self._run("layer.addMask", dict({"layer": layer}, **mask))

        scripts = self.r.get("script") or []
        if isinstance(scripts, str):
            scripts = [scripts]
        for code in scripts:
            self._call("run_script", {"code": code})
        for op in self.r.get("ops") or []:
            kind = op.get("op")
            if kind == "raw":
                self._run(op["id"], op.get("params") or {})
            elif kind == "tool":
                self._call(op["name"], op.get("arguments") or {})
            else:
                raise RecipeError(f"unknown effect op {kind!r} (raw|tool)")

        previews = []
        for preview in self.r.get("preview") or []:
            args = {"time": preview.get("time", 0)}
            if comp_id is not None:
                args["comp"] = comp_id
            previews.append(self._image(args, preview["path"]))
        saved = _absolute_output_path(self.r.get("save"))
        if self.r.get("save"):
            self._call("save_project", {"path": saved})
        render = self.r.get("render") or {}
        rendered = None
        if render.get("path"):
            render_path = _absolute_output_path(render["path"])
            params = {"comp": comp_id, "output": render_path}
            if render.get("format"):
                params["format"] = render["format"]
            for key in ("start", "end"):
                if key in render:
                    params[key] = render[key]
            if "start" in render or "end" in render:
                params["timeSpan"] = "custom"
            self._run("renderQueue.add", params)
            self._run("renderQueue.render", {"wait": True})
            deadline = time.monotonic() + 600
            while not os.path.exists(render_path) and time.monotonic() < deadline:
                time.sleep(0.2)
            if not os.path.exists(render_path):
                raise RecipeError(f"render did not create {render_path}")
            rendered = render_path
        return {"comp": comp_id, "layers": self.layers, "previews": previews,
                "rendered": rendered, "saved": saved}


class SoundRecipeBuilder:
    def __init__(self, recipe, sess):
        self.r = recipe
        self.s = sess
        self.sample_rate = int((recipe.get("session") or {}).get("sample_rate", 48000))
        self.tracks = {}

    @staticmethod
    def _time(seconds):
        return {"seconds": float(seconds)}

    def _call(self, name, arguments=None):
        return self.s.tool(name, _normalize_output_args(name, arguments))

    def _run(self, command, params=None):
        return self.s.run(command, _normalize_output_args(command, params))

    def build(self):
        session = self.r.get("session") or {}
        self._call("new_session", {k: session[k] for k in ("name", "sample_rate", "template")
                                  if k in session})
        for track in self.r.get("tracks") or []:
            created = self._run("track.new", {"name": track["name"], "kind": "audio",
                                               "format": "stereo"})
            ids = created.get("tracks") or []
            track_id = ids[0] if ids else created.get("track", track.get("name"))
            self.tracks[track["name"]] = track_id
            imported = self._call("import_audio", {"path": track["src"], "track": track_id,
                                      "at": self._time(track.get("at", 0))})
            clips = imported.get("clips") or []
            clip_ids = [c.get("id", c) if isinstance(c, dict) else c for c in clips]
            if "gain" in track:
                self._run("mix.volume", {"tracks": [track_id], "db": track["gain"]})
            if "pan" in track:
                self._run("mix.pan", {"tracks": [track_id], "pan": track["pan"]})
            if track.get("mute") is not None:
                self._run("mix.mute", {"tracks": [track_id], "value": bool(track["mute"])})
            if track.get("solo") is not None:
                self._run("mix.solo", {"tracks": [track_id], "value": bool(track["solo"])})
            for index, insert in enumerate(track.get("inserts") or []):
                self._run("mix.insert", {"track": track_id, "slot": index,
                          "plugin": insert["plugin"], "params": insert.get("params") or {}})
            for param, keys in (track.get("automation") or {}).items():
                for key in keys:
                    self._run("automation.set_point", {"track": track_id, "param": param,
                              "at": self._time(key["time"]), "value": key["value"]})
        for marker in self.r.get("markers") or []:
            self._run("markers.add", {"name": marker["name"],
                      "at": self._time(marker.get("at", 0))})
        for op in self.r.get("ops") or []:
            if op.get("op") == "raw":
                self._run(op["id"], op.get("params") or {})
            elif op.get("op") == "tool":
                self._call(op["name"], op.get("arguments") or {})
            else:
                raise RecipeError(f"unknown sound op {op.get('op')!r} (raw|tool)")
        saved = _absolute_output_path(self.r.get("save"))
        if self.r.get("save"):
            self._call("save_session", {"path": saved})
        bounce = self.r.get("bounce") or {}
        bounced = None
        bounce_path = _absolute_output_path(bounce.get("path"))
        if bounce_path:
            args = {k: bounce[k] for k in ("path", "format", "bit_depth", "normalize")
                    if k in bounce}
            args["path"] = bounce_path
            for key in ("start", "end"):
                if key in bounce:
                    args[key] = self._time(bounce[key])
            bounced = self._call("bounce_mix", args)
        return {"tracks": self.tracks, "saved": saved,
                "bounce": bounced, "bounce_path": bounce_path}


def _replace_paths(value, outputs):
    if isinstance(value, str):
        for name, path in outputs.items():
            value = value.replace("{{" + name + "}}", path)
        return value
    if isinstance(value, list):
        return [_replace_paths(item, outputs) for item in value]
    if isinstance(value, dict):
        return {key: _replace_paths(item, outputs) for key, item in value.items()}
    return value


def build_recipe(recipe, session_factory, log=None, close_session=True):
    app = recipe.get("app", "film")
    if app == "pipeline":
        results = []
        outputs = {}
        for step in recipe.get("steps") or []:
            step_id = step.get("id")
            if not step_id:
                raise RecipeError("pipeline step is missing `id`")
            spec = _replace_paths({k: v for k, v in step.items() if k != "id"}, outputs)
            spec.setdefault("app", "film")
            result = build_recipe(spec, session_factory, log=log, close_session=close_session)
            results.append({"id": step_id, "app": spec["app"], "result": result})
            output = (result.get("rendered") if spec["app"] == "effect" else
                      result.get("bounce_path") if spec["app"] == "sound" else
                      result.get("exported"))
            if output:
                outputs[step_id] = output
        return {"steps": results}
    sess = session_factory(app)
    def run():
        if app == "film":
            return RecipeBuilder(recipe, sess, log=log).build()
        if app == "effect":
            return EffectRecipeBuilder(recipe, sess, log=log).build()
        if app == "sound":
            return SoundRecipeBuilder(recipe, sess).build()
        raise RecipeError(f"unknown recipe app {app!r}")
    if close_session:
        with sess:
            return run()
    return run()


def cmd_build(args):
    with open(args.recipe, "r", encoding="utf-8") as f:
        recipe = json.load(f)
    mcp_args = []
    if args.project:
        mcp_args += ["--project", args.project]
    log = (lambda m: print(m, file=sys.stderr)) if args.verbose else (lambda m: None)
    recipe.setdefault("app", args.app)
    out = build_recipe(recipe, lambda app: FcSession(
        app=app, cli=args.cli if app == args.app else None, mcp_args=mcp_args), log=log)
    print(json.dumps(out, indent=None if args.compact else 2))


# ---------------------------------------------------------------------------
# HTTP server


class Handler(BaseHTTPRequestHandler):
    server_version = "fcedit/0.1"
    timeout = 30

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024 * 1024:
            raise FcError("body too large")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise FcError(f"invalid JSON body: {e}")

    def _reply(self, obj, code=200, raw=None, ctype="application/json"):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        origin = self.headers.get("Origin")
        if origin and origin in self.server.cors_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, e, code=400):
        self._reply({"ok": False, "error": str(e)}, code)

    def log_message(self, fmt, *a):
        sys.stderr.write("fcserve %s\n" % (fmt % a))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        origin = self.headers.get("Origin")
        if origin and origin in self.server.cors_origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()

    def _authorized(self):
        from urllib.parse import urlparse
        if self.command == "GET" and urlparse(self.path).path == "/health":
            return True
        token = self.server.token
        if not token:
            return True
        supplied = self.headers.get("Authorization", "")
        return hmac.compare_digest(supplied.encode("utf-8"),
                                   ("Bearer " + token).encode("utf-8"))

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if not self._authorized():
            return self._err(FcError("unauthorized"), 401)
        try:
            if u.path == "/health":
                states = {app: {"started": app in self.server.sessions,
                                "error": self.server.errors.get(app)}
                          for app in self.server.apps}
                return self._reply({"ok": True, "service": "fcedit",
                                    "default": self.server.default_app, "apps": states})
            app, path = self.server.route(u.path)
            sess = self.server.session_for(app) if path not in ("/", "/health") else None
            if path == "/":
                return self._reply({"ok": True, "service": "fcedit", "endpoints": [
                    "GET /commands?filter=x", "POST /run {id,params}", "POST /batch {steps}",
                    "GET /inspect", "POST /frame {seconds}", "POST /recipe <recipe json>",
                    "POST /exec {id,params}", "GET /tools", "POST /tool {name,arguments}",
                    "GET /describe?id=...", "POST /pipeline", "GET /health"]})
            if path == "/version":
                return self._reply({"ok": True, "result": {"tool": "fcedit", "app": app}})
            if path == "/tools":
                return self._reply({"ok": True, "result": sess.tools()})
            if path == "/describe":
                command = q.get("id", q.get("command", [None]))[0]
                if not command:
                    raise FcError("describe requires ?id=<command>")
                return self._reply({"ok": True, "result": sess.describe(command)})
            if path == "/commands":
                return self._reply({"ok": True, "result": sess.commands(q.get("filter", [None])[0])})
            if path == "/inspect":
                return self._reply({"ok": True, "result": sess.inspect()})
            return self._err(FcError("unknown endpoint"), 404)
        except FcError as e:
            return self._err(e)

    def do_POST(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        if not self._authorized():
            return self._err(FcError("unauthorized"), 401)
        try:
            body = self._body()
            if u.path == "/pipeline":
                if body.get("app") != "pipeline":
                    body = dict(body, app="pipeline")
                with self.server.build_guard(self.server.recipe_apps(body)):
                    result = build_recipe(body, lambda app: self.server.session_for(app),
                                          close_session=False)
                return self._reply({"ok": True, "result": result})
            app, path = self.server.route(u.path)
            sess = self.server.session_for(app)
            if path in ("/run", "/exec"):
                return self._reply({"ok": True, "result": sess.run(body["id"], body.get("params"))})
            if path == "/batch":
                return self._reply({"ok": True, "result": sess.batch(
                    body.get("steps") or [], body.get("stop_on_error", True))})
            if path == "/inspect":
                return self._reply({"ok": True, "result": sess.inspect()})
            if path == "/frame":
                png = sess.frame(body.get("seconds", 0), body.get("scale"))
                return self._reply(None, raw=png, ctype="image/png")
            if path == "/recipe":
                spec = dict(body)
                spec.setdefault("app", app)
                with self.server.build_guard(self.server.recipe_apps(spec, app)):
                    res = build_recipe(spec, lambda engine: self.server.session_for(engine),
                                       close_session=False)
                return self._reply({"ok": True, "result": res})
            if path == "/tool":
                res = sess.tool(body["name"], body.get("arguments") or {})
                return self._reply({"ok": True, "result": res})
            if path == "/describe":
                res = sess.describe(body.get("id", body.get("command", "")))
                return self._reply({"ok": True, "result": res})
            if path == "/command":
                return self._reply({"ok": True, "result": sess.run(body["id"], body.get("params"))})
            return self._err(FcError("unknown endpoint"), 404)
        except FcError as e:
            return self._err(e)
        except Exception as e:  # a session-level failure should not kill the service
            return self._err(FcError(f"{type(e).__name__}: {e}"), 500)


class MultiAppServer(ThreadingHTTPServer):
    def __init__(self, address, handler, apps, default_app, cli=None, mcp_args=None,
                 token=None, cors_origins=None):
        super().__init__(address, handler)
        self.apps = tuple(apps)
        self.default_app = default_app
        self.cli = cli
        self.mcp_args = mcp_args or []
        self.token = token
        self.cors_origins = set(cors_origins or [])
        self.sessions = {}
        self.errors = {}
        self.session_lock = threading.Lock()
        self.build_locks = {app: threading.RLock() for app in APP_CONFIG}

    @staticmethod
    def recipe_apps(recipe, default_app="film"):
        app = recipe.get("app", default_app)
        if app != "pipeline":
            return {app}
        apps = set()
        for step in recipe.get("steps") or []:
            nested = dict(step)
            nested.pop("id", None)
            nested.setdefault("app", "film")
            apps.update(MultiAppServer.recipe_apps(nested))
        return apps

    @contextlib.contextmanager
    def build_guard(self, apps):
        with contextlib.ExitStack() as stack:
            for app in sorted(set(apps)):
                stack.enter_context(self.build_locks[app])
            yield

    def route(self, path):
        parts = path.split("/", 2)
        if len(parts) > 1 and parts[1] in self.apps:
            return parts[1], "/" + (parts[2] if len(parts) > 2 else "")
        return self.default_app, path

    def session_for(self, app):
        if app not in self.apps:
            raise FcError(f"app {app!r} is not enabled on this server")
        with self.session_lock:
            if app not in self.sessions:
                try:
                    self.sessions[app] = FcSession(app=app,
                                                   cli=self.cli if app == self.default_app else None,
                                                   mcp_args=self.mcp_args)
                    self.errors.pop(app, None)
                except FcError as error:
                    self.errors[app] = str(error)
                    raise
            return self.sessions[app]


def cmd_serve(args):
    mcp_args = []
    if args.project:
        mcp_args += ["--project", args.project]
    if args.demo:
        mcp_args += ["--demo"]
    if args.bridge:
        mcp_args += ["--bridge", args.bridge]
    apps = [x.strip() for x in (args.apps or args.app).split(",") if x.strip()]
    if not apps or any(x not in APP_CONFIG for x in apps):
        raise FcError("--apps must be a comma-separated subset of film,effect,sound")
    if args.app not in apps:
        raise FcError("the selected --app must be included in --apps")
    token = args.token or os.environ.get("FCEDIT_TOKEN")
    if not token and not args.insecure_no_auth:
        try:
            loopback = args.host.lower() == "localhost" or ipaddress.ip_address(
                args.host.strip("[]")).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise FcError("serving on a non-loopback host requires --token/FCEDIT_TOKEN "
                          "or --insecure-no-auth")
    cors_origins = list(args.cors or [])
    if os.environ.get("FCEDIT_CORS"):
        cors_origins.extend(x.strip() for x in os.environ["FCEDIT_CORS"].split(",") if x.strip())
    server = MultiAppServer((args.host, args.port), Handler, apps, args.app,
                            cli=args.cli, mcp_args=mcp_args, token=token,
                            cors_origins=cors_origins)
    print(f"fcedit serving on http://{args.host}:{args.port} (ctrl+c to stop)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for sess in server.sessions.values():
            sess.close()
        server.server_close()


# ---------------------------------------------------------------------------
# one-shot subcommands


def cmd_exec(args):
    params = json.loads(args.params) if args.params else {}
    with FcSession(app=args.app, cli=args.cli) as sess:
        result = sess.run(args.id, params)
    _write_images(result, args.out)
    print(json.dumps(result, indent=2))


def cmd_commands(args):
    with FcSession(app=args.app, cli=args.cli) as sess:
        print(json.dumps(sess.commands(args.filter), indent=2))


def cmd_inspect(args):
    with FcSession(app=args.app, cli=args.cli) as sess:
        print(json.dumps(sess.inspect(), indent=2))


def cmd_frame(args):
    with FcSession(app=args.app, cli=args.cli) as sess:
        png = sess.frame(args.seconds, args.scale)
    with open(args.out, "wb") as f:
        f.write(png)
    print(json.dumps({"wrote": args.out, "bytes": len(png)}))


def cmd_mcp_config(args):
    servers = {}
    for app, key in (("film", "filmcraft"), ("effect", "effectcraft"),
                     ("sound", "soundcraft")):
        _, binary = APP_CONFIG[app]
        cli = args.cli if app == args.app and args.cli else os.environ.get(APP_CONFIG[app][0], binary)
        servers[key] = {"command": cli, "args": ["mcp"]}
    print(json.dumps({"mcpServers": servers}, indent=2))


def cmd_tools(args):
    with FcSession(app=args.app, cli=args.cli) as sess:
        print(json.dumps(sess.tools(), indent=2))


def cmd_tool(args):
    arguments = json.loads(args.arguments) if args.arguments else {}
    with FcSession(app=args.app, cli=args.cli) as sess:
        result = sess.tool(args.name, arguments)
    paths = args.out.split(",") if args.out else []
    written = _write_images(result, paths)
    if written and isinstance(result, dict):
        result = {k: v for k, v in result.items() if k != "images"}
        result["written"] = written
    print(json.dumps(result, indent=2))


def cmd_describe(args):
    with FcSession(app=args.app, cli=args.cli) as sess:
        print(json.dumps(sess.describe(args.id), indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="fcedit", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cli", help="path to the selected engine CLI")
    p.add_argument("--app", choices=tuple(APP_CONFIG), default=None,
                   help="engine app (default: FCEDIT_APP, CLI name, or film)")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="run a recipe JSON file")
    b.add_argument("recipe")
    b.add_argument("--project", help="open this .fcproj in the session")
    b.add_argument("--verbose", "-v", action="store_true")
    b.add_argument("--compact", action="store_true")
    b.set_defaults(fn=cmd_build)

    s = sub.add_parser("serve", help="persistent session as an HTTP service")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8321)
    s.add_argument("--project")
    s.add_argument("--demo", action="store_true")
    s.add_argument("--bridge", help="drive the running desktop app at 127.0.0.1:PORT instead")
    s.add_argument("--apps", help="comma-separated apps to serve (default: --app)")
    s.add_argument("--cors", action="append", default=[], metavar="ORIGIN",
                   help="allow this browser Origin (repeatable; default: none)")
    s.add_argument("--token", help="require this bearer token on every endpoint except GET /health")
    s.add_argument("--insecure-no-auth", action="store_true",
                   help="allow unauthenticated serving on a non-loopback interface")
    s.set_defaults(fn=cmd_serve)

    e = sub.add_parser("exec", help="run one engine command")
    e.add_argument("id")
    e.add_argument("params", nargs="?")
    e.add_argument("--out", help="write returned image content to this path")
    e.set_defaults(fn=cmd_exec)

    c = sub.add_parser("commands", help="list engine commands")
    c.add_argument("filter", nargs="?")
    c.set_defaults(fn=cmd_commands)

    i = sub.add_parser("inspect", help="project + sequence as JSON")
    i.set_defaults(fn=cmd_inspect)

    f = sub.add_parser("frame", help="render one program frame to PNG")
    f.add_argument("--seconds", type=float, default=0.0)
    f.add_argument("--scale", type=float)
    f.add_argument("--out", default="frame.png")
    f.set_defaults(fn=cmd_frame)

    m = sub.add_parser("mcp-config", help="print an MCP client config snippet")
    m.set_defaults(fn=cmd_mcp_config)

    t = sub.add_parser("tools", help="list MCP tools and descriptions")
    t.set_defaults(fn=cmd_tools)

    t = sub.add_parser("tool", help="call an MCP tool directly")
    t.add_argument("name")
    t.add_argument("arguments", nargs="?")
    t.add_argument("--out", help="write image content to this path (comma-separated for multiple images)")
    t.set_defaults(fn=cmd_tool)

    d = sub.add_parser("describe", help="describe an engine command")
    d.add_argument("id")
    d.set_defaults(fn=cmd_describe)

    args = p.parse_args(argv)
    try:
        args.app = choose_app(args.cli, args.app)
        args.fn(args)
    except FcError as e:
        print(f"fcedit: {e}", file=sys.stderr)
        sys.exit(1)
    except RecipeError as e:
        print(f"fcedit: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
