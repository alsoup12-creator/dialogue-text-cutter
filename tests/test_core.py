import tempfile
import unittest
import subprocess
from pathlib import Path
from unittest import mock

import app


class PersistenceTests(unittest.TestCase):
    def test_missing_transcript_path_uses_default_instead_of_opening_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(app.read_json(Path(folder), {"segments": []}), {"segments": []})


class RecognitionConfigurationTests(unittest.TestCase):
    def make_model(self, root: Path, name: str, size: int = 16) -> Path:
        folder = root / f"faster-whisper-{name}"
        folder.mkdir(parents=True)
        (folder / "model.bin").write_bytes(b"x" * size)
        (folder / "config.json").write_text("{}", encoding="utf-8")
        return folder

    def test_discovers_only_complete_models_and_prefers_large_v3(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            self.make_model(root, "small", 8)
            self.make_model(root, "large-v3", 32)
            (root / "faster-whisper-medium").mkdir()
            models = app.discover_purfview_models(root)
        self.assertEqual([model["id"] for model in models], ["small", "large-v3"])
        self.assertEqual(app.default_model_id(models), "large-v3")
        self.assertGreater(models[1]["size"], models[0]["size"])

    def test_old_project_defaults_to_installed_large_v3(self):
        config = {
            "speechEngines": [{
                "id": app.PURFVIEW_ENGINE_ID,
                "available": True,
                "compatible": True,
                "models": [{"id": "small", "installed": True}, {"id": "large-v3", "installed": True}],
            }],
            "defaultRecognition": {"engineId": app.PURFVIEW_ENGINE_ID, "modelId": "large-v3", "language": "zh"},
        }
        recognition, _, model = app.resolve_recognition({}, config)
        self.assertEqual(recognition["modelId"], "large-v3")
        self.assertEqual(model["id"], "large-v3")

    def test_rejects_model_that_is_not_installed(self):
        config = {
            "speechEngines": [{
                "id": app.PURFVIEW_ENGINE_ID,
                "available": True,
                "compatible": True,
                "models": [{"id": "large-v3", "installed": True}],
            }],
        }
        with self.assertRaisesRegex(ValueError, "未安装"):
            app.resolve_recognition({"recognition": {"engineId": app.PURFVIEW_ENGINE_ID, "modelId": "medium"}}, config)

    def test_selected_model_is_passed_to_purfview(self):
        command = app.build_transcribe_command(
            "faster-whisper-xxl.exe",
            {"language": "zh", "modelId": "small"},
            Path("work"),
            Path("audio.wav"),
        )
        self.assertEqual(command[command.index("--model") + 1], "small")
        self.assertEqual(command[command.index("--model_dir") + 1], str(app.DEFAULT_MODEL_DIR))
        self.assertEqual(command[command.index("--word_timestamps") + 1], "True")

    def test_qwen_requires_aligner_and_builds_combined_command(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            asr = root / "qwen3-asr-0.6b-q8_0.gguf"
            asr.write_bytes(b"asr")
            models, aligner = app.discover_qwen_models(root)
            self.assertIsNone(aligner)
            self.assertFalse(models[0]["ready"])
            forced_aligner = root / "qwen3-forced-aligner-0.6b-q4_k_m.gguf"
            forced_aligner.write_bytes(b"align")
            models, aligner = app.discover_qwen_models(root)
            self.assertEqual(aligner, forced_aligner)
            self.assertTrue(models[0]["ready"])
            command = app.build_qwen_transcribe_command(
                {"path": "qwen3-asr-cli.exe", "alignerPath": str(aligner)},
                models[0],
                Path("audio.wav"),
                Path("alignment.json"),
            )
        self.assertIn("--transcribe-align", command)
        self.assertEqual(command[command.index("--aligner-model") + 1], str(forced_aligner))

    def test_qwen_alignment_json_becomes_word_timed_segments(self):
        with tempfile.TemporaryDirectory() as folder:
            result = Path(folder) / "alignment.json"
            result.write_text(
                '{"language":"zh","words":['
                '{"word":"你好，","start":0.1,"end":0.6},'
                '{"word":"世界。","start":0.7,"end":1.2},'
                '{"word":"第二句","start":2.1,"end":2.8}'
                ']}',
                encoding="utf-8",
            )
            transcript = app.normalize_transcript(result, None)
        self.assertTrue(transcript["wordTimestamps"])
        self.assertEqual(transcript["segmentCount"], 2)
        self.assertEqual(transcript["segments"][0]["text"], "你好，世界。")
        self.assertEqual(transcript["segments"][1]["words"][0]["start"], 2.1)


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.segments = [
            {"id": "s00001", "start": 1.0, "end": 2.0, "text": "第一句", "words": []},
            {"id": "s00002", "start": 2.3, "end": 3.0, "text": "第二句", "words": []},
            {"id": "s00003", "start": 5.0, "end": 6.0, "text": "第三句", "words": []},
        ]
        self.edit = {
            "deletedSegmentIds": [],
            "pauseThresholdMs": 650,
            "leadPaddingMs": 100,
            "tailPaddingMs": 150,
        }

    def test_short_pause_is_merged_and_long_pause_is_cut(self):
        clips = app.compute_clips(self.segments, self.edit, 10)
        self.assertEqual(len(clips), 2)
        self.assertAlmostEqual(clips[0].source_start, 0.9)
        self.assertAlmostEqual(clips[0].source_end, 3.15)
        self.assertAlmostEqual(clips[1].source_start, 4.9)
        self.assertAlmostEqual(clips[1].timeline_start, 2.25)

    def test_automatic_pause_removal_can_be_disabled(self):
        self.edit["autoRemoveLongPauses"] = False
        clips = app.compute_clips(self.segments, self.edit, 10)
        self.assertEqual(len(clips), 1)
        self.assertAlmostEqual(clips[0].source_start, 0.9)
        self.assertAlmostEqual(clips[0].source_end, 6.15)

    def test_deleted_segment_is_not_rendered(self):
        self.edit["deletedSegmentIds"] = ["s00002"]
        clips = app.compute_clips(self.segments, self.edit, 10)
        self.assertEqual(len(clips), 2)
        self.assertAlmostEqual(clips[0].source_end, 2.15)

    def test_deleted_segment_stays_removed_when_auto_pause_is_off(self):
        self.edit["autoRemoveLongPauses"] = False
        self.edit["deletedSegmentIds"] = ["s00002"]
        clips = app.compute_clips(self.segments, self.edit, 10)
        self.assertEqual(len(clips), 2)
        self.assertLessEqual(clips[0].source_end, 2.15)

    def test_gap_marker_can_remove_silence_without_deleting_words(self):
        segments = [{
            "id": "s00001", "start": 0.5, "end": 1.5, "text": "你好",
            "words": [
                {"text": "你", "start": 0.5, "end": 0.8},
                {"text": "好", "start": 1.2, "end": 1.5},
            ],
        }]
        edit = dict(self.edit, deletedTimingIds=["gap:s00001:w0000"])
        clips = app.compute_clips(segments, edit, 2)
        self.assertEqual(len(clips), 2)
        self.assertAlmostEqual(clips[0].source_end, 0.8)
        self.assertAlmostEqual(clips[1].source_start, 1.2)

    def test_stretch_marker_trims_tail_without_deleting_word(self):
        segments = [{
            "id": "s00001", "start": 0.5, "end": 1.5, "text": "啊",
            "words": [{"text": "啊", "start": 0.5, "end": 1.5}],
        }]
        edit = dict(self.edit, deletedTimingIds=["stretch:s00001:w0000"])
        clips = app.compute_clips(segments, edit, 2)
        self.assertEqual(len(clips), 2)
        self.assertAlmostEqual(clips[0].source_end, 0.95)
        self.assertAlmostEqual(clips[1].source_start, 1.5)

    def test_retimed_srt_matches_compacted_timeline(self):
        self.edit["deletedSegmentIds"] = ["s00002"]
        clips = app.compute_clips(self.segments, self.edit, 10)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "result.srt"
            count = app.write_retimed_srt(output, self.segments, self.edit, clips)
            content = output.read_text(encoding="utf-8-sig")
        self.assertEqual(count, 2)
        self.assertIn("第一句", content)
        self.assertNotIn("第二句", content)
        self.assertIn("00:00:01,350 --> 00:00:02,350", content)

    def test_subtitle_correction_changes_srt_without_changing_clips(self):
        clips = app.compute_clips(self.segments, self.edit, 10)
        original_duration = sum(clip.duration for clip in clips)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "corrected.srt"
            app.write_retimed_srt(output, self.segments, self.edit, clips, {"s00001": "修正后的第一句"})
            content = output.read_text(encoding="utf-8-sig")
        self.assertIn("修正后的第一句", content)
        self.assertNotIn("\n第一句\n", content)
        self.assertAlmostEqual(sum(clip.duration for clip in clips), original_duration)

    def test_empty_subtitle_correction_hides_caption_but_keeps_video(self):
        clips = app.compute_clips(self.segments, self.edit, 10)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "hidden-caption.srt"
            count = app.write_retimed_srt(output, self.segments, self.edit, clips, {"s00001": ""})
            content = output.read_text(encoding="utf-8-sig")
        self.assertEqual(count, 2)
        self.assertNotIn("第一句", content)
        self.assertTrue(clips)

    def test_segment_caption_survives_removed_gap_after_its_last_word(self):
        segments = [
            {
                "id": "s00001", "start": 0.4, "end": 1.8, "text": "第一句。",
                "words": [{"text": "第一句。", "start": 0.4, "end": 1.7}],
            },
            {
                "id": "s00002", "start": 2.6, "end": 3.6, "text": "第二句。",
                "words": [{"text": "第二句。", "start": 2.6, "end": 3.6}],
            },
        ]
        edit = dict(self.edit, deletedTimingIds=["gap:s00001:end"])
        clips = app.compute_clips(segments, edit, 4)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "gap.srt"
            app.write_retimed_srt(output, segments, edit, clips, {"s00001": "修正后的第一句。"})
            content = output.read_text(encoding="utf-8-sig")
        self.assertIn("修正后的第一句。", content)
        self.assertIn("第二句。", content)


class TranscriptTests(unittest.TestCase):
    def test_normalizes_word_timestamps(self):
        sample = {
            "language": "zh",
            "segments": [{
                "start": 0.2,
                "end": 1.5,
                "text": "你好",
                "words": [{"word": "你", "start": 0.2, "end": 0.6, "probability": 0.99}],
            }],
        }
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "sample.json"
            app.atomic_write_json(source, sample)
            result = app.normalize_transcript(source, None)
        self.assertTrue(result["wordTimestamps"])
        self.assertEqual(result["segments"][0]["words"][0]["text"], "你")

    def test_subtitle_edits_are_normalized_and_validated(self):
        self.assertEqual(app.normalize_subtitle_edits({"s00001": "第一行\r\n第二行"}), {"s00001": "第一行\n第二行"})
        self.assertEqual(app.normalize_subtitle_edits({"s00001": "第一行\n\n\x00 第二行 "}), {"s00001": "第一行\n第二行"})
        with self.assertRaisesRegex(ValueError, "编号"):
            app.normalize_subtitle_edits({"bad-id": "字幕"})
        with self.assertRaisesRegex(ValueError, "文本"):
            app.normalize_subtitle_edits({"s00001": 123})

    def test_srt_is_sorted_and_invalid_cues_are_ignored(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "sample.srt"
            source.write_text(
                "2\n00:00:02,000 --> 00:00:03,000\n第二句\n\n"
                "1\n00:00:00,500 --> 00:00:01,000\n第一句\n\n"
                "3\n00:00:04,000 --> 00:00:03,000\n无效句\n",
                encoding="utf-8",
            )
            segments = app.parse_srt(source)
        self.assertEqual([segment["text"] for segment in segments], ["第一句", "第二句"])
        self.assertEqual([segment["id"] for segment in segments], ["s00001", "s00002"])

    def test_edit_payload_is_validated_and_deduplicated(self):
        segments = [{
            "id": "s00001", "start": 0.0, "end": 1.0, "text": "你好", "words": [
                {"text": "你", "start": 0.0, "end": 0.4},
                {"text": "好", "start": 0.5, "end": 0.9},
            ],
        }]
        current = {
            "deletedSegmentIds": [], "deletedWordIds": [], "deletedTimingIds": [],
            "autoRemoveLongPauses": True, "pauseThresholdMs": 650,
            "leadPaddingMs": 110, "tailPaddingMs": 150, "wordPaddingMs": 50,
        }
        normalized = app.normalize_edit_update(current, {
            "deletedWordIds": ["s00001:w0000", "s00001:w0000"],
            "deletedTimingIds": ["gap:s00001:w0000"],
            "pauseThresholdMs": 700,
        }, segments)
        self.assertEqual(normalized["deletedWordIds"], ["s00001:w0000"])
        self.assertEqual(normalized["deletedTimingIds"], ["gap:s00001:w0000"])
        with self.assertRaisesRegex(ValueError, "不存在"):
            app.normalize_edit_update(current, {"deletedWordIds": ["s00001:w9999"]}, segments)
        with self.assertRaisesRegex(ValueError, "超出"):
            app.normalize_edit_update(current, {"wordPaddingMs": 999}, segments)


class ProjectDeletionTests(unittest.TestCase):
    def test_delete_moves_project_to_trash_and_keeps_external_source(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            projects = root / "projects"
            trash = root / "trash"
            source = root / "original.mp4"
            source.write_bytes(b"original video")
            with mock.patch.object(app, "PROJECTS_DIR", projects), mock.patch.object(app, "TRASH_DIR", trash):
                project = app.create_project("待删除项目")
                project["source"] = {"path": str(source), "name": source.name}
                app.save_project(project)
                result = app.delete_project(project["id"])

                self.assertTrue(result["recoverable"])
                self.assertFalse((projects / project["id"]).exists())
                self.assertTrue(Path(result["trashPath"]).is_dir())
                self.assertTrue(source.is_file())

    def test_deleted_project_can_be_restored_and_purged(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            projects = root / "projects"
            trash = root / "trash"
            with mock.patch.object(app, "PROJECTS_DIR", projects), mock.patch.object(app, "TRASH_DIR", trash):
                first = app.create_project("可恢复项目")
                deleted = app.delete_project(first["id"])
                trash_id = Path(deleted["trashPath"]).name
                self.assertEqual(len(app.list_trash()), 1)
                restored = app.restore_project(trash_id)
                self.assertEqual(restored["id"], first["id"])
                self.assertTrue((projects / first["id"] / "project.json").is_file())

                app.delete_project(first["id"])
                second_trash_id = app.list_trash()[0]["trashId"]
                result = app.purge_trash(second_trash_id)
                self.assertFalse(result["recoverable"])
                self.assertEqual(app.list_trash(), [])


class ProgressTests(unittest.TestCase):
    def test_ffmpeg_progress_time_is_parsed(self):
        self.assertAlmostEqual(app.progress_seconds("01:02:03.500000"), 3723.5)
        self.assertIsNone(app.progress_seconds("not-a-time"))

    def test_cancel_request_marks_job_and_terminates_process(self):
        job = app.set_job("abc123abc123", "render", "running")
        fake_process = mock.Mock()
        app.RUNNING_PROCESSES[job["id"]] = fake_process
        try:
            with mock.patch.object(app, "terminate_process") as terminate:
                result = app.cancel_job(job["id"])
            self.assertEqual(result["state"], "cancelling")
            self.assertTrue(result["cancelRequested"])
            terminate.assert_called_once_with(fake_process)
        finally:
            app.RUNNING_PROCESSES.pop(job["id"], None)
            app.JOBS.pop(job["id"], None)


class SafeRenderTests(unittest.TestCase):
    def test_failed_render_keeps_previous_output(self):
        with tempfile.TemporaryDirectory() as folder:
            projects = Path(folder) / "projects"
            with mock.patch.object(app, "PROJECTS_DIR", projects):
                project = app.create_project("安全导出")
                source = app.project_dir(project["id"]) / "source.mp4"
                source.write_bytes(b"source")
                transcript_path = app.project_dir(project["id"]) / "transcript.json"
                app.atomic_write_json(transcript_path, {"segments": [{"id": "s00001", "start": 0.0, "end": 1.0, "text": "测试", "words": []}]})
                old_output = app.project_dir(project["id"]) / "outputs" / "成片-old.mp4"
                old_output.parent.mkdir(parents=True)
                old_output.write_bytes(b"old result")
                project["source"] = {"path": str(source), "duration": 1.0}
                project["transcript"] = {"path": str(transcript_path)}
                project["outputs"] = {"final": {"videoPath": str(old_output)}}
                app.save_project(project)

                with mock.patch.object(app, "tool_config", return_value={"ffmpeg": "ffmpeg"}), mock.patch.object(app, "nvenc_available", return_value=False), mock.patch.object(app, "run_logged", side_effect=RuntimeError("编码失败")):
                    with self.assertRaises(RuntimeError):
                        app.render_worker(project["id"], "job123", False)
                self.assertEqual(old_output.read_bytes(), b"old result")
                self.assertEqual(list(old_output.parent.glob("*.partial.mp4")), [])


class WordEditingTests(unittest.TestCase):
    def setUp(self):
        self.segments = [{
            "id": "s00001",
            "start": 0.5,
            "end": 2.0,
            "text": "今天嗯测试",
            "words": [
                {"text": "今天", "start": 0.5, "end": 0.9},
                {"text": "嗯", "start": 0.95, "end": 1.15},
                {"text": "测试", "start": 1.2, "end": 1.8},
            ],
        }]
        self.edit = {
            "deletedSegmentIds": [],
            "deletedWordIds": ["s00001:w0001"],
            "pauseThresholdMs": 650,
            "leadPaddingMs": 110,
            "tailPaddingMs": 150,
            "wordPaddingMs": 50,
        }

    def test_deleted_word_creates_hard_cut(self):
        clips = app.compute_clips(self.segments, self.edit, 3)
        self.assertEqual(len(clips), 2)
        self.assertAlmostEqual(clips[0].source_start, 0.45)
        self.assertAlmostEqual(clips[0].source_end, 0.95)
        self.assertAlmostEqual(clips[1].source_start, 1.15)
        self.assertAlmostEqual(clips[1].timeline_start, 0.5)

    def test_retimed_subtitle_omits_deleted_word(self):
        clips = app.compute_clips(self.segments, self.edit, 3)
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "words.srt"
            app.write_retimed_srt(output, self.segments, self.edit, clips)
            content = output.read_text(encoding="utf-8-sig")
        self.assertIn("今天测试", content)
        self.assertNotIn("今天嗯测试", content)
        self.assertIn("00:00:00,050 --> 00:00:01,150", content)

    def test_deleting_every_word_produces_no_clip(self):
        self.edit["deletedWordIds"] = ["s00001:w0000", "s00001:w0001", "s00001:w0002"]
        self.assertEqual(app.compute_clips(self.segments, self.edit, 3), [])


class MediaIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(app.tool_config().get("ffmpeg"), "需要 FFmpeg")
    def test_recognition_audio_proxy_is_created_and_reused(self):
        ffmpeg = app.tool_config()["ffmpeg"]
        with tempfile.TemporaryDirectory() as folder:
            projects = Path(folder) / "projects"
            with mock.patch.object(app, "PROJECTS_DIR", projects):
                project = app.create_project("音频代理")
                source = app.project_dir(project["id"]) / "source.mp4"
                subprocess.run([
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "color=c=black:s=320x180:d=1:r=25",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
                    "-shortest", "-c:v", "libx264", "-c:a", "aac", str(source),
                ], check=True)
                project["source"] = {"path": str(source), **app.probe_media(source)}
                app.save_project(project)
                job = app.set_job(project["id"], "transcribe", "running")
                try:
                    audio = app.recognition_audio_proxy(project, job["id"])
                    self.assertTrue(audio.is_file())
                    with mock.patch.object(app, "run_logged") as run_again:
                        self.assertEqual(app.recognition_audio_proxy(project, job["id"]), audio)
                    run_again.assert_not_called()
                finally:
                    app.JOBS.pop(job["id"], None)

    @unittest.skipUnless(app.tool_config().get("ffmpeg"), "需要 FFmpeg")
    def test_real_video_can_be_probed_and_cut(self):
        ffmpeg = app.tool_config()["ffmpeg"]
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            source = folder / "source.mp4"
            output = folder / "cut.mp4"
            script = folder / "filter.txt"
            subprocess.run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=0x214d3d:s=640x360:d=6:r=25",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                "-shortest", "-c:v", "libx264", "-c:a", "aac", str(source),
            ], check=True)
            details = app.probe_media(source)
            self.assertAlmostEqual(details["duration"], 6, delta=0.2)
            self.assertEqual(details["width"], 640)
            clips = [app.Clip(0.0, 1.0), app.Clip(3.0, 4.5, 1.0)]
            script.write_text(app.make_filter_script(clips, True), encoding="utf-8")
            subprocess.run([
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
                "-filter_complex_script", str(script), "-map", "[outv]", "-map", "[outa]",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(output),
            ], check=True)
            rendered = app.probe_media(output)
            self.assertTrue(output.is_file())
            self.assertAlmostEqual(rendered["duration"], 2.5, delta=0.2)


if __name__ == "__main__":
    unittest.main()
