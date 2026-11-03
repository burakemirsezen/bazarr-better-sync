"""Scratch-only comparative benchmark; never writes beside the selected media.

Run inside an isolated CPU-capped environment with read-only media mounts.
The original subtitle is a PROVISIONAL timing reference, not human ground truth.
All paths and dialogue stay in the private scratch directory.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys
import time


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--media', required=True)
    p.add_argument('--subtitle', required=True)
    p.add_argument('--scratch', required=True)
    p.add_argument('--start', type=float, default=120)
    p.add_argument('--duration', type=float, default=480)
    p.add_argument('--model', default='tiny.en')
    args = p.parse_args()
    from bazarr_sync.subtitles import load_subtitle, parse_subtitle
    from bazarr_sync.alignment import align
    import imageio_ffmpeg
    from faster_whisper import WhisperModel
    import av
    import psutil

    root = Path(args.scratch).resolve()
    root.mkdir(parents=True, exist_ok=True)
    media = Path(args.media).resolve()
    sub = Path(args.subtitle).resolve()
    if root == media.parent or root == sub.parent:
        raise ValueError('Use a separate scratch directory')
    before = hashlib.sha256(sub.read_bytes()).hexdigest()
    original = load_subtitle(sub)
    selected = [c for c in original.cues if c.start >= args.start and c.end < args.start + args.duration - 10]
    if len(selected) < 20:
        raise ValueError('Insufficient English subtitle cues in selected window')

    def timestamp(t):
        ms = round(t * 1000)
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f'{h:02}:{m:02}:{s:02},{ms:03}'

    def render(transform):
        return '\n\n'.join(f'{i + 1}\n{timestamp(transform(c.start - args.start))} --> {timestamp(transform(c.end - args.start))}\n{c.text}' for i, c in enumerate(selected)).encode('utf-8') + b'\n'

    truth = [(c.start - args.start, c.end - args.start) for c in selected]
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with av.open(str(media)) as container:
        tracks = [{'stream_index':s.index,'language':s.metadata.get('language'), 'title':s.metadata.get('title'), 'codec':s.codec_context.name} for s in container.streams.audio]
    english = [t for t in tracks if t['language'] in ('eng','en') and 'comment' not in (t['title'] or '').lower()]
    if len(english) != 1:
        raise ValueError('Select a media file with one unambiguous English audio track for this harness')
    stream = english[0]['stream_index']
    audio = root / 'audio.wav'
    subprocess.run([ffmpeg, '-v', 'error', '-nostdin', '-y', '-ss', str(args.start), '-i', str(media), '-map', f'0:{stream}', '-t', str(args.duration), '-vn', '-ac', '1', '-ar', '16000', str(audio)], check=True, timeout=180)
    # ffsubsync resolves ffmpeg by name. Only modify the scratch process PATH.
    bindir = root / 'bin'
    bindir.mkdir(exist_ok=True)
    link = bindir / 'ffmpeg'
    if not link.exists():
        link.symlink_to(ffmpeg)
    os.environ['PATH'] = str(bindir) + os.pathsep + os.environ['PATH']

    output = {'reference_kind':'existing-subtitle-provisional', 'audio_seconds':args.duration, 'window_start_seconds':args.start,
              'cue_count':len(truth), 'source_subtitle_sha256':before, 'audio_track':stream,
              'versions':{n:importlib.metadata.version(n) for n in ('ffsubsync','faster-whisper','ctranslate2','webrtcvad-wheels','imageio-ffmpeg')},
              'ffmpeg_version':subprocess.check_output([ffmpeg,'-version'],text=True).splitlines()[0],
              'cgroup_cpu_max':Path('/sys/fs/cgroup/cpu.max').read_text().strip(),
              'cgroup_memory_max':Path('/sys/fs/cgroup/memory.max').read_text().strip(), 'results':[]}
    start = time.monotonic()
    cpu_start = time.process_time()
    model = WhisperModel(args.model, device='cpu', compute_type='int8', cpu_threads=2, num_workers=1, download_root=str(root/'models'))
    output['model_load_download_seconds'] = time.monotonic() - start
    start = time.monotonic()
    segments, info = model.transcribe(str(audio), language='en', beam_size=5, word_timestamps=True, vad_filter=True, condition_on_previous_text=False)
    words = [{'text':w.word,'start':w.start,'end':w.end} for segment in segments if segment.no_speech_prob <= .6 for w in (segment.words or []) if w.end > w.start]
    output['asr_settings'] = {'model':args.model,'device':'cpu','compute_type':'int8','cpu_threads':2,'beam_size':5,'word_timestamps':True,'vad_filter':True,'condition_on_previous_text':False,'max_no_speech_prob':.6,'discard_nonpositive_word_intervals':True}
    output['asr_seconds'] = time.monotonic() - start
    output['asr_cpu_seconds_including_model_load'] = time.process_time() - cpu_start
    output['asr_peak_process_mib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    output['recognized_words'] = len(words)
    output['model_snapshots'] = {d.parent.name:[p.name for p in d.iterdir() if p.is_dir()] for d in (root/'models').glob('*/snapshots')}
    (root/'transcript-private.json').write_text(json.dumps(words))
    print(json.dumps({k:v for k,v in output.items() if k != 'results'}),flush=True)

    cases = {'unchanged':lambda t:t,'offset_3s':lambda t:t+3,'drift_0.6pct':lambda t:t*1.006,'split_5s':lambda t:t+(5 if t >= args.duration/2 else 0)}
    def errors(timings):
        if timings is None or len(timings) != len(truth):
            return None
        values = sorted(abs(x-y) for pair,reference in zip(timings,truth) for x,y in zip(pair,reference))
        return {'mean_error_s':statistics.mean(values),'median_error_s':statistics.median(values),'p95_error_s':values[int(.95*(len(values)-1))],'max_error_s':max(values), 'changed_more_than_0.5s':sum(v>.5 for v in values)}
    for label, transform in cases.items():
        raw = render(transform)
        doc = parse_subtitle(raw,'srt')
        source = root / f'{label}.srt'
        source.write_bytes(raw)
        start = time.monotonic()
        result = align(doc,words,media_duration=args.duration)
        row={'case':label,'method':'asr_anchors','seconds_excluding_asr':time.monotonic()-start,'status':result.status,'reason':result.reason,'report':result.report,'input_error':errors([(c.start,c.end) for c in doc.cues]),'output_error':errors(result.timings)}
        output['results'].append(row)
        print(json.dumps(row),flush=True)
        destination=root/f'{label}.ffsubsync.srt'
        # Captured installed Bazarr profile; not a claim about other installations.
        command=['ffsubsync',str(audio),'-i',str(source),'-o',str(destination),'--vad','webrtc',
                 '--no-fix-framerate','--gss','--max-offset-seconds','600']
        start=time.monotonic()
        peak_rss = 0
        with (root/f'{label}.ffsubsync.log').open('w') as log:
            baseline=subprocess.Popen(command,stdout=log,stderr=subprocess.STDOUT)
            while baseline.poll() is None:
                if time.monotonic() - start > 240:
                    baseline.kill()
                    baseline.wait()
                    raise TimeoutError('Baseline exceeded 240 seconds')
                try:
                    process=psutil.Process(baseline.pid)
                    rss=sum(child.memory_info().rss for child in [process]+process.children(recursive=True) if child.is_running())
                    peak_rss=max(peak_rss,rss)
                except psutil.Error:
                    pass
                time.sleep(.02)
        timings=None
        if baseline.returncode == 0 and destination.exists():
            timings=[(c.start,c.end) for c in load_subtitle(destination).cues]
        row={'case':label,'method':'ffsubsync','settings':command[6:],'seconds':time.monotonic()-start,'sampled_peak_process_tree_mib':peak_rss/1048576,'returncode':baseline.returncode,'output_error':errors(timings)}
        output['results'].append(row)
        print(json.dumps(row),flush=True)
    output['original_subtitle_unchanged']=hashlib.sha256(sub.read_bytes()).hexdigest()==before
    output['asr_peak_process_mib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024
    (root/'results-private.json').write_text(json.dumps(output,indent=2))
    if not output['original_subtitle_unchanged']:
        raise RuntimeError('Source changed externally during benchmark')


if __name__ == '__main__':
    main()
