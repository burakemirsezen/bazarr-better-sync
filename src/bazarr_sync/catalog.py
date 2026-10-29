"""Read-only Bazarr metadata discovery. Import never opens media files."""
import hashlib
import json
from pathlib import PurePosixPath
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward Bazarr's credential to another server.
        return None


class BazarrClient:
    def __init__(self, url, api_key, timeout=15, page_size=100):
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.query or parsed.fragment:
            raise ValueError("Configure an http(s) Bazarr base URL")
        self.url, self.api_key, self.timeout, self.page_size = url.rstrip("/"), api_key, timeout, page_size
        self.opener = build_opener(NoRedirect())

    def get(self, route, params):
        request = Request(self.url + "/api/" + route + "?" + urlencode(params, doseq=True),
                          headers={"X-API-KEY": self.api_key, "Accept": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(32 * 1024 * 1024 + 1)
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError("Bazarr metadata response exceeds 32 MiB")
            return json.loads(raw)
        except Exception as exc:
            # Request exceptions can include URL details. Keep credentials/config out of logs.
            raise RuntimeError("Bazarr metadata request failed; check URL, API key and connectivity") from None

    def pages(self, route, **params):
        start, seen = 0, set()
        for _ in range(10000):
            envelope = self.get(route, dict(params, start=start, length=self.page_size))
            data = envelope.get("data", []) if isinstance(envelope, dict) else envelope
            if not isinstance(data, list):
                raise ValueError("Unexpected Bazarr list response")
            if not data:
                return
            signature = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            if signature in seen:
                raise ValueError("Bazarr returned a repeated metadata page")
            seen.add(signature)
            yield from data
            start += len(data)
            total = envelope.get("total") if isinstance(envelope, dict) else None
            if (isinstance(total, int) and start >= total) or len(data) < self.page_size or (route == "episodes" and total is None):
                return
        raise ValueError("Bazarr pagination limit exceeded")

    def discover(self):
        for movie in self.pages("movies"):
            yield from targets(movie, "movie")
        for series in self.pages("series"):
            sid = series.get("sonarrSeriesId")
            if sid is None:
                continue
            # Bazarr 1.5.6 expects bracket-array seriesid[] and returns all episodes
            # for that one series (no total/pagination envelope).
            for episode in self.pages("episodes", **{"seriesid[]": sid}):
                episode = dict(episode, sonarrSeriesId=sid,
                               seriesTitle=series.get("title", series.get("seriesTitle", "")))
                yield from targets(episode, "episode")


def targets(item, kind):
    media = item.get("path", "")
    source_id = item.get("radarrId" if kind == "movie" else "sonarrEpisodeId")
    subtitles = item.get("subtitles") or []
    for subtitle in subtitles:
        if not isinstance(subtitle, dict):
            continue
        language = subtitle.get("code2") or subtitle.get("language") or "unknown"
        if isinstance(language, dict):
            language = language.get("code2") or language.get("code3") or language.get("name", "unknown")
        path = subtitle.get("path") or ""
        embedded = bool(subtitle.get("embedded") or subtitle.get("isEmbedded") or subtitle.get("stream"))
        if not path or not isinstance(path, str):
            embedded = True
            path = ""
        ext = PurePosixPath(path.replace("\\", "/")).suffix.lower()
        reason = None
        if embedded:
            reason = "Embedded subtitles are catalog-only"
        elif str(language).lower() not in {"en", "eng", "english"}:
            reason = "Only English subtitles are supported"
        elif ext not in {".srt", ".ass", ".ssa"}:
            reason = "Unsupported subtitle format"
        elif not media:
            reason = "Media path is missing"
        key = json.dumps([kind, source_id, path, language, subtitle.get("stream")], sort_keys=True)
        yield {"id": hashlib.sha256(key.encode()).hexdigest()[:24], "kind": kind,
               "source_id": source_id, "series_id": item.get("sonarrSeriesId"),
               "season": item.get("season"), "episode": item.get("episode"),
               "title": item.get("title", ""), "series_title": item.get("seriesTitle", ""),
               "media_path": media, "subtitle_path": path, "language": language,
               "format": ext.lstrip("."), "eligible": reason is None, "reason": reason,
               "assessment": "unassessed", "present": True}
