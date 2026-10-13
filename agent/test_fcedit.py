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
