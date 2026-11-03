"""The worker loop as a service with observable state, shared by the CLI and the Windows tray app."""
from __future__ import annotations

import json
import logging
from pathlib import Path
import shutil
import tempfile
import threading
import time

from .client import Coordinator, CoordinatorError

log = logging.getLogger('bazarr_sync.worker')

STATES = ('starting', 'idle', 'working', 'paused', 'offline', 'error')


def event(**fields):
    log.info(json.dumps({'time': time.strftime('%Y-%m-%dT%H:%M:%S'), **fields}))


class LeaseLost(RuntimeError):
    pass


class Heartbeat:
    """Renews the lease in the background; stops the job if the coordinator revokes it."""

    def __init__(self, coordinator, claim):
        self.coordinator, self.claim = coordinator, claim
        self.lost = threading.Event()
        self.stopped = threading.Event()
        self.interval = max(10, claim['lease_seconds'] / 3)
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stopped.wait(self.interval):
            try:
                self.coordinator.heartbeat(self.claim)
            except CoordinatorError as error:
                if error.status in (403, 409):
                    self.lost.set()
                    return
            except OSError:
                pass  # Transient network loss; the lease survives until it expires.

    def check(self):
        if self.lost.is_set():
            raise LeaseLost('Coordinator revoked or expired the lease')

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stopped.set()


class WorkerService:
    def __init__(self, coordinator: Coordinator, model_cache: Path, alass: Path, scratch: Path,
                 poll_seconds: float = 30, on_change=None, engine_factory=None, unload_after_seconds: float = 600):
        self.coordinator, self.model_cache, self.alass, self.scratch = coordinator, Path(model_cache), Path(alass), Path(scratch)
        self.poll_seconds = poll_seconds
        self.on_change = on_change or (lambda status: None)
        self.engine_factory = engine_factory
        self.engine = None
        self.unload_after_seconds = unload_after_seconds
        self.last_job_finished = time.monotonic()
        self.paused = threading.Event()
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.status = {'state': 'starting', 'detail': 'Starting', 'job': None, 'last_result': None}

    def set_state(self, state, detail, **extra):
        if state != self.status['state']:
            event(stage='state', state=state, detail=detail)
        self.status = {**self.status, 'state': state, 'detail': detail, **extra}
        self.on_change(dict(self.status))

    def pause(self):
        self.paused.set()
        self.wake.set()
        if self.status['state'] != 'working':
            self.set_state('paused', 'Paused; queued jobs wait on the server')

    def resume(self):
        self.paused.clear()
        self.wake.set()

    def stop(self):
        self.stopping.set()
        self.wake.set()

    def cleanup_scratch(self):
        """Startup cleanup: only our own job-* folders, left behind by a crash."""
        self.scratch.mkdir(parents=True, exist_ok=True)
        for stale in self.scratch.glob('job-*'):
            shutil.rmtree(stale, ignore_errors=True)

    def load_engine(self):
        if self.engine is None:
            self.set_state('working', 'Loading speech models onto the GPU')
            if self.engine_factory is not None:
                self.engine = self.engine_factory()
            else:
                from .gpu import SpeechEngine
                self.engine = SpeechEngine(self.model_cache)
            event(stage='models_loaded', **self.engine.describe())
        return self.engine

    def unload_engine(self):
        """Give the VRAM back while idle; the next job reloads the models (~15 s)."""
        if self.engine is None:
            return
        self.engine = None
        try:
            import gc
            import torch
            gc.collect()
            torch.cuda.empty_cache()
        except ImportError:
            pass
        event(stage='models_unloaded')

    def process(self, claim):
        from .pipeline import run
        # A model/GPU failure is not the job's fault: raise before touching the job so its
        # lease simply expires and the coordinator requeues it.
        engine = self.load_engine()
        scratch = Path(tempfile.mkdtemp(prefix='job-', dir=self.scratch))
        started = time.time()
        try:
            minutes = claim['audio']['duration_seconds'] / 60
            self.set_state('working', f'Syncing a {minutes:.0f}-minute episode', job=claim['job_id'])
            with Heartbeat(self.coordinator, claim) as beat:
                audio = scratch / 'audio.wav'
                self.coordinator.download_audio(claim, audio)
                event(stage='audio_downloaded', job=claim['job_id'], seconds=claim['audio']['duration_seconds'])
                result = run(claim, audio, engine, self.alass, scratch, heartbeat=beat.check)
                beat.check()
            ack = self.coordinator.result(result)
            outcome = {'job': claim['job_id'], 'status': ack.get('status'), 'direct_cues': result['diagnostics']['direct_cues'],
                       'cues': len(result['timings']), 'seconds': round(time.time() - started)}
            event(stage='result_accepted', **outcome)
            return outcome
        except LeaseLost as error:
            event(stage='lease_lost', job=claim['job_id'], error=str(error))
            return {'job': claim['job_id'], 'status': 'lease_lost'}
        except Exception as error:
            event(stage='job_failed', job=claim['job_id'], error=str(error)[:500])
            try:
                self.coordinator.fail(claim, f'{type(error).__name__}: {error}')
            except (CoordinatorError, OSError):
                pass  # The lease expires and the coordinator requeues within its attempt limit.
            return {'job': claim['job_id'], 'status': 'failed', 'error': str(error)[:300]}
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def run(self, once=False):
        """Poll for work until stopped. With once=True, process at most one job then return."""
        event(stage='worker_started', coordinator=self.coordinator.url if hasattr(self.coordinator, 'url') else None)
        self.cleanup_scratch()
        while not self.stopping.is_set():
            if self.engine is not None and time.monotonic() - self.last_job_finished > self.unload_after_seconds:
                self.unload_engine()
            if self.paused.is_set():
                self.set_state('paused', 'Paused; queued jobs wait on the server')
                self.wake.wait(self.poll_seconds)
                self.wake.clear()
                continue
            try:
                claim = self.coordinator.claim()
            except (CoordinatorError, OSError) as error:
                event(stage='coordinator_unreachable', error=str(error)[:300])
                self.set_state('offline', 'Cannot reach the coordinator; retrying')
                claim = None
            else:
                if claim is None:
                    self.set_state('idle', 'Waiting for jobs')
            if claim is None:
                if once:
                    return None
                self.wake.wait(self.poll_seconds)
                self.wake.clear()
                continue
            event(stage='claimed', job=claim['job_id'], cues=len(claim['cues']))
            try:
                outcome = self.process(claim)
            except Exception as error:  # Model load failure: report it, stay alive.
                event(stage='worker_error', error=str(error)[:500])
                self.set_state('error', f'Worker error: {str(error)[:120]}', job=None)
                outcome = None
                self.wake.wait(self.poll_seconds)
                self.wake.clear()
            else:
                self.set_state('idle', 'Waiting for jobs', job=None, last_result=outcome)
            self.last_job_finished = time.monotonic()
            if once:
                return outcome
        self.unload_engine()
        return None
