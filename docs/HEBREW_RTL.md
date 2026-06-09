# Hebrew / RTL Rendering — Investigation Log & Playbook

> **Purpose.** Hebrew (and other right‑to‑left scripts) has been "fixed" and
> re‑broken several times. This document is the single source of truth: what the
> problem actually is, every approach we tried, **exactly what each one did and
> what the on‑screen Hebrew looked like**, what was wrong, and what works. Read
> this BEFORE touching RTL again so we stop going in circles.

Last updated: 2026‑06‑08.

---

## 0. TL;DR / Current rules (read this first)

1. **Tkinter on this Windows machine renders RTL CORRECTLY in *display* widgets**
   — `tk.Label` / `CTkLabel`, `tk.Button` / `CTkButton`, and `Canvas` text
   (the slot buttons are `Canvas.create_text`). Those widgets do their **own**
   BiDi reordering when given a **logical**‑order string (the normal stored
   string). **Give them the raw logical string. Do NOT pre‑reorder.**
2. **If you pre‑reorder text (e.g. with `python-bidi`'s `get_display`) and hand
   that to a display widget, you DOUBLE‑transform it and the Hebrew comes out
   MIRRORED / scrambled.** This is the exact mistake that has been made more
   than once (including 2026‑06‑08, see Attempt 4).
3. **Editable widgets are the genuine problem.** `tk.Entry` / `CTkEntry` (and
   `tk.Text`) do **NOT** apply BiDi here. They lay characters out in logical
   order, left‑to‑right, so Hebrew in a text field *looks* reversed while you
   type. This is a real Tk limitation, not something a one‑liner fixes.
4. Therefore: **never apply a logical→visual transform globally.** Display is
   already fine. Only the editable fields need help, and that help must keep the
   *stored* value in logical order (so it still renders correctly everywhere it
   is shown as a label).

If you remember nothing else: **display widgets = leave alone; entries = the
hard part; pre‑reversing = the recurring bug.**

---

## 1. The symptom (what the user reports)

Typed/stored logical string (what the user intends, reading right‑to‑left):

```
לא, מה הקשר          ("No, what's the connection")
```

Bad rendering they have seen at various times:

```
הקשר מה ,לא          (word order flipped, comma on the wrong side)
רשקה המ ,אל          (fully character‑mirrored)
```

Places it has shown up: sound‑slot button labels, tab names, the People hub
(person names, group headers, sound chips), and **text entry fields** (the
"Edit person → Name" box is the clearest repro).

---

## 2. Background — why this is hard

- **Unicode BiDi algorithm**: logical order (the order characters are typed /
  stored) is *not* the order they should be painted for RTL scripts. A correct
  renderer reorders runs for display ("visual order").
- **Tcl/Tk's BiDi support is incomplete and inconsistent across widget types.**
  Empirically on this machine (Windows 11, Tk 8.6):
  - `Canvas.create_text`, `Label`, `Button` → **DO** reorder (look correct from
    logical input).
  - `Entry`, `Text` → **DO NOT** reorder (look reversed from logical input).
- `python-bidi` (`from bidi.algorithm import get_display`) *is* installed
  (declared in `requirements.txt`). It converts logical→visual. It is the right
  tool **only** for a target that does no BiDi of its own — and our display
  widgets already do BiDi, so using it on them is wrong.

### Probing methodology (how to actually verify, not guess)

`Text`/`Entry` expose per‑character geometry via `widget.bbox(index)`. Map the
window, insert the string, and compare the x of the first vs last logical
character:

- first‑char x **<** last‑char x → **logical LTR, NO BiDi** (reversed Hebrew).
- first‑char x **>** last‑char x → **visual, BiDi applied** (correct Hebrew).

⚠️ **Pitfall that burned us:** `tk.Text` is **not** representative of `Canvas`
or `Label`. A `Text` probe says "no BiDi" — but `Canvas`/`Label` behave the
opposite way. **Probe the actual widget class you care about.** For `Canvas`,
`bbox` is per‑item not per‑char, so the reliable oracle for display widgets is a
real screenshot (or trust that slot labels already looked correct historically).

---

## 3. Attempt log (chronological, with exact outcomes)

### Attempt 0 — (historical, before this session) pre‑reverse for the Canvas
- **What:** Earlier code manually reversed RTL strings before drawing slot
  labels on the `Canvas`, believing the Canvas had no BiDi.
- **Outcome:** Over‑corrected. The Canvas *does* BiDi, so reversing made slot
  labels wrong.
- **Resolution:** They turned the helper into a **no‑op** and left this comment
  in `gui._fix_rtl_text`: *"Hebrew slot labels are now displayed correctly
  without special reversal."* → i.e. **logical input renders correctly.** This
  is the single most important breadcrumb and it was later ignored.

### Attempt 1 — (historical) force entries to `justify="left"`
- **What:** `gui._bind_rtl_entry` sets the inner `tk.Entry` `justify="left"`
  on every keystroke, to "keep the paragraph in LTR mode … matching the button
  display and what the user typed."
- **Outcome:** Entry shows the logical string left‑aligned. For Hebrew that
  *looks reversed* (because Entry has no BiDi). It was considered "consistent"
  at the time but is not actually correct RTL.
- **Status:** This is the long‑standing behaviour. Restored as the baseline
  (see Attempt 5). The editable‑field problem is still open.

### Attempt 2 — (2026‑06‑08) pixel probe with `tk.Text`
- **What:** Built a `tk.Text`, inserted `לא, מה הקשר`, measured `bbox` per char.
- **Result:** first‑char x=2 (leftmost), last‑char x=147 (rightmost) →
  concluded **"Tk applies ZERO BiDi."**
- **Why it was misleading:** `tk.Text` genuinely has no BiDi — but that does
  **not** generalize to `Canvas`/`Label`, which is what the slot labels use.
  This false generalization is what led straight into Attempt 4's regression.

### Attempt 3 — (2026‑06‑08) new `soundboard/rtl.py` with `to_display()`
- **What:** Added a module wrapping `get_display` (per line, pure‑LTR fast‑path,
  no‑op fallback if `bidi` missing) plus `is_rtl_dominant`, `entry_justify`.
- **In isolation it is correct:** `to_display("לא, מה הקשר")` → `"רשקה המ ,אל"`
  (the visual form), English untouched, newlines preserved.
- **The module itself is fine.** The mistake was *where* we applied it (next).

### Attempt 4 — (2026‑06‑08) apply `to_display()` to ALL display widgets  ❌ REGRESSION
- **What:** Routed slot labels (`_fix_rtl_text` → `to_display`), tab labels
  (each `configure(text=)` in the ellipsis/marquee code), search footer,
  playlist checkboxes, group menu, and the whole People hub
  (`person_board._disp = to_display`: person titles, group headers, sound chips,
  move‑to‑group entries, drag ghost) through `get_display`.
- **Exact Hebrew outcome (BAD):** Display widgets BiDi the *already‑visual*
  string a second time → **mirrored/scrambled**. User report: *"existing sounds
  are letter‑mirrored."* Slot that should read `לא, מה הקשר` rendered as the
  reversed/mirrored form. The People hub names/groups would be mirrored the same
  way.
- **Why:** Violated rule #1/#2 above — display widgets want **logical** input;
  pre‑reordering double‑transforms.
- **Also during this attempt:** the **startup splash** `pump()` used
  `win.update()`, which pumped the global timer queue and fired a deferred
  `after(1, _build_all_tab_widgets)` early → `AttributeError: '_virtualize'`.
  (Unrelated to Hebrew, fixed separately by using `update_idletasks()`; noted
  here only because it happened in the same batch.)

### Attempt 5 — (2026‑06‑08) REVERT the display‑side transform  ✅ RESTORES DISPLAY
- **What:**
  - `gui._fix_rtl_text` → back to **no‑op** (`return text`). All slot/label/tab
    call sites still route through it, so they now pass logical text straight to
    the widgets (correct).
  - `person_board._disp` → redefined as an **identity** function, neutralising
    every `_disp(...)` wrap in the People hub at one stroke.
  - `gui._bind_rtl_entry` → reverted to the original `justify="left"` baseline.
- **Expected Hebrew outcome:** display widgets back to **correct** (logical in →
  Tk BiDi → correct visual). Entries back to the long‑standing
  left‑aligned‑logical baseline (still looks reversed while editing — open item).
- **Status:** This is the current state. `rtl.py` is kept (harmless, unused by
  display paths) for the entry work below.

---

## 4. What is actually still broken (the real, narrow problem)

Only **editable text fields** (`CTkEntry`/`tk.Entry`, and `tk.Text`):

- They render logical order LTR, so Hebrew you type *looks* reversed in the box.
- The clearest repro: **People hub → Edit person → Name** (`edit_entity_dialog`
  in `person_board.py`, around line 345). That entry has no special handling.
- **Important nuance:** the *stored* value is still correct logical text, so the
  name shown afterwards as a **label** (person row, group header, chip) renders
  correctly. Only the live edit view is wrong.

---

## 5. Candidate fixes for entries (design space + risks)

We must keep the **stored** value in **logical** order (so labels stay correct).
Options, roughly in increasing complexity:

1. **Do nothing / keep baseline.** Lowest risk. Edit field looks reversed while
   typing; everything displayed elsewhere is correct. (Current state.)
2. **`justify="right"` for RTL‑dominant content.** Right‑aligns the field so it
   *feels* more RTL, but does **not** reorder characters — Hebrew still reads
   reversed. Marginal. (We tried this in Attempt 4's entry change and reverted
   it; it is not a real fix.)
3. **Focus‑aware visual swap (most promising).** Keep the logical value in a
   side store. While the field is **unfocused**, *display* `get_display(logical)`
   (Entry has no BiDi, so showing the visual string makes it *look correct*).
   On **focus‑in**, swap the shown text back to logical for editing; on
   **focus‑out**, swap back to visual. Save the **logical** value.
   - Pro: existing Hebrew names read correctly when a dialog opens / at rest.
   - Con: must decouple the shown text from the bound `StringVar`/save value;
     careful with `textvariable`, cursor, and re‑entrancy. While *typing* it
     still looks logical (acceptable — that's the active edit state).
   - **Verify with the `bbox` probe on the real `CTkEntry._entry`** before and
     after, and test save round‑trips (saved value must equal logical input).
4. **True in‑place BiDi editing** (visual display + reverse‑map every keystroke
   to logical, cursor tracking). Correct in theory, **high risk** in Tk, easy to
   corrupt the saved value. **Not recommended** unless 3 proves insufficient.

**Recommendation:** implement #3, scoped to the name entries
(`edit_entity_dialog`, plus optionally the main search / tab‑name entries via
`_bind_rtl_entry`), behind a small reusable helper in `rtl.py`. Prove it with a
bbox probe + a save round‑trip test before wiring it broadly. Do **not** touch
any display widget.

---

## 6. Test checklist before declaring RTL "fixed" again

- [ ] Slot button label with Hebrew renders correct (NOT mirrored). *(regression
      canary for Attempt‑4‑style mistakes.)*
- [ ] Tab name with Hebrew renders correct.
- [ ] People hub: person name, group header, sound chip all render correct.
- [ ] Mixed Hebrew+English+digits label renders sanely.
- [ ] Edit‑person Name field: opening an existing Hebrew name reads correctly
      (if Attempt‑5+#3 done); saving it round‑trips to the **same logical**
      string (assert `saved == typed_logical`).
- [ ] English‑only text is byte‑for‑byte unchanged everywhere.
- [ ] No double‑transform anywhere (grep for `to_display` / `get_display` usage
      and confirm none feeds a Label/Button/Canvas).

---

## 7. Files & symbols involved

- `soundboard/rtl.py` — `to_display`, `is_rtl_dominant`, `entry_justify`,
  `has_rtl`, `RTL_PATTERN`. (Display paths must NOT use `to_display`.)
- `soundboard/gui.py` — `_fix_rtl_text` (**no‑op**, display hook),
  `_is_rtl_dominant`, `_bind_rtl_entry` (entry `justify="left"` baseline).
- `soundboard/person_board.py` — `_disp` (**identity** hook),
  `edit_entity_dialog` (the Name entry = main repro target for the entry fix).
- `soundboard/slot_widget.py` — slot labels via `Canvas.create_text` (display
  widget; logical input renders correct — do not pre‑reorder).

---

## 8. Status

- **Display widgets:** ✅ correct after Attempt 5 revert (logical input).
- **Editable entries:** ⏳ open. Baseline = left‑aligned logical (looks reversed
  while editing). Next step = focus‑aware visual swap (§5.3), display‑only,
  storage stays logical, proven by bbox probe + round‑trip test.
