# -*- coding: utf-8 -*-
"""
串口释放助手
------------------------------------------------------------------
只做三件事：
    ① 列出所有串口，并显示「哪个程序正在占用它」
    ② 选中那个口，点一下强制断开
    ③ 断开后自动复查，直接告诉你「空闲了吗」

除 Python 标准库外不需要任何第三方包（界面用 tkinter，系统调用用 ctypes）。
适用系统：Windows 7 / 8 / 10 / 11（64 位）
"""

import ctypes
import ctypes.wintypes as wt
import os
import queue
import sys
import threading
import time
import tkinter as tk
import winreg
from tkinter import messagebox, ttk

APP_NAME = "串口释放助手"
APP_VER = "2.0"
TITLE = "%s v%s" % (APP_NAME, APP_VER)

# =====================================================================
# 一、Windows API 封装
# =====================================================================
k32 = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
CREATE_ALWAYS = 2
FILE_ATTRIBUTE_NORMAL = 0x80
PROCESS_DUP_HANDLE = 0x00000040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DUPLICATE_CLOSE_SOURCE = 0x00000001
FILE_TYPE_CHAR = 2
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

k32.CreateFileW.restype = wt.HANDLE
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                            wt.DWORD, wt.DWORD, wt.HANDLE]
k32.CloseHandle.restype = wt.BOOL
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.GetFileType.restype = wt.DWORD
k32.GetFileType.argtypes = [wt.HANDLE]
k32.OpenProcess.restype = wt.HANDLE
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.GetCurrentProcess.restype = wt.HANDLE
k32.DuplicateHandle.restype = wt.BOOL
k32.DuplicateHandle.argtypes = [wt.HANDLE, wt.HANDLE, wt.HANDLE,
                                ctypes.POINTER(wt.HANDLE), wt.DWORD, wt.BOOL, wt.DWORD]
k32.QueryDosDeviceW.restype = wt.DWORD
k32.QueryDosDeviceW.argtypes = [wt.LPCWSTR, wt.LPWSTR, wt.DWORD]
k32.QueryFullProcessImageNameW.restype = wt.BOOL
k32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR,
                                           ctypes.POINTER(wt.DWORD)]

ntdll.NtQuerySystemInformation.restype = ctypes.c_long
ntdll.NtQuerySystemInformation.argtypes = [ctypes.c_int, ctypes.c_void_p,
                                           wt.ULONG, ctypes.POINTER(wt.ULONG)]
ntdll.NtQueryObject.restype = ctypes.c_long
ntdll.NtQueryObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                wt.ULONG, ctypes.POINTER(wt.ULONG)]


class _UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT),
                ("Buffer", ctypes.c_void_p)]


class _HANDLE_ENTRY(ctypes.Structure):
    _fields_ = [("Object", ctypes.c_void_p),
                ("UniqueProcessId", ctypes.c_void_p),
                ("HandleValue", ctypes.c_void_p),
                ("GrantedAccess", ctypes.c_ulong),
                ("CreatorBackTraceIndex", ctypes.c_ushort),
                ("ObjectTypeIndex", ctypes.c_ushort),
                ("HandleAttributes", ctypes.c_ulong),
                ("Reserved", ctypes.c_ulong)]


class _HANDLE_INFO(ctypes.Structure):
    _fields_ = [("NumberOfHandles", ctypes.c_void_p),
                ("Reserved", ctypes.c_void_p),
                ("Handles", _HANDLE_ENTRY * 1)]


def list_ports():
    """返回 [("COM6", "\\Device\\VCP0"), ...]"""
    out = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return out
    try:
        i = 0
        while True:
            try:
                _, value, _ = winreg.EnumValue(key, i)
            except OSError:
                break
            i += 1
            if isinstance(value, str) and value.upper().startswith("COM"):
                out.append((value.upper(), ""))
    finally:
        winreg.CloseKey(key)

    # 补上设备路径（用于锁定时定位占用者）
    result = []
    for port, _ in out:
        buf = ctypes.create_unicode_buffer(512)
        n = k32.QueryDosDeviceW(port, buf, 512)
        dev = buf.value if n else ""
        result.append((port, dev))

    def _num(item):
        try:
            return int(item[0][3:])
        except ValueError:
            return 999
    return sorted(set(result), key=_num)


def port_state(port):
    """空闲 / 被占用 / 不存在 —— 靠能否独占打开来判断"""
    h = k32.CreateFileW("\\\\.\\" + port, GENERIC_READ | GENERIC_WRITE, 0, None,
                        OPEN_EXISTING, 0, None)
    if h and h != INVALID_HANDLE_VALUE:
        k32.CloseHandle(h)
        return "idle"
    err = ctypes.get_last_error()
    if err in (2, 3):          # 找不到路径
        return "missing"
    return "busy"              # 5=拒绝访问 32=共享冲突 等，都算被占用


def _enum_handles():
    """取出全系统句柄表"""
    size = 1 << 21
    for _ in range(16):
        buf = ctypes.create_string_buffer(size)
        need = wt.ULONG(0)
        status = ntdll.NtQuerySystemInformation(64, buf, size, ctypes.byref(need))
        status &= 0xFFFFFFFF
        if status == 0:
            info = ctypes.cast(buf, ctypes.POINTER(_HANDLE_INFO)).contents
            count = int(info.NumberOfHandles or 0)
            if count <= 0:
                return []
            addr = ctypes.addressof(info) + 16       # 跳过两个 ULONG_PTR 头
            arr = ctypes.cast(addr, ctypes.POINTER(_HANDLE_ENTRY * count)).contents
            return list(arr)
        if status == 0xC0000004:                     # 缓冲区不够
            size = max(need.value + (1 << 16), size * 2)
            continue
        return []
    return []


def _file_type_index():
    """查出「File」这类对象的类型索引，用来从 11 万个句柄里先粗筛"""
    probe = os.path.join(os.environ.get("TEMP", "."), "_srt_probe.tmp")
    h = k32.CreateFileW(probe, GENERIC_READ | GENERIC_WRITE, 0, None,
                        CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, None)
    if not h or h == INVALID_HANDLE_VALUE:
        return None
    idx = None
    try:
        me = os.getpid()
        for e in _enum_handles():
            if int(e.UniqueProcessId or 0) == me and int(e.HandleValue or 0) == int(h):
                idx = e.ObjectTypeIndex
                break
    finally:
        k32.CloseHandle(h)
        try:
            os.remove(probe)
        except OSError:
            pass
    return idx


def _object_name(handle):
    buf = ctypes.create_string_buffer(4096)
    ret = wt.ULONG(0)
    status = ntdll.NtQueryObject(handle, 1, buf, len(buf), ctypes.byref(ret))
    if (status & 0xFFFFFFFF) != 0:
        return ""
    us = ctypes.cast(buf, ctypes.POINTER(_UNICODE_STRING)).contents
    if not us.Buffer or not us.Length:
        return ""
    return ctypes.wstring_at(us.Buffer, us.Length // 2)


def _proc_name(pid):
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        k32.CloseHandle(h)


def find_owners(dev_map):
    """dev_map: {设备路径小写: "COM6"}  ->  {"COM6": [ {pid,name,handle}, ... ]}"""
    want = {k.lower(): v for k, v in dev_map.items() if k}
    if not want:
        return {}
    type_idx = _file_type_index()
    me = os.getpid()
    procs = {}
    found = {}
    try:
        for e in _enum_handles():
            pid = int(e.UniqueProcessId or 0)
            if pid in (0, 4, me):
                continue
            if type_idx is not None and e.ObjectTypeIndex != type_idx:
                continue
            hproc = procs.get(pid)
            if hproc is None:
                hproc = k32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
                procs[pid] = hproc
            if not hproc:
                continue
            dup = wt.HANDLE()
            if not k32.DuplicateHandle(hproc, wt.HANDLE(e.HandleValue),
                                       k32.GetCurrentProcess(), ctypes.byref(dup),
                                       0, False, 0):
                continue
            try:
                # 必须先确认是字符设备再问名字，否则碰到挂起的管道句柄会卡死
                if k32.GetFileType(dup) != FILE_TYPE_CHAR:
                    continue
                name = _object_name(dup)
            finally:
                k32.CloseHandle(dup)
            if not name:
                continue
            port = want.get(name.lower())
            if port:
                found.setdefault(port, []).append({
                    "pid": pid,
                    "handle": int(e.HandleValue or 0),
                    "name": os.path.basename(_proc_name(pid)) or ("PID %d" % pid),
                })
    finally:
        for h in procs.values():
            if h:
                k32.CloseHandle(h)
    return found


def release(pid, handle):
    """强制关掉别的进程持有的那个句柄 → 端口就释放了"""
    hproc = k32.OpenProcess(PROCESS_DUP_HANDLE, False, pid)
    if not hproc:
        return False, "无法打开进程（可能权限不足）"
    try:
        dup = wt.HANDLE()
        ok = k32.DuplicateHandle(hproc, wt.HANDLE(handle), k32.GetCurrentProcess(),
                                 ctypes.byref(dup), 0, False, DUPLICATE_CLOSE_SOURCE)
        if not ok:
            return False, "关闭句柄失败（错误码 %d）" % ctypes.get_last_error()
        return True, ""
    finally:
        k32.CloseHandle(hproc)


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def resource_path(name):
    """兼容源码运行与 PyInstaller 单文件运行"""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, name)


def enable_dpi_aware():
    """必须在创建窗口之前调用，否则 125% 缩放的屏幕上界面会被系统拉伸发虚"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


# =====================================================================
# 二、界面
# =====================================================================
class App(object):
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.scanning = False
        # port -> {"state":..., "owners":[...]}
        self.data = {}
        self.just_released = {}      # port -> 时间戳，用来显示「已断开」
        self.pending = None          # 刚断开、正在复查的端口

        root.title(TITLE)
        try:
            root.iconbitmap(resource_path("app.ico"))
        except Exception:
            pass
        self._scale()
        root.geometry("%dx%d" % (int(620 * self.k), int(380 * self.k)))
        root.minsize(int(560 * self.k), int(340 * self.k))
        self._build()
        self.root.after(120, self._poll)
        self.refresh()

    # ---------- 高 DPI 适配 ----------
    def _scale(self):
        dpi = self.root.winfo_fpixels("1i")
        self.k = max(1.0, dpi / 96.0)
        self.fs = max(9, int(round(9 * self.k)))
        self.row_h = int(round(26 * self.k))

    def _build(self):
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            try:
                style.theme_use(theme)
                break
            except tk.TclError:
                continue
        style.configure("Treeview", rowheight=self.row_h,
                        font=("Microsoft YaHei UI", self.fs))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", self.fs, "bold"))

        wrap = ttk.Frame(self.root, padding=(10, 8, 10, 4))
        wrap.pack(fill="both", expand=True)

        body = ttk.Frame(wrap)
        body.pack(fill="both", expand=True)

        self.tree = ttk.Treeview(body, columns=("port", "state", "owner"),
                                 show="headings", selectmode="browse")
        self.tree.heading("port", text="端口", anchor="center")
        self.tree.heading("state", text="状态", anchor="center")
        self.tree.heading("owner", text="正在占用它的程序", anchor="w")
        self.tree.column("port", width=int(80 * self.k), anchor="center", stretch=False)
        self.tree.column("state", width=int(120 * self.k), anchor="center", stretch=False)
        self.tree.column("owner", width=int(330 * self.k), anchor="w")
        self.tree.tag_configure("busy", foreground="#c0392b")
        self.tree.tag_configure("idle", foreground="#1e7d32")
        self.tree.tag_configure("ok", foreground="#1e7d32")
        self.tree.bind("<Double-1>", lambda _e: self.do_release())

        sb = ttk.Scrollbar(body, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bar = ttk.Frame(wrap)
        bar.pack(fill="x", pady=(8, 0))
        self.btn_refresh = ttk.Button(bar, text="刷新", width=10, command=self.refresh)
        self.btn_release = ttk.Button(bar, text="强制断开选中的口", width=20,
                                      command=self.do_release)
        self.btn_refresh.pack(side="left")
        self.btn_release.pack(side="left", padx=(8, 0))
        tip = ttk.Label(bar, text="　选中被占用的端口 → 断开（也可双击）",
                        foreground="#666666")
        tip.pack(side="left", padx=(10, 0))

        self.msg = ttk.Label(wrap, text="准备就绪", anchor="w")
        self.msg.pack(fill="x", pady=(6, 0))

    # ---------- 工具 ----------
    def say(self, text):
        self.msg.configure(text=text)

    def _selected_port(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0], "values")[0]

    # ---------- 扫描 ----------
    def refresh(self, note=None):
        if self.scanning:
            return
        self.scanning = True
        self.btn_refresh.configure(state="disabled")
        self.say(note or "正在扫描串口…")
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        try:
            ports = list_ports()
            states = {}
            dev_map = {}
            for port, dev in ports:
                states[port] = port_state(port)
                if dev:
                    dev_map[dev] = port
            busy = [p for p in states if states[p] == "busy"]
            owners = find_owners(dev_map) if busy else {}
            self.q.put(("ok", ports, states, owners))
        except Exception as exc:                     # noqa: BLE001
            self.q.put(("err", str(exc)))

    def _poll(self):
        try:
            item = self.q.get_nowait()
        except queue.Empty:
            item = None
        if item:
            self.scanning = False
            self.btn_refresh.configure(state="normal")
            if item[0] == "ok":
                self._apply(item[1], item[2], item[3])
            elif item[0] == "done":
                self.pending = item[1]
                self.refresh(note="正在复查 %s …" % item[1])
            else:
                self.say("扫描出错：%s" % item[1])
        self.root.after(120, self._poll)

    def _apply(self, ports, states, owners):
        keep = self._selected_port()
        self.tree.delete(*self.tree.get_children())
        self.data = {}
        now = time.time()
        for port, dev in ports:
            state = states.get(port, "missing")
            own = owners.get(port, [])
            self.data[port] = {"state": state, "owners": own, "dev": dev}

            if state == "idle":
                tag = "idle"
                text = "空闲"
                if now - self.just_released.get(port, 0) < 8:
                    tag, text = "ok", "空闲 ✓ 已断开"
            elif state == "busy":
                tag, text = "busy", "被占用"
            else:
                tag, text = "", "不存在"

            if own:
                who = "%s (PID %d)" % (own[0]["name"], own[0]["pid"])
                if len(own) > 1:
                    who += " 等 %d 个句柄" % len(own)
            elif state == "busy":
                who = "未查到进程（可能被驱动或系统占用）"
            else:
                who = "—"

            self.tree.insert("", "end", values=(port, text, who), tags=(tag,))

        # 恢复之前的选择
        if keep:
            for iid in self.tree.get_children():
                if self.tree.item(iid, "values")[0] == keep:
                    self.tree.selection_set(iid)
                    break

        n_busy = sum(1 for p in self.data.values() if p["state"] == "busy")
        n = len(self.data)

        # 刚断开过某个口 → 重点汇报它现在到底空不空
        if self.pending:
            port = self.pending
            self.pending = None
            st = (self.data.get(port) or {}).get("state")
            own = (self.data.get(port) or {}).get("owners") or []
            if st == "idle":
                self.say("%s 已断开，现在是空闲的 ✓" % port)
            elif own:
                self.say("%s 又被 %s 打开了（程序可能自动重连）" % (port, own[0]["name"]))
            elif st == "busy":
                self.say("%s 句柄已断开，但端口还没放开：驱动释放有延迟，"
                         "过十几秒再点「刷新」确认；若一直不放开，在设备管理器里禁用/启用该设备" % port)
            else:
                self.say("%s 已经不存在了（设备被拔掉？）" % port)
            return

        if n == 0:
            self.say("没有检测到任何串口。")
        elif n_busy == 0:
            self.say("共 %d 个串口，全部空闲。" % n)
        else:
            self.say("共 %d 个串口，其中 %d 个被占用。" % (n, n_busy))

    # ---------- 断开 ----------
    def do_release(self):
        port = self._selected_port()
        if not port:
            messagebox.showinfo("提示", "请先在列表里选中一个端口。", parent=self.root)
            return
        info = self.data.get(port, {})
        if info.get("state") != "busy":
            messagebox.showinfo("提示", "%s 当前是空闲状态，不需要断开。" % port,
                                parent=self.root)
            return
        own = info.get("owners") or []
        if not own:
            messagebox.showwarning(
                "断不开", "%s 打不开，但没找到是哪个程序占着它。\n\n"
                "通常是驱动或系统服务占用，这种只能重启那个串口设备"
                "（设备管理器里右键 → 禁用 → 启用）。" % port, parent=self.root)
            return

        names = "、".join("%s (PID %d)" % (o["name"], o["pid"]) for o in own)
        if not messagebox.askyesno(
                "确认断开",
                "端口：%s\n占用程序：%s\n\n断开后该程序可能报错或退出，继续吗？" % (port, names),
                parent=self.root):
            return

        fails = []
        for o in own:
            ok, why = release(o["pid"], o["handle"])
            if not ok:
                fails.append("%s：%s" % (o["name"], why))
        if fails:
            self.say("断开失败 —— " + "；".join(fails))
            if not is_admin():
                self.say("断开失败：权限不足，请右键「以管理员身份运行」再试。")
            return

        self.just_released[port] = time.time()
        self.pending = port
        self.say("已断开 %s，正在复查是否空闲…" % port)
        threading.Thread(target=self._verify, args=(port,), daemon=True).start()

    def _verify(self, port):
        """驱动真正放掉端口有几拍延迟，所以盯最多 5 秒，而不是只查一次"""
        for _ in range(14):
            time.sleep(0.35)
            if port_state(port) == "idle":
                break
        self.q.put(("done", port))


def main():
    enable_dpi_aware()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
