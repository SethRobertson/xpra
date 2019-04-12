#!/usr/bin/env python
# This file is part of Xpra.
# Copyright (c) 2017 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

#@PydevCodeAnalysisIgnore

from ctypes import POINTER, WinDLL, Structure, Union, c_void_p, c_int, c_ubyte
from ctypes.wintypes import DWORD, ULONG, HANDLE, USHORT, BOOL, INT

from ctypes.wintypes import LPCSTR
from xpra.platform.win32.constants import WAIT_ABANDONED, WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED

LPDWORD = POINTER(DWORD)
LPCVOID = c_void_p
LPVOID = c_void_p

WAIT_STR = {
    WAIT_ABANDONED  : "ABANDONED",
    WAIT_OBJECT_0   : "OBJECT_0",
    WAIT_TIMEOUT    : "TIMEOUT",
    WAIT_FAILED     : "FAILED",
    }

INFINITE = 65535
INVALID_HANDLE_VALUE = -1


class _inner_struct(Structure):
    _fields_ = [
        ('Offset',      DWORD),
        ('OffsetHigh',  DWORD),
        ]
class _inner_union(Union):
    _fields_  = [
        ('anon_struct', _inner_struct),
        ('Pointer',     c_void_p),
        ]
class OVERLAPPED(Structure):
    _fields_ = [
        ('Internal',        POINTER(ULONG)),
        ('InternalHigh',    POINTER(ULONG)),
        ('union',           _inner_union),
        ('hEvent',          HANDLE),
        ]
LPOVERLAPPED = POINTER(OVERLAPPED)

class SECURITY_ATTRIBUTES(Structure):
    _fields_ = [
        ("nLength",                 c_int),
        ("lpSecurityDescriptor",    c_void_p),
        ("bInheritHandle",          c_int),
        ]
LPSECURITY_ATTRIBUTES = POINTER(SECURITY_ATTRIBUTES)
class SECURITY_DESCRIPTOR(Structure):
    SECURITY_DESCRIPTOR_CONTROL = USHORT
    REVISION = 1
    _fields_ = [
        ('Revision',    c_ubyte),
        ('Sbz1',        c_ubyte),
        ('Control',     SECURITY_DESCRIPTOR_CONTROL),
        ('Owner',       c_void_p),
        ('Group',       c_void_p),
        ('Sacl',        c_void_p),
        ('Dacl',        c_void_p),
    ]

class TOKEN_USER(Structure):
    _fields_ = [
        ('SID',         c_void_p),
        ('ATTRIBUTES',  DWORD),
    ]


kernel32 = WinDLL("kernel32", use_last_error=True)
WaitForSingleObject = kernel32.WaitForSingleObject
WaitForSingleObject.argtypes = [HANDLE, DWORD]
WaitForSingleObject.restype = DWORD
CreateEventA = kernel32.CreateEventA
CreateEventA.restype = HANDLE
ReadFile = kernel32.ReadFile
ReadFile.argtypes = [HANDLE, LPVOID, DWORD, LPDWORD, LPOVERLAPPED]
ReadFile.restype = BOOL
WriteFile = kernel32.WriteFile
WriteFile.argtypes = [HANDLE, LPCVOID, DWORD, LPDWORD, LPOVERLAPPED]
WriteFile.restype = BOOL
CreateFileA = kernel32.CreateFileA
CreateFileA.argtypes = [LPCSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE]
CreateFileA.restype = HANDLE
WaitNamedPipeA = kernel32.WaitNamedPipeA
SetNamedPipeHandleState = kernel32.SetNamedPipeHandleState
SetNamedPipeHandleState.argtypes = [HANDLE, LPDWORD, LPDWORD, LPDWORD]
SetNamedPipeHandleState.restype = INT
GetOverlappedResult = kernel32.GetOverlappedResult
GetOverlappedResult.argtypes = [HANDLE, LPOVERLAPPED, LPDWORD, BOOL]
GetOverlappedResult.restype = BOOL
CreateNamedPipeA = kernel32.CreateNamedPipeA
CreateNamedPipeA.argtypes = [LPCSTR, DWORD, DWORD, DWORD, DWORD, DWORD, DWORD, LPSECURITY_ATTRIBUTES]
CreateNamedPipeA.restype = HANDLE
ConnectNamedPipe = kernel32.ConnectNamedPipe
ConnectNamedPipe.argtypes = [HANDLE, OVERLAPPED]
ConnectNamedPipe.restype = BOOL
DisconnectNamedPipe = kernel32.DisconnectNamedPipe
DisconnectNamedPipe.argtypes = [HANDLE]
DisconnectNamedPipe.restype = BOOL
FlushFileBuffers = kernel32.FlushFileBuffers
FlushFileBuffers.argtypes = [HANDLE]
FlushFileBuffers.restype = BOOL
GetLastError = kernel32.GetLastError
GetCurrentProcess = kernel32.GetCurrentProcess
GetCurrentProcess.restype = HANDLE

advapi32 = WinDLL("advapi32", use_last_error=True)
InitializeSecurityDescriptor = advapi32.InitializeSecurityDescriptor
SetSecurityDescriptorOwner = advapi32.SetSecurityDescriptorOwner
SetSecurityDescriptorDacl = advapi32.SetSecurityDescriptorDacl
OpenProcessToken = advapi32.OpenProcessToken
GetTokenInformation = advapi32.GetTokenInformation
