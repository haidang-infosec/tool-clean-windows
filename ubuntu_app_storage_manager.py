import os
import re
import shutil
import subprocess
import threading
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import ttk, messagebox, filedialog


# =========================
# Core helpers
# =========================


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_cmd(cmd: str, timeout: int = 180) -> tuple[int, str, str]:
    p = subprocess.run(
        cmd,
        shell=True,
        capture_output=True,
        text=True,
        timeout=timeout,
        encoding="utf-8",
        errors="replace",
    )
    return p.returncode, p.stdout, p.stderr


def bytes_to_human(num_bytes: int) -> str:
    n = float(num_bytes or 0)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if n < 1024:
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} EB"


def safe_tokenize(text: str) -> list[str]:
    if not text:
        return []
    parts = re.split(r"[^a-zA-Z0-9]+", text.lower())
    out = []
    for p in parts:
        if len(p) < 3:
            continue
        out.append(p)
    return out


def get_dir_size(path: str) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                total += os.path.getsize(fp)
            except Exception:
                pass
    return total


@dataclass
class AppEntry:
    id: str             # unique id (source:name)
    source: str         # dpkg | snap | flatpak
    name: str           # package/app name
    display: str        # human display
    installed_bytes: int
    data_bytes: int
    data_paths: list[str]


def list_dpkg_apps() -> list[AppEntry]:
    """
    dpkg-query installed packages with Installed-Size (KB).
    This approximates installed footprint (not counting shared deps).
    """
    rc, out, err = run_cmd(r"dpkg-query -W -f='${Package}\t${Installed-Size}\n'", timeout=180)
    if rc != 0:
        return []
    apps: list[AppEntry] = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split("\t")
        if len(parts) != 2:
            continue
        pkg, kb = parts
        try:
            installed = int(kb) * 1024
        except Exception:
            installed = 0
        apps.append(
            AppEntry(
                id=f"dpkg:{pkg}",
                source="dpkg",
                name=pkg,
                display=pkg,
                installed_bytes=installed,
                data_bytes=0,
                data_paths=[],
            )
        )
    return apps


def list_snap_apps() -> list[AppEntry]:
    """
    snap list -> Name, Version, Rev, Tracking, Publisher, Notes
    Installed size isn't directly available. We'll set installed_bytes=0 by default,
    and use user data size from ~/snap/<name>.
    """
    rc, out, err = run_cmd("snap list", timeout=120)
    if rc != 0:
        return []
    lines = [ln.rstrip("\n") for ln in out.splitlines() if ln.strip()]
    if not lines:
        return []
    # skip header
    lines = lines[1:]
    apps: list[AppEntry] = []
    for ln in lines:
        cols = re.split(r"\s{2,}", ln.strip())
        if not cols:
            continue
        name = cols[0]
        apps.append(
            AppEntry(
                id=f"snap:{name}",
                source="snap",
                name=name,
                display=name,
                installed_bytes=0,
                data_bytes=0,
                data_paths=[],
            )
        )
    return apps


def list_flatpak_apps() -> list[AppEntry]:
    """
    flatpak list --app -> Application ID + Name
    Installed size isn't trivial; we keep installed_bytes=0 and compute user data
    from ~/.var/app/<app_id>.
    """
    rc, out, err = run_cmd("flatpak list --app --columns=application,name", timeout=180)
    if rc != 0:
        return []
    apps: list[AppEntry] = []
    for ln in out.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        parts = ln.split("\t")
        if len(parts) >= 1:
            app_id = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else app_id
            apps.append(
                AppEntry(
                    id=f"flatpak:{app_id}",
                    source="flatpak",
                    name=app_id,
                    display=name,
                    installed_bytes=0,
                    data_bytes=0,
                    data_paths=[],
                )
            )
    return apps


def build_home_data_index(cancel_check=None):
    """
    Index common user data roots once (fast reuse).
    Returns list of dict: {path, base, folder, size}
    """
    home = os.path.expanduser("~")
    roots = [
        ("config", os.path.join(home, ".config")),
        ("local_share", os.path.join(home, ".local", "share")),
        ("cache", os.path.join(home, ".cache")),
    ]
    entries = []
    for base, root in roots:
        if cancel_check and cancel_check():
            break
        if not os.path.isdir(root):
            continue
        try:
            folders = [f for f in os.listdir(root) if os.path.isdir(os.path.join(root, f))]
        except Exception:
            folders = []
        for folder in folders:
            if cancel_check and cancel_check():
                break
            p = os.path.join(root, folder)
            try:
                size = get_dir_size(p)
            except Exception:
                size = 0
            entries.append({"path": p, "base": base, "folder": folder, "size": size})
    return entries


def match_data_paths_for_app(app: AppEntry, index_entries: list[dict]) -> tuple[int, list[str]]:
    """
    Conservative matching:
    - snap: ~/snap/<name>
    - flatpak: ~/.var/app/<app_id>
    - dpkg: match folder name exactly == package name, or token overlap >= 2.
    """
    home = os.path.expanduser("~")
    paths = []

    if app.source == "snap":
        p = os.path.join(home, "snap", app.name)
        if os.path.isdir(p):
            paths.append(p)
    elif app.source == "flatpak":
        p = os.path.join(home, ".var", "app", app.name)
        if os.path.isdir(p):
            paths.append(p)
    else:
        pkg = app.name.lower()
        pkg_tokens = set(safe_tokenize(pkg))
        for e in index_entries:
            folder = (e.get("folder") or "").lower()
            if folder == pkg:
                paths.append(e["path"])
                continue
            folder_tokens = set(safe_tokenize(folder))
            if len(pkg_tokens & folder_tokens) >= 2:
                paths.append(e["path"])

    # compute size
    total = 0
    uniq = []
    seen = set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        uniq.append(p)
        try:
            total += get_dir_size(p)
        except Exception:
            pass
    return total, uniq


# =========================
# GUI
# =========================


class UbuntuAppStorageManager:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Ubuntu App Storage Manager")
        root.geometry("1200x750")

        self.is_busy = False
        self.cancel = threading.Event()
        self.apps: list[AppEntry] = []
        self.filtered: list[AppEntry] = []
        self.data_index = []

        top = ttk.Frame(root)
        top.pack(fill="x", padx=8, pady=8)

        self.refresh_btn = ttk.Button(top, text="Refresh / Scan", command=self.refresh)
        self.refresh_btn.pack(side="left", padx=3)

        self.stop_btn = ttk.Button(top, text="Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=3)

        self.export_btn = ttk.Button(top, text="Export JSON", command=self.export_json, state="disabled")
        self.export_btn.pack(side="left", padx=12)

        self.delete_btn = ttk.Button(top, text="Delete Selected Data (HOME only)", command=self.delete_selected_data)
        self.delete_btn.pack(side="left", padx=3)

        status = ttk.Frame(root)
        status.pack(fill="x", padx=8, pady=(0, 6))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        self.pb = ttk.Progressbar(status, mode="indeterminate", length=220)
        self.pb.pack(side="right")

        # Filter
        filt = ttk.LabelFrame(root, text="Search / Filter", padding=6)
        filt.pack(fill="x", padx=8, pady=(0, 8))

        self.search_var = tk.StringVar()
        self.search_var.trace("w", lambda *_: self.apply_filter())
        ttk.Label(filt, text="Search:").pack(side="left")
        ttk.Entry(filt, textvariable=self.search_var, width=40).pack(side="left", padx=6)

        self.src_var = tk.StringVar(value="All")
        ttk.Label(filt, text="Source:").pack(side="left", padx=(12, 0))
        ttk.Combobox(
            filt,
            textvariable=self.src_var,
            values=["All", "dpkg", "snap", "flatpak"],
            state="readonly",
            width=12,
        ).pack(side="left", padx=6)
        self.src_var.trace("w", lambda *_: self.apply_filter())

        # Table
        main = ttk.Frame(root)
        main.pack(fill="both", expand=True, padx=8, pady=8)

        cols = ("source", "name", "installed", "data", "total", "data_paths")
        self.tree = ttk.Treeview(main, columns=cols, show="headings", selectmode="extended")
        for c in cols:
            self.tree.heading(c, text=c, command=lambda cc=c: self.sort_by(cc))
            self.tree.column(c, width=160 if c not in ("data_paths", "name") else (460 if c == "data_paths" else 240), anchor="w")

        vs = ttk.Scrollbar(main, orient="vertical", command=self.tree.yview)
        hs = ttk.Scrollbar(main, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")

        self.tree.bind("<<TreeviewSelect>>", lambda e: self.show_details())

        # Details + log
        bottom = ttk.Frame(root)
        bottom.pack(fill="both", expand=False, padx=8, pady=(0, 8))

        self.details = tk.Text(bottom, height=8, font=("Consolas", 9))
        self.details.pack(fill="x", expand=False)

        self.sort_state = {"col": "total", "rev": True}

        self.refresh()

    def set_busy(self, busy: bool, status: str):
        self.is_busy = busy
        self.status_var.set(status)
        self.refresh_btn.config(state="disabled" if busy else "normal")
        self.stop_btn.config(state="normal" if busy else "disabled")
        self.export_btn.config(state="disabled" if busy else ("normal" if self.apps else "disabled"))
        if busy:
            self.pb.start(10)
        else:
            self.pb.stop()

    def stop(self):
        if not self.is_busy:
            return
        self.cancel.set()
        self.status_var.set("Stopping...")

    def refresh(self):
        if self.is_busy:
            return
        self.cancel.clear()
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        self.root.after(0, lambda: self.set_busy(True, "Indexing HOME data..."))
        try:
            # 1) build home index
            self.data_index = build_home_data_index(cancel_check=self.cancel.is_set)
            if self.cancel.is_set():
                self.root.after(0, lambda: self.set_busy(False, "Stopped"))
                return

            # 2) list apps
            self.root.after(0, lambda: self.status_var.set("Listing installed apps..."))
            apps = []
            apps.extend(list_dpkg_apps())
            if self.cancel.is_set():
                self.root.after(0, lambda: self.set_busy(False, "Stopped"))
                return
            apps.extend(list_snap_apps())
            if self.cancel.is_set():
                self.root.after(0, lambda: self.set_busy(False, "Stopped"))
                return
            apps.extend(list_flatpak_apps())

            # 3) map data sizes
            total_installed = 0
            total_data = 0
            for i, app in enumerate(apps):
                if self.cancel.is_set():
                    break
                if (i + 1) % 30 == 0:
                    self.root.after(0, lambda i=i, n=len(apps): self.status_var.set(f"Calculating data sizes... {i+1}/{n}"))
                data_bytes, paths = match_data_paths_for_app(app, self.data_index)
                app.data_bytes = data_bytes
                app.data_paths = paths
                total_installed += app.installed_bytes
                total_data += app.data_bytes

            self.apps = apps
            self.root.after(0, self.apply_filter)
            self.root.after(0, lambda: self.status_var.set(
                f"Ready - apps={len(apps)} | installed={bytes_to_human(total_installed)} | data={bytes_to_human(total_data)}"
            ))
            self.root.after(0, lambda: self.set_busy(False, "Ready"))
        except Exception as e:
            self.root.after(0, lambda: self.set_busy(False, "Error"))
            self.root.after(0, lambda: messagebox.showerror("Error", str(e)))

    def apply_filter(self):
        q = (self.search_var.get() or "").lower().strip()
        src = self.src_var.get()
        out = []
        for a in self.apps:
            if src != "All" and a.source != src:
                continue
            if q and q not in (a.name + " " + a.display).lower():
                continue
            out.append(a)
        self.filtered = out
        self.render_table()

    def render_table(self):
        self.tree.delete(*self.tree.get_children())
        for a in self.filtered:
            total = a.installed_bytes + a.data_bytes
            self.tree.insert(
                "",
                "end",
                iid=a.id,
                values=(
                    a.source,
                    a.display if a.display else a.name,
                    bytes_to_human(a.installed_bytes),
                    bytes_to_human(a.data_bytes),
                    bytes_to_human(total),
                    ", ".join(a.data_paths[:3]) + (" ..." if len(a.data_paths) > 3 else ""),
                ),
            )
        self.show_details()

    def sort_by(self, col: str):
        rev = self.sort_state["rev"] if self.sort_state["col"] == col else False
        rev = not rev
        self.sort_state = {"col": col, "rev": rev}

        def key(a: AppEntry):
            if col == "installed":
                return a.installed_bytes
            if col == "data":
                return a.data_bytes
            if col == "total":
                return a.installed_bytes + a.data_bytes
            if col == "source":
                return a.source
            if col == "name":
                return (a.display or a.name).lower()
            if col == "data_paths":
                return ",".join(a.data_paths)
            return (a.display or a.name).lower()

        self.filtered.sort(key=key, reverse=rev)
        self.render_table()

    def _selected_entries(self) -> list[AppEntry]:
        ids = list(self.tree.selection())
        by_id = {a.id: a for a in self.apps}
        return [by_id[i] for i in ids if i in by_id]

    def show_details(self):
        sel = self._selected_entries()
        self.details.delete("1.0", tk.END)
        if not sel:
            self.details.insert(tk.END, "Select an app to see details.\n")
            return
        a = sel[0]
        self.details.insert(
            tk.END,
            f"Source: {a.source}\nName: {a.name}\nDisplay: {a.display}\nInstalled: {bytes_to_human(a.installed_bytes)}\n"
            f"Data: {bytes_to_human(a.data_bytes)}\nData paths:\n  " + "\n  ".join(a.data_paths)
        )

    def export_json(self):
        if not self.apps:
            return
        path = filedialog.asksaveasfilename(
            title="Save JSON report",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialfile=f"ubuntu_app_storage_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        if not path:
            return
        data = {
            "generated_at": now_str(),
            "apps": [
                {
                    "id": a.id,
                    "source": a.source,
                    "name": a.name,
                    "display": a.display,
                    "installed_bytes": a.installed_bytes,
                    "data_bytes": a.data_bytes,
                    "data_paths": a.data_paths,
                }
                for a in self.apps
            ],
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        messagebox.showinfo("OK", f"Saved report to:\n{path}")

    def delete_selected_data(self):
        sel = self._selected_entries()
        if not sel:
            return
        # collect HOME-only data paths
        home = os.path.expanduser("~")
        paths = []
        for a in sel:
            for p in a.data_paths:
                if p.startswith(home + os.sep) and os.path.isdir(p):
                    paths.append(p)
        paths = sorted(set(paths))
        if not paths:
            messagebox.showinfo("Info", "No HOME data paths found for selection.")
            return

        # final confirm
        preview = "\n".join(paths[:15]) + ("\n..." if len(paths) > 15 else "")
        if not messagebox.askyesno(
            "Confirm delete",
            f"Delete user data folders (HOME only) for {len(sel)} app(s)?\n\n"
            f"Folders: {len(paths)}\n\n{preview}\n\nThis cannot be undone.",
        ):
            return

        # delete
        deleted = 0
        failed = 0
        for p in paths:
            try:
                shutil.rmtree(p)
                deleted += 1
            except Exception:
                failed += 1
        messagebox.showinfo("Done", f"Deleted: {deleted}, Failed: {failed}")
        self.refresh()


def main():
    root = tk.Tk()
    app = UbuntuAppStorageManager(root)
    root.mainloop()


if __name__ == "__main__":
    main()

