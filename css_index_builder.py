# repo_ui/css_index_builder.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def build_css_index(
    *,
    repo_root: Path,
    mirror_cache: Dict[str, Dict[str, Any]],
    run_id: str,
    out_name: str = "css_index.json",
) -> Dict[str, Any]:
    """
    Build a deterministic CSS index from Layer4 mirror_cache.

    Sources (locked-in):
      1) All entries with kind=="tcss" (verbatim .tcss content)
      2) Inline DEFAULT_CSS blocks extracted from entries with kind=="py" (mirrored py snippets)

    Output is written next to mirror at:
      repo_root/shadow_ui/layer4/mirror/<out_name>

    The index contains:
      - rules[] (ground truth)
      - id_index: {id: [rule_ref...]}
      - class_index: {class: [rule_ref...]}
      - buckets for parse / extraction issues

    IMPORTANT:
      - "not used" is not computed here (query/report joins Layer3 ids later).
      - We only extract #id / .class tokens from selector regions that own a { ... } rule block.
    """
    mirror_root = repo_root / "shadow_ui" / "layer4" / "mirror"
    out_path = mirror_root / out_name

    docs: List[_CssDoc] = []
    buckets: Dict[str, List[Dict[str, Any]]] = {
        "css_parse_error": [],
        "py_default_css_unextractable": [],
        "py_default_css_present_but_unextracted": [],
    }


    # 1) Collect .tcss docs (verbatim)
    for entry in mirror_cache.values():
        if entry.get("kind") != "tcss":
            continue
        docs.append(
            _CssDoc(
                doc_id=f"tcss:{entry.get('source_rel','')}",
                source_kind="tcss_file",
                source_rel=str(entry.get("source_rel", "")),
                mirror_rel=str(entry.get("mirror_rel", "")),
                text=str(entry.get("content", "") or ""),
                base_line=1,
            )
        )

    # 2) Collect DEFAULT_CSS docs from mirrored .py content
    for entry in mirror_cache.values():
        if entry.get("kind") != "py":
            continue
        py_text = str(entry.get("content", "") or "")
        src_rel = str(entry.get("source_rel", ""))
        mirror_rel = str(entry.get("mirror_rel", ""))
        try:
            blocks = list(extract_default_css_blocks(py_text))
        except Exception as e:
            buckets["py_default_css_unextractable"].append(
                {"source_rel": src_rel, "mirror_rel": mirror_rel, "error": repr(e)}
            )
            continue

        # Flag DEFAULT_CSS presence we couldn't extract (non-literal/dynamic/etc.)
        if ("DEFAULT_CSS" in py_text) and (len(blocks) == 0):
            buckets["py_default_css_present_but_unextracted"].append(
                {"source_rel": src_rel, "mirror_rel": mirror_rel}
            )

        for b in blocks:
            docs.append(
                _CssDoc(
                    doc_id=f"py_default_css:{src_rel}:{b.name_hint}",
                    source_kind="py_default_css",
                    source_rel=src_rel,
                    mirror_rel=mirror_rel,
                    text=b.text,
                    # where the CSS string starts in the mirrored content
                    base_line=b.start_line,
                )
            )

    # Make ordering deterministic for stable rule_i across runs
    docs.sort(key=lambda d: (d.source_kind, d.source_rel, d.doc_id))

    # 3) Scan each doc into rules
    rules: List[Dict[str, Any]] = []
    for doc in docs:
        try:
            for rr in scan_rules(doc.text):
                selector_text_raw = rr.selector_text
                selector_text = _COMMENT_RE.sub(" ", selector_text_raw).strip()

                # Extract tokens from selector region ONLY (cleaned; declarations never scanned)
                ids = sorted(set(extract_id_tokens(selector_text)))
                classes = sorted(set(extract_class_tokens(selector_text)))

                loc = _loc_from_offsets(
                    text=doc.text,
                    start_offset=rr.block_start,
                    end_offset=rr.block_end,
                    base_line=doc.base_line,
                )

                rules.append(
                    {
                        "doc_id": doc.doc_id,
                        "source_kind": doc.source_kind,
                        "source_rel": doc.source_rel,
                        "mirror_rel": doc.mirror_rel,
                        "selector_text": selector_text,
                        "selector_text_raw": selector_text_raw,
                        "declarations_text": rr.declarations_text,
                        "loc": loc,
                        "ids": ids,
                        "classes": classes,
                    }
                )
        except Exception as e:
            buckets["css_parse_error"].append(
                {
                    "doc_id": doc.doc_id,
                    "source_kind": doc.source_kind,
                    "source_rel": doc.source_rel,
                    "mirror_rel": doc.mirror_rel,
                    "error": repr(e),
                }
            )

    # 4) Build indices
    id_index: Dict[str, List[Dict[str, Any]]] = {}
    class_index: Dict[str, List[Dict[str, Any]]] = {}

    for i, r in enumerate(rules):
        ref = {
            "rule_i": i,
            "doc_id": r["doc_id"],
            "source_kind": r["source_kind"],
            "source_rel": r["source_rel"],
            "mirror_rel": r["mirror_rel"],
            "loc": r["loc"],
            "selector_text": r["selector_text"],
        }
        for id_ in r.get("ids") or []:
            id_index.setdefault(id_, []).append(ref)
        for cls in r.get("classes") or []:
            class_index.setdefault(cls, []).append(ref)

    out: Dict[str, Any] = {
        "kind": "css_index",
        "run_id": run_id,
        "docs_count": len(docs),
        "docs": [
            {
                "doc_id": d.doc_id,
                "source_kind": d.source_kind,
                "source_rel": d.source_rel,
                "mirror_rel": d.mirror_rel,
            }
            for d in docs
        ],

        "rules_count": len(rules),
        "rules": rules,
        "id_index": id_index,
        "class_index": class_index,
        "buckets": buckets,
    }

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# ----------------------------
# Data structures
# ----------------------------

@dataclass(frozen=True)
class _CssDoc:
    doc_id: str
    source_kind: str  # "tcss_file" | "py_default_css"
    source_rel: str
    mirror_rel: str
    text: str
    base_line: int  # 1-based line number in the mirrored file where this doc's text starts


@dataclass(frozen=True)
class _DefaultCssBlock:
    text: str
    start_line: int
    end_line: int
    name_hint: str = "DEFAULT_CSS"


@dataclass(frozen=True)
class _RuleSpan:
    selector_text: str
    declarations_text: str
    sel_start: int
    sel_end: int
    block_start: int
    block_end: int


# ----------------------------
# DEFAULT_CSS extraction (literal only)
# ----------------------------

_DEFAULT_CSS_RE = re.compile(
    r'\bDEFAULT_CSS\b\s*=\s*(?P<quote>"""|\'\'\')(?P<body>.*?)(?P=quote)',
    re.DOTALL,
)

def extract_default_css_blocks(py_text: str) -> Iterable[_DefaultCssBlock]:
    """
    Extract literal triple-quoted DEFAULT_CSS blocks.
    Dynamic construction is intentionally not guessed.
    """
    for m in _DEFAULT_CSS_RE.finditer(py_text):
        body = m.group("body") or ""
        start_line = py_text.count("\n", 0, m.start("body")) + 1
        end_line = start_line + body.count("\n")
        yield _DefaultCssBlock(text=body, start_line=start_line, end_line=end_line)


# ----------------------------
# TCSS rule scanner (brace-depth, comment + string aware)
# ----------------------------

def scan_rules(text: str) -> Iterable[_RuleSpan]:
    n = len(text)
    i = 0
    depth = 0
    in_comment = False
    in_string: Optional[str] = None  # "'" or '"'
    last_rule_end = 0

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        # comment start
        if not in_string and not in_comment and ch == "/" and nxt == "*":
            in_comment = True
            i += 2
            continue

        # comment end
        if in_comment:
            if ch == "*" and nxt == "/":
                in_comment = False
                i += 2
            else:
                i += 1
            continue

        # string start/end
        if not in_string and ch in ("'", '"'):
            in_string = ch
            i += 1
            continue
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
                i += 1
                continue
            i += 1
            continue

        # braces (only when not in comment/string)
        if ch == "{":
            if depth == 0:
                sel_start = last_rule_end
                sel_end = i
                block_start = i
                block_end = _find_matching_brace(text, i)

                decl_start = i + 1
                decl_end = block_end

                yield _RuleSpan(
                    selector_text=text[sel_start:sel_end],
                    declarations_text=text[decl_start:decl_end],
                    sel_start=sel_start,
                    sel_end=sel_end,
                    block_start=block_start,
                    block_end=block_end + 1,  # end exclusive
                )

                last_rule_end = block_end + 1
                i = block_end + 1
                continue
            else:
                depth += 1
                i += 1
                continue

        if ch == "}":
            if depth == 0:
                raise ValueError("unmatched '}' at top level")
            depth -= 1
            i += 1
            continue

        i += 1

    if in_comment:
        raise ValueError("unterminated /* comment */")
    if in_string:
        raise ValueError("unterminated string literal")
    if depth != 0:
        raise ValueError("unbalanced braces")


def _find_matching_brace(text: str, open_i: int) -> int:
    n = len(text)
    i = open_i + 1
    depth = 1
    in_comment = False
    in_string: Optional[str] = None

    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if not in_string and not in_comment and ch == "/" and nxt == "*":
            in_comment = True
            i += 2
            continue
        if in_comment:
            if ch == "*" and nxt == "/":
                in_comment = False
                i += 2
            else:
                i += 1
            continue

        if not in_string and ch in ("'", '"'):
            in_string = ch
            i += 1
            continue
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == in_string:
                in_string = None
                i += 1
                continue
            i += 1
            continue

        if ch == "{":
            depth += 1
            i += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return i
            i += 1
            continue

        i += 1

    raise ValueError("unterminated '{' block")


# ----------------------------
# Token extraction (selector-only)
# ----------------------------

# Strip block comments from selector text (keeps query output clean and avoids token weirdness)
_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

_IDENT_RE = re.compile(r"[A-Za-z0-9_-]+")

def extract_id_tokens(selector_text: str) -> List[str]:
    out: List[str] = []
    i = 0
    n = len(selector_text)
    while i < n:
        if selector_text[i] != "#":
            i += 1
            continue
        m = _IDENT_RE.match(selector_text, i + 1)
        if m:
            out.append(m.group(0))
            i = m.end()
        else:
            i += 1
    return out

def extract_class_tokens(selector_text: str) -> List[str]:
    out: List[str] = []
    i = 0
    n = len(selector_text)
    while i < n:
        if selector_text[i] != ".":
            i += 1
            continue
        m = _IDENT_RE.match(selector_text, i + 1)
        if m:
            out.append(m.group(0))
            i = m.end()
        else:
            i += 1
    return out


# ----------------------------
# Loc helpers
# ----------------------------

def _loc_from_offsets(*, text: str, start_offset: int, end_offset: int, base_line: int) -> Dict[str, int]:
    if start_offset < 0:
        start_offset = 0
    if end_offset < start_offset:
        end_offset = start_offset

    start_line_rel = text.count("\n", 0, start_offset) + 1
    end_line_rel = text.count("\n", 0, max(end_offset - 1, 0)) + 1 if text else 1

    return {
        "start_line": base_line + (start_line_rel - 1),
        "end_line": base_line + (end_line_rel - 1),
    }
