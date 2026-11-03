"""Explicit hand-plan integration check; never evidence of a live model decision."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.local_executor import JianyingAdapter, LocalExecutor
from agent_core.runtime import load_config
from agent_core.task_service import TaskService
from agent_core.task_store import atomic_json, digest
from agent_core.timeline_compiler import compile_timeline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--job', required=True)
    parser.add_argument('--label', required=True)
    parser.add_argument('--segments', nargs='+', required=True)
    args = parser.parse_args()
    config = load_config(ROOT / 'config.example.json')
    service = TaskService(config, ROOT / 'data')
    job = service.get_job(args.job)
    folder = service.store.job_dir(args.job) / 'manual-native-smoke' / args.label
    if folder.exists():
        raise FileExistsError('Use a new smoke-test label')
    folder.mkdir(parents=True)
    task = job['task_spec']
    asset = service.store.get_asset(task['asset_ids'][0])
    transcript = json.loads((service.store.job_dir(args.job) / 'transcript.json').read_text(encoding='utf-8'))
    evidence = json.loads((service.store.job_dir(args.job) / 'evidence.json').read_text(encoding='utf-8'))
    known = {row['segment_id']: row for row in evidence['segments']}
    plan = {'schema_version': '1.0', 'revision_id': 'revision_manual_smoke',
            'clips': [{'source_segment_id': sid, 'role': 'test_cut',
                       'reason': 'Explicit hand-selected native plumbing test only',
                       'evidence_ids': [known[sid]['evidence_id']]} for sid in args.segments]}
    timeline = compile_timeline(task, asset, transcript, evidence['segments'], plan)
    atomic_json(folder / 'edit-plan.json', plan)
    atomic_json(folder / 'timeline.json', timeline)
    atomic_json(folder / 'native-plan.json', timeline['native_plan'])
    atomic_json(folder / 'output-captions.json', timeline['captions'])
    executor = LocalExecutor(ROOT / 'python/python.exe', Path(config['toolchain_root']) / 'scripts',
                             config['install_dir'], service.store, args.job)
    adapter = JianyingAdapter(executor)
    build = adapter.build(folder / 'native-plan.json', folder / 'native-build')
    subtitle = adapter.add_subtitles(folder / 'native-build/draft', folder / 'output-captions.json',
                                     folder / 'native-subtitles', provider='manual_integration_local_asr')
    snapshot = json.loads((folder / 'native-subtitles/draft-snapshot.json').read_text(encoding='utf-8'))
    sys.path.insert(0, str(Path(config['toolchain_root']) / 'scripts'))
    try:
        from draft_quality import audit
        quality = audit(snapshot, base_dir=folder / 'native-subtitles')
    finally:
        sys.path.pop(0)
    report = {'schema_version': '1.0', 'status': 'manual_plan_native_smoke',
              'deepseek_used': False, 'source_asset_id': asset['asset_id'],
              'draft': str(folder / 'native-subtitles/draft'),
              'draft_sha256': digest(folder / 'native-subtitles/draft/draft_content.json'),
              'duration_us': snapshot['duration'], 'expected_duration_us': timeline['duration_us'],
              'caption_count': len(timeline['captions']),
              'subtitle_report_status': subtitle['status'],
              'native_build_status': build['status'],
              'structural_status': quality['status'], 'issues': quality['issues'],
              'preview_frames': subtitle['frames'],
              'editor_open_verified': False, 'final_export_created': False}
    atomic_json(folder / 'manual-integration-report.json', report)
    print(json.dumps({'report': str(folder / 'manual-integration-report.json'),
                      'draft': report['draft'], 'duration_us': report['duration_us'],
                      'captions': report['caption_count'], 'issues': len(report['issues'])},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
