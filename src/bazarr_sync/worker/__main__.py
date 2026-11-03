"""Run the desktop GPU worker in a console: python -m bazarr_sync.worker [--once]

Settings come from %LOCALAPPDATA%\\BazarrSyncWorker\\config.json (written by the
installer); flags override them. The bearer token comes from BBS_WORKER_TOKEN,
never a command-line argument, so it stays out of process listings. The worker
only connects outward. For the notification-area app use bazarr_sync.worker.tray.
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys

from .client import Coordinator
from .service import WorkerService
from .settings import Settings, app_dir, user_environment


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--coordinator')
    parser.add_argument('--model-cache', type=Path)
    parser.add_argument('--alass', type=Path, help='Path to alass-cli executable')
    parser.add_argument('--scratch', type=Path)
    parser.add_argument('--poll-seconds', type=float, default=30)
    parser.add_argument('--once', action='store_true', help='Process at most one job, then exit')
    args = parser.parse_args(argv)
    config = app_dir() / 'config.json'
    settings = Settings.load(config) if config.is_file() else None
    coordinator = args.coordinator or (settings and settings.coordinator_url)
    model_cache = args.model_cache or (settings and settings.model_cache)
    alass = args.alass or (settings and settings.alass)
    scratch = args.scratch or (settings and settings.scratch) or app_dir() / 'scratch'
    token = os.getenv('BBS_WORKER_TOKEN') or (settings.token if settings else user_environment('BBS_WORKER_TOKEN'))
    if not (coordinator and model_cache and alass):
        parser.error('Run the installer or pass --coordinator, --model-cache and --alass')
    if not token:
        parser.error('Set BBS_WORKER_TOKEN')
    if not Path(alass).is_file():
        parser.error('alass-cli not found')
    logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stdout)
    service = WorkerService(Coordinator(coordinator, token), model_cache, alass, scratch, poll_seconds=args.poll_seconds)
    try:
        service.run(once=args.once)
    except KeyboardInterrupt:
        service.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
