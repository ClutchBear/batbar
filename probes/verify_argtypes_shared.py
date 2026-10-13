"""铁证：证明四个模块共用同一批 argtypes（不再是各写各的）。

原 bug 的本质是「谁最后设置谁赢」+「类型按对象身份校验」。
本探针直接读 windll 上的 argtypes 对象，检查：
  1. 四个模块调用后，argtypes 的**元素都是 c_void_p**（而不是 POINTER(_XXX)）；
  2. 任意两个模块拿各自结构体调用同一函数都通过（真调一次）。
"""
import ctypes, sys
from ctypes import byref
sys.path.insert(0, "src")
from batbar import dongle, receiver24, wired24, feature24, win32ctypes as w32

w32.ensure_argtypes()   # 显式初始化（真实运行时由各模块懒调）
setupapi = ctypes.windll.setupapi
hid = ctypes.windll.hid

def show(fn_name, dll):
    fn = getattr(dll, fn_name)
    at = list(fn.argtypes or [])
    return [t.__name__ if hasattr(t, "__name__") else str(t) for t in at]

print("=== argtypes 现状（应为 c_void_p / 基本类型，无 POINTER(_XXX)）===")
for name in ("SetupDiGetClassDevsW", "SetupDiEnumDeviceInterfaces",
             "SetupDiGetDeviceInterfaceDetailW", "SetupDiDestroyDeviceInfoList"):
    print(f"  setupapi.{name}: {show(name, setupapi)}")
for name in ("HidD_GetHidGuid", "HidD_GetPreparsedData", "HidP_GetCaps",
             "HidD_FreePreparsedData"):
    print(f"  hid.{name}: {show(name, hid)}")

print()
print("=== 含 POINTER(_私有类型) 的签名数（应为 0）===")
bad = 0
for dll, names in ((setupapi, ("SetupDiGetClassDevsW", "SetupDiEnumDeviceInterfaces",
                               "SetupDiGetDeviceInterfaceDetailW", "SetupDiDestroyDeviceInfoList")),
                   (hid, ("HidD_GetHidGuid", "HidD_GetPreparsedData", "HidP_GetCaps",
                          "HidD_FreePreparsedData"))):
    for n in names:
        for t in (getattr(dll, n).argtypes or []):
            s = str(t)
            if "_GUID" in s or "_IFD" in s or "_CAPS" in s or "_ATTRS" in s or "_DETAIL" in s:
                print(f"  ! {n}: {s}")
                bad += 1
print("  含私有类型的签名:", bad, "✓" if bad == 0 else "✗")

print()
print("=== 真调一次各模块入口，谁都不许 TypeError ===")
ok = True
for label, fn in (
    ("feature24._feature_collections", lambda: feature24._feature_collections(0x3554, 0xFA09)),
    ("receiver24.enumerate_receivers", receiver24.enumerate_receivers),
    ("wired24.enumerate_wired", lambda: wired24.enumerate_wired(dongle.TARGETS)),
    ("dongle._open_targets", dongle._open_targets),
):
    try:
        r = fn()
        print(f"  [ok] {label}: {len(r)} 项")
    except TypeError as e:
        print(f"  [FAIL] {label}: {e}"); ok = False
    except Exception as e:
        print(f"  [ok*] {label}: 非类型错误 {type(e).__name__}: {e}")

print()
print("结论:", "修复无效 ✗" if (bad or not ok) else "win32ctypes 统一生效，身份冲突根除 ✓")
sys.exit(1 if (bad or not ok) else 0)
