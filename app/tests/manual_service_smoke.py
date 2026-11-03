"""Real local pipeline with a scripted planner; never a DeepSeek API test."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent_core.runtime import load_config
from agent_core.task_service import TaskService, task_spec


SEGMENTS = ('segment_0001', 'segment_0002', 'segment_0003', 'segment_0004',
            'segment_0006', 'segment_0007', 'segment_0008', 'segment_0009',
            'segment_0011', 'segment_0012')


class ScriptedPlanner:
    def __init__(self):
        self.config = {'model': 'SCRIPTED_TEST_PROVIDER_NOT_DEEPSEEK'}
        self.usage = {'model_calls': 0, 'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}

    def understand(self, task, asset):
        return {'schema_version': '1.0', 'editorial_goal': 'Test only; no model interpretation',
                'selection_priority': ['fixed fixture segments'], 'uncertainty': 'not a real provider'}

    def plan(self, task, candidates, frames, *, previous_issue=None):
        known = {row['segment_id']: row for row in candidates}
        return {'schema_version': '1.0', 'clips': [{'source_segment_id': sid,
                 'role': 'test', 'reason': 'Scripted deterministic integration test only',
                 'evidence_ids': [known[sid]['evidence_id']]} for sid in SEGMENTS],
                'visual_assessment': {'burned_captions': 'present',
                    'evidence_frame_times_us': [frame['requested_us'] for frame in frames]},
                'visual_observations': [], 'provider_identity': 'SCRIPTED_TEST_PROVIDER_NOT_DEEPSEEK'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True)
    parser.add_argument('--state-dir', required=True)
    args = parser.parse_args()
    config = load_config(ROOT / 'config.example.json')
    service = TaskService(config, args.state_dir, provider_factory=ScriptedPlanner)
    asset_id = service.register_asset(args.input)
    job_id = service.create_job(task_spec('剪成60秒竖屏口播视频，保留核心观点，添加中文字幕',
                                          [asset_id], 'talking_head'))
    state = service.start_job(job_id)
    print(json.dumps({'job_id': job_id, 'state': state['state'], 'error': state['error'],
                      'delivery': str(service.store.job_dir(job_id) / 'delivery.json'),
                      'deepseek_used': False}, ensure_ascii=False, indent=2))
    if state['state'] != 'READY_FOR_REVIEW':
        raise SystemExit(2)


if __name__ == '__main__':
    main()
