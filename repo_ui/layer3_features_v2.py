# layer3_features_v2.py
from __future__ import annotations

import ast
from dataclasses import replace
from typing import Any, Dict, List, Optional, Tuple


def _collect_string_constants(tree: ast.AST) -> Dict[str, str]:
    """
    Collect Name -> "literal string" assignments within the mirror AST.

    We intentionally keep this conservative:
      - Only Assign / AnnAssign
      - Only single Name targets (no tuple unpack)
      - Only string literals
      - Includes module-level and class-body assignments present in the mirror
    """
    out: Dict[str, str] = {}

    def visit_body(body: List[ast.stmt]) -> None:
        for st in body:
            if isinstance(st, ast.Assign):
                if isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
                    for t in st.targets:
                        if isinstance(t, ast.Name):
                            out.setdefault(t.id, st.value.value)
            elif isinstance(st, ast.AnnAssign):
                if (
                    isinstance(st.target, ast.Name)
                    and st.value is not None
                    and isinstance(st.value, ast.Constant)
                    and isinstance(st.value.value, str)
                ):
                    out.setdefault(st.target.id, st.value.value)
            elif isinstance(st, ast.ClassDef):
                visit_body(st.body)

    if isinstance(tree, ast.Module):
        visit_body(tree.body)

    return out


def _extract_id_expr(call: ast.Call) -> Optional[ast.AST]:
    for kw in call.keywords or []:
        if kw.arg == "id":
            return kw.value
    return None


def _render_fstring_pattern(expr: ast.JoinedStr) -> Optional[str]:
    """
    Convert a JoinedStr (f-string) to a stable template string.

    Rules:
      - Constant string parts are preserved
      - FormattedValue parts become "{<name>}" if Name, "{?}" otherwise
      - If the f-string contains format specs we can’t represent, return None
    """
    parts: List[str] = []
    for v in expr.values:
        if isinstance(v, ast.Constant) and isinstance(v.value, str):
            parts.append(v.value)
            continue
        if isinstance(v, ast.FormattedValue):
            # If there is a format_spec, it’s still a pattern, but can be complex.
            # Keep conservative: accept only empty / None format_spec.
            if v.format_spec is not None:
                return None
            inner = v.value
            if isinstance(inner, ast.Name):
                parts.append("{" + inner.id + "}")
            else:
                parts.append("{?}")
            continue
        # Unknown component in f-string
        return None

    return "".join(parts)

def apply_feature_v2_id_resolution(
    *,
    tree: ast.AST,
    nodes: Dict[str, Any],  # NodeRec-like objects
    node_calls: Dict[str, ast.Call],
    edge_case_counts: Dict[str, int],
    edge_case_samples: List[Dict[str, Any]],
    verbose: bool,
    file_rel: str,
    repo_root,  # pathlib.Path, passed from pass1
) -> Dict[str, int]:
    """
    v2.1: Post-pass ID enrichment:
      - Resolve id=NAME if NAME is provably a string constant:
          * in the same file, OR
          * imported via 'from mod import NAME' where mod resolves to a local .py file
            and that file has NAME = "literal"
      - Convert simple f-strings into id patterns (id.kind='pattern')
      - Recompute id_nonliteral bucket accordingly, and add id_pattern bucket
    """
    import pathlib

    # 1) constants in this file
    const_str = _collect_string_constants(tree)

    # 2) import-follow constants (from-import only; conservative)
    imported_const: Dict[str, str] = {}

    def _module_to_path(mod: str) -> Optional[pathlib.Path]:
        # repo uses absolute-ish rel paths; treat module as package path.
        rel = pathlib.Path(*mod.split(".")).with_suffix(".py")
        p = pathlib.Path(repo_root) / rel
        if p.exists():
            return p
        # also allow "tvm_ui.surfaces.pages_surface" mapping within repo root
        return None

    def _parse_file(p: pathlib.Path) -> Optional[ast.Module]:
        try:
            txt = p.read_text(encoding="utf-8", errors="replace")
            return ast.parse(txt)
        except Exception:
            return None

    def _collect_from_imports(module_ast: ast.AST) -> List[Tuple[str, str]]:
        """
        Returns list of (imported_name, module) for: from module import imported_name
        Ignores:
          - star imports
          - aliases (we resolve to asname if present; NAME refers to asname in current file)
        """
        out: List[Tuple[str, str]] = []
        for n in ast.walk(module_ast):
            if isinstance(n, ast.ImportFrom) and n.module:
                mod = n.module
                for a in n.names or []:
                    if a.name == "*":
                        continue
                    bind = a.asname or a.name
                    out.append((bind, mod))
        return out

    # Build mapping for names imported in THIS file only.
    for bind, mod in _collect_from_imports(tree):
        # If we already resolved in-file, skip.
        if bind in const_str:
            continue
        p = _module_to_path(mod)
        if not p:
            continue
        mod_ast = _parse_file(p)
        if not mod_ast:
            continue
        mod_consts = _collect_string_constants(mod_ast)
        # We imported bind name, but in the source module it might have different original name (if alias used).
        # Conservative: only resolve when alias not used OR module defines the bound name.
        if bind in mod_consts:
            imported_const[bind] = mod_consts[bind]

    # Merge (local wins)
    resolved_table = dict(imported_const)
    resolved_table.update(const_str)

    resolved_literal = 0
    pattern_ids = 0
    pattern_samples: List[Dict[str, Any]] = []
    remaining_nonliteral_nodes: List[Tuple[str, str, Dict[str, Any]]] = []

    # Remove existing id_nonliteral samples; rebuild below
    edge_case_samples[:] = [s for s in edge_case_samples if s.get("kind") not in ("id_nonliteral", "id_pattern")]

    for nid, n in list(nodes.items()):
        if getattr(n, "id_kind", None) != "dynamic":
            continue

        call = node_calls.get(nid)
        if call is None:
            continue

        id_expr = _extract_id_expr(call)
        if id_expr is None:
            continue

        # Case A: id = SOME_CONST (local or imported)
        if isinstance(id_expr, ast.Name) and id_expr.id in resolved_table:
            lit = resolved_table[id_expr.id]
            nodes[nid] = replace(n, id_kind="literal", id_value=lit)
            resolved_literal += 1
            continue

        # Case B: id = f"..."
        if isinstance(id_expr, ast.JoinedStr):
            pat = _render_fstring_pattern(id_expr)
            if pat is not None:
                nodes[nid] = replace(n, id_kind="pattern", id_value=pat)
                pattern_ids += 1
                if len(pattern_samples) < 10:
                    pattern_samples.append(
                        {
                            "kind": "id_pattern",
                            "message": "UI constructor has id kwarg as f-string; captured as pattern",
                            "type": getattr(n, "type_name", "unknown"),
                            "pattern": pat,
                            "provenance": {
                                "anchor_ref": getattr(n, "anchor_ref", None),
                                **({"focus": getattr(n, "focus")} if getattr(n, "focus", None) else {}),
                            },
                        }
                    )
                continue

        # Still nonliteral
        prov = {
            "anchor_ref": getattr(n, "anchor_ref", None),
            **({"focus": getattr(n, "focus")} if getattr(n, "focus", None) else {}),
        }
        remaining_nonliteral_nodes.append((nid, getattr(n, "type_name", "unknown"), prov))

    # Recompute id_nonliteral
    nonliteral = 0
    nonliteral_samples: List[Dict[str, Any]] = []
    for nid, ty, prov in remaining_nonliteral_nodes:
        call = node_calls.get(nid)
        if call is None:
            continue
        if _extract_id_expr(call) is None:
            continue
        if getattr(nodes[nid], "id_kind", None) != "dynamic":
            continue
        nonliteral += 1
        if len(nonliteral_samples) < 10:
            nonliteral_samples.append(
                {
                    "kind": "id_nonliteral",
                    "message": "UI constructor has id kwarg but value is not a string literal",
                    "type": ty,
                    "provenance": prov,
                }
            )

    edge_case_counts["id_nonliteral"] = nonliteral
    if pattern_ids > 0:
        edge_case_counts["id_pattern"] = edge_case_counts.get("id_pattern", 0) + pattern_ids

    edge_case_samples.extend(nonliteral_samples)
    edge_case_samples.extend(pattern_samples)

    if verbose:
        print(
            f"[layer3/features] {file_rel}: id_resolution_v2_1: "
            f"resolved_literal={resolved_literal} pattern_ids={pattern_ids} "
            f"import_const={len(imported_const)} local_const={len(const_str)}"
        )

    return {"resolved_literal": resolved_literal, "pattern_ids": pattern_ids}
