# repo_ui/layer3_features.py
"""
Layer3 feature augmentations (opt-in, additive).

This module is intentionally:
- repo-agnostic (works for any "dialect" as long as you provide `is_ui_constructor_call`)
- additive (it only *adds* edges / exclusions / diagnostics; it does not remove or rewrite pass1 output)
- provenance-friendly (always tries to attach anchor_ref/focus when possible, but never requires it)

v1 features:
- with_edges_v1: model `with X(...): yield Y(...)` containment edges (ordered)
- mount_edges_v1: detect `.mount(X(...))` as explicit edges (parent is a string ref; safe + conservative)

You integrate these by calling the apply_* functions from layer3_pass1 AFTER your baseline pass1 edges are built,
and then passing the returned exclusion sets into your residue bucketing.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple


# -----------------------------
# Types
# -----------------------------

# These callables let this module stay independent of your pass1 file layout.
IsUIConstructorCall = Callable[[ast.Call, Set[str]], bool]
EnclosingTopLevelBlock = Callable[[ast.Module, ast.AST], Optional[ast.AST]]
AnchorRefForTopLevelNode = Callable[[str, ast.AST, Dict[str, Any]], Optional[str]]
AnchorData = Callable[[Dict[str, Any], str], Optional[Dict[str, Any]]]
FocusSpanFromBlock = Callable[[ast.AST, ast.AST, Dict[str, Any]], Optional[Dict[str, int]]]
ExprToCompactText = Callable[[ast.AST], str]


@dataclass(frozen=True)
class FeatureHooks:
    """
    Required hooks from your pass1 implementation.
    """
    is_ui_constructor_call: IsUIConstructorCall
    enclosing_top_level_block: EnclosingTopLevelBlock
    anchor_ref_for_top_level_node: AnchorRefForTopLevelNode
    anchor_data: AnchorData
    focus_span_from_block: FocusSpanFromBlock
    expr_to_compact_text: ExprToCompactText


@dataclass
class FeatureResult:
    """
    Returned by each feature to support:
    - residue exclusion (don't count modeled cases as 'unmodeled')
    - diagnostics about what was/wasn't modeled
    """
    exclude_with_nodes: Set[int]
    exclude_mount_calls: Set[int]
    notes: List[str]


# -----------------------------
# Helpers
# -----------------------------

def _call_key(call: ast.Call) -> Tuple[int, int, int, int]:
    ln = int(getattr(call, "lineno", 0) or 0)
    col = int(getattr(call, "col_offset", 0) or 0)
    eln = int(getattr(call, "end_lineno", ln) or ln)
    ecol = int(getattr(call, "end_col_offset", col) or col)
    return (ln, col, eln, ecol)


def _iter_top_level_stmts(mod: ast.Module) -> Sequence[ast.stmt]:
    return list(getattr(mod, "body", []) or [])


def _yield_value_from_stmt(stmt: ast.stmt) -> Optional[ast.AST]:
    # Common AST shape: Expr(value=Yield(value=...))
    if isinstance(stmt, ast.Expr) and isinstance(getattr(stmt, "value", None), ast.Yield):
        return stmt.value.value
    # Rare: standalone Yield node
    if isinstance(stmt, ast.Yield):
        return getattr(stmt, "value", None)
    return None


# -----------------------------
# Feature: with-block containment edges
# -----------------------------

def apply_with_edges_v1(
    *,
    tree: ast.Module,
    ui_syms: Set[str],
    call_to_node_id: Dict[Tuple[int, int, int, int], str],
    edges: List[Any],
    children_map: Dict[str, List[str]],
    nodes: Dict[str, Any],
    mirror_text: str,
    meta_refs: Dict[str, Any],
    edge_ctor: Callable[..., Any],
    hooks: FeatureHooks,
) -> FeatureResult:
    """
    Model containment via:

        with Parent(...):
            yield Child(...)
            with NestedParent(...):
                ...

    Upgrade (v1.1):
    - A with-block is considered "modeled" (excluded from residue) if:
        * its context_expr is a UI constructor call AND
        * that constructor resolves to a node_id
      even if we cannot resolve any child constructor calls.
    - We still emit child edges for resolvable children.
    - We report diagnostics in notes so you can see what's missing.
    """
    modeled_with_nodes: Set[int] = set()
    notes: List[str] = []

    # dedupe on (parent, child, order_index)
    existing = {
        (getattr(e, "parent", None), getattr(e, "child", None), int(getattr(e, "order_index", 0)))
        for e in edges
    }

    # diagnostics
    with_candidates = 0
    with_parent_resolved = 0
    with_no_child_calls = 0
    with_child_calls_unresolved = 0
    child_edges_added = 0

    for w in (n for n in ast.walk(tree) if isinstance(n, ast.With)):
        # find the first UI constructor in with-items
        parent_call: Optional[ast.Call] = None
        for item in w.items:
            ce = item.context_expr
            if isinstance(ce, ast.Call) and hooks.is_ui_constructor_call(ce, ui_syms):
                parent_call = ce
                break
        if parent_call is None:
            continue

        with_candidates += 1

        parent_id = call_to_node_id.get(_call_key(parent_call))
        if not parent_id:
            # it's a UI constructor syntactically, but we couldn't map it to a node_id
            # (should be rare; keep it in residue by not excluding)
            continue

        with_parent_resolved += 1

        # IMPORTANT CHANGE:
        # mark as modeled as soon as parent resolves (even if children don't)
        modeled_with_nodes.add(id(w))

        # provenance best-effort from enclosing top-level block
        anchor_ref: Optional[str] = None
        focus: Optional[Dict[str, int]] = None
        block = hooks.enclosing_top_level_block(tree, w)
        if block is not None:
            anchor_ref = hooks.anchor_ref_for_top_level_node(mirror_text, block, meta_refs)
            if anchor_ref:
                anchor = hooks.anchor_data(meta_refs, anchor_ref)
                if anchor:
                    focus = hooks.focus_span_from_block(block, w, anchor)

        # collect children in immediate with-body (strict, conservative)
        child_calls: List[Tuple[int, ast.Call]] = []
        for stmt in (w.body or []):
            # yield Child(...)
            yv = _yield_value_from_stmt(stmt)
            if isinstance(yv, ast.Call) and hooks.is_ui_constructor_call(yv, ui_syms):
                child_calls.append((int(getattr(stmt, "lineno", 10**9) or 10**9), yv))

            # nested with NestedParent(...)
            if isinstance(stmt, ast.With):
                nested_parent: Optional[ast.Call] = None
                for it in stmt.items:
                    ce2 = it.context_expr
                    if isinstance(ce2, ast.Call) and hooks.is_ui_constructor_call(ce2, ui_syms):
                        nested_parent = ce2
                        break
                if nested_parent is not None:
                    child_calls.append((int(getattr(stmt, "lineno", 10**9) or 10**9), nested_parent))

        if not child_calls:
            with_no_child_calls += 1
            continue

        child_calls.sort(key=lambda t: t[0])

        order = 0
        any_resolved_child = False

        for _ln, ccall in child_calls:
            child_id = call_to_node_id.get(_call_key(ccall))
            if not child_id:
                continue

            tup = (parent_id, child_id, order)
            if tup not in existing:
                # use computed anchor_ref when available; else fallback to parent node anchor_ref
                ar = anchor_ref or getattr(nodes.get(parent_id), "anchor_ref", None)
                edges.append(
                    edge_ctor(
                        parent=parent_id,
                        child=child_id,
                        order_index=order,
                        anchor_ref=ar,
                        focus=focus,
                    )
                )
                children_map[parent_id].append(child_id)
                existing.add(tup)
                child_edges_added += 1

            any_resolved_child = True
            order += 1

        if not any_resolved_child:
            with_child_calls_unresolved += 1

    if with_candidates or with_parent_resolved or child_edges_added:
        notes.append(
            "with_edges_v1: "
            f"with_candidates={with_candidates} "
            f"parent_resolved={with_parent_resolved} "
            f"modeled={len(modeled_with_nodes)} "
            f"child_edges_added={child_edges_added} "
            f"with_no_child_calls={with_no_child_calls} "
            f"with_child_calls_unresolved={with_child_calls_unresolved}"
        )

    return FeatureResult(exclude_with_nodes=modeled_with_nodes, exclude_mount_calls=set(), notes=notes)

# -----------------------------
# Feature: mount edges (conservative)
# -----------------------------

def apply_mount_edges_v1(
    *,
    tree: ast.Module,
    ui_syms: Set[str],
    call_to_node_id: Dict[Tuple[int, int, int, int], str],
    edges: List[Any],
    mirror_text: str,
    meta_refs: Dict[str, Any],
    edge_ctor: Callable[..., Any],
    hooks: FeatureHooks,
    edge_kind_field: str = "edge_kind",
) -> FeatureResult:
    """
    Model `.mount(<UIConstructor>(...))` as explicit edges.

    Conservative rule:
    - We only model the child side (must resolve to node_id).
    - The parent is kept as a string receiver ref (e.g. "self._body") to avoid pretending we know runtime identity.

    This feature is useful because it:
    - removes "mount_edges_unmodeled" noise for direct mount patterns
    - preserves a traceable link to where the mount occurred
    - keeps semantics honest (parent not resolved -> represented as ref)
    """
    exclude_calls: Set[int] = set()
    notes: List[str] = []

    # Best-effort: include minimal provenance for each mount call.
    for c in (n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)):
        if c.func.attr != "mount":
            continue

        # Find first argument that is UI constructor call
        child_call: Optional[ast.Call] = None
        for a in list(c.args or []):
            if isinstance(a, ast.Call) and hooks.is_ui_constructor_call(a, ui_syms):
                child_call = a
                break
        if child_call is None:
            continue

        child_id = call_to_node_id.get(_call_key(child_call))
        if not child_id:
            continue

        receiver = c.func.value
        parent_ref = hooks.expr_to_compact_text(receiver) if receiver is not None else "(unknown_receiver)"

        # Provenance
        anchor_ref: Optional[str] = None
        focus: Optional[Dict[str, int]] = None
        block = hooks.enclosing_top_level_block(tree, c)
        if block is not None:
            anchor_ref = hooks.anchor_ref_for_top_level_node(mirror_text, block, meta_refs)
            if anchor_ref:
                anchor = hooks.anchor_data(meta_refs, anchor_ref)
                if anchor:
                    focus = hooks.focus_span_from_block(block, c, anchor)

        # Emit a "mount" edge. We don't assume `parent` is a node_id, so we store it as a string.
        # Your EdgeRec can accept extra fields, or you can standardize on an `edge_kind` attribute.
        e = edge_ctor(
            parent=parent_ref,   # intentionally not a node_id
            child=child_id,
            order_index=0,
            anchor_ref=anchor_ref,
            focus=focus,
        )
        # Best-effort: stamp edge kind if the type supports it.
        try:
            setattr(e, edge_kind_field, "mount")
        except Exception:
            pass

        edges.append(e)
        exclude_calls.add(id(c))

    if exclude_calls:
        notes.append(f"mount_edges_v1: modeled {len(exclude_calls)} mount call(s) (parent as ref)")

    return FeatureResult(exclude_with_nodes=set(), exclude_mount_calls=exclude_calls, notes=notes)
