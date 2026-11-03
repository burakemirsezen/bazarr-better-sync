"""Run the desktop GPU worker: python -m bazarr_sync.worker --coordinator URL --model-cache DIR --alass PATH

The bearer token comes from BBS_WORKER_TOKEN (never a command-line argument, so
it stays out of process listings). The worker only connects outward.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time

from .client import Coordinator, CoordinatorError


def log(**event):
    print(json.dumps({'time': time.strftime('%Y-%m-%dT%H:%M:%S'), **event}), flush=True)


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


class LeaseLost(RuntimeError):
    pass


def process(coordinator, claim, engine, alass, scratch_root):
    from .pipeline import run
    scratch = Path(tempfile.mkdtemp(prefix='job-', dir=scratch_root))
    try:
        with Heartbeat(coordinator, claim) as beat:
            audio = scratch / 'audio.wav'
            coordinator.download_audio(claim, audio)
            log(stage='audio_downloaded', job=claim['job_id'], seconds=claim['audio']['duration_seconds'])
            result = run(claim, audio, engine, alass, scratch, heartbeat=beat.check)
            beat.check()
        ack = coordinator.result(result)
        log(stage='result_accepted', job=claim['job_id'], status=ack.get('status'),
            direct_cues=result['diagnostics']['direct_cues'], cues=len(result['timings']),
            seconds=result['diagnostics']['total_seconds'])
    except LeaseLost as error:
        log(stage='lease_lost', job=claim['job_id'], error=str(error))
    except Exception as error:
        log(stage='job_failed', job=claim['job_id'], error=str(error)[:500])
        try:
            coordinator.fail(claim, f'{type(error).__name__}: {error}')
        except (CoordinatorError, OSError):
            pass  # The lease expires and the coordinator requeues within its attempt limit.
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--coordinator', default=os.getenv('BBS_COORDINATOR_URL'), required=os.getenv('BBS_COORDINATOR_URL') is None)
    parser.add_argument('--model-cache', type=Path, required=True)
    parser.add_argument('--alass', type=Path, required=True, help='Path to alass-cli executable')
    parser.add_argument('--scratch', type=Path, default=Path(tempfile.gettempdir()) / 'bazarr-sync-worker')
    parser.add_argument('--poll-seconds', type=float, default=30)
    parser.add_argument('--once', action='store_true', help='Process at most one job, then exit')
    args = parser.parse_args(argv)
    token = os.getenv('BBS_WORKER_TOKEN')
    if not token:
        parser.error('Set BBS_WORKER_TOKEN')
    if not args.alass.is_file():
        parser.error('alass-cli not found')
    args.scratch.mkdir(parents=True, exist_ok=True)
    # Startup cleanup: only our own job-* folders, left behind by a crash.
    for stale in args.scratch.glob('job-*'):
        shutil.rmtree(stale, ignore_errors=True)
    coordinator = Coordinator(args.coordinator, token)
    engine = None
    while True:
        try:
            claim = coordinator.claim()
        except (CoordinatorError, OSError) as error:
            log(stage='coordinator_unreachable', error=str(error)[:300])
            claim = None
        if claim is None:
            if args.once:
                return 0
            time.sleep(args.poll_seconds)
            continue
        log(stage='claimed', job=claim['job_id'], cues=len(claim['cues']))
        if engine is None:
            from .gpu import SpeechEngine
            engine = SpeechEngine(args.model_cache)
            log(stage='models_loaded', **engine.describe())
        process(coordinator, claim, engine, args.alass, args.scratch)
        if args.once:
            return 0


if __name__ == '__main__':
    sys.exit(main())
