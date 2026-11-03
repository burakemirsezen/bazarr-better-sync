"""Per-user worker settings and Windows start-at-login registration.

Layout under %LOCALAPPDATA%\\BazarrSyncWorker (written by worker/windows/install.ps1):
  config.json   coordinator_url, review_url, model_cache, alass, scratch
  logs/         rotating worker.log
The bearer token is read from the BBS_WORKER_TOKEN environment variable, never from this file.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys

APP_NAME = 'BazarrSyncWorker'
RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'


def app_dir() -> Path:
    base = os.getenv('LOCALAPPDATA') or str(Path.home() / '.local' / 'share')
    return Path(base) / APP_NAME


@dataclass
class Settings:
    coordinator_url: str
    model_cache: Path
    alass: Path
    scratch: Path
    review_url: str = ''
    token: str = ''

    @classmethod
    def load(cls, path: Path | None = None) -> 'Settings':
        path = path or app_dir() / 'config.json'
        data = json.loads(path.read_text(encoding='utf-8-sig'))  # Windows PowerShell 5.1 writes a BOM
        root = path.parent
        return cls(coordinator_url=data['coordinator_url'], review_url=data.get('review_url', ''),
                   model_cache=Path(data.get('model_cache') or root / 'models'),
                   alass=Path(data.get('alass') or root / 'alass' / 'alass-cli.exe'),
                   scratch=Path(data.get('scratch') or root / 'scratch'),
                   token=os.getenv('BBS_WORKER_TOKEN') or user_environment('BBS_WORKER_TOKEN'))


def user_environment(name: str) -> str:
    """Read a persistent user environment variable even if this process started before it was set."""
    if sys.platform != 'win32':
        return ''
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            return str(winreg.QueryValueEx(key, name)[0])
    except OSError:
        return ''


def autostart_command() -> str:
    """pythonw (no console window) running the tray app from this same environment."""
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    interpreter = pythonw if pythonw.is_file() else Path(sys.executable)
    return f'"{interpreter}" -m bazarr_sync.worker.tray'


def autostart_enabled() -> bool:
    if sys.platform != 'win32':
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return bool(winreg.QueryValueEx(key, APP_NAME)[0])
    except OSError:
        return False


def set_autostart(enabled: bool):
    if sys.platform != 'win32':
        raise RuntimeError('Start at login is only supported on Windows')
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
