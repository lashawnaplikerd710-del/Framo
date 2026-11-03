"""Build a clean friend bundle from an explicit allowlist, never the project root."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

ROOT=Path(__file__).resolve().parent
SKIP={'__pycache__','.pytest_cache','tests','test','.git','data','outputs','private','.wrangler','node_modules'}
TEXT={'.py','.pyw','.json','.md','.txt','.ps1','.cmd','.js','.mjs','.html','.css','.pth','.cfg','.ini','.cs'}

def sha(path):
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()

def include_file(relative):
    if any(p in SKIP for p in relative.parts) or relative.suffix in {'.pyc','.pyo','.log'}:return False
    if relative.name.startswith(('.env','.dev.vars')) or relative.name in {'direct_url.json','pyvenv.cfg'}:return False
    return True

def copy_tree(source,dest,extensions=None):
    for path in sorted(source.rglob('*')):
        relative=path.relative_to(source)
        if not path.is_file() or not include_file(relative):continue
        if path.is_symlink():raise ValueError('Bundle inputs must be ordinary files')
        if extensions is not None and path.suffix not in extensions:continue
        output=dest/relative;output.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,output)


def copy_motion_skill(destination):
    """Keep the public template renderer and its local runtimes together."""
    source = ROOT/'skills/adu-motion-video'
    if not (source/'SKILL.md').is_file():
        raise FileNotFoundError(source/'SKILL.md')
    target = destination/'skills/adu-motion-video'
    for path in sorted(source.rglob('*')):
        relative = path.relative_to(source)
        if not path.is_file() or path.suffix in {'.pyc', '.pyo', '.log'} or \
                path.name.startswith(('.env', '.dev.vars')) or path.name in {'direct_url.json', 'pyvenv.cfg'} or \
                any(part in {'.git', '__pycache__', '.pytest_cache', 'tests'} for part in relative.parts):
            continue
        if path.is_symlink():
            raise ValueError('Template skill inputs must be ordinary files')
        output = target/relative
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, output)


def copy_studio_fonts(destination):
    """Ship the unmodified rounded faces together with their redistribution license."""
    fonts = ROOT/'desktop-assets/fonts'
    names = ('LXGW975YuanSC-400W.ttf', 'LXGW975YuanSC-700W.ttf', 'OFL-975Yuan.txt', 'ORIGIN.md')
    for name in names:
        if not (fonts/name).is_file():
            raise FileNotFoundError(fonts/name)
    target = Path(destination)/'desktop-assets/fonts'
    target.mkdir(parents=True, exist_ok=True)
    for name in names:
        shutil.copy2(fonts/name, target/name)
    media = ROOT/'desktop-assets/media-library'
    if media.is_dir():
        copy_tree(media, Path(destination)/'desktop-assets/media-library')

def build_desktop_launcher(destination):
    """Compile a small Windows GUI entry point; Python and Tk ship beside it."""
    import os
    from PIL import Image, ImageDraw
    assets=destination/'desktop-assets';assets.mkdir()
    icon=Image.new('RGBA',(256,256),(0,0,0,0))
    draw=ImageDraw.Draw(icon)
    draw.rounded_rectangle((4,4,252,252),radius=54,fill='#6754dc')
    draw.polygon([(98,67),(98,190),(197,128)],fill='white')
    draw.rounded_rectangle((57,67,73,189),radius=7,fill='#c4baff')
    icon.save(assets/'workbench.ico',format='ICO',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
    compiler=Path(os.environ.get('WINDIR','C:/Windows'))/'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    if not compiler.exists():
        compiler=Path(os.environ.get('WINDIR','C:/Windows'))/'Microsoft.NET/Framework/v4.0.30319/csc.exe'
    if not compiler.exists():raise FileNotFoundError('Windows .NET Framework compiler is required to build the desktop launcher')
    args=[str(compiler),'/nologo','/target:winexe','/platform:anycpu','/optimize+', '/codepage:65001',
          '/reference:System.Windows.Forms.dll','/win32icon:'+str(assets/'workbench.ico'),
          '/out:'+str(destination/'Jianying-Workbench.exe'),str(ROOT/'desktop-launcher.cs')]
    result=subprocess.run(args,capture_output=True,text=True,creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise RuntimeError('Desktop launcher compilation failed: '+result.stdout+result.stderr)


def build(destination, model_dir, endpoint='', archive=True):
    destination=Path(destination).resolve()
    if destination.exists():raise FileExistsError('Use a new output directory; existing bundles are preserved')
    if destination.is_relative_to(ROOT):raise ValueError('Build outside the runtime source tree')
    if endpoint:
        from agent_core.gateway_credentials import validate_profile
        validate_profile({'transport':'gateway','api_key_env':'JIANYING_GATEWAY_TOKEN','base_url':endpoint})
    model_dir=Path(model_dir).resolve(strict=True)
    required=('model.int8.onnx','tokens.txt','LICENSE','MODEL_LICENSE')
    for name in required:
        if not (model_dir/name).is_file():raise FileNotFoundError(model_dir/name)
    destination.mkdir(parents=True)
    copy_tree(ROOT/'agent_core',destination/'agent_core',{'.py'})
    copy_tree(ROOT/'toolchain/scripts',destination/'toolchain/scripts',{'.py','.mjs','.ps1'})
    # The direct backend is self-contained.  Keep the renderer beside the
    # scripts so a friend bundle never falls back to an editor's FFmpeg DLL.
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        renderer = ROOT/'toolchain'/name
        if not renderer.is_file():
            raise FileNotFoundError(renderer)
        shutil.copy2(renderer, destination/'toolchain'/name)
    copy_tree(ROOT/'toolchain/third_party',destination/'toolchain/third_party')
    copy_tree(ROOT/'toolchain/node',destination/'toolchain/node')
    copy_tree(ROOT/'python',destination/'python')
    copy_motion_skill(destination)
    for name in ('Launch-Desktop.pyw','Start-Workbench.cmd','WORKBENCH-QUICKSTART.md','agent-prompt.md','desktop-launcher.cs'):
        shutil.copy2(ROOT/name,destination/name)
    build_desktop_launcher(destination)
    copy_studio_fonts(destination)
    direct_resources = ROOT/'resources/DirectBuiltin'
    if direct_resources.is_dir():
        copy_tree(direct_resources, destination/'Resources/DirectBuiltin')
    target=destination/'models/sensevoice';target.mkdir(parents=True)
    for name in required:shutil.copy2(model_dir/name,target/name)
    (target/'ORIGIN.md').write_text('# SenseVoiceSmall\n\nAuthors: Alibaba / FunAudioLLM. ONNX conversion: csukuangfj / sherpa-onnx.\n\nModel: sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\n\nhttps://huggingface.co/csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17\n\nLicense reference and complete referenced model license are preserved beside the weights.\n',encoding='utf-8')
    config=json.loads((ROOT/'config.native.json').read_text(encoding='utf-8-sig'))
    config.update(install_dir='',draft_root='',user_data_dir='',toolchain_root='toolchain',
                  asr_model_dir='models/sensevoice', direct_ffmpeg_dir='toolchain',
                  direct_resources_root='Resources/DirectBuiltin')
    (destination/'config.native.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    (destination/'gateway-endpoint.json').write_text(json.dumps({'base_url':endpoint},indent=2),encoding='utf-8')
    entries=[]
    for path in sorted(destination.rglob('*')):
        if not path.is_file():continue
        if path.suffix.lower() in TEXT and path.stat().st_size<10*1024*1024:
            text=path.read_text(encoding='utf-8',errors='replace')
            if re.search(r'(?<![\w-])sk-[A-Za-z0-9_-]{24,}',text) or re.search(r'jyg_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])',text):
                raise ValueError('Credential-like literal found; bundle rejected: '+str(path.relative_to(destination)))
            # Owner data/configs must not be included indirectly by an allowlisted file.
            if re.search(r'C:[/\\]+Users[/\\]+dicoc',text,re.I):
                raise ValueError('Owner absolute path found; bundle rejected: '+str(path.relative_to(destination)))
        entries.append({'path':path.relative_to(destination).as_posix(),'bytes':path.stat().st_size,'sha256':sha(path)})
    manifest={'schema':'jianying-friends-workbench-v1','version':'0.2.0','built_at':datetime.now(timezone.utc).isoformat(),
              'ui':'native_tk','browser_required':False,'http_server':False,'entry_point':'Jianying-Workbench.exe',
              'codex_required':False,'provider_keys_included':False,'user_media_included':False,
              'jianying_binaries_included':False,'gateway_configured':bool(endpoint),'tested_jianying_version':'11.5.0.14471',
              'second_machine_verified':False,'files':entries}
    (destination/'package-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    result={'directory':str(destination),'file_count':len(entries),'bytes':sum(e['bytes'] for e in entries),
            'gateway_configured':bool(endpoint),'credential_scan':'passed'}
    if archive:
        archive_path=destination.with_name(destination.name+'.zip')
        with zipfile.ZipFile(archive_path,'x',zipfile.ZIP_DEFLATED,compresslevel=3) as bundle:
            for path in sorted(destination.rglob('*')):
                if path.is_file():bundle.write(path,Path(destination.name)/path.relative_to(destination))
        result.update(archive=str(archive_path),archive_bytes=archive_path.stat().st_size,archive_sha256=sha(archive_path))
    return result

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--destination',required=True);p.add_argument('--model-dir',required=True)
    p.add_argument('--endpoint',default='');p.add_argument('--no-zip',action='store_true');a=p.parse_args()
    print(json.dumps(build(a.destination,a.model_dir,a.endpoint,not a.no_zip),ensure_ascii=False,indent=2))
