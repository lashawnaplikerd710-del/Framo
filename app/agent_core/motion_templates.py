"""Framo adapter for the frozen public adu-motion-video distribution.

Plans/builds use upstream validators and renderers. A registered family is not a
completed film; failed bindings remain visible and never fall back to a demo.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile

from .task_store import atomic_json

ROOT = Path(__file__).resolve().parents[1]
PURPOSES = {
    '01': '达人口播、商品卖点、步骤与证据',
    '02': '商品对比、选购理由、使用方法',
    '03': '数码产品、细节放大、多屏展示',
    '04': '技术参数、功能说明、自动化流程',
    '05': '情境种草、故事、成果与反馈',
    '06': '开头大字、卖点清单、总结与行动引导',
}
SLUGS = dict(zip(PURPOSES, ('template-01-integrated', 'template-02-expanded',
    'template-03-native', 'template-04-dark-console', 'template-05-native', 'vibrant-poster')))
STATES = {'CREATED': '准备素材', 'PLANNING': '规划模板', 'NEEDS_INPUT': '需要调整',
          'READY': '方案已就绪', 'BUILDING': '构建工程', 'AUDIO': '制作声音',
          'RENDERING': '导出视频', 'CHECKING': '检查视频',
          'COMPLETED': '已导出，待看听', 'FAILED': '执行失败', 'CANCELLED': '已取消'}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def asset_digest(path):
    path = Path(path)
    if path.is_file():
        return sha(path)
    if not path.is_dir():
        raise ValueError('补充素材不存在。')
    files = sorted(p for p in path.rglob('*') if p.is_file())
    if not files or len(files) > 10000 or any(p.is_symlink() for p in path.rglob('*')):
        raise ValueError('序列素材目录需包含 1–10000 个普通文件。')
    digest = hashlib.sha256()
    for file in files:
        digest.update(file.relative_to(path).as_posix().encode('utf-8'))
        digest.update(bytes.fromhex(sha(file)))
    return digest.hexdigest()


def srt_rows(path):
    text = Path(path).read_text(encoding='utf-8-sig').replace('\r\n', '\n').strip()
    rows = []
    def seconds(value):
        h, m, s = value.replace(',', '.').split(':')
        return int(h)*3600 + int(m)*60 + float(s)
    for block in re.split(r'\n\s*\n', text):
        lines = block.splitlines()
        index = next((i for i, line in enumerate(lines) if '-->' in line), None)
        if index is None:
            raise ValueError('字幕格式无效，请选择 UTF-8 SRT 文件。')
        match = re.fullmatch(r'\s*(\d+:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d+:\d{2}:\d{2}[,.]\d{3})\s*', lines[index])
        if not match:
            raise ValueError('SRT 时间格式无效。')
        a, b = map(seconds, match.groups())
        body = '\n'.join(lines[index+1:]).strip()
        if not body or not 0 <= a < b or (rows and a < rows[-1]['end']-.001):
            raise ValueError('字幕为空、时间倒序或重叠，请校正后重试。')
        rows.append(dict(id=str(len(rows)+1), start=a, end=b, text=body))
    if not rows or len(rows) > 2000:
        raise ValueError('字幕需包含 1–2000 条有效内容。')
    return rows


class TemplateLibrary:
    def __init__(self, state_dir, root=ROOT):
        self.root = Path(root).resolve()
        self.skill = self.root/'skills/adu-motion-video'
        self.state = Path(state_dir).resolve()/'motion-templates'

    def catalog(self):
        path = self.skill/'references/template-catalog.json'
        if not path.is_file():
            return []
        result = []
        for family in read_json(path)['templates']:
            if family['id'] not in PURPOSES:
                continue
            styles = []
            for style in family['styles']:
                styles.append({**style, 'installed': self.resolve_pack(style, 'portrait', strict=False) is not None})
            result.append({**family, 'styles': styles, 'purpose': PURPOSES[family['id']],
                           'url': 'https://motiongit.com/@adunext/'+SLUGS[family['id']]})
        return result

    def resolve_pack(self, style, layout, *, strict=True):
        if layout not in ('portrait', 'landscape'):
            raise ValueError('请选择横屏或竖屏。')
        if layout not in style.get('layouts', {}):
            if strict:
                raise ValueError('此公开版风格尚不支持所选画幅。')
            return None
        selected = style['layouts'][layout].get('selection') or (
            style.get('portraitSelection') if layout == 'portrait' else style.get('selection'))
        for path in (self.skill/'packs').glob('**/manifest.json'):
            data = read_json(path)
            if f"{data.get('id')}@{data.get('version')}" == selected:
                return path
        if strict:
            raise ValueError('对应版本的模板包缺失，请修复 Skill 安装。')
        return None

    def style(self, style_id, layout='portrait'):
        for family in self.catalog():
            for style in family['styles']:
                if style['id'] == style_id:
                    self.resolve_pack(style, layout)
                    return style
        raise ValueError('模板风格不存在。')

    def environment(self):
        env = os.environ.copy()
        node_dir = self.root/'toolchain/node'
        env['PATH'] = os.pathsep.join([str(node_dir), str(self.root/'toolchain'), env.get('PATH', '')])
        env['PYTHONPATH'] = os.pathsep.join([str(self.skill/'python-libs'), env.get('PYTHONPATH', '')])
        env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
        # Never leak per-run rendering overrides from a parent development shell.
        for name in ('W', 'H', 'END', 'SILENT', 'HTML', 'VOICE', 'FPS', 'K'):
            env.pop(name, None)
        return env

    def python(self):
        return str(self.root/'python/python.exe') if (self.root/'python/python.exe').is_file() else sys.executable

    def check(self):
        env = self.environment()
        checks = [('Skill', (self.skill/'SKILL.md').is_file()),
                  ('Node.js', bool(shutil.which('node', path=env['PATH']))),
                  ('FFmpeg', bool(shutil.which('ffmpeg', path=env['PATH']))),
                  ('FFprobe', bool(shutil.which('ffprobe', path=env['PATH']))),
                  ('Playwright', (self.skill/'node_modules/playwright-core/package.json').is_file())]
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        probe = subprocess.run([self.python(), '-c', 'import numpy,PIL,scipy,cv2,fontTools'],
                               env=env, capture_output=True, creationflags=flags, timeout=45)
        checks.append(('Python 媒体依赖', probe.returncode == 0))
        node = shutil.which('node', path=env['PATH'])
        if node:
            browser = subprocess.run([node, '--input-type=module', '-e',
                "import {chrome} from './scripts/chrome.mjs'; console.log(chrome());"],
                cwd=self.skill, env=env, capture_output=True, creationflags=flags, timeout=45)
            checks.append(('Chrome / Edge', browser.returncode == 0))
        return checks

    def import_motion(self, source):
        """Import data only. Unknown package code is never dispatched as a renderer."""
        source = Path(source).resolve(strict=True)
        if source.suffix.lower() != '.motion' or source.stat().st_size > 512*1024*1024:
            raise ValueError('请选择不超过 512 MB 的 .motion 包。')
        digest = sha(source)
        target = self.state/'imports'/digest
        if target.exists():
            return target
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            total = 0
            names = set()
            for info in entries:
                name = info.filename.replace('\\', '/')
                parts = name.split('/')
                if (name.startswith('/') or ':' in name or '..' in parts or
                    (info.external_attr >> 16) & 0o170000 == 0o120000):
                    raise ValueError('模板包含不安全路径或符号链接，未导入。')
                normalized = name.rstrip('/').casefold()
                if normalized in names or any(p.rstrip(' .') != p for p in parts if p):
                    raise ValueError('模板包含重复或不兼容的文件名。')
                names.add(normalized)
                total += info.file_size
            if len(entries) > 20000 or total > 2*1024**3:
                raise ValueError('模板解压规模超出限制。')
            if 'motion.json' not in archive.namelist():
                raise ValueError('模板根目录缺少 motion.json。')
            manifest = json.loads(archive.read('motion.json').decode('utf-8-sig'))
            if manifest.get('format') != 'motiongit-template' or not isinstance(manifest.get('template'), dict):
                raise ValueError('不是支持的 MotionGit 模板格式。')
            # Keep the original private download intact; do not copy keys into a public bundle.
            target.mkdir(parents=True)
            shutil.copy2(source, target/'original.motion')
            atomic_json(target/'import.json', {'sha256': digest, 'name': source.name,
                'template': manifest['template'], 'status': 'imported-not-executed'})
        return target

    def jobs(self):
        rows = []
        for path in (self.state/'jobs').glob('*/job.json'):
            try:
                rows.append({**read_json(path), 'directory': str(path.parent)})
            except (OSError, ValueError):
                continue
        return sorted(rows, key=lambda r:r['created'], reverse=True)


class TemplateRunner:
    def __init__(self, library, cancel=None):
        self.library = library
        self.cancel = cancel or threading.Event()

    def reject_author_example(self, name):
        """Keep bundled author demos out of both new and previously planned films."""
        path = Path(name).resolve(strict=True)
        if path.is_relative_to(self.library.skill.resolve()):
            raise ValueError('不能将 Skill 自带的作者示例作为本期口播或商品素材。')
        if path.is_file():
            demos = self.library.skill/'assets/demos'
            for demo in demos.glob('*'):
                if (demo.is_file() and demo.suffix.lower() == path.suffix.lower()
                        and demo.stat().st_size == path.stat().st_size and sha(demo) == sha(path)):
                    raise ValueError('不能将作者示例的副本作为本期口播或商品素材。')
        return path

    def command(self, args, directory, *, accepted=(0,), timeout=3600):
        if self.cancel.is_set():
            raise InterruptedError('模板任务已取消。')
        env = self.library.environment()
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        log = Path(directory)/'pipeline.log'
        with log.open('ab') as stream:
            proc = subprocess.Popen([str(a) for a in args], cwd=self.library.skill, env=env,
                                    stdout=stream, stderr=subprocess.STDOUT, creationflags=flags)
            start = time.monotonic()
            while proc.poll() is None:
                if self.cancel.wait(.15) or time.monotonic()-start > timeout:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(proc.pid), '/T', '/F'],
                                       capture_output=True, creationflags=flags)
                    else:
                        proc.kill()
                    proc.wait()
                    if self.cancel.is_set():
                        raise InterruptedError('模板任务已取消。')
                    raise TimeoutError('模板步骤超时，请查看运行记录。')
        if proc.returncode not in accepted:
            raise ValueError('模板步骤未通过，详细原因已保存在 pipeline.log。')
        return proc.returncode

    def pipeline(self, args, directory, **kw):
        return self.command([self.library.python(), '-X', 'utf8',
            self.library.skill/'scripts/pipeline.py', *args], directory, **kw)

    def probe(self, source):
        tool = shutil.which('ffprobe', path=self.library.environment()['PATH'])
        if not tool:
            raise ValueError('缺少 FFprobe。')
        result = subprocess.run([tool, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(source)],
            capture_output=True, timeout=45, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            raise ValueError('视频无法读取，请选择完整的视频文件。')
        info = json.loads(result.stdout)
        video = next((s for s in info.get('streams', []) if s.get('codec_type') == 'video'), None)
        if video is None:
            raise ValueError('请选择包含画面的口播成片。')
        if not any(s.get('codec_type') == 'audio' for s in info.get('streams', [])):
            raise ValueError('口播视频没有音轨。')
        # Match adu's import_talk.input_timing exactly: the video stream's end
        # defines the 60 fps project clock, not a longer container/audio tail.
        # Otherwise a READY plan can immediately fail its exact-frame preflight.
        container = info.get('format', {})
        try:
            origin = float(container.get('start_time') or 0)
            offset = max(0., float(video.get('start_time') or origin) - origin)
            raw_duration = float(video.get('duration') or
                                 (float(container.get('duration') or 0) - offset))
            end = offset + raw_duration
            if not math.isfinite(end) or not 0 < end <= 600:
                raise ValueError
            frames = math.floor(end * 60 + .5)
            if frames < 1:
                raise ValueError
        except (TypeError, ValueError, OverflowError):
            raise ValueError('模板口播需在 0–600 秒内。') from None
        return frames / 60

    def update(self, directory, state, **values):
        path = Path(directory)/'job.json'
        job = read_json(path)
        job.update(state=state, **values)
        atomic_json(path, job)
        return job

    def plan(self, request, generate):
        style = self.library.style(request['style'], request['layout'])
        brand = request.get('brand', '').strip()
        if not brand or len(brand) > 100:
            raise ValueError('商品或品牌名称应为 1–100 个字符。')
        source = self.reject_author_example(request['source'])
        subtitle = Path(request['subtitle']).resolve(strict=True)
        rows = srt_rows(subtitle)
        duration = self.probe(source)
        if rows[-1]['end'] > duration+.15:
            raise ValueError('字幕时间超过视频时长，请使用与当前口播匹配的 SRT。')
        music = {'mode': 'none'}
        if request.get('music'):
            music = {'mode': 'track', 'path': str(Path(request['music']).resolve(strict=True)), 'offset': 0}
        directory = self.library.state/'jobs'/uuid.uuid4().hex
        directory.mkdir(parents=True)
        atomic_json(directory/'job.json', dict(id=directory.name, created=time.time(), state='CREATED',
            style=style['id'], style_title=style['title'], layout=request['layout'], source=str(source)))
        try:
            checks = self.library.check()
            missing = [name for name, okay in checks if not okay]
            if missing:
                raise ValueError('缺少运行依赖：'+'、'.join(missing))
            atomic_json(directory/'request.json', request)
            # Capture actual capabilities with the upstream module, filtered before model input.
            caps_file = directory/'capabilities.json'
            code = 'import json,sys;sys.path.insert(0,"scripts");from auto_templates import capabilities,library;d=capabilities(library(layout=sys.argv[1]),sys.argv[1]);d["groups"]=[g for g in d["groups"] if g["style"]==sys.argv[2]];open(sys.argv[3],"w",encoding="utf-8").write(json.dumps(d,ensure_ascii=False))'
            self.command([self.library.python(), '-X', 'utf8', '-c', code, request['layout'], style['id'], caps_file], directory)
            capabilities = read_json(caps_file)
            assets = []
            for name in request.get('media', []):
                path = self.reject_author_example(name)
                if path.is_dir():
                    files = [p for p in path.iterdir() if p.is_file() and p.suffix.lower() in {'.png','.jpg','.jpeg','.webp'}]
                    if not files:
                        raise ValueError('补充序列目录至少需要一张图片。')
                    assets.append({'path': str(path), 'name': path.name, 'kind': 'frames-folder', 'files': len(files)})
                elif path.suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.mp4','.mov','.mkv'}:
                    raise ValueError('补充素材只接受图片和视频。')
                else:
                    assets.append({'path': str(path), 'name': path.name, 'kind': 'file'})
            inputs = [source, subtitle, *([Path(music['path'])] if music['mode']=='track' else []),
                      *(Path(a['path']) for a in assets)]
            atomic_json(directory/'inputs.json', {str(p):asset_digest(p) for p in inputs})
            self.update(directory, 'PLANNING')
            guide = (self.library.skill/'references/adaptive-composition.md').read_text(encoding='utf-8')
            payload = dict(task='根据真实字幕编排所选模板；原片完整保留，不凭空添加商品事实。',
                requirements=request.get('instruction',''), style=style['id'], layout=request['layout'],
                brand=brand, durationFrames=round(duration*60), transcript=rows, media=assets, capabilities=capabilities,
                guide=guide, music=music)
            brief = generate(payload)
            if self.cancel.is_set():
                raise InterruptedError('模板任务已取消。')
            if not isinstance(brief, dict) or not isinstance(brief.get('segments'), list) or not brief['segments']:
                raise ValueError('模型未返回有效分镜，请调整要求后重新规划。')
            # The model cannot override user selection, media clock, music or repair policy.
            brief.update(schema='adu-auto-brief/1', layout=request['layout'], fps=60, brand=brand,
                allowedStyles=[style['id']], transcript=rows, narrationDuration=duration,
                music=music, fontPolicy='preferred', repairPolicy={'mode':'off'})
            brief.pop('segmentations', None)
            brief.pop('contentEvidence', None)  # Upstream derives evidence from original whole SRT cues.
            if sum(s.get('durationFrames',0) for s in brief['segments']) != round(duration*60):
                raise ValueError('分镜时长与口播不一致，请重新规划。')
            # Only actual supplied paths may be passed as media bindings.
            allowed_paths = {str(source), *(a['path'] for a in assets)}
            def check_paths(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in {'path','src','file','directory'} and isinstance(child,str) and (Path(child).is_absolute() or '/' in child or '\\' in child):
                            if str(Path(child).resolve()) not in allowed_paths:
                                raise ValueError('分镜引用了未提供的素材，请补充真实素材后重试。')
                        check_paths(child)
                elif isinstance(value,list):
                    for child in value: check_paths(child)
            check_paths(brief['segments'])
            atomic_json(directory/'brief.json', brief)
            # The subtitle renderer accepts original, unchanged timed text.
            self.command([self.library.python(), '-X', 'utf8',
                self.library.skill/'scripts/build_subs.py', '--srt', subtitle,
                '--out', directory/'subs.js'], directory)
            self.pipeline(['auto-plan', directory/'brief.json', directory/'plan'], directory, accepted=(0,2))
            report = read_json(directory/'plan/report.json')
            state = 'READY' if report.get('ready') else 'NEEDS_INPUT'
            self.update(directory, state, report=str(directory/'plan/report.md'))
            return directory
        except Exception as exc:
            self.update(directory, 'CANCELLED' if isinstance(exc,InterruptedError) else 'FAILED',
                        error=str(exc)[:1500])
            raise

    def render(self, directory):
        directory = Path(directory).resolve(strict=True)
        jobs_root = (self.library.state/'jobs').resolve()
        if directory.parent != jobs_root:
            raise ValueError('只能渲染本模板库创建的工程。')
        job = read_json(directory/'job.json')
        if job['state'] != 'READY' or not read_json(directory/'plan/report.json').get('ready'):
            raise ValueError('方案尚未通过检查，请先补齐素材并重新规划。')
        try:
            request = read_json(directory/'request.json')
            self.reject_author_example(job['source'])
            for name in request.get('media', []):
                self.reject_author_example(name)
            for name, digest in read_json(directory/'inputs.json').items():
                if asset_digest(name) != digest:
                    raise ValueError('输入素材已改变，请重新规划。')
            self.update(directory, 'BUILDING')
            project = directory/('project-'+uuid.uuid4().hex[:8])
            self.pipeline(['auto-build', directory/'plan/auto_plan.json', job['source'], project,
                           '--subs-js', directory/'subs.js'], directory)
            self.update(directory, 'AUDIO', project=str(project))
            self.pipeline(['audio', project], directory)
            self.pipeline(['mix', project], directory)
            self.update(directory, 'RENDERING')
            output = project/'Framo-template.mp4'
            self.pipeline(['render', project, output], directory)
            self.update(directory, 'CHECKING')
            self.pipeline(['check', output], directory)
            if not output.is_file() or output.stat().st_size == 0:
                raise ValueError('渲染没有生成有效 MP4。')
            self.update(directory, 'COMPLETED', output=str(output), quality='需人工连续看听确认')
            return directory
        except Exception as exc:
            self.update(directory, 'CANCELLED' if isinstance(exc,InterruptedError) else 'FAILED', error=str(exc)[:1500])
            raise


def model_generator(config):
    """Use Framo's configured provider and credential store; no new API account."""
    from .provider import ProviderClient
    from .gateway_credentials import target
    from .credential_store import load_key
    settings = dict(config['provider'])
    key = load_key(target=target(settings)) if settings.get('transport') == 'gateway' else None
    client = ProviderClient(settings, key_value=key)
    def generate(payload):
        role = ('你是 Framo 模板剪辑规划器。只返回 adu-auto-brief/1 JSON 对象。'
            '根据 guide 和 capabilities 生成 segments，每段 id/text/intent/phases/counts/durationFrames/anchors/candidates。'
            'candidates 的键必须是完整组 ID，值用 slots 按能力 fields 的 id 绑定文字和素材，或用 inputs 按 inputPath 绑定。'
            '只使用本次字幕事实和素材，保持否定、数字、条件；不增加商品效果、优惠、认证。'
            '按完整表达分段，不能为凑模板虚构阶段或固定数量。分镜总帧数必须等于 durationFrames。'
            '分段边界不能穿过字幕。anchors 是段内秒数 at 或帧 frame，只有字幕边界或提供的可靠时间可用；'
            '句内词的时间未知则留空，由校验报告待补。缺媒体用 null，不能编造路径。'
            '不要生成 contentEvidence、修剪或改速设置。短显示文案使用口播语言。brand 没有提供则空字符串。')
        response = client.complete([{'role':'system','content':role},
            {'role':'user','content':json.dumps(payload,ensure_ascii=False)}], response_format={'type':'json_object'})
        if response.get('finish_reason') == 'length':
            raise ValueError('模板分镜响应被截断，请缩短本次口播或提高模型输出限制。')
        return json.loads(response['message']['content'])
    return generate
