"""Offline, script-bound speech library. No network or credentials at runtime."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid
import wave

VERSION = 'local-voice-v2'


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def library_root(config):
    return (config.get('direct_media') or {}).get('local_voice_library')


def inside(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root):
        raise ValueError('音色库路径越界')
    return path


def catalog(root):
    result = json.loads((Path(root) / 'catalog.json').read_text(encoding='utf-8'))
    voices = result['voices']
    ids = [v['id'] for v in voices]
    if len(ids) != len(set(ids)) or result.get('schema_version') != 1:
        raise ValueError('音色目录无效')
    return result


def choose(root, locale, voice_id=None):
    data = catalog(root)
    voice_id = voice_id or data['defaults'].get(locale)
    for voice in data['voices']:
        if voice['id'] == voice_id:
            if voice['locale'] != locale:
                raise ValueError(f'音色 {voice_id} 不支持 {locale}，不会自动换语言')
            return voice
    raise ValueError(f'没有可用的 {locale} 音色: {voice_id}')


def validate_wav(path):
    # Decode every PCM sample; reject empty, non-finite/clipped and silent output.
    import array
    with wave.open(str(path), 'rb') as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() != 1 or wav.getcomptype() != 'NONE':
            raise ValueError('配音必须为单声道 PCM16 WAV')
        frames, rate = wav.getnframes(), wav.getframerate()
        samples = array.array('h', wav.readframes(frames))
    if not samples or len(samples) != frames or rate < 16000:
        raise ValueError('配音文件为空或未完整解码')
    rms = (sum(float(x)*x for x in samples)/len(samples)) ** .5 / 32768
    clipping = sum(abs(x) >= 32760 for x in samples) / len(samples)
    if rms < .0005 or clipping > .01:
        raise ValueError('配音静音或严重削波')
    return {'duration_us': round(frames/rate*1e6), 'sample_rate': rate,
            'rms': rms, 'clipping_fraction': clipping, 'decode_passed': True}


def speech_chunks(text, maximum=160):
    parts=[]
    while text:
        end=min(len(text),maximum)
        if len(text)>maximum:
            boundaries=[m.end() for m in re.finditer(r'[。！？!?；;，,\n]',text[:maximum])]
            if boundaries and boundaries[-1]>=maximum//2:
                end=boundaries[-1]
        parts.append(text[:end]);text=text[end:]
    return parts


def attribution_text(voice):
    """Keep publication credits with the audio, without altering the spoken copy."""
    if not voice.get('attribution_required'):
        return ''
    credit = voice.get('attribution', '').strip()
    terms = voice.get('terms_url', '').strip()
    if not credit or not terms:
        raise ValueError('该音色缺少发布署名或使用条款，不能生成配音')
    return ('配音发布署名说明\n\n'
            f"音色：{voice.get('label', voice['id'])}\n"
            f'发布视频时，请在视频说明或视频内保留以下署名：\n{credit}\n\n'
            f'音色使用条款：{terms}\n'
            '模型使用条款：https://github.com/VOICEVOX/voicevox_vvm\n'
            '署名说明文件本身不代替发布时的署名；请同时遵守音色的使用条款。\n')


def _write_attribution(cache_root, voice):
    notice = attribution_text(voice)
    if not notice:
        return None
    # Voice IDs are catalog values, but still enforce the same path boundary.
    path = inside(cache_root, Path('cache')/'credits'/(voice['id']+'.txt'))
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:
        part.write_text(notice, encoding='utf-8')
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
    return path


def _load_voicevox(root, model, voice):
    # The official CPU core and its Python modules stay in this model's private
    # directory. The desktop and other speech engines do not import them.
    sys.path.insert(0, str(inside(root, model['python'])))
    from voicevox_core.blocking import Onnxruntime, OpenJtalk, Synthesizer, VoiceModelFile
    synthesizer = Synthesizer(
        Onnxruntime.load_once(filename=str(inside(root, model['onnxruntime']))),
        OpenJtalk(str(inside(root, model['dictionary']))),
        acceleration_mode='CPU', cpu_num_threads=2)
    with VoiceModelFile.open(str(inside(root, model['voice_model']))) as loaded:
        matches = [speaker for speaker in loaded.metas
                   if speaker.speaker_uuid == voice['speaker_uuid']]
        if (len(matches) != 1 or not any(style.id == voice['speaker_id']
                                       for style in matches[0].styles)):
            raise ValueError('日语模型中的说话人与所选音色不一致')
        synthesizer.load_voice_model(loaded)
    return synthesizer


def generate(root, texts, locale, work_dir, *, voice_id=None):
    """One speaker per item; worker caches each exact script independently."""
    root = Path(root).resolve(strict=True)
    voice = choose(root, locale, voice_id)
    notice = attribution_text(voice)
    if not texts or len(texts) > 100 or any(not isinstance(t, str) or not t.strip() or len(t) > 6000 for t in texts):
        raise ValueError('配音文案为空或超过单次限制')
    for text in texts:
        if ((locale == 'en-US' and (not re.search('[A-Za-z]', text) or re.search(r'[\u3040-\u30ff\u3400-\u9fff]', text)))
                or (locale == 'ja-JP' and not re.search(r'[\u3040-\u30ff]', text))):
            raise ValueError('文案语言与所选音色不一致')
    work_dir = Path(work_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    # Installed releases keep generated speech out of the immutable library.
    cache_root = Path(os.environ.get('FRAMO_VOICE_CACHE') or root).resolve()
    (cache_root / 'tmp').mkdir(parents=True, exist_ok=True)
    request = work_dir / ('voice-request-' + uuid.uuid4().hex + '.json')
    result = request.with_suffix('.result.json')
    request.write_text(json.dumps({'root': str(root), 'cache_root': str(cache_root), 'texts': texts, 'voice': voice['id'],
                                   'locale': locale, 'result': str(result)}, ensure_ascii=False), encoding='utf-8')
    python = Path(sys.executable)
    if python.name.lower() == 'pythonw.exe':
        python = python.with_name('python.exe')
    env = dict(os.environ)
    env.update(PYTHONDONTWRITEBYTECODE='1', HF_HUB_OFFLINE='1',
               HF_HOME=str(cache_root/'cache'), XDG_CACHE_HOME=str(cache_root/'cache'),
               TEMP=str(cache_root/'tmp'), TMP=str(cache_root/'tmp'))
    try:
        run = subprocess.run([str(python), '-B', '-X', 'utf8', str(Path(__file__).with_name('local_voice_worker.py')),
                              str(request)], capture_output=True, encoding='utf-8', errors='replace',
                             timeout=max(120, min(3600, sum(len(t) for t in texts)*2)), env=env,
                             creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if run.returncode or not result.is_file():
            detail = ''
            if result.is_file():
                detail = json.loads(result.read_text(encoding='utf-8')).get('error', '')
            raise ValueError('本地配音失败: ' + (detail or run.stderr[-800:] or '进程未返回结果'))
        rows = json.loads(result.read_text(encoding='utf-8'))['items']
        if len(rows) != len(texts):
            raise ValueError('配音句数不一致')
        for row, text in zip(rows, texts):
            path = inside(cache_root, row['path'])
            if row['text'] != text or row['locale'] != locale or row['sha256'] != sha256(path):
                raise ValueError('配音返回文案、语言或指纹不一致')
            row['path'] = str(path)
            row['checks'] = validate_wav(path)
            if row.get('attribution_file'):
                row['attribution_file'] = str(inside(cache_root, row['attribution_file']))
        if notice:
            (work_dir/'配音署名.txt').write_text(notice, encoding='utf-8')
        return rows
    except subprocess.TimeoutExpired:
        raise ValueError('本地配音超时；已结束子进程，可重试并复用已完成的句子') from None
    finally:
        request.unlink(missing_ok=True)


def synthesize_request(request):
    """Worker only. Imports are isolated from the desktop application's packages."""
    root = Path(request['root']).resolve()
    data = catalog(root)
    voice = choose(root, request['locale'], request['voice'])
    model = data['models'][voice['engine']]
    if voice['engine'] not in ('kokoro', 'kokoro_zh', 'libritts', 'voicevox_metan', 'voicevox_nana'):
        raise ValueError('不支持的本地配音引擎：'+str(voice['engine']))
    for relative, expected in model['files'].items():
        if sha256(inside(root, relative)) != expected:
            raise ValueError('配音模型指纹不一致: ' + relative)
    cache_root = Path(request.get('cache_root') or root).resolve()
    cache = cache_root/'cache'/'audio'
    cache.mkdir(parents=True, exist_ok=True)
    credit_path = _write_attribution(cache_root, voice)
    rows, engine, g2p = [], None, None
    for text in request['texts']:
        key = hashlib.sha256(json.dumps([VERSION, voice, model['files'], text], ensure_ascii=False,
                                       sort_keys=True).encode('utf-8')).hexdigest()
        path = cache/(key+'.wav')
        receipt = path.with_suffix('.json')
        reusable = False
        if path.is_file() and receipt.is_file():
            try:
                saved = json.loads(receipt.read_text(encoding='utf-8'))
                reusable = saved.get('sha256') == sha256(path) and saved.get('key') == key
                if reusable:
                    validate_wav(path)
            except (ValueError, OSError, wave.Error):
                reusable = False
        if not reusable:
            import numpy as np
            if engine is None:
                if voice['engine'].startswith('voicevox_'):
                    engine = _load_voicevox(root, model, voice)
                elif voice['engine'] == 'kokoro_zh':
                    import sherpa_onnx
                    directory = inside(root, model['directory'])
                    os.chdir(directory)
                    config = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
                        kokoro=sherpa_onnx.OfflineTtsKokoroModelConfig(
                            model='model.int8.onnx', voices='voices.bin', tokens='tokens.txt',
                            data_dir='espeak-ng-data', dict_dir='dict',
                            lexicon='lexicon-zh.txt,lexicon-us-en.txt', lang='zh'),
                        num_threads=2, provider='cpu'),
                        rule_fsts='date-zh.fst,number-zh.fst,phone-zh.fst',max_num_sentences=1)
                    engine = sherpa_onnx.OfflineTts(config)
                elif voice['engine'] == 'libritts':
                    import sherpa_onnx
                    directory = inside(root, model['directory'])
                    # espeak uses narrow Windows paths. The isolated worker's
                    # Unicode-aware cwd lets it open an ASCII relative data path.
                    os.chdir(directory)
                    config = sherpa_onnx.OfflineTtsConfig(model=sherpa_onnx.OfflineTtsModelConfig(
                        vits=sherpa_onnx.OfflineTtsVitsModelConfig(model=str(directory/'en_US-libritts-high.onnx'),
                            tokens=str(directory/'tokens.txt'), data_dir='espeak-ng-data'),
                        num_threads=2, provider='cpu'))
                    engine = sherpa_onnx.OfflineTts(config)
                else:
                    from kokoro_onnx import Kokoro
                    from kokoro_onnx.config import EspeakConfig
                    import espeakng_loader
                    espeak_data = Path(espeakng_loader.get_data_path()).resolve()
                    os.chdir(espeak_data.parent)
                    engine = Kokoro(str(inside(root, model['model'])), str(inside(root, model['voices'])),
                                    espeak_config=EspeakConfig(data_path=espeak_data.name,
                                        lib_path=espeakng_loader.get_library_path()))
                    if voice['locale'] == 'ja-JP':
                        from misaki import ja
                        # Kokoro v1.0 expects Cutlet IPA, not the experimental
                        # OpenJTalk accent suffix (which can become audible).
                        g2p = ja.JAG2P()
            if voice['engine'].startswith('voicevox_'):
                # Read the complete official WAV rather than clipping it to a
                # shot or changing pitch to manufacture additional speakers.
                generated = engine.tts(text, int(voice['speaker_id']))
                with wave.open(io.BytesIO(generated), 'rb') as wav:
                    if wav.getsampwidth() != 2 or wav.getnchannels() != 1:
                        raise ValueError('日语配音引擎返回的音频格式无效')
                    rate = wav.getframerate()
                    samples = np.frombuffer(wav.readframes(wav.getnframes()), dtype='<i2').astype(np.float32)/32768
            elif voice['engine'] == 'kokoro_zh':
                # Bound each neural inference, then concatenate all complete
                # chunks at natural speed. Never drop or trim long narration.
                parts = speech_chunks(text)
                if ''.join(parts) != text:
                    raise ValueError('长旁白分段校验失败，原文未修改')
                chunks = [engine.generate(part,sid=int(voice['speaker_id']),speed=1.0) for part in parts]
                rate = chunks[0].sample_rate
                if any(chunk.sample_rate != rate for chunk in chunks):
                    raise ValueError('中文配音分段采样率不一致')
                samples = np.concatenate([np.asarray(chunk.samples) for chunk in chunks])
            elif voice['engine'] == 'libritts':
                audio = engine.generate(text, sid=voice['speaker_id'], speed=1.0)
                samples, rate = np.asarray(audio.samples), audio.sample_rate
            elif g2p:
                phonemes, _ = g2p(text)
                if not phonemes or '❓' in phonemes:
                    raise ValueError('日语文案含无法识别的发音')
                samples, rate = engine.create(phonemes, voice=voice['speaker_id'], speed=1.0, is_phonemes=True)
            else:
                samples, rate = engine.create(text, voice=voice['speaker_id'], speed=1.0, lang='en-us')
            samples = np.asarray(samples)
            if not samples.size or not np.isfinite(samples).all():
                raise ValueError('配音引擎返回空音频或非法采样')
            # Preserve the full sentence, including pauses; never trim to visual length.
            part = path.with_name(key+'.'+uuid.uuid4().hex+'.wav')
            try:
                with wave.open(str(part), 'wb') as wav:
                    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(int(rate))
                    wav.writeframes((np.clip(samples,-1,1)*32767).astype('<i2').tobytes())
                validate_wav(part)
                part.replace(path)
            finally:
                part.unlink(missing_ok=True)
            receipt_part = receipt.with_name(key+'.'+uuid.uuid4().hex+'.json')
            receipt_part.write_text(json.dumps({'key':key, 'sha256':sha256(path)}), encoding='utf-8')
            receipt_part.replace(receipt)
        row = {'path':str(path.relative_to(cache_root)), 'sha256':sha256(path), 'text':text,
                     'locale':voice['locale'], 'voice_id':voice['id'], 'provider':'local_neural_tts',
                     'engine':voice['engine'], 'cache_hit':reusable,
                     'review':{'status':'试听待确认', 'human_listened':False},
                     'license':model['license'], 'source':model['source']}
        if credit_path:
            row.update(attribution_required=True, attribution=voice['attribution'],
                       terms_url=voice['terms_url'],
                       attribution_file=str(credit_path.relative_to(cache_root)))
        rows.append(row)
    return {'items':rows}
