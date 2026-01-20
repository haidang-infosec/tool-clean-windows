import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass, asdict
from datetime import datetime
from tkinter import ttk, messagebox, filedialog
import winreg


# =========================
# Core helpers
# =========================


APP_NAME = "Startup Manager (ToolCleanWindows)"
APP_VENDOR_KEY = r"Software\ToolCleanWindows\StartupManager"
DISABLED_STORE_KEY = APP_VENDOR_KEY + r"\Disabled"


def now_ts():
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


@dataclass
class StartupItem:
    id: str
    category: str  # Registry | StartupFolder | ScheduledTask | Service
    scope: str     # HKCU | HKLM | USER | ALL | SYSTEM
    name: str
    status: str    # Enabled | Disabled
    command: str   # command/target/path
    location: str  # registry path / file path / task path / service name
    detail: dict


def run_cmd(cmd: str, timeout: int = 120) -> tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return p.returncode, p.stdout, p.stderr


def run_powershell(ps: str, timeout: int = 120) -> tuple[int, str, str]:
    cmd = f'powershell -NoProfile -ExecutionPolicy Bypass -Command "{ps}"'
    return run_cmd(cmd, timeout=timeout)


def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)


def backups_dir():
    d = os.path.join(os.path.dirname(__file__), "backups")
    ensure_dir(d)
    return d


def disabled_store_set(item: StartupItem):
    """Store a disabled item record in HKCU under ToolCleanWindows."""
    data = json.dumps(asdict(item), ensure_ascii=False)
    k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, DISABLED_STORE_KEY)
    winreg.SetValueEx(k, item.id, 0, winreg.REG_SZ, data)
    winreg.CloseKey(k)


def disabled_store_delete(item_id: str):
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, DISABLED_STORE_KEY, 0, winreg.KEY_SET_VALUE)
        winreg.DeleteValue(k, item_id)
        winreg.CloseKey(k)
    except Exception:
        pass


def disabled_store_list() -> list[StartupItem]:
    items: list[StartupItem] = []
    try:
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, DISABLED_STORE_KEY, 0, winreg.KEY_READ)
    except Exception:
        return items

    i = 0
    while True:
        try:
            name, val, _t = winreg.EnumValue(k, i)
            try:
                d = json.loads(val)
                items.append(StartupItem(**d))
            except Exception:
                pass
            i += 1
        except OSError:
            break

    winreg.CloseKey(k)
    return items


def reg_hive_name(h):
    if h == winreg.HKEY_CURRENT_USER:
        return "HKCU"
    if h == winreg.HKEY_LOCAL_MACHINE:
        return "HKLM"
    return "HIVE"


def list_registry_run_items() -> list[StartupItem]:
    """
    Enumerate common startup registry locations (Run/RunOnce + policies).
    Notes:
    - Disable/Enable is handled safely by moving to our disabled store (HKCU only store).
    """
    paths = [
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", "Run"),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "RunOnce"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Run", "Run"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "RunOnce"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "Run(32)"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce", "RunOnce(32)"),
        # Policies
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run", "PoliciesRun"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer\Run", "PoliciesRun"),
    ]
    items: list[StartupItem] = []

    for hive, subkey, bucket in paths:
        try:
            k = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ)
        except Exception:
            continue

        idx = 0
        while True:
            try:
                name, value, vtype = winreg.EnumValue(k, idx)
                idx += 1
                item_id = f"reg:{reg_hive_name(hive)}:{subkey}:{name}"
                items.append(
                    StartupItem(
                        id=item_id,
                        category="Registry",
                        scope=reg_hive_name(hive),
                        name=name,
                        status="Enabled",
                        command=str(value),
                        location=subkey,
                        detail={"bucket": bucket, "value_type": vtype},
                    )
                )
            except OSError:
                break
        winreg.CloseKey(k)

    # Apply disabled overlay from our store
    disabled = {it.id: it for it in disabled_store_list() if it.category == "Registry"}
    out: list[StartupItem] = []
    for it in items:
        if it.id in disabled:
            d = disabled[it.id]
            it.status = "Disabled"
            it.detail["disabled_by_tool"] = True
            it.detail["disabled_at"] = d.detail.get("disabled_at")
        out.append(it)
    # Also include disabled registry items that no longer exist in Run (still show for re-enable)
    existing_ids = {it.id for it in items}
    for d in disabled.values():
        if d.id not in existing_ids:
            out.append(d)
    return out


def startup_folders():
    user = os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup")
    all_users = os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs\Startup")
    return [("USER", user), ("ALL", all_users)]


def list_startup_folder_items() -> list[StartupItem]:
    exts = {".lnk", ".url", ".bat", ".cmd", ".ps1", ".exe", ".vbs"}
    items: list[StartupItem] = []
    for scope, folder in startup_folders():
        if not folder or not os.path.isdir(folder):
            continue
        for fn in os.listdir(folder):
            p = os.path.join(folder, fn)
            if not os.path.isfile(p):
                continue
            if os.path.splitext(fn)[1].lower() not in exts:
                continue
            item_id = f"file:{scope}:{p}"
            items.append(
                StartupItem(
                    id=item_id,
                    category="StartupFolder",
                    scope=scope,
                    name=fn,
                    status="Enabled",
                    command=p,
                    location=folder,
                    detail={"file": p},
                )
            )

    # disabled overlay (we disable by moving into __disabled folder)
    disabled = {it.id: it for it in disabled_store_list() if it.category == "StartupFolder"}
    out: list[StartupItem] = []
    for it in items:
        if it.id in disabled:
            it.status = "Disabled"
            it.detail["disabled_by_tool"] = True
        out.append(it)
    existing_ids = {it.id for it in items}
    for d in disabled.values():
        if d.id not in existing_ids:
            out.append(d)
    return out


def list_scheduled_startup_tasks() -> list[StartupItem]:
    """
    Use PowerShell to find tasks likely to run at logon/startup.
    This is 'best effort' but covers most real-world cases.
    """
    ps = r"""
$tasks = Get-ScheduledTask | ForEach-Object {
  $t = $_
  $tr = $t.Triggers | ForEach-Object {
    $_.TriggerType
  }
  $ac = $t.Actions | ForEach-Object {
    if ($_.Execute) { ($_.Execute + " " + $_.Arguments).Trim() } else { "" }
  }
  [PSCustomObject]@{
    TaskName=$t.TaskName
    TaskPath=$t.TaskPath
    State=($t.State.ToString())
    Triggers=($tr -join ",")
    Actions=($ac -join " | ")
  }
}
$tasks | ConvertTo-Json -Depth 6
"""
    rc, out, err = run_powershell(ps, timeout=180)
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []

    if isinstance(data, dict):
        data = [data]

    items: list[StartupItem] = []
    for t in data:
        triggers = (t.get("Triggers") or "").lower()
        # common trigger types: Logon, Boot, Startup
        if not any(k in triggers for k in ["logon", "boot", "startup"]):
            continue
        name = f"{t.get('TaskPath','')}{t.get('TaskName','')}"
        item_id = f"task:{name}"
        state = (t.get("State") or "").lower()
        status = "Disabled" if "disabled" in state else "Enabled"
        items.append(
            StartupItem(
                id=item_id,
                category="ScheduledTask",
                scope="SYSTEM",
                name=name,
                status=status,
                command=t.get("Actions") or "",
                location=name,
                detail={"triggers": t.get("Triggers") or "", "state": t.get("State") or ""},
            )
        )
    return items


def list_auto_services() -> list[StartupItem]:
    """
    Auto-start services are effectively startup items.
    """
    ps = r"""
$svcs = Get-CimInstance Win32_Service | Select-Object Name, DisplayName, StartMode, State, PathName
$svcs | ConvertTo-Json -Depth 4
"""
    rc, out, err = run_powershell(ps, timeout=180)
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except Exception:
        return []
    if isinstance(data, dict):
        data = [data]

    items: list[StartupItem] = []
    for s in data:
        start_mode = (s.get("StartMode") or "").lower()
        if start_mode not in ["auto"]:
            continue
        name = s.get("Name") or ""
        disp = s.get("DisplayName") or name
        item_id = f"svc:{name}"
        state = (s.get("State") or "").lower()
        status = "Enabled"  # auto-start means enabled; disabling changes start mode
        items.append(
            StartupItem(
                id=item_id,
                category="Service",
                scope="SYSTEM",
                name=disp,
                status=status,
                command=s.get("PathName") or "",
                location=name,
                detail={"service_name": name, "state": state, "start_mode": s.get("StartMode") or ""},
            )
        )
    return items


def create_shortcut_lnk(dst_lnk: str, target: str, args: str = "", start_in: str = "", icon: str = ""):
    # Use PowerShell COM WScript.Shell to create shortcut
    ps = (
        "$W=New-Object -ComObject WScript.Shell; "
        f"$S=$W.CreateShortcut('{dst_lnk}'); "
        f"$S.TargetPath='{target}'; "
        f"$S.Arguments='{args}'; "
        + (f"$S.WorkingDirectory='{start_in}'; " if start_in else "")
        + (f"$S.IconLocation='{icon}'; " if icon else "")
        + "$S.Save();"
    )
    rc, out, err = run_powershell(ps, timeout=60)
    if rc != 0:
        raise RuntimeError(err or out or "Failed to create shortcut")


def snapshot(items: list[StartupItem], reason: str) -> str:
    p = os.path.join(backups_dir(), f"startup_snapshot_{now_ts()}_{reason}.json")
    data = {
        "generated_at": datetime.now().isoformat(),
        "reason": reason,
        "is_admin": is_admin(),
        "items": [asdict(i) for i in items],
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p


# =========================
# GUI
# =========================


class StartupManagerUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(APP_NAME)
        root.geometry("1200x750")

        self.items: list[StartupItem] = []
        self.filtered: list[StartupItem] = []
        self.is_busy = False
        self.cancel = threading.Event()

        top = ttk.Frame(root)
        top.pack(fill="x", padx=8, pady=8)

        self.refresh_btn = ttk.Button(top, text="🔄 Refresh", command=self.refresh)
        self.refresh_btn.pack(side="left", padx=3)

        self.stop_btn = ttk.Button(top, text="⏹ Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=3)

        self.add_btn = ttk.Button(top, text="➕ Add", command=self.add_item)
        self.add_btn.pack(side="left", padx=12)

        self.enable_btn = ttk.Button(top, text="✅ Enable", command=lambda: self.toggle_selected(True))
        self.enable_btn.pack(side="left", padx=3)

        self.disable_btn = ttk.Button(top, text="⛔ Disable", command=lambda: self.toggle_selected(False))
        self.disable_btn.pack(side="left", padx=3)

        self.delete_btn = ttk.Button(top, text="🗑 Delete", command=self.delete_selected)
        self.delete_btn.pack(side="left", padx=3)

        self.export_btn = ttk.Button(top, text="💾 Export Snapshot", command=self.export_snapshot)
        self.export_btn.pack(side="right", padx=3)

        self.restore_btn = ttk.Button(top, text="♻ Restore Snapshot", command=self.restore_snapshot)
        self.restore_btn.pack(side="right", padx=3)

        # Search / filter
        filt = ttk.LabelFrame(root, text="Search / Filter", padding=6)
        filt.pack(fill="x", padx=8, pady=(0, 8))

        self.search_var = tk.StringVar()
        self.search_var.trace("w", lambda *_: self.apply_filter())
        ttk.Label(filt, text="Search:").pack(side="left")
        ttk.Entry(filt, textvariable=self.search_var, width=40).pack(side="left", padx=6)

        self.cat_var = tk.StringVar(value="All")
        ttk.Label(filt, text="Category:").pack(side="left", padx=(12, 0))
        ttk.Combobox(
            filt,
            textvariable=self.cat_var,
            values=["All", "Registry", "StartupFolder", "ScheduledTask", "Service"],
            width=16,
            state="readonly",
        ).pack(side="left", padx=6)
        self.cat_var.trace("w", lambda *_: self.apply_filter())

        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(filt, textvariable=self.status_var, foreground="gray").pack(side="right")

        # Main split: table + details
        main = ttk.Frame(root)
        main.pack(fill="both", expand=True, padx=8, pady=8)

        left = ttk.Frame(main)
        left.pack(side="left", fill="both", expand=True)

        cols = ("category", "scope", "name", "status", "command", "location")
        self.tree = ttk.Treeview(left, columns=cols, show="headings", selectmode="extended")
        for c in cols:
            self.tree.heading(c, text=c, command=lambda cc=c: self.sort_by(cc))
            self.tree.column(c, width=150 if c not in ("command", "location") else 320, anchor="w")
        vs = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(left, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")

        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())
        self.tree.bind("<Double-1>", lambda e: self.open_location())

        right = ttk.LabelFrame(main, text="Details", padding=8)
        right.pack(side="right", fill="y")

        self.detail_box = tk.Text(right, width=55, height=30, font=("Consolas", 9))
        self.detail_box.pack(fill="both", expand=True)

        det_btns = ttk.Frame(right)
        det_btns.pack(fill="x", pady=(8, 0))
        ttk.Button(det_btns, text="📂 Open location", command=self.open_location).pack(side="left")
        ttk.Button(det_btns, text="📋 Copy command", command=self.copy_command).pack(side="left", padx=6)

        # Log
        log_frame = ttk.LabelFrame(root, text="Log", padding=6)
        log_frame.pack(fill="x", padx=8, pady=(0, 8))
        self.log_box = tk.Text(log_frame, height=7, font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)

        self.sort_state = {"col": "category", "rev": False}

        self.log(f"Admin: {is_admin()}")
        self.refresh()

    # ---- UI utils ----

    def log(self, msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        self.log_box.insert(tk.END, line + "\n")
        self.log_box.see(tk.END)

    def set_busy(self, busy: bool, status: str = None):
        self.is_busy = busy
        self.refresh_btn.config(state="disabled" if busy else "normal")
        self.stop_btn.config(state="normal" if busy else "disabled")
        self.add_btn.config(state="disabled" if busy else "normal")
        self.enable_btn.config(state="disabled" if busy else "normal")
        self.disable_btn.config(state="disabled" if busy else "normal")
        self.delete_btn.config(state="disabled" if busy else "normal")
        if status is not None:
            self.status_var.set(status)

    def stop(self):
        if not self.is_busy:
            return
        self.cancel.set()
        self.status_var.set("Stopping...")
        self.log("Stop requested.")

    # ---- data load ----

    def refresh(self):
        if self.is_busy:
            return
        self.cancel.clear()
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        self.root.after(0, lambda: self.set_busy(True, "Scanning startup sources..."))
        try:
            items: list[StartupItem] = []
            # Registry + our disabled overlay
            items.extend(list_registry_run_items())
            if self.cancel.is_set():
                return
            items.extend(list_startup_folder_items())
            if self.cancel.is_set():
                return
            items.extend(list_scheduled_startup_tasks())
            if self.cancel.is_set():
                return
            items.extend(list_auto_services())
            if self.cancel.is_set():
                return

            # de-dupe by id
            uniq = {}
            for it in items:
                uniq[it.id] = it
            items = list(uniq.values())
            items.sort(key=lambda x: (x.category, x.name.lower()))

            self.items = items
            self.root.after(0, self.apply_filter)
            self.root.after(0, lambda: self.log(f"Loaded {len(items)} items."))
            self.root.after(0, lambda: self.set_busy(False, f"Ready - {len(items)} items"))
        except Exception as e:
            self.root.after(0, lambda: self.set_busy(False, "Error"))
            self.root.after(0, lambda: messagebox.showerror("Error", str(e)))

    def apply_filter(self):
        q = (self.search_var.get() or "").lower().strip()
        cat = self.cat_var.get()
        out = []
        for it in self.items:
            if cat != "All" and it.category != cat:
                continue
            if q:
                blob = f"{it.category} {it.scope} {it.name} {it.status} {it.command} {it.location}".lower()
                if q not in blob:
                    continue
            out.append(it)
        self.filtered = out
        self.render_table()

    def render_table(self):
        self.tree.delete(*self.tree.get_children())
        for it in self.filtered:
            self.tree.insert(
                "",
                "end",
                iid=it.id,
                values=(it.category, it.scope, it.name, it.status, it.command, it.location),
            )
        self.status_var.set(f"Showing {len(self.filtered)}/{len(self.items)}")
        self.show_details()

    def sort_by(self, col: str):
        rev = self.sort_state["rev"] if self.sort_state["col"] == col else False
        rev = not rev
        self.sort_state = {"col": col, "rev": rev}
        idx = {"category": 0, "scope": 1, "name": 2, "status": 3, "command": 4, "location": 5}[col]
        self.filtered.sort(key=lambda x: (x.category, x.scope, x.name.lower(), x.status, x.command, x.location)[idx], reverse=rev)
        self.render_table()

    def get_selected_items(self) -> list[StartupItem]:
        ids = list(self.tree.selection())
        by_id = {it.id: it for it in self.items}
        return [by_id[i] for i in ids if i in by_id]

    def show_details(self):
        sel = self.get_selected_items()
        self.detail_box.delete("1.0", tk.END)
        if not sel:
            self.detail_box.insert(tk.END, "No selection.\n")
            return
        it = sel[0]
        d = asdict(it)
        self.detail_box.insert(tk.END, json.dumps(d, ensure_ascii=False, indent=2))

    def copy_command(self):
        sel = self.get_selected_items()
        if not sel:
            return
        cmd = sel[0].command
        self.root.clipboard_clear()
        self.root.clipboard_append(cmd)
        self.log("Copied command to clipboard.")

    def open_location(self):
        sel = self.get_selected_items()
        if not sel:
            return
        it = sel[0]
        try:
            if it.category == "StartupFolder":
                p = it.command
                folder = os.path.dirname(p)
                subprocess.Popen(f'explorer "{folder}"')
            elif it.category == "Registry":
                messagebox.showinfo("Info", "Registry item. Location is registry path (open regedit manually).")
            elif it.category == "ScheduledTask":
                subprocess.Popen("taskschd.msc", shell=True)
            elif it.category == "Service":
                subprocess.Popen("services.msc", shell=True)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ---- operations ----

    def export_snapshot(self):
        if not self.items:
            return
        path = filedialog.asksaveasfilename(
            title="Save snapshot",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialfile=f"startup_snapshot_{now_ts()}.json",
        )
        if not path:
            return
        data = {"generated_at": datetime.now().isoformat(), "items": [asdict(i) for i in self.items]}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.log(f"Snapshot saved: {path}")

    def restore_snapshot(self):
        path = filedialog.askopenfilename(
            title="Select snapshot",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        messagebox.showinfo(
            "Restore snapshot",
            "Restore is intentionally conservative.\n\n"
            "Tool sẽ chỉ restore các mục đã bị disable bởi tool (từ Disabled Store).\n"
            "Không tự động 'xóa' những mục mới phát sinh.",
        )
        try:
            with open(path, "r", encoding="utf-8") as f:
                snap = json.load(f)
            disabled_ids = [it.id for it in disabled_store_list()]
            # try enabling all disabled by tool
            enabled = 0
            for it_id in disabled_ids:
                # find record in disabled store
                for it in disabled_store_list():
                    if it.id == it_id:
                        if self._enable_item(it, confirm=False):
                            enabled += 1
            self.log(f"Restored: enabled {enabled} items from Disabled Store.")
            self.refresh()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def add_item(self):
        if self.is_busy:
            return

        win = tk.Toplevel(self.root)
        win.title("Add Startup Item")
        win.geometry("600x380")
        win.transient(self.root)
        win.grab_set()

        kind = tk.StringVar(value="Registry (HKCU Run)")
        name = tk.StringVar(value="")
        cmd = tk.StringVar(value="")
        args = tk.StringVar(value="")
        start_in = tk.StringVar(value="")

        frm = ttk.Frame(win, padding=10)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Type:").grid(row=0, column=0, sticky="w")
        ttk.Combobox(frm, textvariable=kind, state="readonly", values=[
            "Registry (HKCU Run)",
            "Startup Folder (User)",
        ]).grid(row=0, column=1, sticky="ew", pady=3)

        ttk.Label(frm, text="Name:").grid(row=1, column=0, sticky="w")
        ttk.Entry(frm, textvariable=name).grid(row=1, column=1, sticky="ew", pady=3)

        ttk.Label(frm, text="Target / Command:").grid(row=2, column=0, sticky="w")
        ttk.Entry(frm, textvariable=cmd).grid(row=2, column=1, sticky="ew", pady=3)

        ttk.Label(frm, text="Arguments (optional):").grid(row=3, column=0, sticky="w")
        ttk.Entry(frm, textvariable=args).grid(row=3, column=1, sticky="ew", pady=3)

        ttk.Label(frm, text="Working dir (optional):").grid(row=4, column=0, sticky="w")
        ttk.Entry(frm, textvariable=start_in).grid(row=4, column=1, sticky="ew", pady=3)

        frm.grid_columnconfigure(1, weight=1)

        info = ttk.Label(
            frm,
            text="Lưu ý: Startup Folder sẽ tạo shortcut .lnk.\nRegistry sẽ ghi vào HKCU\\...\\Run (chạy cho user hiện tại).",
            foreground="gray",
        )
        info.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 0))

        def on_add():
            n = name.get().strip()
            c = cmd.get().strip()
            if not n or not c:
                messagebox.showwarning("Missing", "Name và Command không được trống.")
                return
            try:
                if kind.get().startswith("Registry"):
                    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
                    k = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
                    value = c if not args.get().strip() else f"\"{c}\" {args.get().strip()}"
                    winreg.SetValueEx(k, n, 0, winreg.REG_SZ, value)
                    winreg.CloseKey(k)
                    self.log(f"Added registry startup: {n}")
                else:
                    folder = startup_folders()[0][1]
                    ensure_dir(folder)
                    dst = os.path.join(folder, f"{n}.lnk")
                    create_shortcut_lnk(dst, c, args=args.get().strip(), start_in=start_in.get().strip())
                    self.log(f"Added startup folder shortcut: {dst}")
                win.destroy()
                self.refresh()
            except Exception as e:
                messagebox.showerror("Error", str(e))

        btns = ttk.Frame(frm)
        btns.grid(row=6, column=0, columnspan=2, sticky="e", pady=(12, 0))
        ttk.Button(btns, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(btns, text="Add", command=on_add).pack(side="right", padx=8)

        win.wait_window()

    def toggle_selected(self, enable: bool):
        if self.is_busy:
            return
        sel = self.get_selected_items()
        if not sel:
            return

        action = "Enable" if enable else "Disable"
        if not messagebox.askyesno(
            "Confirm",
            f"{action} {len(sel)} selected item(s)?\n\n"
            "Tip: Với Scheduled Tasks/Services có thể cần quyền Administrator.",
        ):
            return
        snapshot(self.items, f"before_{action.lower()}")

        ok_count = 0
        for it in sel:
            if enable:
                ok_count += 1 if self._enable_item(it) else 0
            else:
                ok_count += 1 if self._disable_item(it) else 0
        self.log(f"{action} done: {ok_count}/{len(sel)}")
        self.refresh()

    def delete_selected(self):
        if self.is_busy:
            return
        sel = self.get_selected_items()
        if not sel:
            return

        # High-risk warning
        if not messagebox.askyesno(
            "Confirm Delete (High risk)",
            f"Delete {len(sel)} item(s) permanently?\n\n"
            "⚠ Cảnh báo: Xóa startup item có thể làm app không tự chạy.\n"
            "Tool sẽ tạo snapshot trước khi xóa.",
        ):
            return
        snapshot(self.items, "before_delete")

        ok_count = 0
        for it in sel:
            ok_count += 1 if self._delete_item(it) else 0
        self.log(f"Delete done: {ok_count}/{len(sel)}")
        self.refresh()

    # ---- per-type implementations ----

    def _disable_item(self, it: StartupItem) -> bool:
        try:
            if it.category == "Registry":
                return self._disable_registry(it)
            if it.category == "StartupFolder":
                return self._disable_startup_folder(it)
            if it.category == "ScheduledTask":
                return self._disable_task(it)
            if it.category == "Service":
                return self._disable_service(it)
            return False
        except Exception as e:
            self.log(f"Disable failed: {it.name} ({e})")
            return False

    def _enable_item(self, it: StartupItem, confirm: bool = True) -> bool:
        try:
            if it.category == "Registry":
                return self._enable_registry(it)
            if it.category == "StartupFolder":
                return self._enable_startup_folder(it)
            if it.category == "ScheduledTask":
                return self._enable_task(it)
            if it.category == "Service":
                return self._enable_service(it)
            return False
        except Exception as e:
            self.log(f"Enable failed: {it.name} ({e})")
            return False

    def _delete_item(self, it: StartupItem) -> bool:
        try:
            if it.category == "Registry":
                return self._delete_registry(it)
            if it.category == "StartupFolder":
                return self._delete_startup_folder(it)
            if it.category == "ScheduledTask":
                return self._delete_task(it)
            if it.category == "Service":
                return self._delete_service(it)
            return False
        except Exception as e:
            self.log(f"Delete failed: {it.name} ({e})")
            return False

    # Registry (disable by moving to our disabled store + removing value)
    def _parse_registry_id(self, it: StartupItem):
        # reg:HKCU:<subkey>:<name>
        parts = it.id.split(":", 3)
        if len(parts) != 4:
            raise ValueError("Bad registry id")
        _, hive_name, subkey, value_name = parts
        hive = winreg.HKEY_CURRENT_USER if hive_name == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        return hive, subkey, value_name

    def _disable_registry(self, it: StartupItem) -> bool:
        hive, subkey, value_name = self._parse_registry_id(it)
        # read value
        k = winreg.OpenKey(hive, subkey, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE)
        val, vtype = winreg.QueryValueEx(k, value_name)
        # delete from real key
        winreg.DeleteValue(k, value_name)
        winreg.CloseKey(k)
        # store record for restore
        it2 = StartupItem(
            id=it.id,
            category="Registry",
            scope=it.scope,
            name=it.name,
            status="Disabled",
            command=str(val),
            location=it.location,
            detail={"disabled_at": datetime.now().isoformat(), "origin_type": "registry", "vtype": int(vtype)},
        )
        disabled_store_set(it2)
        self.log(f"Disabled registry: {it.name}")
        return True

    def _enable_registry(self, it: StartupItem) -> bool:
        # restore from disabled store, if present
        hive, subkey, value_name = self._parse_registry_id(it)
        # locate stored record
        rec = None
        for d in disabled_store_list():
            if d.id == it.id:
                rec = d
                break
        if not rec:
            self.log(f"No disabled record found for: {it.name}")
            return False
        vtype = int(rec.detail.get("vtype", winreg.REG_SZ))
        k = winreg.CreateKey(hive, subkey)
        winreg.SetValueEx(k, value_name, 0, vtype, rec.command)
        winreg.CloseKey(k)
        disabled_store_delete(it.id)
        self.log(f"Enabled registry: {it.name}")
        return True

    def _delete_registry(self, it: StartupItem) -> bool:
        # If disabled by tool, delete record; also try delete actual value.
        try:
            hive, subkey, value_name = self._parse_registry_id(it)
            k = winreg.OpenKey(hive, subkey, 0, winreg.KEY_SET_VALUE)
            try:
                winreg.DeleteValue(k, value_name)
            except Exception:
                pass
            winreg.CloseKey(k)
        except Exception:
            pass
        disabled_store_delete(it.id)
        self.log(f"Deleted registry startup: {it.name}")
        return True

    # Startup folder (disable by moving to __disabled folder and storing record)
    def _disable_startup_folder(self, it: StartupItem) -> bool:
        p = it.command
        if not os.path.exists(p):
            # already missing -> just store disabled record
            it2 = StartupItem(**asdict(it))
            it2.status = "Disabled"
            it2.detail["disabled_at"] = datetime.now().isoformat()
            disabled_store_set(it2)
            return True
        base = os.path.dirname(p)
        disabled_dir = os.path.join(base, "__disabled_startup")
        ensure_dir(disabled_dir)
        dst = os.path.join(disabled_dir, os.path.basename(p))
        shutil.move(p, dst)
        it2 = StartupItem(**asdict(it))
        it2.status = "Disabled"
        it2.command = dst
        it2.detail["original_path"] = p
        it2.detail["disabled_at"] = datetime.now().isoformat()
        disabled_store_set(it2)
        self.log(f"Disabled startup folder item: {it.name}")
        return True

    def _enable_startup_folder(self, it: StartupItem) -> bool:
        rec = None
        for d in disabled_store_list():
            if d.id == it.id:
                rec = d
                break
        if not rec:
            self.log("No disabled record.")
            return False
        src = rec.command
        dst = rec.detail.get("original_path")
        if not dst:
            return False
        ensure_dir(os.path.dirname(dst))
        if os.path.exists(src):
            shutil.move(src, dst)
        disabled_store_delete(it.id)
        self.log(f"Enabled startup folder item: {it.name}")
        return True

    def _delete_startup_folder(self, it: StartupItem) -> bool:
        p = it.command
        try:
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
        # if disabled record exists, delete that file too
        for d in disabled_store_list():
            if d.id == it.id and d.category == "StartupFolder":
                try:
                    if os.path.exists(d.command):
                        os.remove(d.command)
                except Exception:
                    pass
        disabled_store_delete(it.id)
        self.log(f"Deleted startup folder item: {it.name}")
        return True

    # Scheduled task
    def _disable_task(self, it: StartupItem) -> bool:
        # location is full task path+name
        name = it.location
        rc, out, err = run_cmd(f'schtasks /Change /TN "{name}" /DISABLE', timeout=120)
        if rc != 0:
            raise RuntimeError(err or out)
        self.log(f"Disabled task: {name}")
        return True

    def _enable_task(self, it: StartupItem) -> bool:
        name = it.location
        rc, out, err = run_cmd(f'schtasks /Change /TN "{name}" /ENABLE', timeout=120)
        if rc != 0:
            raise RuntimeError(err or out)
        self.log(f"Enabled task: {name}")
        return True

    def _delete_task(self, it: StartupItem) -> bool:
        name = it.location
        rc, out, err = run_cmd(f'schtasks /Delete /TN "{name}" /F', timeout=120)
        if rc != 0:
            raise RuntimeError(err or out)
        self.log(f"Deleted task: {name}")
        return True

    # Service
    def _disable_service(self, it: StartupItem) -> bool:
        svc = it.detail.get("service_name") or it.location
        if not is_admin():
            raise RuntimeError("Administrator permission required to change service start mode.")
        rc, out, err = run_cmd(f'sc config "{svc}" start= disabled', timeout=120)
        if rc != 0:
            raise RuntimeError(err or out)
        self.log(f"Disabled service: {svc}")
        return True

    def _enable_service(self, it: StartupItem) -> bool:
        svc = it.detail.get("service_name") or it.location
        if not is_admin():
            raise RuntimeError("Administrator permission required to change service start mode.")
        rc, out, err = run_cmd(f'sc config "{svc}" start= auto', timeout=120)
        if rc != 0:
            raise RuntimeError(err or out)
        self.log(f"Enabled service: {svc}")
        return True

    def _delete_service(self, it: StartupItem) -> bool:
        svc = it.detail.get("service_name") or it.location
        if not is_admin():
            raise RuntimeError("Administrator permission required to delete a service.")
        # Extra confirm
        if not messagebox.askyesno(
            "Dangerous",
            f"Delete Windows Service '{svc}'?\n\n"
            "⚠ Đây là hành động RẤT nguy hiểm. Có thể làm Windows/app hỏng.\n"
            "Khuyến nghị: Disable thay vì Delete.\n\n"
            "Bạn chắc chắn?",
        ):
            return False
        rc, out, err = run_cmd(f'sc delete "{svc}"', timeout=120)
        if rc != 0:
            raise RuntimeError(err or out)
        self.log(f"Deleted service: {svc}")
        return True


def main():
    if os.name != "nt":
        raise SystemExit("This tool is for Windows only.")
    root = tk.Tk()
    app = StartupManagerUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()

