# Tool Clean Windows (Python)

Small collection of Python desktop tools for Windows (and one Ubuntu variant) to inspect and clean storage-related data.

## Requirements

- Python 3.10+ recommended
- **Windows**: built-in `tkinter` (usually included with Python on Windows)
- **Docker tools**: Docker CLI available (`docker` in PATH) and Docker daemon running

## Tools

### `appdata_manager.py` (Windows)

Scan `%AppData%` (Local/Roaming/LocalLow), show folder sizes, open folder, delete selected folders, search/filter, multi-delete, and basic statistics.

Run:

```bash
python appdata_manager.py
```

### `app_manager.py` (Windows)

Application manager that:

- Lists installed apps from registry
- Estimates per-app AppData usage using a one-time AppData index (safer matching + preview before deletion)
- Uninstall app (normal) and optionally delete selected AppData folders (with preview)
- “Uninstall complete” with preview-based cleanup (install folder + AppData + uninstall registry key)

Run:

```bash
python app_manager.py
```

### `startup_manager.py` (Windows)

Deep startup manager:

- Sources: Registry Run/RunOnce/Policies, Startup folders, Scheduled Tasks (logon/boot/startup triggers), Auto services
- Actions: add, enable/disable, delete
- Safety: snapshots and a Disabled Store for reversible disable operations

Run:

```bash
python startup_manager.py
```

### `docker_storage_manager.py` (Windows)

Docker storage manager UI:

- Parses `docker system df -v`
- Lists Images/Containers/Volumes/Build Cache
- Remove selected resources
- Safe prune workflow with confirmations
- Export JSON report

Run:

```bash
python docker_storage_manager.py
```

### `docker_storage_manager_ubuntu.py` (Ubuntu)

Ubuntu/Linux variant of the Docker storage manager UI (same core features).

Run:

```bash
python3 docker_storage_manager_ubuntu.py
```

## Notes / Safety

- Deleting AppData, startup entries, scheduled tasks, services, Docker volumes, and pruning Docker data can cause data loss or break apps.
- Prefer **disable** over **delete** when unsure, and always review previews/snapshots.

