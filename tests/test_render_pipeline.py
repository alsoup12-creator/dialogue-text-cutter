import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app
import render_pipeline as pipeline


class BatchPlanTests(unittest.TestCase):
    def test_many_cuts_have_bounded_fanout_and_source_span(self):
        clips = [app.Clip(i * 0.7, i * 0.7 + 0.123) for i in range(448)]
        batches = pipeline.plan_batches(clips, 60)
        self.assertTrue(all(len(batch) <= 12 for batch in batches))
        self.assertTrue(all(batch[-1]['end'] - batch[0]['start'] <= 45 for batch in batches))
        self.assertEqual(sum(p['frames'] for b in batches for p in b), round(448 * 0.123 * 60))
        self.assertEqual([p['start'] for b in batches for p in b], [c.source_start for c in clips])

    def test_split_long_speech_does_not_add_fades_at_artificial_boundaries(self):
        batches = pipeline.plan_batches([app.Clip(2, 97)], 25)
        pieces = [p for batch in batches for p in batch]
        self.assertEqual(len(pieces), 4)
        self.assertEqual([p['fadeIn'] for p in pieces], [True, False, False, False])
        self.assertEqual([p['fadeOut'] for p in pieces], [False, False, False, True])
        self.assertEqual(sum(p['frames'] for p in pieces), 95 * 25)

    def test_cache_identity_changes_only_when_render_inputs_change(self):
        batch = pipeline.plan_batches([app.Clip(0, 1)], 25)[0]
        signature = {'path': 'source', 'size': 10, 'mtime': 1}
        key = pipeline.cache_key(signature, batch, 25, False, False)
        self.assertEqual(key, pipeline.cache_key(dict(signature), batch, 25, False, False))
        self.assertNotEqual(key, pipeline.cache_key(signature | {'mtime': 2}, batch, 25, False, False))
        self.assertNotEqual(key, pipeline.cache_key(signature, batch, 25, True, False))
        self.assertNotEqual(key, pipeline.cache_key(signature, batch, 25, False, True))


class StallTests(unittest.TestCase):
    def test_heartbeat_does_not_mask_stalled_frames(self):
        with mock.patch.object(app.time, 'time', return_value=140):
            job = app.job_snapshot({'state': 'running', 'lastAdvanceAt': 100, 'updatedAt': 'just now'})
            self.assertEqual(job['stalledSeconds'], 40)
            self.assertEqual(app.job_snapshot({'state': 'completed', 'lastAdvanceAt': 100})['stalledSeconds'], 0)

    def test_repeated_progress_preserves_last_advance_and_global_position(self):
        job = app.set_job('abc123abc123', 'render', 'running')
        process = mock.Mock(stdout=io.StringIO('out_time=00:00:00.200000\nout_time=00:00:00.200000\n'))
        process.wait.return_value = 0
        try:
            with mock.patch.object(app.subprocess, 'Popen', return_value=process), mock.patch.object(app.time, 'time', side_effect=[100, 101]):
                app.run_logged(['tool'], job['id'], expected_duration=1,
                               progress_start=40, progress_span=10, processed_offset=10, total_duration=20)
            current = app.JOBS[job['id']]
            self.assertEqual(current['lastAdvanceAt'], 101)
            self.assertEqual(current['processedSeconds'], 10.2)
            self.assertEqual(current['expectedSeconds'], 20)
            self.assertEqual(current['progress'], 42)
        finally:
            app.JOBS.pop(job['id'], None)


@unittest.skipUnless(app.tool_config().get('ffmpeg'), '需要本地 FFmpeg')
class SegmentedMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patch = mock.patch.object(app, 'PROJECTS_DIR', Path(self.temp.name) / 'projects')
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.ffmpeg = app.tool_config()['ffmpeg']
        self.project = app.create_project('分段集成')
        folder = app.project_dir(self.project['id'])
        source = folder / 'source.mp4'
        subprocess.run([
            self.ffmpeg, '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', 'testsrc2=s=320x180:r=25:d=12',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000:duration=12',
            '-shortest', '-c:v', 'libx264', '-c:a', 'aac', str(source),
        ], check=True, capture_output=True)
        self.project['source'] = {'path': str(source), **app.probe_media(source)}
        self.segments = [{'id': f's{i:05}', 'start': 0.1 + i * 0.25,
                          'end': 0.223 + i * 0.25, 'text': f'内容{i}', 'words': []} for i in range(40)]
        transcript = folder / 'transcript.json'
        app.atomic_write_json(transcript, {'segments': self.segments})
        self.project['transcript'] = {'path': str(transcript)}
        self.project['edit'].update(leadPaddingMs=0, tailPaddingMs=0, pauseThresholdMs=0)
        app.save_project(self.project)

    def export(self, run=None):
        job = app.set_job(self.project['id'], 'render', 'running')
        self.addCleanup(lambda: app.JOBS.pop(job['id'], None))
        with mock.patch.object(app, 'nvenc_available', return_value=False), mock.patch.object(pipeline, 'MAX_PIECES', 4):
            if run:
                with mock.patch.object(app, 'run_logged', side_effect=run):
                    app.render_worker(self.project['id'], job['id'], False)
            else:
                app.render_worker(self.project['id'], job['id'], False)
        return app.load_project(self.project['id'])['outputs']['final']

    def test_real_many_cut_export_has_no_cumulative_av_drift_and_reuses_after_edit(self):
        result = self.export()
        self.assertEqual(result['batchCount'], 10)
        self.assertAlmostEqual(result['duration'], 4.92, delta=0.02)
        video = subprocess.run([
            self.ffmpeg, '-v', 'error', '-i', result['videoPath'], '-an', '-vf', 'scale=1:1',
            '-pix_fmt', 'gray', '-f', 'rawvideo', 'pipe:1',
        ], capture_output=True, check=True).stdout
        audio = subprocess.run([
            self.ffmpeg, '-v', 'error', '-i', result['videoPath'], '-vn', '-ac', '1', '-ar', '48000',
            '-f', 'f32le', 'pipe:1',
        ], capture_output=True, check=True).stdout
        self.assertEqual(len(video), 123)
        self.assertAlmostEqual(len(audio) / 4 / 48000, len(video) / 25, delta=0.05)
        project = app.load_project(self.project['id'])
        project['editRevision'] = 1
        project['subtitleEdits'] = {'s00000': '只修改字幕'}
        app.save_project(project)
        commands = []
        real_run = app.run_logged
        def record(command, *args, **kwargs):
            commands.append(command)
            return real_run(command, *args, **kwargs)
        reused = self.export(record)
        self.assertEqual(reused['cachedBatches'], reused['batchCount'])
        self.assertEqual(len(commands), 1, '只改字幕时仅需合并已有分段')
        self.assertIn('只修改字幕', Path(reused['subtitlePath']).read_text(encoding='utf-8-sig'))
        project = app.load_project(self.project['id'])
        project['edit']['deletedSegmentIds'] = ['s00020']
        project['editRevision'] += 1
        app.save_project(project)
        changed = self.export()
        self.assertGreater(changed['cachedBatches'], 0)
        self.assertLess(changed['cachedBatches'], changed['batchCount'])
        self.assertAlmostEqual(changed['duration'], 39 * 0.123, delta=0.04)

    def test_cancelled_export_keeps_completed_cache_and_previous_output(self):
        old = app.project_dir(self.project['id']) / 'old.mp4'
        old.write_bytes(b'previous output')
        self.project['outputs']['final'] = {'videoPath': str(old)}
        app.save_project(self.project)
        real_set_job = app.set_job
        def cancel_after_first(*args, **kwargs):
            result = real_set_job(*args, **kwargs)
            if kwargs.get('completedBatches') == 1 and kwargs.get('processedSeconds', 0) > 0:
                app.JOBS[kwargs['jobId']]['cancelRequested'] = True
            return result
        with mock.patch.object(app, 'set_job', side_effect=cancel_after_first):
            with self.assertRaises(app.JobCancelled):
                self.export()
        self.assertEqual(old.read_bytes(), b'previous output')
        self.assertEqual(len(list((old.parent / 'cache' / 'render-v1').glob('*.nut'))), 1)
        result = self.export()
        self.assertEqual(result['cachedBatches'], 1)

    def test_failed_join_preserves_previous_output_and_retries_from_cache(self):
        old = app.project_dir(self.project['id']) / 'old.mp4'
        old.write_bytes(b'previous output')
        self.project['outputs']['final'] = {'videoPath': str(old)}
        app.save_project(self.project)
        real_run = app.run_logged
        def fail_join(command, *args, **kwargs):
            if 'concat' in command:
                raise RuntimeError('合并失败')
            return real_run(command, *args, **kwargs)
        with self.assertRaisesRegex(RuntimeError, '合并失败'):
            self.export(fail_join)
        self.assertEqual(old.read_bytes(), b'previous output')
        result = self.export()
        self.assertEqual(result['cachedBatches'], result['batchCount'])

    def test_edits_during_export_do_not_replace_the_render_snapshot(self):
        real_run = app.run_logged
        changed = False
        def edit_during_render(command, *args, **kwargs):
            nonlocal changed
            if not changed:
                latest = app.load_project(self.project['id'])
                latest['editRevision'] = 9
                latest['subtitleEdits'] = {'s00000': '新版本的字幕'}
                app.save_project(latest)
                changed = True
            return real_run(command, *args, **kwargs)
        result = self.export(edit_during_render)
        latest = app.load_project(self.project['id'])
        self.assertEqual(latest['editRevision'], 9)
        self.assertEqual(result['editRevision'], 0)
        self.assertNotIn('新版本的字幕', Path(result['subtitlePath']).read_text(encoding='utf-8-sig'))


if __name__ == '__main__':
    unittest.main()
