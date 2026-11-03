"""Coordinator side of the remote GPU worker protocol, exercised over HTTP with a fake worker."""
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import wave

from bazarr_sync.api import Application, make_handler
from bazarr_sync.config import Config

TOKEN = "worker-secret-token"
SOURCE = (b"1\n00:00:01,000 --> 00:00:03,000\nHello there, how are you?\n\n"
          b"2\n00:00:05,000 --> 00:00:07,000\nI am fine, thank you.\n\n"
          b"3\n00:00:09,000 --> 00:00:10,000\n[door slams]\n")


class WorkerProtocolTests(unittest.TestCase):
    def setUp(self, **overrides):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.media = self.root / "episode.mkv"
        self.media.write_bytes(b"media placeholder")
        self.sub = self.root / "episode.en.srt"
        self.sub.write_bytes(SOURCE)
        settings = dict(data_dir=self.root / "data", media_roots=[self.root], backend="remote-gpu",
                        worker_tokens={"desk": hashlib.sha256(TOKEN.encode()).hexdigest()})
        settings.update(overrides)
        self.app = Application(Config(**settings))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.instance_lock.close()
        self.temp.cleanup()

    def restart(self, **overrides):
        self.tearDown()
        self.setUp(**overrides)

    # Fixtures -----------------------------------------------------------

    def ready_job(self):
        """A job whose audio artifact is prepared, as process() leaves it for remote-gpu."""
        job = self.app.state.enqueue_many([self.app.prepare({"media_path": str(self.media),
                                                             "subtitle_path": str(self.sub), "language": "en"})])[0]
        path = self.app.artifact_path(job["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(path), "wb") as audio:
            audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
            audio.writeframes(b"\x00\x00" * 16000 * 12)
        artifact = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "size_bytes": path.stat().st_size,
                    "duration_seconds": 12.0, "sample_rate": 16000, "channels": 1, "encoding": "pcm_s16le",
                    "media_offset_seconds": 0.0, "audio_index": 1}
        self.app.state.finish(job["id"], "awaiting_worker", {"artifact": artifact})
        return job["id"]

    def call(self, method, route, body=None, token=TOKEN, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + route, data=data, method=method,
                          headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {}), **(headers or {})})
        try:
            with urlopen(request, timeout=5) as response:
                raw = response.read()
                return response.status, (json.loads(raw) if raw and response.headers.get_content_type() == "application/json" else raw)
        except HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def claim(self):
        status, body = self.call("POST", "/worker/v1/claim", {"protocol": 1})
        self.assertEqual(status, 200)
        return body

    def result(self, claim, shift=0.5, result_id="result-0001", statuses=("aligned", "aligned", "legacy_inferred")):
        timings = [{"cue_index": c["index"], "start": c["start"] + shift, "end": c["end"] + shift, "status": s}
                   for c, s in zip(claim["cues"], statuses)]
        return {"protocol": 1, "job_id": claim["job_id"], "result_id": result_id, "lease_token": claim["lease_token"],
                "subtitle_sha256": claim["subtitle_sha256"], "audio_sha256": claim["audio"]["sha256"],
                "timings": timings, "engine": {"mapper": "sequence-0.2"}, "diagnostics": {"direct_rejected": 0}}

    # Tests --------------------------------------------------------------

    def test_requires_worker_token(self):
        self.assertEqual(self.call("POST", "/worker/v1/claim", {"protocol": 1}, token=None)[0], 401)
        self.assertEqual(self.call("POST", "/worker/v1/claim", {"protocol": 1}, token="wrong")[0], 401)

    def test_claim_audio_heartbeat_and_empty_queue(self):
        ident = self.ready_job()
        claim = self.claim()
        self.assertEqual(claim["job_id"], ident)
        self.assertEqual([c["index"] for c in claim["cues"]], [0, 1, 2])
        self.assertNotIn("subtitle_path", json.dumps(claim))
        self.assertEqual(self.call("POST", "/worker/v1/claim", {"protocol": 1})[0], 204)
        status, audio = self.call("GET", f"/worker/v1/jobs/{ident}/audio", headers={"X-BBS-Lease": claim["lease_token"]})
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.sha256(audio).hexdigest(), claim["audio"]["sha256"])
        self.assertEqual(self.call("GET", f"/worker/v1/jobs/{ident}/audio", headers={"X-BBS-Lease": "forged"})[0], 403)
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/heartbeat", {"lease_token": claim["lease_token"]})[0], 200)

    def test_result_becomes_reviewable_proposal_without_touching_the_file(self):
        ident = self.ready_job()
        claim = self.claim()
        status, ack = self.call("POST", f"/worker/v1/jobs/{ident}/result", self.result(claim))
        self.assertEqual((status, ack["status"]), (200, "awaiting_apply"))
        self.assertEqual(self.sub.read_bytes(), SOURCE)
        job = self.app.state.job(ident)
        self.assertTrue(job["report"]["validation"]["eligible"])
        self.assertIn("automatic apply is disabled", job["report"]["reason"])
        self.assertFalse(self.app.artifact_path(ident).exists())
        # Idempotent replay, conflicting replay.
        self.assertTrue(self.call("POST", f"/worker/v1/jobs/{ident}/result", self.result(claim))[1]["duplicate"])
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/result", self.result(claim, result_id="result-0002"))[0], 409)
        # Explicit Apply publishes with a backup; rollback restores the exact original.
        self.app.apply(ident)
        self.assertIn(b"00:00:01,500 --> 00:00:03,500", self.sub.read_bytes())
        self.assertIn(b"[door slams]", self.sub.read_bytes())
        self.app.rollback(ident)
        self.assertEqual(self.sub.read_bytes(), SOURCE)

    def test_auto_apply_only_when_confident(self):
        self.restart(auto_apply=True)
        ident = self.ready_job()
        claim = self.claim()
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/result", self.result(claim))[1]["status"], "applied")
        self.assertTrue(self.app.state.job(ident)["report"]["auto_applied"])
        self.app.rollback(ident)
        self.assertEqual(self.sub.read_bytes(), SOURCE)
        # Half the spoken cues measured directly: below the 80% default, so it waits for review.
        changed = SOURCE.replace(b"Hello there", b"Hello again")
        self.sub.write_bytes(changed)
        ident = self.ready_job()
        claim = self.claim()
        status, ack = self.call("POST", f"/worker/v1/jobs/{ident}/result",
                                self.result(claim, statuses=("aligned", "legacy_inferred", "legacy_inferred")))
        self.assertEqual(ack["status"], "awaiting_apply")
        self.assertIn("direct coverage 50%", self.app.state.job(ident)["report"]["reason"])
        self.assertEqual(self.sub.read_bytes(), changed)

    def test_invalid_results_rejected(self):
        ident = self.ready_job()
        claim = self.claim()
        bad = self.result(claim); bad["timings"] = bad["timings"][:2]
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/result", bad)[0], 400)
        bad = self.result(claim); bad["timings"][1]["end"] = bad["timings"][1]["start"]
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/result", bad)[0], 400)
        bad = self.result(claim); bad["audio_sha256"] = "0" * 64
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/result", bad)[0], 400)
        bad = self.result(claim); bad["timings"][2]["end"] = 99
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/result", bad)[0], 400)
        self.assertEqual(self.app.state.job(ident)["status"], "worker_running")

    def test_cancel_revokes_lease_and_late_result(self):
        ident = self.ready_job()
        claim = self.claim()
        self.app.cancel(ident)
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/result", self.result(claim))[0], 409)
        self.assertEqual(self.app.state.job(ident)["status"], "cancelled")
        self.assertEqual(self.sub.read_bytes(), SOURCE)

    def test_subtitle_changed_during_inference_is_not_proposed(self):
        ident = self.ready_job()
        claim = self.claim()
        self.sub.write_bytes(SOURCE + b"\n")
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/result", self.result(claim))[0], 409)
        self.assertEqual(self.app.state.job(ident)["status"], "failed")

    def test_expired_lease_requeues_then_fails_after_max_attempts(self):
        self.restart(lease_seconds=1, max_attempts=2)
        ident = self.ready_job()
        first = self.claim()
        time.sleep(1.1)
        self.app.state.expire_leases(self.app.config.max_attempts)
        self.assertEqual(self.app.state.job(ident)["status"], "awaiting_worker")
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/result", self.result(first))[0], 409)
        second = self.claim()
        self.assertNotEqual(first["lease_token"], second["lease_token"])
        time.sleep(1.1)
        self.app.state.expire_leases(self.app.config.max_attempts)
        self.assertEqual(self.app.state.job(ident)["status"], "failed")
        self.assertEqual([a["status"] for a in self.app.state.attempts(ident)], ["expired", "expired"])

    def test_worker_cannot_reach_browser_api_or_publish(self):
        ident = self.ready_job()
        claim = self.claim()
        self.call("POST", f"/worker/v1/jobs/{ident}/result", self.result(claim))
        self.assertEqual(self.call("POST", f"/worker/v1/jobs/{ident}/apply", {"lease_token": claim["lease_token"]})[0], 404)
        self.assertEqual(self.sub.read_bytes(), SOURCE)


if __name__ == "__main__":
    unittest.main()
