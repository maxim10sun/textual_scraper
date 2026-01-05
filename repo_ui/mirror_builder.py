from __future__ import annotations

import ast
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

def build_mirrors(
    repo_root: Path,
    files_cache: List[Dict[str, Any]],
    *,
    run_id: str | None = None,
) -> Dict[str, Dict[str, Any]]:
    """
    Ring0 mirror builder + provenance sidecars.

    Ring0 membership:
      - Any .tcss file (always)
      - Any .py file that contains at least one AST ImportFrom whose module starts with "textual"

    For Ring0 .py mirrors:
      - Carry over ONLY top-level "from textual..." import statements whose bound names are USED in the file.
      - Copy whole top-level blocks (class/def/async def/assign/annassign) that reference any imported Textual name.
      - Record provenance (file + start/end lines + sha1) for both imports and blocks in meta.json.
    """

    def log(msg: str) -> None:
        print(f"[mirror] {msg}")

    if run_id is None:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    mirror_root = repo_root / "shadow_ui" / "layer4" / "mirror"
    mirror_cache: Dict[str, Dict[str, Any]] = {}

    log(f"starting mirror build  run_id={run_id}")

    # ----------------------------
    # Phase 1: mirror all .tcss verbatim (Ring0 membership)
    # ----------------------------
    tcss_files = [f for f in files_cache if (f.get("ext") or "").lower() == ".tcss"]
    log(f"mirroring tcss files ({len(tcss_files)})")

    for f in tcss_files:
        src_rel: Path = f["rel_path"]
        rel_md = src_rel.with_suffix(src_rel.suffix + ".md")
        content = f.get("text", "")

        meta = _base_meta(
            run_id=run_id,
            kind="tcss",
            source_rel=src_rel.as_posix(),
            mirror_rel=rel_md.as_posix(),
            file_info=_file_info_from_cache(f),
            extractor={
                "name": "mirror_builder",
                "phase": "layer4_mirror",
                "rule": "tcss_verbatim",
            },
        )
        meta["snippets"] = [
            {
                "snippet_kind": "tcss_file",
                "start_line": 1,
                "end_line": _line_count(content),
                "snippet_sha1": _sha1_text(content),
                "snippet_len_chars": len(content),
            }
        ]

        write_mirror_with_meta(mirror_root, rel_md, content, meta)

        key = src_rel.as_posix()
        mirror_cache[key] = {
            "kind": "tcss",
            "source_rel": key,
            "mirror_rel": rel_md.as_posix(),
            "content": content,
            "meta_rel": (rel_md.as_posix() + ".meta.json"),
        }

    log("tcss mirroring done")

    # ----------------------------
    # Phase 2: mirror Ring0 python files (AST-detected "from textual...")
    # ----------------------------
    py_files = [f for f in files_cache if (f.get("ext") or "").lower() == ".py"]
    log(f"scanning python files for ring0 membership ({len(py_files)})")

    ring0_candidates: List[Tuple[Dict[str, Any], Set[str], int]] = []
    parse_errors = 0

    for f in py_files:
        text = f.get("text", "")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            parse_errors += 1
            continue

        imported_names = _collect_textual_imported_names(tree)
        if imported_names:
            ring0_candidates.append((f, imported_names, len(imported_names)))

    log(f"ring-0 python files detected ({len(ring0_candidates)})  parse_errors={parse_errors}")

    written_py = 0
    skipped_no_output = 0

    for f, imported_names, _n in ring0_candidates:
        text = f.get("text", "")
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue

        # Collect *all* Name ids used anywhere in file.
        used_name_ids: Set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                used_name_ids.add(node.id)

        # --- Carry over ONLY used "from textual..." imports (top-level only) ---
        import_stmts: List[str] = []
        import_meta: List[Dict[str, Any]] = []

        for node in tree.body:
            if not isinstance(node, ast.ImportFrom):
                continue

            mod = node.module or ""
            if not (mod == "textual" or mod.startswith("textual.")):
                continue

            # Names bound by this import statement
            bound = [(a.asname or a.name) for a in node.names if a.name != "*"]

            # Keep the statement only if at least one bound name is used somewhere.
            if not any(name in used_name_ids for name in bound):
                continue

            src = _source_for_node(text, node)
            if not src:
                continue

            ln = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", None)
            if not (isinstance(ln, int) and isinstance(end, int) and end >= ln):
                ln, end = _lineno_span_fallback(text, src)

            import_stmts.append(src)
            import_meta.append(
                {
                    "snippet_kind": "py_import_textual_used",
                    "node_kind": type(node).__name__,
                    "module": mod,
                    "bound_names": bound,
                    "used_bound_names": sorted([n for n in bound if n in used_name_ids]),
                    "start_line": ln,
                    "end_line": end,
                    "snippet_sha1": _sha1_text(src),
                    "snippet_len_chars": len(src),
                }
            )

        # --- Existing behavior: capture relevant top-level blocks ---
        blocks: List[str] = []
        block_meta: List[Dict[str, Any]] = []

        for node in tree.body:
            if _is_top_level_block(node) and _node_uses_any_name(node, imported_names):
                src = _source_for_node(text, node)
                if not src:
                    continue

                ln = getattr(node, "lineno", None)
                end = getattr(node, "end_lineno", None)
                if not (isinstance(ln, int) and isinstance(end, int) and end >= ln):
                    ln, end = _lineno_span_fallback(text, src)

                blocks.append(src)
                block_meta.append(
                    {
                        "snippet_kind": "py_top_level_block",
                        "node_kind": type(node).__name__,
                        "start_line": ln,
                        "end_line": end,
                        "snippet_sha1": _sha1_text(src),
                        "snippet_len_chars": len(src),
                    }
                )

        # If nothing to write, skip
        if not import_stmts and not blocks:
            skipped_no_output += 1
            continue

        src_rel: Path = f["rel_path"]
        rel_md = src_rel.with_suffix(src_rel.suffix + ".md")

        parts: List[str] = []
        if import_stmts:
            parts.append("\n".join(import_stmts).rstrip())
        if blocks:
            parts.append("\n\n".join(blocks).rstrip())
        content = "\n\n".join([p for p in parts if p]).rstrip() + "\n"

        meta = _base_meta(
            run_id=run_id,
            kind="py",
            source_rel=src_rel.as_posix(),
            mirror_rel=rel_md.as_posix(),
            file_info=_file_info_from_cache(f),
            extractor={
                "name": "mirror_builder",
                "phase": "layer4_mirror",
                "rule": "ring0_textual_imports_used_plus_blocks",
            },
        )
        meta["textual_imports"] = sorted(imported_names)

        meta["imports"] = {
            "count": len(import_stmts),
            "snippets": import_meta,
        }
        meta["blocks"] = {
            "count": len(blocks),
            "snippets": block_meta,
        }

        meta["imports_count"] = len(import_stmts)
        meta["blocks_count"] = len(blocks)

        meta["snippets"] = import_meta + block_meta
        meta["mirror_sha1"] = _sha1_text(content)
        meta["mirror_len_chars"] = len(content)

        write_mirror_with_meta(mirror_root, rel_md, content, meta)
        written_py += 1

        key = src_rel.as_posix()
        mirror_cache[key] = {
            "kind": "py",
            "source_rel": key,
            "mirror_rel": rel_md.as_posix(),
            "content": content,
            "textual_imports": sorted(imported_names),
            "imports_count": len(import_stmts),
            "blocks_count": len(blocks),
            "meta_rel": (rel_md.as_posix() + ".meta.json"),
        }

    log(
        "done  "
        f"mirrors_written={len(mirror_cache)}  "
        f"(tcss={len(tcss_files)} py={written_py})  "
        f"ring0_py_no_output={skipped_no_output}  "
        f"py_parse_errors={parse_errors}"
    )

    return mirror_cache

# ----------------------------
# AST helpers
# ----------------------------

def _collect_textual_imported_names(tree: ast.AST) -> Set[str]:
    """
    Collect *locally bound* imported names from any ImportFrom where module startswith "textual".
    Works even if the import is inside try/except, if blocks, etc.
    """
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "textual" or mod.startswith("textual."):
                for alias in node.names:
                    names.add(alias.asname or alias.name)
    return names


def _is_top_level_block(node: ast.AST) -> bool:
    """
    What we consider "carry-over-able" in Ring0 mirrors:
      - class defs
      - function defs (sync + async)
      - assignments (incl annotated assignments)
    """
    return isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Assign, ast.AnnAssign))


def _node_uses_any_name(node: ast.AST, names: Set[str]) -> bool:
    """
    True if node's subtree references any of the given names as a Name identifier.
    """
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id in names:
            return True
    return False


def _source_for_node(text: str, node: ast.AST) -> str:
    """
    Get exact source for a node. Prefer ast.get_source_segment, fallback to lineno/end_lineno slice.
    """
    seg = ast.get_source_segment(text, node)
    if seg is not None:
        return seg

    ln = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if isinstance(ln, int) and isinstance(end, int) and end >= ln:
        lines = text.splitlines()
        return "\n".join(lines[ln - 1 : end])

    return ""


def _lineno_span_fallback(full_text: str, snippet_text: str) -> Tuple[int, int]:
    """
    Best-effort fallback for line span if AST doesn't provide lineno/end_lineno.
    We locate the snippet within the full text and compute line span from that.
    If not found, return (1, 1).
    """
    idx = full_text.find(snippet_text)
    if idx < 0:
        return 1, 1
    start_line = full_text.count("\n", 0, idx) + 1
    end_line = start_line + snippet_text.count("\n")
    return start_line, end_line


# ----------------------------
# IO helpers
# ----------------------------

def write_mirror_with_meta(mirror_root: Path, rel_path: Path, content: str, meta: Dict[str, Any]) -> None:
    """
    Write:
      - mirror markdown to <rel_path>
      - metadata json to <rel_path>.meta.json
    """
    out_path = mirror_root / rel_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")

    meta_path = Path(str(out_path) + ".meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _base_meta(
    *,
    run_id: str,
    kind: str,
    source_rel: str,
    mirror_rel: str,
    file_info: Dict[str, Any],
    extractor: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "kind": kind,
        "source_rel": source_rel,
        "mirror_rel": mirror_rel,
        "file": file_info,
        "extractor": extractor,
        # Explicit contract notes to prevent drift:
        "pointer_contract": {
            "line_base": 1,
            "end_inclusive": True,
            "path_norm": "posix_rel",
        },
        "snippets": [],
    }


def _file_info_from_cache(f: Dict[str, Any]) -> Dict[str, Any]:
    """
    Pull stable file identity info from loader-produced cache fields.
    """
    info: Dict[str, Any] = {
        "abs_path": str(f.get("abs_path", "")),
        "source_sha1": f.get("source_sha1", None),
        "source_bytes": f.get("source_bytes", None),
        "source_mtime_ns": f.get("source_mtime_ns", None),
        "encoding": f.get("encoding", "utf-8"),
        "read_errors_mode": f.get("read_errors_mode", "replace"),
    }
    return info


def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _line_count(text: str) -> int:
    if not text:
        return 0
    # splitlines() ignores trailing newline; this matches human expectations in editors.
    return len(text.splitlines())
