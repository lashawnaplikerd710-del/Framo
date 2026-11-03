"""Apply one bounded prior-model suggestion; does not make another model call."""
import argparse
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.runtime import load_config
from agent_core.task_service import TaskService
from agent_core.task_store import atomic_json, digest
from agent_core.review_repairs import replace_repeated_ending
from agent_core.local_executor import draft_lock, JianyingAdapter
from agent_core.quality_controller import check_native_draft
from agent_core.timeline_compiler import PlanIssue


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--state-dir', default=str(ROOT / 'data'))
    args = parser.parse_args()
    import numpy as np
    import sherpa_onnx
    config = load_config(ROOT / 'config.example.json')
    service = TaskService(config, args.state_dir)
    root = service.store.job_dir(args.job)
    def read(kind):
        artifact = service.store.artifact(args.job, kind, 'revision_001')
        if not artifact:
            raise ValueError('Missing checkpoint: ' + kind)
        return json.loads(Path(artifact['path']).read_text(encoding='utf-8'))
    task = service.get_job(args.job)['task_spec']
    assets = [service._check_asset(service.store.get_asset(a)) for a in task['asset_ids']]
    plan, review, candidates = read('edit_plan'), read('model_review'), read('evidence')['segments']
    transcripts = {a['asset_id']: read('transcript_' + a['asset_id']) for a in assets}
    last = next(c for c in candidates if c['segment_id'] == plan['clips'][-1]['source_segment_id'])
    asset = next(a for a in assets if a['asset_id'] == last['asset_id'])
    fps = task['target']['fps']
    first = ((max(c['end_us'] for c in transcripts[asset['asset_id']]['cues']) + 500_000) * fps + 999_999) // 1_000_000
    end = (asset['video_duration_us'] * fps // 1_000_000 - 1) * 1_000_000 // fps
    wav = root / 'sources' / asset['asset_id'] / 'asr/audio.wav'
    with wave.open(str(wav), 'rb') as reader:
        rate = reader.getframerate()
        if (rate, reader.getnchannels(), reader.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Unexpected decoded audio format')
        samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype='<i2').astype(np.float32) / 32768
    model = Path(config['asr_model_dir'])
    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(model=str(model / 'model.int8.onnx'),
        tokens=str(model / 'tokens.txt'), num_threads=4, use_itn=True, language='ja', debug=False)
    probes, result = [], None
    for offset in (0, fps // 2, fps):
        start = (first + offset) * 1_000_000 // fps
        if end - start < 1_000_000:
            continue
        stream = recognizer.create_stream()
        stream.accept_waveform(rate, samples[start * rate // 1_000_000:end * rate // 1_000_000])
        recognizer.decode_stream(stream)
        probe = {'schema_version': '1.0', 'asset_id': asset['asset_id'],
            'source_sha256': asset['sha256'], 'decoded_audio_sha256': digest(wav),
            'range_us': [start, end], 'engine': 'local_sensevoice_tail_probe',
            'raw_result': json.loads(str(stream.result)), 'silence_verified': False}
        probes.append(probe)
        try:
            result = replace_repeated_ending(task, assets, candidates, plan, review, transcripts, probe)
        except PlanIssue:
            continue
        break
    if result is None:
        raise PlanIssue('No independently probed tail and legal duration were found; retain the original draft')
    revised, evidence, timeline, resolution = result
    out = root / 'revisions/002'
    with draft_lock(root / '.job-run.lock'):
        if out.exists():
            raise FileExistsError('Repair revision already exists; inspect before retrying')
        service.store.event(args.job, 'REVISING', 'progress', {
            'message': '按 DeepSeek 复核意见修订重复结尾，保留四条来源', 'revision_id': 'revision_002'})
        for name, value in [('tail-probes.json', {'schema_version': '1.0', 'probes': probes}),
                            ('edit_plan.json', revised), ('evidence.json', {'schema_version': '1.0', 'segments': evidence}),
                            ('timeline.json', timeline), ('native-plan.json', timeline['native_plan']),
                            ('repair-resolution.json', resolution)]:
            atomic_json(out / name, value)
        adapter = JianyingAdapter(service.executor_factory(args.job))
        adapter.build(out / 'native-plan.json', out / 'native-build')
        snapshot_path = out / 'native-build/draft-snapshot.json'
        snapshot = json.loads(snapshot_path.read_text(encoding='utf-8'))
        sys.path.insert(0, str(Path(config['toolchain_root']) / 'scripts'))
        from draft_quality import audit
        qc = check_native_draft(snapshot, timeline, audit(snapshot, base_dir=snapshot_path.parent),
                               editable_captions=False, source_manifest={a['asset_id']: a for a in assets})
        atomic_json(out / 'readback-report.json', qc)
        if qc['issues']:
            raise ValueError('Repair readback failed; see readback-report.json')
        positions = sorted({0, timeline['duration_us'] - 1, *[
            (r['timeline_start_us'] + r['timeline_end_us']) // 2 for r in timeline['source_map']]})
        preview = adapter.preview(out / 'native-build/draft', out / 'native-preview', positions)
        for kind, name in [('native_draft', 'native-build/draft/draft_content.json'),
                           ('timeline', 'timeline.json'), ('edit_plan', 'edit_plan.json'),
                           ('repair_resolution', 'repair-resolution.json'), ('readback_report', 'readback-report.json')]:
            service.store.put_artifact(args.job, kind, 'revision_002', out / name,
                                      verified=kind in ('native_draft', 'timeline', 'readback_report'))
        report = {'schema_version': '1.0', 'duration_us': timeline['duration_us'],
            'draft_readback_passed': True, 'native_preview_frame_count': len(preview['frames']),
            'source_count': len({r['asset_id'] for r in timeline['source_map']}),
            'model_review_request_followed': True, 'second_model_review_performed': False,
            'final_audio_listened': False}
        atomic_json(out / 'repair-build-report.json', report)
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
