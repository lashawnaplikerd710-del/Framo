"""Resolve short editorial accents by event; never put one ding on every cut."""
import hashlib
import json
import math
from pathlib import Path
import random
import re

POLICY = 'event_sfx_v1'


def catalog(root=None):
    root = Path(root or Path(__file__).resolve().parents[1] / 'desktop-assets/media-library/sound-library').resolve()
    manifest = root / 'manifest.json'
    if not manifest.is_file():
        return []
    rows, seen = [], set()
    for row in json.loads(manifest.read_text(encoding='utf-8')).get('files', []):
        path = (root / row['file']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        sha = hashlib.sha256(path.read_bytes()).hexdigest()
        if sha != row['sha256'] or sha in seen:
            continue
        if not 30_000 <= row['duration_us'] <= 1_000_000:
            continue
        seen.add(sha)
        rows.append({**row, 'path': str(path)})
    return rows


def arrange(task, timeline, plan, *, seed='', candidates=None):
    """Stable within a job, varied across jobs. Dense mode still avoids repeats."""
    candidates = catalog() if candidates is None else candidates
    unique = {row['sha256']: row for row in candidates}
    candidates = list(unique.values())
    instruction = task.get('instruction', '')
    from .original_montage import active as original_montage
    original=original_montage(task)
    dense = bool(re.search(r'(?:频繁|密集).{0,8}音效|音效.{0,8}(?:频繁|密集)|高能|快节奏', instruction))
    sparse = bool(re.search(r'少(?:加|用|量).{0,6}音效|音效.{0,6}(?:少|克制)|安静|舒缓', instruction))
    gap = 1_200_000 if dense and not sparse else 4_000_000 if sparse else 3_200_000
    total = int(timeline['duration_us'])
    maximum = min(12, max(1, math.ceil(total / (2_200_000 if dense and not sparse else 5_000_000))))
    if original and not sparse:
        dense=True;gap=1_200_000
        maximum=min(240,max(1,math.ceil(total/2_800_000)))
    rng = random.Random(hashlib.sha256(str(seed).encode()).hexdigest())
    slots = []
    for i, (slot, clip) in enumerate(zip(timeline.get('source_map', []), plan.get('clips', []))):
        role = str(clip.get('role', '')).lower()
        reason = str(clip.get('reason', ''))
        text = role + ' ' + reason
        point = int(slot['timeline_start_us']) + (150_000 if i == 0 else 0)
        if i == 0:
            family, event = 'reveal', 'opening_reveal'
        elif re.search(r'clos|cta|收束|结尾', text):
            family, event = 'accent', 'closing_emphasis'
        elif re.search(r'拉链|拆|移除|拿出|zip|remov|取出|press|按压', text, re.I):
            family, event = 'soft_tap', 'detail_emphasis'
        elif re.search(r'use|使用|进入|钻|走|动作|move|enter', text, re.I):
            family, event = 'movement', 'action_transition'
        elif re.search(r'proof|detail|细节|揭示|展示', text, re.I):
            family, event = 'accent', 'feature_reveal'
        elif dense:
            family, event = 'movement', 'rhythm_transition'
        else:
            continue
        slots.append({'start_us': point, 'preferred_family': family, 'event': event, 'clip_index': i+1})
    if original and not sparse:
        for i,slot in enumerate(timeline.get('source_map',[])):
            if i%3==1 and slot['timeline_end_us']-slot['timeline_start_us']>=3_000_000:
                slots.append({'start_us':slot['timeline_start_us']+1_400_000,
                    'preferred_family':'accent','event':'small_graphic_accent','clip_index':i+1})
        slots.sort(key=lambda r:r['start_us'])
    chosen, last_sha, last_family, used_at = [], None, None, {}
    for slot in slots:
        if len(chosen) >= maximum or (chosen and slot['start_us'] - chosen[-1]['start_us'] < gap):
            continue
        pool = [r for r in candidates if r['sha256'] != last_sha
                and slot['start_us'] - used_at.get(r['sha256'], -99_000_000) >= 8_000_000]
        if not pool:
            continue  # A single supplied sound plays once instead of looping.
        varied = [r for r in pool if r.get('family') != last_family]
        pool = varied or pool
        match = [r for r in pool if r.get('family') == slot['preferred_family']]
        row = rng.choice(match or pool)
        duration = min(int(row['duration_us']), total-slot['start_us'])
        if duration <= 0:
            continue
        # Accents stay below speech; don't normalize every asset to a loud ding.
        gain = float(row.get('recommended_gain', .16))
        if task.get('narration_mode') == 'new_native' or original:
            gain *= .72
        chosen.append({**slot, 'sound': row, 'duration_us': duration, 'volume': round(gain, 4)})
        last_sha, last_family = row['sha256'], row.get('family')
        used_at[last_sha] = slot['start_us']
    return {'policy': POLICY, 'density': 'dense' if dense and not sparse else 'sparse' if sparse else 'balanced',
            'min_gap_us': gap, 'catalog_count': len(candidates), 'events': chosen,
            'audio_listened': False}
