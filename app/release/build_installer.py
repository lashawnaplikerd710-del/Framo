"""Build a clean Framo installer from explicit inputs, never a user's work state."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
from package_workbench import sha
from builtin_audio import build as build_audio
from agent_core.version import VERSION

EXCLUDE = {'__pycache__', '.git', '.pytest_cache', 'tests', 'test'}
DEMO_NAMES = {'01-A', '01-B', '01-C', '01-D', '01-E', '02-A', '02-B',
              '03-A', '03-A-portrait', '04-A', '05-A', '06-A'}
PUBLIC_DOCS = {'visual-effects.md','subtitle-removal.md','subtitle-position.md',
               'automatic-script-recovery.md',
               'narration-repair-policy.md','long-video-no-voice.md',
               'caption-motion-and-japanese-voices.md'}


def copy_public_docs(source, target):
    """Ship usage rules, never internal QA jobs or the owner's artifact paths."""
    target.mkdir(parents=True, exist_ok=True)
    for name in sorted(PUBLIC_DOCS):
        content=(source/name).read_text(encoding='utf-8-sig')
        content=re.sub(r'## 已确认的参考逻辑\n.*?(?=## 分析和规划)', '', content, flags=re.S)
        content='\n'.join(line for line in content.splitlines()
                          if not re.search(r'[A-Z]:[/\\]|job_[0-9a-f]{16,64}',line))+'\n'
        (target/name).write_text(content,encoding='utf-8')


def bundled_demo(relative):
    path = Path(relative)
    return (path.parent.as_posix() == 'skills/adu-motion-video/assets/demos'
            and path.suffix == '.mp4' and path.stem in DEMO_NAMES)


def copy_clean(source, target, suffixes=None, excluded_relatives=()):
    if not source.is_dir():
        raise FileNotFoundError(source)
    for path in sorted(source.rglob('*')):
        rel = path.relative_to(source)
        if rel.as_posix() in excluded_relatives:
            continue
        if any(part in EXCLUDE for part in rel.parts) or not path.is_file():
            continue
        if path.is_symlink() or path.is_junction():
            raise ValueError('Linked input rejected: ' + str(rel))
        if path.suffix in {'.pyc', '.pyo', '.log'} or path.name in {'direct_url.json', 'pyvenv.cfg'}:
            continue
        if path.name.startswith(('.env', '.dev.vars')):
            raise ValueError('Private file in input: ' + str(rel))
        if suffixes and path.suffix not in suffixes:
            continue
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)


def clean_config(runtime):
    raw = json.loads((runtime / 'config.native.json').read_text(encoding='utf-8-sig'))
    endpoint = json.loads((runtime / 'gateway-endpoint.json').read_text(encoding='utf-8'))['base_url']
    from agent_core.gateway_credentials import apply_gateway
    # Explicit schema, not a generic copy of an owner's configuration.
    config = {k: raw[k] for k in ('schema', 'provider', 'limits', 'narration')}
    config['provider'] = {k: raw['provider'][k] for k in
        ('base_url','model','api_key_env','timeout_seconds','max_output_tokens','thinking','capabilities')
        if k in raw['provider']}
    # Match the tested repair pipeline budget without copying personal settings.
    config['limits'] = {**config['limits'], 'max_model_calls':48,
                        'token_stop_threshold':640000}
    config.update(render_backend_default='direct_ffmpeg', install_dir='', draft_root='', user_data_dir='',
                  toolchain_root='toolchain', asr_model_dir='models/sensevoice', direct_output_dir='',
                  direct_ffmpeg_dir='toolchain', direct_resources_root='Resources/DirectBuiltin',
                  direct_media={'local_voice_library':'Resources/voice-library'})
    return apply_gateway(config, endpoint), endpoint


def scan_and_manifest(payload):
    files = []
    for path in sorted(payload.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(payload).as_posix()
        if path.suffix.lower() in {'.sqlite3', '.db', '.log', '.pyc', '.pyo'} or (
                path.suffix.lower() == '.mp4' and not bundled_demo(rel)):
            raise ValueError('Unapproved state/media file: '+rel)
        if path.name in {'settings.json','runtime-config.json','grants.json'}:
            raise ValueError('Private state file: '+rel)
        if path.suffix.lower() in {'.json','.py','.pyw','.md','.txt','.pth','.ini','.cfg','.ps1','.mjs','.js','.html','.css','.yaml','.yml','.toml'} and path.stat().st_size < 10*1024*1024:
            content = path.read_text(encoding='utf-8', errors='replace')
            if re.search(r'(?<![\w-])sk-[A-Za-z0-9_-]{24,}|jyg_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])', content):
                raise ValueError('Credential-like value rejected: '+rel)
            if re.search(r'[CD]:[/\\]+Users[/\\]+(?:sir|dicoc)(?:[/\\]|\b)', content, re.I):
                raise ValueError('Owner home path rejected: '+rel)
        files.append({'path':rel,'bytes':path.stat().st_size,'sha256':sha(path)})
    manifest = {'schema':'framo-installer-v1','version':VERSION,'product_name':'Framo','entry_point':'Framo.exe',
                'built_at':datetime.now(timezone.utc).isoformat(),'files':files,
                'provider_keys_included':False,'user_media_included':False,'task_history_included':False,
                'bundled_template_demos':[row['path'] for row in files if bundled_demo(row['path'])],
                'second_machine_verified':False,'access_code_included':False}
    (payload/'package-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2),encoding='utf-8')
    return manifest


def build(args):
    destination = Path(args.destination).resolve()
    if destination.exists():
        raise FileExistsError('Choose a new staging directory')
    runtime, voice = Path(args.runtime).resolve(strict=True), Path(args.voices).resolve(strict=True)
    destination.mkdir(parents=True)
    payload = destination/'payload'; payload.mkdir()
    for src, target, suffixes in [
        (runtime/'agent_core',payload/'agent_core',{'.py'}),
        (runtime/'python',payload/'python',None),
        (runtime/'toolchain',payload/'toolchain',None),
        (runtime/'models',payload/'models',None),
        (runtime/'desktop-assets',payload/'desktop-assets',None),
        (runtime/'skills/adu-motion-video',payload/'skills/adu-motion-video',None)]:
        copy_clean(src,target,suffixes)
    # The working desktop contains newer runtime-only composition and supplement
    # features. Preserve it as the release baseline; add only installation glue.
    shutil.copy2(SOURCE/'agent_core/installed_runtime.py',payload/'agent_core/installed_runtime.py')
    (payload/'agent_core/version.py').write_text(
        '"""Public release version shared by the desktop and installer builder."""\n'
        f'VERSION = {VERSION!r}\n',encoding='utf-8')
    copy_public_docs(SOURCE/'docs',payload/'docs')
    for name in ('Launch-Desktop.pyw','agent-prompt.md'):
        shutil.copy2(SOURCE/name,payload/name)
    # Recompile the existing branded launcher with the same release version.
    launcher = (SOURCE/'desktop-launcher.cs').read_text(encoding='utf-8-sig')
    quad_version='.'.join((VERSION.split('.')+['0']*4)[:4])
    launcher = re.sub(r'(Assembly(?:File)?Version\(")[^"]+',r'\g<1>'+quad_version,launcher)
    launcher_source = destination/'desktop-launcher.cs'
    launcher_source.write_text(launcher,encoding='utf-8-sig')
    compiler = Path(os.environ['SystemRoot'])/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    subprocess.run([str(compiler),'/nologo','/target:winexe','/optimize+',
        '/out:'+str(payload/'Framo.exe'),'/win32icon:'+str(payload/'desktop-assets/framo.ico'),
        '/reference:System.Windows.Forms.dll',str(launcher_source)],check=True,capture_output=True,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    shutil.copy2(SOURCE/'release/installer_guard.py',payload/'installer_guard.py')
    target = payload/'Resources/voice-library'
    for name in ('python','models','licenses'):
        # The extracted LibriTTS model and dictionaries are verified below.
        # Its original download archive is never opened by the speech worker.
        excluded={'libritts-high.tar.bz2'} if name=='models' else ()
        copy_clean(voice/name,target/name,excluded_relatives=excluded)
    # Phonemizer resolves relative espeak data paths to Unicode absolute paths.
    # The native Windows espeak build cannot open those. Preserve a validated
    # relative path in our isolated worker; upstream license/source stay intact.
    wrapper = target/'python/phonemizer/backend/espeak/wrapper.py'
    if sha(wrapper) != '90da9104d413a3551da04894675c8bc6546a69e4b53274fa6745bec43fbbaa14':
        raise ValueError('Review the phonemizer compatibility patch for this dependency version')
    text = wrapper.read_text(encoding='utf-8')
    if text.count('self._data_path = data_path.resolve()') != 2:
        raise ValueError('Unexpected phonemizer source')
    wrapper.write_text('# Framo 2026-10-14: retain validated relative data paths for Unicode Windows installations.\n'+
        text.replace('self._data_path = data_path.resolve()', 'self._data_path = data_path'),encoding='utf-8')
    for name in ('catalog.json','dependency-lock.json','NOTICE.md'):
        shutil.copy2(voice/name,target/name)
    catalog = json.loads((target/'catalog.json').read_text(encoding='utf-8'))
    notice=target/'NOTICE.md'
    content=notice.read_text(encoding='utf-8-sig')
    content=re.sub(r'共 59 种独立说话人音色：[^\n]+',
        '共 69 种独立说话人音色：美式英语 50 种、日语 9 种、中文 10 种（女声 5 种、男声 5 种）。'
        '可离线朗读新文案；同一成品使用一个音色，避免句间换人。',content)
    content+='\n## 中文配音\n\n中文男女各五种独立预设音色采用 Kokoro v1.1 中文模型，Apache-2.0；'
    content+='来源、固定版本及许可随 models/kokoro-zh-v1.1 保留。\n'
    notice.write_text(content,encoding='utf-8')
    (target/'samples').mkdir()
    for row in catalog['voices']:
        name = row['id']+'.wav'
        if '/' in name or '\\' in name:
            raise ValueError('Invalid sample name')
        shutil.copy2(voice/'samples'/name,target/'samples'/name)
    for model in catalog['models'].values():
        for relative, expected in model['files'].items():
            file = (target/relative).resolve(strict=True)
            if not file.is_relative_to(target.resolve()) or sha(file) != expected:
                raise ValueError('Voice model fingerprint mismatch: '+relative)
    resources = payload/'Resources/DirectBuiltin'
    build_audio(resources)
    shutil.copy2(SOURCE/'release/builtin_audio.py',resources/'builtin_audio.py')
    config, endpoint = clean_config(runtime)
    for name, content in [('config.native.json',config), ('gateway-endpoint.json',{'base_url':endpoint}),
                          ('installed-profile.json',{'schema':'framo-installed-v1','version':VERSION})]:
        (payload/name).write_text(json.dumps(content,ensure_ascii=False,indent=2),encoding='utf-8')
    for name in ('使用说明.txt','THIRD-PARTY-NOTICES.md'):
        shutil.copy2(SOURCE/'release'/name,payload/name)
    (payload/'licenses/NSIS').mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(args.nsis)/'COPYING', payload/'licenses/NSIS/COPYING.txt')
    copy_clean(Path(args.licenses), payload/'licenses/FFmpeg')
    version = subprocess.run([str(payload/'toolchain/ffmpeg.exe'),'-version'],capture_output=True,
        text=True,encoding='utf-8',check=True,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (payload/'toolchain/FFmpeg-BUILD.txt').write_text(version.stdout,encoding='utf-8')
    manifest = scan_and_manifest(payload)
    delete_list = destination/'uninstall-files.nsh'
    entries = [p for p in payload.rglob('*') if p.is_file()]
    dirs = sorted({p.parent for p in entries if p.parent != payload},key=lambda p:len(p.parts), reverse=True)
    def quote(path):
        return str(path.relative_to(payload)).replace('$','$$').replace('"','$\\"')
    # Keep the small installation marker so reinstalling beside retained
    # UserData is recognized as this app's directory, not an unrelated folder.
    delete_list.write_text('\n'.join(['Delete "$INSTDIR\\'+quote(p)+'"' for p in entries
        if p.name != 'installed-profile.json']+
        ['RMDir "$INSTDIR\\'+quote(p)+'"' for p in dirs]),encoding='utf-8-sig')
    output = destination/f'Framo-Setup-{VERSION}.exe'
    cmd = [str(Path(args.nsis)/'makensis.exe'),'/V3','/INPUTCHARSET','UTF8',
           '/DAPP_VERSION='+VERSION,'/DAPP_VERSION_QUAD='+quad_version,
           '/DPAYLOAD='+str(payload),'/DOUTPUT='+str(output),'/DDELETE_LIST='+str(delete_list),
           str(SOURCE/'release/Framo.nsi')]
    with (destination/'compiler.log').open('w',encoding='utf-8') as log:
        subprocess.run(cmd,stdout=log,stderr=subprocess.STDOUT,check=True,
                       creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    result = {'version':VERSION,'installer':str(output),'sha256':sha(output),'bytes':output.stat().st_size,
              'compression':'lzma-per-file-64mb',
              'files':len(manifest['files']),'installed_bytes':sum(r['bytes'] for r in manifest['files']),
              'voices':len(catalog['voices']),'credential_scan':'passed','second_machine_verified':False}
    (destination/'build-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('--destination',required=True)
    p.add_argument('--runtime',required=True); p.add_argument('--voices',required=True);p.add_argument('--nsis',required=True)
    p.add_argument('--licenses',required=True)
    build(p.parse_args())
