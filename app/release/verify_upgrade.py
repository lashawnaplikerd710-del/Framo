"""Upgrade old to new only in a fresh, isolated release-test directory."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

old=Path(sys.argv[1]).resolve(strict=True)
release=Path(sys.argv[2]).resolve(strict=True)
install=Path(sys.argv[3]).resolve()
qa=Path(sys.argv[4]).resolve();qa.mkdir(parents=True,exist_ok=True)
assert install.is_relative_to(Path('D:/FramoRelease').resolve())
assert install.name.startswith('升级验收-') and not install.exists()
records=[]
def save(step,**values):
    records.append({'step':step,'time':time.time(),**values})
    (qa/'upgrade.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    print(step,values,flush=True)
def run_installer(path):
    completed=subprocess.run('"'+str(path)+'" /S /ISOLATED /D='+str(install),
        creationflags=subprocess.CREATE_NO_WINDOW,timeout=300)
    assert completed.returncode==0
run_installer(old)
before=json.loads((install/'installed-profile.json').read_text(encoding='utf-8'))['version']
save('old_version_installed',version=before)
sentinels={'UserData/preserve-upgrade.txt':'retained local task marker',
           'UserData/Exports/preserve-upgrade.txt':'retained export marker'}
for name,value in sentinels.items():
    path=install/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value,encoding='utf-8')
deadline=time.monotonic()+900
while not (release/'build-result.json').is_file():
    if time.monotonic()>deadline:raise TimeoutError('New installer build not completed')
    time.sleep(5)
result=json.loads((release/'build-result.json').read_text(encoding='utf-8'))
new=Path(result['installer']);assert hashlib.file_digest(new.open('rb'),'sha256').hexdigest()==result['sha256']
run_installer(new)
assert json.loads((install/'installed-profile.json').read_text(encoding='utf-8'))['version']==result['version']
for name,value in sentinels.items():assert (install/name).read_text(encoding='utf-8')==value
manifest=json.loads((install/'package-manifest.json').read_text(encoding='utf-8'))
for row in manifest['files']:
    assert hashlib.file_digest((install/row['path']).open('rb'),'sha256').hexdigest()==row['sha256'],row['path']
assert not (install/'UserData/tasks/agent.sqlite3').exists()
save('complete',from_version=before,to_version=result['version'],payload_files_verified=len(manifest['files']),
    sentinels_preserved=True,isolated_install=True,owner_integration_modified=False)
