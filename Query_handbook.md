
---

# REPO_UI HANDBOOK

**Layer 3 (UI Semantics) · Layer 4 (Wiring / Impact) · Query Interface**

---

## 0. PURPOSE OF THIS HANDBOOK

This handbook defines:

* how to **navigate** UI-related repositories using `repo_ui`
* how to **query** Layer 3 and Layer 4 safely
* what **guarantees** each layer provides
* what **is intentionally not modeled**
* how an agent should reason without guessing

This is written so that **ChatGPT can operate the system correctly**.

---

## 1. CORE PRINCIPLES (NON-NEGOTIABLE)

### 1.1 No Assumptions

If something is not present in a layer’s output, it is **unknown**.

### 1.2 Evidence First

All answers must be backed by:

* a query result
* a layer artifact
* or a direct code reference

### 1.3 Progressive Disclosure

Navigation always follows:

```
Query → Inspect → Narrow → Inspect → (Code if needed)
```

Never start from code unless forced.

---

## 2. LAYER OVERVIEW (MENTAL MODEL)

### Layer Roles

| Layer   | Purpose                   | Answers                                              |
| ------- | ------------------------- | ---------------------------------------------------- |
| Layer 3 | UI *structure & identity* | “What UI exists? How is it shaped? What IDs exist?”  |
| Layer 4 | Wiring & dependencies     | “What depends on what? What breaks if this changes?” |
| Query   | Read-only navigation      | “Show me facts from layers”                          |

Layers are **orthogonal lenses**, not a workflow.

---

## 3. LAYER 3 — UI SEMANTICS (THE HEART)

### 3.1 What Layer 3 Is

Layer 3 is a **static UI semantic extraction layer**.

It describes:

* UI constructor usage
* layout structure
* widget identity (IDs)
* parent/child relationships
* layout hashes
* known unresolved cases

It does **not**:

* execute code
* simulate runtime
* infer semantics
* guess behavior

---

### 3.2 What Layer 3 Guarantees

Layer 3 guarantees:

1. **Every UI constructor call is captured**
2. **Every `id=` usage is classified**
3. **Every unresolved case is recorded**
4. **Every recorded item is traceable to source**
5. **No false certainty**

This is critical:
Layer 3 is **honest, not clever**.

---

## 4. LAYER 3 DATA MODEL (CONCEPTUAL)

Each Python mirror produces a **Layer 3 Tile**.

### 4.1 Nodes (UI Constructors)

Each node represents a UI constructor call:

```
Static(...)
Container(...)
DataTable(...)
```

Each node has:

* `node_id` (stable internal ID)
* `type` (constructor name)
* `id` (classified identity)
* provenance (where it came from)

#### ID Classification (CRITICAL)

Every `id=` falls into exactly one category:

| kind                     | meaning                                 |
| ------------------------ | --------------------------------------- |
| `literal`                | Fully resolved static ID                |
| `pattern`                | Deterministic template (e.g. f-strings) |
| `dynamic` / `nonliteral` | Runtime-only                            |
| `none`                   | No id kwarg                             |

Nothing is dropped. Nothing is guessed.

---

### 4.2 Edges (Structure)

Edges represent **structural containment**, not semantics.

Sources of edges:

* positional constructor nesting
* `with` blocks
* `.mount(...)` calls

Edges preserve:

* **child order**
* provenance
* edge type (implicit via feature)

---

### 4.3 Roots & Trees

Roots are created from:

```
yield Widget(...)
```

Each root defines:

* a UI tree
* a canonical layout hash
* a stable structure signature

Trees allow:

* diffing UI layouts
* detecting structural changes
* grouping equivalent layouts

---

### 4.4 Hashes

Each UI tree produces a **canonical hash** based on:

* node types
* child order
* ID presence

This answers:

> “Did the layout shape change?”

Hashes are **structural**, not visual.

---

## 5. EDGE CASE SYSTEM (CRITICAL DESIGN)

Layer 3 never hides uncertainty.

### 5.1 Edge-Case Buckets

Examples:

| bucket                 | meaning                                     |
| ---------------------- | ------------------------------------------- |
| `id_nonliteral`        | ID exists but cannot be resolved statically |
| `id_pattern`           | ID is a template (f-string)                 |
| `child_star_args`      | Children come from `*args`                  |
| `with_block_unmodeled` | (historical; now resolved)                  |

Edge-cases are **features**, not bugs.

They tell the agent:

> “This exists, but requires human/runtime inspection.”

---

### 5.2 Provenance Guarantee

Every edge-case includes:

* file path
* anchor (snippet)
* line focus (when available)

Thus:

* nothing is untraceable
* fallback to editor/shell is always possible

---

## 6. QUERY INTERFACE — HOW TO NAVIGATE

### 6.1 Query Philosophy

The query tool is:

* read-only
* deterministic
* state-aware

It **never computes new facts**.

---

### 6.2 Global Commands

#### Show available edge-cases

```bash
python -m repo_ui.query list edge-cases
```

#### Show samples with provenance

```bash
python -m repo_ui.query list edge-cases --samples 50 --meta
```

#### Narrow to one bucket

```bash
python -m repo_ui.query list edge-cases --kind id_nonliteral --samples 50 --meta
```

This is the **primary debugging entrypoint**.

---

### 6.3 File-Level Inspection

```bash
python -m repo_ui.query show file tvm_ui/app.py
```

Shows:

* nodes
* ids
* counts
* roots
* hashes
* edge-cases for that file

This answers:

> “What UI lives here?”

---

### 6.4 ID-Centric Queries

#### List all IDs

```bash
python -m repo_ui.query list ids
```

#### Filter by file

```bash
python -m repo_ui.query list ids --file tvm_ui/app.py
```

#### Filter by ID type

```bash
python -m repo_ui.query list ids --kind literal
python -m repo_ui.query list ids --kind pattern
```

This answers:

> “What identities exist and how stable are they?”

---

### 6.5 Understanding Provenance Output

Example:

```
- id_nonliteral tvm_ui/screens/chains.py anchor=s006 focus=L86-L86
```

Interpretation:

* unresolved ID
* in file `chains.py`
* snippet anchor `s006`
* exact line 86

**Next step**:
Open that line in editor or shell.

---

## 7. LAYER 4 — WIRING & IMPACT (OVERVIEW)

Layer 4 operates **above Layer 3**.

### 7.1 Purpose

Layer 4 answers:

* “What depends on this UI?”
* “What code path leads here?”
* “What breaks if this changes?”

It focuses on:

* import graphs
* usage relationships
* fan-in / fan-out
* cross-file dependencies

---

### 7.2 Relationship to Layer 3

Layer 3 gives **facts**
Layer 4 gives **relationships**

Layer 4 never reinterprets Layer 3 — it only links.

---

### 7.3 Typical Layer 4 Questions

* Which files construct this widget?
* Where is this UI tree referenced?
* What services feed this UI?
* What modules import this screen?

---

## 8. HOW CHATGPT SHOULD USE THIS SYSTEM

### 8.1 Correct Reasoning Pattern

1. Ask: “What am I trying to locate?”
2. Choose the layer:

   * structure → Layer 3
   * dependencies → Layer 4
3. Run the smallest possible query
4. Inspect results
5. Narrow further
6. Only then open code

---

### 8.2 What NOT to Do

* Do not infer runtime behavior
* Do not assume Textual semantics
* Do not collapse unresolved cases
* Do not “fix” edge-cases silently

---

## 9. DESIGN INTENT (IMPORTANT FOR FUTURE)

This system is designed so that:

* Textual is **just one dialect**
* The same machinery can inspect:

  * other UI frameworks
  * declarative layouts
  * DSLs

Textual works here because:

* constructors
* IDs
* containment
* yields

Not because the system “knows Textual”.

---

## 10. FINAL SUMMARY (FOR AGENT MEMORY)

* Layer 3 is **complete, honest, static UI truth**
* Edge-cases are intentional and valuable
* Query is for navigation, not interpretation
* IDs are sacred: capture all, classify honestly
* Never trade correctness for convenience

---

---

## 11. LAYER 4 — CSS INDEX (STYLING NAVIGATION)

This system includes a **Layer 4 CSS Index** to enable deterministic navigation from **IDs / classes** to the **CSS rules** that style them.

### 11.1 Purpose

The CSS Index answers:

* “Where is `#some_id` styled?”
* “Where is `.some_class` styled?”
* “Show me every CSS rule that references this selector token.”

It does **not**:

* infer which rule “matters most”
* interpret visual meaning (color vs layout vs spacing)
* determine whether a class is actually used at runtime
* simulate Textual styling behavior

It is **navigation-only** and **evidence-first**.

---

### 11.2 Inputs (2 Sources Only)

The CSS Index is built from **Layer 4 mirror content** only:

1. **External `.tcss` files** (mirrored verbatim)
2. **Inline `DEFAULT_CSS`** blocks inside mirrored `.py` files

If CSS is not present in mirrors, it is **unknown**.

---

### 11.3 What Counts as “CSS”

A selector token counts only when it appears in a **selector region that owns a `{ ... }` rule block**.

This prevents false positives from non-CSS contexts such as:

* Python queries like `query_one("#window_host")`
* hex colors like `background: #0b0b0b;`

Tokens are extracted **from selector text only**, not from declarations.

---

### 11.4 Selector Tokens Captured

The CSS Index extracts:

* **IDs**: `#<id>` tokens
* **Classes**: `.<class>` tokens

Selectors may be compound or stacked, e.g.:

* `#app_root.modal_open #shell_modal`
* `#id1, #id2 { ... }`

The system records **all matches**. Multiple matches are normal and not an error.

---

### 11.5 Provenance Guarantee

Every CSS match is traceable to:

* `source_rel` (repo-relative source file)
* a line-range location
* the selector text

Thus an agent can immediately open the file and navigate to the exact rule region.

---

### 11.6 Buckets (CSS Uncertainty)

The CSS Index never hides uncertainty. It records issues in buckets such as:

* `css_parse_error` — malformed CSS blocks (e.g., unbalanced braces)
* `py_default_css_unextractable` — error extracting inline CSS
* `py_default_css_present_but_unextracted` — `DEFAULT_CSS` is present in mirrored `.py` but not statically extractable (non-literal/dynamic).
  This is **not an error**; it means **unknown** and may require runtime/manual inspection.

---

## 12. QUERY INTERFACE — CSS NAVIGATION

### 12.1 List CSS IDs

List IDs referenced in CSS selectors:

```bash
python -m repo_ui.query list css-ids
python -m repo_ui.query list css-ids --contains app
python -m repo_ui.query list css-ids --sort count --limit 50
```

### 12.2 Show CSS Rules for an ID

Show all CSS rule locations referencing a specific id:

```bash
python -m repo_ui.query show css-id app_root
```

This answers:

> “Where should I look to change styling for `#app_root`?”

---

### 12.3 List CSS Classes

List class selectors referenced in CSS:

```bash
python -m repo_ui.query list css-classes
python -m repo_ui.query list css-classes --contains toast
python -m repo_ui.query list css-classes --show-locs
```

### 12.4 Show CSS Rules for a Class

Show all CSS rule locations referencing a class:

```bash
python -m repo_ui.query show css-class toast
```

This answers:

> “Where is `.toast` styled?”

---

## 13. AGENT REASONING PATTERN FOR STYLING

When asked: “I want to change the styling of this UI element”:

1. Locate candidate **IDs** and/or **classes** (prefer IDs if available)

2. Use query to retrieve styling locations:

   * `show css-id <id>`
   * `show css-class <class>`

3. Open the referenced file(s) and edit the relevant rule block(s)

Do not infer which rule is “the correct one.”
Return the **set of evidence-backed matches**, and let the user decide which rule to change.

---

## 14. FINAL SUMMARY (CSS EXTENSION)

* CSS index is a Layer 4 navigation artifact built from mirrored `.tcss` + inline `DEFAULT_CSS`
* “In CSS” means “appears in selector text owning a `{...}` rule”
* IDs and classes are indexed separately
* Multiple matches are normal
* Unextractable `DEFAULT_CSS` is recorded as **unknown**, not an error
