"""Process ownership: every process an agent execution starts dies with the orchestrator.

Package B measured the gap this closes: a hard-killed orchestrator (Task Manager, a closed
terminal, `TerminateProcess`) left `opencode.exe` running, still able to write into the worktree a
resumed run would use. Nothing in-process can help there - a killed process runs no `finally`, no
`atexit`, no signal handler. What *can* help is something the operating system does on the
process's behalf when it dies, and on Windows that is a **job object** with
`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`: when the last handle to the job closes - which the kernel
does itself when the owning process exits, however it exits - every process in the job is ended.

How ownership is established
----------------------------
* **One job per execution.** Each process started through `spawn_owned` gets its own job, so
  stopping one execution (`stop_owned`) ends exactly its processes and no one else's, and a
  retained native session can be released on its own (`release_owned`).
* **No race.** The process is created suspended, assigned to its job, and only then resumed, so
  it cannot start a child before it is owned. Children inherit job membership from their parent
  automatically, including a grandchild whose parent has already exited - the case a
  parent-PID tree walk (`taskkill /T`) cannot reach.
* **Only what was started here.** Nothing is ever found by name or by scanning the process table.
  A job contains a process only because this module put it there.
* **Degrade, never fail** (invariant 6). If a job cannot be created or assigned - an old Windows,
  a restrictive outer job - the process still runs, and the caller's existing tree stop
  (`taskkill /T` on the PID it holds a handle to) remains the fallback.

Everything here uses documented Win32 APIs only (`CreateJobObjectW`, `SetInformationJobObject`,
`AssignProcessToJobObject`, `TerminateJobObject`, `QueryInformationJobObject`,
`CreateToolhelp32Snapshot`/`Thread32First`/`Thread32Next`, `OpenThread`, `ResumeThread`,
`OpenProcess`, `WaitForSingleObject`). On other platforms `spawn_owned` is `subprocess.Popen`.
"""

import subprocess
import sys
import threading
from typing import Any, Callable, List, Optional

IS_WINDOWS = sys.platform == "win32"

#: The attribute a Popen carries its job on. Private to this module and `launcher.stop_tree`.
_JOB_ATTR = "_orchestrator_job"

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectBasicProcessIdList = 3
    _JobObjectExtendedLimitInformation = 9
    CREATE_SUSPENDED = 0x00000004
    _PROCESS_TERMINATE = 0x0001
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SYNCHRONIZE = 0x00100000
    _THREAD_SUSPEND_RESUME = 0x0002
    _TH32CS_SNAPTHREAD = 0x00000004
    _WAIT_OBJECT_0 = 0x00000000
    _WAIT_TIMEOUT = 0x00000102
    _INFINITE = 0xFFFFFFFF
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC_LIMIT),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _MAX_LISTED = 1024

    class _PID_LIST(ctypes.Structure):
        _fields_ = [
            ("NumberOfAssignedProcesses", wintypes.DWORD),
            ("NumberOfProcessIdsInList", wintypes.DWORD),
            ("ProcessIdList", ctypes.c_size_t * _MAX_LISTED),
        ]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    def _fn(name: str, restype: Any, *argtypes: Any) -> Any:
        fn = getattr(_kernel32, name)
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    _CreateJobObjectW = _fn("CreateJobObjectW", wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCWSTR)
    _SetInformationJobObject = _fn(
        "SetInformationJobObject", wintypes.BOOL,
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    )
    _QueryInformationJobObject = _fn(
        "QueryInformationJobObject", wintypes.BOOL,
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID,
    )
    _AssignProcessToJobObject = _fn(
        "AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE
    )
    _TerminateJobObject = _fn("TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
    _CloseHandle = _fn("CloseHandle", wintypes.BOOL, wintypes.HANDLE)
    _OpenProcess = _fn("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _WaitForSingleObject = _fn("WaitForSingleObject", wintypes.DWORD, wintypes.HANDLE, wintypes.DWORD)
    _CreateToolhelp32Snapshot = _fn(
        "CreateToolhelp32Snapshot", wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD
    )
    _Thread32First = _fn(
        "Thread32First", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)
    )
    _Thread32Next = _fn(
        "Thread32Next", wintypes.BOOL, wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)
    )
    _OpenThread = _fn("OpenThread", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    _ResumeThread = _fn("ResumeThread", wintypes.DWORD, wintypes.HANDLE)
    _TerminateProcess = _fn("TerminateProcess", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
else:  # pragma: no cover - exercised only off Windows
    CREATE_SUSPENDED = 0


class ProcessJob:
    """One kill-on-close job object. Every process in it ends when it is closed or terminated.

    `active` is False when the job could not be created (or off Windows); every method is then a
    harmless no-op returning False, so callers never branch on the platform.
    """

    def __init__(self) -> None:
        self._handle: Optional[int] = None
        self._lock = threading.Lock()
        if not IS_WINDOWS:
            return
        handle = _CreateJobObjectW(None, None)
        if not handle:
            return
        if not self._set_kill_on_close(handle, True):
            _CloseHandle(handle)
            return
        self._handle = handle

    @staticmethod
    def _set_kill_on_close(handle: int, enabled: bool) -> bool:
        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE if enabled else 0
        return bool(
            _SetInformationJobObject(
                handle, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            )
        )

    @property
    def active(self) -> bool:
        return self._handle is not None

    def assign_handle(self, process_handle: Any) -> bool:
        """Put the process behind an open process handle into this job."""
        with self._lock:
            if self._handle is None:
                return False
            try:
                return bool(_AssignProcessToJobObject(self._handle, int(process_handle)))
            except Exception:
                return False

    def assign_pid(self, pid: Any) -> bool:
        """Put a process into this job by PID. Only for a process this module did not create
        (pywinpty's ConPTY child) - `spawn_owned` is race-free, this is not."""
        if not IS_WINDOWS or self._handle is None:
            return False
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        handle = _OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not handle:
            return False
        try:
            return self.assign_handle(handle)
        finally:
            _CloseHandle(handle)

    def pids(self) -> List[int]:
        """The PIDs currently in this job - for diagnostics and tests, never for killing."""
        with self._lock:
            if self._handle is None:
                return []
            info = _PID_LIST()
            if not _QueryInformationJobObject(
                self._handle, _JobObjectBasicProcessIdList, ctypes.byref(info),
                ctypes.sizeof(info), None,
            ):
                return []
            return [int(info.ProcessIdList[i]) for i in range(info.NumberOfProcessIdsInList)]

    def terminate(self, exit_code: int = 1) -> bool:
        """End every process in this job now. The job stays open until `close`."""
        with self._lock:
            if self._handle is None:
                return False
            return bool(_TerminateJobObject(self._handle, int(exit_code)))

    def release(self) -> bool:
        """Let this job's processes outlive it: closing the job will no longer end them.

        For a process kept running on purpose after its execution (a retained native TUI
        session, `native_sessions`), which is then accounted for by its own registry instead.
        """
        with self._lock:
            if self._handle is None:
                return False
            return self._set_kill_on_close(self._handle, False)

    def close(self) -> None:
        """Close the job. Unless released, every process still in it ends."""
        with self._lock:
            if self._handle is not None:
                try:
                    _CloseHandle(self._handle)
                finally:
                    self._handle = None


def _resume_process(pid: int) -> int:
    """Resume every thread of a process created suspended. Returns how many were resumed."""
    snapshot = _CreateToolhelp32Snapshot(_TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == _INVALID_HANDLE_VALUE:
        return 0
    resumed = 0
    try:
        entry = _THREADENTRY32()
        entry.dwSize = ctypes.sizeof(entry)
        more = _Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = _OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    try:
                        if _ResumeThread(thread) != 0xFFFFFFFF:
                            resumed += 1
                    finally:
                        _CloseHandle(thread)
            more = _Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        _CloseHandle(snapshot)
    return resumed


def spawn_owned(args: Any, **kwargs: Any) -> subprocess.Popen:
    """`subprocess.Popen`, with the new process and everything it starts owned by its own job.

    Same arguments, same return value. The process is created suspended, put in a kill-on-close
    job, then resumed; its job travels on the Popen for `stop_owned` / `finish_owned` /
    `release_owned`. When ownership cannot be established the process is still resumed and
    returned un-owned - a process is never left suspended, and never refused.
    """
    if not IS_WINDOWS:
        return subprocess.Popen(args, **kwargs)
    job = ProcessJob()
    if not job.active:
        return subprocess.Popen(args, **kwargs)

    flags = int(kwargs.pop("creationflags", 0) or 0) | CREATE_SUSPENDED
    try:
        process = subprocess.Popen(args, creationflags=flags, **kwargs)
    except BaseException:
        job.close()
        raise

    handle = getattr(process, "_handle", None)
    pid = getattr(process, "pid", None)
    if not isinstance(pid, int) or not isinstance(handle, int):
        # A stand-in for Popen (a test's fake): nothing real was created, so nothing is owned.
        job.close()
        return process

    owned = job.assign_handle(handle)
    if _resume_process(pid) == 0:
        # A suspended process nobody can resume would hang its caller until the timeout. It was
        # created a moment ago by this call, so ending it is ending our own work, not a guess.
        try:
            _TerminateProcess(int(handle), 1)
        except Exception:
            pass
        job.close()
        raise OSError(f"could not resume the process it just started (pid {pid})")

    if owned:
        setattr(process, _JOB_ATTR, job)
    else:
        job.close()
    return process


def own_pid(pid: Any) -> Optional[ProcessJob]:
    """Own an already-running process (and what it starts from now on) by PID, or None."""
    job = ProcessJob()
    if job.active and job.assign_pid(pid):
        return job
    job.close()
    return None


def job_of(process: Any) -> Optional[ProcessJob]:
    """The job a process was started into by `spawn_owned`, or None."""
    job = getattr(process, _JOB_ATTR, None)
    return job if isinstance(job, ProcessJob) else None


def stop_owned(process: Any) -> bool:
    """End the process and everything it started. False when it was not owned by a job."""
    job = job_of(process)
    return bool(job and job.terminate())


def finish_owned(process: Any) -> None:
    """Close the process's job once its execution is over. Anything it left running ends too."""
    job = job_of(process)
    if job is not None:
        job.close()
        try:
            delattr(process, _JOB_ATTR)
        except AttributeError:
            pass


def release_owned(process: Any) -> bool:
    """Let a deliberately kept process outlive the orchestrator (see `ProcessJob.release`)."""
    job = job_of(process)
    if job is None:
        return False
    released = job.release()
    finish_owned(process)
    return released


def process_alive(pid: Any) -> bool:
    """True when a process with this PID is running. For diagnostics and tests."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if not IS_WINDOWS:  # pragma: no cover
        import os

        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    handle = _OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        return _WaitForSingleObject(handle, 0) == _WAIT_TIMEOUT
    finally:
        _CloseHandle(handle)


def watch_owner(owner_pid: Any, on_gone: Callable[[], None]) -> Optional[threading.Thread]:
    """Call `on_gone` if the process `owner_pid` exits. For a process the orchestrator did not
    start and so cannot own - a visible runner hosted by Windows Terminal or by the IDE's
    terminal bridge - which must still not outlive the run that asked for it.

    A handle to the owner is opened *now*, while it is known to be alive, so the watch follows
    that exact process and never a later one that reused its PID. When the owner cannot be
    opened, nothing is watched and nothing is ever stopped on a guess.
    """
    if not IS_WINDOWS:
        return None
    try:
        owner_pid = int(owner_pid)
    except (TypeError, ValueError):
        return None
    if owner_pid <= 0:
        return None
    handle = _OpenProcess(_SYNCHRONIZE, False, owner_pid)
    if not handle:
        return None

    def wait() -> None:
        try:
            if _WaitForSingleObject(handle, _INFINITE) == _WAIT_OBJECT_0:
                on_gone()
        except Exception:
            pass
        finally:
            _CloseHandle(handle)

    thread = threading.Thread(target=wait, name="owner-watch", daemon=True)
    thread.start()
    return thread
