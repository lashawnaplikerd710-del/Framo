"""Explicit hand-selected native regression; never a DeepSeek planning result."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.runtime import load_config
from agent_core.task_store import TaskStore, atomic_json
from agent_core.montage import compile_montage
from agent_core.quality_controller import check_native_draft


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', required=True)
    parser.add_argument('--job', required=True)
    parser.add_argument('--out', required=True)
    args = parser.parse_args()
    store = TaskStore(args.state_dir)
    task = store.get_job(args.job)['task_spec']
    candidates = json.loads((store.job_dir(args.job) / 'evidence.json').read_text(encoding='utf-8'))['segments']
    assets = [store.get_asset(aid) for aid in task['asset_ids']]
    chosen = []
    for asset in assets:
        rows = [r for r in candidates if r['asset_id'] == asset['asset_id']]
        voice = [r for r in rows if r['kind'] == 'complete_detected_speech_with_tail']
        visual = [r for r in rows if r['kind'] == 'visual_window_no_speech_detected']
        chosen.extend(voice[:1] if voice else visual[:2] if visual else rows[:1])
    plan = {'schema_version': '1.0', 'clips': [
        {'source_segment_id': r['segment_id'], 'role': 'regression_fixture',
         'reason': 'Hand-selected native regression fixture, not model decision',
         'evidence_ids': [r['evidence_id']]} for r in chosen]}
    config = load_config(ROOT / 'config.example.json')
    output = Path(args.out).resolve()
    if output.exists():
        raise FileExistsError('Use a new smoke test directory')
    output.mkdir(parents=True)
    timeline = compile_montage(task, assets, candidates, plan)
    atomic_json(output / 'native-plan.json', timeline['native_plan'])
    atomic_json(output / 'timeline.json', timeline)
    sys.path.insert(0, str(Path(config['toolchain_root']) / 'scripts'))
    from native_timeline import build
    from native_preview import preview
    from draft_quality import audit
    build(output / 'native-plan.json', config['install_dir'], output / 'native-build')
    snapshot_path = output / 'native-build/draft-snapshot.json'
    snapshot = json.loads(snapshot_path.read_text(encoding='utf-8-sig'))
    quality = check_native_draft(snapshot, timeline, audit(snapshot, base_dir=snapshot_path.parent),
                                editable_captions=False,
                                source_manifest={a['asset_id']: a for a in assets})
    if quality['issues']:
        atomic_json(output / 'readback-failed.json', quality)
        raise ValueError('Native multi-source readback failed; inspect readback-failed.json')
    positions = [(row['timeline_start_us'] + row['timeline_end_us']) // 2 for row in timeline['source_map']]
    frames = preview(config['install_dir'], output / 'native-build/draft',
                     output / 'native-preview', positions, 360)
    report = {'schema_version': '1.0', 'deepseek_used': False, 'registered_to_homepage': False,
              'source_count': len(assets), 'segment_count': len(chosen),
              'duration_us': timeline['duration_us'], 'readback': quality,
              'native_preview': frames, 'continuous_av_reviewed': False}
    atomic_json(output / 'smoke-report.json', report)
    print(json.dumps({'status': 'native_montage_smoke_passed', 'deepseek_used': False,
                      'duration_us': timeline['duration_us'], 'source_count': len(assets),
                      'segment_count': len(chosen), 'report': str(output / 'smoke-report.json')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
