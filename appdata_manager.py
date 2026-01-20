import os
import shutil
import threading
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog
import subprocess
import re

# ---------------- CORE ----------------


def get_folder_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except:
                pass
    return total


def bytes_to_human(size):
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} PB"


def human_to_bytes(size_str):
    """Convert human readable size to bytes"""
    size_str = size_str.strip().upper()
    multipliers = {"B": 1, "KB": 1024, "MB": 1024 **
                   2, "GB": 1024**3, "TB": 1024**4}

    for unit, mult in multipliers.items():
        if size_str.endswith(unit):
            try:
                num = float(size_str[:-len(unit)])
                return int(num * mult)
            except:
                pass
    return 0

# ---------------- GUI ----------------


class AppDataManager:
    def __init__(self, root):
        self.root = root
        root.title("AppData Storage Manager (Python)")
        root.geometry("900x600")

        # Variables
        self.is_scanning = False
        self.scan_progress = {"current": 0, "total": 0, "current_folder": ""}
        self.all_results = []  # Store all scan results for filtering
        self.tree_data = {}  # Store item_id -> (size_bytes, path) mapping

        # Top frame with buttons
        top = ttk.Frame(root)
        top.pack(fill="x", padx=5, pady=5)

        btn_frame = ttk.Frame(top)
        btn_frame.pack(side="left")

        self.scan_btn = ttk.Button(
            btn_frame, text="🔍 Scan AppData", command=self.scan)
        self.scan_btn.pack(side="left", padx=2)

        self.refresh_btn = ttk.Button(
            btn_frame, text="🔄 Refresh Selected", command=self.refresh_selected)
        self.refresh_btn.pack(side="left", padx=2)

        self.delete_btn = ttk.Button(
            btn_frame, text="🗑 Delete Selected", command=self.delete_selected)
        self.delete_btn.pack(side="left", padx=2)

        self.delete_multi_btn = ttk.Button(
            btn_frame, text="🗑 Delete Multiple", command=self.delete_multiple)
        self.delete_multi_btn.pack(side="left", padx=2)

        stats_btn = ttk.Button(
            btn_frame, text="📊 Statistics", command=self.show_statistics)
        stats_btn.pack(side="left", padx=2)

        # Search and filter frame
        filter_frame = ttk.LabelFrame(root, text="Search & Filter", padding=5)
        filter_frame.pack(fill="x", padx=5, pady=2)

        search_frame = ttk.Frame(filter_frame)
        search_frame.pack(fill="x", pady=2)

        ttk.Label(search_frame, text="Search:").pack(side="left", padx=2)
        self.search_var = tk.StringVar()
        self.search_var.trace("w", lambda *args: self.apply_filters())
        search_entry = ttk.Entry(
            search_frame, textvariable=self.search_var, width=30)
        search_entry.pack(side="left", padx=2)

        ttk.Label(search_frame, text="Min Size:").pack(
            side="left", padx=(10, 2))
        self.size_filter_var = tk.StringVar()
        self.size_filter_var.trace("w", lambda *args: self.apply_filters())
        size_entry = ttk.Entry(
            search_frame, textvariable=self.size_filter_var, width=15)
        size_entry.pack(side="left", padx=2)
        ttk.Label(search_frame, text="(e.g., 100MB, 1GB)", font=(
            "Arial", 7), foreground="gray").pack(side="left", padx=2)

        clear_btn = ttk.Button(
            search_frame, text="Clear Filters", command=self.clear_filters)
        clear_btn.pack(side="left", padx=5)

        # Status frame
        status_frame = ttk.Frame(root)
        status_frame.pack(fill="x", padx=5, pady=2)

        self.status_label = ttk.Label(
            status_frame, text="Ready", font=("Arial", 9))
        self.status_label.pack(side="left", padx=5)

        self.progress_bar = ttk.Progressbar(
            status_frame, mode="indeterminate", length=200)
        self.progress_bar.pack(side="right", padx=5)

        # Info label
        self.info_label = ttk.Label(
            root, text="", font=("Arial", 8), foreground="gray")
        self.info_label.pack(fill="x", padx=5, pady=2)

        # Treeview
        tree_frame = ttk.Frame(root)
        tree_frame.pack(fill="both", expand=True, padx=5, pady=5)

        self.tree = ttk.Treeview(
            tree_frame,
            columns=("size", "path"),
            show="headings",
            selectmode="extended"  # Allow multiple selection
        )
        self.tree.heading("size", text="Size",
                          command=lambda: self.sort_by_column("size"))
        self.tree.heading("path", text="Path",
                          command=lambda: self.sort_by_column("path"))
        self.tree.column("size", width=120, anchor="center")
        self.tree.column("path", width=750)

        scrollbar = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self.open_folder)
        # Right-click menu
        self.tree.bind("<Button-3>", self.show_context_menu)
        self.sort_reverse = {"size": False, "path": False}

        # Log area
        log_frame = ttk.LabelFrame(root, text="Log", padding=5)
        log_frame.pack(fill="x", padx=5, pady=5)

        self.log = tk.Text(log_frame, height=5, font=("Consolas", 8))
        self.log.pack(fill="both", expand=True)

        # Context menu
        self.context_menu = tk.Menu(root, tearoff=0)
        self.context_menu.add_command(
            label="Open Folder", command=self.open_selected_folder)
        self.context_menu.add_command(
            label="Refresh Size", command=self.refresh_selected)
        self.context_menu.add_command(
            label="Delete", command=self.delete_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(
            label="View Details", command=self.view_details)

    def log_msg(self, msg):
        self.log.insert(tk.END, msg + "\n")
        self.log.see(tk.END)

    def update_status(self, text):
        """Thread-safe status update"""
        self.root.after(0, lambda: self.status_label.config(text=text))

    def update_info(self, text):
        """Thread-safe info update"""
        self.root.after(0, lambda: self.info_label.config(text=text))

    def scan(self):
        if self.is_scanning:
            return
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self):
        self.is_scanning = True
        self.root.after(0, lambda: self.scan_btn.config(state="disabled"))
        self.root.after(0, lambda: self.delete_btn.config(state="disabled"))
        self.root.after(0, lambda: self.refresh_btn.config(state="disabled"))
        self.root.after(
            0, lambda: self.delete_multi_btn.config(state="disabled"))
        self.root.after(0, lambda: self.progress_bar.start(10))

        self.tree.delete(*self.tree.get_children())
        self.log_msg("=" * 50)
        self.log_msg("Starting AppData scan...")
        self.update_status("Scanning... Please wait")

        base = os.path.expanduser("~")
        appdata_paths = [
            os.path.join(base, "AppData", "Local"),
            os.path.join(base, "AppData", "Roaming"),
            os.path.join(base, "AppData", "LocalLow"),
        ]

        results = []
        total_folders = 0

        for idx, root_path in enumerate(appdata_paths):
            if not os.path.exists(root_path):
                continue

            path_name = os.path.basename(root_path)
            self.update_status(f"Scanning {path_name}...")
            self.log_msg(f"Scanning {path_name}...")

            folders = [f for f in os.listdir(root_path)
                       if os.path.isdir(os.path.join(root_path, f))]
            total_folders += len(folders)

            for folder_idx, folder in enumerate(folders):
                full = os.path.join(root_path, folder)
                if os.path.isdir(full):
                    self.update_status(f"Calculating size: {folder}...")
                    self.scan_progress["current_folder"] = folder
                    size = get_folder_size(full)
                    results.append((size, full))

                    # Update info periodically
                    if folder_idx % 5 == 0:
                        self.update_info(f"Found {len(results)} folders...")

        self.update_status("Sorting results...")
        self.log_msg("Sorting results by size...")
        results.sort(reverse=True)

        self.update_status("Loading results...")
        total_size = 0
        self.all_results = results.copy()  # Store for filtering
        self.tree_data.clear()

        for size, path in results:
            item_id = self.tree.insert(
                "", "end", values=(bytes_to_human(size), path))
            self.tree_data[item_id] = (size, path)
            total_size += size

        # Finalize
        self.root.after(0, lambda: self.progress_bar.stop())
        self.root.after(0, lambda: self.scan_btn.config(state="normal"))
        self.root.after(0, lambda: self.delete_btn.config(state="normal"))
        self.root.after(0, lambda: self.refresh_btn.config(state="normal"))
        self.root.after(
            0, lambda: self.delete_multi_btn.config(state="normal"))
        self.root.after(0, self.apply_filters)  # Apply any existing filters

        self.update_status(f"Ready - Found {len(results)} folders")
        self.update_info(
            f"Total: {len(results)} folders | Total size: {bytes_to_human(total_size)}")
        self.log_msg(
            f"Scan completed. Found {len(results)} folders ({bytes_to_human(total_size)})")
        self.log_msg("=" * 50)

        self.is_scanning = False

    def open_folder(self, event):
        item = self.tree.selection()
        if not item:
            return
        path = self.tree.item(item, "values")[1]
        subprocess.Popen(f'explorer "{path}"')

    def apply_filters(self):
        """Apply search and size filters"""
        search_text = self.search_var.get().lower()
        min_size_str = self.size_filter_var.get().strip()
        min_size_bytes = human_to_bytes(min_size_str) if min_size_str else 0

        # Clear current display
        for item in self.tree.get_children():
            self.tree.delete(item)

        # Filter and display
        filtered_count = 0
        total_size = 0

        for size_bytes, path in self.all_results:
            folder_name = os.path.basename(path).lower()

            # Search filter
            if search_text and search_text not in folder_name and search_text not in path.lower():
                continue

            # Size filter
            if min_size_bytes > 0 and size_bytes < min_size_bytes:
                continue

            # Add to tree
            item_id = self.tree.insert("", "end", values=(
                bytes_to_human(size_bytes), path))
            self.tree_data[item_id] = (size_bytes, path)
            filtered_count += 1
            total_size += size_bytes

        self.update_info(
            f"Showing {filtered_count} of {len(self.all_results)} folders | Total: {bytes_to_human(total_size)}")

    def clear_filters(self):
        """Clear all filters"""
        self.search_var.set("")
        self.size_filter_var.set("")
        self.apply_filters()

    def sort_by_column(self, column):
        """Sort tree by column"""
        items = [(self.tree.set(item, column), item)
                 for item in self.tree.get_children("")]

        if column == "size":
            # Sort by actual bytes
            items.sort(key=lambda x: self.tree_data.get(
                x[1], (0, ""))[0], reverse=self.sort_reverse["size"])
        else:
            items.sort(key=lambda x: x[0].lower(),
                       reverse=self.sort_reverse[column])

        self.sort_reverse[column] = not self.sort_reverse[column]

        # Rearrange items
        for index, (val, item) in enumerate(items):
            self.tree.move(item, "", index)

    def refresh_selected(self):
        """Refresh size of selected folder(s)"""
        if self.is_scanning:
            messagebox.showwarning(
                "Warning", "Please wait for scan to complete.")
            return

        items = self.tree.selection()
        if not items:
            messagebox.showinfo("Info", "Please select folder(s) to refresh.")
            return

        if len(items) > 1:
            if not messagebox.askyesno("Confirm", f"Refresh size of {len(items)} folders? This may take a while."):
                return

        def refresh_thread():
            self.root.after(0, lambda: self.update_status(
                "Refreshing sizes..."))
            self.root.after(0, lambda: self.scan_btn.config(state="disabled"))
            self.root.after(
                0, lambda: self.refresh_btn.config(state="disabled"))

            for item in items:
                if item not in self.tree_data:
                    continue

                old_size, path = self.tree_data[item]
                self.root.after(0, lambda p=path: self.update_status(
                    f"Refreshing: {os.path.basename(p)}..."))

                try:
                    new_size = get_folder_size(path)
                    self.root.after(0, lambda i=item, s=new_size,
                                    p=path: self._update_item_size(i, s, p))
                except Exception as e:
                    self.log_msg(f"✗ Error refreshing {path}: {e}")

            self.root.after(0, lambda: self.scan_btn.config(state="normal"))
            self.root.after(0, lambda: self.refresh_btn.config(state="normal"))
            self.root.after(0, lambda: self.update_status(
                "Ready - Refresh completed"))
            # Re-apply filters after refresh
            self.root.after(0, self.apply_filters)

        threading.Thread(target=refresh_thread, daemon=True).start()

    def _update_item_size(self, item_id, new_size, path):
        """Update item size in tree and data"""
        self.tree.item(item_id, values=(bytes_to_human(new_size), path))
        self.tree_data[item_id] = (new_size, path)

        # Update in all_results
        for idx, (size, p) in enumerate(self.all_results):
            if p == path:
                self.all_results[idx] = (new_size, path)
                break

    def delete_selected(self):
        """Delete single selected folder"""
        if self.is_scanning:
            messagebox.showwarning(
                "Warning", "Please wait for scan to complete.")
            return

        items = self.tree.selection()
        if not items:
            messagebox.showinfo("Info", "Please select a folder to delete.")
            return

        if len(items) > 1:
            self.delete_multiple()
            return

        item = items[0]
        if item not in self.tree_data:
            return

        size_bytes, path = self.tree_data[item]
        size = bytes_to_human(size_bytes)

        if not messagebox.askyesno(
            "Confirm Delete",
            f"Delete folder?\n\n{path}\n\nSize: {size}\n\n⚠ This cannot be undone!"
        ):
            return

        self._delete_folders([(item, path)])

    def delete_multiple(self):
        """Delete multiple selected folders"""
        if self.is_scanning:
            messagebox.showwarning(
                "Warning", "Please wait for scan to complete.")
            return

        items = self.tree.selection()
        if not items:
            messagebox.showinfo("Info", "Please select folder(s) to delete.")
            return

        if len(items) == 1:
            self.delete_selected()
            return

        # Calculate total size
        total_size = 0
        folders_to_delete = []
        for item in items:
            if item in self.tree_data:
                size_bytes, path = self.tree_data[item]
                total_size += size_bytes
                folders_to_delete.append((item, path))

        if not messagebox.askyesno(
            "Confirm Delete Multiple",
            f"Delete {len(folders_to_delete)} folders?\n\nTotal size: {bytes_to_human(total_size)}\n\n⚠ This cannot be undone!"
        ):
            return

        self._delete_folders(folders_to_delete)

    def _delete_folders(self, folders):
        """Delete folders (internal method)"""
        self.update_status("Deleting...")
        self.scan_btn.config(state="disabled")
        self.delete_btn.config(state="disabled")
        self.delete_multi_btn.config(state="disabled")

        deleted_count = 0
        failed_count = 0

        for item, path in folders:
            try:
                shutil.rmtree(path)
                self.tree.delete(item)
                if item in self.tree_data:
                    del self.tree_data[item]
                # Remove from all_results
                self.all_results = [(s, p)
                                    for s, p in self.all_results if p != path]
                self.log_msg(f"✓ Deleted: {path}")
                deleted_count += 1
            except Exception as e:
                error_msg = f"Error deleting {path}: {e}"
                self.log_msg(f"✗ {error_msg}")
                failed_count += 1

        self.update_status(
            f"Ready - Deleted {deleted_count}, Failed {failed_count}")

        # Update info
        remaining_items = len(self.tree.get_children())
        self.update_info(f"Remaining: {remaining_items} folders")

        self.scan_btn.config(state="normal")
        self.delete_btn.config(state="normal")
        self.delete_multi_btn.config(state="normal")
        self.apply_filters()  # Refresh display

    def show_context_menu(self, event):
        """Show right-click context menu"""
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def open_selected_folder(self):
        """Open selected folder from context menu"""
        item = self.tree.selection()
        if item:
            self.open_folder(None)

    def view_details(self):
        """View detailed information about selected folder"""
        items = self.tree.selection()
        if not items:
            return

        item = items[0]
        if item not in self.tree_data:
            return

        size_bytes, path = self.tree_data[item]

        # Count files and subdirectories
        file_count = 0
        dir_count = 0
        try:
            for root, dirs, files in os.walk(path):
                file_count += len(files)
                dir_count += len(dirs)
        except:
            pass

        details = f"""Folder Details:

Path: {path}
Size: {bytes_to_human(size_bytes)}
Files: {file_count:,}
Subdirectories: {dir_count:,}
Parent: {os.path.dirname(path)}"""

        messagebox.showinfo("Folder Details", details)

    def show_statistics(self):
        """Show detailed statistics about scanned folders"""
        if not self.all_results:
            messagebox.showinfo("Info", "Please scan AppData first.")
            return

        total_folders = len(self.all_results)
        total_size = sum(size for size, _ in self.all_results)

        if total_folders == 0:
            messagebox.showinfo("Statistics", "No folders found.")
            return

        # Top 10 largest
        sorted_results = sorted(self.all_results, reverse=True)[:10]
        top_10 = "\n".join([f"{i+1}. {os.path.basename(path)} - {bytes_to_human(size)}"
                           for i, (size, path) in enumerate(sorted_results)])

        # Size distribution
        large = sum(1 for size, _ in self.all_results if size >=
                    1024**3)  # >= 1GB
        medium = sum(1 for size, _ in self.all_results if 1024 **
                     2 <= size < 1024**3)  # 1MB - 1GB
        small = sum(1 for size, _ in self.all_results if size <
                    1024**2)  # < 1MB

        # Average size
        avg_size = total_size / total_folders if total_folders > 0 else 0

        stats = f"""📊 AppData Statistics

Total Folders: {total_folders:,}
Total Size: {bytes_to_human(total_size)}
Average Size: {bytes_to_human(avg_size)}

Size Distribution:
  Large (≥1GB): {large} folders
  Medium (1MB-1GB): {medium} folders
  Small (<1MB): {small} folders

Top 10 Largest Folders:
{top_10}"""

        messagebox.showinfo("Statistics", stats)

# ---------------- RUN ----------------


if __name__ == "__main__":
    root = tk.Tk()
    app = AppDataManager(root)
    root.mainloop()
