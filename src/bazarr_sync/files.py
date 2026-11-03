"""Original preservation and guarded atomic replacement for the single-instance worker."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sync_directory(path: Path):
    if os.name == 'nt':
        return  # Windows does not expose portable directory fsync through Python.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _locked(path: Path):
    lock = path.with_name('.' + path.name + '.bazarr-sync.lock')
    try:
        descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise ValueError('Subtitle is locked; inspect interrupted work before removing its lock') from error
    try:
        with os.fdopen(descriptor, 'w') as handle:
            handle.write(str(os.getpid()))
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        lock.unlink(missing_ok=True)


def apply_result(path: str | Path, output: bytes, expected_sha256: str, backup_dir: str | Path) -> dict:
    """Back up exact original and replace only the expected version.

    The lock serializes this application's writers. Bazarr/external editors do
    not honor it: hash/stat rechecks narrow races but shared storage has no
    portable compare-and-swap rename. Deployment must avoid concurrent writers.
    Backups are never automatically expired.
    """
    submitted = Path(path).absolute()
    if submitted.is_symlink() or not submitted.is_file():
        raise ValueError('Expected a regular subtitle file, not a symlink')
    path = submitted.resolve(strict=True)
    if not isinstance(output, bytes):
        raise ValueError('Output must contain subtitle bytes')
    backup_dir = Path(backup_dir).resolve()
    backup_dir.mkdir(parents=True, exist_ok=True)
    with _locked(path):
        before = path.stat()
        original = path.read_bytes()
        if sha256(original) != expected_sha256:
            raise ValueError('Subtitle changed since analysis; refusing stale replacement')
        identifier = uuid.uuid4().hex
        backup = backup_dir / (identifier + path.suffix)
        with backup.open('xb') as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        _sync_directory(backup_dir)
        descriptor, temporary = tempfile.mkstemp(prefix='.' + path.name + '.', suffix='.tmp', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(output)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, stat.S_IMODE(before.st_mode))
            after = path.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or sha256(path.read_bytes()) != expected_sha256:
                raise ValueError('Subtitle changed during publication')
            record = {'path': str(path), 'backup': str(backup), 'original_sha256': expected_sha256,
                      'applied_sha256': sha256(output), 'id': identifier, 'status': 'prepared'}
            metadata = backup_dir / (identifier + '.json')
            # Persist recovery information before publication. A crash may leave
            # prepared metadata; compare the current hash to determine outcome.
            with metadata.open('x', encoding='utf-8') as handle:
                json.dump(record, handle)
                handle.flush()
                os.fsync(handle.fileno())
            _sync_directory(backup_dir)
            os.replace(temporary, path)
            _sync_directory(path.parent)
            record['status'] = 'applied'
            return record
        finally:
            Path(temporary).unlink(missing_ok=True)


def rollback(record: dict) -> dict:
    backup = Path(record['backup'])
    original = backup.read_bytes()
    if sha256(original) != record['original_sha256']:
        raise ValueError('Backup integrity check failed')
    return apply_result(record['path'], original, record['applied_sha256'], backup.parent)
