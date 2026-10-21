"""Finite owned subprocesses. Windows Job Object contains descendants before resume."""
import ctypes
import os
import signal
import subprocess
from ctypes import wintypes as W

class OwnedProcess:
    def __init__(self,argv,**kwargs):
        self.job=None
        if os.name=='nt':
            k=ctypes.WinDLL('kernel32',use_last_error=True)
            class Basic(ctypes.Structure):
                _fields_=[('ProcessTime',ctypes.c_longlong),('JobTime',ctypes.c_longlong),('Flags',W.DWORD),
                          ('MinWS',ctypes.c_size_t),('MaxWS',ctypes.c_size_t),('Active',W.DWORD),
                          ('Affinity',ctypes.c_size_t),('Priority',W.DWORD),('Scheduling',W.DWORD)]
            class IO(ctypes.Structure):
                _fields_=[(n,ctypes.c_ulonglong) for n in ('ReadOps','WriteOps','OtherOps','ReadBytes','WriteBytes','OtherBytes')]
            class Limits(ctypes.Structure):
                _fields_=[('Basic',Basic),('IO',IO),('ProcessMemory',ctypes.c_size_t),('JobMemory',ctypes.c_size_t),
                          ('PeakProcess',ctypes.c_size_t),('PeakJob',ctypes.c_size_t)]
            k.CreateJobObjectW.argtypes=[ctypes.c_void_p,W.LPCWSTR];k.CreateJobObjectW.restype=W.HANDLE
            k.SetInformationJobObject.argtypes=[W.HANDLE,ctypes.c_int,ctypes.c_void_p,W.DWORD]
            k.AssignProcessToJobObject.argtypes=[W.HANDLE,W.HANDLE]
            k.CloseHandle.argtypes=[W.HANDLE]
            self.kernel=k;self.job=k.CreateJobObjectW(None,None)
            limits=Limits();limits.Basic.Flags=0x2000
            if not self.job or not k.SetInformationJobObject(self.job,9,ctypes.byref(limits),ctypes.sizeof(limits)):
                self.close();raise ctypes.WinError(ctypes.get_last_error())
            self.process=subprocess.Popen(argv,creationflags=0x00000004|0x08000000,**kwargs)
            if not k.AssignProcessToJobObject(self.job,W.HANDLE(int(self.process._handle))):
                self.process.kill();self.process.wait(timeout=5);self.close();raise ctypes.WinError(ctypes.get_last_error())
            resume=ctypes.WinDLL('ntdll').NtResumeProcess;resume.argtypes=[W.HANDLE];resume.restype=ctypes.c_long
            if resume(W.HANDLE(int(self.process._handle)))!=0:
                self.close();raise RuntimeError('owned suspended process could not resume')
        else:self.process=subprocess.Popen(argv,start_new_session=True,**kwargs)
    def close(self):
        if self.job:
            self.kernel.CloseHandle(self.job);self.job=None
        elif os.name!='nt' and hasattr(self,'process'):
            try:os.killpg(self.process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
        if hasattr(self,'process') and self.process.poll() is None:
            self.process.wait(timeout=5)

def run(argv,*,timeout,**kwargs):
    supplied=kwargs.pop('input',None)
    if supplied is not None:kwargs['stdin']=subprocess.PIPE
    owner=OwnedProcess(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,**kwargs)
    try:
        try:
            out,err=owner.process.communicate(input=supplied,timeout=timeout)
            return subprocess.CompletedProcess(argv,owner.process.returncode,out,err)
        except subprocess.TimeoutExpired:
            owner.close();out,err=owner.process.communicate(timeout=5)
            raise subprocess.TimeoutExpired(argv,timeout,output=out,stderr=err)
    finally:owner.close()
