import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from bazarr_sync.api import Application, make_handler
from bazarr_sync.catalog import BazarrClient, targets
from bazarr_sync.config import Config
from bazarr_sync.state import State


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "movie.mp4"
        self.sub = self.root / "movie.en.srt"
        self.media.write_bytes(b"test media placeholder")
        self.original = b"1\n00:00:01,000 --> 00:00:03,000\nHello world.\n\n2\n00:00:05,000 --> 00:00:07,000\nSecond subtitle.\n"
        self.sub.write_bytes(self.original)
        self.config = Config(data_dir=self.root / "data", media_roots=[self.root])
        self.app = Application(self.config)

    def tearDown(self):
        self.app.stopping.set()
        if self.app.worker:
            self.app.worker.join(3)
        self.app.instance_lock.close()
        self.temp.cleanup()

    def payload(self):
        return {"media_path": str(self.media), "subtitle_path": str(self.sub), "language": "en"}

    def job(self):
        return self.app.state.enqueue_many([self.app.prepare(self.payload())])[0]

    def test_durable_deduplicated_submission(self):
        first = self.job()
        second = self.job()
        self.assertEqual(first["id"], second["id"])
        state = State(self.config.data_dir)
        self.assertEqual(state.job(first["id"])["status"], "queued")
        self.assertEqual(len(state.jobs()), 1)

    def test_restart_marks_interrupted_instead_of_silently_applying(self):
        job = self.job()
        self.app.state.claim()
        state = State(self.config.data_dir)
        self.assertEqual(state.job(job["id"])["status"], "interrupted")
        self.assertEqual(self.sub.read_bytes(), self.original)

    def test_explicit_retry_of_same_completed_job_always_creates_new_work(self):
        original = self.job()
        self.app.state.finish(original["id"], "failed", {"reason": "temporary failure"})
        first_retry = self.app.retry(original["id"])
        self.app.state.finish(first_retry["id"], "failed", {"reason": "another temporary failure"})
        second_retry = self.app.retry(original["id"])
        self.assertNotEqual(first_retry["id"], second_retry["id"])
        self.assertEqual(second_retry["status"], "queued")
        self.assertEqual(second_retry["payload"]["retry_of"], original["id"])
        # Bazarr's unchanged machine submission still returns its original job.
        self.assertEqual(self.job()["id"], original["id"])
        self.assertEqual(len(self.app.state.jobs()), 3)

    def test_failed_outcome_persistence_stops_worker_until_restart(self):
        first = self.job()
        second = self.app.state.enqueue_many([dict(self.app.prepare(self.payload()), retry_of=first["id"])])[0]
        with patch.object(self.app, "process", side_effect=RuntimeError("processing failure")), \
             patch.object(self.app.state, "finish", side_effect=OSError("storage unavailable")):
            self.app.start_worker()
            self.app.worker.join(2)
        self.assertFalse(self.app.worker.is_alive())
        self.assertIn("Could not persist", self.app.worker_error)
        self.assertEqual(self.app.required_job(first["id"])["status"], "running")
        self.assertEqual(self.app.required_job(second["id"])["status"], "queued")
        with self.assertRaisesRegex(RuntimeError, "restart"):
            self.app.start_worker()
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(HTTPError) as caught:
                urlopen("http://127.0.0.1:" + str(server.server_port) + "/readyz", timeout=2)
            self.assertEqual(caught.exception.code, 503)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.app.instance_lock.close()
        self.app = Application(self.config)
        self.assertEqual(self.app.required_job(first["id"])["status"], "interrupted")
        self.assertEqual(self.app.required_job(second["id"])["status"], "queued")

    def test_second_application_instance_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "Another application"):
            Application(self.config)

    def test_import_reconcile_never_queues_or_modifies_files(self):
        item = {"radarrId": 1, "title": "Example", "path": str(self.media),
                "subtitles": [{"code2": "en", "path": str(self.sub)}]}
        with patch.object(BazarrClient, "__init__", return_value=None), patch.object(BazarrClient, "discover", return_value=list(targets(item, "movie"))):
            self.app.import_catalog()
            self.app.import_catalog()
        self.assertEqual(len(self.app.state.catalog()), 1)
        self.assertEqual(self.app.state.jobs(), [])
        self.assertEqual(self.sub.read_bytes(), self.original)

    def test_failed_import_keeps_previous_catalog(self):
        self.app.state.import_catalog([{"id": "old", "present": True}])
        def broken():
            yield {"id": "new", "present": True}
            raise RuntimeError("offline")
        with self.assertRaises(RuntimeError):
            self.app.state.import_catalog(broken())
        self.assertEqual([x["id"] for x in self.app.state.catalog()], ["old"])

    def test_selected_targets_only_and_atomic_validation(self):
        eligible = list(targets({"radarrId": 1, "path": str(self.media),
            "subtitles": [{"code2": "en", "path": str(self.sub)}, {"code2": "tr", "path": "bad.tr.srt"}]}, "movie"))
        self.app.state.import_catalog(eligible)
        with self.assertRaises(ValueError):
            self.app.enqueue_selection([x["id"] for x in eligible])
        self.assertEqual(self.app.state.jobs(), [])
        jobs = self.app.enqueue_selection([eligible[0]["id"]])
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(self.app.state.jobs()), 1)

    def test_path_boundary_and_mapping(self):
        config = Config(media_roots=[self.root], path_mappings={"/tv": str(self.root)})
        self.assertEqual(config.map_path("/tv/movie.en.srt"), self.sub.resolve())
        for bad in ["/tv/../secret", "/tv-other/movie.en.srt", str(self.root.parent / "secret")]:
            with self.assertRaises(ValueError):
                config.map_path(bad)

    def test_overlapping_mapping_is_not_reapplied_to_stored_paths(self):
        library = self.root / "library"
        library.mkdir()
        media = library / self.media.name
        subtitle = library / self.sub.name
        media.write_bytes(self.media.read_bytes())
        subtitle.write_bytes(self.original)
        self.config.path_mappings = {str(self.root): str(library)}
        # The mapped destination itself still begins with the source prefix.
        job = self.job()
        self.assertEqual(job["payload"]["media_path"], str(media.resolve()))
        self.assertEqual(self.app.check_current(job["payload"]), (media.resolve(), subtitle.resolve()))
        self.app.state.finish(job["id"], "needs_review", {})
        retried = self.app.retry(job["id"])
        self.assertEqual(retried["payload"]["media_path"], str(media.resolve()))
        with patch("bazarr_sync.inference.probe_media", return_value={"format": {"duration": "30"}}):
            self.app.preview(job["id"], [{"cue_index": 0, "time": 2.0}])
        self.app.apply(job["id"])
        self.assertIn(b"00:00:02,000", subtitle.read_bytes())
        self.app.rollback(job["id"])
        self.assertEqual(subtitle.read_bytes(), self.original)
        self.assertEqual(self.sub.read_bytes(), self.original)

    def test_config_rejects_wrong_shapes_and_non_origin_urls(self):
        invalid = [{"BBS_MEDIA_ROOTS": '"/media"'}, {"BBS_MEDIA_ROOTS": '[4]'},
                   {"BBS_PATH_MAPPINGS": '[]'}, {"BBS_PATH_MAPPINGS": '{"/media": 1}'},
                   {"BBS_PUBLIC_ORIGIN": "https://user:password@example.com"},
                   {"BBS_PUBLIC_ORIGIN": "https://example.com?query=1"},
                   {"BBS_PUBLIC_ORIGIN": "https://example.com#fragment"}]
        for values in invalid:
            with self.subTest(values=values), patch.dict(os.environ, values, clear=True):
                with self.assertRaises(ValueError):
                    Config.from_env()

    def test_embedded_and_unsupported_catalog_only(self):
        items = list(targets({"radarrId": 4, "path": str(self.media), "subtitles": [
            {"code2": "en", "path": None}, {"code2": "tr", "path": "film.tr.srt"},
            {"code2": "en", "path": "film.en.sup"}, {"code2": "en", "path": "film.en.ass"}]}, "movie"))
        self.assertEqual([x["eligible"] for x in items], [False, False, False, True])

    def test_installed_api_pagination_and_episode_parameter(self):
        client = BazarrClient("http://bazarr:6767", "test-not-a-secret", page_size=1)
        calls = []
        def get(route, params):
            calls.append((route, params))
            if route == "movies":
                return {"data": [{"radarrId": params["start"], "path": "film.mkv", "subtitles": []}], "total": 2}
            if route == "series":
                return {"data": [{"sonarrSeriesId": 9}], "total": 1}
            self.assertEqual(params["seriesid[]"], 9)
            return {"data": [{"sonarrEpisodeId": 5, "path": "show.mkv", "subtitles": [{"code2": "en", "path": "show.srt"}]}]}
        client.get = get
        found = list(client.discover())
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["series_id"], 9)
        self.assertEqual([p[1]["start"] for p in calls if p[0] == "movies"], [0, 1])

    def test_stale_input_blocks_manual_preview(self):
        job = self.job()
        self.app.state.finish(job["id"], "needs_review", {})
        self.sub.write_bytes(self.original + b"\nchanged")
        with self.assertRaisesRegex(ValueError, "Inputs changed"):
            self.app.preview(job["id"], [{"cue_index": 0, "time": 2}])

    def test_manual_preview_explicit_apply_and_rollback(self):
        job = self.job()
        self.app.state.finish(job["id"], "needs_review", {})
        with self.assertRaises(ValueError):
            self.app.apply(job["id"])
        with patch("bazarr_sync.inference.probe_media", return_value={"format": {"duration": "30"}}):
            preview = self.app.preview(job["id"], [{"cue_index": 0, "time": 2.0}])
        self.assertEqual(preview["status"], "awaiting_apply")
        self.assertNotIn("proposal", preview["report"])
        self.assertEqual(self.sub.read_bytes(), self.original)
        applied = self.app.apply(job["id"])
        self.assertEqual(applied["status"], "applied")
        self.assertIn(b"00:00:02,000", self.sub.read_bytes())
        self.app.rollback(job["id"])
        self.assertEqual(self.sub.read_bytes(), self.original)

    def test_manual_preview_respects_media_duration(self):
        job = self.job()
        self.app.state.finish(job["id"], "needs_review", {})
        with patch("bazarr_sync.inference.probe_media", return_value={"format": {"duration": "8"}}):
            with self.assertRaises(ValueError):
                self.app.preview(job["id"], [{"cue_index": 0, "time": 20.0}])
        self.assertEqual(self.sub.read_bytes(), self.original)

    def test_manual_preview_refuses_missing_or_nonfinite_duration(self):
        job = self.job()
        self.app.state.finish(job["id"], "needs_review", {})
        for probe in [{}, {"format": {"duration": "NaN"}}, {"format": {"duration": "0"}}]:
            with self.subTest(probe=probe), patch("bazarr_sync.inference.probe_media", return_value=probe):
                with self.assertRaisesRegex(ValueError, "determine media duration"):
                    self.app.preview(job["id"], [{"cue_index": 0, "time": 2.0}])
        self.assertEqual(self.app.required_job(job["id"])["status"], "needs_review")
        self.assertEqual(self.sub.read_bytes(), self.original)

    def test_worker_does_not_block_durable_submission(self):
        first = self.job()
        started, release = threading.Event(), threading.Event()
        def slow(job):
            started.set()
            release.wait(2)
            self.app.state.finish(job["id"], "needs_review", {})
        self.app.process = slow
        self.app.start_worker()
        self.assertTrue(started.wait(2))
        try:
            start = time.monotonic()
            with self.app.mutation_lock:
                self.app.state.enqueue_many([dict(self.app.prepare(self.payload()), retry_of=first["id"])])
            self.assertLess(time.monotonic() - start, 1)
        finally:
            release.set()

    def test_http_mutation_origin_and_content_guards(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_port)
        try:
            for headers in [{"Content-Type": "application/json"},
                            {"Content-Type": "application/json", "X-BBS-Client": "1", "Origin": "https://evil.example"}]:
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(url + "/api/jobs", b"{}", headers), timeout=2)
                self.assertEqual(caught.exception.code, 403)
                caught.exception.close()
            headers = {"Content-Type": "application/json", "X-BBS-Client": "1"}
            with urlopen(Request(url + "/api/jobs", json.dumps(self.payload()).encode(), headers), timeout=2) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(json.load(response)["status"], "queued")
            with self.assertRaises(HTTPError) as caught:
                urlopen(url + "/readyz", timeout=2)
            self.assertEqual(caught.exception.code, 503)
            caught.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)

    def test_slow_manual_preview_does_not_block_http_acknowledgement(self):
        job = self.job()
        self.app.state.finish(job["id"], "needs_review", {})
        started, release = threading.Event(), threading.Event()
        def slow_preview(ident, anchors):
            started.set()
            release.wait(3)
            return {"id": ident, "status": "awaiting_apply"}
        self.app.preview = slow_preview
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:" + str(server.server_port)
        headers = {"Content-Type": "application/json", "X-BBS-Client": "1"}
        errors = []
        def preview():
            try:
                with urlopen(Request(url + "/api/jobs/" + job["id"] + "/preview", b"{}", headers), timeout=5) as response:
                    response.read()
            except Exception as exc:
                errors.append(exc)
        preview_thread = threading.Thread(target=preview)
        try:
            preview_thread.start()
            self.assertTrue(started.wait(2))
            start = time.monotonic()
            with urlopen(Request(url + "/api/jobs", json.dumps(self.payload()).encode(), headers), timeout=1) as response:
                self.assertEqual(response.status, 202)
                self.assertEqual(json.load(response)["id"], job["id"])
            self.assertLess(time.monotonic() - start, 1)
        finally:
            release.set()
            preview_thread.join(5)
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
