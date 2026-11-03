"""Two pinned, licensed font files shipped inside the tested Jianying install.

These are built-in font IDs belonging to this adapter, not invented online
resource IDs. No downloaded/system font or arbitrary path is accepted.
"""
from __future__ import annotations
import hashlib
from pathlib import Path
import struct

FONTS = {
    'source_han_cn': {
        'relative_path': 'Resources/Font/SystemFont/zh-hans.ttf',
        'sha256': '23c4c2392431725af8a8cf23b8e355c181ead687a43e79e842c9ae1647b293b2',
        'family': 'Source Han Sans CN Medium', 'license_marker': 'Apache License, Version 2.0',
        'license': 'Apache-2.0', 'script_scope': 'Chinese and Latin; other glyph coverage unverified'},
    'noto_sans': {
        'relative_path': 'Resources/Font/SystemFont/NotoSans-Regular.ttf',
        'sha256': '2ec33f84606cbaa0a1a944488e14f97faf2f6a25ecdd8354f5358f06da13c7d9',
        'family': 'Noto Sans', 'license_marker': 'SIL Open Font License, Version 1.1',
        'license': 'OFL-1.1', 'script_scope': 'Latin; Chinese must use source_han_cn'},
}


def name_records(data):
    """Bounded SFNT name-table reader. Called only after pinned SHA verification."""
    if len(data) < 12:
        raise ValueError('Font header is truncated')
    count = struct.unpack_from('>H', data, 4)[0]
    if count > 256 or 12 + count * 16 > len(data):
        raise ValueError('Invalid font table count')
    table = None
    for i in range(count):
        pos = 12 + 16*i
        if data[pos:pos+4] == b'name':
            offset, length = struct.unpack_from('>II', data, pos+8)
            if offset + length > len(data):
                raise ValueError('Font name table leaves file')
            table = data[offset:offset+length]
            break
    if table is None or len(table) < 6:
        raise ValueError('Font has no name evidence')
    _, rows, strings = struct.unpack_from('>HHH', table)
    if rows > 4096 or 6 + rows*12 > len(table):
        raise ValueError('Invalid font name records')
    result = {}
    for i in range(rows):
        platform, _, _, name_id, length, relative = struct.unpack_from('>6H', table, 6+12*i)
        if name_id not in (1, 13, 14):
            continue
        start = strings + relative
        if start + length > len(table):
            raise ValueError('Font name string leaves table')
        value = table[start:start+length].decode('utf-16-be' if platform in (0, 3) else 'mac_roman')
        if value not in result.setdefault(name_id, []):
            result[name_id].append(value)
    return result


def resolve_font(install, font_id='source_han_cn'):
    from .resource_policy import ResourcePolicyBlocked
    if not isinstance(font_id, str) or font_id not in FONTS:
        raise ResourcePolicyBlocked('unverified_font: only pinned Jianying built-in font IDs are supported')
    record = FONTS[font_id]
    root = Path(install).resolve(strict=True)
    path = root / record['relative_path']
    if path.resolve(strict=True) != path or not path.is_file():
        raise ValueError('Built-in font path cannot be redirected')
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if sha != record['sha256']:
        raise ValueError('Jianying built-in font changed; revalidate this installation')
    names = name_records(data)
    if record['family'] not in names.get(1, []) or not any(
            record['license_marker'] in value for value in names.get(13, [])):
        raise ValueError('Built-in font identity/license evidence does not match')
    return {'adapter_font_id': font_id, 'online_resource_id': None,
            'origin': 'jianying_installation_builtin', 'path': path.as_posix(),
            'sha256': sha, 'family': record['family'], 'license': record['license'],
            'license_evidence': 'pinned font name table, name ID 13',
            'license_text': names[13], 'license_urls': names.get(14, []),
            'member_required': False, 'entitlement_basis': 'bundled standard font with embedded open license; not an online premium resource',
            'script_scope': record['script_scope']}


def inventory(install):
    records = []
    for font_id in FONTS:
        try:
            records.append({'status': 'verified', **resolve_font(install, font_id)})
        except (OSError, ValueError) as exc:
            records.append({'adapter_font_id': font_id, 'status': 'unavailable', 'reason': str(exc)})
    return records


def validate_text(record, text):
    """Reject missing glyphs instead of silently rendering with an OS fallback."""
    data = Path(record['path']).read_bytes()
    if hashlib.sha256(data).hexdigest() != record['sha256']:
        raise ValueError('Font changed before glyph validation')
    count = struct.unpack_from('>H', data, 4)[0]
    cmap = None
    for i in range(count):
        p = 12 + 16*i
        if data[p:p+4] == b'cmap':
            start, length = struct.unpack_from('>II', data, p+8)
            cmap = data[start:start+length]; break
    if cmap is None:
        raise ValueError('Font has no verified Unicode glyph map')
    maps = []
    for i in range(struct.unpack_from('>H', cmap, 2)[0]):
        platform, encoding, offset = struct.unpack_from('>HHI', cmap, 4+8*i)
        if platform == 0 or (platform == 3 and encoding in (1, 10)):
            fmt = struct.unpack_from('>H', cmap, offset)[0]
            if fmt in (4, 12): maps.append((fmt, offset))
    def glyph(code, fmt, offset):
        if fmt == 12:
            n = struct.unpack_from('>I', cmap, offset+12)[0]
            for i in range(n):
                first, last, base = struct.unpack_from('>III', cmap, offset+16+12*i)
                if first <= code <= last: return base + code-first
            return 0
        if code > 0xFFFF: return 0
        n = struct.unpack_from('>H', cmap, offset+6)[0]//2
        for i in range(n):
            end = struct.unpack_from('>H', cmap, offset+14+2*i)[0]
            start = struct.unpack_from('>H', cmap, offset+16+2*n+2*i)[0]
            if start <= code <= end:
                delta = struct.unpack_from('>h', cmap, offset+16+4*n+2*i)[0]
                at = offset+16+6*n+2*i
                rel = struct.unpack_from('>H', cmap, at)[0]
                if rel == 0: return (code+delta)&0xFFFF
                index = struct.unpack_from('>H', cmap, at+rel+2*(code-start))[0]
                return (index+delta)&0xFFFF if index else 0
        return 0
    missing = sorted({ord(c) for c in text if not c.isspace() and
                      not any(glyph(ord(c), fmt, offset) for fmt,offset in maps)})
    if missing:
        raise ValueError('Built-in font lacks glyphs; external fallback refused: ' +
                         ', '.join(f'U+{n:04X}' for n in missing[:12]))
    return True
