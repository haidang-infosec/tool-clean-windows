import json
import os
import re
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime
from tkinter import ttk, messagebox, filedialog


# ---------------- CORE ----------------


def _now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_size_to_bytes(size_str: str) -> int:
    """
    Parse Docker size strings like:
      '12.3MB', '1.2GB', '456kB', '10MiB', '1.3GiB', '0B'
    Returns bytes (int). Unknown returns 0.
    """
    if not size_str:
        return 0
    s = str(size_str).strip()
    if not s:
        return 0

    # sometimes: "12.3MB (virtual 1.2GB)" -> keep first token
    s = s.split()[0]

    m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([a-zA-Z]+)\s*$", s)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2)

    unit_map = {
        # decimal
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "TB": 1000**4,
        "PB": 1000**5,
        "kB": 1000,
        # binary
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
        "PiB": 1024**5,
    }
    mult = unit_map.get(unit)
    if mult is None:
        return 0
    return int(num * mult)


def bytes_to_human(num_bytes: int) -> str:
    if num_bytes is None:
        num_bytes = 0
    num = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB", "PB"]:
        if num < 1024:
            return f"{num:.2f} {unit}"
        num /= 1024
    return f"{num:.2f} EB"


@dataclass
class CmdResult:
    ok: bool
    rc: int
    stdout: str
    stderr: str
    cmd: str
    duration_ms: int


def run_cmd(cmd: str, timeout: int = 120) -> CmdResult:
    start = time.time()
    try:
        p = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        dur = int((time.time() - start) * 1000)
        ok = p.returncode == 0
        return CmdResult(ok=ok, rc=p.returncode, stdout=p.stdout, stderr=p.stderr, cmd=cmd, duration_ms=dur)
    except subprocess.TimeoutExpired as e:
        dur = int((time.time() - start) * 1000)
        return CmdResult(ok=False, rc=124, stdout=e.stdout or "", stderr="Timeout", cmd=cmd, duration_ms=dur)
    except Exception as e:
        dur = int((time.time() - start) * 1000)
        return CmdResult(ok=False, rc=1, stdout="", stderr=str(e), cmd=cmd, duration_ms=dur)


def run_docker(args: str, timeout: int = 120) -> CmdResult:
    return run_cmd(f"docker {args}", timeout=timeout)


def parse_system_df(text: str):
    """
    Parse 'docker system df -v' output (English). Returns:
      summary: list[dict]
      sections: dict[str, list[dict]]  # Images/Containers/Local Volumes/Build Cache
    Best-effort; if parsing fails returns empty structures.
    """
    lines = [ln.rstrip("\n") for ln in (text or "").splitlines()]
    summary = []
    sections = {"Images": [], "Containers": [], "Local Volumes": [], "Build Cache": []}

    def split_cols(line: str):
        return [c.strip() for c in re.split(r"\s{2,}", line.strip()) if c.strip()]

    # summary table
    try:
        idx = next(i for i, ln in enumerate(lines) if ln.strip().startswith("TYPE"))
        headers = split_cols(lines[idx])
        for j in range(idx + 1, min(idx + 10, len(lines))):
            if not lines[j].strip():
                break
            cols = split_cols(lines[j])
            if len(cols) >= 4:
                row = dict(zip(headers, cols))
                summary.append(row)
    except Exception:
        pass

    # verbose sections
    # Identify section blocks like: "Images space usage:" then header and rows
    section_map = {
        "Images space usage:": "Images",
        "Containers space usage:": "Containers",
        "Local Volumes space usage:": "Local Volumes",
        "Build Cache space usage:": "Build Cache",
    }

    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        if ln in section_map:
            sec_name = section_map[ln]
            # next non-empty line is header
            k = i + 1
            while k < len(lines) and not lines[k].strip():
                k += 1
            if k >= len(lines):
                break
            headers = split_cols(lines[k])
            k += 1
            rows = []
            while k < len(lines) and lines[k].strip():
                cols = split_cols(lines[k])
                # ignore lines that don't match the header count
                if len(cols) >= len(headers):
                    row = dict(zip(headers, cols[: len(headers)]))
                    rows.append(row)
                k += 1
            sections[sec_name] = rows
            i = k
            continue
        i += 1

    return summary, sections


# ---------------- GUI ----------------


class DockerStorageManager:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Docker Storage Manager (Windows) - Chuyên sâu")
        self.root.geometry("1200x750")

        self.is_busy = False
        self.cancel = threading.Event()
        self.last_report = None

        # Top actions
        top = ttk.Frame(root)
        top.pack(fill="x", padx=8, pady=8)

        self.refresh_btn = ttk.Button(top, text="🔄 Refresh / Analyze", command=self.refresh)
        self.refresh_btn.pack(side="left", padx=3)

        self.stop_btn = ttk.Button(top, text="⏹ Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=3)

        self.export_btn = ttk.Button(top, text="💾 Export Report", command=self.export_report, state="disabled")
        self.export_btn.pack(side="left", padx=3)

        self.open_docker_btn = ttk.Button(top, text="🧰 Open Docker Desktop", command=self.open_docker_desktop)
        self.open_docker_btn.pack(side="left", padx=10)

        # Status line
        status = ttk.Frame(root)
        status.pack(fill="x", padx=8, pady=(0, 6))
        self.status_var = tk.StringVar(value="Ready")
        ttk.Label(status, textvariable=self.status_var).pack(side="left")
        self.pb = ttk.Progressbar(status, mode="indeterminate", length=220)
        self.pb.pack(side="right")

        # Summary cards
        summary = ttk.LabelFrame(root, text="Tổng quan", padding=8)
        summary.pack(fill="x", padx=8, pady=(0, 8))

        self.env_var = tk.StringVar(value="Docker: (unknown)")
        self.total_var = tk.StringVar(value="Total reclaimable: -")
        self.images_var = tk.StringVar(value="Images: -")
        self.containers_var = tk.StringVar(value="Containers: -")
        self.volumes_var = tk.StringVar(value="Volumes: -")
        self.build_var = tk.StringVar(value="Build Cache: -")

        ttk.Label(summary, textvariable=self.env_var).grid(row=0, column=0, sticky="w")
        ttk.Label(summary, textvariable=self.total_var).grid(row=0, column=1, sticky="w", padx=20)
        ttk.Label(summary, textvariable=self.images_var).grid(row=1, column=0, sticky="w")
        ttk.Label(summary, textvariable=self.containers_var).grid(row=1, column=1, sticky="w", padx=20)
        ttk.Label(summary, textvariable=self.volumes_var).grid(row=2, column=0, sticky="w")
        ttk.Label(summary, textvariable=self.build_var).grid(row=2, column=1, sticky="w", padx=20)

        for c in range(2):
            summary.grid_columnconfigure(c, weight=1)

        # Notebook tabs
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_images = self._make_table_tab("Images", columns=("repo", "tag", "id", "size", "reclaimable", "shared", "unique", "containers"))
        self.tab_containers = self._make_table_tab("Containers", columns=("id", "image", "command", "local_volumes", "size", "created", "status", "names"))
        self.tab_volumes = self._make_table_tab("Volumes", columns=("name", "links", "size", "reclaimable"))
        self.tab_build = self._make_table_tab("Build Cache", columns=("id", "type", "size", "last_used", "usage_count", "reclaimable"))
        self.tab_prune = self._make_prune_tab()
        self.tab_logs = self._make_logs_tab()

        self.nb.add(self.tab_images["frame"], text="Images")
        self.nb.add(self.tab_containers["frame"], text="Containers")
        self.nb.add(self.tab_volumes["frame"], text="Volumes")
        self.nb.add(self.tab_build["frame"], text="Build Cache")
        self.nb.add(self.tab_prune, text="Prune (an toàn)")
        self.nb.add(self.tab_logs, text="Logs")

        self._log(f"Started at {_now_str()}")

        # Quick self-check
        self.root.after(300, self.refresh)

    # ---------- UI helpers ----------

    def _make_table_tab(self, title: str, columns):
        frame = ttk.Frame(self.nb)
        top = ttk.Frame(frame)
        top.pack(fill="x", padx=6, pady=6)

        ttk.Label(top, text=f"{title} - chọn dòng rồi xóa nếu cần").pack(side="left")

        btn_frame = ttk.Frame(top)
        btn_frame.pack(side="right")

        rm_btn = ttk.Button(btn_frame, text="🗑 Remove Selected", command=lambda: self.remove_selected(title))
        rm_btn.pack(side="left", padx=3)

        refresh_btn = ttk.Button(btn_frame, text="🔄 Refresh", command=self.refresh)
        refresh_btn.pack(side="left", padx=3)

        table_frame = ttk.Frame(frame)
        table_frame.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        for col in columns:
            tree.heading(col, text=col, command=lambda c=col, t=tree: self.sort_tree(t, c))
            tree.column(col, width=140, anchor="w")

        vs = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        hs = ttk.Scrollbar(table_frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)

        tree.pack(side="left", fill="both", expand=True)
        vs.pack(side="right", fill="y")
        hs.pack(side="bottom", fill="x")

        return {"frame": frame, "tree": tree, "columns": columns}

    def _make_prune_tab(self):
        frame = ttk.Frame(self.nb)
        row = ttk.Frame(frame)
        row.pack(fill="x", padx=10, pady=10)

        ttk.Label(row, text="Chế độ an toàn: luôn confirm + log. Nên refresh trước khi prune.").pack(side="left")

        opts = ttk.LabelFrame(frame, text="Tùy chọn prune", padding=10)
        opts.pack(fill="x", padx=10, pady=(0, 10))

        self.prune_a = tk.BooleanVar(value=False)
        self.prune_volumes = tk.BooleanVar(value=False)
        self.prune_build = tk.BooleanVar(value=True)
        self.prune_until = tk.StringVar(value="24h")

        ttk.Checkbutton(opts, text="Remove ALL unused images (-a) (nguy hiểm hơn)", variable=self.prune_a).grid(row=0, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Remove unused volumes (--volumes)", variable=self.prune_volumes).grid(row=1, column=0, sticky="w")
        ttk.Checkbutton(opts, text="Prune build cache", variable=self.prune_build).grid(row=2, column=0, sticky="w")
        ttk.Label(opts, text="Build cache filter (until=):").grid(row=2, column=1, sticky="e", padx=8)
        ttk.Entry(opts, textvariable=self.prune_until, width=12).grid(row=2, column=2, sticky="w")

        btns = ttk.Frame(frame)
        btns.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(btns, text="🧪 Simulate (dry info)", command=self.simulate_prune).pack(side="left")
        ttk.Button(btns, text="🔥 Run Prune", command=self.run_prune).pack(side="left", padx=6)

        self.prune_output = tk.Text(frame, height=12, font=("Consolas", 9))
        self.prune_output.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        return frame

    def _make_logs_tab(self):
        frame = ttk.Frame(self.nb)
        self.log_box = tk.Text(frame, height=10, font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=8, pady=8)
        return frame

    def _set_busy(self, busy: bool, status: str = None):
        self.is_busy = busy
        if status is not None:
            self.status_var.set(status)
        self.refresh_btn.config(state="disabled" if busy else "normal")
        self.stop_btn.config(state="normal" if busy else "disabled")
        if busy:
            self.pb.start(10)
        else:
            self.pb.stop()

    def _log(self, msg: str):
        line = f"[{_now_str()}] {msg}"
        if hasattr(self, "log_box"):
            self.log_box.insert(tk.END, line + "\n")
            self.log_box.see(tk.END)

    # ---------- actions ----------

    def stop(self):
        if not self.is_busy:
            return
        self.cancel.set()
        self._log("Stop requested.")
        self.status_var.set("Stopping... (will stop after current command)")

    def open_docker_desktop(self):
        # Docker Desktop path varies; attempt start by app id
        try:
            subprocess.Popen("start shell:AppsFolder\\DockerDesktop_0jql9y3f4m8p0!DockerDesktop", shell=True)
            self._log("Opened Docker Desktop.")
        except Exception as e:
            messagebox.showerror("Error", f"Could not open Docker Desktop: {e}")

    def refresh(self):
        if self.is_busy:
            return
        self.cancel.clear()
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        self.root.after(0, lambda: self._set_busy(True, "Analyzing Docker storage..."))
        self.root.after(0, lambda: self.export_btn.config(state="disabled"))
        self._log("Refresh started.")

        # 1) docker version / info
        ver = run_docker("version --format \"{{.Server.Version}}\"")
        if not ver.ok:
            self.root.after(0, lambda: self._set_busy(False, "Docker not available"))
            self._log(f"Docker not available. {ver.stderr.strip()}")
            self.root.after(0, lambda: messagebox.showerror(
                "Docker not available",
                "Không gọi được Docker CLI.\n\n"
                "Kiểm tra:\n- Docker Desktop đã chạy chưa?\n- `docker` có trong PATH chưa?\n\n"
                f"Command: {ver.cmd}\nError: {ver.stderr.strip()}"
            ))
            return
        if self.cancel.is_set():
            self.root.after(0, lambda: self._set_busy(False, "Stopped"))
            return

        ctx = run_docker("context show")
        server_version = ver.stdout.strip() or "(unknown)"
        ctx_name = ctx.stdout.strip() if ctx.ok else "(unknown)"
        self.root.after(0, lambda: self.env_var.set(f"Docker Server: {server_version} | Context: {ctx_name}"))

        # 2) docker system df -v
        df = run_docker("system df -v", timeout=240)
        self._log(f"docker system df -v rc={df.rc} ({df.duration_ms}ms)")
        if not df.ok:
            self.root.after(0, lambda: self._set_busy(False, "Error"))
            self.root.after(0, lambda: messagebox.showerror("Error", df.stderr or df.stdout))
            return

        summary, sections = parse_system_df(df.stdout)
        report = {
            "generated_at": _now_str(),
            "docker_server_version": server_version,
            "docker_context": ctx_name,
            "summary": summary,
            "sections": sections,
            "raw": {
                "system_df_v": df.stdout,
            },
        }
        self.last_report = report

        # Update summary labels
        self.root.after(0, lambda: self._apply_summary(summary))

        # Update tables
        self.root.after(0, lambda: self._fill_sections(sections))
        self.root.after(0, lambda: self.export_btn.config(state="normal"))
        self.root.after(0, lambda: self._set_busy(False, "Ready"))
        self._log("Refresh completed.")

    def _apply_summary(self, summary_rows):
        # best-effort mapping for the top cards
        rows = {r.get("TYPE") or r.get("Type") or r.get("type"): r for r in summary_rows}
        def fmt_row(label, r):
            if not r:
                return f"{label}: -"
            size = r.get("SIZE") or r.get("Size") or r.get("size") or "-"
            active = r.get("ACTIVE") or r.get("Active") or "-"
            total = r.get("TOTAL") or r.get("Total") or "-"
            reclaim = r.get("RECLAIMABLE") or r.get("Reclaimable") or "-"
            return f"{label}: total={total}, active={active}, size={size}, reclaimable={reclaim}"

        self.images_var.set(fmt_row("Images", rows.get("Images")))
        self.containers_var.set(fmt_row("Containers", rows.get("Containers")))
        self.volumes_var.set(fmt_row("Volumes", rows.get("Local Volumes")))
        self.build_var.set(fmt_row("Build Cache", rows.get("Build Cache")))

        # total reclaimable (pick max info)
        total_reclaim = "-"
        for r in summary_rows:
            rec = r.get("RECLAIMABLE") or r.get("Reclaimable")
            if rec:
                total_reclaim = rec
        self.total_var.set(f"Total reclaimable (rough): {total_reclaim}")

    def _fill_sections(self, sections):
        self._fill_tree(self.tab_images["tree"], self.tab_images["columns"], sections.get("Images", []), mapper=self._map_image_row)
        self._fill_tree(self.tab_containers["tree"], self.tab_containers["columns"], sections.get("Containers", []), mapper=self._map_container_row)
        self._fill_tree(self.tab_volumes["tree"], self.tab_volumes["columns"], sections.get("Local Volumes", []), mapper=self._map_volume_row)
        self._fill_tree(self.tab_build["tree"], self.tab_build["columns"], sections.get("Build Cache", []), mapper=self._map_build_row)

    def _fill_tree(self, tree: ttk.Treeview, columns, rows, mapper):
        tree.delete(*tree.get_children())
        for r in rows:
            vals = mapper(r, columns)
            tree.insert("", "end", values=vals)

    # ---------- row mapping (docker system df -v section rows) ----------

    def _map_image_row(self, r, cols):
        # headers usually: REPOSITORY TAG IMAGE ID CREATED SIZE SHARED SIZE UNIQUE SIZE CONTAINERS
        return (
            r.get("REPOSITORY", ""),
            r.get("TAG", ""),
            r.get("IMAGE ID", "") or r.get("IMAGE", "") or "",
            r.get("SIZE", ""),
            r.get("RECLAIMABLE", ""),
            r.get("SHARED SIZE", ""),
            r.get("UNIQUE SIZE", ""),
            r.get("CONTAINERS", ""),
        )

    def _map_container_row(self, r, cols):
        # headers usually: CONTAINER ID IMAGE COMMAND LOCAL VOLUMES SIZE CREATED STATUS NAMES
        return (
            r.get("CONTAINER ID", ""),
            r.get("IMAGE", ""),
            r.get("COMMAND", ""),
            r.get("LOCAL VOLUMES", ""),
            r.get("SIZE", ""),
            r.get("CREATED", ""),
            r.get("STATUS", ""),
            r.get("NAMES", ""),
        )

    def _map_volume_row(self, r, cols):
        # headers usually: VOLUME NAME LINKS SIZE RECLAIMABLE
        return (
            r.get("VOLUME NAME", "") or r.get("VOLUME", "") or "",
            r.get("LINKS", ""),
            r.get("SIZE", ""),
            r.get("RECLAIMABLE", ""),
        )

    def _map_build_row(self, r, cols):
        # headers usually: CACHE ID CACHE TYPE SIZE CREATED LAST USED USAGE COUNT IN USE
        return (
            r.get("CACHE ID", ""),
            r.get("CACHE TYPE", ""),
            r.get("SIZE", ""),
            r.get("LAST USED", "") or r.get("CREATED", ""),
            r.get("USAGE COUNT", ""),
            r.get("IN USE", ""),
        )

    # ---------- sorting ----------

    def sort_tree(self, tree: ttk.Treeview, col: str):
        items = list(tree.get_children(""))
        if not items:
            return

        # Determine sort type: size columns try parse bytes, else string
        col_idx = tree["columns"].index(col)

        def get_val(iid):
            vals = tree.item(iid, "values")
            v = vals[col_idx] if col_idx < len(vals) else ""
            return v

        def key_func(iid):
            v = get_val(iid)
            if col in ("size", "shared", "unique", "reclaimable"):
                return parse_size_to_bytes(v)
            if "size" in col.lower():
                return parse_size_to_bytes(v)
            return str(v).lower()

        # toggle direction
        asc = getattr(tree, "_sort_asc", True)
        tree._sort_asc = not asc

        items.sort(key=key_func, reverse=asc)
        for idx, iid in enumerate(items):
            tree.move(iid, "", idx)

    # ---------- destructive actions ----------

    def remove_selected(self, kind: str):
        if self.is_busy:
            return
        tab = {
            "Images": self.tab_images,
            "Containers": self.tab_containers,
            "Volumes": self.tab_volumes,
            "Build Cache": self.tab_build,
        }.get(kind)
        if not tab:
            return

        tree = tab["tree"]
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Info", "Chọn ít nhất 1 dòng.")
            return

        if kind == "Images":
            ids = []
            for iid in sel:
                vals = tree.item(iid, "values")
                # id column is 3rd
                ids.append(vals[2])
            msg = f"Xóa {len(ids)} image(s)?\n\n⚠ Có thể ảnh hưởng container đang dùng."
            if not messagebox.askyesno("Confirm", msg):
                return
            threading.Thread(target=self._remove_images, args=(ids,), daemon=True).start()
        elif kind == "Containers":
            ids = [tree.item(iid, "values")[0] for iid in sel]
            if not messagebox.askyesno("Confirm", f"Remove {len(ids)} container(s)?"):
                return
            threading.Thread(target=self._remove_containers, args=(ids,), daemon=True).start()
        elif kind == "Volumes":
            names = [tree.item(iid, "values")[0] for iid in sel]
            if not messagebox.askyesno("Confirm", f"Remove {len(names)} volume(s)?\n\n⚠ Dữ liệu sẽ mất."):
                return
            threading.Thread(target=self._remove_volumes, args=(names,), daemon=True).start()
        elif kind == "Build Cache":
            if not messagebox.askyesno("Confirm", "Prune build cache entries? (builder prune)"):
                return
            threading.Thread(target=self._prune_build_cache, daemon=True).start()

    def _remove_images(self, image_ids):
        self.cancel.clear()
        self.root.after(0, lambda: self._set_busy(True, "Removing images..."))
        for img in image_ids:
            if self.cancel.is_set():
                break
            res = run_docker(f"image rm {img}", timeout=240)
            self._log(f"docker image rm {img} rc={res.rc}")
            if not res.ok:
                self._log(res.stderr.strip() or res.stdout.strip())
        self.root.after(0, lambda: self._set_busy(False, "Ready"))
        self.root.after(0, self.refresh)

    def _remove_containers(self, container_ids):
        self.cancel.clear()
        self.root.after(0, lambda: self._set_busy(True, "Removing containers..."))
        for cid in container_ids:
            if self.cancel.is_set():
                break
            res = run_docker(f"rm -f {cid}", timeout=240)
            self._log(f"docker rm -f {cid} rc={res.rc}")
            if not res.ok:
                self._log(res.stderr.strip() or res.stdout.strip())
        self.root.after(0, lambda: self._set_busy(False, "Ready"))
        self.root.after(0, self.refresh)

    def _remove_volumes(self, volume_names):
        self.cancel.clear()
        self.root.after(0, lambda: self._set_busy(True, "Removing volumes..."))
        for v in volume_names:
            if self.cancel.is_set():
                break
            res = run_docker(f"volume rm {v}", timeout=240)
            self._log(f"docker volume rm {v} rc={res.rc}")
            if not res.ok:
                self._log(res.stderr.strip() or res.stdout.strip())
        self.root.after(0, lambda: self._set_busy(False, "Ready"))
        self.root.after(0, self.refresh)

    def _prune_build_cache(self):
        self.cancel.clear()
        self.root.after(0, lambda: self._set_busy(True, "Pruning build cache..."))
        until = (self.prune_until.get() or "24h").strip()
        res = run_docker(f"builder prune -f --filter until={until}", timeout=600)
        self._log(f"docker builder prune rc={res.rc}")
        if not res.ok:
            self._log(res.stderr.strip() or res.stdout.strip())
        self.root.after(0, lambda: self._set_busy(False, "Ready"))
        self.root.after(0, self.refresh)

    # ---------- prune planner ----------

    def simulate_prune(self):
        if self.is_busy:
            return
        self.prune_output.delete("1.0", tk.END)
        self.prune_output.insert(tk.END, "== Current docker system df -v ==\n")
        df = run_docker("system df -v", timeout=240)
        if df.ok:
            self.prune_output.insert(tk.END, df.stdout)
        else:
            self.prune_output.insert(tk.END, df.stderr or df.stdout)

    def run_prune(self):
        if self.is_busy:
            return

        a = self.prune_a.get()
        vols = self.prune_volumes.get()
        build = self.prune_build.get()
        until = (self.prune_until.get() or "24h").strip()

        parts = ["system prune -f"]
        if a:
            parts.append("-a")
        if vols:
            parts.append("--volumes")

        msg = (
            "Bạn sắp chạy:\n\n"
            f"docker {' '.join(parts)}\n"
            + (f"docker builder prune -f --filter until={until}\n" if build else "")
            + "\n⚠ Đây là hành động xóa dữ liệu Docker (không hoàn tác)."
        )

        if not messagebox.askyesno("Confirm prune", msg):
            return

        threading.Thread(target=self._run_prune_worker, args=(parts, build, until), daemon=True).start()

    def _run_prune_worker(self, system_prune_parts, build: bool, until: str):
        self.cancel.clear()
        self.root.after(0, lambda: self._set_busy(True, "Running prune..."))
        self.root.after(0, lambda: self.prune_output.delete("1.0", tk.END))

        cmd1 = " ".join(system_prune_parts)
        r1 = run_docker(cmd1.replace("docker ", ""), timeout=600) if cmd1.startswith("docker ") else run_docker(cmd1, timeout=600)
        # ensure proper cmd string for log
        self._log(f"docker {cmd1} rc={r1.rc}")
        self.root.after(0, lambda: self.prune_output.insert(tk.END, f"$ docker {cmd1}\n{r1.stdout}\n{r1.stderr}\n"))

        if build and not self.cancel.is_set():
            r2 = run_docker(f"builder prune -f --filter until={until}", timeout=600)
            self._log(f"docker builder prune rc={r2.rc}")
            self.root.after(0, lambda: self.prune_output.insert(tk.END, f"$ docker builder prune -f --filter until={until}\n{r2.stdout}\n{r2.stderr}\n"))

        self.root.after(0, lambda: self._set_busy(False, "Ready"))
        self.root.after(0, self.refresh)

    # ---------- export ----------

    def export_report(self):
        if not self.last_report:
            messagebox.showinfo("Info", "No report yet. Click Refresh first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save report",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("All files", "*.*")],
            initialfile=f"docker_storage_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.last_report, f, ensure_ascii=False, indent=2)
            self._log(f"Report exported: {path}")
            messagebox.showinfo("OK", f"Saved report to:\n{path}")
        except Exception as e:
            messagebox.showerror("Error", str(e))


# ---------------- RUN ----------------


def main():
    root = tk.Tk()
    app = DockerStorageManager(root)
    root.mainloop()


if __name__ == "__main__":
    main()

