# repo_ui/query.py
"""
repo_ui.query — UI (Textual-dialect) Layer3 query app (print-only).

Contract: v1 (see conversation lock-in)
- Reads scope:
    shadow_ui/layer3/scope.generated.json  (pipeline-owned, overwritten)
    shadow_ui/layer3/scope.user.json       (user-owned, persistent overlay)
- Reads Layer3:
    shadow_ui/layer3/index.json
    shadow_ui/layer3/tiles/**/<source_rel>.layer3.json

Usage:
  python -m repo_ui.query help [command]
  python -m repo_ui.query tree
  python -m repo_ui.query about

  python -m repo_ui.query scope
  python -m repo_ui.query scope add tiles-root <path>
  python -m repo_ui.query scope remove tiles-root <path>
  python -m repo_ui.query scope set index <path>
  python -m repo_ui.query scope reset

  python -m repo_ui.query list ids [--type T] [--contains S] [--show-locs] [--meta] [--limit N] [--sort id|count] [--file PATH]
  python -m repo_ui.query list types [--limit N] [--sort count|type] [--file PATH]
  python -m repo_ui.query list hashes [--limit N] [--sort count|hash] [--show-locs] [--meta] [--file PATH]
  python -m repo_ui.query list files [--limit N] [--sort constructors|ids|roots|edge-cases]
  python -m repo_ui.query list edge-cases [--kind K] [--samples N] [--show-locs] [--meta] [--limit N] [--file PATH]

  python -m repo_ui.query list css-ids [--contains S] [--show-locs] [--limit N] [--sort id|count]
  python -m repo_ui.query list css-classes [--contains S] [--show-locs] [--limit N] [--sort class|count]

  python -m repo_ui.query count ids [--type T] [--contains S] [--file PATH]
  python -m repo_ui.query count types [--file PATH]
  python -m repo_ui.query count edge-cases [--kind K] [--file PATH]

  python -m repo_ui.query show file <repo_rel_path> [--limit N] [--show-locs] [--meta] [--meta-refs]
  python -m repo_ui.query show css-id <id> [--limit N]
  python -m repo_ui.query show css-class <class> [--limit N]

  python -m repo_ui.query find hash <sha1:...> [--limit N] [--show-locs] [--meta] [--show-canonical]

Notes:
- "meta" = provenance (anchor_ref + focus original-source lines when available)
- "show-locs" expands summaries to occurrences (node/root instances)
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# -----------------------------
# Paths (repo-biased defaults)
# -----------------------------

SCOPE_GENERATED_REL = Path("shadow_ui/layer3/scope.generated.json")
SCOPE_USER_REL = Path("shadow_ui/layer3/scope.user.json")

DEFAULT_INDEX_REL = "shadow_ui/layer3/index.json"
DEFAULT_TILES_ROOTS = ["shadow_ui/layer3/tiles"]
DEFAULT_TILE_SUFFIX = ".layer3.json"
# Layer4 CSS index (produced by css_index_builder.py)
DEFAULT_CSS_INDEX_REL = "shadow_ui/layer4/mirror/css_index.json"
DEFAULT_DISPLAY_LIMIT = 50


# -----------------------------
# Command registry (authoritative for help/tree)
# -----------------------------

REGISTRY: Dict[str, Any] = {
    "meta": {
        "help": {
            "desc": "Show greeting + usage. `help <command>` shows details.",
            "usage": "help [command]",
            "params": [],
            "examples": [
                "python -m repo_ui.query help",
                "python -m repo_ui.query help list",
                "python -m repo_ui.query help scope",
            ],
        },
        "tree": {
            "desc": "Print an ASCII dir-tree of all commands/params.",
            "usage": "tree",
            "params": [],
            "examples": ["python -m repo_ui.query tree"],
        },
        "about": {
            "desc": "Explain what this tool reads, and what it does/doesn't do.",
            "usage": "about",
            "params": [],
            "examples": ["python -m repo_ui.query about"],
        },
        "scope": {
            "desc": "Print or edit query scope/config (generated + user overlay).",
            "usage": "scope | scope add tiles-root <path> | scope remove tiles-root <path> | scope set index <path> | scope reset",
            "params": [],
            "examples": [
                "python -m repo_ui.query scope",
                "python -m repo_ui.query scope add tiles-root shadow_ui/layer3/tiles_extra",
                "python -m repo_ui.query scope set index shadow_ui/layer3/index.json",
            ],
        },
    },
    "query": {
        "list": {
            "desc": "List items (ids/types/hashes/files/edge-cases).",
            "usage": "list <thing> [flags...]",
            "params": [
                ("--limit <N>", "Cap output size (default from scope.display.default_limit, fallback 50)."),
                ("--sort <key>", "Sort key (limited options per thing)."),
                ("--file <repo_rel_path>", "Restrict to a single file tile when supported."),
                ("--show-locs", "Expand summaries into occurrences."),
                ("--meta", "Include provenance (anchor_ref + focus original lines) when showing occurrences."),
            ],
            "examples": [
                "python -m repo_ui.query list ids",
                "python -m repo_ui.query list ids --type Static --show-locs --meta",
                "python -m repo_ui.query list types --limit 30",
                "python -m repo_ui.query list hashes --limit 20",
                "python -m repo_ui.query list files --sort ids --limit 20",
                "python -m repo_ui.query list edge-cases",
            ],
            "things": {
                "ids": {
                    "params": [
                        ("--type <TypeName>", "Only IDs attached to this constructor type."),
                        ("--contains <substr>", "Substring filter on ID value."),
                        ("--sort id|count", "Sort unique IDs lexicographically or by frequency."),
                    ],
                    "examples": [
                        "python -m repo_ui.query list ids",
                        "python -m repo_ui.query list ids --contains confirm",
                        "python -m repo_ui.query list ids --type Static --show-locs --meta",
                    ],
                },
                "types": {
                    "params": [("--sort count|type", "Sort histogram by count desc or name asc.")],
                    "examples": ["python -m repo_ui.query list types --limit 30"],
                },
                "hashes": {
                    "params": [("--sort count|hash", "Sort by frequency desc or hash asc.")],
                    "examples": [
                        "python -m repo_ui.query list hashes --limit 20",
                        "python -m repo_ui.query find hash sha1:abcd... --show-canonical",
                    ],
                },
                "files": {
                    "params": [
                        ("--sort constructors|ids|roots|edge-cases", "Sort by file summary key."),
                    ],
                    "examples": ["python -m repo_ui.query list files --sort ids --limit 20"],
                },
                "edge-cases": {
                    "params": [
                        ("--kind <bucket>", "Filter to one edge-case bucket kind."),
                        ("--samples <N>", "Collect up to N sample occurrences (requires tile scan)."),
                    ],
                    "examples": [
                        "python -m repo_ui.query list edge-cases",
                        "python -m repo_ui.query list edge-cases --kind id_nonliteral --samples 10 --meta",
                    ],
                },
                "css-ids": {
                    "params": [
                        ("--contains <substr>", "Substring filter on CSS ID token (without the leading '#')."),
                        ("--sort id|count", "Sort unique CSS IDs lexicographically or by frequency."),
                        ("--show-locs", "Show occurrence locations (source file + line span + selector)."),
                        ("--limit <N>", "Cap output size."),
                    ],
                    "examples": [
                        "python -m repo_ui.query list css-ids --limit 50",
                        "python -m repo_ui.query list css-ids --contains app",
                        "python -m repo_ui.query show css-id app_root",
                    ],
                },
                "css-classes": {
                    "params": [
                        ("--contains <substr>", "Substring filter on CSS class token (without the leading '.')."),
                        ("--sort class|count", "Sort unique CSS classes lexicographically or by frequency."),
                        ("--show-locs", "Show occurrence locations (source file + line span + selector)."),
                        ("--limit <N>", "Cap output size."),
                    ],
                    "examples": [
                        "python -m repo_ui.query list css-classes --show-locs",
                        "python -m repo_ui.query list css-classes --contains toast",
                        "python -m repo_ui.query show css-class toast",
                    ],
                },

            },
        },
        "count": {
            "desc": "Count items (ids/types/edge-cases).",
            "usage": "count <thing> [flags...]",
            "params": [
                ("--file <repo_rel_path>", "Restrict to a single file tile when supported."),
            ],
            "examples": [
                "python -m repo_ui.query count ids --type Static",
                "python -m repo_ui.query count types",
                "python -m repo_ui.query count edge-cases",
            ],
        },
        "show": {
            "desc": "Show details for one object.",
            "usage": "show file <repo_rel_path> [--show-locs] [--meta] [--meta-refs] [--limit N]",
            "params": [
                ("--show-locs", "Include occurrence-level listings."),
                ("--meta", "Include provenance on occurrence lines."),
                ("--meta-refs", "Print tile meta_refs snippet anchor table."),
                ("--limit <N>", "Cap lists."),
            ],
            "examples": [
                "python -m repo_ui.query show file repo_ui/screens/confirm.py",
                "python -m repo_ui.query show file repo_ui/screens/confirm.py --show-locs --meta --meta-refs",
            ],
        },
        "find": {
            "desc": "Lookup occurrences by exact key.",
            "usage": "find hash <sha1:...> [--show-canonical] [--show-locs] [--meta] [--limit N]",
            "params": [
                ("--show-canonical", "Include canonical serialization when available (may open a tile)."),
                ("--show-locs", "Include occurrences (file/root) — default true for find hash."),
                ("--meta", "Include provenance when available."),
                ("--limit <N>", "Cap occurrences."),
            ],
            "examples": [
                "python -m repo_ui.query find hash sha1:abcd... --show-canonical",
            ],
        },
    },
}


# -----------------------------
# Utilities
# -----------------------------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _norm_rel(path: str) -> str:
    return path.replace("\\", "/").lstrip("./")


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _repo_root_from_cwd() -> str:
    return _abs(str(Path.cwd()))


def _read_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _ensure_user_scope_skeleton(path: Path) -> None:
    if path.exists():
        return
    payload = {
        "contract": "repo_ui_scope_user_v1",
        "layer3": {
            "index_path": None,
            "tiles_roots_add": [],
            "tiles_roots_remove": [],
            "tile_suffix": None,
        },
        "display": {
            "default_limit": DEFAULT_DISPLAY_LIMIT,
            "packet": True,
        },
    }
    _write_json(path, payload)


def _merge_scope(g: Dict[str, Any], u: Dict[str, Any]) -> Dict[str, Any]:
    """
    Effective scope = generated + user overlay
    Rules:
      - Scalars: user overrides if non-null
      - tiles_roots: start with generated.tiles_roots, then add tiles_roots_add, then remove tiles_roots_remove
      - tile_suffix: user overrides if non-null
      - default_limit: user overrides if present
    """
    eff: Dict[str, Any] = {}
    eff["contract"] = "repo_ui_scope_effective_v1"
    eff["generated_at_utc"] = g.get("generated_at_utc")
    eff["repo_root"] = g.get("repo_root")

    gl3 = (g.get("layer3") or {}) if isinstance(g, dict) else {}
    ul3 = (u.get("layer3") or {}) if isinstance(u, dict) else {}
    gdisp = (g.get("display") or {}) if isinstance(g, dict) else {}
    udisp = (u.get("display") or {}) if isinstance(u, dict) else {}

    index_path = ul3.get("index_path", None)
    if index_path is None:
        index_path = gl3.get("index_path", DEFAULT_INDEX_REL)

    tile_suffix = ul3.get("tile_suffix", None)
    if tile_suffix is None:
        tile_suffix = gl3.get("tile_suffix", DEFAULT_TILE_SUFFIX)

    tiles_roots = list(gl3.get("tiles_roots") or DEFAULT_TILES_ROOTS)
    add = list(ul3.get("tiles_roots_add") or [])
    remove = set(_norm_rel(str(x)) for x in (ul3.get("tiles_roots_remove") or []))
    # Normalize and combine
    combined: List[str] = []
    for x in tiles_roots + add:
        xr = _norm_rel(str(x))
        if xr and xr not in combined and xr not in remove:
            combined.append(xr)

    default_limit = udisp.get("default_limit", None)
    if default_limit is None:
        default_limit = gdisp.get("default_limit", None)
    if default_limit is None:
        default_limit = DEFAULT_DISPLAY_LIMIT

    eff["layer3"] = {
        "index_path": _norm_rel(str(index_path)),
        "tiles_roots": combined,
        "tile_suffix": str(tile_suffix),
    }
    eff["display"] = {
        "default_limit": int(default_limit),
        "packet": bool(udisp.get("packet", gdisp.get("packet", True))),
    }
    eff["layer3_contracts"] = g.get("layer3_contracts")
    return eff


@dataclass
class ScopeBundle:
    generated_path: Path
    user_path: Path
    generated: Dict[str, Any]
    user: Dict[str, Any]
    effective: Dict[str, Any]


def _load_scope(repo_root: str) -> ScopeBundle:
    rr = Path(repo_root)
    gen_path = rr / SCOPE_GENERATED_REL
    user_path = rr / SCOPE_USER_REL

    if not gen_path.exists():
        raise SystemExit(f"[repo_ui.query] missing generated scope: {gen_path} (run layer3_pass1 first)")

    _ensure_user_scope_skeleton(user_path)

    g = _read_json(gen_path)
    u = _read_json(user_path)
    eff = _merge_scope(g, u)
    return ScopeBundle(gen_path, user_path, g, u, eff)


def _iter_tiles(repo_root: str, scope: Dict[str, Any]) -> Iterable[Tuple[str, Path]]:
    """
    Iterate over all tiles under effective tiles_roots.
    Yields (tile_rel_to_repo_root, tile_abs_path).
    """
    rr = Path(repo_root)
    l3 = scope.get("layer3") or {}
    roots = l3.get("tiles_roots") or []
    suffix = str(l3.get("tile_suffix") or DEFAULT_TILE_SUFFIX)
    for r in roots:
        base = rr / _norm_rel(str(r))
        if not base.exists():
            continue
        for dirpath, _dirnames, filenames in os.walk(base):
            for fn in filenames:
                if fn.endswith(suffix):
                    p = Path(dirpath) / fn
                    rel = _norm_rel(os.path.relpath(p, rr))
                    yield rel, p


def _tile_path_for_file(repo_root: str, scope: Dict[str, Any], file_rel: str) -> Path:
    rr = Path(repo_root)
    l3 = scope.get("layer3") or {}
    roots = l3.get("tiles_roots") or []
    suffix = str(l3.get("tile_suffix") or DEFAULT_TILE_SUFFIX)

    file_rel = _norm_rel(file_rel)
    for r in roots:
        base = rr / _norm_rel(str(r))
        candidate = base / f"{file_rel}{suffix}"
        if candidate.exists():
            return candidate
    raise SystemExit(f"[repo_ui.query] tile not found for file: {file_rel} (looked under tiles_roots)")


def _load_index(repo_root: str, scope: Dict[str, Any]) -> Tuple[Path, Dict[str, Any]]:
    rr = Path(repo_root)
    idx_rel = _norm_rel(str((scope.get("layer3") or {}).get("index_path") or DEFAULT_INDEX_REL))
    idx_path = rr / idx_rel
    if not idx_path.exists():
        raise SystemExit(f"[repo_ui.query] missing layer3 index: {idx_path} (run layer3_pass1 first)")
    return idx_path, _read_json(idx_path)

def _load_css_index(repo_root: str) -> Tuple[Path, Dict[str, Any]]:
    rr = Path(repo_root)
    p = rr / _norm_rel(DEFAULT_CSS_INDEX_REL)
    if not p.exists():
        raise SystemExit(f"[repo_ui.query] missing css index: {p} (run: python -m repo_ui)")
    return p, _read_json(p)

def _prov_str(prov: Dict[str, Any]) -> str:
    if not isinstance(prov, dict):
        return ""
    a = prov.get("anchor_ref", None)
    f = prov.get("focus", None)
    if isinstance(f, dict) and "start_line" in f and "end_line" in f:
        return f"anchor={a} focus=L{int(f['start_line'])}-L{int(f['end_line'])}"
    if a is not None:
        return f"anchor={a}"
    return ""


def _print_packet_header(repo_root: str, command_line: str, sources: List[str], warnings: List[str]) -> None:
    print("=== repo_ui.query PACKET ===")
    print(f"timestamp: {_now()}")
    print(f"repo_root: {repo_root}")
    print(f"command: {command_line}")
    if sources:
        print("sources:")
        for s in sources:
            print(f"  - {s}")
    if warnings:
        print("warnings:")
        for w in warnings:
            print(f"  - {w}")
    print("=== BEGIN ===")


def _print_packet_footer() -> None:
    print("=== END ===")


def _reconstruct_command_line(argv: Sequence[str]) -> str:
    # Provide a stable reconstruction (no quoting complexity)
    return "python -m repo_ui.query " + " ".join(argv)


# -----------------------------
# Help / tree / about
# -----------------------------

def cmd_tree() -> None:
    # "No pools/categories" — tree is verbs + sub-structure.
    print("repo_ui.query/")
    print("├─ help [command]")
    print("├─ tree")
    print("├─ about")
    print("├─ scope")
    print("│  ├─ (print generated/user/effective scope)")
    print("│  ├─ add tiles-root <path>")
    print("│  ├─ remove tiles-root <path>")
    print("│  ├─ set index <path>")
    print("│  └─ reset")
    print("├─ list <thing>")
    print("│  ├─ ids [--type <T>] [--contains <s>] [--sort id|count] [--show-locs] [--meta] [--limit N] [--file <path>]")
    print("│  ├─ types [--sort count|type] [--limit N] [--file <path>]")
    print("│  ├─ hashes [--sort count|hash] [--show-locs] [--meta] [--limit N] [--file <path>]")
    print("│  ├─ files [--sort constructors|ids|roots|edge-cases] [--limit N]")
    print("│  ├─ edge-cases [--kind <k>] [--samples N] [--show-locs] [--meta] [--limit N] [--file <path>]")
    print("│  ├─ css-ids [--contains <s>] [--sort id|count] [--show-locs] [--limit N]")
    print("│  └─ css-classes [--contains <s>] [--sort class|count] [--show-locs] [--limit N]")
    print("├─ count <thing>")
    print("│  ├─ ids [--type <T>] [--contains <s>] [--file <path>]")
    print("│  ├─ types [--file <path>]")
    print("│  └─ edge-cases [--kind <k>] [--file <path>]")
    print("├─ show file <repo_rel_path>")
    print("│  ├─ [--show-locs]")
    print("│  ├─ [--meta]")
    print("│  ├─ [--meta-refs]")
    print("│  └─ [--limit N]")
    print("├─ show css-id <id> [--limit N]")
    print("└─ show css-class <class> [--limit N]")
    print("   ")
    print("find/")
    print("└─ find hash <sha1:...>")
    print("   ├─ [--show-canonical]")
    print("   ├─ [--show-locs]")
    print("   ├─ [--meta]")
    print("   └─ [--limit N]")

def cmd_help(topic: Optional[str]) -> None:
    if not topic:
        print("repo_ui.query — Layer3 + Layer4(CSS) UI state query app (print-only)")
        print()
        print("Reads:")
        print(f"  - {SCOPE_GENERATED_REL.as_posix()}  (generated by layer3_pass1)")
        print(f"  - {SCOPE_USER_REL.as_posix()}       (user overlay; editable via scope commands)")
        print("  - <effective scope>.layer3.index + tiles")
        print(f"  - {DEFAULT_CSS_INDEX_REL}  (generated by css_index_builder; created by `python -m repo_ui`)")
        print()
        print("Usage:")
        print("  python -m repo_ui.query <command> ...")
        print()
        print("Commands:")
        print("  help [command]        Show usage (this).")
        print("  tree                  Show full command map (ASCII).")
        print("  about                 What this tool does/reads.")
        print("  scope                 Print/edit config scope.")
        print("  list <thing>          List layer3 ids/types/hashes/files/edge-cases; plus css-ids/css-classes.")
        print("  count <thing>         Count ids/types/edge-cases.")
        print("  show file <path>      Drill into a single file tile.")
        print("  show css-id <id>      Locate CSS rules that reference an ID selector (#id).")
        print("  show css-class <cls>  Locate CSS rules that reference a class selector (.class).")
        print("  find hash <hash>      Lookup a layout hash.")
        print()
        print("Global flags (where meaningful):")
        print("  --limit N     Cap output size.")
        print("  --show-locs   Expand summaries into occurrences.")
        print("  --meta        Attach provenance to occurrence lines (Layer3 tiles).")
        print()
        print("See everything at once:")
        print("  python -m repo_ui.query tree")
        print()
        print("Examples (Layer 3):")
        print("  python -m repo_ui.query list ids")
        print("  python -m repo_ui.query list ids --type Static --show-locs --meta")
        print("  python -m repo_ui.query list types --limit 30")
        print("  python -m repo_ui.query find hash sha1:... --show-canonical")
        print("  python -m repo_ui.query show file repo_ui/screens/confirm.py --show-locs --meta --meta-refs")
        print()
        print("Examples (CSS):")
        print("  python -m repo_ui.query list css-ids --limit 50")
        print("  python -m repo_ui.query list css-classes --show-locs")
        print("  python -m repo_ui.query show css-id app_root")
        print("  python -m repo_ui.query show css-class toast")
        return

    # For any non-empty topic, keep existing topic-handling logic below.
    # (Do not modify; registry-driven help continues to work.)

    t = topic.strip().lower()
    # Detail help: show info for a verb
    if t in ("help", "tree", "about", "scope", "list", "count", "show", "find"):
        # Pull from registry where possible
        if t in REGISTRY["meta"]:
            rec = REGISTRY["meta"][t]
        else:
            rec = REGISTRY["query"][t]

        print(f"{t} — {rec.get('desc','')}")
        print(f"usage: python -m repo_ui.query {rec.get('usage','')}")

        if rec.get("params"):
            print("\nparams:")
            for p, d in rec["params"]:
                print(f"  {p:<24} {d}")

        if t == "list":
            things = rec.get("things") or {}
            print("\nthings:")
            for k in sorted(things.keys()):
                print(f"  - {k}")

            # If list.things includes nested aliases, show them too (optional, safe)
            # (Does nothing if there are no aliases.)
            aliases = rec.get("aliases") or {}
            if aliases:
                print("\naliases:")
                for a, target in sorted(aliases.items()):
                    print(f"  - {a} -> {target}")

            print("\nexamples:")
            for ex in rec.get("examples") or []:
                print(f"  {ex}")

            print("\nMore:")
            print("  Use: python -m repo_ui.query tree")
            return

        print("\nexamples:")
        for ex in rec.get("examples") or []:
            print(f"  {ex}")
        return


    # Detail help: "help list ids" style isn’t a separate contract,
    # but we can support "help ids" to print list-thing specifics.
    if t in ("ids", "types", "hashes", "files", "edge-cases", "edge_cases", "css-ids", "css_ids", "css-classes", "css_classes"):
        if t in ("edge-cases", "edge_cases"):
            key = "edge-cases"
        elif t in ("css-ids", "css_ids"):
            key = "css-ids"
        elif t in ("css-classes", "css_classes"):
            key = "css-classes"
        else:
            key = t
        rec = REGISTRY["query"]["list"]["things"][key]
        print(f"list {key} — params")
        for p, d in rec.get("params") or []:
            print(f"  {p:<24} {d}")
        print("\nexamples:")
        for ex in rec.get("examples") or []:
            print(f"  {ex}")
        return

    print(f"unknown help topic: {topic}")
    print("Try: python -m repo_ui.query tree")


def cmd_about(repo_root: str, scope: ScopeBundle) -> None:
    eff = scope.effective
    print("repo_ui.query — about")
    print()
    print("What it is:")
    print("  - A small, print-only query app for UI Layer3 state (pass1).")
    print("  - Repo-biased by default, but scope is configurable via scope.user.json.")
    print()
    print("What it reads:")
    print(f"  - generated scope: {_norm_rel(str(scope.generated_path.relative_to(repo_root)))}")
    print(f"  - user overlay:    {_norm_rel(str(scope.user_path.relative_to(repo_root)))}")
    print(f"  - layer3 index:    {eff['layer3']['index_path']}")
    print("  - layer3 tiles roots:")
    for r in eff["layer3"]["tiles_roots"]:
        print(f"      - {r}")
    print()
    print("What it does NOT do:")
    print("  - It does not crawl source code; it only reads Layer3 JSON artifacts.")
    print("  - It does not infer behavior; it reports structural/style evidence only.")
    print()
    print("If state is missing:")
    print("  - Run repo_ui pipeline (layer3_pass1) to generate tiles/index/scope.generated.json.")


# -----------------------------
# Scope commands
# -----------------------------

def scope_print(repo_root: str, scope: ScopeBundle) -> None:
    print("# scope.generated.json")
    print(json.dumps(scope.generated, ensure_ascii=False, indent=2))
    print("\n# scope.user.json")
    print(json.dumps(scope.user, ensure_ascii=False, indent=2))
    print("\n# scope.effective")
    print(json.dumps(scope.effective, ensure_ascii=False, indent=2))


def scope_add_tiles_root(repo_root: str, scope: ScopeBundle, path: str) -> None:
    p = _norm_rel(path)
    u = scope.user
    u.setdefault("layer3", {})
    add = u["layer3"].setdefault("tiles_roots_add", [])
    if p not in add:
        add.append(p)
    _write_json(scope.user_path, u)
    # Reload to show effective
    new_scope = _load_scope(repo_root)
    print(f"OK: added tiles-root: {p}")
    scope_print(repo_root, new_scope)


def scope_remove_tiles_root(repo_root: str, scope: ScopeBundle, path: str) -> None:
    p = _norm_rel(path)
    u = scope.user
    u.setdefault("layer3", {})
    rem = u["layer3"].setdefault("tiles_roots_remove", [])
    if p not in rem:
        rem.append(p)
    _write_json(scope.user_path, u)
    new_scope = _load_scope(repo_root)
    print(f"OK: removed tiles-root (by overlay removal): {p}")
    scope_print(repo_root, new_scope)


def scope_set_index(repo_root: str, scope: ScopeBundle, path: str) -> None:
    p = _norm_rel(path)
    u = scope.user
    u.setdefault("layer3", {})
    u["layer3"]["index_path"] = p
    _write_json(scope.user_path, u)
    new_scope = _load_scope(repo_root)
    print(f"OK: set index override: {p}")
    scope_print(repo_root, new_scope)


def scope_reset(repo_root: str, scope: ScopeBundle) -> None:
    if scope.user_path.exists():
        scope.user_path.unlink()
    _ensure_user_scope_skeleton(scope.user_path)
    new_scope = _load_scope(repo_root)
    print("OK: reset scope.user.json to skeleton")
    scope_print(repo_root, new_scope)


# -----------------------------
# Query implementation helpers
# -----------------------------

def _apply_limit(items: List[Any], limit: int) -> List[Any]:
    return items[: max(0, int(limit))]


def _maybe_int(x: Any, default: int) -> int:
    try:
        return int(x)
    except Exception:
        return default


def _warn_cwd_differs(repo_root: str, scope: ScopeBundle) -> Optional[str]:
    gen_root = scope.generated.get("repo_root", None)
    if isinstance(gen_root, str) and gen_root and _abs(gen_root) != _abs(repo_root):
        return f"cwd differs from generated repo_root ({gen_root})"
    return None


def _read_tile(p: Path) -> Dict[str, Any]:
    return _read_json(p)


def _file_filter_match(tile: Dict[str, Any], want_file: Optional[str]) -> bool:
    if not want_file:
        return True
    src = (tile.get("source") or {}).get("source_rel", "")
    return _norm_rel(str(src)) == _norm_rel(want_file)

def _print_css_refs(refs: List[Dict[str, Any]], limit: int) -> None:
    refs = refs[: max(0, int(limit))]
    if not refs:
        print("- (none)")
        return
    for r in refs:
        src = r.get("source_rel", "")
        loc = r.get("loc", {})
        sel = r.get("selector_text", "")
        rule_i = r.get("rule_i", None)
        print(f"- rule={rule_i} {src} {loc}  {sel}")


def list_css_ids(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    contains: Optional[str],
    show_locs: bool,
    sort_key: str,
    limit: int,
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    css_path, css = _load_css_index(repo_root)
    sources = [_norm_rel(str(css_path.relative_to(repo_root)))]

    contains_l = (contains or "").lower().strip()
    idx = css.get("id_index") or {}

    items: List[Tuple[str, int]] = [(k, len(v or [])) for k, v in idx.items()]
    if contains_l:
        items = [(k, c) for (k, c) in items if contains_l in k.lower()]

    if sort_key == "count":
        items.sort(key=lambda t: (-t[1], t[0]))
    else:
        items.sort(key=lambda t: t[0])

    items = _apply_limit(items, limit)

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# css-ids (IDs referenced in CSS selectors)\n")

    if not items:
        print("- (none)")
        _print_packet_footer()
        return

    if not show_locs:
        for k, c in items:
            print(f"- `{k}` count={c}")
        _print_packet_footer()
        return

    for k, c in items:
        print(f"\n## {k}  count={c}")
        _print_css_refs(idx.get(k) or [], limit)

    _print_packet_footer()


def list_css_classes(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    contains: Optional[str],
    show_locs: bool,
    sort_key: str,
    limit: int,
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    css_path, css = _load_css_index(repo_root)
    sources = [_norm_rel(str(css_path.relative_to(repo_root)))]

    contains_l = (contains or "").lower().strip()
    idx = css.get("class_index") or {}

    items: List[Tuple[str, int]] = [(k, len(v or [])) for k, v in idx.items()]
    if contains_l:
        items = [(k, c) for (k, c) in items if contains_l in k.lower()]

    if sort_key == "count":
        items.sort(key=lambda t: (-t[1], t[0]))
    elif sort_key == "class":
        items.sort(key=lambda t: t[0])
    else:
        items.sort(key=lambda t: t[0])

    items = _apply_limit(items, limit)

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# css-classes (classes referenced in CSS selectors)\n")

    if not items:
        print("- (none)")
        _print_packet_footer()
        return

    if not show_locs:
        for k, c in items:
            print(f"- `{k}` count={c}")
        _print_packet_footer()
        return

    for k, c in items:
        print(f"\n## {k}  count={c}")
        _print_css_refs(idx.get(k) or [], limit)

    _print_packet_footer()


def show_css_id(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    id_value: str,
    limit: int,
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    css_path, css = _load_css_index(repo_root)
    sources = [_norm_rel(str(css_path.relative_to(repo_root)))]

    idv = id_value.strip()
    refs = (css.get("id_index") or {}).get(idv) or []

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# show css-id\n")
    print(f"- id: `{idv}`")
    print(f"- matches: {len(refs)}\n")
    _print_css_refs(list(refs), limit)
    _print_packet_footer()


def show_css_class(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    class_value: str,
    limit: int,
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    css_path, css = _load_css_index(repo_root)
    sources = [_norm_rel(str(css_path.relative_to(repo_root)))]

    cv = class_value.strip()
    refs = (css.get("class_index") or {}).get(cv) or []

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# show css-class\n")
    print(f"- class: `{cv}`")
    print(f"- matches: {len(refs)}\n")
    _print_css_refs(list(refs), limit)
    _print_packet_footer()

# -----------------------------
# list/count/show/find commands
# -----------------------------

def list_ids(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    want_type: Optional[str],
    contains: Optional[str],
    show_locs: bool,
    meta: bool,
    sort_key: str,
    limit: int,
    file_filter: Optional[str],
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    eff = scope.effective
    sources: List[str] = []
    idx_path, idx = _load_index(repo_root, eff)
    sources.append(_norm_rel(str(idx_path.relative_to(repo_root))))

    contains_l = (contains or "").lower().strip()
    want_type = (want_type or "").strip() or None

    # Fast path: unique ids, no type filter, no locations/meta needed
    if not want_type and not show_locs and not meta:
        id_index = idx.get("id_index") or {}
        # id_index shape: id -> list of occurrences (file/node_id/tile_rel)
        ids = sorted(id_index.keys())
        if contains_l:
            ids = [i for i in ids if contains_l in i.lower()]
        ids = _apply_limit(ids, limit)

        _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
        print("# ids (unique, literal only)\n")
        if not ids:
            print("- (none)")
        else:
            for i in ids:
                print(f"- `{i}`")
        _print_packet_footer()
        return

    # Otherwise scan tiles (need type filter or occurrences/meta)
    sources.append(_norm_rel(str(Path(repo_root) / (eff["layer3"]["tiles_roots"][0]) if eff["layer3"]["tiles_roots"] else "shadow_ui/layer3/tiles")))
    hits: List[Tuple[str, str, str, str]] = []
    # (id_value, type, file, linekey)

    for _tile_rel, p in _iter_tiles(repo_root, eff):
        tile = _read_tile(p)
        if file_filter and not _file_filter_match(tile, file_filter):
            continue
        src_file = _norm_rel(str((tile.get("source") or {}).get("source_rel", "") or ""))
        nodes = ((tile.get("pools") or {}).get("constructors") or {}).get("nodes") or []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            typ = str(n.get("type") or "")
            if want_type and typ != want_type:
                continue
            id_obj = n.get("id") or {}
            if not isinstance(id_obj, dict) or id_obj.get("kind") != "literal":
                continue
            idv = str(id_obj.get("value") or "")
            if not idv:
                continue
            if contains_l and contains_l not in idv.lower():
                continue

            prov = n.get("provenance") or {}
            # stable linekey for ordering
            focus = prov.get("focus") if isinstance(prov, dict) else None
            start_line = focus.get("start_line") if isinstance(focus, dict) else None
            linekey = f"{src_file}:{start_line if start_line is not None else 10**9}:{n.get('node_id','')}"
            hits.append((idv, typ, src_file, linekey))

    # Sort
    if sort_key == "count":
        # count unique id frequency
        freq: Dict[str, int] = {}
        for idv, _typ, _f, _k in hits:
            freq[idv] = freq.get(idv, 0) + 1
        ids_sorted = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))
        ids_sorted = ids_sorted[:limit]

        _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
        print("# ids (unique, sorted by count)\n")
        if not ids_sorted:
            print("- (none)")
        else:
            for idv, c in ids_sorted:
                print(f"- `{idv}` count={c}")
        if meta and not show_locs:
            print("\n(note) --meta has no per-item provenance in unique-only mode; use --show-locs.")
        _print_packet_footer()
        return

    # Default: id sort
    if not show_locs:
        uniq = sorted(set(h[0] for h in hits))
        uniq = _apply_limit(uniq, limit)
        _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
        print("# ids (unique, literal only)\n")
        if not uniq:
            print("- (none)")
        else:
            for idv in uniq:
                print(f"- `{idv}`")
        if meta:
            print("\n(note) --meta has no per-item provenance in unique-only mode; use --show-locs.")
        _print_packet_footer()
        return

    # show-locs mode: occurrences
    hits.sort(key=lambda t: (t[0], t[2], t[3], t[1]))
    hits = _apply_limit(hits, limit)

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# ids (occurrences; literal only)\n")
    if not hits:
        print("- (none)")
        _print_packet_footer()
        return

    # To attach provenance/node_id reliably, we need to read nodes again per file occurrence.
    # Keep it simple: rescan tiles but only print matching lines until limit reached.
    printed = 0
    cur_id: Optional[str] = None
    for _tile_rel, p in _iter_tiles(repo_root, eff):
        if printed >= limit:
            break
        tile = _read_tile(p)
        if file_filter and not _file_filter_match(tile, file_filter):
            continue
        src_file = _norm_rel(str((tile.get("source") or {}).get("source_rel", "") or ""))
        nodes = ((tile.get("pools") or {}).get("constructors") or {}).get("nodes") or []
        for n in nodes:
            if printed >= limit:
                break
            typ = str(n.get("type") or "")
            if want_type and typ != want_type:
                continue
            id_obj = n.get("id") or {}
            if not isinstance(id_obj, dict) or id_obj.get("kind") != "literal":
                continue
            idv = str(id_obj.get("value") or "")
            if contains_l and contains_l not in idv.lower():
                continue
            if idv not in set(h[0] for h in hits):
                continue  # only those in capped hits

            if idv != cur_id:
                cur_id = idv
                print(f"\n## {idv}")
            node_id = str(n.get("node_id") or "?")
            line = f"- type={typ} {src_file} {node_id}"
            if meta:
                prov = n.get("provenance") or {}
                ps = _prov_str(prov if isinstance(prov, dict) else {})
                if ps:
                    line += f"  {ps}"
            print(line)
            printed += 1

    _print_packet_footer()


def list_types(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    sort_key: str,
    limit: int,
    file_filter: Optional[str],
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    eff = scope.effective
    sources = [_norm_rel(str((Path(repo_root) / eff["layer3"]["index_path"]).as_posix()))]
    sources.append("shadow_ui/layer3/tiles/**")

    counts: Dict[str, int] = {}
    literal_ids: Dict[str, int] = {}

    for _tile_rel, p in _iter_tiles(repo_root, eff):
        tile = _read_tile(p)
        if file_filter and not _file_filter_match(tile, file_filter):
            continue
        nodes = ((tile.get("pools") or {}).get("constructors") or {}).get("nodes") or []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            t = str(n.get("type") or "")
            if not t:
                continue
            counts[t] = counts.get(t, 0) + 1
            id_obj = n.get("id") or {}
            if isinstance(id_obj, dict) and id_obj.get("kind") == "literal":
                literal_ids[t] = literal_ids.get(t, 0) + 1

    items = list(counts.items())
    if sort_key == "type":
        items.sort(key=lambda kv: kv[0])
    else:
        items.sort(key=lambda kv: (-kv[1], kv[0]))
    items = _apply_limit(items, limit)

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# types (constructor nodes)\n")
    if not items:
        print("- (none)")
    else:
        for t, c in items:
            lid = literal_ids.get(t, 0)
            print(f"- `{t}` count={c} literal_ids={lid}")
    _print_packet_footer()


def list_hashes(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    show_locs: bool,
    meta: bool,
    sort_key: str,
    limit: int,
    file_filter: Optional[str],
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    eff = scope.effective
    idx_path, idx = _load_index(repo_root, eff)
    sources = [_norm_rel(str(idx_path.relative_to(repo_root)))]

    layout_index = idx.get("layout_index") or {}
    # layout_index: hash -> [ {file, root_id, tile_rel} ... ]
    # We compute frequencies from index (fast).
    freq = [(h, len(v or [])) for h, v in layout_index.items()]
    if sort_key == "hash":
        freq.sort(key=lambda t: t[0])
    else:
        freq.sort(key=lambda t: (-t[1], t[0]))
    freq = _apply_limit(freq, limit)

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# hashes (layout_hash frequency)\n")
    if not freq:
        print("- (none)")
        _print_packet_footer()
        return

    for h, c in freq:
        print(f"- `{h}` count={c}")

    if show_locs or meta:
        print("\n(note) To lookup occurrences/canonical, use:")
        print("  python -m repo_ui.query find hash <sha1:...> [--show-canonical] [--meta]")

    _print_packet_footer()


def list_files(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    sort_key: str,
    limit: int,
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    eff = scope.effective
    sources = ["shadow_ui/layer3/tiles/**"]

    rows: List[Tuple[str, int, int, int, int]] = []
    # file, constructors, literal_ids, roots, edge_case_total
    for _tile_rel, p in _iter_tiles(repo_root, eff):
        tile = _read_tile(p)
        src_file = _norm_rel(str((tile.get("source") or {}).get("source_rel", "") or ""))
        stats = tile.get("stats") or {}
        constructors = _maybe_int(stats.get("constructor_calls"), 0)
        literal_ids = _maybe_int(stats.get("literal_ids"), 0)
        roots = _maybe_int(stats.get("roots"), 0)
        edge_b = (tile.get("edge_cases") or {}).get("buckets") or []
        edge_total = sum(_maybe_int(b.get("count"), 0) for b in edge_b if isinstance(b, dict))
        rows.append((src_file, constructors, literal_ids, roots, edge_total))

    if sort_key == "constructors":
        rows.sort(key=lambda r: (-r[1], r[0]))
    elif sort_key == "ids":
        rows.sort(key=lambda r: (-r[2], r[0]))
    elif sort_key == "roots":
        rows.sort(key=lambda r: (-r[3], r[0]))
    elif sort_key == "edge-cases":
        rows.sort(key=lambda r: (-r[4], r[0]))
    else:
        rows.sort(key=lambda r: (-r[2], r[0]))  # sensible default: ids

    rows = _apply_limit(rows, limit)

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# files (tile summaries)\n")
    if not rows:
        print("- (none)")
    else:
        for f, c, ids, roots, edge_total in rows:
            print(f"- `{f}` constructors={c} literal_ids={ids} roots={roots} edge_cases={edge_total}")
    _print_packet_footer()


def list_edge_cases(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    kind: Optional[str],
    samples: int,
    show_locs: bool,
    meta: bool,
    limit: int,
    file_filter: Optional[str],
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    eff = scope.effective
    idx_path, idx = _load_index(repo_root, eff)
    sources = [_norm_rel(str(idx_path.relative_to(repo_root)))]

    want_kind = (kind or "").strip() or None

    # Repo-wide bucket counts (from index)
    buckets = ((idx.get("edge_cases") or {}).get("buckets")) or []
    bucket_rows: List[Tuple[str, int]] = []
    for b in buckets:
        if not isinstance(b, dict):
            continue
        k = str(b.get("kind") or "")
        c = _maybe_int(b.get("count"), 0)
        if not k:
            continue
        if want_kind and k != want_kind:
            continue
        bucket_rows.append((k, c))
    bucket_rows.sort(key=lambda t: (-t[1], t[0]))

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# edge-cases (bucket counts)\n")
    if not bucket_rows:
        print("- (none)")
    else:
        for k, c in bucket_rows[:limit]:
            print(f"- {k}: {c}")

    # Samples require scanning tiles (optional)
    samples = int(samples or 0)
    if samples > 0 or show_locs:
        print("\n# samples\n")
        sources.append("shadow_ui/layer3/tiles/**")
        found = 0
        for _tile_rel, p in _iter_tiles(repo_root, eff):
            if found >= samples:
                break
            tile = _read_tile(p)
            if file_filter and not _file_filter_match(tile, file_filter):
                continue
            src_file = _norm_rel(str((tile.get("source") or {}).get("source_rel", "") or ""))
            smp = ((tile.get("edge_cases") or {}).get("samples")) or []
            for s in smp:
                if found >= samples:
                    break
                if not isinstance(s, dict):
                    continue
                k = str(s.get("kind") or "")
                if want_kind and k != want_kind:
                    continue
                line = f"- {k} {src_file}"
                if meta:
                    prov = s.get("provenance") or {}
                    ps = _prov_str(prov if isinstance(prov, dict) else {})
                    if ps:
                        line += f"  {ps}"
                msg = s.get("message")
                if msg:
                    line += f"  msg={msg}"
                print(line)
                found += 1
        if found == 0:
            print("- (none)")
        elif found < samples:
            print(f"\n(note) found {found} samples (< requested {samples})")

    _print_packet_footer()


def count_ids(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    want_type: Optional[str],
    contains: Optional[str],
    file_filter: Optional[str],
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    eff = scope.effective
    sources = ["shadow_ui/layer3/tiles/**"]

    contains_l = (contains or "").lower().strip()
    want_type = (want_type or "").strip() or None

    uniq: set[str] = set()
    occ = 0
    for _tile_rel, p in _iter_tiles(repo_root, eff):
        tile = _read_tile(p)
        if file_filter and not _file_filter_match(tile, file_filter):
            continue
        nodes = ((tile.get("pools") or {}).get("constructors") or {}).get("nodes") or []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            typ = str(n.get("type") or "")
            if want_type and typ != want_type:
                continue
            id_obj = n.get("id") or {}
            if not isinstance(id_obj, dict) or id_obj.get("kind") != "literal":
                continue
            idv = str(id_obj.get("value") or "")
            if contains_l and contains_l not in idv.lower():
                continue
            uniq.add(idv)
            occ += 1

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# count ids\n")
    print(f"unique_ids={len(uniq)} occurrences={occ}")
    _print_packet_footer()


def count_types(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    file_filter: Optional[str],
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    eff = scope.effective
    sources = ["shadow_ui/layer3/tiles/**"]

    counts: Dict[str, int] = {}
    for _tile_rel, p in _iter_tiles(repo_root, eff):
        tile = _read_tile(p)
        if file_filter and not _file_filter_match(tile, file_filter):
            continue
        nodes = ((tile.get("pools") or {}).get("constructors") or {}).get("nodes") or []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            t = str(n.get("type") or "")
            if not t:
                continue
            counts[t] = counts.get(t, 0) + 1

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# count types\n")
    print(f"types={len(counts)} total_nodes={sum(counts.values())}")
    _print_packet_footer()


def count_edge_cases(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    kind: Optional[str],
    file_filter: Optional[str],
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    eff = scope.effective
    idx_path, idx = _load_index(repo_root, eff)
    sources = [_norm_rel(str(idx_path.relative_to(repo_root)))]

    want_kind = (kind or "").strip() or None
    buckets = ((idx.get("edge_cases") or {}).get("buckets")) or []
    total = 0
    for b in buckets:
        if not isinstance(b, dict):
            continue
        k = str(b.get("kind") or "")
        c = _maybe_int(b.get("count"), 0)
        if want_kind and k != want_kind:
            continue
        total += c

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# count edge-cases\n")
    if want_kind:
        print(f"bucket={want_kind} count={total}")
    else:
        print(f"total_edge_cases={total}")
    _print_packet_footer()


def show_file(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    file_rel: str,
    limit: int,
    show_locs: bool,
    meta: bool,
    meta_refs: bool,
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    eff = scope.effective
    tile_path = _tile_path_for_file(repo_root, eff, file_rel)
    sources = [_norm_rel(str(tile_path.relative_to(repo_root)))]

    tile = _read_tile(tile_path)
    src = tile.get("source") or {}
    stats = tile.get("stats") or {}
    dialect = tile.get("dialect") or {}

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# file (layer3 tile)\n")
    print(f"- file: `{_norm_rel(str(src.get('source_rel') or file_rel))}`")
    print(f"- mirror: `{_norm_rel(str(src.get('mirror_rel') or ''))}`")
    print(f"- source_sha1: {src.get('source_sha1', None)}")
    print(f"- ui_symbols_used: {len(dialect.get('ui_symbols_used') or [])}")
    print(f"- constructor_calls: {stats.get('constructor_calls', 0)}")
    print(f"- literal_ids: {stats.get('literal_ids', 0)}")
    print(f"- roots: {stats.get('roots', 0)}")
    print(f"- trees: {stats.get('trees', 0)}")

    # Hash items
    hash_items = ((tile.get("pools") or {}).get("hashes") or {}).get("items") or []
    if hash_items:
        print("\n## hashes\n")
        for it in _apply_limit(list(hash_items), limit):
            h = it.get("layout_hash")
            rid = it.get("root_id")
            line = f"- root={rid} `{h}`"
            if meta:
                prov = it.get("provenance") or {}
                ps = _prov_str(prov if isinstance(prov, dict) else {})
                if ps:
                    line += f"  {ps}"
            print(line)

    # IDs
    ids_index = tile.get("indexes") or {}
    ids_by_value = ids_index.get("ids_by_value") or {}
    ids = sorted(ids_by_value.keys())
    ids = _apply_limit(ids, limit)

    print("\n## ids (unique)\n")
    if not ids:
        print("- (none)")
    else:
        for i in ids:
            print(f"- `{i}`")

    if show_locs:
        print("\n## id occurrences\n")
        nodes = ((tile.get("pools") or {}).get("constructors") or {}).get("nodes") or []
        printed = 0
        for n in nodes:
            if printed >= limit:
                break
            if not isinstance(n, dict):
                continue
            id_obj = n.get("id") or {}
            if not isinstance(id_obj, dict) or id_obj.get("kind") != "literal":
                continue
            idv = str(id_obj.get("value") or "")
            if not idv:
                continue
            typ = str(n.get("type") or "")
            node_id = str(n.get("node_id") or "?")
            line = f"- `{idv}` type={typ} node={node_id}"
            if meta:
                prov = n.get("provenance") or {}
                ps = _prov_str(prov if isinstance(prov, dict) else {})
                if ps:
                    line += f"  {ps}"
            print(line)
            printed += 1
        if printed == 0:
            print("- (none)")

    # Edge cases buckets
    b = (tile.get("edge_cases") or {}).get("buckets") or []
    print("\n## edge-cases (buckets)\n")
    if not b:
        print("- (none)")
    else:
        for it in b[:limit]:
            if isinstance(it, dict):
                print(f"- {it.get('kind','?')}: {int(it.get('count',0) or 0)}")

    # Optional: meta_refs snippet table
    if meta_refs:
        print("\n## meta-refs (snippets)\n")
        snippets = (tile.get("meta_refs") or {}).get("snippets") or {}
        if not snippets:
            print("- (none)")
        else:
            # Deterministic order by ref
            for ref in sorted(snippets.keys()):
                rec = snippets[ref]
                if not isinstance(rec, dict):
                    continue
                print(
                    f"- {ref} kind={rec.get('snippet_kind','')} sha1={rec.get('snippet_sha1','')} "
                    f"L{rec.get('start_line','?')}-L{rec.get('end_line','?')}"
                )

    _print_packet_footer()


def find_hash(
    repo_root: str,
    scope: ScopeBundle,
    argv: Sequence[str],
    hash_value: str,
    limit: int,
    show_locs: bool,
    meta: bool,
    show_canonical: bool,
) -> None:
    warnings: List[str] = []
    w = _warn_cwd_differs(repo_root, scope)
    if w:
        warnings.append(w)

    eff = scope.effective
    idx_path, idx = _load_index(repo_root, eff)
    sources = [_norm_rel(str(idx_path.relative_to(repo_root)))]

    hv = hash_value.strip()
    layout_index = idx.get("layout_index") or {}
    occ = list(layout_index.get(hv) or [])
    occ = occ[:limit]

    _print_packet_header(_repo_root_from_cwd(), _reconstruct_command_line(argv), sources, warnings)
    print("# find hash\n")
    if not occ:
        print(f"- (none)  hash not found: `{hv}`")
        _print_packet_footer()
        return

    # canonical requires opening a tile
    canonical: Optional[str] = None
    if show_canonical:
        # open first available occurrence tile and locate canonical
        for o in occ:
            tile_rel = o.get("tile_rel")
            if not tile_rel:
                continue
            tp = Path(repo_root) / _norm_rel(str(tile_rel))
            if tp.exists():
                sources.append(_norm_rel(str(tp.relative_to(repo_root))))
                tile = _read_tile(tp)
                items = ((tile.get("pools") or {}).get("hashes") or {}).get("items") or []
                for it in items:
                    if isinstance(it, dict) and it.get("layout_hash") == hv:
                        canonical = it.get("canonical")
                        break
            if canonical:
                break

    if show_canonical:
        print("\n## canonical\n")
        print(canonical if canonical else "(canonical_not_found)")

    # occurrences
    if show_locs or True:
        print("\n## occurrences\n")
        for o in occ:
            f = _norm_rel(str(o.get("file") or ""))
            rid = str(o.get("root_id") or "")
            line = f"- `{f}` root={rid}"
            # meta is only available if we open the tile; v1: keep it optional and best-effort
            # (We won't open every tile for meta; that would be expensive.)
            if meta and o.get("tile_rel") and not show_canonical:
                # best-effort: open just this tile until we find provenance for this root_id
                tp = Path(repo_root) / _norm_rel(str(o.get("tile_rel")))
                if tp.exists():
                    tile = _read_tile(tp)
                    items = ((tile.get("pools") or {}).get("hashes") or {}).get("items") or []
                    for it in items:
                        if isinstance(it, dict) and it.get("root_id") == rid and it.get("layout_hash") == hv:
                            prov = it.get("provenance") or {}
                            ps = _prov_str(prov if isinstance(prov, dict) else {})
                            if ps:
                                line += f"  {ps}"
                            break
            print(line)

    _print_packet_footer()


# -----------------------------
# Argument parsing (lightweight)
# -----------------------------

def _pop_flag(args: List[str], flag: str) -> bool:
    if flag in args:
        args.remove(flag)
        return True
    return False


def _get_flag_value(args: List[str], flag: str) -> Optional[str]:
    if flag not in args:
        return None
    i = args.index(flag)
    if i + 1 >= len(args):
        return None
    val = args[i + 1]
    # remove flag + val
    del args[i : i + 2]
    return val


def _default_limit_from_scope(scope: ScopeBundle) -> int:
    disp = scope.effective.get("display") or {}
    return _maybe_int(disp.get("default_limit"), DEFAULT_DISPLAY_LIMIT)


def main(argv: Optional[List[str]] = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    # Treat no args or -h/--help as help
    if not argv or "-h" in argv or "--help" in argv:
        cmd_help(None)
        return

    repo_root = _repo_root_from_cwd()
    # Load scope (needed for most commands except pure help/tree)
    scope: Optional[ScopeBundle] = None

    cmd = argv[0].strip().lower()

    # Meta-family commands (no packet wrapping; these are their own UI)
    if cmd == "help":
        topic = argv[1] if len(argv) > 1 else None
        cmd_help(topic)
        return
    if cmd == "tree":
        cmd_tree()
        return

    # Commands that need scope
    scope = _load_scope(repo_root)

    if cmd == "about":
        # about is informational, no packet
        cmd_about(repo_root, scope)
        return

    if cmd == "scope":
        # scope commands are informational/editorial, no packet
        if len(argv) == 1:
            scope_print(repo_root, scope)
            return
        sub = argv[1].lower()
        if sub == "add" and len(argv) >= 4 and argv[2].lower() == "tiles-root":
            scope_add_tiles_root(repo_root, scope, argv[3])
            return
        if sub == "remove" and len(argv) >= 4 and argv[2].lower() == "tiles-root":
            scope_remove_tiles_root(repo_root, scope, argv[3])
            return
        if sub == "set" and len(argv) >= 4 and argv[2].lower() == "index":
            scope_set_index(repo_root, scope, argv[3])
            return
        if sub == "reset":
            scope_reset(repo_root, scope)
            return
        print("unknown scope command")
        print("Try: python -m repo_ui.query help scope")
        return

    # Query verbs
    args = argv[1:]  # remaining
    limit = _get_flag_value(args, "--limit")
    file_filter = _get_flag_value(args, "--file")
    sort_key = _get_flag_value(args, "--sort") or ""
    meta = _pop_flag(args, "--meta")
    show_locs = _pop_flag(args, "--show-locs")

    # Use scope default limit if none provided
    lim = _maybe_int(limit, _default_limit_from_scope(scope))

    if cmd == "list":
        if not args:
            print("missing <thing> for list")
            print("Try: python -m repo_ui.query help list")
            return
        thing = args[0].lower()
        rest = args[1:]

        # Parse thing-specific flags
        want_type = None
        contains = None
        kind = None
        samples = 0

        # Use local parsing on rest
        rest2 = list(rest)
        want_type = _get_flag_value(rest2, "--type")
        contains = _get_flag_value(rest2, "--contains")
        kind = _get_flag_value(rest2, "--kind")
        samples_s = _get_flag_value(rest2, "--samples")
        if samples_s is not None:
            samples = _maybe_int(samples_s, 0)

        if thing == "ids":
            list_ids(repo_root, scope, argv, want_type, contains, show_locs, meta, (sort_key or "id"), lim, file_filter)
            return
        if thing == "types":
            list_types(repo_root, scope, argv, (sort_key or "count"), lim, file_filter)
            return
        if thing == "hashes":
            list_hashes(repo_root, scope, argv, show_locs, meta, (sort_key or "count"), lim, file_filter)
            return
        if thing == "files":
            list_files(repo_root, scope, argv, (sort_key or "ids"), lim)
            return
        if thing in ("edge-cases", "edge_cases"):
            list_edge_cases(repo_root, scope, argv, kind, samples, show_locs, meta, lim, file_filter)
            return
        if thing in ("css-ids", "css_ids"):
            list_css_ids(repo_root, scope, argv, contains, show_locs, (sort_key or "id"), lim)
            return

        if thing in ("css-classes", "css_classes"):
            list_css_classes(repo_root, scope, argv, contains, show_locs, (sort_key or "class"), lim)
            return

        print(f"unknown list thing: {thing}")
        print("Try: python -m repo_ui.query tree")
        return

    if cmd == "count":
        if not args:
            print("missing <thing> for count")
            print("Try: python -m repo_ui.query help count")
            return
        thing = args[0].lower()
        rest = list(args[1:])
        want_type = _get_flag_value(rest, "--type")
        contains = _get_flag_value(rest, "--contains")
        kind = _get_flag_value(rest, "--kind")

        if thing == "ids":
            count_ids(repo_root, scope, argv, want_type, contains, file_filter)
            return
        if thing == "types":
            count_types(repo_root, scope, argv, file_filter)
            return
        if thing in ("edge-cases", "edge_cases"):
            count_edge_cases(repo_root, scope, argv, kind, file_filter)
            return

        print(f"unknown count thing: {thing}")
        print("Try: python -m repo_ui.query tree")
        return

    if cmd == "show":
        if len(args) < 2:
            print("usage: show file <repo_rel_path> | show css-id <id> | show css-class <class> [--limit N]")
            return
        obj = args[0].lower()

        if obj == "file":
            file_rel = args[1]
            meta_refs = _pop_flag(args, "--meta-refs")
            show_file(repo_root, scope, argv, file_rel, lim, show_locs, meta, meta_refs)
            return

        if obj in ("css-id", "css_id"):
            show_css_id(repo_root, scope, argv, args[1], lim)
            return

        if obj in ("css-class", "css_class"):
            show_css_class(repo_root, scope, argv, args[1], lim)
            return

        print("v1 supports: show file | show css-id | show css-class")
        return


    if cmd == "find":
        if len(args) < 2:
            print("usage: find hash <sha1:...> [--show-canonical] [--meta] [--limit N]")
            return
        what = args[0].lower()
        if what != "hash":
            print("v1 supports only: find hash <sha1:...>")
            return
        hv = args[1]
        show_canonical = _pop_flag(args, "--show-canonical")
        # find hash defaults to showing occurrences; allow --show-locs but it’s redundant
        find_hash(repo_root, scope, argv, hv, lim, True, meta, show_canonical)
        return

    print(f"unknown command: {cmd}")
    print("Try: python -m repo_ui.query help")
    print("Or:  python -m repo_ui.query tree")


if __name__ == "__main__":
    main()
