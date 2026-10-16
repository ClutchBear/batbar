"""Win32 HID / SetupAPI 的 ctypes 共享声明（唯一真相源）。

── 为什么需要这个模块（2026-10-13 黑盒冒烟测试抓到的真 bug）──
`ctypes.windll.setupapi` / `ctypes.windll.hid` 是**进程级单例**，函数的
`argtypes` 是**全局可变状态**。`dongle` / `receiver24` / `wired24` 三个模块
各自定义了一份 `_GUID` / `_IFD` / `_ATTRS`（布局相同、**类对象不同**），
又各自重写 `SetupDiEnumDeviceInterfaces.argtypes` 等 —— **最后设置的那个赢**。

之后任何别的模块拿**自己的** `_GUID` / `_IFD` 去调用同一个函数，ctypes 会
按「类型对象身份」校验，直接抛：

    TypeError: expected LP__GUID instance instead of pointer to _GUID
    TypeError: expected LP__IFD  instance instead of pointer to _IFD

（`agrs` 被上层 try/except 吞掉后，表现为 **2.4G 条目从托盘菜单里悄悄消失**，
以及 `2.4G 快照失败（2.4G 条目会缺失）` 这类 WARNING。2026-10-12 `wired24.py`
曾经单独规避过这个坑，但 `dongle.py` / `receiver24.py` 没同步 ⇒ 回归。）

── 修法（本模块）──
1. **结构体只此一份**：`GUID` / `IFD` / `DETAIL` / `ATTRS` / `CAPS`。
2. **指针参数一律 `ctypes.c_void_p`**：这样即使别的代码仍传自己的结构体指针
   也不会被身份校验拦下（`byref(任意 Structure)` 传给 `c_void_p` 是合法的）。
3. **声明函数只做一次**（`ensure_argtypes()` 幂等）：三个模块都调它，结果一致，
   谁先谁后都不影响。

⚠️ 铁律：新增 Win32 HID/SetupAPI 调用时，**签名一律加在这里**，
不要在业务模块里写 `xxx.argtypes = [...]`（会重新引入全局覆盖竞争）。
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, wintypes

# ---------------------------------------------------------------- 结构体
# 全部按 MSDN 字段顺序，错一位就读到垃圾。


class GUID(ctypes.Structure):
    """Windows GUID（16 字节）。"""

    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class IFD(ctypes.Structure):
    """SP_DEVICE_INTERFACE_DATA。

    ⚠️ 调用前**必须**设 `cbSize = ctypes.sizeof(IFD)`，否则第一条枚举就失败
    （枚举出 0 个，最难查）。
    """

    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("InterfaceClassGuid", GUID),
        ("Flags", wintypes.DWORD),
        ("Reserved", ctypes.c_ulonglong),
    ]


class DETAIL(ctypes.Structure):
    """SP_DEVICE_INTERFACE_DETAIL_DATA_W。

    ⚠️ x64 下 `cbSize` 必须填 **8**（对齐后的 sizeof(DWORD)+padding），
    填 0/6 会 `ERROR_INVALID_DATA`。
    """

    _fields_ = [("cbSize", wintypes.DWORD), ("DevicePath", wintypes.WCHAR * 1024)]


class ATTRS(ctypes.Structure):
    """HIDD_ATTRIBUTES。"""

    _fields_ = [
        ("Size", wintypes.ULONG),
        ("VendorID", ctypes.c_ushort),
        ("ProductID", ctypes.c_ushort),
        ("VersionNumber", ctypes.c_ushort),
    ]


class CAPS(ctypes.Structure):
    """HIDP_CAPS —— 字段顺序照 MSDN，错一位就读到垃圾长度。"""

    _fields_ = [
        ("Usage", ctypes.c_ushort),
        ("UsagePage", ctypes.c_ushort),
        ("InputReportByteLength", ctypes.c_ushort),
        ("OutputReportByteLength", ctypes.c_ushort),
        ("FeatureReportByteLength", ctypes.c_ushort),
        ("Reserved", ctypes.c_ushort * 17),
        ("NumberLinkCollectionNodes", ctypes.c_ushort),
        ("NumberInputButtonCaps", ctypes.c_ushort),
        ("NumberInputValueCaps", ctypes.c_ushort),
        ("NumberInputDataIndices", ctypes.c_ushort),
        ("NumberOutputButtonCaps", ctypes.c_ushort),
        ("NumberOutputValueCaps", ctypes.c_ushort),
        ("NumberOutputDataIndices", ctypes.c_ushort),
        ("NumberFeatureButtonCaps", ctypes.c_ushort),
        ("NumberFeatureValueCaps", ctypes.c_ushort),
        ("NumberFeatureDataIndices", ctypes.c_ushort),
    ]


# ---------------------------------------------------------------- 句柄/常量
def invalid_handle() -> int:
    """INVALID_HANDLE_VALUE（(HANDLE)-1）。"""
    return ctypes.c_void_p(-1).value


DIGCF_PRESENT = 0x02
DIGCF_DEVICEINTERFACE = 0x10

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x01
FILE_SHARE_WRITE = 0x02
OPEN_EXISTING = 3

# HID/SetupAPI 函数指针参数：**一律 c_void_p**（见模块头「修法」第 2 条）。
_DETAIL_SIZE = ctypes.sizeof(DETAIL)

_argtypes_done = False


def ensure_argtypes() -> None:
    """声明 HID / SetupAPI 函数签名（幂等，可重复调用）。

    所有指针参数用 `c_void_p` 而不是 `POINTER(结构体)` —— 这样不同模块
    传各自定义的结构体指针也不会触发 ctypes 的「类型身份」校验失败。
    """
    global _argtypes_done
    if _argtypes_done:
        return

    try:
        setupapi = ctypes.windll.setupapi  # type: ignore[attr-defined]
        hid = ctypes.windll.hid  # type: ignore[attr-defined]
        k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    except AttributeError:
        # 非 Windows（ctypes.windll 不存在）或单测把 ctypes.windll 换成了
        # 只含 kernel32 的替身 —— 此时声明签名无意义，直接返回。
        # 调用方本来就有 `except AttributeError` 兜底（返回 []/None）。
        return

    # ---- HID ----
    hid.HidD_GetHidGuid.argtypes = [ctypes.c_void_p]
    hid.HidD_GetHidGuid.restype = None
    hid.HidD_GetAttributes.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    hid.HidD_GetAttributes.restype = wintypes.BOOLEAN
    hid.HidD_GetProductString.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
    hid.HidD_GetProductString.restype = wintypes.BOOLEAN
    hid.HidD_GetPreparsedData.argtypes = [wintypes.HANDLE, POINTER(ctypes.c_void_p)]
    hid.HidD_GetPreparsedData.restype = wintypes.BOOLEAN
    hid.HidD_FreePreparsedData.argtypes = [ctypes.c_void_p]
    hid.HidD_FreePreparsedData.restype = wintypes.BOOLEAN
    hid.HidP_GetCaps.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    hid.HidP_GetCaps.restype = wintypes.LONG
    # 写 feature/report（probe_query 之类才会用到；声明无害）
    hid.HidD_SetFeature.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]
    hid.HidD_SetFeature.restype = wintypes.BOOLEAN

    # ---- SetupAPI ----
    setupapi.SetupDiGetClassDevsW.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        wintypes.HWND,
        wintypes.DWORD,
    ]
    setupapi.SetupDiGetClassDevsW.restype = wintypes.HANDLE
    setupapi.SetupDiEnumDeviceInterfaces.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    setupapi.SetupDiEnumDeviceInterfaces.restype = wintypes.BOOL
    setupapi.SetupDiGetDeviceInterfaceDetailW.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    setupapi.SetupDiGetDeviceInterfaceDetailW.restype = wintypes.BOOL
    setupapi.SetupDiDestroyDeviceInfoList.argtypes = [wintypes.HANDLE]
    setupapi.SetupDiDestroyDeviceInfoList.restype = wintypes.BOOL

    # ---- kernel32 ----
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    k32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    k32.ReadFile.restype = wintypes.BOOL
    k32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    k32.WriteFile.restype = wintypes.BOOL
    k32.CreateEventW.restype = wintypes.HANDLE
    k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CancelIoEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    k32.CancelIoEx.restype = wintypes.BOOL

    _argtypes_done = True


def hid_guid() -> GUID:
    """取 HID 设备接口类 GUID（HidD_GetHidGuid）。"""
    ensure_argtypes()
    guid = GUID()
    ctypes.windll.hid.HidD_GetHidGuid(ctypes.byref(guid))  # type: ignore[attr-defined]
    return guid
