import os
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import winreg
import shutil
from pathlib import Path
import re

# ---------------- CORE ----------------

_STOPWORDS = {
    "the", "and", "for", "with", "app", "apps", "application", "software",
    "inc", "ltd", "llc", "co", "corp", "corporation", "limited", "gmbh",
    "company", "studio", "team", "version", "setup", "installer"
}


def bytes_to_human(size):
    """Convert bytes to human readable format"""
    if size == 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def _tokenize(text: str):
    if not text:
        return []
    parts = re.split(r"[^a-zA-Z0-9]+", text.lower())
    tokens = []
    for p in parts:
        if len(p) < 3:
            continue
        if p in _STOPWORDS:
            continue
        tokens.append(p)
    return tokens


def _score_folder_match(folder_name: str, app_name: str, publisher: str, install_location: str):
    """
    Return (score:int, reasons:[str]) for how likely folder_name belongs to app.
    This is intentionally conservative to avoid deleting wrong folders.
    """
    reasons = []
    score = 0

    fn = (folder_name or "").strip()
    fn_low = fn.lower()
    app_low = (app_name or "").strip().lower()

    if not fn_low:
        return 0, reasons

    # Strong exact-ish matches
    if app_low and fn_low == app_low:
        return 100, ["folder == app name"]

    # Token signals
    app_tokens = set(_tokenize(app_name))
    pub_tokens = set(_tokenize(publisher))
    install_base = os.path.basename(install_location or "")
    install_tokens = set(_tokenize(install_base))

    folder_tokens = set(_tokenize(fn))

    overlap_app = len(app_tokens & folder_tokens)
    overlap_pub = len(pub_tokens & folder_tokens)
    overlap_install = len(install_tokens & folder_tokens)

    if overlap_app:
        score += overlap_app * 6
        reasons.append(f"app tokens overlap: {overlap_app}")

    if overlap_pub:
        score += overlap_pub * 3
        reasons.append(f"publisher tokens overlap: {overlap_pub}")

    if overlap_install:
        score += overlap_install * 4
        reasons.append(f"install-folder tokens overlap: {overlap_install}")

    # Substring only as weak signal (dangerous)
    if app_low and (app_low in fn_low or fn_low in app_low):
        score += 2
        reasons.append("weak substring match")

    return score, reasons


def build_appdata_index(cancel_check=None, progress_cb=None):
    """
    Scan AppData once and build index entries:
      [{folder, path, base, size}]
    This is expensive but done once; reused for per-app mapping and preview.
    """
    base = os.path.expanduser("~")
    roots = [
        os.path.join(base, "AppData", "Local"),
        os.path.join(base, "AppData", "Roaming"),
        os.path.join(base, "AppData", "LocalLow"),
    ]

    entries = []
    total_size = 0
    total_folders = 0

    for root in roots:
        if cancel_check and cancel_check():
            break
        if not os.path.exists(root):
            continue

        try:
            folders = [f for f in os.listdir(
                root) if os.path.isdir(os.path.join(root, f))]
        except:
            folders = []

        total_folders += len(folders)

        for idx, folder in enumerate(folders):
            if cancel_check and cancel_check():
                break
            p = os.path.join(root, folder)
            size = 0
            try:
                size = get_folder_size(p)
            except:
                size = 0
            total_size += size
            entries.append({
                "folder": folder,
                "path": p,
                "base": root,
                "size": size
            })
            if progress_cb and (len(entries) % 30 == 0):
                try:
                    progress_cb(
                        f"Indexing AppData... {len(entries)}/{total_folders} folders")
                except:
                    pass

    return entries, total_size


def find_appdata_candidates(app, appdata_entries=None, cancel_check=None, min_score=8, max_candidates=50):
    """
    Find likely AppData folders for an app. Conservative scoring + threshold.
    Returns list of dicts: {path, size, score, reasons, base}
    """
    app_name = app.get("name", "")
    publisher = app.get("publisher", "")
    install_location = app.get("install_location", "")

    candidates = []
    if appdata_entries is not None:
        # Use pre-built index (fast)
        for e in appdata_entries:
            if cancel_check and cancel_check():
                break
            folder = e.get("folder", "")
            score, reasons = _score_folder_match(
                folder, app_name=app_name, publisher=publisher, install_location=install_location
            )
            if score < min_score:
                continue
            candidates.append({
                "path": e.get("path", ""),
                "size": int(e.get("size", 0) or 0),
                "score": score,
                "reasons": reasons,
                "base": e.get("base", ""),
            })
    else:
        # Fallback: live scan (slower)
        base = os.path.expanduser("~")
        roots = [
            os.path.join(base, "AppData", "Local"),
            os.path.join(base, "AppData", "Roaming"),
            os.path.join(base, "AppData", "LocalLow"),
        ]

        for root in roots:
            if cancel_check and cancel_check():
                break
            if not os.path.exists(root):
                continue
            try:
                for folder in os.listdir(root):
                    if cancel_check and cancel_check():
                        break
                    folder_path = os.path.join(root, folder)
                    if not os.path.isdir(folder_path):
                        continue

                    score, reasons = _score_folder_match(
                        folder, app_name=app_name, publisher=publisher, install_location=install_location
                    )
                    if score < min_score:
                        continue

                    size = 0
                    try:
                        size = get_folder_size(folder_path)
                    except:
                        pass

                    candidates.append({
                        "path": folder_path,
                        "size": size,
                        "score": score,
                        "reasons": reasons,
                        "base": root,
                    })
            except:
                continue

    candidates.sort(key=lambda x: (x["score"], x["size"]), reverse=True)
    return candidates[:max_candidates]


def get_installed_apps():
    """Get all installed applications from registry"""
    apps = []

    # Registry paths to check
    registry_paths = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    for hkey, path in registry_paths:
        try:
            key = winreg.OpenKey(hkey, path)
            i = 0
            while True:
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)

                    try:
                        name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        if name:
                            # Get other info
                            try:
                                version = winreg.QueryValueEx(
                                    subkey, "DisplayVersion")[0]
                            except:
                                version = "Unknown"

                            try:
                                publisher = winreg.QueryValueEx(
                                    subkey, "Publisher")[0]
                            except:
                                publisher = "Unknown"

                            try:
                                install_date = winreg.QueryValueEx(
                                    subkey, "InstallDate")[0]
                                if install_date and len(install_date) == 8:
                                    install_date = f"{install_date[4:6]}/{install_date[6:8]}/{install_date[0:4]}"
                                else:
                                    install_date = "Unknown"
                            except:
                                install_date = "Unknown"

                            try:
                                install_location = winreg.QueryValueEx(
                                    subkey, "InstallLocation")[0]
                                if not install_location or not os.path.exists(install_location):
                                    install_location = ""
                            except:
                                install_location = ""

                            try:
                                uninstall_string = winreg.QueryValueEx(
                                    subkey, "UninstallString")[0]
                            except:
                                uninstall_string = ""

                            try:
                                size_bytes = winreg.QueryValueEx(
                                    subkey, "EstimatedSize")[0]
                                if size_bytes:
                                    # Convert KB to bytes
                                    size_bytes = int(size_bytes) * 1024
                                else:
                                    size_bytes = 0
                            except:
                                size_bytes = 0

                            # Calculate actual size if install location exists
                            if install_location and os.path.exists(install_location):
                                try:
                                    actual_size = get_folder_size(
                                        install_location)
                                    if actual_size > size_bytes:
                                        size_bytes = actual_size
                                except:
                                    pass

                            apps.append({
                                "name": name,
                                "version": version,
                                "publisher": publisher,
                                "install_date": install_date,
                                "install_location": install_location,
                                "uninstall_string": uninstall_string,
                                "size": size_bytes,
                                "registry_key": subkey_name,
                                "reg_hive": hkey,
                                "reg_path": path,
                                "reg_subkey": subkey_name
                            })
                    except:
                        pass

                    winreg.CloseKey(subkey)
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(key)
        except Exception as e:
            pass

    # Remove duplicates (same name and publisher)
    seen = set()
    unique_apps = []
    for app in apps:
        key = (app["name"], app["publisher"])
        if key not in seen:
            seen.add(key)
            unique_apps.append(app)

    return unique_apps


def get_folder_size(path):
    """Calculate folder size"""
    total = 0
    try:
        for root, dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except:
                    pass
    except:
        pass
    return total


def get_appdata_size(app, appdata_entries=None, cancel_check=None):
    """Calculate (conservative) AppData size for an application"""
    total = 0
    try:
        for c in find_appdata_candidates(app, appdata_entries=appdata_entries, cancel_check=cancel_check):
            total += int(c.get("size", 0) or 0)
    except:
        pass
    return total


def uninstall_app(uninstall_string, app_name):
    """Uninstall application using uninstall string"""
    try:
        # Parse uninstall string
        if uninstall_string.startswith('"'):
            # Extract executable path
            end_quote = uninstall_string.find('"', 1)
            exe_path = uninstall_string[1:end_quote]
            args = uninstall_string[end_quote + 1:].strip()
        else:
            parts = uninstall_string.split(' ', 1)
            exe_path = parts[0]
            args = parts[1] if len(parts) > 1 else ""

        # Add silent uninstall flags if possible
        if "msiexec" in exe_path.lower():
            if "/I" in args:
                args = args.replace("/I", "/X")
            if "/quiet" not in args.lower() and "/qn" not in args.lower():
                args = "/quiet /norestart " + args
        elif "uninstall" not in args.lower() and "silent" not in args.lower():
            # Try common silent flags
            if "/S" not in args:
                args = "/S " + args
            elif "/silent" not in args.lower():
                args = "/silent " + args

        # Execute uninstall
        cmd = f'"{exe_path}" {args}'
        result = subprocess.run(
            cmd, shell=True, capture_output=True, timeout=300)
        return result.returncode == 0, result.stdout.decode() + result.stderr.decode()
    except Exception as e:
        return False, str(e)


def cleanup_leftovers(app_name, install_location):
    """Clean up leftover files and registry entries"""
    cleaned = []

    # Clean install location if exists
    if install_location and os.path.exists(install_location):
        try:
            shutil.rmtree(install_location)
            cleaned.append(f"Removed folder: {install_location}")
        except Exception as e:
            cleaned.append(f"Could not remove folder: {e}")

    # Clean AppData folders
    base = os.path.expanduser("~")
    appdata_paths = [
        os.path.join(base, "AppData", "Local"),
        os.path.join(base, "AppData", "Roaming"),
        os.path.join(base, "AppData", "LocalLow"),
    ]

    for appdata_path in appdata_paths:
        if os.path.exists(appdata_path):
            for folder in os.listdir(appdata_path):
                folder_path = os.path.join(appdata_path, folder)
                if os.path.isdir(folder_path):
                    # Check if folder name contains app name
                    if app_name.lower() in folder.lower():
                        try:
                            shutil.rmtree(folder_path)
                            cleaned.append(f"Removed AppData: {folder_path}")
                        except:
                            pass

    # Clean registry entries
    registry_paths = [
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE,
         r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER,
         r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]

    for hkey, path in registry_paths:
        try:
            key = winreg.OpenKey(hkey, path, 0, winreg.KEY_ALL_ACCESS)
            i = 0
            to_delete = []
            while True:
                try:
                    subkey_name = winreg.EnumKey(key, i)
                    subkey = winreg.OpenKey(key, subkey_name)
                    try:
                        name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                        if app_name.lower() in name.lower():
                            to_delete.append(subkey_name)
                    except:
                        pass
                    winreg.CloseKey(subkey)
                    i += 1
                except OSError:
                    break

            for subkey_name in to_delete:
                try:
                    winreg.DeleteKey(key, subkey_name)
                    cleaned.append(f"Removed registry: {subkey_name}")
                except:
                    pass

            winreg.CloseKey(key)
        except Exception as e:
            pass

    return cleaned


def delete_folders(paths):
    """Delete folders and return log lines."""
    logs = []
    for p in paths:
        try:
            if p and os.path.exists(p):
                shutil.rmtree(p)
                logs.append(f"Removed folder: {p}")
        except Exception as e:
            logs.append(f"Could not remove folder {p}: {e}")
    return logs


def delete_uninstall_registry_key(app):
    """Delete the exact uninstall registry subkey for this app (safer than substring)."""
    try:
        hkey = app.get("reg_hive")
        reg_path = app.get("reg_path")
        reg_subkey = app.get("reg_subkey")
        if hkey is None or not reg_path or not reg_subkey:
            return None
        key = winreg.OpenKey(hkey, reg_path, 0, winreg.KEY_ALL_ACCESS)
        try:
            winreg.DeleteKey(key, reg_subkey)
            return f"Removed registry: {reg_subkey}"
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        return f"Could not remove registry: {e}"

# ---------------- GUI ----------------


class AppManager:
    def __init__(self, root):
        self.root = root
        root.title("Application Manager - Gỡ Bỏ Ứng Dụng Hoàn Toàn")
        root.geometry("1200x700")

        # Variables
        self.is_scanning = False
        self.cancel_scan = threading.Event()
        self.all_apps = []
        self.tree_data = {}
        self.appdata_index_entries = None
        self.appdata_index_total = 0

        # Top frame with buttons
        top = ttk.Frame(root)
        top.pack(fill="x", padx=5, pady=5)

        btn_frame = ttk.Frame(top)
        btn_frame.pack(side="left")

        self.scan_btn = ttk.Button(
            btn_frame, text="🔍 Quét Ứng Dụng", command=self.scan_apps)
        self.scan_btn.pack(side="left", padx=2)

        self.stop_btn = ttk.Button(
            btn_frame, text="⏹ Stop Scan", command=self.stop_scan, state="disabled")
        self.stop_btn.pack(side="left", padx=2)

        self.uninstall_btn = ttk.Button(
            btn_frame, text="🗑 Gỡ Bỏ", command=self.uninstall_selected)
        self.uninstall_btn.pack(side="left", padx=2)

        self.uninstall_complete_btn = ttk.Button(
            btn_frame, text="🗑 Gỡ Bỏ Hoàn Toàn", command=self.uninstall_complete)
        self.uninstall_complete_btn.pack(side="left", padx=2)

        self.refresh_btn = ttk.Button(
            btn_frame, text="🔄 Làm Mới", command=self.refresh_selected)
        self.refresh_btn.pack(side="left", padx=2)

        stats_btn = ttk.Button(
            btn_frame, text="📊 Thống Kê", command=self.show_statistics)
        stats_btn.pack(side="left", padx=2)

        # Status frame
        status_frame = ttk.Frame(root)
        status_frame.pack(fill="x", padx=5, pady=2)

        self.status_label = ttk.Label(
            status_frame, text="Sẵn sàng", font=("Arial", 9))
        self.status_label.pack(side="left", padx=5)

        self.progress_bar = ttk.Progressbar(
            status_frame, mode="indeterminate", length=200)
        self.progress_bar.pack(side="right", padx=5)

        # Info label
        self.info_label = ttk.Label(
            root, text="", font=("Arial", 8), foreground="gray")
        self.info_label.pack(fill="x", padx=5, pady=2)

        # Search and filter frame
        filter_frame = ttk.LabelFrame(root, text="Tìm Kiếm & Lọc", padding=5)
        filter_frame.pack(fill="x", padx=5, pady=2)

        search_frame = ttk.Frame(filter_frame)
        search_frame.pack(fill="x", pady=2)

        ttk.Label(search_frame, text="Tìm kiếm:").pack(side="left", padx=2)
        self.search_var = tk.StringVar()
        self.search_var.trace("w", lambda *args: self.apply_filters())
        search_entry = ttk.Entry(
            search_frame, textvariable=self.search_var, width=30)
        search_entry.pack(side="left", padx=2)

        ttk.Label(search_frame, text="Publisher:").pack(
            side="left", padx=(10, 2))
        self.publisher_var = tk.StringVar()
        self.publisher_var.trace("w", lambda *args: self.apply_filters())
        publisher_entry = ttk.Entry(
            search_frame, textvariable=self.publisher_var, width=20)
        publisher_entry.pack(side="left", padx=2)

        clear_btn = ttk.Button(
            search_frame, text="Xóa Bộ Lọc", command=self.clear_filters)
        clear_btn.pack(side="left", padx=5)

        # Treeview
        tree_frame = ttk.Frame(root)
        tree_frame.pack(fill="both", expand=True, padx=5, pady=5)

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("name", "version", "publisher", "size",
                     "appdata_size", "install_date", "location"),
            show="headings",
            selectmode="extended"
        )
        self.tree.heading("name", text="Tên Ứng Dụng",
                          command=lambda: self.sort_by_column("name"))
        self.tree.heading("version", text="Phiên Bản",
                          command=lambda: self.sort_by_column("version"))
        self.tree.heading("publisher", text="Nhà Phát Hành",
                          command=lambda: self.sort_by_column("publisher"))
        self.tree.heading("size", text="Kích Thước",
                          command=lambda: self.sort_by_column("size"))
        self.tree.heading("appdata_size", text="AppData",
                          command=lambda: self.sort_by_column("appdata_size"))
        self.tree.heading("install_date", text="Ngày Cài Đặt",
                          command=lambda: self.sort_by_column("install_date"))
        self.tree.heading("location", text="Vị Trí")

        self.tree.column("name", width=250)
        self.tree.column("version", width=100)
        self.tree.column("publisher", width=180)
        self.tree.column("size", width=120)
        self.tree.column("appdata_size", width=120)
        self.tree.column("install_date", width=120)
        self.tree.column("location", width=250)

        scrollbar = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self.view_details)
        self.tree.bind("<Button-3>", self.show_context_menu)

        self.sort_reverse = {
            "name": False, "version": False, "publisher": False,
            "size": False, "appdata_size": False, "install_date": False
        }

        # Log area
        log_frame = ttk.LabelFrame(root, text="Nhật Ký", padding=5)
        log_frame.pack(fill="x", padx=5, pady=5)

        self.log = tk.Text(log_frame, height=5, font=("Consolas", 8))
        self.log.pack(fill="both", expand=True)

        # Context menu
        self.context_menu = tk.Menu(root, tearoff=0)
        self.context_menu.add_command(
            label="Xem Chi Tiết", command=self.view_details)
        self.context_menu.add_command(
            label="Mở Thư Mục", command=self.open_folder)
        self.context_menu.add_separator()
        self.context_menu.add_command(
            label="Gỡ Bỏ", command=self.uninstall_selected)
        self.context_menu.add_command(
            label="Gỡ Bỏ Hoàn Toàn", command=self.uninstall_complete)

    def _preview_select_appdata_folders(self, app, candidates):
        """
        Modal preview dialog for AppData folders to delete.
        Returns list of selected paths, or None if cancelled.
        """
        win = tk.Toplevel(self.root)
        win.title(f"Preview AppData - {app.get('name', '')}")
        win.geometry("900x450")
        win.transient(self.root)
        win.grab_set()

        ttk.Label(
            win,
            text="Chọn các thư mục AppData sẽ xóa (mặc định chọn các mục score cao).",
            font=("Arial", 9)
        ).pack(fill="x", padx=10, pady=(10, 5))

        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=10, pady=5)

        tree = ttk.Treeview(
            frame,
            columns=("del", "size", "score", "path", "reason"),
            show="headings",
            selectmode="extended"
        )
        tree.heading("del", text="Xóa?")
        tree.heading("size", text="Dung lượng")
        tree.heading("score", text="Score")
        tree.heading("path", text="Đường dẫn")
        tree.heading("reason", text="Lý do")

        tree.column("del", width=60, anchor="center")
        tree.column("size", width=110, anchor="e")
        tree.column("score", width=70, anchor="center")
        tree.column("path", width=420)
        tree.column("reason", width=200)

        vs = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vs.set)
        tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")

        # Default selection: high-confidence
        selected = {}
        for c in candidates:
            default = c.get("score", 0) >= 12
            selected[c["path"]] = default
            reason = ", ".join(c.get("reasons", [])[:2])
            tree.insert(
                "",
                "end",
                values=("✓" if default else "", bytes_to_human(
                    c.get("size", 0)), c.get("score", 0), c["path"], reason)
            )

        def toggle_selected_rows():
            for iid in tree.selection():
                vals = list(tree.item(iid, "values"))
                path = vals[3]
                new_state = not selected.get(path, False)
                selected[path] = new_state
                vals[0] = "✓" if new_state else ""
                tree.item(iid, values=tuple(vals))

        tree.bind("<Double-1>", lambda e: toggle_selected_rows())

        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=10)

        result = {"paths": None}

        def select_all():
            for iid in tree.get_children():
                vals = list(tree.item(iid, "values"))
                path = vals[3]
                selected[path] = True
                vals[0] = "✓"
                tree.item(iid, values=tuple(vals))

        def select_none():
            for iid in tree.get_children():
                vals = list(tree.item(iid, "values"))
                path = vals[3]
                selected[path] = False
                vals[0] = ""
                tree.item(iid, values=tuple(vals))

        def on_cancel():
            result["paths"] = None
            win.destroy()

        def on_delete():
            paths = [p for p, v in selected.items() if v]
            if not paths:
                if not messagebox.askyesno("Xác nhận", "Bạn chưa chọn thư mục nào. Tiếp tục (không xóa AppData)?"):
                    return
            result["paths"] = paths
            win.destroy()

        ttk.Button(btns, text="Chọn tất cả",
                   command=select_all).pack(side="left")
        ttk.Button(btns, text="Bỏ chọn tất cả",
                   command=select_none).pack(side="left", padx=5)
        ttk.Button(btns, text="Hủy", command=on_cancel).pack(side="right")
        ttk.Button(btns, text="Xóa mục đã chọn",
                   command=on_delete).pack(side="right", padx=5)

        win.wait_window()
        return result["paths"]

    def log_msg(self, msg):
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)

    def update_status(self, text):
        """Thread-safe status update"""
        self.root.after(0, lambda: self.status_label.config(text=text))

    def update_info(self, text):
        """Thread-safe info update"""
        self.root.after(0, lambda: self.info_label.config(text=text))

    def scan_apps(self):
        if self.is_scanning:
            return
        self.cancel_scan.clear()
        threading.Thread(target=self._scan_apps, daemon=True).start()

    def stop_scan(self):
        """Request scan cancellation"""
        if not self.is_scanning:
            return
        self.cancel_scan.set()
        self.update_status("Đang dừng quét... (vui lòng đợi)")

    def _scan_apps(self):
        self.is_scanning = True
        self.root.after(0, lambda: self.scan_btn.config(state="disabled"))
        self.root.after(0, lambda: self.stop_btn.config(state="normal"))
        self.root.after(0, lambda: self.uninstall_btn.config(state="disabled"))
        self.root.after(
            0, lambda: self.uninstall_complete_btn.config(state="disabled"))
        self.root.after(0, lambda: self.refresh_btn.config(state="disabled"))
        self.root.after(0, lambda: self.progress_bar.start(10))

        self.tree.delete(*self.tree.get_children())
        self.log_msg("=" * 60)
        self.log_msg("Đang quét ứng dụng đã cài đặt...")
        self.update_status("Đang quét... Vui lòng đợi")

        try:
            apps = get_installed_apps()
            self.all_apps = apps

            if self.cancel_scan.is_set():
                raise RuntimeError("Scan cancelled")

            # 1) Build AppData index once (fast reuse)
            self.update_status("Indexing AppData (1 lần)...")
            self.log_msg("Indexing AppData (1 lần)...")
            entries, idx_total = build_appdata_index(
                cancel_check=self.cancel_scan.is_set,
                progress_cb=lambda msg: self.update_status(msg)
            )
            self.appdata_index_entries = entries
            self.appdata_index_total = idx_total

            if self.cancel_scan.is_set():
                raise RuntimeError("Scan cancelled")

            self.update_status("Đang map AppData cho từng ứng dụng...")
            total_size = 0
            total_appdata_size = 0
            self.tree_data.clear()

            for idx, app in enumerate(apps):
                if self.cancel_scan.is_set():
                    break
                # Calculate AppData size
                self.update_status(
                    f"Đang tính AppData: {app['name']}... ({idx+1}/{len(apps)})")
                appdata_size = get_appdata_size(
                    app, appdata_entries=self.appdata_index_entries, cancel_check=self.cancel_scan.is_set)
                app["appdata_size"] = appdata_size
                total_appdata_size += appdata_size

                item_id = self.tree.insert(
                    "", "end",
                    values=(
                        app["name"],
                        app["version"],
                        app["publisher"],
                        bytes_to_human(app["size"]),
                        bytes_to_human(appdata_size),
                        app["install_date"],
                        app["install_location"][:50] + "..." if len(
                            app["install_location"]) > 50 else app["install_location"]
                    )
                )
                self.tree_data[item_id] = app
                total_size += app["size"]

                # Update info periodically
                if (idx + 1) % 10 == 0:
                    self.update_info(
                        f"Đã xử lý {idx+1}/{len(apps)} ứng dụng...")

            self.root.after(0, lambda: self.progress_bar.stop())
            self.root.after(0, lambda: self.scan_btn.config(state="normal"))
            self.root.after(0, lambda: self.stop_btn.config(state="disabled"))
            self.root.after(
                0, lambda: self.uninstall_btn.config(state="normal"))
            self.root.after(
                0, lambda: self.uninstall_complete_btn.config(state="normal"))
            self.root.after(0, lambda: self.refresh_btn.config(state="normal"))
            self.root.after(0, self.apply_filters)

            processed = len(self.tree.get_children())
            if self.cancel_scan.is_set():
                self.update_status(
                    f"Đã dừng quét - Đã xử lý {processed}/{len(apps)} ứng dụng")
                self.log_msg(
                    f"⏹ Đã dừng quét. Đã xử lý {processed}/{len(apps)} ứng dụng.")
            else:
                self.update_status(f"Sẵn sàng - Tìm thấy {len(apps)} ứng dụng")
                self.log_msg(
                    f"Hoàn thành. Tìm thấy {len(apps)} ứng dụng | Kích thước: {bytes_to_human(total_size)} | AppData: {bytes_to_human(total_appdata_size)}")

            self.update_info(
                f"Tổng: {len(apps)} ứng dụng | Kích thước: {bytes_to_human(total_size)} | AppData: {bytes_to_human(total_appdata_size)}")
            self.log_msg("=" * 60)

        except Exception as e:
            if str(e) == "Scan cancelled":
                self.log_msg("⏹ Đã dừng quét.")
                self.update_status("Đã dừng quét")
            else:
                self.log_msg(f"✗ Lỗi: {e}")
                self.update_status("Lỗi khi quét")
        finally:
            self.root.after(0, lambda: self.progress_bar.stop())
            self.root.after(0, lambda: self.scan_btn.config(state="normal"))
            self.root.after(0, lambda: self.stop_btn.config(state="disabled"))
            self.root.after(
                0, lambda: self.uninstall_btn.config(state="normal"))
            self.root.after(
                0, lambda: self.uninstall_complete_btn.config(state="normal"))
            self.root.after(0, lambda: self.refresh_btn.config(state="normal"))
            self.is_scanning = False

    def apply_filters(self):
        """Apply search and publisher filters"""
        search_text = self.search_var.get().lower()
        publisher_text = self.publisher_var.get().lower()

        # Clear current display
        for item in self.tree.get_children():
            self.tree.delete(item)

        # Filter and display
        filtered_count = 0
        total_size = 0

        for app in self.all_apps:
            # Search filter
            if search_text:
                if search_text not in app["name"].lower() and \
                   search_text not in app["publisher"].lower():
                    continue

            # Publisher filter
            if publisher_text:
                if publisher_text not in app["publisher"].lower():
                    continue

            # Add to tree
            appdata_size = app.get("appdata_size", 0)
            item_id = self.tree.insert(
                "", "end",
                values=(
                    app["name"],
                    app["version"],
                    app["publisher"],
                    bytes_to_human(app["size"]),
                    bytes_to_human(appdata_size),
                    app["install_date"],
                    app["install_location"][:50] +
                    "..." if len(app["install_location"]
                                 ) > 50 else app["install_location"]
                )
            )
            self.tree_data[item_id] = app
            filtered_count += 1
            total_size += app["size"]

        self.update_info(
            f"Hiển thị {filtered_count} / {len(self.all_apps)} ứng dụng | Tổng: {bytes_to_human(total_size)}")

    def clear_filters(self):
        """Clear all filters"""
        self.search_var.set("")
        self.publisher_var.set("")
        self.apply_filters()

    def sort_by_column(self, column):
        """Sort tree by column"""
        items = []
        for item in self.tree.get_children(""):
            col_idx = ["name", "version", "publisher", "size", "appdata_size",
                       "install_date", "location"].index(column)
            value = self.tree.item(item, "values")[col_idx]
            items.append((value, item))

        if column == "size":
            # Sort by actual bytes
            items.sort(key=lambda x: self.tree_data.get(
                x[1], {}).get("size", 0), reverse=self.sort_reverse[column])
        elif column == "appdata_size":
            # Sort by actual AppData bytes
            items.sort(key=lambda x: self.tree_data.get(
                x[1], {}).get("appdata_size", 0), reverse=self.sort_reverse[column])
        else:
            items.sort(key=lambda x: str(x[0]).lower(),
                       reverse=self.sort_reverse[column])

        self.sort_reverse[column] = not self.sort_reverse[column]

        # Rearrange items
        for index, (val, item) in enumerate(items):
            self.tree.move(item, "", index)

    def view_details(self, event=None):
        """View detailed information about selected app (pretty layout)"""
        items = self.tree.selection()
        if not items:
            return

        item = items[0]
        if item not in self.tree_data:
            return

        app = self.tree_data[item]
        appdata_size = app.get("appdata_size", 0)

        # Chuẩn hóa dữ liệu hiển thị
        name = app.get("name", "Không rõ")
        version = app.get("version", "Không rõ")
        publisher = app.get("publisher", "Không rõ")
        install_date = app.get("install_date", "Không rõ")
        install_location = app.get("install_location") or "Không xác định"
        uninstall_str = app.get("uninstall_string", "")
        if len(uninstall_str) > 150:
            uninstall_str_display = uninstall_str[:150] + "..."
        else:
            uninstall_str_display = uninstall_str or "Không có"

        size_str = bytes_to_human(app.get("size", 0))
        appdata_str = bytes_to_human(appdata_size)
        total_str = bytes_to_human(app.get("size", 0) + appdata_size)

        details = (
            "📦 Thông tin ứng dụng\n"
            "────────────────────────────\n"
            f"• Tên: {name}\n"
            f"• Phiên bản: {version}\n"
            f"• Nhà phát hành: {publisher}\n"
            f"• Ngày cài đặt: {install_date}\n"
            "\n"
            "💾 Dung lượng\n"
            "────────────────────────────\n"
            f"• Kích thước chương trình: {size_str}\n"
            f"• Dung lượng AppData:      {appdata_str}\n"
            f"• Tổng dung lượng:         {total_str}\n"
            "\n"
            "📂 Đường dẫn & Gỡ cài đặt\n"
            "────────────────────────────\n"
            f"• Vị trí cài đặt:\n  {install_location}\n"
            f"• Lệnh gỡ bỏ:\n  {uninstall_str_display}"
        )

        messagebox.showinfo("Chi tiết ứng dụng", details)

    def open_folder(self):
        """Open installation folder"""
        items = self.tree.selection()
        if not items:
            return

        item = items[0]
        if item not in self.tree_data:
            return

        app = self.tree_data[item]
        location = app["install_location"]

        if location and os.path.exists(location):
            subprocess.Popen(f'explorer "{location}"')
        else:
            messagebox.showinfo("Thông báo", "Không tìm thấy thư mục cài đặt.")

    def uninstall_selected(self):
        """Uninstall selected application(s). Optionally delete AppData with preview (safe)."""
        if self.is_scanning:
            messagebox.showwarning(
                "Cảnh báo", "Vui lòng đợi quá trình quét hoàn thành.")
            return

        items = self.tree.selection()
        if not items:
            messagebox.showinfo(
                "Thông báo", "Vui lòng chọn ứng dụng để gỡ bỏ.")
            return

        # 1) Confirm uninstall
        if len(items) > 1:
            if not messagebox.askyesno(
                "Xác nhận",
                f"Gỡ bỏ {len(items)} ứng dụng?\n\n⚠ Cảnh báo: Hành động này không thể hoàn tác!"
            ):
                return
        else:
            app0 = self.tree_data.get(items[0], {})
            if not messagebox.askyesno(
                "Xác nhận Gỡ Bỏ",
                f"Gỡ bỏ ứng dụng?\n\n{app0.get('name', '')}\n\n⚠ Cảnh báo: Hành động này không thể hoàn tác!"
            ):
                return

        # 2) Ask if delete AppData + show preview per app (avoid risky matching)
        total_appdata_size = 0
        for iid in items:
            a = self.tree_data.get(iid)
            if a:
                total_appdata_size += int(a.get("appdata_size", 0) or 0)

        delete_appdata = False
        appdata_delete_plan = {}  # iid -> [paths]

        if total_appdata_size > 0:
            delete_appdata = messagebox.askyesno(
                "Tùy chọn",
                "Bạn có muốn xóa AppData không?\n\n"
                f"Tổng AppData (ước tính): {bytes_to_human(total_appdata_size)}\n\n"
                "⚠ Tool sẽ hiện preview thư mục trước khi xóa để tránh xóa nhầm."
            )

            if delete_appdata:
                for iid in items:
                    app = self.tree_data.get(iid)
                    if not app:
                        continue
                    candidates = find_appdata_candidates(
                        app, appdata_entries=self.appdata_index_entries
                    )
                    if not candidates:
                        appdata_delete_plan[iid] = []
                        continue
                    selected_paths = self._preview_select_appdata_folders(
                        app, candidates)
                    if selected_paths is None:
                        return
                    appdata_delete_plan[iid] = selected_paths

        def uninstall_thread():
            self.root.after(0, lambda: self.update_status("Đang gỡ bỏ..."))
            self.root.after(0, lambda: self.scan_btn.config(state="disabled"))
            self.root.after(
                0, lambda: self.uninstall_btn.config(state="disabled"))
            self.root.after(
                0, lambda: self.uninstall_complete_btn.config(state="disabled"))

            success_count = 0
            failed_count = 0

            for iid in items:
                app = self.tree_data.get(iid)
                if not app:
                    continue

                self.root.after(0, lambda a=app: self.update_status(
                    f"Đang gỡ bỏ: {a.get('name', '')}..."))

                if app.get("uninstall_string"):
                    success, output = uninstall_app(
                        app["uninstall_string"], app.get("name", ""))
                    if success:
                        self.log_msg(f"✓ Đã gỡ bỏ: {app.get('name', '')}")
                        success_count += 1

                        if delete_appdata:
                            paths = appdata_delete_plan.get(iid, [])
                            if paths:
                                self.root.after(0, lambda a=app: self.update_status(
                                    f"Đang xóa AppData: {a.get('name', '')}..."))
                                for line in delete_folders(paths):
                                    self.log_msg(f"  ✓ {line}")

                        # Remove from tree + data
                        self.root.after(0, lambda i=iid: self.tree.delete(i))
                        self.root.after(
                            0, lambda i=iid: self.tree_data.pop(i, None))
                        self.root.after(0, lambda a=app: self.all_apps.remove(
                            a) if a in self.all_apps else None)
                    else:
                        self.log_msg(
                            f"✗ Lỗi gỡ bỏ {app.get('name', '')}: {output}")
                        failed_count += 1
                else:
                    self.log_msg(
                        f"✗ Không tìm thấy lệnh gỡ bỏ: {app.get('name', '')}")
                    failed_count += 1

            self.root.after(0, lambda: self.scan_btn.config(state="normal"))
            self.root.after(
                0, lambda: self.uninstall_btn.config(state="normal"))
            self.root.after(
                0, lambda: self.uninstall_complete_btn.config(state="normal"))
            self.root.after(0, lambda: self.update_status(
                f"Sẵn sàng - Thành công: {success_count}, Thất bại: {failed_count}"))
            self.root.after(0, self.apply_filters)

        threading.Thread(target=uninstall_thread, daemon=True).start()

    def uninstall_complete(self):
        """Uninstall application completely (including leftovers)"""
        if self.is_scanning:
            messagebox.showwarning(
                "Cảnh báo", "Vui lòng đợi quá trình quét hoàn thành.")
            return

        items = self.tree.selection()
        if not items:
            messagebox.showinfo(
                "Thông báo", "Vui lòng chọn ứng dụng để gỡ bỏ hoàn toàn.")
            return

        if len(items) > 1:
            if not messagebox.askyesno(
                "Xác nhận Gỡ Bỏ Hoàn Toàn",
                f"Gỡ bỏ hoàn toàn {len(items)} ứng dụng?\n\n⚠ Cảnh báo: Hành động này sẽ:\n- Gỡ bỏ ứng dụng\n- Xóa các file còn sót lại\n- Xóa registry entries\n- Xóa AppData\n\nKhông thể hoàn tác!"
            ):
                return
        else:
            app = self.tree_data[items[0]]
            if not messagebox.askyesno(
                "Xác nhận Gỡ Bỏ Hoàn Toàn",
                f"Gỡ bỏ hoàn toàn ứng dụng?\n\n{app['name']}\n\n⚠ Cảnh báo: Hành động này sẽ:\n- Gỡ bỏ ứng dụng\n- Xóa các file còn sót lại\n- Xóa registry entries\n- Xóa AppData\n\nKhông thể hoàn tác!"
            ):
                return

        # Preview AppData deletion plan per app (safer than substring matching)
        appdata_delete_plan = {}  # iid -> [paths]
        for iid in items:
            app = self.tree_data.get(iid)
            if not app:
                continue
            candidates = find_appdata_candidates(
                app, appdata_entries=self.appdata_index_entries
            )
            if not candidates:
                appdata_delete_plan[iid] = []
                continue
            selected_paths = self._preview_select_appdata_folders(
                app, candidates)
            if selected_paths is None:
                return
            appdata_delete_plan[iid] = selected_paths

        def uninstall_complete_thread():
            self.root.after(0, lambda: self.update_status(
                "Đang gỡ bỏ hoàn toàn..."))
            self.root.after(0, lambda: self.scan_btn.config(state="disabled"))
            self.root.after(
                0, lambda: self.uninstall_btn.config(state="disabled"))
            self.root.after(
                0, lambda: self.uninstall_complete_btn.config(state="disabled"))

            success_count = 0
            failed_count = 0

            for item in items:
                app = self.tree_data.get(item)
                if not app:
                    continue
                self.root.after(0, lambda a=app: self.update_status(
                    f"Đang gỡ bỏ hoàn toàn: {a['name']}..."))

                # Step 1: Uninstall normally
                if app.get("uninstall_string"):
                    success, output = uninstall_app(
                        app["uninstall_string"], app["name"])
                    if success:
                        self.log_msg(f"✓ Đã gỡ bỏ: {app['name']}")
                    else:
                        self.log_msg(
                            f"⚠ Gỡ bỏ không thành công, tiếp tục dọn dẹp: {app['name']}")

                # Step 2: Clean leftovers (safer)
                self.root.after(0, lambda a=app: self.update_status(
                    f"Đang dọn dẹp: {a['name']}..."))

                # 2.1 Remove install folder
                for line in delete_folders([app.get("install_location", "")]):
                    self.log_msg(f"  ✓ {line}")

                # 2.2 Remove selected AppData folders (preview-approved)
                paths = appdata_delete_plan.get(item, [])
                if paths:
                    for line in delete_folders(paths):
                        self.log_msg(f"  ✓ {line}")

                # 2.3 Remove exact uninstall registry key (avoid substring delete)
                reg_line = delete_uninstall_registry_key(app)
                if reg_line:
                    self.log_msg(f"  ✓ {reg_line}")

                # Remove from tree
                self.root.after(0, lambda i=item: self.tree.delete(i))
                # Remove from data
                self.root.after(0, lambda i=item: self.tree_data.pop(i, None))
                # Remove from all_apps
                self.root.after(0, lambda a=app: self.all_apps.remove(
                    a) if a in self.all_apps else None)

                success_count += 1

            self.root.after(0, lambda: self.scan_btn.config(state="normal"))
            self.root.after(
                0, lambda: self.uninstall_btn.config(state="normal"))
            self.root.after(
                0, lambda: self.uninstall_complete_btn.config(state="normal"))
            self.root.after(0, lambda: self.update_status(
                f"Sẵn sàng - Đã gỡ bỏ hoàn toàn {success_count} ứng dụng"))
            self.root.after(0, self.apply_filters)

        threading.Thread(target=uninstall_complete_thread, daemon=True).start()

    def refresh_selected(self):
        """Refresh selected app information"""
        items = self.tree.selection()
        if not items:
            messagebox.showinfo(
                "Thông báo", "Vui lòng chọn ứng dụng để làm mới.")
            return

        # Rescan to update
        self.scan_apps()

    def show_context_menu(self, event):
        """Show right-click context menu"""
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def show_statistics(self):
        """Show statistics about installed apps"""
        if not self.all_apps:
            messagebox.showinfo("Thông báo", "Vui lòng quét ứng dụng trước.")
            return

        total_apps = len(self.all_apps)
        total_size = sum(app["size"] for app in self.all_apps)
        avg_size = total_size / total_apps if total_apps > 0 else 0

        # Count by publisher
        publishers = {}
        for app in self.all_apps:
            pub = app["publisher"]
            publishers[pub] = publishers.get(pub, 0) + 1

        top_publishers = sorted(publishers.items(),
                                key=lambda x: x[1], reverse=True)[:10]
        top_publishers_str = "\n".join(
            [f"  {i+1}. {pub}: {count} ứng dụng" for i, (pub, count) in enumerate(top_publishers)])

        # Largest apps
        largest = sorted(
            self.all_apps, key=lambda x: x["size"], reverse=True)[:10]
        largest_str = "\n".join(
            [f"  {i+1}. {app['name']} - {bytes_to_human(app['size'])}" for i, app in enumerate(largest)])

        stats = f"""📊 Thống Kê Ứng Dụng

Tổng số ứng dụng: {total_apps:,}
Tổng dung lượng: {bytes_to_human(total_size)}
Dung lượng trung bình: {bytes_to_human(avg_size)}

Top 10 Nhà Phát Hành:
{top_publishers_str}

Top 10 Ứng Dụng Lớn Nhất:
{largest_str}"""

        messagebox.showinfo("Thống Kê", stats)


# ---------------- RUN ----------------

if __name__ == "__main__":
    root = tk.Tk()
    app = AppManager(root)
    root.mainloop()
