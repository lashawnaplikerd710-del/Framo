import json
from pathlib import Path
import tempfile
import unittest
import wave
from unittest.mock import patch

from agent_core.direct_renderer import snapshot_from_timeline
from agent_core.direct_resources import build_direct_media_plan
from agent_core.independent_export import build_ffmpeg_command
from agent_core.task_service import TaskService


class DirectResourcesTests(unittest.TestCase):
    def test_direct_service_does_not_require_jianying_install_path(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / "toolchain" / "scripts").mkdir(parents=True)
            config = {"render_backend_default": "direct_ffmpeg", "install_dir": "",
                      "toolchain_root": str(root / "toolchain"),
                      "provider": {"model": "deepseek-flash"}}
            service = TaskService(config, root / "state")
            executor = service.executor_factory("job_" + "a" * 32)
            self.assertTrue(executor.install.is_dir())

    def test_product_ad_media_plan_adds_cached_audio_and_sapi_voice(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            music = root / "music"; sfx = root / "sfx"
            music.mkdir(); sfx.mkdir()
            # The resolver checks bytes and extension; the actual FFmpeg render
            # is covered by the direct smoke run against the bundled resources.
            (music / "bed.mp3").write_bytes(b"music")
            (sfx / "ding.mp3").write_bytes(b"sfx")
            timeline = {
                "duration_us": 2_000_000,
                "source_map": [{"asset_id": "asset_a", "source_start_us": 0,
                                "source_end_us": 2_000_000, "timeline_start_us": 0,
                                "timeline_end_us": 2_000_000}],
                "captions": [{"start_us": 0, "end_us": 2_000_000, "text": "细节展示"}],
                "narration_cues": [{"start_us": 0, "end_us": 2_000_000, "text": "给爱宠一个小窝。"}],
            }
            task = {"workflow": "product_ad", "narration_mode": "new_native",
                    "direct_media": {"music": str(music / "bed.mp3"),
                                     "sound_effects": [str(sfx / "ding.mp3")]}}
            plan = {"title": "商品展示"}
            config = {"direct_media": {"tts": True}}
            def synth(text, output, **kwargs):
                with wave.open(str(output),'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                    wav.writeframes(b'\x00\x00'*16000)
                return output
            with patch('agent_core.direct_resources.synthesize_windows_sapi',side_effect=synth) as voice:
                result = build_direct_media_plan(task, timeline, plan, root / "job", config)
            self.assertEqual(voice.call_args.args[0], '给爱宠一个小窝。')
            self.assertFalse(result["pending"])
            self.assertGreaterEqual(len(result["tracks"]), 1)
            self.assertEqual(result["report"]["tts"]["status"], "COMPLETED")
            for item in result["resources"]:
                self.assertTrue(Path(item["path"]).is_file())

            assets = [{"asset_id": "asset_a", "path": str(root / "video.mp4"),
                       "sha256": "a" * 64, "width": 720, "height": 1280,
                       "has_audio": True}]
            snapshot = snapshot_from_timeline({"target": {"fps": 30, "aspect_ratio": "9:16"}},
                                              assets, timeline, media_plan=result)
            self.assertTrue(any(track["type"] == "audio" for track in snapshot["tracks"]))
            self.assertTrue(any(track["type"] == "music" for track in snapshot["tracks"]))

    def test_music_loop_is_explicit_in_external_command(self):
        snapshot = {
            "duration": 1_000_000, "fps": 30,
            "canvas_config": {"width": 720, "height": 1280, "fps": 30},
            "materials": {"videos": [{"id": "v", "path": str(Path(__file__).resolve()),
                                       "has_audio": False}],
                           "audio": [{"id": "m", "path": str(Path(__file__).resolve())}]},
            "tracks": [{"type": "video", "segments": [{"material_id": "v",
                "target_timerange": {"start": 0, "duration": 1_000_000},
                "source_timerange": {"start": 0, "duration": 1_000_000}}]},
                {"type": "music", "segments": [{"material_id": "m",
                "target_timerange": {"start": 0, "duration": 1_000_000},
                "source_timerange": {"start": 0, "duration": 1_000_000},
                "loop": True}]}],
        }
        command = build_ffmpeg_command(snapshot, Path("out.mp4"), "ffmpeg")
        self.assertIn("-stream_loop", command)
        self.assertIn("amix=inputs=2", " ".join(command))


if __name__ == "__main__":
    unittest.main()
