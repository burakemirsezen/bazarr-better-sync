"""Outbound-only HTTP client for the coordinator's /worker/v1 API."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from ..protocol import PROTOCOL_VERSION


class CoordinatorError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f'{status}: {message}')
        self.status = status


class Coordinator:
    def __init__(self, url: str, token: str, timeout: float = 60):
        if not url.startswith(('http://', 'https://')):
            raise ValueError('Coordinator URL must be http(s)')
        self.url, self.token, self.timeout = url.rstrip('/'), token, timeout

    def _request(self, method, route, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.url + route, data=data, method=method, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json', **(headers or {})})
        try:
            return urlopen(request, timeout=self.timeout)
        except HTTPError as error:
            with error:
                try:
                    message = json.loads(error.read()).get('error', '')
                except ValueError:
                    message = ''
            raise CoordinatorError(error.code, message) from None

    def _json(self, method, route, body=None):
        with self._request(method, route, body) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def claim(self):
        with self._request('POST', '/worker/v1/claim', {'protocol': PROTOCOL_VERSION}) as response:
            return None if response.status == 204 else json.loads(response.read())

    def download_audio(self, claim, destination: Path):
        """Stream the job's audio artifact and verify its checksum; partial files are never kept."""
        digest, size = hashlib.sha256(), 0
        partial = destination.with_suffix('.part')
        try:
            with self._request('GET', f"/worker/v1/jobs/{claim['job_id']}/audio",
                               headers={'X-BBS-Lease': claim['lease_token']}) as response, partial.open('wb') as out:
                while block := response.read(1024 * 1024):
                    digest.update(block); size += len(block); out.write(block)
            if digest.hexdigest() != claim['audio']['sha256'] or size != claim['audio']['size_bytes']:
                raise RuntimeError('Downloaded audio does not match the announced checksum')
            partial.replace(destination)
        finally:
            partial.unlink(missing_ok=True)

    def heartbeat(self, claim):
        return self._json('POST', f"/worker/v1/jobs/{claim['job_id']}/heartbeat", {'lease_token': claim['lease_token']})

    def result(self, result):
        return self._json('POST', f"/worker/v1/jobs/{result['job_id']}/result", result)

    def fail(self, claim, reason):
        return self._json('POST', f"/worker/v1/jobs/{claim['job_id']}/fail",
                          {'lease_token': claim['lease_token'], 'reason': str(reason)[:1000]})
