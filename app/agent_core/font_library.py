"""Portable font selector for Framo. Pillow is needed only by load_pillow_font()."""
from pathlib import Path
from functools import lru_cache
import hashlib
import json


@lru_cache(maxsize=1)
def bundled_font_library():
    return FontLibrary(Path(__file__).resolve().parents[1] /
                       'desktop-assets/media-library/font-library')


def validate_subtitle_font_id(value):
    """Accept only a bundled family ID; an empty preference means automatic."""
    if value is None or value == '':
        return None
    if not isinstance(value, str) or value not in bundled_font_library().families:
        raise ValueError('所选字幕字体不在内置字体库中，请重新选择')
    library = bundled_font_library()
    for record in library.families[value]['files']:
        library._path(record)
    return value


class FontLibrary:
    def __init__(self, root=None):
        self.root = Path(root or Path(__file__).parent).resolve()
        self.manifest = json.loads((self.root / 'manifest.json').read_text(encoding='utf-8'))
        self.families = {item['id']: item for item in self.manifest['families']}
        self._verified = set()

    def _path(self, record):
        path = (self.root / record['file']).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('Font path leaves library')
        if record['file'] not in self._verified:
            if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
                raise ValueError('Font checksum mismatch: ' + record['file'])
            self._verified.add(record['file'])
        return path

    @staticmethod
    def missing(record, text):
        ranges = record['unicode_ranges']
        return sorted({c for c in text if not c.isspace()
                       and not any(a <= ord(c) <= b for a, b in ranges)})

    def select(self, text, language='zh-CN', role='body', tags=(), preferred=None):
        """Return a verified font for the entire text; explicit locale is required.

        Tags: clean/professional/technology/soft/cute/lifestyle/playful/bold/
        commerce/energetic/traditional/handwritten. This consumes video analysis,
        it does not analyze video itself. Unknown styles fall back to defaults.
        """
        if not isinstance(text, str) or not text.strip():
            raise ValueError('Require nonempty text')
        if language not in self.manifest['default_families']:
            raise ValueError('Supported locales: zh-CN, ja, en')
        if role not in ('body', 'heading', 'emphasis'):
            raise ValueError('Supported roles: body, heading, emphasis')
        if preferred is not None and preferred not in self.families:
            raise ValueError('Unknown preferred font')
        tags = set(tags)
        desired_weight = self.manifest['selection_policy'][role + '_weight']
        default = self.manifest['default_families'][language]
        candidates = []
        for family in self.families.values():
            if role not in family['roles']:
                continue
            for record in family['files']:
                if self.missing(record, text):
                    continue
                weight = record['weight']
                for axis in record['axes']:
                    if axis['tag'] == 'wght':
                        weight = max(axis['min'], min(axis['max'], desired_weight))
                score = 10 * len(tags.intersection(family['tags']))
                score += 100 if family['id'] == preferred else 0
                score += 1 if family['id'] == default else 0
                score -= abs(weight - desired_weight) / 1000
                # Locale guides typography; actual glyph coverage is decisive.
                # A Japanese task may contain an explicitly requested Chinese
                # heading. Never discard its characters to fit a locale font.
                locale_match = language in family['languages']
                candidates.append((locale_match, score, family['id'], record['file'], family, record, weight))
        if not candidates:
            usable = [r for f in self.families.values() if role in f['roles'] for r in f['files']]
            missing = [c for c in sorted(set(text)) if not c.isspace()
                       and not any(not self.missing(r, c) for r in usable)]
            detail = '、'.join(f'U+{ord(c):04X}' for c in missing[:12])
            raise ValueError('内置字体无法完整显示这段字幕；请补充字体或调整特殊字符'
                             + ('（缺少字符：'+detail+'）' if detail else '（需要支持混合文字的字体）'))
        locale_match, _, _, _, family, record, weight = max(candidates, key=lambda x: x[:4])
        return {'font_id': family['id'], 'label': family['label'],
                'path': str(self._path(record)), 'weight': weight,
                'axes': {a['tag']: weight if a['tag'] == 'wght' else a['default']
                         for a in record['axes']},
                'license': family['license'],
                'license_file': str(self.root / family['license_file']),
                'sha256': record['sha256'], 'role': role, 'language': language,
                'style_tags': family['tags'], 'requested_preference_applied': family['id'] == preferred,
                'locale_fallback': not locale_match,
                'coverage_policy': 'complete_text_verified',
                'fallback_reason': '目标语言字体缺少字符，使用完整覆盖原文的内置字体' if not locale_match else None}

    def load_pillow_font(self, selection, pixels):
        from PIL import ImageFont
        font = ImageFont.truetype(selection['path'], pixels)
        if selection['axes']:
            family = self.families[selection['font_id']]
            record = next(r for r in family['files'] if str(self.root / r['file']) == selection['path'])
            font.set_variation_by_axes([selection['axes'][a['tag']] for a in record['axes']])
        return font
