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
import json
import os
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

TICKS = 254_016_000_000  # FilmCraft time base (ticks per second)


def ticks(seconds):
    return int(round(float(seconds) * TICKS))


class FcError(Exception):
    pass


class FcSession:
    """A live headless engine session behind the MCP stdio server.

    Commands are serialized: one request in flight at a time.
    """

    def __init__(self, cli=None, mcp_args=None, cwd=None):
        self.cli = cli or os.environ.get("FILMCRAFT_CLI") or "filmcraft-cli"
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
                f"filmcraft-cli not found ({self.cli!r}). Install it (agent/install.sh) "
                "or set FILMCRAFT_CLI to the binary path."
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
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                text = c.get("text", "")
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        return result

    # -- convenience wrappers -------------------------------------------------

    def run(self, command_id, params=None):
        return self.tool("command_run", {"id": command_id, "params": params or {}})

    def batch(self, steps, stop_on_error=True):
        return self.tool("command_batch", {"steps": steps, "stop_on_error": stop_on_error})

    def inspect(self):
        return self.tool("doc_inspect")

    def commands(self, filter_text=None):
        args = {"filter": filter_text} if filter_text else {}
        return self.tool("command_list", args)

    def frame(self, seconds=0.0, scale=None):
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
            self.s.run("file.saveAs", {"path": project["save"]})
            self.log(f"saved {project['save']}")
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
        track = op.get("track") or ("A1" if audio_only else "V1")
        params = {"item": item, "track": track, "time": self._at(track, op)}
        if audio_only or op.get("audioTrack"):
            params["audioTrack"] = op.get("audioTrack", "A1" if audio_only else op.get("audioTrack"))
        if not audio_only and op.get("audioTrack"):
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
        self._register(op, clips)
        end = params["time"] + (dur or 0)
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
            params = {"moves": [{"clip": self._clip(_need(op, "clip")),
                                 "time": ticks(_need(op, "to"))}]}
            if "track" in op:
                params["moves"][0]["track"] = op["track"]
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
                params["path"] = op["path"]
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
            return self.s.run(_need(op, "id"), op.get("params") or {})

        raise RecipeError(f"unknown op `{kind}` (place|audio|image|matte|bars|text|shape|"
                          "transition|effect|param|speed|gain|trim|move|razor|caption|"
                          "importCaptions|marker|preview|transcribe|addTracks|raw)")

    def _export(self, spec):
        path = spec.get("path")
        if not path:
            self.log("no export block — project only")
            return {"exported": None}
        params = {"path": path, "wait": True}
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


def cmd_build(args):
    with open(args.recipe, "r", encoding="utf-8") as f:
        recipe = json.load(f)
    mcp_args = []
    if args.project:
        mcp_args += ["--project", args.project]
    log = (lambda m: print(m, file=sys.stderr)) if args.verbose else (lambda m: None)
    with FcSession(cli=args.cli, mcp_args=mcp_args) as sess:
        out = RecipeBuilder(recipe, sess, log=log).build()
    print(json.dumps(out, indent=None if args.compact else 2))


# ---------------------------------------------------------------------------
# HTTP server


class Handler(BaseHTTPRequestHandler):
    server_version = "fcedit/0.1"

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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _err(self, e, code=400):
        self._reply({"ok": False, "error": str(e)}, code)

    def log_message(self, fmt, *a):
        sys.stderr.write("fcserve %s\n" % (fmt % a))

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/health"):
                return self._reply({"ok": True, "service": "fcedit", "endpoints": [
                    "GET /commands?filter=x", "POST /run {id,params}", "POST /batch {steps}",
                    "GET /inspect", "POST /frame {seconds}", "POST /recipe <recipe json>",
                    "POST /exec {id,params}", "GET /version"]})
            if u.path == "/version":
                v = self.server.sess.run("app.about", {}) if False else {"tool": "fcedit"}
                return self._reply({"ok": True, "result": v})
            if u.path == "/commands":
                return self._reply({"ok": True, "result": self.server.sess.commands(q.get("filter", [None])[0])})
            if u.path == "/inspect":
                return self._reply({"ok": True, "result": self.server.sess.inspect()})
            return self._err(FcError("unknown endpoint"), 404)
        except FcError as e:
            return self._err(e)

    def do_POST(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        try:
            body = self._body()
            sess = self.server.sess
            if u.path in ("/run", "/exec"):
                return self._reply({"ok": True, "result": sess.run(body["id"], body.get("params"))})
            if u.path == "/batch":
                return self._reply({"ok": True, "result": sess.batch(
                    body.get("steps") or [], body.get("stop_on_error", True))})
            if u.path == "/inspect":
                return self._reply({"ok": True, "result": sess.inspect()})
            if u.path == "/frame":
                png = sess.frame(body.get("seconds", 0), body.get("scale"))
                return self._reply(None, raw=png, ctype="image/png")
            if u.path == "/recipe":
                res = RecipeBuilder(body, sess).build()
                return self._reply({"ok": True, "result": res})
            if u.path == "/command":
                return self._reply({"ok": True, "result": sess.run(body["id"], body.get("params"))})
            return self._err(FcError("unknown endpoint"), 404)
        except FcError as e:
            return self._err(e)
        except Exception as e:  # a session-level failure should not kill the service
            return self._err(FcError(f"{type(e).__name__}: {e}"), 500)


def cmd_serve(args):
    mcp_args = []
    if args.project:
        mcp_args += ["--project", args.project]
    if args.demo:
        mcp_args += ["--demo"]
    if args.bridge:
        mcp_args += ["--bridge", args.bridge]
    sess = FcSession(cli=args.cli, mcp_args=mcp_args)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.sess = sess
    print(f"fcedit serving on http://{args.host}:{args.port} (ctrl+c to stop)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sess.close()


# ---------------------------------------------------------------------------
# one-shot subcommands


def cmd_exec(args):
    params = json.loads(args.params) if args.params else {}
    with FcSession(cli=args.cli) as sess:
        print(json.dumps(sess.run(args.id, params), indent=2))


def cmd_commands(args):
    with FcSession(cli=args.cli) as sess:
        print(json.dumps(sess.commands(args.filter), indent=2))


def cmd_inspect(args):
    with FcSession(cli=args.cli) as sess:
        print(json.dumps(sess.inspect(), indent=2))


def cmd_frame(args):
    with FcSession(cli=args.cli) as sess:
        png = sess.frame(args.seconds, args.scale)
    with open(args.out, "wb") as f:
        f.write(png)
    print(json.dumps({"wrote": args.out, "bytes": len(png)}))


def cmd_mcp_config(args):
    cli = args.cli or "filmcraft-cli"
    print(json.dumps({"mcpServers": {
        "filmcraft": {"command": os.path.abspath(cli), "args": ["mcp"]},
        "filmcraft-http": {"url": "http://127.0.0.1:8321"},
    }}, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="fcedit", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cli", help="path to filmcraft-cli (default: FILMCRAFT_CLI or PATH)")
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
    s.set_defaults(fn=cmd_serve)

    e = sub.add_parser("exec", help="run one engine command")
    e.add_argument("id")
    e.add_argument("params", nargs="?")
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

    args = p.parse_args(argv)
    try:
        args.fn(args)
    except FcError as e:
        print(f"fcedit: {e}", file=sys.stderr)
        sys.exit(1)
    except RecipeError as e:
        print(f"fcedit: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
