"""Desktop worker service loop with a fake coordinator and engine (no GPU, no network)."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from bazarr_sync.worker.client import CoordinatorError
from bazarr_sync.worker.service import WorkerService
from bazarr_sync.worker.settings import Settings, autostart_command


def claim(job='job-1'):
    return {'job_id': job, 'lease_token': 't', 'lease_seconds': 300,
            'audio': {'duration_seconds': 1680, 'sha256': 'x', 'size_bytes': 1}, 'cues': [{'index': 0}]}


class FakeCoordinator:
    def __init__(self, claims):
        self.claims, self.results, self.failures = list(claims), [], []

    def claim(self):
        item = self.claims.pop(0) if self.claims else None
        if isinstance(item, Exception):
            raise item
        return item

    def download_audio(self, claim, destination):
        destination.write_bytes(b'audio')

    def heartbeat(self, claim):
        return {}

    def result(self, result):
        self.results.append(result)
        return {'status': 'awaiting_apply'}

    def fail(self, claim, reason):
        self.failures.append(reason)


class FakeEngine:
    def describe(self):
        return {'device': 'fake'}


def fake_run(claim, audio, engine, alass, scratch, heartbeat):
    assert audio.read_bytes() == b'audio'
    heartbeat()
    return {'job_id': claim['job_id'], 'timings': [{}], 'diagnostics': {'direct_cues': 1}}


class WorkerServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.scratch = Path(self.temp.name) / 'scratch'
        self.states = []

    def tearDown(self):
        self.temp.cleanup()

    def service(self, coordinator, engine_factory=FakeEngine, **kwargs):
        return WorkerService(coordinator, Path('models'), Path('alass.exe'), self.scratch, poll_seconds=0.01,
                             on_change=lambda s: self.states.append(s['state']), engine_factory=engine_factory, **kwargs)

    def test_idle_when_queue_empty(self):
        service = self.service(FakeCoordinator([None]))
        self.assertIsNone(service.run(once=True))
        self.assertEqual(service.status['state'], 'idle')

    def test_job_result_is_posted_and_scratch_cleaned(self):
        (self.scratch / 'job-stale').mkdir(parents=True)
        coordinator = FakeCoordinator([claim()])
        service = self.service(coordinator)
        with patch('bazarr_sync.worker.pipeline.run', fake_run):
            outcome = service.run(once=True)
        self.assertEqual(outcome['status'], 'awaiting_apply')
        self.assertEqual(len(coordinator.results), 1)
        self.assertIn('working', self.states)
        self.assertEqual(service.status['state'], 'idle')
        self.assertEqual(list(self.scratch.iterdir()), [])

    def test_pipeline_error_reports_failure(self):
        coordinator = FakeCoordinator([claim()])
        service = self.service(coordinator)
        with patch('bazarr_sync.worker.pipeline.run', side_effect=RuntimeError('boom')):
            outcome = service.run(once=True)
        self.assertEqual(outcome['status'], 'failed')
        self.assertIn('boom', coordinator.failures[0])

    def test_model_failure_does_not_fail_the_job(self):
        coordinator = FakeCoordinator([claim()])
        def broken():
            raise RuntimeError('No GPU available')
        service = self.service(coordinator, engine_factory=broken)
        service.run(once=True)
        self.assertEqual(coordinator.failures, [])
        self.assertEqual(service.status['state'], 'error')

    def test_unreachable_coordinator_is_offline(self):
        service = self.service(FakeCoordinator([OSError('refused')]))
        service.run(once=True)
        self.assertEqual(service.status['state'], 'offline')

    def test_pause_stops_claiming_until_resumed(self):
        coordinator = FakeCoordinator([claim()])
        service = self.service(coordinator)
        service.pause()
        thread = threading.Thread(target=service.run, daemon=True)
        thread.start()
        thread.join(0.2)
        self.assertEqual(service.status['state'], 'paused')
        self.assertEqual(len(coordinator.claims), 1)
        service.stop()
        thread.join(1)

    def test_models_unloaded_after_idle_period(self):
        service = self.service(FakeCoordinator([claim(), None]), unload_after_seconds=0)
        with patch('bazarr_sync.worker.pipeline.run', fake_run):
            service.run(once=True)
        self.assertIsNotNone(service.engine)
        service.run(once=True)
        self.assertIsNone(service.engine)


class SettingsTests(unittest.TestCase):
    def test_load_config_and_token_from_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            # Windows PowerShell 5.1 writes UTF-8 with a BOM.
            path.write_text(json.dumps({'coordinator_url': 'https://worker.example', 'review_url': 'https://review.example'}),
                            encoding='utf-8-sig')
            with patch.dict(os.environ, {'BBS_WORKER_TOKEN': 'secret'}):
                settings = Settings.load(path)
        self.assertEqual(settings.token, 'secret')
        self.assertEqual(settings.alass, Path(directory) / 'alass' / 'alass-cli.exe')
        self.assertEqual(settings.model_cache, Path(directory) / 'models')

    def test_autostart_runs_tray_without_console(self):
        command = autostart_command()
        self.assertTrue(command.endswith('-m bazarr_sync.worker.tray'))
        if os.name == 'nt':
            self.assertIn('pythonw.exe', command)


if __name__ == '__main__':
    unittest.main()
