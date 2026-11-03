"""Win32 lifecycle for an owned job on an inactive desktop. No UI operations.

This is process containment, not an authorization boundary or a guarantee that
an application cannot ask an existing process to create a window elsewhere.
"""
from __future__ import annotations
import ctypes as c
from ctypes import wintypes as w
import os
from pathlib import Path
import subprocess
import time
import uuid


class DesktopFailure(ValueError):
    pass


class STARTUPINFO(c.Structure):
    _fields_=[('cb',w.DWORD),('lpReserved',w.LPWSTR),('lpDesktop',w.LPWSTR),('lpTitle',w.LPWSTR),
              ('dwX',w.DWORD),('dwY',w.DWORD),('dwXSize',w.DWORD),('dwYSize',w.DWORD),
              ('dwXCountChars',w.DWORD),('dwYCountChars',w.DWORD),('dwFillAttribute',w.DWORD),
              ('dwFlags',w.DWORD),('wShowWindow',w.WORD),('cbReserved2',w.WORD),
              ('lpReserved2',c.POINTER(c.c_byte)),('hStdInput',w.HANDLE),
              ('hStdOutput',w.HANDLE),('hStdError',w.HANDLE)]


class PROCESSINFO(c.Structure):
    _fields_=[('hProcess',w.HANDLE),('hThread',w.HANDLE),('dwProcessId',w.DWORD),('dwThreadId',w.DWORD)]


class BASIC_LIMIT(c.Structure):
    _fields_=[('PerProcessUserTimeLimit',c.c_int64),('PerJobUserTimeLimit',c.c_int64),
              ('LimitFlags',w.DWORD),('MinimumWorkingSetSize',c.c_size_t),
              ('MaximumWorkingSetSize',c.c_size_t),('ActiveProcessLimit',w.DWORD),
              ('Affinity',c.c_size_t),('PriorityClass',w.DWORD),('SchedulingClass',w.DWORD)]


class IO_COUNTERS(c.Structure):
    _fields_=[(name,c.c_uint64) for name in ('ReadOperationCount','WriteOperationCount',
              'OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]


class EXTENDED_LIMIT(c.Structure):
    _fields_=[('BasicLimitInformation',BASIC_LIMIT),('IoInfo',IO_COUNTERS),
              ('ProcessMemoryLimit',c.c_size_t),('JobMemoryLimit',c.c_size_t),
              ('PeakProcessMemoryUsed',c.c_size_t),('PeakJobMemoryUsed',c.c_size_t)]


class PID_LIST(c.Structure):
    _fields_=[('assigned',w.DWORD),('count',w.DWORD),('pids',c.c_size_t*512)]


def _bind(library,name,args,result):
    fn=getattr(library,name);fn.argtypes=args;fn.restype=result


class DesktopJob:
    """Internal primitive. Callers must verify the executable and user intent."""
    def __init__(self):
        if os.name!='nt':raise DesktopFailure('windows_required')
        self.user=c.WinDLL('user32',use_last_error=True)
        self.kernel=c.WinDLL('kernel32',use_last_error=True)
        self.callback_type=c.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
        for name,args,result in [
            ('GetUserObjectInformationW',[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.POINTER(w.DWORD)],w.BOOL),
            ('OpenInputDesktop',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE),
            ('CloseDesktop',[w.HANDLE],w.BOOL),('GetProcessWindowStation',[],w.HANDLE),
            ('CreateDesktopW',[w.LPCWSTR,w.LPCWSTR,c.c_void_p,w.DWORD,w.DWORD,c.c_void_p],w.HANDLE),
            ('EnumDesktopWindows',[w.HANDLE,self.callback_type,w.LPARAM],w.BOOL),
            ('GetWindowThreadProcessId',[w.HWND,c.POINTER(w.DWORD)],w.DWORD),
        ]:_bind(self.user,name,args,result)
        for name,args,result in [
            ('CreateProcessW',[w.LPCWSTR,w.LPWSTR,c.c_void_p,c.c_void_p,w.BOOL,w.DWORD,c.c_void_p,
                               w.LPCWSTR,c.POINTER(STARTUPINFO),c.POINTER(PROCESSINFO)],w.BOOL),
            ('CreateJobObjectW',[c.c_void_p,w.LPCWSTR],w.HANDLE),
            ('SetInformationJobObject',[w.HANDLE,c.c_int,c.c_void_p,w.DWORD],w.BOOL),
            ('QueryInformationJobObject',[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.c_void_p],w.BOOL),
            ('AssignProcessToJobObject',[w.HANDLE,w.HANDLE],w.BOOL),
            ('TerminateJobObject',[w.HANDLE,w.UINT],w.BOOL),
            ('ResumeThread',[w.HANDLE],w.DWORD),('WaitForSingleObject',[w.HANDLE,w.DWORD],w.DWORD),
            ('CloseHandle',[w.HANDLE],w.BOOL),('OpenProcess',[w.DWORD,w.BOOL,w.DWORD],w.HANDLE),
            ('TerminateProcess',[w.HANDLE,w.UINT],w.BOOL),
            ('CreateMutexW',[c.c_void_p,w.BOOL,w.LPCWSTR],w.HANDLE),
            ('ReleaseMutex',[w.HANDLE],w.BOOL),
        ]:_bind(self.kernel,name,args,result)
        self.desktop=self.input_desktop=self.job=self.mutex=None
        self.pi=PROCESSINFO();self.assigned=False;self.handles={};self.seen=set()
        self.before=None;self.closed=False;self.close_report=None

    def name(self,handle):
        size=w.DWORD()
        self.user.GetUserObjectInformationW(handle,2,None,0,c.byref(size))
        if not 2<=size.value<=65536:raise DesktopFailure('desktop_name_unavailable')
        buf=c.create_unicode_buffer(size.value//2)
        if not self.user.GetUserObjectInformationW(handle,2,buf,size.value,c.byref(size)):
            raise DesktopFailure('desktop_name_unavailable')
        return buf.value

    def input_name(self):
        handle=self.user.OpenInputDesktop(0,False,1)
        if not handle:raise DesktopFailure('input_desktop_unavailable')
        try:return self.name(handle)
        finally:self.user.CloseDesktop(handle)

    def start(self,exe,args,environment,cwd):
        if self.closed or self.job:raise DesktopFailure('lifecycle_already_used')
        try:
            # An OS mutex, not a stale file, prevents simultaneous Agent hosts.
            self.mutex=self.kernel.CreateMutexW(None,False,'Local\\JianyingAgentIsolatedHost')
            if not self.mutex:raise DesktopFailure('lifecycle_lock_unavailable')
            if self.kernel.WaitForSingleObject(self.mutex,0) not in (0,0x80):
                self.kernel.CloseHandle(self.mutex);self.mutex=None
                raise DesktopFailure('another_managed_host_active')
            self.before=self.input_name();station=self.name(self.user.GetProcessWindowStation())
            self.desktop_name='JianyingAgentHost_'+uuid.uuid4().hex
            # Excludes DESKTOP_SWITCHDESKTOP. The application may create its own UI here.
            self.desktop=self.user.CreateDesktopW(self.desktop_name,None,None,0,0xff,None)
            self.input_desktop=self.user.OpenInputDesktop(0,False,0x41)
            self.job=self.kernel.CreateJobObjectW(None,None)
            if not all((self.desktop,self.input_desktop,self.job)):
                raise DesktopFailure('desktop_or_job_creation_failed')
            limits=EXTENDED_LIMIT();limits.BasicLimitInformation.LimitFlags=0x2000
            if not self.kernel.SetInformationJobObject(self.job,9,c.byref(limits),c.sizeof(limits)):
                raise DesktopFailure('job_limit_failed')
            si=STARTUPINFO();si.cb=c.sizeof(si);si.lpDesktop=station+'\\'+self.desktop_name
            command=c.create_unicode_buffer(subprocess.list2cmdline([str(exe),*args]))
            env=c.create_unicode_buffer('\0'.join(k+'='+v for k,v in sorted(environment.items()))+'\0\0')
            if not self.kernel.CreateProcessW(str(exe),command,None,None,False,0x4|0x400|0x08000000,
                                              env,str(cwd),c.byref(si),c.byref(self.pi)):
                raise DesktopFailure('process_creation_failed')
            self.seen.add(self.pi.dwProcessId)
            if not self.kernel.AssignProcessToJobObject(self.job,self.pi.hProcess):
                raise DesktopFailure('job_assignment_failed')
            self.assigned=True
            if self.kernel.ResumeThread(self.pi.hThread)==0xffffffff:
                raise DesktopFailure('process_resume_failed')
            return self.pi.dwProcessId
        except BaseException:
            self.close();raise

    def members(self):
        value=PID_LIST()
        if not self.kernel.QueryInformationJobObject(self.job,3,c.byref(value),c.sizeof(value),None):
            raise DesktopFailure('job_inspection_failed')
        if value.assigned>512:raise DesktopFailure('job_process_limit_exceeded')
        return set(value.pids[:value.count])

    def _windows(self,desktop,pids):
        count=0
        @self.callback_type
        def visit(hwnd,unused):
            nonlocal count
            pid=w.DWORD();self.user.GetWindowThreadProcessId(hwnd,c.byref(pid))
            if pid.value in pids:count+=1
            return True
        c.set_last_error(0)
        if not self.user.EnumDesktopWindows(desktop,visit,0) and c.get_last_error():
            raise DesktopFailure('window_inspection_failed')
        return count

    def inspect(self):
        if self.closed or not self.assigned:raise DesktopFailure('host_not_running')
        pids=self.members();self.seen.update(pids)
        for pid in pids-self.handles.keys():
            handle=self.kernel.OpenProcess(0x100000|0x1000,False,pid)
            if handle:self.handles[pid]=handle
        return {'job_pids':sorted(pids),'input_desktop_unchanged':self.input_name()==self.before,
                'input_window_count':self._windows(self.input_desktop,self.seen),
                'isolated_window_count':self._windows(self.desktop,pids)}

    def close(self):
        if self.closed:return self.close_report
        self.closed=True
        report={'owned_processes_ended':False,'owned_pids':sorted(self.seen),'errors':[]}
        try:
            deadline=time.monotonic()+8
            try:
                if self.assigned:
                    try:
                        final_members=self.members();self.seen.update(final_members)
                        report['owned_pids']=sorted(self.seen)
                        for pid in final_members-self.handles.keys():
                            handle=self.kernel.OpenProcess(0x100000|0x1000,False,pid)
                            if handle:self.handles[pid]=handle
                    except DesktopFailure:
                        report['errors'].append('final_job_members_unavailable')
                if self.pi.hProcess:
                    ok=(self.kernel.TerminateJobObject(self.job,0) if self.assigned else
                        self.kernel.TerminateProcess(self.pi.hProcess,1))
                    if not ok:report['errors'].append('owned_termination_failed')
                if self.assigned:
                    try:
                        while True:
                            active=self.members()
                            if not active or time.monotonic()>=deadline:break
                            time.sleep(.05)
                        report['remaining_job_pids']=sorted(active)
                        if active:report['errors'].append('owned_job_still_active')
                    except DesktopFailure:report['errors'].append('job_exit_unverified')
            finally:
                # Kill-on-close remains a fallback even if any observation fails.
                if self.job:
                    if not self.kernel.CloseHandle(self.job):report['errors'].append('job_handle_close_failed')
                    self.job=None
            pending=[]
            for pid,handle in {**self.handles,**({self.pi.dwProcessId:self.pi.hProcess} if self.pi.hProcess else {})}.items():
                remaining=max(0,int((deadline-time.monotonic())*1000))
                if self.kernel.WaitForSingleObject(handle,remaining)!=0:pending.append(pid)
            report['pending_owned_pids']=pending
            report['owned_processes_ended']=not pending and not report['errors']
        finally:
            for handle in self.handles.values():self.kernel.CloseHandle(handle)
            if self.pi.hThread:self.kernel.CloseHandle(self.pi.hThread)
            if self.pi.hProcess:self.kernel.CloseHandle(self.pi.hProcess)
            if self.input_desktop:self.user.CloseDesktop(self.input_desktop)
            if self.desktop:report['desktop_closed']=bool(self.user.CloseDesktop(self.desktop))
            if self.mutex:
                self.kernel.ReleaseMutex(self.mutex);self.kernel.CloseHandle(self.mutex)
            try:report['input_desktop_unchanged']=self.input_name()==self.before
            except DesktopFailure:report['input_desktop_unchanged']=False
            self.close_report=report
        return report


def child_environment():
    allowed={'SYSTEMROOT','WINDIR','SYSTEMDRIVE','COMSPEC','PATHEXT','TEMP','TMP','PATH',
             'USERPROFILE','LOCALAPPDATA','APPDATA','PROGRAMDATA','USERNAME','USERDOMAIN',
             'HOMEDRIVE','HOMEPATH','ALLUSERSPROFILE','PROGRAMFILES','PROGRAMFILES(X86)',
             'COMMONPROGRAMFILES','COMMONPROGRAMFILES(X86)','PROGRAMW6432','SESSIONNAME'}
    return {k:v for k,v in os.environ.items() if k.upper() in allowed}
