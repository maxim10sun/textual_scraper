# layer3_pass1.py
from __future__ import annotations

import ast
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from repo_ui.layer3_features import (
    FeatureHooks,
    apply_with_edges_v1,
    apply_mount_edges_v1,
)


# ----------------------------
# Config (v1)
# ----------------------------

CONTRACT_VERSION_TILE = "layer3_tile_v1"
CONTRACT_VERSION_INDEX = "layer3_repo_index_v1"

DIALECT_PREFIXES = ["textual"]  # v1: textual-only, but designed to be swappable later

# Output locations (relative to repo root)
MIRROR_ROOT = Path("shadow_ui") / "layer4" / "mirror"
OUT_TILES_ROOT = Path("shadow_ui") / "layer3" / "tiles"
OUT_INDEX_PATH = Path("shadow_ui") / "layer3" / "index.json"


# ----------------------------
# Helpers
# ----------------------------

def _sha1_text(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _module_matches_prefix(mod: str, prefixes: List[str]) -> bool:
    for p in prefixes:
        if mod == p or mod.startswith(p + "."):
            return True
    return False


def _escape_id_value_for_canonical(s: str) -> str:
    """
    Canonical escaping for ID values in the layout serialization.

    We only escape characters that would conflict with the serialization grammar:
      '#', '(', ')', ',', '\\'
    Using percent-encoding keeps it deterministic and unambiguous.
    """
    # Percent-encode only the grammar chars and backslash.
    # Keep everything else as-is for readability.
    repl = {
        "\\": "%5C",
        "#": "%23",
        "(": "%28",
        ")": "%29",
        ",": "%2C",
    }
    out = []
    for ch in s:
        out.append(repl.get(ch, ch))
    return "".join(out)


@dataclass(frozen=True)
class SnippetAnchor:
    snippet_kind: str
    snippet_sha1: str
    start_line: int
    end_line: int


def _build_meta_refs(meta: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, SnippetAnchor]]:
    """
    Build:
      - meta_refs JSON object (snippets table)
      - reverse map from snippet_sha1 -> anchor_ref id and anchor data

    Deterministic ordering: by (start_line, end_line, snippet_kind, snippet_sha1)
    """
    snippets = meta.get("snippets", []) or []
    anchors: List[SnippetAnchor] = []
    for s in snippets:
        try:
            anchors.append(
                SnippetAnchor(
                    snippet_kind=str(s.get("snippet_kind", "")),
                    snippet_sha1=str(s.get("snippet_sha1", "")),
                    start_line=int(s.get("start_line", 1)),
                    end_line=int(s.get("end_line", 1)),
                )
            )
        except Exception:
            continue

    anchors.sort(key=lambda a: (a.start_line, a.end_line, a.snippet_kind, a.snippet_sha1))

    meta_refs: Dict[str, Any] = {"snippets": {}}
    by_sha1: Dict[str, SnippetAnchor] = {}
    for i, a in enumerate(anchors, start=1):
        ref = f"s{i:03d}"
        meta_refs["snippets"][ref] = {
            "snippet_kind": a.snippet_kind,
            "snippet_sha1": a.snippet_sha1,
            "start_line": a.start_line,
            "end_line": a.end_line,
        }
        # If duplicates, keep first deterministically.
        if a.snippet_sha1 and a.snippet_sha1 not in by_sha1:
            by_sha1[a.snippet_sha1] = a

    return meta_refs, by_sha1


def _collect_ui_imported_names(tree: ast.AST, dialect_prefixes: List[str]) -> Set[str]:
    """
    Collect locally bound names from any ImportFrom whose module matches the dialect prefix.
    """
    names: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if _module_matches_prefix(mod, dialect_prefixes):
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    names.add(alias.asname or alias.name)
    return names


def _collect_used_name_ids(tree: ast.AST) -> Set[str]:
    used: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
    return used


def _ui_symbols_used(tree: ast.AST, dialect_prefixes: List[str]) -> List[str]:
    imported = _collect_ui_imported_names(tree, dialect_prefixes)
    if not imported:
        return []
    used = _collect_used_name_ids(tree)
    # "used" per contract: imported AND referenced (Load). Mirrors already bias this.
    return sorted([n for n in imported if n in used])


def _is_ui_constructor_call(node: ast.AST, ui_symbols: Set[str]) -> bool:
    if not isinstance(node, ast.Call):
        return False
    # v1 strict: only Name(...) where Name is in ui_symbols
    return isinstance(node.func, ast.Name) and node.func.id in ui_symbols


def _extract_id_kw(node: ast.Call) -> Tuple[str, Optional[str], Optional[str]]:
    """
    Returns (kind, literal_value, reason)
      kind: 'literal' | 'none' | 'dynamic'
      literal_value: only for 'literal'
      reason: for 'dynamic' (e.g., 'non_string_literal')
    """
    id_kw = None
    for kw in node.keywords or []:
        if kw.arg == "id":
            id_kw = kw
            break

    if id_kw is None:
        return ("none", None, None)

    val = id_kw.value
    if isinstance(val, ast.Constant) and isinstance(val.value, str):
        return ("literal", val.value, None)

    # v1: only accept quoted string literals; everything else becomes (dynamic)
    return ("dynamic", None, "id_nonliteral")


def _enclosing_top_level_block(tree: ast.Module, target: ast.AST) -> Optional[ast.AST]:
    """
    Find the smallest enclosing top-level node (from tree.body) that spans target.lineno..end_lineno.
    Used to map to mirror meta snippet via snippet_sha1 of that block.
    """
    t_ln = getattr(target, "lineno", None)
    t_end = getattr(target, "end_lineno", None)
    if not isinstance(t_ln, int) or not isinstance(t_end, int):
        return None

    best = None
    best_span = None
    for node in tree.body:
        n_ln = getattr(node, "lineno", None)
        n_end = getattr(node, "end_lineno", None)
        if not isinstance(n_ln, int) or not isinstance(n_end, int):
            continue
        if n_ln <= t_ln and t_end <= n_end:
            span = (n_end - n_ln, n_ln, n_end)
            if best is None or span < best_span:
                best = node
                best_span = span
    return best


def _anchor_ref_for_top_level_node(
    mirror_text: str,
    node: ast.AST,
    meta_refs: Dict[str, Any],
) -> Optional[str]:
    """
    Compute snippet_sha1 for the exact top-level node source segment and match it to meta_refs.
    Returns anchor_ref (e.g., s001) if found.
    """
    seg = ast.get_source_segment(mirror_text, node)
    if not seg:
        return None
    h = _sha1_text(seg)
    # meta_refs["snippets"] has entries keyed by s###
    for ref, rec in (meta_refs.get("snippets") or {}).items():
        if rec.get("snippet_sha1") == h:
            return ref
    return None


def _anchor_data(meta_refs: Dict[str, Any], anchor_ref: str) -> Optional[SnippetAnchor]:
    rec = (meta_refs.get("snippets") or {}).get(anchor_ref)
    if not rec:
        return None
    try:
        return SnippetAnchor(
            snippet_kind=str(rec.get("snippet_kind", "")),
            snippet_sha1=str(rec.get("snippet_sha1", "")),
            start_line=int(rec.get("start_line", 1)),
            end_line=int(rec.get("end_line", 1)),
        )
    except Exception:
        return None

def _expr_to_compact_text(expr: ast.AST) -> str:
    try:
        # py>=3.9
        return ast.unparse(expr)
    except Exception:
        # fallback: stable-ish repr
        return expr.__class__.__name__

def _focus_span_from_block(
    block: ast.AST,
    inner: ast.AST,
    anchor: SnippetAnchor,
) -> Optional[Dict[str, int]]:
    """
    Medium precision: map inner node's lineno/end_lineno to original-source lines
    using relative offset within the top-level block.

    Returns {"start_line": ..., "end_line": ...} in original-source coordinates.
    """
    b_ln = getattr(block, "lineno", None)
    i_ln = getattr(inner, "lineno", None)
    i_end = getattr(inner, "end_lineno", None)
    if not all(isinstance(x, int) for x in (b_ln, i_ln, i_end)):
        return None
    # Relative to block start; block segment matches original source segment.
    rel_start = i_ln - b_ln
    rel_end = i_end - b_ln
    start_line = anchor.start_line + rel_start
    end_line = anchor.start_line + rel_end
    if start_line < 1 or end_line < start_line:
        return None
    return {"start_line": start_line, "end_line": end_line}


# ----------------------------
# Layout building
# ----------------------------

@dataclass
class NodeRec:
    node_id: str
    type_name: str
    id_kind: str  # literal|none|dynamic
    id_value: Optional[str]
    anchor_ref: Optional[str]
    focus: Optional[Dict[str, int]]  # original-source
    # Mirror coords for determinism (fallback)
    mirror_lno: int


@dataclass
class EdgeRec:
    parent: str
    child: str
    order_index: int
    anchor_ref: Optional[str]
    focus: Optional[Dict[str, int]]  # original-source


@dataclass
class RootRec:
    root_id: str
    node_id: str
    context: Dict[str, str]
    anchor_ref: Optional[str]
    focus: Optional[Dict[str, int]]  # original-source


def _canonical_serialize(node_id: str, nodes: Dict[str, NodeRec], children_map: Dict[str, List[str]]) -> str:
    n = nodes[node_id]
    type_name = n.type_name
    if n.id_kind == "literal" and n.id_value is not None:
        id_part = _escape_id_value_for_canonical(n.id_value)
    elif n.id_kind == "none":
        id_part = "(none)"
    else:
        id_part = "(dynamic)"

    kids = children_map.get(node_id, [])
    if not kids:
        return f"{type_name}#{id_part}()"
    inner = ",".join(_canonical_serialize(k, nodes, children_map) for k in kids)
    return f"{type_name}#{id_part}({inner})"


def _find_ui_calls(tree: ast.AST, ui_symbols: Set[str]) -> List[ast.Call]:
    calls: List[ast.Call] = []
    for node in ast.walk(tree):
        if _is_ui_constructor_call(node, ui_symbols):
            calls.append(node)  # type: ignore[arg-type]
    return calls


def _find_yield_roots(tree: ast.AST, ui_symbols: Set[str]) -> List[ast.Yield]:
    roots: List[ast.Yield] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Yield):
            v = node.value
            if isinstance(v, ast.Call) and _is_ui_constructor_call(v, ui_symbols):
                roots.append(node)
    return roots

def _detect_unmodeled_patterns(
    tree: ast.AST,
    ui_symbols: Set[str],
    *,
    exclude_with_nodes: Optional[Set[int]] = None,
    exclude_mount_calls: Optional[Set[int]] = None,
) -> Dict[str, List[ast.AST]]:
    """
    Pass-1 residue: patterns we do NOT model as edges/roots, but want to count & sample.

    New in feature_v1:
      - allow excluding specific nodes/calls that WERE modeled by an optional feature pass
        (so residue buckets reflect "unknown" rather than "not implemented yet").
    """
    exclude_with_nodes = exclude_with_nodes or set()
    exclude_mount_calls = exclude_mount_calls or set()

    buckets: Dict[str, List[ast.AST]] = {
        "with_block_unmodeled": [],
        "mount_edges_unmodeled": [],
        "child_star_args": [],
    }

    # with <UIConstructor>(...): ...
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            if id(node) in exclude_with_nodes:
                continue
            for item in node.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and _is_ui_constructor_call(ce, ui_symbols):
                    buckets["with_block_unmodeled"].append(node)
                    break

    # receiver.mount( <UIConstructor>(...) ) patterns
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "mount":
                if id(node) in exclude_mount_calls:
                    continue
                for a in node.args:
                    if isinstance(a, ast.Call) and _is_ui_constructor_call(a, ui_symbols):
                        buckets["mount_edges_unmodeled"].append(node)
                        break

    # Starred children in constructor args: Container(*items)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_ui_constructor_call(node, ui_symbols):
            for a in node.args:
                if isinstance(a, ast.Starred):
                    buckets["child_star_args"].append(node)
                    break

    return buckets

def _feature_model_with_edges_v1(
    tree: ast.Module,
    ui_syms: Set[str],
    call_to_node_id: Dict[Tuple[int, int, int, int], str],
    *,
    edges: List["EdgeRec"],
    children_map: Dict[str, List[str]],
    nodes: Dict[str, "NodeRec"],
    mirror_text: str,
    meta_refs: Dict[str, Any],
) -> Set[int]:
    """
    Feature v1: Model 'with <UIConstructor>(...): yield <UIConstructor>(...)' containment.

    Returns:
      exclude_with_nodes: Set[id(ast.With)] that were successfully modeled, so residue bucketing can ignore them.
    """

    def node_key(call: ast.Call) -> Tuple[int, int, int, int]:
        ln = int(getattr(call, "lineno", 0) or 0)
        col = int(getattr(call, "col_offset", 0) or 0)
        eln = int(getattr(call, "end_lineno", ln) or ln)
        ecol = int(getattr(call, "end_col_offset", col) or col)
        return (ln, col, eln, ecol)

    # Prevent duplicate edges if feature is re-run or overlaps with positional edges
    existing = {(e.parent, e.child, int(e.order_index)) for e in edges}

    modeled_with_nodes: Set[int] = set()

    # Walk only With nodes; model if parent + at least 1 child resolve
    for w in (n for n in ast.walk(tree) if isinstance(n, ast.With)):
        parent_call: Optional[ast.Call] = None
        for item in w.items:
            ce = item.context_expr
            if isinstance(ce, ast.Call) and _is_ui_constructor_call(ce, ui_syms):
                parent_call = ce
                break
        if parent_call is None:
            continue

        parent_id = call_to_node_id.get(node_key(parent_call))
        if not parent_id:
            continue

        # Provenance: use enclosing top-level block
        block = _enclosing_top_level_block(tree, w)
        anchor_ref = None
        focus = None
        if block is not None:
            anchor_ref = _anchor_ref_for_top_level_node(mirror_text, block, meta_refs)
            if anchor_ref:
                anchor = _anchor_data(meta_refs, anchor_ref)
                if anchor:
                    focus = _focus_span_from_block(block, w, anchor)

        # Collect children in lexical order within the with-body:
        # - yield <UIConstructor>(...)
        # - nested with <UIConstructor>(...)  (treated as child = its context_expr call)
        child_calls: List[Tuple[int, ast.Call]] = []

        for stmt in (w.body or []):
            # yield X(...)
            if isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Yield):
                y = stmt.value
                v = getattr(y, "value", None)
                if isinstance(v, ast.Call) and _is_ui_constructor_call(v, ui_syms):
                    child_calls.append((int(getattr(y, "lineno", 10**9) or 10**9), v))

            # direct Yield node (rarely appears without Expr wrapper, but safe)
            if isinstance(stmt, ast.Yield):
                v = getattr(stmt, "value", None)
                if isinstance(v, ast.Call) and _is_ui_constructor_call(v, ui_syms):
                    child_calls.append((int(getattr(stmt, "lineno", 10**9) or 10**9), v))

            # nested with Y(...):
            if isinstance(stmt, ast.With):
                nested_parent: Optional[ast.Call] = None
                for it in stmt.items:
                    ce2 = it.context_expr
                    if isinstance(ce2, ast.Call) and _is_ui_constructor_call(ce2, ui_syms):
                        nested_parent = ce2
                        break
                if nested_parent is not None:
                    child_calls.append((int(getattr(stmt, "lineno", 10**9) or 10**9), nested_parent))

        if not child_calls:
            continue

        child_calls.sort(key=lambda t: t[0])

        # Emit edges with stable order_index (relative to with-body ordering)
        order = 0
        any_child = False
        for _ln, ccall in child_calls:
            child_id = call_to_node_id.get(node_key(ccall))
            if not child_id:
                continue

            tup = (parent_id, child_id, order)
            if tup not in existing:
                edges.append(
                    EdgeRec(
                        parent=parent_id,
                        child=child_id,
                        order_index=order,
                        anchor_ref=anchor_ref or nodes[parent_id].anchor_ref,
                        focus=focus,
                    )
                )
                children_map[parent_id].append(child_id)
                existing.add(tup)

            any_child = True
            order += 1

        if any_child:
            modeled_with_nodes.add(id(w))

    return modeled_with_nodes

# ----------------------------
# Main per-file tile builder
# ----------------------------

def build_layer3_tile_for_mirror(mirror_path: Path) -> Optional[Dict[str, Any]]:
    """
    Build a Layer3 pass-1 tile from a single mirror .py.md and its .meta.json sidecar.
    Returns tile JSON dict, or None if not a python mirror.
    """
    if not mirror_path.name.endswith(".py.md"):
        return None
    meta_path = Path(str(mirror_path) + ".meta.json")
    if not meta_path.exists():
        return None

    mirror_text = mirror_path.read_text(encoding="utf-8", errors="replace")
    meta = _read_json(meta_path)

    # Run identity
    run_id = str(meta.get("run_id", "")) or "unknown"

    # Source pointers
    source_rel = str(meta.get("source_rel", mirror_path.name.replace(".md", "")))
    mirror_rel = str(meta.get("mirror_rel", mirror_path.as_posix()))
    pointer_contract = meta.get("pointer_contract", {"line_base": 1, "end_inclusive": True, "path_norm": "posix_rel"})
    file_info = meta.get("file", {}) or {}
    source_sha1 = file_info.get("source_sha1", None)

    # Parse mirror code
    try:
        tree = ast.parse(mirror_text)
    except SyntaxError:
        # Tile still emitted, but everything is edge_case parse_error.
        tile = {
            "contract_version": CONTRACT_VERSION_TILE,
            "run_id": run_id,
            "source": {
                "source_rel": source_rel,
                "mirror_rel": mirror_rel,
                "meta_rel": meta_path.as_posix(),
                "source_sha1": source_sha1,
                "pointer_contract": pointer_contract,
            },
            "meta_refs": {"snippets": {}},
            "dialect": {"prefixes": DIALECT_PREFIXES, "ui_symbols_used": []},
            "pools": {
                "constructors": {"count": 0, "nodes": []},
                "edges": {"count": 0, "edges": []},
                "roots": {"count": 0, "roots": []},
                "trees": {"count": 0, "trees": []},
                "hashes": {"count": 0, "items": []},
            },
            "indexes": {"ids_by_value": {}, "types_by_name": {}, "hashes_by_value": {}},
            "edge_cases": {
                "buckets": [{"kind": "parse_error", "count": 1}],
                "samples": [{"kind": "parse_error", "message": "mirror_ast_parse_failed", "provenance": {"anchor_ref": None}}],
            },
            "stats": {"ui_symbols_used": 0, "constructor_calls": 0, "literal_ids": 0, "nodes_without_id": 0, "roots": 0, "trees": 0},
        }
        return tile

    # Meta refs (anchors)
    meta_refs, _by_sha1 = _build_meta_refs(meta)

    # Dialect symbols (used)
    ui_syms_list = _ui_symbols_used(tree, DIALECT_PREFIXES)
    ui_syms: Set[str] = set(ui_syms_list)

    # Collect constructor calls
    calls = _find_ui_calls(tree, ui_syms)

    # Build node records with provenance
    raw_nodes: List[Tuple[ast.Call, Optional[str], Optional[ast.AST]]] = []
    for call in calls:
        block = _enclosing_top_level_block(tree, call)  # top-level block node
        anchor_ref = None
        if block is not None:
            anchor_ref = _anchor_ref_for_top_level_node(mirror_text, block, meta_refs)
        raw_nodes.append((call, anchor_ref, block))

    # Deterministic sorting of calls (mirror coordinate based)
    def call_sort_key(t: Tuple[ast.Call, Optional[str], Optional[ast.AST]]) -> Tuple[int, int, str]:
        call, anchor_ref, _block = t
        ln = getattr(call, "lineno", 10**9)
        col = getattr(call, "col_offset", 10**9)
        ty = call.func.id if isinstance(call.func, ast.Name) else ""
        return (int(ln) if isinstance(ln, int) else 10**9, int(col) if isinstance(col, int) else 10**9, ty)

    raw_nodes.sort(key=call_sort_key)

    # Assign node_ids
    nodes: Dict[str, NodeRec] = {}
    call_to_node_id: Dict[Tuple[int, int, int, int], str] = {}
    node_calls: Dict[str, ast.Call] = {}  # v2: node_id -> ast.Call for feature passes

    edge_case_counts: Dict[str, int] = {
        "id_nonliteral": 0,
        "with_block_unmodeled": 0,
        "mount_edges_unmodeled": 0,
        "child_star_args": 0,
        # v2 may add: id_pattern
    }
    edge_case_samples: List[Dict[str, Any]] = []

    def node_key(c: ast.Call) -> Tuple[int, int, int, int]:
        ln = getattr(c, "lineno", -1)
        col = getattr(c, "col_offset", -1)
        end = getattr(c, "end_lineno", ln)
        endc = getattr(c, "end_col_offset", col)
        return (
            int(ln) if isinstance(ln, int) else -1,
            int(col) if isinstance(col, int) else -1,
            int(end) if isinstance(end, int) else -1,
            int(endc) if isinstance(endc, int) else -1,
        )

    for i, (call, anchor_ref, block) in enumerate(raw_nodes, start=1):
        nid = f"n{i:06d}"
        call_to_node_id[node_key(call)] = nid
        node_calls[nid] = call

        type_name = call.func.id  # strict Name(...) ensured
        id_kind, id_val, reason = _extract_id_kw(call)

        # provenance focus mapping (medium)
        focus = None
        if anchor_ref and block is not None:
            anchor = _anchor_data(meta_refs, anchor_ref)
            if anchor:
                focus = _focus_span_from_block(block, call, anchor)

        nodes[nid] = NodeRec(
            node_id=nid,
            type_name=type_name,
            id_kind=id_kind,
            id_value=id_val,
            anchor_ref=anchor_ref,
            focus=focus,
            mirror_lno=int(getattr(call, "lineno", 10**9) or 10**9),
        )

        if reason == "id_nonliteral":
            edge_case_counts["id_nonliteral"] += 1
            if len(edge_case_samples) < 10:
                edge_case_samples.append(
                    {
                        "kind": "id_nonliteral",
                        "message": "UI constructor has id kwarg but value is not a string literal",
                        "type": type_name,
                        "provenance": {"anchor_ref": anchor_ref, **({"focus": focus} if focus else {})},
                    }
                )

    # ----------------------------
    # Feature v2: ID resolution + ID patterns
    # ----------------------------
    try:
        from .layer3_features_v2 import apply_feature_v2_id_resolution  # type: ignore
    except Exception:
        apply_feature_v2_id_resolution = None  # type: ignore

    FEATURE_ID_RESOLUTION_V2 = True
    FEATURE_V2_VERBOSE = True
    repo_root = Path(meta.get("repo_root", ""))
    if FEATURE_ID_RESOLUTION_V2 and apply_feature_v2_id_resolution is not None:
        apply_feature_v2_id_resolution(
            tree=tree,
            nodes=nodes,
            node_calls=node_calls,
            edge_case_counts=edge_case_counts,
            edge_case_samples=edge_case_samples,
            verbose=FEATURE_V2_VERBOSE,
            file_rel=source_rel,
            repo_root=repo_root,
        )

    # Build edges (nested positional constructor calls)
    edges: List[EdgeRec] = []
    children_map: Dict[str, List[str]] = {nid: [] for nid in nodes.keys()}

    # For mapping a child call to node_id, we rely on exact lineno/col spans.
    for call, anchor_ref, block in raw_nodes:
        parent_id = call_to_node_id.get(node_key(call))
        if not parent_id:
            continue

        # Determine provenance for edge (parent call span)
        edge_focus = None
        if anchor_ref and block is not None:
            anchor = _anchor_data(meta_refs, anchor_ref)
            if anchor:
                edge_focus = _focus_span_from_block(block, call, anchor)

        order = 0
        for arg in call.args:
            # v1 modeled: positional arg that is UI constructor call
            if isinstance(arg, ast.Call) and _is_ui_constructor_call(arg, ui_syms):
                child_id = call_to_node_id.get(node_key(arg))
                if child_id:
                    edges.append(
                        EdgeRec(
                            parent=parent_id,
                            child=child_id,
                            order_index=order,
                            anchor_ref=nodes[parent_id].anchor_ref,
                            focus=edge_focus,
                        )
                    )
                    children_map[parent_id].append(child_id)
                    order += 1
            elif isinstance(arg, ast.Starred):
                # residue bucket (already counted separately too)
                pass

    # Deterministic child ordering: already preserved by traversal order index
    for pid in children_map:
        pass

    # ----------------------------
    # Feature v1: structure modeling (with + mount)
    # ----------------------------
    # NOTE: this must run AFTER baseline edges/children_map exist,
    # and BEFORE residue bucketing.
    from repo_ui.layer3_features import FeatureHooks, apply_with_edges_v1, apply_mount_edges_v1

    hooks = FeatureHooks(
        is_ui_constructor_call=_is_ui_constructor_call,
        enclosing_top_level_block=_enclosing_top_level_block,
        anchor_ref_for_top_level_node=_anchor_ref_for_top_level_node,
        anchor_data=_anchor_data,
        focus_span_from_block=_focus_span_from_block,
        expr_to_compact_text=_expr_to_compact_text,
    )

    feat_with = apply_with_edges_v1(
        tree=tree,
        ui_syms=ui_syms,
        call_to_node_id=call_to_node_id,
        edges=edges,
        children_map=children_map,
        nodes=nodes,
        mirror_text=mirror_text,
        meta_refs=meta_refs,
        edge_ctor=EdgeRec,
        hooks=hooks,
    )

    feat_mount = apply_mount_edges_v1(
        tree=tree,
        ui_syms=ui_syms,
        call_to_node_id=call_to_node_id,
        edges=edges,
        mirror_text=mirror_text,
        meta_refs=meta_refs,
        edge_ctor=EdgeRec,
        hooks=hooks,
    )

    # Print feature notes so missing wiring is obvious
    for note in (feat_with.notes + feat_mount.notes):
        print(f"[layer3/features] {source_rel}: {note}")

    # ----------------------------
    # Unmodeled patterns buckets (exclude modeled cases)
    # ----------------------------
    residues = _detect_unmodeled_patterns(
        tree,
        ui_syms,
        exclude_with_nodes=feat_with.exclude_with_nodes,
        exclude_mount_calls=feat_mount.exclude_mount_calls,
    )

    for kind, nodes_ast in residues.items():
        edge_case_counts[kind] = edge_case_counts.get(kind, 0) + len(nodes_ast)
        # Samples
        for n_ast in nodes_ast[: max(0, 10 - len(edge_case_samples))]:
            block = _enclosing_top_level_block(tree, n_ast)  # type: ignore[arg-type]
            anchor_ref = None
            focus = None
            if block is not None:
                anchor_ref = _anchor_ref_for_top_level_node(mirror_text, block, meta_refs)
                if anchor_ref:
                    anchor = _anchor_data(meta_refs, anchor_ref)
                    if anchor:
                        focus = _focus_span_from_block(block, n_ast, anchor)  # type: ignore[arg-type]
            edge_case_samples.append(
                {
                    "kind": kind,
                    "message": "pattern_detected_not_modeled_in_pass1",
                    "provenance": {"anchor_ref": anchor_ref, **({"focus": focus} if focus else {})},
                }
            )

    # Roots via yield X(...)
    yield_nodes = _find_yield_roots(tree, ui_syms)
    roots: List[RootRec] = []

    # Assign deterministic root ids by yield focus line
    tmp_roots: List[Tuple[int, ast.Yield, ast.Call, Optional[str], Optional[ast.AST]]] = []
    for y in yield_nodes:
        call = y.value  # type: ignore[assignment]
        if not isinstance(call, ast.Call):
            continue
        block = _enclosing_top_level_block(tree, y)
        anchor_ref = None
        if block is not None:
            anchor_ref = _anchor_ref_for_top_level_node(mirror_text, block, meta_refs)
        lno = int(getattr(y, "lineno", 10**9) or 10**9)
        tmp_roots.append((lno, y, call, anchor_ref, block))

    tmp_roots.sort(key=lambda t: (t[0],))

    def _context_for_yield(y: ast.Yield, module: ast.Module) -> Dict[str, str]:
        yln = getattr(y, "lineno", None)
        yend = getattr(y, "end_lineno", None)
        if not isinstance(yln, int):
            return {"kind": "yield", "container": "unknown", "name": "unknown"}
        for node in module.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                nln = getattr(node, "lineno", None)
                nend = getattr(node, "end_lineno", None)
                if isinstance(nln, int) and isinstance(nend, int) and nln <= yln <= nend:
                    if isinstance(node, ast.ClassDef):
                        for sub in node.body:
                            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                sln = getattr(sub, "lineno", None)
                                send = getattr(sub, "end_lineno", None)
                                if isinstance(sln, int) and isinstance(send, int) and sln <= yln <= send:
                                    return {"kind": "yield", "container": "method", "name": sub.name}
                        return {"kind": "yield", "container": "class", "name": node.name}
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        return {"kind": "yield", "container": "function", "name": node.name}
        return {"kind": "yield", "container": "unknown", "name": "unknown"}

    for i, (lno, y, call, anchor_ref, block) in enumerate(tmp_roots, start=1):
        rid = f"r{i:06d}"
        nid = call_to_node_id.get(node_key(call))
        if not nid:
            continue

        focus = None
        if anchor_ref and block is not None:
            anchor = _anchor_data(meta_refs, anchor_ref)
            if anchor:
                focus = _focus_span_from_block(block, y, anchor)

        roots.append(
            RootRec(
                root_id=rid,
                node_id=nid,
                context=_context_for_yield(y, tree),
                anchor_ref=anchor_ref,
                focus=focus,
            )
        )

    # Build trees: one per root (subtree via children_map)
    trees_out: List[Dict[str, Any]] = []
    hashes_out: List[Dict[str, Any]] = []

    def _collect_subtree(root_id: str) -> List[str]:
        out: List[str] = []
        stack: List[str] = [root_id]
        while stack:
            cur = stack.pop()
            out.append(cur)
            kids = children_map.get(cur, [])
            for k in reversed(kids):
                stack.append(k)
        return out

    for r in roots:
        root_nid = r.node_id
        subtree = _collect_subtree(root_nid)
        tree_nodes: List[Dict[str, Any]] = []
        for nid in subtree:
            n = nodes[nid]
            tree_nodes.append(
                {
                    "node_id": n.node_id,
                    "type": n.type_name,
                    "id": (
                        {"kind": "literal", "value": n.id_value}
                        if n.id_kind == "literal" and n.id_value is not None
                        else (
                            {"kind": n.id_kind, "value": n.id_value}
                            if n.id_kind == "pattern" and n.id_value is not None
                            else {"kind": n.id_kind}
                        )
                    ),
                    "children": children_map.get(nid, []),
                    "provenance": {
                        "anchor_ref": n.anchor_ref,
                        **({"focus": n.focus} if n.focus else {}),
                    },
                }
            )

        trees_out.append({"root_id": r.root_id, "root_node_id": root_nid, "nodes": tree_nodes})

        canonical = _canonical_serialize(root_nid, nodes, children_map)
        layout_hash = "sha1:" + _sha1_text(canonical)

        hashes_out.append(
            {
                "root_id": r.root_id,
                "layout_hash": layout_hash,
                "canonical": canonical,
                "includes_ids": True,
                "child_order_matters": True,
                "provenance": {"anchor_ref": r.anchor_ref, **({"focus": r.focus} if r.focus else {})},
            }
        )

    # Indexes
    ids_by_value: Dict[str, List[str]] = {}
    types_by_name: Dict[str, List[str]] = {}
    hashes_by_value: Dict[str, List[str]] = {}

    nodes_without_id = 0

    for nid, n in nodes.items():
        types_by_name.setdefault(n.type_name, []).append(nid)
        if n.id_kind == "literal" and n.id_value is not None:
            ids_by_value.setdefault(n.id_value, []).append(nid)
        elif n.id_kind == "none":
            nodes_without_id += 1

    for h in hashes_out:
        hv = h["layout_hash"]
        rid = h["root_id"]
        hashes_by_value.setdefault(hv, []).append(rid)

    # Edge cases buckets list
    buckets_out = []
    for k in sorted(edge_case_counts.keys()):
        c = int(edge_case_counts.get(k, 0))
        if c > 0:
            buckets_out.append({"kind": k, "count": c})

    # Pool JSON
    constructors_out = []

    def node_sort_key(n: NodeRec) -> Tuple[int, int, str, str]:
        a_start = 10**9
        if n.anchor_ref:
            a = _anchor_data(meta_refs, n.anchor_ref)
            if a:
                a_start = a.start_line
        f_start = n.focus["start_line"] if n.focus and "start_line" in n.focus else 10**9
        idv = n.id_value or ""
        return (a_start, f_start, n.type_name, idv)

    for nid in sorted(nodes.keys(), key=lambda k: node_sort_key(nodes[k])):
        n = nodes[nid]
        constructors_out.append(
            {
                "node_id": n.node_id,
                "type": n.type_name,
                "id": (
                    {"kind": "literal", "value": n.id_value}
                    if n.id_kind == "literal" and n.id_value is not None
                    else (
                        {"kind": "pattern", "value": n.id_value}
                        if n.id_kind == "pattern" and n.id_value is not None
                        else {"kind": n.id_kind}
                    )
                ),
                "provenance": {"anchor_ref": n.anchor_ref, **({"focus": n.focus} if n.focus else {})},
            }
        )

    edges_out = []

    def edge_sort_key(e: EdgeRec) -> Tuple[int, int, str, str]:
        p = nodes.get(e.parent)
        p_start = p.focus["start_line"] if p and p.focus else 10**9
        return (p_start, e.order_index, e.parent, e.child)

    for e in sorted(edges, key=edge_sort_key):
        edges_out.append(
            {
                "parent": e.parent,
                "child": e.child,
                "order_index": e.order_index,
                "provenance": {"anchor_ref": e.anchor_ref, **({"focus": e.focus} if e.focus else {})},
            }
        )

    roots_out = []
    for r in roots:
        roots_out.append(
            {
                "root_id": r.root_id,
                "node_id": r.node_id,
                "context": r.context,
                "provenance": {"anchor_ref": r.anchor_ref, **({"focus": r.focus} if r.focus else {})},
            }
        )

    tile = {
        "contract_version": CONTRACT_VERSION_TILE,
        "run_id": run_id,
        "source": {
            "source_rel": source_rel,
            "mirror_rel": mirror_rel,
            "meta_rel": meta_path.as_posix(),
            "source_sha1": source_sha1,
            "pointer_contract": pointer_contract,
        },
        "meta_refs": meta_refs,
        "dialect": {"prefixes": DIALECT_PREFIXES, "ui_symbols_used": ui_syms_list},
        "pools": {
            "constructors": {"count": len(constructors_out), "nodes": constructors_out},
            "edges": {"count": len(edges_out), "edges": edges_out},
            "roots": {"count": len(roots_out), "roots": roots_out},
            "trees": {"count": len(trees_out), "trees": trees_out},
            "hashes": {"count": len(hashes_out), "items": hashes_out},
        },
        "indexes": {"ids_by_value": ids_by_value, "types_by_name": types_by_name, "hashes_by_value": hashes_by_value},
        "edge_cases": {"buckets": buckets_out, "samples": edge_case_samples[:10]},
        "stats": {
            "ui_symbols_used": len(ui_syms_list),
            "constructor_calls": len(constructors_out),
            "literal_ids": sum(len(v) for v in ids_by_value.values()),
            "nodes_without_id": nodes_without_id,
            "roots": len(roots_out),
            "trees": len(trees_out),
        },
    }

    return tile

# ----------------------------
# Repo-level driver
# ----------------------------

def build_layer3_pass1(repo_root: Path) -> None:
    mirror_root = repo_root / MIRROR_ROOT
    if not mirror_root.exists():
        raise FileNotFoundError(f"mirror root not found: {mirror_root}")
    print("[layer3] features: with_edges_v1=ON mount_edges_v1=ON")

    out_tiles_root = repo_root / OUT_TILES_ROOT
    out_index_path = repo_root / OUT_INDEX_PATH

    tiles_written = 0
    repo_id_index: Dict[str, List[Dict[str, Any]]] = {}
    repo_layout_index: Dict[str, List[Dict[str, Any]]] = {}
    repo_edge_buckets: Dict[str, int] = {}

    # Walk mirror root for .py.md files
    for dirpath, _dirnames, filenames in os.walk(mirror_root):
        for fn in filenames:
            if not fn.endswith(".py.md"):
                continue
            mirror_path = Path(dirpath) / fn
            tile = build_layer3_tile_for_mirror(mirror_path)
            if tile is None:
                continue

            # Tile output path: tiles/<source_rel>.layer3.json
            source_rel = tile["source"]["source_rel"]
            out_path = out_tiles_root / (source_rel + ".layer3.json")
            _write_json(out_path, tile)
            tiles_written += 1

            # Aggregate indexes
            for idv, node_ids in (tile.get("indexes", {}).get("ids_by_value") or {}).items():
                for nid in node_ids:
                    repo_id_index.setdefault(idv, []).append(
                        {
                            "file": source_rel,
                            "node_id": nid,
                            "tile_rel": out_path.relative_to(repo_root).as_posix(),
                        }
                    )

            for hv, root_ids in (tile.get("indexes", {}).get("hashes_by_value") or {}).items():
                for rid in root_ids:
                    repo_layout_index.setdefault(hv, []).append(
                        {
                            "file": source_rel,
                            "root_id": rid,
                            "tile_rel": out_path.relative_to(repo_root).as_posix(),
                        }
                    )

            for b in (tile.get("edge_cases", {}).get("buckets") or []):
                kind = b.get("kind")
                cnt = int(b.get("count", 0) or 0)
                if kind:
                    repo_edge_buckets[kind] = repo_edge_buckets.get(kind, 0) + cnt

    # Repo index payload
    index_payload = {
        "contract_version": CONTRACT_VERSION_INDEX,
        "run_id": None,  # mixed; can be filled if you want a single-run constraint
        "tiles_written": tiles_written,
        "id_index": repo_id_index,
        "layout_index": repo_layout_index,
        "edge_cases": {"buckets": [{"kind": k, "count": repo_edge_buckets[k]} for k in sorted(repo_edge_buckets.keys())]},
    }
    _write_json(out_index_path, index_payload)

    # NEW: generated scope capsule for repo_ui.query
    # This is intentionally "small + stable": points to index + tile roots.
    from datetime import datetime, timezone

    scope_path = repo_root / "shadow_ui" / "layer3" / "scope.generated.json"
    scope_payload = {
        "contract_version": "repo_ui_scope_v1",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo_root": repo_root.as_posix(),
        "layer3": {
            "index_path": out_index_path.relative_to(repo_root).as_posix(),
            "tiles_roots": [out_tiles_root.relative_to(repo_root).as_posix()],
            "tile_suffix": ".layer3.json",
        },
        "dialect": {
            "prefixes": DIALECT_PREFIXES,
        },
    }
    _write_json(scope_path, scope_payload)

def main() -> None:
    repo_root = Path.cwd()
    build_layer3_pass1(repo_root)
    print(f"[layer3] wrote tiles under {OUT_TILES_ROOT.as_posix()} and index {OUT_INDEX_PATH.as_posix()}")


if __name__ == "__main__":
    main()
