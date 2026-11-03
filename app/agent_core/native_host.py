"""Explicit opt-in isolated Jianying host lifetime; no GUI interaction.

Health is not resource entitlement. The normal Agent resource gate is unchanged.
The wrapper was tested with Jianying on 2026-09-28. Resource and export calls
still require independent authorization and delivery checks.
"""
from __future__ import annotations
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

from . import native_connection as bridge
from .isolated_desktop import DesktopFailure, DesktopJob, child_environment
from .task_store import atomic_json


class HostFailure(ValueError):
    """Only fixed, non-secret reasons enter this exception."""


def editor_pids():
    command=Path(os.environ['SystemRoot'])/'System32/tasklist.exe'
    result=subprocess.run([str(command),'/FI','IMAGENAME eq JianyingPro.exe','/FO','CSV','/NH'],
                          capture_output=True,timeout=10,env=child_environment(),
                          creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:raise HostFailure('editor_process_inspection_failed')
    return sorted(int(row[1]) for row in csv.reader(result.stdout.decode('ascii',errors='replace').splitlines())
                  if len(row)>1 and row[0].lower()=='jianyingpro.exe')


def signature_valid(exe):
    env=child_environment();env['JY_HOST_SIGNATURE_FILE']=str(exe)
    command=Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe'
    script="[Console]::Write((Get-AuthenticodeSignature -LiteralPath $env:JY_HOST_SIGNATURE_FILE).Status.ToString())"
    result=subprocess.run([str(command),'-NoProfile','-NonInteractive','-Command',script],
                          capture_output=True,timeout=15,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
    return result.returncode==0 and result.stdout.decode('utf-8-sig').strip()=='Valid'


def index_state(path):
    if not path.is_file():return {'exists':False}
    raw=path.read_bytes()
    result={'exists':True,'sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw)}
    try:
        data=json.loads(raw)
        if isinstance(data,dict) and isinstance(data.get('all_draft_store'),list):
            result['draft_count']=len(data['all_draft_store'])
    except (ValueError,UnicodeError):result['parseable']=False
    return result


class IsolatedNativeHost:
    def __init__(self,install,output,*,allow_isolated_host=False,user_data=None,
                 startup_timeout=60,lifetime_timeout=180):
        if allow_isolated_host is not True:raise HostFailure('explicit_isolated_host_opt_in_required')
        if (type(startup_timeout) not in (int,float) or not 1<=startup_timeout<=90 or
                type(lifetime_timeout) not in (int,float) or not startup_timeout<lifetime_timeout<=600):
            raise HostFailure('invalid_host_time_budget')
        self.install=Path(install).resolve(strict=True);self.output=Path(output).resolve()
        data=Path(user_data) if user_data else Path(os.environ['LOCALAPPDATA'])/'JianyingPro/User Data'
        self.index=data.resolve()/'Projects/com.lveditor.draft/root_meta_info.json'
        if (self.output.exists() or self.output.is_relative_to(self.install) or
                self.output.is_relative_to(data.resolve())):
            raise HostFailure('host_report_needs_new_directory_outside_install_and_user_data')
        self.startup_timeout=startup_timeout;self.lifetime_timeout=lifetime_timeout
        self.job=None;self.thread=None;self.stop=threading.Event();self.lock=threading.RLock()
        self.failure=None;self.started=None;self.closed=False
        self.report={'schema':'jianying-managed-isolated-host-v1','status':'preflight',
                     'isolated_app_launch_requested':True,'gui_operated':False,'desktop_switched':False,
                     'resource_authorization_verified':False,'native_export_verified':False,
                     'credentials_disclosed':False,'health_observations':[],
                     'window_poll_interval_seconds':.2,'max_input_windows_observed':0,
                     'index_automatically_restored':False,'automatic_restart':False}

    def save(self):
        with self.lock:
            if self.output.is_dir():atomic_json(self.output/'host-lifecycle.json',self.report)

    def _check_other_editors(self):
        # Query the editor snapshot first: a child can join the job during tasklist.
        editors=set(editor_pids())
        with self.lock:owned=self.job.members()
        for pid in editors-owned:
            try:bridge.process_image(pid)
            except bridge.ConnectionFailure as exc:
                if str(exc)=='discovery_stale':continue
            raise HostFailure('unowned_editor_appeared')

    def _owned_health(self):
        try:
            context=bridge.read_discovery(Path(os.environ['TEMP'])/bridge.DISCOVERY_NAME)
            with self.lock:owned=self.job.members()
            if context['pid'] not in owned:
                return {'status':'blocked','reason':'discovery_not_owned_by_managed_host','health_requested':False}
            result=bridge.check(self.install,expected_pid=context['pid'])
            # A PID change between ownership check and discovery read must not be accepted.
            if result.get('pid')!=context['pid']:
                return {'status':'blocked','reason':'host_changed_during_health_check'}
            return result
        except bridge.ConnectionFailure as exc:
            return {'status':'blocked','reason':str(exc),'health_requested':False}

    def _monitor(self):
        while not self.stop.is_set():
            try:
                with self.lock:
                    sample=self.job.inspect()
                    self.report['last_observation']=sample
                    self.report['max_input_windows_observed']=max(
                        self.report['max_input_windows_observed'],sample['input_window_count'])
                    if not sample['input_desktop_unchanged'] or sample['input_window_count']:
                        raise HostFailure('desktop_isolation_lost')
                    if not sample['job_pids']:raise HostFailure('owned_host_exited')
                    if time.monotonic()-self.started>=self.lifetime_timeout:
                        raise HostFailure('host_lifetime_timeout')
            except (HostFailure,DesktopFailure,OSError):
                # Record a fixed reason; never serialize arbitrary environment/server errors.
                with self.lock:
                    self.failure='host_monitor_failed'
                    if 'sample' in locals():
                        if not sample['input_desktop_unchanged'] or sample['input_window_count']:
                            self.failure='desktop_isolation_lost'
                        elif not sample['job_pids']:self.failure='owned_host_exited'
                        elif time.monotonic()-self.started>=self.lifetime_timeout:
                            self.failure='host_lifetime_timeout'
                    self.report.update(status='failed',reason=self.failure)
                    # Kill only this owned job. Owner thread releases handles/mutex later.
                    self.job.kernel.TerminateJobObject(self.job.job,1)
                return
            self.stop.wait(.2)

    def ensure_running(self):
        if self.failure:raise HostFailure(self.failure)
        if self.closed or not self.job:raise HostFailure('managed_host_not_running')

    def __enter__(self):
        try:
            if editor_pids():raise HostFailure('existing_editor_running')
            exe=self.install/'JianyingPro.exe'
            bridge.verify_host_image(exe,self.install)
            if not signature_valid(exe):raise HostFailure('host_signature_invalid')
            bridge.verify_host_image(exe,self.install)
            self.output.mkdir(parents=True,exist_ok=False)
            self.report['index_before']=index_state(self.index)
            if self.index.is_file():
                backup=self.output/'root_meta_info.before.json';shutil.copy2(self.index,backup)
                if hashlib.sha256(backup.read_bytes()).hexdigest()!=self.report['index_before']['sha256']:
                    raise HostFailure('homepage_backup_changed_during_copy')
            if editor_pids():raise HostFailure('editor_appeared_during_preflight')
            self.job=DesktopJob();self.started=time.monotonic()
            pid=self.job.start(exe,[],child_environment(),self.install)
            self.report.update(status='waiting_for_health',started_pid=pid)
            self.thread=threading.Thread(target=self._monitor,name='jianying-host-watchdog',daemon=True)
            self.thread.start();self.save();consecutive=0;previous_pid=None
            while time.monotonic()-self.started<self.startup_timeout:
                self.ensure_running()
                self._check_other_editors()
                health=self._owned_health()
                self.report['health_observations'].append({'seconds':round(time.monotonic()-self.started,2),**health})
                if health['status']=='healthy':
                    consecutive=consecutive+1 if previous_pid==health['pid'] else 1
                    previous_pid=health['pid']
                else:consecutive=0;previous_pid=None
                self.save()
                if consecutive>=2:
                    self.ensure_running();self.report.update(status='healthy',healthy_host_pid=previous_pid)
                    self.save();return self
                self.stop.wait(2)
            raise HostFailure('official_host_health_timeout')
        except BaseException as exc:
            self.report.update(status='failed',reason=str(exc) if isinstance(exc,(HostFailure,DesktopFailure,bridge.ConnectionFailure)) else 'host_start_failed')
            self.close();raise

    def close(self):
        if self.closed:return self.report
        self.closed=True;self.stop.set()
        if self.thread:self.thread.join(timeout=5)
        if self.thread and self.thread.is_alive():
            self.report.update(status='failed',reason='host_monitor_shutdown_timeout')
        with self.lock:
            if self.job:self.report['cleanup']=self.job.close()
            self.report['index_after']=index_state(self.index)
            self.report['index_unchanged']=self.report.get('index_before')==self.report['index_after']
            if self.job:
                try:self.report['editor_pids_after']=editor_pids()
                except (OSError,ValueError,subprocess.TimeoutExpired):self.report['editor_process_recheck_failed']=True
            if self.failure:self.report.update(status='failed',reason=self.failure)
            elif self.report['status']=='healthy':self.report['status']='closed_after_healthy_connection'
            if self.job and not self.report['cleanup'].get('owned_processes_ended'):
                self.report.update(status='cleanup_incomplete',reason='owned_process_exit_unverified')
            if self.report.get('index_before') and not self.report['index_unchanged']:
                self.report.update(status='homepage_index_changed',reason='review_preserved_backup_before_further_host_runs')
            self.save()
        return self.report

    def __exit__(self,kind,value,traceback):
        self.close()
        if kind is None and self.report['status']!='closed_after_healthy_connection':
            raise HostFailure(self.report.get('reason','managed_host_cleanup_failed'))
