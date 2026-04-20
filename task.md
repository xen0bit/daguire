# DAGUIRE Development Plan: Structure Editor & Parser

## Core Vision

Transform daguire from a pure frequency-analysis viewer into an interactive binary format reverse engineering tool where:

1. You visually explore the DAG to discover structure
2. You interactively author a structure definition (like Kaitai `.ksy`, but simpler and tuned for the DAG paradigm)
3. You apply that structure as a filter/parser overlay on the DAG view
4. NULL bytes serve as abstract padding/alignment wildcards that re-align structurally equivalent bytes across records
5. All pure Python stdlib, all inside the existing tkinter GUI
6. SVG export remains fully functional from CLI for headless verification

---

## The NULL-Aligned DAG (Key Innovation)

This is the distinguishing feature vs. Kaitai/ImHex.

**Problem**: Real-world captures contain records with different offsets for the same logical structure. E.g., a protocol where some packets have a 2-byte VLAN tag and others don't — the same TCP header fields appear at byte 0 in one record and byte 2 in another. In the raw DAG, these show up as separate, unaligned columns.

**Solution**: When a structure declares a `pad` field, the engine creates a virtual aligned view by:

1. For each record, evaluating the structure's fields left-to-right
2. When a `pad` field is encountered, determining how many NULL bytes to virtually insert before the next field to align it to the structure's declared offset
3. These virtual NULLs are stored in a separate aligned SQLite table (`records_aligned`) — the original `records` table is never modified
4. The DAG is then drawn from the aligned table, so structurally equivalent bytes across records share the same column regardless of their raw offset

**Concrete example**:
```
Structure: [magic:u32 @0] [pad:align @4] [field1:u8 @?]

Record A raw: DEADBEEF 01 02 03
Record B raw: AA BB DEADBEEF 01 02 03  (2-byte preamble)

After alignment:
            off_0  off_1  off_2  off_3  off_4  off_5  off_6
Record A:   0xDE   0xAD   0xBE   0xEF   NULL   0x01   0x02
Record B:   0xDE   0xAD   0xBE   0xEF   NULL   0x01   0x02
             ↑ same column now ↑
```

The `pad:align` field says "the next field starts at the structure's next declared offset; insert NULLs to fill the gap." The alignment engine looks at each record's raw bytes, finds the magic bytes, and shifts subsequent data to align.

This means:
- `pad` doesn't consume actual data bytes — it's a virtual column inserter
- The aligned table may be wider than the raw table (additional columns due to alignment padding)
- The DAG view shows NULL columns for padding with a distinct visual (dotted border, "PAD" label, collapsed width)
- Filtering by structure automatically switches to the aligned view

---

## Data Model: `.dgs` Structure Format

Stored as JSON in `~/.daguire/structures/`. Example:

```json
{
  "name": "tcp_header",
  "version": 1,
  "description": "TCP segment header (20 bytes min)",
  "endian": "big",
  "fields": [
    {
      "name": "src_port",
      "type": "u16be",
      "offset": 0,
      "description": "Source port"
    },
    {
      "name": "dst_port",
      "type": "u16be",
      "offset": 2,
      "description": "Destination port"
    },
    {
      "name": "seq_num",
      "type": "u32be",
      "offset": 4
    },
    {
      "name": "ack_num",
      "type": "u32be",
      "offset": 8
    },
    {
      "name": "data_offset",
      "type": "u4",
      "offset": 12,
      "bit_offset": 4
    },
    {
      "name": "flags",
      "type": "u8",
      "offset": 13,
      "enum": {
        "2": "SYN",
        "16": "ACK",
        "18": "SYN_ACK",
        "1": "FIN",
        "4": "RST"
      }
    },
    {
      "name": "window",
      "type": "u16be",
      "offset": 14
    },
    {
      "name": "checksum",
      "type": "u16be",
      "offset": 16
    },
    {
      "name": "urgent_ptr",
      "type": "u16be",
      "offset": 18
    },
    {
      "name": "options_pad",
      "type": "pad",
      "offset": 20,
      "align_to": 4,
      "description": "Options + padding to align to 4-byte boundary"
    }
  ]
}
```

### Field Types

| Type | Size | Description |
|---|---|---|
| `u8` | 1 byte | Unsigned 8-bit integer |
| `u16be` / `u16le` | 2 bytes | Unsigned 16-bit, big/little endian |
| `u32be` / `u32le` | 4 bytes | Unsigned 32-bit |
| `u64be` / `u64le` | 8 bytes | Unsigned 64-bit |
| `s8`, `s16be`, etc. | 1-8 bytes | Signed variants |
| `f32be` / `f32le` | 4 bytes | IEEE 754 float |
| `f64be` / `f64le` | 8 bytes | IEEE 754 double |
| `ascii<N>` | N bytes | ASCII string of length N |
| `magic` | N bytes | Fixed byte sequence (validation) |
| `enum` | 1-4 bytes | Maps numeric values to labels |
| `pad` | variable | NULL-alignment wildcard — inserts virtual NULLs to align next field |
| `raw<N>` | N bytes | Raw bytes, no decoding |
| `bitfield<N>` | N bits | Sub-byte field within a byte |
| `leb128` | variable | LEB128 variable-length integer |

### `pad` Field Properties

| Property | Description |
|---|---|
| `offset` | Starting byte offset in the structure where padding begins |
| `align_to` | Byte boundary to align the next field to (e.g., 4 for 4-byte alignment) |
| `max_size` | Maximum number of bytes to skip (safety limit, default 256) |
| `match_byte` | Optional: the byte value consumed as padding (default: `null` = any non-field bytes) |

The alignment algorithm for `pad`:
1. From the `pad` field's `offset`, scan forward in the raw record
2. The next non-pad field's `offset` tells us where we need to be
3. Insert virtual NULL columns for the gap
4. If `align_to` is specified, the next field starts at the next `offset` that satisfies `offset % align_to == 0`

---

## Architecture: Layout / Render Separation

Current flow:
```
stdin -> Dag -> CanvasApp.draw_dag() -> tkinter canvas items
                           \-> CanvasApp._write_svg() -> duplicate layout logic -> SVG
```

New flow:
```
stdin -> Dag -> LayoutEngine.compute() -> LayoutResult (pure data)
                                           |                   |
                                     CanvasRenderer         SVGRenderer
                                     (tkinter canvas)      (SVG string/file)
```

### New Components

```
daguire.py (single file, all classes inline as currently)
|
+-- class Node                      (existing, enhanced)
+-- class Dag                       (existing, enhanced)
|   +-- build_aligned_table()       (NEW - applies structure -> creates records_aligned)
|   +-- get_struct_field_at()        (NEW - which structure field covers offset o?)
|   +-- get_aligned_val_counts()    (NEW - queries from aligned table)
|
+-- class Structure                 (NEW - data model for .dgs)
+-- class Field                     (NEW - single field in a structure)
+-- class StructureParser           (NEW - evaluates structure against records)
|   +-- match_record()              (does this record match the structure?)
|   +-- align_record()              (returns aligned byte list with virtual NULLs)
|   +-- decode_field()              (raw bytes -> decoded value for a field)
|   +-- get_match_stats()           (aggregate match/alignment statistics)
|
+-- class StructureStore            (NEW - CRUD for .dgs files on disk)
|   +-- list_structures()            (scan ~/.daguire/structures/)
|   +-- load()                       (JSON -> Structure object)
|   +-- save()                       (Structure -> JSON file)
|   +-- delete()
|   +-- export_ksy() / import_ksy()  (future: Kaitai interop)
|
+-- class PlacedNode                (NEW - node with layout coordinates)
+-- class PlacedEdge                (NEW - edge with layout coordinates and style)
+-- class FieldHeader               (NEW - structure field header for rendering)
+-- class PadColumn                 (NEW - collapsed padding column info)
+-- class LayoutResult              (NEW - full layout output)
+-- class LayoutEngine              (NEW - pure computation, no tkinter)
|   +-- compute(dag, structure, options, filter_seeds) -> LayoutResult
|
+-- class SVGRenderer               (NEW - LayoutResult -> SVG string/file)
|   +-- render(layout_result) -> str
|
+-- class CanvasRenderer            (NEW - LayoutResult -> tkinter canvas)
|   +-- render(canvas, layout_result)
|
+-- class StructureEditorDialog(tk.Toplevel)  (NEW - table-based editor)
|   +-- _build_field_table()        (treeview with field list)
|   +-- _add_field() / _remove_field()
|   +-- _edit_field_properties()    (name, type, offset, enum editor)
|   +-- _validate_structure()       (overlaps, out-of-range, etc.)
|   +-- _on_apply()                  (apply structure to DAG, rebuild aligned table)
|
+-- class CanvasApp(tk.Tk)          (existing, significantly enhanced)
|   +-- _build_toolbar()             (existing, extended: structure dropdown)
|   +-- _build_context_menu()        (NEW - right-click menus)
|   +-- draw_dag()                   (existing, enhanced: uses LayoutEngine)
|   +-- draw_field_headers()          (NEW - field name labels above columns)
|   +-- draw_multi_byte_brackets()   (NEW - spanning brackets for u16/u32 etc.)
|   +-- draw_collapsed_pad()         (NEW - narrow pad columns)
|   +-- draw_decoded_labels()        (NEW - decoded values on nodes when structure active)
|   +-- on_right_click()            (NEW)
|   +-- _apply_structure_overlay()   (NEW - switches DAG to aligned view)
|
+-- main()                          (existing, enhanced: --svg, --structure, --list-structures)
```

---

## CLI Interface

```bash
# Current (GUI only):
cat data.txt | uv run daguire hex 8

# New (headless SVG export):
cat data.txt | uv run daguire hex 8 --svg output.svg

# New (headless SVG with structure):
cat data.txt | uv run daguire hex 8 --svg output.svg --structure tcp_header

# New (headless SVG with display options):
cat data.txt | uv run daguire hex 8 --svg output.svg --no-decimal --hex --binary

# New (list saved structures):
uv run daguire --list-structures

# New (GUI with structure pre-loaded):
cat data.txt | uv run daguire hex 8 --structure tcp_header
```

Argparse specification:

```
positional:
  fmt          input format [hex, file]
  sz           size of DAG [8]

optional:
  -h, --help
  --svg PATH        export SVG to file and exit (no GUI)
  --structure NAME  apply saved structure by name
  --list-structures list saved structures and exit
  --decimal / --no-decimal    show decimal in labels (default: on)
  --hex / --no-hex            show hex in labels (default: on)
  --binary / --no-binary      show binary in labels (default: on)
  --ascii / --no-ascii        show ASCII in labels (default: on)
```

When `--svg` is provided:
1. Reads stdin data into `Dag`
2. If `--structure`, loads it, runs `StructureParser`, builds aligned table
3. Runs `LayoutEngine.compute()` with the given options
4. Runs `SVGRenderer.render()` and writes file
5. Exits (no GUI, no `mainloop()`)

---

## SVG Verifiability

SVG is the primary test artifact. Every visual feature must produce identifiable SVG elements.

SVG output conventions:
- Field headers: `<text class="field-header" data-field-name="...">`
- Multi-byte brackets: `<line class="field-bracket">` + `<text>`
- Pad columns: `<rect class="pad-column">` + `<text>` label
- Edge styling: `class="edge-normal"`, `class="edge-dimmed"`, `class="edge-pad"`
- Decoded labels: `<text class="decoded-label">`
- Structure metadata as XML comments:
  ```xml
  <!-- Structure: tcp_header -->
  <!-- Records: 150 total, 142 matched (94.7%), 8 unmatched -->
  <!-- Field src_port: 142/142 values decoded -->
  <!-- Field src_port: 142/142 values decoded -->
  <!-- Field pad: avg 2.3 NULLs inserted per record -->
  ```

Verification commands:
```bash
cat tests/hex_samples.txt | uv run daguire hex 8 --svg /tmp/test.svg --structure simple_header
grep 'class="field-header"' /tmp/test.svg
grep '<!-- Structure:' /tmp/test.svg
```

---

## Structure Editor Dialog

```
+------------------------------------------------------------+
| Structure: [dropdown] [New] [Delete]                       |
+------------------+-----------------------------------------+
|  Saved           |  Field Editor                            |
|  Structures      |  +------+-------+--------+------+------+
|  +-------------+ |  | Name | Type  | Offset | Size | Desc  |
|  | tcp_header  | |  +------+-------+--------+------+------+
|  | gif_header  | |  |src_p |u16be  |   0    |  2  | Src  |
|  | ...         | |  |dst_p |u16be  |   2    |  2  | Dst  |
|  |             | |  |pad   |pad    |   4    |  -  | Align|
|  +-------------+ |  |seq   |u32be  |   6    |  4  | Seq  |
|                  |  +------+-------+--------+------+------+
|  [Import][Export]|  [Add Field] [Remove] [Move Up/Down]    |
|                  |  --------------------------------------- |
|                  |  Enum Editor (when enum type selected)  |
|                  |  +------+----------+                    |
|                  |  | Value| Label    |                    |
|                  |  |  2   | SYN      |                    |
|                  |  |  16  | ACK      |                    |
|                  |  +------+----------+                    |
+------------------+-----------------------------------------+
| [Save] [Save As...] [Apply to DAG] [Cancel]               |
+------------------------------------------------------------+
```

- `ttk.Treeview` for both structure list and field table
- `ttk.Combobox` dropdowns for field types
- Inline editing via double-click on cells
- Enum sub-editor appears when a field's type involves enum mapping
- Real-time validation: overlapping fields in red, out-of-range in orange
- "Apply to DAG" triggers `Dag.build_aligned_table()` and switches canvas to aligned view

---

## Right-Click Context Menus

Bind `ButtonPress-3` on canvas nodes:

- "Create new structure starting at offset {o}" -> opens editor with first field pre-filled
- "Add to structure -> [list]" -> adds a field at this offset
- "Mark offset {o} as padding" -> adds a `pad` field
- "Set as magic value 0x{VV}" -> adds a `magic` field
- "Create enum from values at offset {o}" -> queries distinct values, populates enum editor
- "Decode as -> u8 / u16be / u32be / ascii / ..." -> quick-decode tooltip

---

## Structure-Aware DAG Drawing

When a structure is applied, the DAG view transforms:

| Before | After |
|---|---|
| N individual offset columns | Grouped columns by field, with field headers |
| NULL columns visible but ghostly | NULL/pad columns collapsed to single narrow slot |
| All edges equal | Inter-field edges prominent, intra-field edges dimmed |
| Node labels = raw byte values | Node labels = decoded values (e.g., "0x1F90 = 8080" for u16be port) |
| Click filters on single (offset, value) | Click filters on field-level semantics |

### Field Header Row
Above each column (or group of columns), draw a label with the field name. Multi-byte fields get spanning brackets connecting the columns they cover.

### Multi-byte Brackets
Visual bracket above columns: e.g., `[--- src_port ---]` spanning off_0 and off_1. Drawn using canvas line items with small vertical ends.

### Pad Column Rendering
- Width reduced to ~30px (vs. normal 150px)
- Background: dotted pattern
- Label: "PAD" + average padding count
- No individual value nodes (all NULL)
- Single edge through the column (no branching)

### Edge Styling
- Inter-field: normal appearance (width 2, arrows, full opacity)
- Intra-field (within a multi-byte field): dimmed (width 1, reduced opacity)
- Edges touching pad columns: dashed/dotted

### Decoded Labels
When structure is active and "Show decoded values" toggle is on:
- Multi-byte fields show the decoded value on the spanning bracket (e.g., "8080")
- Single-byte fields show decoded value alongside raw (e.g., "ACK" for flags)
- Controlled by toolbar toggle checkbox

---

## Structure-Based Filtering

1. **"Filter: structure match"** toggle in toolbar: only records matching the structure are included in the aligned table
2. **Per-field filter from field headers**: click a field header -> dropdown of distinct decoded values -> filter to one or more
3. **"Find misalignments"** button: iterates all records, flags where alignment fails or magic bytes don't match. Results in a scrollable popup with record ID, mismatch offset, expected vs. actual value. Click a result to filter the DAG to that record.
4. **Field-level statistics panel**: collapsible side panel with per-field distinct value count, most common value, entropy, match rate

---

## Implementation Phases

### Phase 0: Extract Layout Engine + SVG CLI (~150 lines)

Prerequisite for all subsequent work.

1. Extract layout computation from `CanvasApp.draw_dag()` and `_write_svg()` into `LayoutEngine.compute(dag, structure, display_options, filter_seeds) -> LayoutResult`
2. Extract SVG generation into `SVGRenderer.render(layout_result) -> str`
3. Make `CanvasApp.draw_dag()` use `LayoutEngine` + `CanvasRenderer`
4. Add `--svg` CLI argument for headless SVG export
5. Add `--structure` CLI argument (load and apply structure)
6. **Checkpoint**: `cat tests/hex_samples.txt | uv run daguire hex 8 --svg /tmp/test.svg` produces output identical to the current `_write_svg()` — regression test. Read the SVG to verify nodes and edges are present and correct.

### Phase 1: Structure Data Model & Storage (~100 lines)

1. Define `Field` dataclass: `name`, `type`, `offset`, `size`, `description`, `enum`, `align_to`, `match_byte`, `bit_offset`
2. Define `Structure` dataclass: `name`, `version`, `description`, `endian`, `fields`
3. `StructureStore`: directory `~/.daguire/structures/`, list/load/save/delete `.dgs` files
4. Field size computation: mapping type strings to byte sizes
5. Validation: no overlapping fields, offsets in range, valid types
6. Add `--list-structures` CLI argument
7. **Checkpoint**: create a `.dgs` file manually, confirm `--list-structures` lists it, confirm `--structure <name> --svg` loads without error.

### Phase 2: Structure Parser Engine (~150 lines)

1. `StructureParser.match_record()`: check if a record matches the structure constraints (magic bytes, enum values)
2. `StructureParser.align_record()`: return aligned byte list with virtual NULLs for pad fields
3. `StructureParser.decode_field()`: use `struct.unpack()` for u16/u32/f32 etc., ASCII decoding, enum lookup, bitfield extraction
4. `StructureParser.get_match_stats()`: aggregate match/alignment statistics
5. **Checkpoint**: pipe test data with `--structure` and `--svg`, verify aligned SVG has more columns and field metadata comments.

### Phase 3: Aligned DAG in SQLite (~80 lines)

1. `Dag.build_aligned_table(structure)`: creates `records_aligned` table, inserts aligned records
2. `Dag.get_struct_field_at(offset)`: return which Field covers an aligned offset
3. Parameterize `get_val_counts_by_offset()` and `get_edge_counts_by_offsets()` to work on either `records` or `records_aligned`
4. View switching: when structure is active, draw from `records_aligned`
5. **Checkpoint**: verify via SVG that aligned table produces wider output when structure has pad fields.

### Phase 4: Structure Editor Dialog (~200 lines)

1. `StructureEditorDialog(tk.Toplevel)` with left panel (saved structures list) and right panel (field editor table)
2. `ttk.Treeview` for field list with columns: Name, Type, Offset, Size, Description
3. Type dropdown (`ttk.Combobox`) with all supported types
4. Inline editing (double-click to edit cells)
5. Enum sub-editor panel
6. Validation highlighting (overlaps, out-of-range)
7. Save / Save As / Apply to DAG / Cancel buttons
8. **Checkpoint**: GUI-only feature; verify "Apply to DAG" button triggers correct `build_aligned_table()` and LayoutEngine path; re-export CLI SVG to confirm consistency.

### Phase 5: Right-Click Context Menus & Interactive Authoring (~100 lines)

1. Bind `ButtonPress-3` on canvas for right-click context menus
2. Menu items: create structure, add to structure, mark as padding, set as magic, create enum
3. "Decode as" quick-decode tooltip
4. **Checkpoint**: verify context menu actions create correct `.dgs` entries; re-export CLI SVG to confirm.

### Phase 6: Structure-Aware DAG Drawing (~200 lines)

1. Field header row above columns with field names
2. Multi-byte field spanning brackets
3. Pad column collapse (narrow width, dotted background)
4. Decoded value labels on nodes and brackets
5. Edge styling: inter-field (normal), intra-field (dimmed), pad (dashed)
6. "Show decoded values" toolbar toggle
7. **Checkpoint**: CLI SVG with structure applied must contain: `class="field-header"`, `class="field-bracket"`, `class="pad-column"`, `class="edge-dimmed"`, `class="decoded-label"`, and structure metadata comments.

### Phase 7: Structure-Based Filtering & Analysis (~80 lines)

1. "Filter: structure match" toggle in toolbar
2. Per-field value filters from field header clicks
3. "Find misalignments" button and results popup
4. Field-level statistics side panel
5. **Checkpoint**: `--svg` with `--filter-match` flag produces SVG showing only matched records.

### Phase 8: Polish & Advanced Features (~90 lines)

1. Edge weight visualization (scale width proportional to transition count)
2. Saved filter presets in `~/.daguire/presets/`
3. Structure templates for common patterns (magic header, TLV, fixed-header + payload)
4. Performance: cache aligned table results, avoid rebuilding on every redraw
5. Future: Kaitai `.ksy` import/export

---

## File Structure After Implementation

```
daguire/
+-- daguire.py                 (single file, ~1800 lines -- grows from ~640)
+-- pyproject.toml             (no changes to dependencies)
+-- README.md                  (updated with new features)
+-- task.md                    (this file)
+-- tests/
|   +-- hex_samples.txt
|   +-- file_list.txt
|   +-- bin/
|   |   +-- sample1.bin
|   |   +-- sample2.bin
|   |   +-- sample3.bin
|   +-- test_structures/       (NEW - test .dgs files)
|       +-- simple_header.dgs
|       +-- padded_tlv.dgs
|       +-- multi_endian.dgs
+-- ~/.daguire/                 (created at runtime)
    +-- structures/            (user's saved .dgs files)
```

All new code lives in `daguire.py` (single-file convention). No new dependencies.

---

## Estimated Size

| Phase | Feature | Est. Lines | SVG CLI? |
|---|---|---|---|
| 0 | Extract layout engine + SVG CLI | ~150 | Yes (key deliverable) |
| 1 | Structure data model + storage | ~100 | Yes (`--list-structures`) |
| 2 | Structure parser engine | ~150 | Yes (`--structure`) |
| 3 | Aligned DAG in SQLite | ~80 | Yes (`--structure` + `--svg`) |
| 4 | Structure editor dialog | ~200 | No (GUI only) |
| 5 | Right-click menus & interactive authoring | ~100 | Partially |
| 6 | Structure-aware DAG drawing | ~200 | Yes (key deliverable) |
| 7 | Structure-based filtering & analysis | ~80 | Yes (`--filter-match`) |
| 8 | Polish | ~90 | Yes |

**Total**: ~1150 new lines, `daguire.py` grows from ~640 to ~1800 lines.

Phases 0-4 form the minimum viable feature set. Phases 5-7 make it truly intuitive. Phase 8 is nice-to-have.