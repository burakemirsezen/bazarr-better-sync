"""Windows notification-area (tray) app around the GPU worker: pythonw -m bazarr_sync.worker.tray"""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import os
import sys
import threading
import webbrowser

from .client import Coordinator
from .service import WorkerService
from .settings import Settings, app_dir, autostart_enabled, set_autostart

COLOURS = {'starting': '#868e96', 'idle': '#2f9e44', 'working': '#1f6feb', 'paused': '#868e96',
           'offline': '#f08c00', 'error': '#e03131'}


def icon_image(state: str):
    from PIL import Image, ImageDraw
    size = 64
    image = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, size - 4, size - 4), radius=14, fill=COLOURS.get(state, '#868e96'))
    # Two subtitle lines; the lower one shifted, standing for re-timing.
    draw.rounded_rectangle((14, 22, 50, 30), radius=4, fill='white')
    draw.rounded_rectangle((20, 36, 56 if state == 'working' else 46, 44), radius=4, fill='white')
    return image


def single_instance() -> bool:
    if sys.platform != 'win32':
        return True
    import ctypes
    ctypes.windll.kernel32.CreateMutexW(None, False, 'Local\\BazarrSyncWorker')
    return ctypes.windll.kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def configure_logging():
    logs = app_dir() / 'logs'
    logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs / 'worker.log', maxBytes=2_000_000, backupCount=3, encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(message)s'))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def main():
    import pystray
    if not single_instance():
        return 0
    configure_logging()
    status = {'state': 'starting', 'detail': 'Starting'}
    service = None
    try:
        settings = Settings.load()
        if not settings.token:
            raise ValueError('BBS_WORKER_TOKEN is not set for this user')
        service = WorkerService(Coordinator(settings.coordinator_url, settings.token), settings.model_cache,
                                settings.alass, settings.scratch)
    except (OSError, ValueError, KeyError) as error:
        settings = None
        status = {'state': 'error', 'detail': f'Not configured: {error}'.replace(str(app_dir()), '%LOCALAPPDATA%\\BazarrSyncWorker')}
        logging.getLogger('bazarr_sync.worker').error(status['detail'])

    def label(_=None):
        return status['detail'][:120]

    def on_change(new):
        previous = status.get('state')
        status.update(new)
        icon.icon = icon_image(new['state'])
        icon.title = 'Bazarr Sync: ' + label()
        icon.update_menu()
        result = new.get('last_result') or {}
        if previous == 'working' and new['state'] == 'idle' and result.get('status') in ('awaiting_apply', 'applied', 'unchanged'):
            message = {'awaiting_apply': 'A re-timed subtitle is ready for review.',
                       'applied': 'A re-timed subtitle was applied.',
                       'unchanged': 'An episode was already in sync.'}[result['status']]
            try:
                icon.notify(message, 'Bazarr Sync')
            except Exception:
                pass

    def toggle_pause(_icon, _item):
        if service.paused.is_set():
            service.resume()
        else:
            service.pause()

    def toggle_autostart(_icon, _item):
        set_autostart(not autostart_enabled())
        icon.update_menu()

    def quit_app(_icon, _item):
        if service:
            service.stop()
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem(label, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Open review page', lambda *_: webbrowser.open(settings.review_url),
                         visible=lambda _: bool(settings and settings.review_url), default=True),
        pystray.MenuItem(lambda _: 'Resume' if service and service.paused.is_set() else 'Pause', toggle_pause,
                         visible=lambda _: service is not None),
        pystray.MenuItem('Start at login', toggle_autostart, checked=lambda _: autostart_enabled()),
        pystray.MenuItem('Open logs', lambda *_: os.startfile(app_dir() / 'logs')),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem('Quit', quit_app),
    )
    icon = pystray.Icon('BazarrSyncWorker', icon_image(status['state']), 'Bazarr Sync: ' + label(), menu)

    def setup(tray):
        tray.visible = True
        if service:
            service.on_change = on_change
            threading.Thread(target=service.run, name='worker', daemon=True).start()

    icon.run(setup=setup)
    return 0


if __name__ == '__main__':
    sys.exit(main())
