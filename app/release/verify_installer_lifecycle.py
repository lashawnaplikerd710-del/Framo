"""Exercise a built installer only inside an explicitly isolated release folder."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import winreg

release = Path(sys.argv[1]).resolve(strict=True)
qa = Path(sys.argv[2]).resolve()
install = Path(sys.argv[3]).resolve()
build_result=json.loads((release/'build-result.json').read_text(encoding='utf-8'))
version=build_result['version']
assert install.is_relative_to(Path('D:/FramoRelease').resolve())
assert install.name == '安装验收-'+version
assert not install.exists(), 'Use a fresh installation destination'
qa.mkdir(parents=True, exist_ok=True)
installer=release/f'Framo-Setup-{version}.exe'
assert hashlib.file_digest(installer.open('rb'),'sha256').hexdigest()==build_result['sha256']
flags=subprocess.CREATE_NO_WINDOW
records=[]

def save(step, **values):
    records.append({'step':step,'time':time.time(),**values})
    (qa/'lifecycle.json').write_text(json.dumps(records,ensure_ascii=False,indent=2),encoding='utf-8')
    print(step,values,flush=True)

def install_once():
    # NSIS requires /D as the unquoted last argument; CreateProcess, no shell.
    command='"'+str(installer)+'" /S /ISOLATED /D='+str(install)
    result=subprocess.run(command,creationflags=flags,timeout=240)
    assert result.returncode==0,result.returncode
    assert (install/'Framo.exe').is_file()

install_once();save('actual_installer_extracted',directory=str(install))
env=dict(os.environ)
for key in list(env):
    if key.endswith(('_API_KEY','_TOKEN')) or key in (
            'PYTHONPATH','PYTHONHOME','JIANYING_AGENT_STATE_DIR',
            'CHROME','PLAYWRIGHT_BROWSERS_PATH','FRAMO_VOICE_CACHE'):
        env.pop(key,None)
env['PATH']=str(Path(os.environ['SystemRoot'])/'System32')+';'+os.environ['SystemRoot']
with (qa/'installed-verification.log').open('w',encoding='utf-8') as log:
    completed=subprocess.run([str(install/'python/python.exe'),'-B','-X','utf8',
        str(Path(__file__).with_name('verify_installation.py')),str(qa/'installed')],
        env=env,cwd=install,stdout=log,stderr=subprocess.STDOUT,timeout=240,creationflags=flags)
assert completed.returncode==0,'See installed-verification.log'
save('installed_python_ui_speech_render_checks_passed')
receipt=install/'UserData/desktop-status.json'
subprocess.run([str(install/'Framo.exe')],cwd=install,env=env,check=True,creationflags=flags)
deadline=time.monotonic()+20
while time.monotonic()<deadline:
    if receipt.exists():
        status=json.loads(receipt.read_text(encoding='utf-8'))
        if status.get('window_mapped'):
            break
    time.sleep(.2)
else:
    raise AssertionError('Actual launcher did not map its Framo window')
assert Path(status['state_dir'])==install/'UserData'
assert Path(status['package_root'])==install
assert status['version']==version
save('real_exe_bootstrap',title=status['title'],state_dir=status['state_dir'])
guard=subprocess.run([str(install/'python/python.exe'),'-B',str(install/'installer_guard.py')],
    creationflags=flags,capture_output=True)
assert guard.returncode==2
save('running_app_update_guard')
import psutil
process=psutil.Process(status['pid'])
assert Path(process.exe()).resolve()==install/'python/pythonw.exe'
# Leave the real window available for visual verification. The Windows
# Computer Use driver captures and closes this exact isolated app window.
save('window_ready_for_ui_check',pid=status['pid'],executable=str(process.exe()))
process.wait(timeout=240)
save('real_exe_closed_normally')
sentinel=install/'UserData/preserve-test.txt';sentinel.write_text('preserve',encoding='utf-8')
install_once()
assert sentinel.read_text(encoding='utf-8')=='preserve'
save('reinstall_preserves_user_data')
uninstaller=install/'Uninstall.exe'
result=subprocess.run('"'+str(uninstaller)+'" /S _?='+str(install),
                      creationflags=flags,timeout=120)
assert result.returncode==0,result.returncode
assert not (install/'Framo.exe').exists()
assert not (install/'python/python.exe').exists()
assert sentinel.read_text(encoding='utf-8')=='preserve'
assert (install/'UserData/SmokeRender/installation-smoke.mp4').is_file()
assert (install/'installed-profile.json').is_file()
save('uninstall_removes_program_preserves_data_and_mp4')
install_once()
assert sentinel.read_text(encoding='utf-8')=='preserve'
save('install_after_uninstall_reuses_retained_data')

# Exercise normal Windows integration only when its namespace is unused;
# never replace the owner's existing desktop entry or another installation.
buffer=ctypes.create_unicode_buffer(32768)
ctypes.windll.shell32.SHGetFolderPathW(None,2,None,0,buffer)
menu=Path(buffer.value)/'Framo'
ctypes.windll.shell32.SHGetFolderPathW(None,0x10,None,0,buffer)
desktop=Path(buffer.value)/'Framo.lnk'
desktop_before=desktop.read_bytes() if desktop.exists() else None
keyname=r'Software\Microsoft\Windows\CurrentVersion\Uninstall\Framo'
try:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,keyname,0,winreg.KEY_READ|winreg.KEY_WOW64_32KEY):
        registered=True
except FileNotFoundError:
    registered=False
integration='ISOLATED only: prior integration exists'
if not menu.exists() and not registered:
    result=subprocess.run('"'+str(installer)+'" /S /D='+str(install),creationflags=flags,timeout=240)
    assert result.returncode==0
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER,keyname,0,winreg.KEY_READ|winreg.KEY_WOW64_32KEY) as key:
        assert Path(winreg.QueryValueEx(key,'InstallLocation')[0])==install
        assert winreg.QueryValueEx(key,'DisplayVersion')[0]==version
    assert (menu/'Framo.lnk').is_file()
    if desktop_before is not None:
        assert desktop.read_bytes()==desktop_before
    else:
        assert desktop.is_file()
    save('normal_install_start_menu_and_uninstall_registration',existing_desktop_preserved=desktop_before is not None)
    result=subprocess.run('"'+str(uninstaller)+'" /S _?='+str(install),creationflags=flags,timeout=120)
    assert result.returncode==0
    assert not menu.exists()
    assert sentinel.read_text(encoding='utf-8')=='preserve'
    if desktop_before is not None:
        assert desktop.read_bytes()==desktop_before
    else:
        assert not desktop.exists()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,keyname,0,winreg.KEY_READ|winreg.KEY_WOW64_32KEY):
            raise AssertionError('Uninstall registration remains')
    except FileNotFoundError:
        pass
    integration='normal install and uninstall passed; previous desktop shortcut unchanged'
    save('normal_uninstall_registration_cleanup')
save('complete',installer_sha256=hashlib.file_digest(installer.open('rb'),'sha256').hexdigest(),
     integration=integration,
     second_machine_verified=False,real_gateway_edit_verified=False)
