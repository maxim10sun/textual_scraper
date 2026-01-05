# repo_ui/__main__.py
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .loader import load_repo
from .mirror_builder import build_mirrors
from .css_index_builder import build_css_index  # <-- ADD
from .layer3_pass1 import build_layer3_pass1


def _make_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_run_stamp(repo_root: Path, run_id: str, summary: dict) -> None:
    out_dir = repo_root / "shadow_ui" / "layer4"
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / "run.json"
    payload = {
        "run_id": run_id,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_root": repo_root.as_posix(),
        "summary": summary,
    }
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    print("[repo_ui] starting")

    repo_root = Path.cwd()
    print(f"[repo_ui] repo_root = {repo_root}")

    run_id = _make_run_id()
    print(f"[repo_ui] run_id = {run_id}")

    # Phase 1: load
    files_cache = load_repo(repo_root)
    print("[repo_ui] loader finished")

    # Phase 2: ring0 mirrors (+ meta)
    mirror_cache = build_mirrors(repo_root, files_cache, run_id=run_id)
    print(f"[repo_ui] mirror build finished  mirror_cache={len(mirror_cache)}")

    # Phase 2.5: css index (reads from mirror_cache)
    css_index = build_css_index(repo_root=repo_root, mirror_cache=mirror_cache, run_id=run_id)
    print(f"[repo_ui] css index finished  rules={css_index.get('rules_count')}")

    # Phase 3: layer3 pass1 tiles (reads from mirrors)
    build_layer3_pass1(repo_root)
    print("[repo_ui] layer3 pass1 finished  wrote shadow_ui/layer3")

    summary = {
        "files_loaded": len(files_cache),
        "mirrors_written": len(mirror_cache),
        "css_index": (repo_root / "shadow_ui" / "layer4" / "mirror" / "css_index.json").as_posix(),
        "css_rules_count": css_index.get("rules_count", None),
        "layer3_tiles_dir": (repo_root / "shadow_ui" / "layer3" / "tiles").as_posix(),
        "layer3_index": (repo_root / "shadow_ui" / "layer3" / "index.json").as_posix(),
    }
    _write_run_stamp(repo_root, run_id, summary)
    print("[repo_ui] wrote shadow_ui/layer4/run.json")


if __name__ == "__main__":
    main()
