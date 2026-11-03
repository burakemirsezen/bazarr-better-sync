"""Local speech evidence: Qwen3-ASR transcription plus Qwen3 forced alignment, one window at a time.

Pinned to the revisions evaluated in the 2026-11-03 benchmarks. Weights load
from an explicitly provisioned cache; audio never leaves this machine.
"""
from __future__ import annotations

import time

ASR_MODEL = ('Qwen/Qwen3-ASR-1.7B-hf', 'bcd2b5b7f32b480ab5790554cfa8347f246a14f3')
ALIGNER_MODEL = ('Qwen/Qwen3-ForcedAligner-0.6B-hf', 'c07281df297b9905d24a508279258cccf987a064')
MAX_NEW_TOKENS = 768


class SpeechEngine:
    def __init__(self, model_cache, device='cuda:0'):
        import torch
        from transformers import AutoModelForTokenClassification, AutoProcessor, Qwen3ASRForConditionalGeneration
        if not torch.cuda.is_available():
            raise RuntimeError('No GPU available to torch; refusing a silent full-episode CPU run')
        self.torch, self.device = torch, device
        started = time.perf_counter()
        name, revision = ASR_MODEL
        self.asr_processor = AutoProcessor.from_pretrained(name, revision=revision, cache_dir=model_cache)
        self.asr = Qwen3ASRForConditionalGeneration.from_pretrained(
            name, revision=revision, cache_dir=model_cache, dtype=torch.float16,
            attn_implementation='eager').to(device).eval()
        name, revision = ALIGNER_MODEL
        self.aligner_processor = AutoProcessor.from_pretrained(name, revision=revision, cache_dir=model_cache)
        self.aligner = AutoModelForTokenClassification.from_pretrained(
            name, revision=revision, cache_dir=model_cache, dtype=torch.float16,
            attn_implementation='eager').to(device).eval()
        torch.cuda.synchronize()
        self.load_seconds = time.perf_counter() - started

    def describe(self) -> dict:
        torch = self.torch
        return {'asr_model': '@'.join(ASR_MODEL), 'aligner_model': '@'.join(ALIGNER_MODEL),
                'torch': torch.__version__, 'hip': torch.version.hip, 'cuda': torch.version.cuda,
                'device': torch.cuda.get_device_name(0), 'precision': 'float16', 'attention': 'eager',
                'max_new_tokens': MAX_NEW_TOKENS, 'model_load_seconds': round(self.load_seconds, 2)}

    def words(self, samples) -> tuple[list[dict], str | None]:
        """Timed words for one window of mono 16 kHz float32 samples, or ([], reason) when unusable.
        Each word carries smoothing_seconds: how far the aligner's raw timestamps were corrected."""
        torch = self.torch
        inputs = self.asr_processor.apply_transcription_request(audio=samples, language='English', prompt=None)
        inputs = inputs.to(self.asr.device, self.asr.dtype)
        with torch.inference_mode():
            generated = self.asr.generate(**inputs, max_new_tokens=MAX_NEW_TOKENS, do_sample=False)
        tokens = generated[0, inputs['input_ids'].shape[1]:]
        if int(tokens.shape[0]) >= MAX_NEW_TOKENS:
            return [], 'asr_token_limit'
        transcript = self.asr_processor.decode(tokens, return_format='transcription_only')
        if not transcript.strip():
            return [], 'empty_transcript'
        processor = self.aligner_processor
        inputs, word_lists = processor.prepare_forced_aligner_inputs(audio=samples, transcript=transcript, language='English')
        inputs = inputs.to(self.device, torch.float16)
        with torch.inference_mode():
            result = self.aligner(**inputs)
        mask = inputs['input_ids'][0] == self.aligner.config.timestamp_token_id
        raw = (result.logits[0][mask].argmax(-1).float() * processor.timestamp_segment_time / 1000).cpu().tolist()
        decoded = processor.decode_forced_alignment(logits=result.logits, input_ids=inputs['input_ids'], word_lists=word_lists,
                                                    timestamp_token_id=self.aligner.config.timestamp_token_id)[0]
        if len(raw) != 2 * len(decoded):
            return [], 'alignment_timestamp_count_mismatch'
        return [{'text': w['text'], 'start_time': w['start_time'], 'end_time': w['end_time'],
                 'smoothing_seconds': max(abs(raw[2 * i] - w['start_time']), abs(raw[2 * i + 1] - w['end_time']))}
                for i, w in enumerate(decoded)], None

    def peak_memory(self) -> dict:
        return {'peak_gpu_allocated_bytes': self.torch.cuda.max_memory_allocated(),
                'peak_gpu_reserved_bytes': self.torch.cuda.max_memory_reserved()}
