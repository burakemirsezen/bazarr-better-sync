"""Exercise real ffprobe, durable worker, preview, apply and rollback on generated files only."""
import json
from pathlib import Path
import tempfile
import time
import wave
from bazarr_sync.api import Application
from bazarr_sync.config import Config


def main():
    with tempfile.TemporaryDirectory(prefix='bbs-smoke-') as directory:
        root = Path(directory)
        media = root / 'generated.wav'
        with wave.open(str(media), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b'\0\0' * 160000)
        subtitle = root / 'generated.en.srt'
        original = b'1\n00:00:01,000 --> 00:00:03,000\nGenerated smoke test.\n\n2\n00:00:05,000 --> 00:00:07,000\nSecond generated cue.\n'
        subtitle.write_bytes(original)
        app = Application(Config(data_dir=root/'state', media_roots=[root], backend='disabled'))
        try:
            job = app.state.enqueue_many([app.prepare({'media_path':str(media), 'subtitle_path':str(subtitle), 'language':'en', 'audio_index':0})])[0]
            app.start_worker()
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                result = app.state.job(job['id'])
                if result['status'] not in ('queued','running'):
                    break
                time.sleep(.05)
            assert result['status'] == 'needs_review', result
            assert result['report']['media']['format']['duration'] == '10.000000', result
            assert subtitle.read_bytes() == original
            preview = app.preview(job['id'], [{'cue_index':0,'time':2.0}])
            assert preview['status'] == 'awaiting_apply'
            assert subtitle.read_bytes() == original
            applied = app.apply(job['id'])
            assert applied['status'] == 'applied'
            assert b'00:00:02,000 --> 00:00:04,000' in subtitle.read_bytes()
            rolled = app.rollback(job['id'])
            assert rolled['status'] == 'rolled_back'
            assert subtitle.read_bytes() == original
            print(json.dumps({'real_ffprobe':True,'generated_duration_s':10,'durable_worker':'passed','preview_without_write':'passed','explicit_apply':'passed','rollback_exact_bytes':'passed'}))
        finally:
            app.stopping.set()
            if app.worker:
                app.worker.join(3)
            app.instance_lock.close()


if __name__ == '__main__':
    main()
