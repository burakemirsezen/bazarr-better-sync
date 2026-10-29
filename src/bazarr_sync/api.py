"""Single-instance HTTP service with a durable queue and review-only worker."""
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import uuid
from urllib.parse import urlsplit

from .catalog import BazarrClient
from .state import State


def file_identity(path):
    info = Path(path).stat()
    return {"size": info.st_size, "mtime_ns": info.st_mtime_ns, "inode": info.st_ino}


def subtitle_hash(path):
    if Path(path).stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Subtitle exceeds 8 MiB limit")
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Application:
    def __init__(self, config):
        self.config = config
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self.instance_lock = (config.data_dir / "instance.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.instance_lock.write(b"0")
                self.instance_lock.flush()
                self.instance_lock.seek(0)
                msvcrt.locking(self.instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.instance_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.instance_lock.close()
            raise RuntimeError("Another application instance owns this data directory") from None
        self.state = State(config.data_dir)
        self.mutation_lock = threading.RLock()
        self.stopping = threading.Event()
        self.worker = None
        self.worker_error = None

    def prepare(self, payload, *, local_paths=False):
        # Only internal callers may identify paths already mapped at submission.
        # HTTP/catalog payload fields never control this keyword argument.
        media = self.config.map_path(payload.get("media_path"), apply_mapping=not local_paths)
        subtitle = self.config.map_path(payload.get("subtitle_path"), apply_mapping=not local_paths)
        if media == subtitle:
            raise ValueError("Media and subtitle must be different files")
        if subtitle.suffix.lower() not in {".srt", ".ass", ".ssa"}:
            raise ValueError("Only SRT/ASS/SSA subtitles are supported")
        if str(payload.get("language", "en")).lower() not in {"en", "eng", "english"}:
            raise ValueError("Only English subtitles are supported")
        track = payload.get("audio_index")
        if track is not None and (type(track) is not int or track < 0):
            raise ValueError("Audio stream index must be a nonnegative integer")
        return {"media_path": str(media), "subtitle_path": str(subtitle), "language": "en",
                "audio_index": track, "media_identity": file_identity(media),
                "subtitle_sha256": subtitle_hash(subtitle)}

    def check_current(self, payload):
        media = self.config.map_path(payload["media_path"], apply_mapping=False)
        subtitle = self.config.map_path(payload["subtitle_path"], apply_mapping=False)
        if file_identity(media) != payload["media_identity"] or subtitle_hash(subtitle) != payload["subtitle_sha256"]:
            raise ValueError("Inputs changed after submission; submit the current files as a new job")
        return media, subtitle

    def import_catalog(self):
        client = BazarrClient(self.config.bazarr_url, self.config.bazarr_api_key)
        entries = list(client.discover())
        # No stat/open of library files during discovery.
        for entry in entries:
            if entry["eligible"]:
                try:
                    self.config.map_path(entry["media_path"], must_exist=False)
                    self.config.map_path(entry["subtitle_path"], must_exist=False)
                except ValueError:
                    entry["eligible"], entry["reason"] = False, "Path is outside configured mappings/roots"
        return {"imported": self.state.import_catalog(entries), "enqueued": 0}

    def enqueue_selection(self, ids):
        if not isinstance(ids, list) or not ids or len(ids) > 500 or any(not isinstance(i, str) for i in ids):
            raise ValueError("Select between 1 and 500 explicit catalog target IDs")
        catalog = {entry["id"]: entry for entry in self.state.catalog()}
        payloads = []
        for ident in dict.fromkeys(ids):
            item = catalog.get(ident)
            if item is None or not item.get("present") or not item["eligible"]:
                raise ValueError("Selection contains a missing or unsupported catalog target")
            payloads.append(self.prepare(item))
        return self.state.enqueue_many(payloads)

    def required_job(self, ident):
        job = self.state.job(ident)
        if not job:
            raise KeyError("Job not found")
        return job

    def preview(self, ident, anchors):
        from .alignment import manual_preview
        from .subtitles import load_subtitle
        from .inference import probe_media, media_duration
        job = self.required_job(ident)
        if job["status"] in {"queued", "running", "applied", "rolled_back"}:
            raise ValueError("Job is not available for manual preview")
        media, subtitle = self.check_current(job["payload"])
        if not isinstance(anchors, list) or not anchors or len(anchors) > 100:
            raise ValueError("Provide between 1 and 100 manual cue anchors")
        document = load_subtitle(subtitle)
        probe = probe_media(media)
        duration = media_duration(probe)
        if duration is None:
            raise ValueError("Could not determine media duration for bounded manual preview")
        result = manual_preview(document, anchors, media_duration=duration)
        report = dict(job["report"], reason=result.reason, validation=result.report, manual=True)
        if result.timings is not None and result.status != "uncertain":
            report["proposal"] = base64.b64encode(document.render(result.timings)).decode()
            report["preview"] = [{"cue_index": cue.index, "text": cue.text,
                "before": [cue.start, cue.end], "after": list(timing)}
                for cue, timing in zip(document.cues, result.timings)][:500]
            report["anchors"] = anchors
            status = "awaiting_apply"
        else:
            report.pop("proposal", None)
            status = "uncertain"
        self.state.finish(ident, status, report)
        return self.public_job(self.required_job(ident))

    def apply(self, ident):
        from .files import apply_result
        job = self.required_job(ident)
        if job["status"] != "awaiting_apply" or not job["report"].get("manual"):
            raise ValueError("An explicit successful manual preview is required before Apply")
        _, subtitle = self.check_current(job["payload"])
        record = apply_result(subtitle, base64.b64decode(job["report"]["proposal"], validate=True),
                              job["payload"]["subtitle_sha256"], self.config.data_dir / "backups")
        report = dict(job["report"], rollback=record)
        report.pop("proposal", None)
        self.state.finish(ident, "applied", report)
        return self.public_job(self.required_job(ident))

    def rollback(self, ident):
        from .files import rollback
        job = self.required_job(ident)
        if job["status"] != "applied" or not job["report"].get("rollback"):
            raise ValueError("This job has no active applied result to roll back")
        self.config.map_path(job["payload"]["subtitle_path"], apply_mapping=False)
        record = rollback(job["report"]["rollback"])
        self.state.finish(ident, "rolled_back", dict(job["report"], rollback_result=record))
        return self.public_job(self.required_job(ident))

    def retry(self, ident, audio_index=None):
        """Each explicit retry is new work; ordinary hook submissions remain deduplicated."""
        job = self.required_job(ident)
        if job["status"] in {"queued", "running"}:
            raise ValueError("Job is already queued or running")
        payload = self.prepare(dict(job["payload"], audio_index=audio_index), local_paths=True)
        payload["retry_of"] = ident
        payload["retry_request_id"] = uuid.uuid4().hex
        return self.public_job(self.state.enqueue_many([payload])[0])

    @staticmethod
    def public_job(job):
        result = dict(job, report=dict(job["report"]))
        result["report"].pop("proposal", None)
        return result

    def process(self, job):
        from .subtitles import load_subtitle
        from .inference import probe_media, media_duration, choose_audio_track, FasterWhisperBackend
        from .alignment import align
        media, subtitle = self.check_current(job["payload"])
        document = load_subtitle(subtitle)
        report = {"reason": "Inference disabled; use manual preview or configure an experimental backend",
                  "manual": False, "cues": [{"index": c.index, "start": c.start, "end": c.end,
                      "text": c.text} for c in document.cues][:500], "cue_count": len(document.cues)}
        report["stage"] = "inspecting_media"
        self.state.finish(job["id"], "running", report)
        probe = probe_media(media)
        report["media"] = probe
        try:
            audio = choose_audio_track(probe, job["payload"].get("audio_index"))
            report["audio_index"] = audio
        except ValueError as exc:
            report["reason"] = str(exc)
            self.state.finish(job["id"], "needs_audio_track", report)
            return
        if self.config.backend == "disabled":
            report["stage"] = "complete"
            self.state.finish(job["id"], "needs_review", report)
            return
        backend = FasterWhisperBackend(model=self.config.model, threads=self.config.threads,
                                       model_dir=str(self.config.data_dir / "models"))
        report.update(stage="transcribing", reason="Obtaining local CPU speech timing evidence")
        self.state.finish(job["id"], "running", report)
        transcript = backend.transcribe(media, audio, self.config.data_dir / "cache")
        report.update(stage="aligning", reason="Matching dialogue and validating held-out timing anchors")
        self.state.finish(job["id"], "running", report)
        result = align(document, transcript, media_duration=media_duration(probe))
        report.update(reason=result.reason, validation=result.report,
                      candidate_status=result.status, stage="complete",
                      backend={"name": "faster-whisper", "model": self.config.model, "device": "cpu"})
        # Comparative real-media quality gates remain uncalibrated. Never auto-publish yet.
        report["automatic_apply_enabled"] = False
        if result.timings is not None:
            report["preview"] = [{"cue_index": cue.index, "text": cue.text,
                "before": [cue.start, cue.end], "after": list(timing)}
                for cue, timing in zip(document.cues, result.timings)][:500]
        self.check_current(job["payload"])
        status = "unchanged" if result.status == "unchanged" else "uncertain" if result.timings is None else "needs_review"
        self.state.finish(job["id"], status, report)

    def start_worker(self):
        if self.worker is not None:
            raise RuntimeError("Worker already initialized; restart the application to recover a stopped worker")
        def run():
            while not self.stopping.is_set():
                try:
                    job = self.state.claim()
                    self.worker_error = None
                except Exception:
                    self.worker_error = "Queue storage unavailable"
                    self.stopping.wait(1)
                    continue
                if job is None:
                    self.stopping.wait(0.5)
                    continue
                try:
                    self.process(job)
                except Exception as exc:
                    try:
                        self.state.finish(job["id"], "failed", {"reason": str(exc)[:2000]})
                    except Exception:
                        self.worker_error = "Could not persist job outcome; restart to recover interrupted work"
                        # Do not claim more jobs or clear this failure on a later
                        # successful query. Restart reconciles the durable running
                        # record as interrupted before accepting worker execution.
                        return
        self.worker = threading.Thread(target=run, name="alignment-worker", daemon=True)
        self.worker.start()


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Do not log request content, private paths, or API credentials.

        def respond(self, status, value, content_type="application/json; charset=utf-8"):
            body = json.dumps(value).encode() if content_type.startswith("application/json") else value
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'self'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = urlsplit(self.path).path
            if route == "/readyz" and (app.worker is None or not app.worker.is_alive() or app.worker_error):
                return self.respond(503, {"status": "not_ready"})
            if route in {"/healthz", "/readyz"}:
                return self.respond(200, {"status": "ok", "backend": app.config.backend,
                                          "automatic_apply_enabled": False})
            if route == "/api/catalog":
                return self.respond(200, {"data": app.state.catalog()})
            if route == "/api/jobs":
                return self.respond(200, {"data": [app.public_job(j) for j in app.state.jobs()]})
            if route.startswith("/api/jobs/"):
                job = app.state.job(route.rsplit("/", 1)[-1])
                return self.respond(200, app.public_job(job)) if job else self.respond(404, {"error": "Job not found"})
            static = {"/": ("index.html", "text/html; charset=utf-8"),
                      "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                      "/style.css": ("style.css", "text/css; charset=utf-8")}
            if route in static:
                name, mime = static[route]
                return self.respond(200, (Path(__file__).parent / "static" / name).read_bytes(), mime)
            self.respond(404, {"error": "Route not found"})

        def do_POST(self):
            try:
                if self.headers.get("X-BBS-Client") != "1" or self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    return self.respond(403, {"error": "JSON and X-BBS-Client: 1 required"})
                origin = self.headers.get("Origin")
                allowed = {app.config.public_origin} if app.config.public_origin else {
                    "http://" + self.headers.get("Host", ""), "https://" + self.headers.get("Host", "")}
                if origin and origin not in allowed:
                    return self.respond(403, {"error": "Origin rejected"})
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    return self.respond(403, {"error": "Cross-site request rejected"})
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 1024 * 1024:
                    return self.respond(413, {"error": "Request body limit is 1 MiB"})
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Expected a JSON object")
                route = urlsplit(self.path).path
                if route == "/api/catalog/import":
                    return self.respond(200, app.import_catalog())
                if route == "/api/catalog/queue":
                    jobs = app.enqueue_selection(body.get("ids"))
                    return self.respond(202, {"data": [app.public_job(j) for j in jobs]})
                if route == "/api/jobs":
                    return self.respond(202, app.public_job(app.state.enqueue_many([app.prepare(body)])[0]))
                parts = route.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "jobs"]:
                    ident, action = parts[2:]
                    if action == "retry":
                        return self.respond(202, app.retry(ident, body.get("audio_index")))
                    with app.mutation_lock:
                        if action == "preview":
                            return self.respond(200, app.preview(ident, body.get("anchors")))
                        if action == "apply":
                            return self.respond(200, app.apply(ident))
                        if action == "rollback":
                            return self.respond(200, app.rollback(ident))
                return self.respond(404, {"error": "Route not found"})
            except KeyError:
                self.respond(404, {"error": "Job not found"})
            except (ValueError, TypeError) as exc:
                self.respond(400, {"error": str(exc)[:2000]})
            except Exception:
                self.respond(500, {"error": "Operation failed; check configuration and local storage"})
    return Handler


def serve(config):
    app = Application(config)
    server = ThreadingHTTPServer((config.host, config.port), make_handler(app))
    app.start_worker()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stopping.set()
        server.server_close()
        # Process exit releases the instance lock after any daemon worker stops.
