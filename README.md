# repo_ui — Static UI Inspection & Navigation Tool (Textual)

`repo_ui` is a **static inspection and navigation tool** for **Textual / Textualize-style Python UI repositories**.

It inspects Textual UIs **without executing code**, extracting **UI structure**, **identity (`id=`)**, and **styling references** (`.tcss`, `DEFAULT_CSS`) and exposes them through a **read-only query interface** designed for deterministic, evidence-based navigation.

This tool is built so both **humans and LLM agents** can answer questions like:

* *What Textual UI exists in this repo?*
* *What widget IDs and layouts are defined?*
* *Where is this UI element styled (CSS or inline)?*
* *What might be impacted if I change this screen?*
* *Where should I edit styling for this widget?*

## Core Design Principles

* **No assumptions** — if something cannot be proven statically, it is marked as *unknown*
* **Evidence first** — every answer links back to source files and line ranges
* **Read-only** — no execution, no mutation, no inference
* **Progressive disclosure** — query → inspect → narrow → inspect → (code if needed)

## What `repo_ui` Does

### 1. Repository Scanning

* Reads `folders.json` at repo root
* Recursively scans only the declared folders
* Collects `.py` and `.tcss` files
* Ignores runtime, virtualenvs, and non-UI files

### 2. Layer 3 — UI Semantics (Structure & Identity)

Builds a **static UI model** by inspecting Python code:

* Captures every UI constructor call
* Extracts and classifies `id=` usage:

  * literal
  * pattern
  * dynamic / nonliteral
  * none
* Builds parent/child containment trees
* Computes canonical layout hashes
* Records unresolved edge-cases instead of guessing

Output is written to:

```
shadow_ui/layer3/
```

---

### 3. Layer 4 — CSS Index (Styling Navigation)

Builds a **CSS selector index** from mirrored sources:

**Sources**

* External `.tcss` files
* Inline `DEFAULT_CSS` blocks inside `.py`

**Captured**

* `#id` selectors
* `.class` selectors
* Selector → file → line range provenance

**Not Captured**

* Runtime style mutations
* Visual semantics
* Usage inference (only where CSS is written)

Unextractable or dynamic CSS is explicitly bucketed as *unknown*.

Output is written to:

```
shadow_ui/layer4/mirror/css_index.json
```

## Query Interface

All inspection happens via:

```bash
python -m repo_ui.query ...
```

Examples:

```bash
# List UI IDs
python -m repo_ui.query list ids

# Show CSS rules for an ID
python -m repo_ui.query show css-id app_root

# Show CSS rules for a class
python -m repo_ui.query show css-class toast

# Inspect a UI file’s structure
python -m repo_ui.query show file tvm_ui/screens/chains.py

# See everything
python -m repo_ui.query tree
```

The query tool is:

* deterministic
* read-only
* provenance-preserving


## What This Enables

* Fast navigation from **UI element → styling location**
* Safe refactors (know what exists before touching code)
* UI diffing via layout hashes
* LLM-assisted UI debugging without hallucination
* Future extensions (class usage, runtime hooks, visual overlays)


## What It Does *Not* Do

* Execute UI code
* Simulate runtime behavior
* Infer styling intent
* Guess about unresolved constructs
* Replace manual review when ambiguity exists

Unknowns are **first-class outputs**, not errors.

## Philosophy

> *Correctness beats convenience.*

`repo_ui` prefers to say *“unknown”* rather than be clever.
This makes it reliable for both humans and autonomous agents.

## LLM Prompt

* Copy paste ready Query_handbook.md for chatgpt
