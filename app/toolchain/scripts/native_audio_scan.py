"""Sample only an owned Jianying player's audio meter across every timeline window.

This measures an output signal at recorded native positions, not audio content,
integrated loudness, human listening, or lip sync. It never records audio.
"""
import argparse
import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import subprocess
import sys
import time

from native_draft_codec import file_digest
from native_frames import capture_at, prime
from native_service import Service

POLL_SECONDS = .1
MAX_GAP_US = 500_000
# Native playback needs a short warm-up before the first position-linked
# meter sample becomes available.  Keep the edge tolerance above that normal
# startup latency so the muted-control pass does not reject an otherwise
# complete scan.
EDGE_SLACK_US = 500_000
PEAK_EPSILON = .0001


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')


def plan_windows(duration_us, window_us=60_000_000):
    if type(duration_us) is not int or duration_us <= 0:
        raise ValueError('Positive integer draft duration required')
    if type(window_us) is not int or not 1_000_000 <= window_us <= 60_000_000:
        raise ValueError('Window must be between 1 and 60 seconds')
    if duration_us > 24 * 60 * 60 * 1_000_000:
        raise ValueError('Split drafts longer than 24 hours into bounded tasks')
    return [{'start_us': start, 'end_us': min(duration_us, start + window_us)}
            for start in range(0, duration_us, window_us)]


def analyse_samples(rows, start_us, end_us, owned_pid, *, max_gap_us=MAX_GAP_US,
                    edge_slack_us=EDGE_SLACK_US):
    """Summarize actual samples; planned playback time never counts as coverage."""
    errors = []
    valid = []
    for row in rows:
        peak, pos = row.get('peak'), row.get('position_us')
        elapsed = row.get('elapsed_us')
        if (row.get('meter_pid') != owned_pid or type(pos) is not int or
                type(elapsed) is not int or not isinstance(peak, (int, float)) or
                isinstance(peak, bool) or not math.isfinite(peak) or peak < 0 or
                pos < max(0, start_us - 40_000) or pos > end_us + edge_slack_us):
            errors.append('Sample lacks a valid owned meter, position, or finite peak')
            continue
        valid.append(row)
    if len(valid) < 3:
        errors.append('Fewer than three actual meter samples')
    position_gaps = [b['position_us'] - a['position_us'] for a, b in zip(valid, valid[1:])]
    wall_gaps = [b['elapsed_us'] - a['elapsed_us'] for a, b in zip(valid, valid[1:])]
    if any(gap < 0 for gap in position_gaps) or any(gap <= 0 for gap in wall_gaps):
        errors.append('Native position or monotonic sample clock moved backwards')
    query_gaps = [row.get('position_query_elapsed_us', max_gap_us + 1) for row in valid]
    maximum_position_gap = max(position_gaps, default=None)
    maximum_wall_gap = max(wall_gaps, default=None)
    if any(gap > max_gap_us for gap in position_gaps + wall_gaps + query_gaps):
        errors.append('Meter polling or native-position association exceeded declared gap tolerance')
    first = valid[0]['position_us'] if valid else None
    last = valid[-1]['position_us'] if valid else None
    start_gap = max(0, first - start_us) if first is not None else end_us - start_us
    end_gap = max(0, end_us - last) if last is not None else end_us - start_us
    if start_gap > edge_slack_us or end_gap > edge_slack_us:
        errors.append('Actual samples did not reach both window edges within declared slack')
    if first is None or last - first < min(100_000, (end_us - start_us) // 2):
        errors.append('Native playback clock did not advance sufficiently')
    peak = max((row['peak'] for row in valid), default=None)
    nonzero = sum(row['peak'] > PEAK_EPSILON for row in valid)
    return {
        'coverage_complete_within_tolerances': not errors,
        'errors': sorted(set(errors)), 'actual_meter_samples': len(valid),
        'first_observed_position_us': first, 'last_observed_position_us': last,
        'observed_span_us': max(0, last - first) if valid else 0,
        'start_unobserved_us': start_gap, 'end_unobserved_us': end_gap,
        'largest_native_position_gap_us': maximum_position_gap,
        'largest_meter_poll_gap_us': maximum_wall_gap,
        'largest_position_query_elapsed_us': max(query_gaps, default=None),
        'peak_meter_max': peak, 'nonzero_meter_samples': nonzero,
        'zero_signal_needs_review': bool(valid) and nonzero == 0,
        'continuous_audio_samples_verified': False,
    }


def verify_artifacts(report):
    """Verify the exact draft and every persisted window, not a copied status flag."""
    errors = []
    source = Path(report['draft_file'])
    if not source.is_file() or file_digest(source) != report['draft_sha256']:
        errors.append('Draft missing or changed')
    planned = report.get('planned_windows', [])
    cursor = 0
    for row in planned:
        if row.get('start_us') != cursor or type(row.get('end_us')) is not int or row['end_us'] <= cursor:
            errors.append('Planned windows are not consecutive')
            break
        cursor = row['end_us']
    if not planned or cursor != report.get('duration_us'):
        errors.append('Planned windows do not cover the full draft duration')
    for index, window in enumerate(report.get('windows', [])):
        if index >= len(planned) or (window.get('start_us'), window.get('end_us')) != (
                planned[index].get('start_us'), planned[index].get('end_us')):
            errors.append('Persisted window differs from planned timeline range')
        path = Path(window['evidence_path'])
        if not path.is_file() or file_digest(path) != window['sha256']:
            errors.append('Window evidence missing or changed: ' + str(path))
            continue
        saved = json.loads(path.read_text(encoding='utf-8'))
        if saved.get('draft_sha256') != report['draft_sha256']:
            errors.append('Window evidence belongs to a different draft')
        if (saved.get('start_us'), saved.get('end_us')) != (window['start_us'], window['end_us']):
            errors.append('Window evidence range mismatch')
    if len(report.get('windows', [])) != len(planned):
        errors.append('Not all planned windows have persisted evidence')
    return errors


def _find_meter(owned_pid):
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    found = [session for session in AudioUtilities.GetAllSessions() if session.ProcessId == owned_pid]
    if len(found) > 1:
        raise RuntimeError('Multiple audio sessions for owned process; single-session evidence is ambiguous')
    if not found:
        return None, None
    session = found[0]
    info = {'pid': owned_pid, 'session_muted': bool(session.SimpleAudioVolume.GetMute()),
            'session_volume': float(session.SimpleAudioVolume.GetMasterVolume())}
    return session._ctl.QueryInterface(IAudioMeterInformation), info


def _observe(svc, sid, consumer, start_us, end_us, destination):
    """Associate each meter read with an actual recent player position."""
    origin = time.monotonic()
    deadline = origin + (end_us - start_us) / 1_000_000 + 8
    rows, meter, info = [], None, None
    destination['samples'] = rows
    destination['audio_session'] = None
    played = svc.request('player', 'play', {}, sid)['result']
    if played.get('deferred'):
        raise RuntimeError('Native playback deferred despite attached frame consumer')
    try:
        while time.monotonic() < deadline:
            if consumer.poll() is not None:
                raise RuntimeError('Owned frame consumer exited before window completion')
            if meter is None:
                meter, info = _find_meter(svc.pid)
                destination['audio_session'] = info
            query_start = time.monotonic()
            state = svc.request('player', 'getState', {}, sid)['result']
            query_end = time.monotonic()
            peak = float(meter.GetPeakValue()) if meter is not None else None
            sampled = time.monotonic()
            position = state.get('positionUs')
            rows.append({'elapsed_us': round((sampled - origin) * 1_000_000),
                         'position_us': position, 'peak': peak,
                         'meter_pid': svc.pid if meter is not None else None,
                         'position_query_elapsed_us': round((query_end - query_start) * 1_000_000),
                         'meter_delay_after_position_query_us': round((sampled - query_end) * 1_000_000)})
            if type(position) is int and position >= end_us - 40_000:
                return
            time.sleep(POLL_SECONDS)
        raise TimeoutError('Native playback did not reach the requested window end')
    finally:
        svc.request('player', 'pause', {}, sid)


def _window(install, source, draft_hash, snapshot, output, planned):
    result = {'schema': 'jianying-native-audio-window-v1', 'draft_sha256': draft_hash,
              **planned, 'status': 'observing', 'muted_control': {}, 'signal': {}}
    output.mkdir()
    consumer, sid = None, None
    try:
        if file_digest(source) != draft_hash:
            raise RuntimeError('Draft changed before window playback')
        with Service(install, output / 'service', request_timeout=5) as svc:
            result['owned_pid'] = svc.pid
            width = 360
            canvas = snapshot['canvas_config']
            height = round(width * canvas['height'] / canvas['width'])
            if height > 1920:
                width = max(16, round(width * 1920 / height))
                height = 1920
            sid = svc.request('player', 'create', {'kind': 'draft', 'serviceGeneration': 1})['result']['sessionId']
            try:
                svc.request('player', 'load', {'draftPath': str(source), 'workspacePath': str(output)}, sid)
                prime(svc, sid, snapshot['duration'], width, height)
                ready = output / 'consumer-ready.json'
                consumer = subprocess.Popen(
                    [str(install / 'bun.exe'), str(Path(__file__).with_name('native_frame_consumer.mjs')),
                     svc.endpoint, sid, str(ready), str(width), str(height)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                attach_deadline = time.monotonic() + 5
                while not ready.exists():
                    if consumer.poll() is not None or time.monotonic() > attach_deadline:
                        raise RuntimeError('Owned native frame consumer did not attach')
                    time.sleep(.05)
                muted = copy.deepcopy(snapshot)
                for track in muted['tracks']:
                    if track['type'] in ('audio', 'video'):
                        for segment in track['segments']:
                            segment['volume'] = 0
                result['control_mute_scope'] = 'top_level_video_audio_segments_in_owned_memory_only'
                control_end = min(planned['end_us'], planned['start_us'] + 1_000_000)
                svc.request('edit', 'draft.setDraft', {'draftSnapshot': muted}, sid)
                capture_at(svc, sid, planned['start_us'], width, height)
                _observe(svc, sid, consumer, planned['start_us'], control_end, result['muted_control'])
                control = analyse_samples(result['muted_control']['samples'], planned['start_us'], control_end, svc.pid)
                result['muted_control']['coverage'] = control
                result['muted_control']['passed'] = (control['coverage_complete_within_tolerances'] and
                                                    control['peak_meter_max'] is not None and
                                                    control['peak_meter_max'] <= PEAK_EPSILON)
                svc.request('edit', 'draft.setDraft', {'draftSnapshot': snapshot}, sid)
                capture_at(svc, sid, planned['start_us'], width, height)
                _observe(svc, sid, consumer, planned['start_us'], planned['end_us'], result['signal'])
                coverage = analyse_samples(result['signal']['samples'], planned['start_us'], planned['end_us'], svc.pid)
                result['signal']['coverage'] = coverage
                if not coverage['coverage_complete_within_tolerances'] or not result['muted_control']['passed']:
                    result['status'] = 'incomplete'
                elif coverage['zero_signal_needs_review']:
                    result['status'] = 'sampled_zero_signal_needs_review'
                else:
                    result['status'] = 'sampled_within_declared_tolerances'
            finally:
                if consumer is not None:
                    if consumer.poll() is None:
                        consumer.terminate()
                    try:
                        consumer.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        consumer.kill()
                        consumer.wait(timeout=5)
                if sid is not None:
                    svc.request('player', 'destroy', {}, sid)
        if file_digest(source) != draft_hash:
            raise RuntimeError('Draft changed during window playback')
        result['draft_preserved'] = True
    except Exception as exc:
        result.update(status='incomplete', error=str(exc))
    finally:
        save(output / 'audio-window.json', result)
    return result


def scan(install, draft, output, window_us=60_000_000):
    install, source = Path(install).resolve(strict=True), Path(draft).resolve(strict=True)
    if source.is_dir():
        source = source / 'draft_content.json'
    source = source.resolve(strict=True)
    output = Path(output).resolve()
    if output.exists() or output.is_relative_to(source.parent) or output.is_relative_to(install):
        raise ValueError('Choose a new directory outside the draft and installation')
    digest = file_digest(source)
    output.mkdir(parents=True)
    report = {
        'schema': 'jianying-native-audio-scan-v1', 'status': 'incomplete',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'draft_file': str(source), 'draft_sha256': digest, 'planned_windows': [], 'windows': [],
        'method': 'Owned-process Windows Core Audio peak meter during native timeline playback',
        'policy': {'requested_poll_interval_us': round(POLL_SECONDS * 1_000_000),
                   'maximum_sample_gap_us': MAX_GAP_US, 'edge_slack_us': EDGE_SLACK_US,
                   'window_maximum_us': 60_000_000, 'peak_nonzero_epsilon': PEAK_EPSILON},
        'full_duration_signal_sampling_complete': False,
        'human_listening_verified': False, 'audio_content_verified': False,
        'lip_sync_verified': False, 'true_loudness_verified': False,
        'continuous_audio_samples_verified': False, 'audio_recorded': False,
        'system_volume_changed': False,
        'limits': ['Meter readings are associated with recent native player positions, not sample-accurate audio.',
                   'Coverage within declared polling/edge tolerances can miss shorter events.',
                   'Zero signal requires editorial review; intentional silence is not a definite error.',
                   'Raw session peak values are not integrated loudness or a clipping verdict.'],
    }
    try:
        with Service(install, output / 'source-probe', request_timeout=5) as svc:
            snapshot = json.loads(svc.request('draft', 'readFile', {
                'path': str(source), 'rootPath': str(source.parent)})['result']['content'])
        report['duration_us'] = snapshot['duration']
        report['planned_windows'] = plan_windows(snapshot['duration'], window_us)
        for index, planned in enumerate(report['planned_windows']):
            window_dir = output / ('window-%03d' % (index + 1))
            result = _window(install, source, digest, snapshot, window_dir, planned)
            evidence = window_dir / 'audio-window.json'
            report['windows'].append({**planned, 'evidence_path': str(evidence),
                                      'sha256': file_digest(evidence), 'status': result['status'],
                                      'owned_pid': result.get('owned_pid'),
                                      'coverage': result.get('signal', {}).get('coverage'),
                                      'muted_control_passed': result.get('muted_control', {}).get('passed', False)})
            print(json.dumps({'window': index + 1, 'total': len(report['planned_windows']),
                              'status': result['status']}, ensure_ascii=False), flush=True)
            save(output / 'audio-scan-report.json', report)
            if result.get('error'):
                raise RuntimeError(result['error'])
        integrity = verify_artifacts(report)
        report['integrity_errors'] = integrity
        complete = bool(report['windows']) and not integrity and all(
            item['status'] in ('sampled_within_declared_tolerances', 'sampled_zero_signal_needs_review')
            for item in report['windows'])
        report['full_duration_signal_sampling_complete'] = complete
        report['zero_signal_windows_needing_review'] = [i + 1 for i, item in enumerate(report['windows'])
                                                       if item['status'] == 'sampled_zero_signal_needs_review']
        report['status'] = ('signal_sampling_complete_with_review_flags' if report['zero_signal_windows_needing_review']
                            else 'signal_sampling_complete') if complete else 'incomplete'
        report['source_file_preserved'] = file_digest(source) == digest
    except Exception as exc:
        report['error'] = str(exc)
        report['status'] = 'incomplete'
        report['full_duration_signal_sampling_complete'] = False
    finally:
        save(output / 'audio-scan-report.json', report)
    return report


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('install-dir', 'draft', 'out'):
        parser.add_argument('--' + name, required=True)
    parser.add_argument('--window-seconds', type=int, default=60)
    args = parser.parse_args()
    try:
        result = scan(args.install_dir, args.draft, args.out, args.window_seconds * 1_000_000)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['full_duration_signal_sampling_complete'] else 2
    except Exception as exc:
        print(json.dumps({'status': 'incomplete', 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
