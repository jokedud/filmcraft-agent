import os
import tempfile
import threading
import unittest

import fcedit


class FakeSession:
    def __init__(self):
        self.calls = []
        self.placed = []

    def run(self, command, params=None):
        self.calls.append((command, params))
        if command == "timeline.place":
            clips = [10 + len(self.placed) * 2, 11 + len(self.placed) * 2]
            self.placed.extend(clips)
            return {"clips": clips}
        return {}

    def inspect(self):
        return {
            "project": {"root": {"children": [{"item": 8, "duration": 125}]}},
            "sequence": {
                "video": [{"id": 1, "items": [
                    {"clip": clip, "duration": 125, "start": 0} for clip in self.placed[::2]
                ]}],
                "audio": [{"id": 4, "items": [
                    {"clip": clip, "duration": 125, "start": 0} for clip in self.placed[1::2]
                ]}],
            },
        }


class FakeEffectSession:
    def __init__(self):
        self.run_calls = []
        self.tool_calls = []
        self.output_path = None

    def run(self, command, params=None):
        params = params or {}
        self.run_calls.append((command, params))
        if command == "comp.new":
            return {"comp": "comp-1"}
        if command == "renderQueue.add":
            self.output_path = params["output"]
        elif command == "renderQueue.render":
            os.makedirs(os.path.dirname(self.output_path), exist_ok=True)
            with open(self.output_path, "wb") as output:
                output.write(b"render")
        return {}

    def tool(self, name, arguments=None):
        arguments = arguments or {}
        self.tool_calls.append((name, arguments))
        if name == "render_preview":
            return {"images": [{"data": "eA=="}]}
        return {}


class FakeSoundSession:
    def __init__(self):
        self.run_calls = []
        self.tool_calls = []

    def run(self, command, params=None):
        params = params or {}
        self.run_calls.append((command, params))
        if command == "track.new":
            return {"tracks": [7]}
        return {}

    def tool(self, name, arguments=None):
        arguments = arguments or {}
        self.tool_calls.append((name, arguments))
        return {}


class RecipeRegressionTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.builder = fcedit.RecipeBuilder({}, self.session)
        self.builder.items["media"] = 8

    def test_audio_only_uses_audio_track_once(self):
        self.builder._place({"op": "audio", "src": "media", "track": "A2"},
                            audio_only=True)
        params = self.session.calls[-1][1]
        self.assertNotIn("track", params)
        self.assertEqual(params["audioTrack"], "A2")

    def test_move_defaults_to_current_track(self):
        self.builder.vclips["clip"] = 10
        self.session.placed = [10]
        self.builder._op({"op": "move", "clip": "clip", "to": 2})
        moves = self.session.calls[-1][1]["moves"]
        self.assertEqual(moves[0]["track"], 1)

    def test_implicit_place_advances_by_inspected_duration(self):
        self.builder._place({"op": "place", "src": "media"})
        self.builder._place({"op": "place", "src": "media"})
        place_calls = [params for command, params in self.session.calls
                       if command == "timeline.place"]
        self.assertEqual(place_calls[1]["time"], 125)

    def test_film_save_and_export_paths_are_absolute(self):
        builder = fcedit.RecipeBuilder({
            "project": {"save": "rel/project.fcproj"},
            "export": {"path": "rel/export.mp4"},
        }, self.session)
        result = builder.build()
        save = next(params for command, params in self.session.calls
                    if command == "file.saveAs")
        export = next(params for command, params in self.session.calls
                      if command == "file.exportMedia")
        self.assertEqual(save["path"], os.path.abspath("rel/project.fcproj"))
        self.assertEqual(export["path"], os.path.abspath("rel/export.mp4"))
        self.assertEqual(result["exported"], os.path.abspath("rel/export.mp4"))

    def test_film_preview_and_raw_output_paths_are_absolute(self):
        self.builder._op({"op": "preview", "path": "rel/preview.png"})
        self.builder._op({"op": "raw", "id": "file.exportFrame",
                          "params": {"path": "rel/raw.png"}})
        preview, raw = self.session.calls[-2:]
        self.assertEqual(preview[1]["path"], os.path.abspath("rel/preview.png"))
        self.assertEqual(raw[1]["path"], os.path.abspath("rel/raw.png"))


class RecipeOutputPathTests(unittest.TestCase):
    def test_effect_output_paths_are_absolute(self):
        with tempfile.TemporaryDirectory() as cwd:
            original_cwd = os.getcwd()
            os.chdir(cwd)
            try:
                session = FakeEffectSession()
                result = fcedit.EffectRecipeBuilder({
                    "comp": {"name": "test", "width": 16, "height": 16, "duration": 1},
                    "preview": [{"time": 0.2, "path": "rel/preview.png"}],
                    "save": "rel/project.ecproj",
                    "render": {"path": "rel/out.mp4", "format": "h264"},
                }, session).build()

                preview_path = os.path.abspath("rel/preview.png")
                save_path = os.path.abspath("rel/project.ecproj")
                render_path = os.path.abspath("rel/out.mp4")
                render = next(params for command, params in session.run_calls
                              if command == "renderQueue.add")
                save = next(args for name, args in session.tool_calls
                            if name == "save_project")
                self.assertEqual(render["output"], render_path)
                self.assertEqual(save["path"], save_path)
                self.assertEqual(result["previews"][0]["path"], preview_path)
                self.assertEqual(result["saved"], save_path)
                self.assertEqual(result["rendered"], render_path)
                self.assertTrue(os.path.isfile(preview_path))
                self.assertTrue(os.path.isfile(render_path))
            finally:
                os.chdir(original_cwd)

    def test_sound_save_and_bounce_paths_are_absolute(self):
        session = FakeSoundSession()
        result = fcedit.SoundRecipeBuilder({
            "save": "rel/session.scraft",
            "bounce": {"path": "rel/out.wav", "format": "wav"},
        }, session).build()

        save = next(args for name, args in session.tool_calls if name == "save_session")
        bounce = next(args for name, args in session.tool_calls if name == "bounce_mix")
        self.assertEqual(save["path"], os.path.abspath("rel/session.scraft"))
        self.assertEqual(bounce["path"], os.path.abspath("rel/out.wav"))
        self.assertEqual(result["saved"], os.path.abspath("rel/session.scraft"))
        self.assertEqual(result["bounce_path"], os.path.abspath("rel/out.wav"))

    def test_pipeline_passes_normalized_output_to_next_step(self):
        with tempfile.TemporaryDirectory() as cwd:
            original_cwd = os.getcwd()
            os.chdir(cwd)
            try:
                sessions = {
                    "effect": FakeEffectSession(),
                    "sound": FakeSoundSession(),
                }
                result = fcedit.build_recipe({
                    "app": "pipeline",
                    "steps": [
                        {
                            "id": "title",
                            "app": "effect",
                            "comp": {"name": "test", "width": 16, "height": 16, "duration": 1},
                            "render": {"path": "rel/out.mp4"},
                        },
                        {
                            "id": "mix",
                            "app": "sound",
                            "tracks": [{"name": "title", "src": "{{title}}"}],
                            "bounce": {"path": "rel/out.wav"},
                        },
                    ],
                }, lambda app: sessions[app], close_session=False)

                render_path = os.path.abspath("rel/out.mp4")
                imported = next(args for name, args in sessions["sound"].tool_calls
                                if name == "import_audio")
                self.assertEqual(imported["path"], render_path)
                self.assertEqual(result["steps"][0]["result"]["rendered"], render_path)
            finally:
                os.chdir(original_cwd)


class RecipeLockTests(unittest.TestCase):
    def test_pipeline_lock_order_serializes_shared_apps(self):
        server = fcedit.MultiAppServer(("127.0.0.1", 0), fcedit.Handler,
                                       ["film", "effect", "sound"], "film")
        first_inside = threading.Event()
        release_first = threading.Event()
        second_inside = threading.Event()

        def first():
            with server.build_guard(["sound", "film"]):
                first_inside.set()
                release_first.wait(2)

        def second():
            with server.build_guard(["film", "sound"]):
                second_inside.set()

        first_thread = threading.Thread(target=first)
        second_thread = threading.Thread(target=second)
        try:
            first_thread.start()
            self.assertTrue(first_inside.wait(2))
            second_thread.start()
            self.assertFalse(second_inside.wait(0.05))
            release_first.set()
            self.assertTrue(second_inside.wait(2))
        finally:
            release_first.set()
            first_thread.join(2)
            second_thread.join(2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
