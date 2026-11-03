"""Optional local SenseVoice transcription via Windows audio decoding, no FFmpeg.

This is not Jianying's built-in ASR. Produces raw tokens and SRT, not a draft.
Install requirements-asr.txt and provide a downloaded SenseVoice model directory.
"""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import sys
import wave

from common import local_file, media_info


def make_cues(result, limit_us, fps=30):
    tokens, times = result['tokens'], result['timestamps']
    if not tokens or len(tokens) != len(times) or any(not math.isfinite(t) or t < 0 for t in times):
        raise ValueError('Recognition returned no valid timed tokens')
    if any(a > b for a, b in zip(times, times[1:])):
        raise ValueError('Recognition timestamps are not monotonic')
    cues, group, start = [], [], None
    for index, (token, when) in enumerate(zip(tokens, times)):
        token = token.replace('▁', ' ').strip()
        if not token or token.startswith('<|'):
            continue
        if start is None:
            start = round(when * 1_000_000)
        group.append(token)
        next_us = round(times[index+1]*1_000_000) if index+1 < len(times) else min(limit_us, round(when*1_000_000)+350000)
        text = ''.join(group)
        speech_gap = next_us-round(when*1_000_000) > 600000
        if ((token[-1] in '，。？！；,.!?;' and len(text) >= 5) or len(text) >= 14
                or speech_gap or next_us-start > 3200000 or index == len(tokens)-1):
            end = min(limit_us, max(start+10000,
                min(next_us-10000,round(when*1000000)+350000) if speech_gap else next_us-10000))
            if start < end:
                cues.append({'start_us': start, 'end_us': end, 'text': text})
            group, start = [], None
    if group and start is not None and start < limit_us:
        cues.append({'start_us': start, 'end_us': min(limit_us, max(start+10000, round(times[-1]*1_000_000)+350000)), 'text': ''.join(group)})
    # Fixed character grouping may leave a final word/punctuation as a flashing
    # one-frame cue. Join a short adjacent tail without crossing a speech gap.
    merged = []
    for cue in cues:
        chars = len(cue['text'].strip('，。？！；,.!?; '))
        if (merged and (chars <= 2 or cue['end_us']-cue['start_us'] < 400000)
                and 0 <= cue['start_us']-merged[-1]['end_us'] <= 250000
                and cue['end_us']-merged[-1]['start_us'] <= 5000000
                and len(merged[-1]['text'])+len(cue['text']) <= 24):
            merged[-1]['text'] += cue['text']
            merged[-1]['end_us'] = cue['end_us']
        else:
            merged.append(cue)
    cues = merged
    # Native text timelines use frame boundaries; retain the raw estimates too.
    for cue in cues:
        cue['asr_start_us'], cue['asr_end_us'] = cue['start_us'], cue['end_us']
        for key in ('start_us', 'end_us'):
            cue[key] = min(limit_us, ((cue[key]*fps+999999)//1000000)*1000000//fps)
    for left, right in zip(cues, cues[1:]):
        left['end_us'] = min(left['end_us'], right['start_us'])
    cues = [c for c in cues if 0 <= c['start_us'] < c['end_us'] <= limit_us]
    if not cues:
        raise ValueError('No usable subtitle cues')
    return cues


def srt_time(us):
    seconds, ms = divmod(round(us/1000), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}'


async def decode_audio(source, destination):
    from winrt.windows.storage import StorageFile, StorageFolder, CreationCollisionOption
    from winrt.windows.media.mediaproperties import MediaEncodingProfile, AudioEncodingQuality
    from winrt.windows.media.transcoding import MediaTranscoder
    folder = await StorageFolder.get_folder_from_path_async(str(destination.parent))
    original = await StorageFile.get_file_from_path_async(str(source))
    target = await folder.create_file_async(destination.name, CreationCollisionOption.FAIL_IF_EXISTS)
    profile = MediaEncodingProfile.create_wav(AudioEncodingQuality.LOW)
    profile.audio.sample_rate = 16000
    profile.audio.channel_count = 1
    profile.audio.bits_per_sample = 16
    profile.audio.bitrate = 256000
    prepared = await MediaTranscoder().prepare_file_transcode_async(original, target, profile)
    if not prepared.can_transcode:
        raise RuntimeError(f'Windows audio decoding failed: {prepared.failure_reason}')
    await prepared.transcode_async()


def recognize_chunks(recognizer, samples, sample_rate=16000):
    """Bound long SenseVoice inputs to 32 seconds with one-second context."""
    if len(samples) <= 180*sample_rate:
        stream=recognizer.create_stream()
        stream.accept_waveform(sample_rate,samples)
        recognizer.decode_stream(stream)
        return json.loads(str(stream.result))
    tokens,timestamps,chunks=[],[],[]
    step=30*sample_rate
    for center in range(0,len(samples),step):
        start=max(0,center-sample_rate)
        end=min(len(samples),center+step+sample_rate)
        stream=recognizer.create_stream()
        stream.accept_waveform(sample_rate,samples[start:end])
        recognizer.decode_stream(stream)
        result=json.loads(str(stream.result))
        if len(result.get('tokens',[])) != len(result.get('timestamps',[])):
            raise ValueError('长素材语音识别分段缺少完整时间戳')
        for token,when in zip(result.get('tokens',[]),result.get('timestamps',[])):
            global_time=start/sample_rate+when
            if center/sample_rate <= global_time < min(len(samples),center+step)/sample_rate:
                tokens.append(token);timestamps.append(global_time)
        chunks.append({'start_s':start/sample_rate,'end_s':end/sample_rate})
    return {'tokens':tokens,'timestamps':timestamps,'text':''.join(tokens),
            'chunking':'30s_with_1s_context','chunks':chunks}


def transcribe(source, model_dir, output, fps=30, language='zh', allow_empty=False):
    import sherpa_onnx
    import numpy as np
    source = Path(local_file(str(source), Path.cwd()))
    model = Path(model_dir).resolve(strict=True)
    output = Path(output).resolve()
    if output.exists() or output.is_symlink():
        raise FileExistsError('Choose a new output directory')
    if type(fps) is not int or not 1 <= fps <= 120:
        raise ValueError('fps must be an integer in 1..120')
    if language not in ('auto', 'zh', 'ja', 'en', 'ko', 'yue'):
        raise ValueError('Unsupported transcription language')
    meta = media_info(source)
    if not meta['has_audio'] or not 0 < meta['duration'] <= 420:
        raise ValueError('本地语音识别支持有音轨素材，单条最长 7 分钟')
    for name in ('model.int8.onnx', 'tokens.txt'):
        if not (model/name).is_file():
            raise FileNotFoundError(model/name)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    output.mkdir(parents=True)
    audio = output/'audio.wav'
    asyncio.run(decode_audio(source, audio))
    with wave.open(str(audio), 'rb') as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Unexpected Windows decoded PCM format')
        samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2').astype(np.float32)/32768
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(model=str(model/'model.int8.onnx'),
        tokens=str(model/'tokens.txt'), num_threads=4, use_itn=True, language=language, debug=False)
    raw = recognize_chunks(recognizer,samples)
    duration = meta.get('video_duration') or meta['duration']
    limit = math.floor(duration*fps+1e-6)*1_000_000//fps
    spoken_tokens = [token for token in raw.get('tokens', [])
                     if token.replace('▁', ' ').strip() and not token.startswith('<|')]
    has_speech_text = any(char.isalnum() for token in spoken_tokens for char in token)
    cues = [] if allow_empty and not has_speech_text else make_cues(raw, limit, fps)
    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise RuntimeError('Source changed during transcription')
    report = {'status': 'local_asr_completed', 'provider': 'SenseVoice via sherpa-onnx; not Jianying ASR',
        'source': str(source), 'source_sha256': digest, 'model_directory': str(model),
        'chunk_count':len(raw.get('chunks',[])) or 1,
        'subtitle_count': len(cues), 'fps': fps, 'language': language,
        'speech_detected': bool(cues), 'silence_verified': False,
        'decoder': 'Windows MediaTranscoder', 'ffmpeg_invoked': False, 'media_uploaded': False,
        'subtitles_written_to_draft': False, 'proofreading_required': True}
    for name, value in [('raw-asr.json', raw), ('captions.json', cues), ('asr-report.json', report)]:
        (output/name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    (output/'captions.srt').write_text('\n\n'.join(
        f'{n}\n{srt_time(c["start_us"])} --> {srt_time(c["end_us"])}\n{c["text"]}'
        for n, c in enumerate(cues, 1))+'\n', encoding='utf-8')
    return report


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8'); sys.stderr.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', required=True)
    parser.add_argument('--model-dir', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--fps', type=int, default=30)
    parser.add_argument('--language', choices=['auto', 'zh', 'ja', 'en', 'ko', 'yue'], default='zh')
    parser.add_argument('--allow-empty', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(transcribe(args.input, args.model_dir, args.out, args.fps,
                                   args.language, args.allow_empty), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
