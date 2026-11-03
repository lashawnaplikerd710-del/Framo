"""Create a movable local Windows Python and Jianying toolchain bundle.

Build inputs are explicit local paths; this script never modifies them.
The copied Python base carries its own standard library/Tk libraries; the
copied dependency site-packages carries the already installed editing tools.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil

HERE=Path(__file__).resolve().parent
SKIP={'__pycache__','.pytest_cache','tests'}

def copy_tree(source,dest):
    if dest.exists():raise FileExistsError(dest)
    shutil.copytree(source,dest,ignore=lambda parent,names: [n for n in names if n in SKIP or n.endswith('.pyc')])

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):h.update(block)
    return h.hexdigest()

def bundle(base,env,toolchain):
    base=Path(base).resolve(strict=True)
    env=Path(env).resolve(strict=True)
    toolchain=Path(toolchain).resolve(strict=True)
    target=HERE/'python'
    desttool=HERE/'toolchain'
    if target.exists() or desttool.exists():
        raise FileExistsError('Existing bundle detected; preserve it and use a new staging directory to update')
    required=['python.exe','pythonw.exe','python3.dll','python312.dll','vcruntime140.dll','vcruntime140_1.dll','LICENSE.txt']
    for name in required:
        if not (base/name).is_file():raise FileNotFoundError(base/name)
    if not (toolchain/'scripts/autocut_task.py').is_file():raise ValueError('Invalid native toolchain')
    target.mkdir()
    for name in required:shutil.copy2(base/name,target/name)
    for name in ('DLLs','tcl'):
        copy_tree(base/name,target/name)
    shutil.copytree(base/'Lib',target/'Lib',ignore=lambda parent,names:
        [n for n in names if n in SKIP or n.endswith('.pyc') or (Path(parent)==base/'Lib' and n=='site-packages')])
    copy_tree(env/'Lib/site-packages',target/'Lib/site-packages')
    shutil.copytree(toolchain,desttool,ignore=lambda parent,names:
        [n for n in names if n in SKIP or n.endswith('.pyc') or n=='.venv'])
    report={'schema':'jianying-portable-bundle-v1','source_python':str(base),'python_version':'3.12',
            'python_executable':'python/python.exe','native_toolchain':'toolchain',
            'runtime_source':'local copied Python distribution and project dependencies',
            'from_codex_paths_required_at_run_time':False,
            'license_python':'python/LICENSE.txt','selected_checks':{
             'python_exe':digest(target/'python.exe'),
             'toolchain_entry':digest(desttool/'scripts/autocut_task.py'),
             'requirements':digest(desttool/'requirements.txt')}}
    (HERE/'bundle-manifest.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--python-base',required=True);p.add_argument('--existing-venv',required=True)
    p.add_argument('--toolchain',required=True);a=p.parse_args()
    bundle(a.python_base,a.existing_venv,a.toolchain)
