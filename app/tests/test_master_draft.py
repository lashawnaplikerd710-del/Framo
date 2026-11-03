import copy
import tempfile
import unittest
from pathlib import Path

from agent_core.master_draft import (MasterDraftError, compose_master,
                                     extract_output, validate_master,
                                     write_snapshot_draft)
from agent_core.batch_export import prepare_independent_outputs
from agent_core.batch_quality import inspect_batch_snapshot


def snapshot(name, duration=3_000_000):
    return {
        'id': name + '-draft', 'name': name, 'duration': duration, 'fps': 30,
        'canvas_config': {'width': 1080, 'height': 1920},
        'materials': {'videos': [{'id': name + '-material', 'path': name + '.mp4'}]},
        'tracks': [{'id': name + '-video', 'type': 'video', 'segments': [
            {'id': name + '-segment', 'material_id': name + '-material',
             'target_timerange': {'start': 0, 'duration': duration},
             'source_timerange': {'start': 0, 'duration': duration}}
        ]}],
    }


class MasterDraftTests(unittest.TestCase):
    def test_compose_uses_absolute_ranges_and_empty_all_track_gap(self):
        master, report = compose_master([('one', snapshot('one')), ('two', snapshot('two'))], gap_us=1_000_000)
        self.assertEqual(report['status'], 'clear')
        self.assertEqual(master['duration'], 7_000_000)
        self.assertFalse(master['config']['maintrack_adsorb'])
        self.assertFalse(master['batch_settings']['maintrack_adsorb'])
        self.assertEqual(master['batch_settings']['gap_us'], 1_000_000)
        ranges = [s['target_timerange'] for t in master['tracks'] for s in t['segments']]
        self.assertEqual(sorted(ranges, key=lambda r: r['start']), [
            {'start': 0, 'duration': 3_000_000},
            {'start': 4_000_000, 'duration': 3_000_000}])
        self.assertEqual(validate_master(master)['output_count'], 2)

    def test_default_master_gap_is_three_seconds(self):
        master, _ = compose_master([('one', snapshot('one')), ('two', snapshot('two'))])
        self.assertEqual(master['duration'], 9_000_000)
        self.assertEqual(master['batch_outputs'][0]['gap_after_us'], 3_000_000)
        self.assertEqual(master['batch_outputs'][1]['start_us'], 6_000_000)

    def test_extract_is_zero_based_and_rejects_crossing_segment(self):
        master, _ = compose_master([('one', snapshot('one')), ('two', snapshot('two'))])
        child = extract_output(master, 'two')
        self.assertEqual(child['duration'], 3_000_000)
        self.assertEqual(child['tracks'][0]['segments'][0]['target_timerange']['start'], 0)
        broken = copy.deepcopy(master)
        broken['tracks'][0]['segments'][1]['target_timerange']['start'] = 3_500_000
        with self.assertRaises(MasterDraftError):
            extract_output(broken, 'two')

    def test_write_temporary_draft_keeps_content_and_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            path = write_snapshot_draft(snapshot('one'), Path(temp) / 'draft')
            self.assertTrue((path / 'draft_content.json').is_file())
            self.assertTrue((path / 'draft_meta_info.json').is_file())
            self.assertTrue((path / 'batch-output.json').is_file())

    def test_independent_export_fallback_creates_zero_based_drafts(self):
        master, _ = compose_master([('one', snapshot('one')), ('two', snapshot('two'))])
        with tempfile.TemporaryDirectory() as temp:
            report = prepare_independent_outputs(master, Path(temp) / 'outputs')
            self.assertEqual(report['status'], 'temporary_drafts_ready')
            self.assertEqual(len(report['outputs']), 2)
            self.assertTrue((Path(report['outputs'][0]['draft_path']) / 'draft_content.json').is_file())

    def test_quality_report_keeps_black_frame_claim_pending(self):
        master, _ = compose_master([('one', snapshot('one')), ('two', snapshot('two'))])
        quality = inspect_batch_snapshot(master)
        self.assertEqual(quality['status'], 'clear')
        self.assertEqual(quality['evidence']['black_frame_scan'], 'pending_native_preview')


if __name__ == '__main__':
    unittest.main()
