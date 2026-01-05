import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List


def load_repo(repo_root: Path) -> List[Dict[str, Any]]:
    """
    Loads .py and .tcss sources from include roots in folders.json.

    Emits a per-file cache dict containing:
      - abs_path, rel_path, ext
      - text (decoded with utf-8, errors="replace")
      - source_sha1: sha1 over raw file bytes (stable identity)
      - source_bytes, source_mtime_ns
      - encoding, read_errors_mode
    """
    print("[loader] reading folders.json")

    folders_json = repo_root / "folders.json"
    data = json.loads(folders_json.read_text(encoding="utf-8"))

    include_roots = data.get("include_root_folders", [])
    ignore_dirs = set(data.get("ignore_dir_names", []))

    print(f"[loader] include_root_folders = {include_roots}")
    print(f"[loader] ignore_dir_names = {len(ignore_dirs)} entries")

    files_cache: List[Dict[str, Any]] = []

    def walk(dir_path: Path) -> None:
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name in ignore_dirs:
                            continue
                        walk(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        name = entry.name.lower()
                        if name.endswith(".py") or name.endswith(".tcss"):
                            p = Path(entry.path)

                            # Read raw bytes once to derive stable identity.
                            try:
                                raw = p.read_bytes()
                            except OSError:
                                continue

                            source_sha1 = hashlib.sha1(raw).hexdigest()
                            source_bytes = len(raw)

                            # Decode for AST/text processing (lossy but explicit).
                            encoding = "utf-8"
                            read_errors_mode = "replace"
                            text = raw.decode(encoding, errors=read_errors_mode)

                            try:
                                st = p.stat()
                                mtime_ns = getattr(st, "st_mtime_ns", None)
                            except OSError:
                                mtime_ns = None

                            files_cache.append(
                                {
                                    "abs_path": p,
                                    "rel_path": p.relative_to(repo_root),
                                    "ext": p.suffix,
                                    "text": text,
                                    # provenance / identity
                                    "source_sha1": source_sha1,
                                    "source_bytes": source_bytes,
                                    "source_mtime_ns": mtime_ns,
                                    "encoding": encoding,
                                    "read_errors_mode": read_errors_mode,
                                }
                            )
        except PermissionError:
            pass

    print("[loader] scanning filesystem...")

    for root in include_roots:
        root_path = repo_root / root
        if root_path.exists() and root_path.is_dir():
            walk(root_path)

    py_count = sum(1 for f in files_cache if f["ext"] == ".py")
    tcss_count = sum(1 for f in files_cache if f["ext"] == ".tcss")

    print(f"[loader] done  files={len(files_cache)}  py={py_count}  tcss={tcss_count}")

    return files_cache
