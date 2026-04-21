#! /usr/bin/env python3
import sys
import sqlite3
import argparse
import re
import xml.sax.saxutils as saxutils
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path

# Tkinter imports
import tkinter as tk
from tkinter import ttk
from tkinter.filedialog import asksaveasfilename


# ============================================================================
# Structure Data Model & Storage (Phase 1)
# ============================================================================


@dataclass
class Field:
    """A single field in a structure definition."""

    name: str
    type: str
    offset: int
    size: int = 0  # computed from type
    description: str = ""
    enum: dict[str, str] = field(default_factory=dict)  # value -> label mapping
    align_to: int = 0  # for pad fields: alignment boundary
    match_byte: Optional[int] = None  # for pad fields: specific byte value
    bit_offset: int = 0  # for bitfield types
    magic_value: Optional[bytes] = None  # for magic fields: expected byte sequence

    def __post_init__(self):
        if self.size == 0:
            self.size = compute_field_size(self.type, self.magic_value)


@dataclass
class Structure:
    """A complete structure definition (.dgs format)."""

    name: str
    version: int = 1
    description: str = ""
    endian: str = "big"  # "big" or "little"
    fields: list[Field] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Serialize to dictionary for JSON export."""
        # Sort fields by offset for deterministic output
        sorted_fields = sorted(self.fields, key=lambda f: f.offset)

        # Compute total_size as max(offset + size) across all fields
        total_size = 0
        for f in sorted_fields:
            if f.size > 0:
                total_size = max(total_size, f.offset + f.size)

        fields_data = []
        for f in sorted_fields:
            fd = {
                "name": f.name,
                "type": f.type,
                "offset": f.offset,
                "size": f.size,
            }
            if f.description:
                fd["description"] = f.description
            # Always serialize enum as dict (empty {} if none)
            fd["enum"] = f.enum if f.enum else {}
            if f.align_to:
                fd["align_to"] = f.align_to
            if f.match_byte is not None:
                fd["match_byte"] = f.match_byte
            if f.bit_offset:
                fd["bit_offset"] = f.bit_offset
            if f.magic_value is not None:
                fd["magic_value"] = f.magic_value.hex().upper()
            fields_data.append(fd)

        result = {
            "name": self.name,
            "version": self.version,
            "fields": fields_data,
        }
        if self.description:
            result["description"] = self.description
        if self.endian != "big":
            result["endian"] = self.endian
        if total_size > 0:
            result["total_size"] = total_size

        return result

    @classmethod
    def from_dict(cls, data: dict) -> "Structure":
        """Deserialize from dictionary (JSON load)."""
        fields = []
        for fd in data.get("fields", []):
            # Parse magic_value if present (stored as hex string in JSON)
            magic_value = None
            if fd.get("magic_value"):
                magic_value = bytes.fromhex(fd["magic_value"])

            field = Field(
                name=fd["name"],
                type=fd["type"],
                offset=fd["offset"],
                size=fd.get("size", compute_field_size(fd["type"], magic_value)),
                description=fd.get("description", ""),
                enum=fd.get("enum") or {},
                align_to=fd.get("align_to") or 0,
                match_byte=fd.get("match_byte"),
                bit_offset=fd.get("bit_offset") or 0,
                magic_value=magic_value,
            )
            fields.append(field)
        return cls(
            name=data.get("name", "unnamed"),
            version=data.get("version", 1),
            description=data.get("description", ""),
            endian=data.get("endian", "big"),
            fields=fields,
        )


# ============================================================================
# Structure Parser Engine (Phase 2)
# ============================================================================

import struct


class StructureParser:
    """Evaluates structures against records, performs alignment and decoding."""

    def __init__(self, structure: Structure, dag_sz: int):
        self.structure = structure
        self.dag_sz = dag_sz
        self._match_cache: dict[int, bool] = {}
        self._aligned_cache: dict[int, list[int | None]] = {}

    def match_record(self, record_bytes: list[int | None], record_id: int) -> bool:
        """Check if a record matches the structure constraints.

        Checks magic bytes and enum value constraints.
        Returns True if the record is compatible with the structure.
        """
        if record_id in self._match_cache:
            return self._match_cache[record_id]

        for field in self.structure.fields:
            # Skip pad fields for matching
            if field.type == "pad":
                continue

            # Check if we have enough bytes
            if field.offset + field.size > len(record_bytes):
                self._match_cache[record_id] = False
                return False

            # Extract raw bytes for this field
            raw_bytes = record_bytes[field.offset : field.offset + field.size]

            # Check for None bytes (incomplete record)
            if any(b is None for b in raw_bytes):
                self._match_cache[record_id] = False
                return False

            # Check magic field
            if field.type.startswith("magic"):
                if field.magic_value is None:
                    # No magic value specified - skip validation
                    continue
                if bytes(raw_bytes) != field.magic_value:
                    self._match_cache[record_id] = False
                    return False

            # Check enum field - validate value is in enum mapping
            if field.type.startswith("enum:"):
                underlying_type = field.type[5:]
                try:
                    value = self._unpack_bytes(bytes(raw_bytes), underlying_type)
                    if value is not None and str(value) not in field.enum:
                        # Value not in enum - record doesn't match
                        self._match_cache[record_id] = False
                        return False
                except struct.error:
                    self._match_cache[record_id] = False
                    return False

        self._match_cache[record_id] = True
        return True

    def align_record(
        self, record_bytes: list[int | None], record_id: int
    ) -> list[int | None]:
        """Return aligned byte list with virtual NULLs for pad fields.

        The aligned record may be wider than the original due to inserted padding.

        Uses dual-position tracking:
        - raw_pos: position in the raw record bytes (what we've consumed)
        - aligned_pos: position in the aligned output (where we are in the structure)
        """
        if record_id in self._aligned_cache:
            return self._aligned_cache[record_id]

        aligned: list[int | None] = []
        raw_pos = 0  # Position in raw record bytes
        aligned_pos = 0  # Position in aligned output (structure space)

        fields = self.structure.fields
        num_fields = len(fields)

        for i, field in enumerate(fields):
            # Determine where this field should start in aligned space
            field_start_aligned = field.offset

            # If we're before the field's declared offset, insert NULLs
            while aligned_pos < field_start_aligned:
                aligned.append(None)
                aligned_pos += 1

            if field.type == "pad":
                # Pad field: insert virtual NULLs, don't consume raw bytes
                # The pad fills from its declared offset to the next field's offset
                # align_to specifies the alignment boundary for the next field
                target_offset = field.offset

                if i + 1 < num_fields:
                    next_field = fields[i + 1]
                    target_offset = next_field.offset

                    # If align_to is specified, ensure next field starts at aligned boundary
                    if field.align_to and field.align_to > 0:
                        # Compute the aligned offset: next multiple of align_to >= next_field.offset
                        remainder = next_field.offset % field.align_to
                        if remainder != 0:
                            target_offset = next_field.offset + (
                                field.align_to - remainder
                            )

                # Insert NULLs until we reach the target offset
                while aligned_pos < target_offset:
                    aligned.append(None)
                    aligned_pos += 1
            elif field.type == "leb128":
                # LEB128: variable length, decode to find how many bytes to consume
                if raw_pos < len(record_bytes):
                    # Read LEB128 encoded value
                    leb_bytes = []
                    shift = 0
                    result = 0
                    while raw_pos < len(record_bytes):
                        byte = record_bytes[raw_pos]
                        if byte is None:
                            break
                        leb_bytes.append(byte)
                        result |= (byte & 0x7F) << shift
                        shift += 7
                        raw_pos += 1
                        if (byte & 0x80) == 0:
                            break
                    # Add the raw LEB128 bytes to aligned output
                    for b in leb_bytes:
                        aligned.append(b)
                        aligned_pos += 1
                else:
                    # Not enough bytes - add NULL
                    aligned.append(None)
                    aligned_pos += 1
            elif field.type.startswith("bitfield"):
                # bitfield<N>: extract N bits from current byte position
                # For simplicity, we consume one byte and mark the bitfield
                if raw_pos < len(record_bytes):
                    byte = record_bytes[raw_pos]
                    if byte is not None:
                        aligned.append(byte)
                        raw_pos += 1
                        aligned_pos += 1
                    else:
                        aligned.append(None)
                        aligned_pos += 1
                else:
                    aligned.append(None)
                    aligned_pos += 1
            else:
                # Standard fixed-size field: copy bytes from raw to aligned
                for j in range(field.size):
                    if raw_pos < len(record_bytes):
                        byte = record_bytes[raw_pos]
                        aligned.append(byte)
                        raw_pos += 1
                    else:
                        aligned.append(None)
                    aligned_pos += 1

        self._aligned_cache[record_id] = aligned
        return aligned

    def _unpack_bytes(self, data: bytes, type_str: str) -> int | float | None:
        """Unpack bytes according to type string using struct module."""
        try:
            if type_str in ("u8", "s8"):
                fmt = "B" if type_str == "u8" else "b"
                return struct.unpack(fmt, data)[0]
            elif type_str == "u16be":
                return struct.unpack(">H", data)[0]
            elif type_str == "u16le":
                return struct.unpack("<H", data)[0]
            elif type_str == "s16be":
                return struct.unpack(">h", data)[0]
            elif type_str == "s16le":
                return struct.unpack("<h", data)[0]
            elif type_str == "u32be":
                return struct.unpack(">I", data)[0]
            elif type_str == "u32le":
                return struct.unpack("<I", data)[0]
            elif type_str == "s32be":
                return struct.unpack(">i", data)[0]
            elif type_str == "s32le":
                return struct.unpack("<i", data)[0]
            elif type_str == "u64be":
                return struct.unpack(">Q", data)[0]
            elif type_str == "u64le":
                return struct.unpack("<Q", data)[0]
            elif type_str == "s64be":
                return struct.unpack(">q", data)[0]
            elif type_str == "s64le":
                return struct.unpack("<q", data)[0]
            elif type_str == "f32be":
                return struct.unpack(">f", data)[0]
            elif type_str == "f32le":
                return struct.unpack("<f", data)[0]
            elif type_str == "f64be":
                return struct.unpack(">d", data)[0]
            elif type_str == "f64le":
                return struct.unpack("<d", data)[0]
            elif type_str.startswith("ascii"):
                return data.decode("ascii", errors="replace")
        except struct.error:
            return None
        return None

    def decode_field(
        self, record_bytes: list[int | None], field: Field
    ) -> str | int | float | None:
        """Decode a field's raw bytes to a human-readable value.

        Returns the decoded value, with enum labels applied if applicable.
        """
        if field.offset + field.size > len(record_bytes):
            return None

        raw_bytes = record_bytes[field.offset : field.offset + field.size]
        if any(b is None for b in raw_bytes):
            return None

        try:
            data = bytes(raw_bytes)

            # Handle enum types
            if field.type.startswith("enum:"):
                underlying = field.type[5:]
                value = self._unpack_bytes(data, underlying)
                if value is not None:
                    # Return enum label if available, else raw value
                    return field.enum.get(str(value), str(value))
                return None

            # Handle ASCII types
            if field.type.startswith("ascii"):
                return data.decode("ascii", errors="replace")

            # Handle magic types (return as hex string)
            if field.type.startswith("magic"):
                return data.hex().upper() if data else None

            # Handle standard numeric types
            value = self._unpack_bytes(data, field.type)
            if value is not None:
                # Check if there's an enum mapping for this value
                if field.enum and str(value) in field.enum:
                    return field.enum[str(value)]
                return value

            return None
        except (struct.error, ValueError):
            return None

    def get_match_stats(self, records: list[tuple[int, list[int | None]]]) -> dict:
        """Aggregate match/alignment statistics over a list of records.

        records: list of (record_id, record_bytes) tuples
        Returns dict with match_count, total_count, match_rate, field_stats.
        """
        match_count = 0
        total_count = len(records)
        field_stats = {
            f.name: {"decoded": 0, "total": 0} for f in self.structure.fields
        }

        for record_id, record_bytes in records:
            if self.match_record(record_bytes, record_id):
                match_count += 1
                aligned = self.align_record(record_bytes, record_id)

                for field in self.structure.fields:
                    field_stats[field.name]["total"] += 1
                    if field.type != "pad":
                        decoded = self.decode_field(aligned, field)
                        if decoded is not None:
                            field_stats[field.name]["decoded"] += 1

        match_rate = (match_count / total_count * 100) if total_count > 0 else 0.0

        return {
            "match_count": match_count,
            "total_count": total_count,
            "match_rate": match_rate,
            "field_stats": field_stats,
        }


# Field type to size mapping (in bytes)
FIELD_SIZE_MAP = {
    "u8": 1,
    "s8": 1,
    "u16be": 2,
    "u16le": 2,
    "s16be": 2,
    "s16le": 2,
    "u32be": 4,
    "u32le": 4,
    "s32be": 4,
    "s32le": 4,
    "u64be": 8,
    "u64le": 8,
    "s64be": 8,
    "s64le": 8,
    "f32be": 4,
    "f32le": 4,
    "f64be": 8,
    "f64le": 8,
    "pad": 0,  # pad fields don't consume bytes, they insert virtual NULLs
    "leb128": 0,  # variable length
}


def compute_field_size(type_str: str, magic_value: Optional[bytes] = None) -> int:
    """Compute size in bytes for a field type.

    Handles fixed sizes and parameterized types like ascii<N>, raw<N>, bitfield<N>.
    For plain 'magic' type, uses magic_value length if provided.
    Returns 0 for variable-length types.
    """
    import re

    # Check fixed-size types
    if type_str in FIELD_SIZE_MAP:
        return FIELD_SIZE_MAP[type_str]

    # Parameterized types: ascii<N>, raw<N>, bitfield<N>, magic<N>
    match = re.match(r"^(ascii|raw|bitfield|magic)\((\d+)\)$", type_str)
    if match:
        return int(match.group(2))

    # Plain magic type - size from magic_value
    if type_str == "magic" and magic_value is not None:
        return len(magic_value)

    # Enum type - size depends on underlying type
    if type_str.startswith("enum:"):
        underlying = type_str[5:]
        return compute_field_size(underlying)

    # Unknown type
    return 0


def validate_structure(structure: Structure, max_offset: int) -> list[str]:
    """Validate a structure definition.

    Returns a list of error messages (empty if valid).
    """
    errors = []

    # Check for valid types
    valid_type_prefixes = (
        "u8",
        "s8",
        "u16",
        "s16",
        "u32",
        "s32",
        "u64",
        "s64",
        "f32",
        "f64",
        "ascii",
        "raw",
        "bitfield",
        "magic",
        "pad",
        "leb128",
        "enum:",
    )
    for f in structure.fields:
        type_valid = (
            f.type in FIELD_SIZE_MAP
            or re.match(r"^(ascii|raw|bitfield|magic)\(\d+\)$", f.type)
            or f.type.startswith("enum:")
            or f.type == "magic"  # plain magic type (size from magic_value)
        )
        if not type_valid:
            errors.append(f"Field '{f.name}': invalid type '{f.type}'")

    # Check for overlapping fields
    sorted_fields = sorted(structure.fields, key=lambda f: f.offset)
    for i in range(len(sorted_fields) - 1):
        f1 = sorted_fields[i]
        f2 = sorted_fields[i + 1]
        f1_end = f1.offset + f1.size
        if f1_end > f2.offset:
            errors.append(
                f"Fields '{f1.name}' (ends at {f1_end}) and '{f2.name}' "
                f"(starts at {f2.offset}) overlap"
            )

    # Check offsets are in range
    for f in structure.fields:
        if f.offset < 0:
            errors.append(f"Field '{f.name}': negative offset {f.offset}")
        if f.offset >= max_offset:
            errors.append(
                f"Field '{f.name}': offset {f.offset} >= max offset {max_offset}"
            )
        if f.offset + f.size > max_offset and f.size > 0:
            errors.append(
                f"Field '{f.name}': extends beyond max offset "
                f"({f.offset} + {f.size} > {max_offset})"
            )

    # Check pad field properties
    for f in structure.fields:
        if f.type == "pad":
            if f.align_to and f.align_to <= 0:
                errors.append(f"Field '{f.name}': align_to must be positive")

    return errors


class StructureStore:
    """CRUD operations for .dgs structure files.

    Structures are stored in ~/.daguire/structures/ as JSON files.
    """

    def __init__(self):
        self.structures_dir = Path.home() / ".daguire" / "structures"
        self.structures_dir.mkdir(parents=True, exist_ok=True)

    def list_structures(self) -> list[str]:
        """Return list of structure names (filenames without .dgs extension)."""
        if not self.structures_dir.exists():
            return []
        return sorted([p.stem for p in self.structures_dir.glob("*.dgs")])

    def load(self, name: str) -> Optional[Structure]:
        """Load a structure by name."""
        path = self.structures_dir / f"{name}.dgs"
        if not path.exists():
            return None
        try:
            import json

            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return Structure.from_dict(data)
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            print(f"Error loading structure '{name}': {e}", file=sys.stderr)
            return None

    def save(self, structure: Structure) -> bool:
        """Save a structure to disk."""
        path = self.structures_dir / f"{structure.name}.dgs"
        try:
            import json

            with open(path, "w", encoding="utf-8") as f:
                json.dump(structure.to_dict(), f, indent=2)
            return True
        except (IOError, TypeError) as e:
            print(f"Error saving structure '{structure.name}': {e}", file=sys.stderr)
            return False

    def delete(self, name: str) -> bool:
        """Delete a structure by name."""
        path = self.structures_dir / f"{name}.dgs"
        if not path.exists():
            return False
        try:
            path.unlink()
            return True
        except IOError as e:
            print(f"Error deleting structure '{name}': {e}", file=sys.stderr)
            return False

    def export_ksy(self, name: str) -> Optional[str]:
        """Export a structure to Kaitai Struct .ksy format (future)."""
        # TODO: implement Kaitai interop
        return None

    def import_ksy(self, ksy_path: Path) -> Optional[Structure]:
        """Import a structure from Kaitai Struct .ksy format (future)."""
        # TODO: implement Kaitai interop
        return None


# ============================================================================
# Layout Engine - Pure computation, no GUI dependencies
# ============================================================================


@dataclass
class PlacedNode:
    """A node with computed layout coordinates."""

    offset: int
    val: int | None
    ratio: float
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    color: str = "#2d2d3a"

    def get_color(self, v):
        if v is None:
            return "#2d2d3a"
        if v == 0xFF:
            return "#3d3d4a"
        if v == 0x00:
            return "#1a1a24"
        if v < 0x20:
            return "#c45c5c"
        if 0x20 <= v <= 0x7F:
            return "#b8a84e"
        if 0x7F < v <= 0xBF:
            return "#4a9b99"
        return "#4a9b6a"


@dataclass
class PlacedEdge:
    """An edge with computed layout coordinates."""

    src_offset: int
    src_val: int | None
    dst_offset: int
    dst_val: int | None
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0


@dataclass
class FieldHeader:
    """A structure field header for rendering above columns."""

    name: str
    field_type: str
    start_offset: int
    end_offset: int  # exclusive
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    decoded_value: str = ""  # decoded value for multi-byte fields


@dataclass
class PadColumn:
    """Collapsed padding column info."""

    offset: int
    width: int  # number of NULL columns
    x1: float = 0.0
    y1: float = 0.0
    x2: float = 0.0
    y2: float = 0.0
    label: str = "PAD"


@dataclass
class LayoutResult:
    """Complete layout output from LayoutEngine."""

    nodes: list[PlacedNode] = field(default_factory=list)
    edges: list[PlacedEdge] = field(default_factory=list)
    field_headers: list[FieldHeader] = field(default_factory=list)
    pad_columns: list[PadColumn] = field(default_factory=list)
    max_y: float = 0.0
    layout_right: float = 0.0
    layout_bottom: float = 0.0


class LayoutEngine:
    """Pure layout computation engine - no tkinter, no SVG, just coordinates."""

    def __init__(
        self,
        xpad: int = 150,
        ypad: int = 150,
        node_width: int = 150,
        col_height: int = 600,
    ):
        self.xpad = xpad
        self.ypad = ypad
        self.node_width = node_width
        self.col_height = col_height

    def compute(
        self,
        dag,
        display_options: dict[str, bool],
        filter_seeds: set[tuple[int, int]] | None = None,
        use_aligned: bool = False,
        show_structure: bool = True,
    ) -> LayoutResult:
        """Compute layout for the DAG, returning LayoutResult.

        If use_aligned=True and dag has a structure, uses the aligned table.
        If show_structure=True, generates field headers and pad columns.
        """
        # Determine effective DAG size (aligned may be wider)
        dag_sz = dag.get_aligned_sz() if use_aligned and dag.structure else dag.sz

        visible_nodes = (
            dag.get_visible_nodes_filtered(filter_seeds) if filter_seeds else None
        )
        result = LayoutResult()
        prev_offset_nodes: list[PlacedNode] = []
        x_offset = 0.0
        layout_max_y = 0.0

        # Track field spans for structure-aware rendering
        field_spans = {}  # offset -> (field, start_x, end_x)
        pad_regions = []  # list of (start_offset, end_offset, start_x, end_x)

        for o in range(0, dag_sz):
            # Check if this is a pad column
            is_pad_column = False
            if show_structure and dag.structure:
                field = dag.get_struct_field_at(o)
                if field and field.type == "pad":
                    is_pad_column = True

            # Build nodes for this offset
            nodes: list[PlacedNode] = []
            if not is_pad_column or not show_structure:
                for v, vct in dag.get_val_counts_by_offset(o, use_aligned=use_aligned):
                    if visible_nodes is None or (o, v) in visible_nodes:
                        nodes.append(
                            PlacedNode(
                                offset=o,
                                val=v,
                                ratio=vct,
                                color=PlacedNode.get_color(None, v),
                            )
                        )

            total_ratio = sum(n.ratio for n in nodes) if nodes else 0
            y_position = 0.0

            for node in nodes:
                height = (
                    (node.ratio / total_ratio) * (self.col_height - 2 * self.ypad)
                    if total_ratio
                    else 0
                )
                node.x1, node.y1 = x_offset, y_position
                node.x2, node.y2 = x_offset + self.node_width, y_position + height
                y_position += height + self.ypad

            layout_max_y = max(layout_max_y, y_position)
            result.nodes.extend(nodes)

            # Build edges from previous offset to this one
            if o != 0:
                edges_data = dag.get_edge_counts_by_offsets(
                    o - 1, o, use_aligned=use_aligned
                )
                for edge in edges_data:
                    src_val, dst_val = edge[0], edge[1]
                    if src_val is None or dst_val is None:
                        continue
                    if visible_nodes is not None:
                        if (o - 1, src_val) not in visible_nodes or (
                            o,
                            dst_val,
                        ) not in visible_nodes:
                            continue
                    src_node = next(
                        (n for n in prev_offset_nodes if n.val == src_val), None
                    )
                    dst_node = next((n for n in nodes if n.val == dst_val), None)
                    if src_node is None or dst_node is None:
                        continue
                    _, sy1, sx2, sy2 = (
                        src_node.x1,
                        src_node.y1,
                        src_node.x2,
                        src_node.y2,
                    )
                    dx1, dy1, _, dy2 = (
                        dst_node.x1,
                        dst_node.y1,
                        dst_node.x2,
                        dst_node.y2,
                    )
                    placed_edge = PlacedEdge(
                        src_offset=o - 1,
                        src_val=src_val,
                        dst_offset=o,
                        dst_val=dst_val,
                        x1=sx2,
                        y1=sy1 + (sy2 - sy1) / 2,
                        x2=dx1,
                        y2=dy1 + (dy2 - dy1) / 2,
                    )
                    result.edges.append(placed_edge)

            # Track field spans for structure rendering
            if show_structure and dag.structure:
                field = dag.get_struct_field_at(o)
                if field:
                    if field.type == "pad":
                        # Track pad region
                        if not pad_regions or pad_regions[-1][1] != o:
                            pad_regions.append(
                                [o, o + 1, x_offset, x_offset + self.node_width]
                            )
                        else:
                            pad_regions[-1][1] = o + 1
                            pad_regions[-1][3] = x_offset + self.node_width
                    else:
                        # Track regular field span
                        field_spans[o] = (field, x_offset, x_offset + self.node_width)

            x_offset += self.xpad * 2
            prev_offset_nodes = nodes

        # Generate field headers from field spans
        if show_structure and dag.structure and dag.structure.fields:
            # Group consecutive offsets by field
            field_groups = {}  # field_name -> (field, min_x, max_x, min_offset, max_offset)
            for o, (field, x1, x2) in field_spans.items():
                if field.name not in field_groups:
                    field_groups[field.name] = [field, x1, x2, o, o]
                else:
                    fg = field_groups[field.name]
                    fg[1] = min(fg[1], x1)
                    fg[2] = max(fg[2], x2)
                    fg[3] = min(fg[3], o)
                    fg[4] = max(fg[4], o)

            # Create FieldHeader objects
            header_y = -40  # Above the nodes
            for name, (field, x1, x2, min_o, max_o) in field_groups.items():
                fh = FieldHeader(
                    name=field.name,
                    field_type=field.type,
                    start_offset=min_o,
                    end_offset=max_o + 1,
                    x1=x1,
                    y1=header_y,
                    x2=x2,
                    y2=header_y + 25,
                )
                result.field_headers.append(fh)

            # Create PadColumn objects
            for start_o, end_o, x1, x2 in pad_regions:
                width = end_o - start_o
                pc = PadColumn(
                    offset=start_o,
                    width=width,
                    x1=x1,
                    y1=0,
                    x2=x2,
                    y2=layout_max_y,
                    label=f"PAD ({width})",
                )
                result.pad_columns.append(pc)

        # Compute layout bounds
        result.layout_right = (
            (dag_sz - 1) * (self.xpad * 2) + self.node_width + self.xpad
        )
        result.layout_bottom = layout_max_y + self.ypad
        result.max_y = layout_max_y
        return result


class SVGRenderer:
    """Renders LayoutResult to SVG string."""

    def __init__(self, theme: dict, xpad: int = 150, ypad: int = 150):
        self.theme = theme
        self.xpad = xpad
        self.ypad = ypad
        self.node_width = 150
        self.r = 25

    def render(
        self,
        layout: LayoutResult,
        dag_sz: int,
        display_options: dict[str, bool],
        structure_name: Optional[str] = None,
    ) -> str:
        """Render layout to SVG string."""
        font_name, font_size = self.theme["font"][0], self.theme["font"][1]

        # Compute bounding box
        if not layout.nodes:
            w, h = 800, 600
        else:
            shape_max_x = max(n.x2 for n in layout.nodes)
            shape_max_y = max(n.y2 for n in layout.nodes)
            w = int(max(shape_max_x, layout.layout_right) + self.xpad)
            h = int(max(shape_max_y, layout.layout_bottom) + self.ypad)

        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">',
            f'  <defs><marker id="arrow" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><polygon points="0 0, 10 3.5, 0 7" fill="{self.theme["node_text"]}"/></marker></defs>',
            f'  <rect width="{w}" height="{h}" fill="{self.theme["canvas_bg"]}"/>',
        ]

        # Add structure metadata comment if applicable
        if structure_name:
            lines.insert(3, f"  <!-- Structure: {structure_name} -->")

        # Draw pad columns first (behind everything)
        for pc in layout.pad_columns:
            lines.append(
                f'  <rect x1="{pc.x1:.1f}" y1="{pc.y1:.1f}" x2="{pc.x2:.1f}" y2="{pc.y2:.1f}" class="pad-column" fill="none" stroke="{self.theme["node_outline"]}" stroke-width="1" stroke-dasharray="5,5"/>'
            )
            # Pad label
            mid_x = (pc.x1 + pc.x2) / 2
            mid_y = (pc.y1 + pc.y2) / 2
            lines.append(
                f'  <text x="{mid_x:.1f}" y="{mid_y:.1f}" text-anchor="middle" dominant-baseline="middle" class="pad-label" fill="{self.theme["node_text"]}" font-family="{font_name}" font-size="{font_size}" opacity="0.5">{saxutils.escape(pc.label)}</text>'
            )

        # Draw field headers (above nodes)
        for fh in layout.field_headers:
            # Header background
            lines.append(
                f'  <rect x="{fh.x1:.1f}" y="{fh.y1:.1f}" width="{fh.x2 - fh.x1:.1f}" height="{fh.y2 - fh.y1:.1f}" class="field-header-bg" fill="{self.theme["toolbar_bg"]}" stroke="{self.theme["node_outline"]}" stroke-width="1"/>'
            )
            # Field name
            mid_x = (fh.x1 + fh.x2) / 2
            mid_y = (fh.y1 + fh.y2) / 2
            lines.append(
                f'  <text x="{mid_x:.1f}" y="{mid_y:.1f}" text-anchor="middle" dominant-baseline="middle" class="field-header" data-field-name="{saxutils.escape(fh.name)}" fill="{self.theme["node_text"]}" font-family="{font_name}" font-size="{font_size}" font-weight="bold">{saxutils.escape(fh.name)}</text>'
            )
            # Multi-byte bracket (if field spans multiple columns)
            if fh.end_offset - fh.start_offset > 1:
                bracket_y = fh.y1 - 15
                # Top line of bracket
                lines.append(
                    f'  <line x1="{fh.x1 + 10:.1f}" y1="{bracket_y:.1f}" x2="{fh.x2 - 10:.1f}" y2="{bracket_y:.1f}" class="field-bracket" stroke="{self.theme["node_outline"]}" stroke-width="2"/>'
                )
                # Left end
                lines.append(
                    f'  <line x1="{fh.x1 + 10:.1f}" y1="{bracket_y:.1f}" x2="{fh.x1 + 10:.1f}" y2="{bracket_y + 5:.1f}" class="field-bracket" stroke="{self.theme["node_outline"]}" stroke-width="2"/>'
                )
                # Right end
                lines.append(
                    f'  <line x1="{fh.x2 - 10:.1f}" y1="{bracket_y:.1f}" x2="{fh.x2 - 10:.1f}" y2="{bracket_y + 5:.1f}" class="field-bracket" stroke="{self.theme["node_outline"]}" stroke-width="2"/>'
                )

        # Draw edges first (so they appear behind nodes)
        for edge in layout.edges:
            # Determine edge style based on whether it crosses field boundaries
            edge_class = "edge-normal"
            stroke_width = 2
            if layout.field_headers:
                src_field = None
                dst_field = None
                for fh in layout.field_headers:
                    if fh.start_offset <= edge.src_offset < fh.end_offset:
                        src_field = fh.name
                    if fh.start_offset <= edge.dst_offset < fh.end_offset:
                        dst_field = fh.name
                if src_field and dst_field and src_field == dst_field:
                    # Intra-field edge: dimmed
                    edge_class = "edge-dimmed"
                    stroke_width = 1

            # Edge weight visualization (Phase 8): scale width by transition count
            # Note: edge counts not available in SVG renderer without dag reference
            # Use a base width that can be enhanced in CanvasRenderer
            lines.append(
                f'  <line x1="{edge.x1:.1f}" y1="{edge.y1:.1f}" x2="{edge.x2:.1f}" y2="{edge.y2:.1f}" class="{edge_class}" stroke="{self.theme["node_text"]}" stroke-width="{stroke_width}" marker-end="url(#arrow)"/>'
            )

        # Draw nodes
        for node in layout.nodes:
            label = format_byte_label(node.val, display_options)
            text_fill = (
                self.theme["node_text_light"]
                if node.val == 0x00
                else self.theme["node_text"]
            )
            lines.append(
                f'  <rect x="{node.x1:.1f}" y="{node.y1:.1f}" width="{node.x2 - node.x1:.1f}" height="{node.y2 - node.y1:.1f}" rx="{self.r}" ry="{self.r}" fill="{node.color}" stroke="{self.theme["node_outline"]}" stroke-width="{self.theme["node_outline_width"]}"/>'
            )
            text_x, text_y = (node.x1 + node.x2) / 2, (node.y1 + node.y2) / 2
            label_lines = label.split("\n")
            line_height = font_size * 1.2
            start_y = text_y - (len(label_lines) - 1) * line_height / 2
            for i, line in enumerate(label_lines):
                escaped = saxutils.escape(line)
                dy = start_y + i * line_height
                lines.append(
                    f'  <text x="{text_x:.1f}" y="{dy:.1f}" text-anchor="middle" dominant-baseline="middle" fill="{text_fill}" font-family="{font_name}" font-size="{font_size}">{escaped}</text>'
                )

        lines.append("</svg>")
        return "\n".join(lines)


class CanvasRenderer:
    """Renders LayoutResult to tkinter canvas."""

    def __init__(self, canvas, theme: dict, xpad: int = 150, ypad: int = 150):
        self.canvas = canvas
        self.theme = theme
        self.xpad = xpad
        self.ypad = ypad
        self.node_width = 150
        self.r = 25

        self.tk_module = tk

    def render(
        self,
        layout: LayoutResult,
        display_options: dict[str, bool],
        filter_seeds: set[tuple[int, int]] | None = None,
    ):
        """Render layout to tkinter canvas."""
        font_name, font_size = self.theme["font"][0], self.theme["font"][1]
        tk = self.tk_module

        # Draw pad columns first (behind everything)
        for pc in layout.pad_columns:
            self.canvas.create_rectangle(
                pc.x1,
                pc.y1,
                pc.x2,
                pc.y2,
                outline=self.theme["node_outline"],
                width=1,
                dash=(5, 5),
                tags=("pad-column",),
            )
            mid_x = (pc.x1 + pc.x2) / 2
            mid_y = (pc.y1 + pc.y2) / 2
            self.canvas.create_text(
                mid_x,
                mid_y,
                text=pc.label,
                fill=self.theme["node_text"],
                font=font_name,
                opacity=0.5,
                tags=("pad-label",),
            )

        # Draw field headers
        for fh in layout.field_headers:
            # Header background
            self.canvas.create_rectangle(
                fh.x1,
                fh.y1,
                fh.x2,
                fh.y2,
                fill=self.theme["toolbar_bg"],
                outline=self.theme["node_outline"],
                width=1,
                tags=("field-header-bg", f"field-header-{fh.name}"),
            )
            # Field name
            mid_x = (fh.x1 + fh.x2) / 2
            mid_y = (fh.y1 + fh.y2) / 2
            self.canvas.create_text(
                mid_x,
                mid_y,
                text=fh.name,
                fill=self.theme["node_text"],
                font=(font_name[0], font_size[1], "bold"),
                tags=("field-header", f"field-header-{fh.name}"),
            )
            # Multi-byte bracket
            if fh.end_offset - fh.start_offset > 1:
                bracket_y = fh.y1 - 15
                # Top line
                self.canvas.create_line(
                    fh.x1 + 10,
                    bracket_y,
                    fh.x2 - 10,
                    bracket_y,
                    fill=self.theme["node_outline"],
                    width=2,
                    tags=("field-bracket", f"field-bracket-{fh.name}"),
                )
                # Left end
                self.canvas.create_line(
                    fh.x1 + 10,
                    bracket_y,
                    fh.x1 + 10,
                    bracket_y + 5,
                    fill=self.theme["node_outline"],
                    width=2,
                    tags=("field-bracket", f"field-bracket-{fh.name}"),
                )
                # Right end
                self.canvas.create_line(
                    fh.x2 - 10,
                    bracket_y,
                    fh.x2 - 10,
                    bracket_y + 5,
                    fill=self.theme["node_outline"],
                    width=2,
                    tags=("field-bracket", f"field-bracket-{fh.name}"),
                )

        # Draw edges with structure-aware styling
        for edge in layout.edges:
            stroke_width = 2
            # Determine if intra-field edge (dimmed) and edge weight
            stroke_width = 2
            if layout.field_headers:
                src_field = None
                dst_field = None
                for fh in layout.field_headers:
                    if fh.start_offset <= edge.src_offset < fh.end_offset:
                        src_field = fh.name
                    if fh.start_offset <= edge.dst_offset < fh.end_offset:
                        dst_field = fh.name
                if src_field and dst_field and src_field == dst_field:
                    stroke_width = 1  # Dimmed intra-field edge

            # Scale edge width by transition count (Phase 8: edge weight visualization)
            # Get edge count from DAG
            edge_counts = dag.get_edge_counts_by_offsets(
                edge.src_offset, edge.dst_offset, use_aligned=False
            )
            for ec in edge_counts:
                if ec[0] == edge.src_val and ec[1] == edge.dst_val:
                    count = ec[2] if len(ec) > 2 else 1
                    # Scale width: 1-5 transitions -> width 1-3, cap at 5
                    weight_width = min(5, max(1, int(count / 10) + 1))
                    stroke_width = max(stroke_width, weight_width)
                    break

            self.canvas.create_line(
                edge.x1,
                edge.y1,
                edge.x2,
                edge.y2,
                fill=self.theme["node_text"],
                width=stroke_width,
                smooth=True,
                arrow=tk.LAST,
                tags=("edge", f"edge-{edge.src_offset}-{edge.dst_offset}"),
            )

        # Draw nodes
        for node in layout.nodes:
            label = format_byte_label(node.val, display_options)
            text_fill = (
                self.theme["node_text_light"]
                if node.val == 0x00
                else self.theme["node_text"]
            )
            tag = f"node_{node.offset}_{node.val}"
            tags = ("node", tag)

            self.create_round_rectangle(
                node.x1,
                node.y1,
                node.x2,
                node.y2,
                self.r,
                fill=node.color,
                outline=self.theme["node_outline"],
                width=self.theme["node_outline_width"],
                tags=tags,
            )
            text_x = (node.x1 + node.x2) / 2
            text_y = (node.y1 + node.y2) / 2
            self.canvas.create_text(
                text_x,
                text_y,
                text=label,
                fill=text_fill,
                anchor=self.tk_module.CENTER,
                font=self.theme["font"],
                tags=tags,
            )

    def create_round_rectangle(self, x1, y1, x2, y2, r=25, tags=(), **kwargs):
        if "tags" in kwargs:
            tags = kwargs.pop("tags")
        points = (
            x1 + r,
            y1,
            x1 + r,
            y1,
            x2 - r,
            y1,
            x2 - r,
            y1,
            x2,
            y1,
            x2,
            y1 + r,
            x2,
            y1 + r,
            x2,
            y2 - r,
            x2,
            y2 - r,
            x2,
            y2,
            x2 - r,
            y2,
            x2 - r,
            y2,
            x1 + r,
            y2,
            x1 + r,
            y2,
            x1,
            y2,
            x1,
            y2 - r,
            x1,
            y2 - r,
            x1,
            y1 + r,
            x1,
            y1 + r,
            x1,
            y1,
        )
        return self.canvas.create_polygon(points, tags=tags, **kwargs, smooth=True)


# Display format options for byte labels (used when drawing nodes)
def format_byte_label(val: int | None, options: dict[str, bool]) -> str:
    if val is None:
        return str(None)
    parts = []
    if options.get("decimal", True):
        parts.append(str(val))
    if options.get("hex", True):
        parts.append(f"0x{val:02X}")
    if options.get("binary", True):
        parts.append(f"{val:b}")
    if options.get("ascii", True):
        parts.append(chr(val) if 0x20 <= val <= 0x7E else "·")
    return "\n".join(parts) if parts else str(val)


class Dag:
    def __init__(self, conn: sqlite3.Connection, fmt="hex", sz=8):
        self.conn = conn
        self.fmt = fmt
        self.sz = sz
        self.colnames = None
        self.valnames = None
        self.structure = None  # Optional[Structure] - set when structure is applied
        self.parser = None  # Optional[StructureParser] - set when structure is applied
        self.initDb()
        if self.fmt == "hex":
            self.read_lines()
        else:
            self.read_files()

    def initDb(self):
        cur = self.conn.cursor()
        q = """CREATE TABLE IF NOT EXISTS "records" (
        "id"	INTEGER,
        """
        for i in range(0, self.sz):
            q += f"off_{i}	INTEGER,\n"
        q += """PRIMARY KEY("id" AUTOINCREMENT)
                        );
        """
        cur.execute(q)
        col_names = "("
        for i in range(0, self.sz):
            col_names += f"off_{i},"
        col_names = col_names[:-1]
        col_names += ")"
        self.colnames = col_names
        val_names = "("
        for i in range(0, self.sz):
            val_names += f"?,"
        val_names = val_names[:-1]
        val_names += ")"
        self.valnames = val_names

    def read_lines(self):
        print("Reading data from STDIN")
        cur = self.conn.cursor()
        for line in sys.stdin:
            try:
                byte_line = list(bytearray.fromhex(line.strip()))[: self.sz]
                if len(byte_line) < self.sz:
                    byte_line.extend([None] * (self.sz - len(byte_line)))
                iq = f"INSERT INTO records {self.colnames} VALUES {self.valnames};"
                cur.execute(iq, byte_line)
            except:
                print(f"Failure parsing: {line.strip()}", file=sys.stderr)
        self.conn.commit()

    def read_files(self):
        print("Reading data from FILES")
        cur = self.conn.cursor()
        for path in sys.stdin:
            try:
                with open(path.strip(), "rb") as f:
                    byte_line = list(bytearray(f.read(self.sz)))
                    if len(byte_line) < self.sz:
                        byte_line.extend([None] * (self.sz - len(byte_line)))
                    iq = f"INSERT INTO records {self.colnames} VALUES {self.valnames};"
                    cur.execute(iq, byte_line)
            except Exception as e:
                print(f"Failure parsing: {path.strip()}, {e}", file=sys.stderr)
        self.conn.commit()

    def get_val_counts_by_offset(self, o: int):
        cur = self.conn.cursor()
        res = cur.execute(
            f"SELECT off_{o}, count(*) FROM records GROUP BY off_{o} ORDER BY count(*) ASC;"
        )
        return res.fetchall()

    def get_edge_counts_by_offsets(self, o0: int, o1: int):
        cur = self.conn.cursor()
        res = cur.execute(
            f"SELECT off_{o0}, off_{o1}, count(*) AS ect from records GROUP BY off_{o0}, off_{o1};"
        )
        return res.fetchall()

    def get_downstream_from(self, seeds: set[tuple[int, int]]) -> set[tuple[int, int]]:
        """All (offset, value) nodes reachable by following edges forward from seeds."""
        if not seeds:
            return set()
        reachable = set(seeds)
        for o in range(0, self.sz - 1):
            for off, val in list(reachable):
                if off != o:
                    continue
                for row in self.get_edge_counts_by_offsets(o, o + 1):
                    if row[0] == val:
                        reachable.add((o + 1, row[1]))
        return reachable

    def get_visible_nodes_filtered(
        self, seeds: set[tuple[int, int]]
    ) -> set[tuple[int, int]]:
        """Per-offset filter: at each offset, show only seed values at that offset (if any), else show nodes reachable from previous offset."""
        if not seeds:
            return set()
        visible = set()
        values_at_0 = {v for (v, _) in self.get_val_counts_by_offset(0)}
        seeds_at_0 = {v for (o, v) in seeds if o == 0}
        if seeds_at_0:
            visible |= {(0, v) for v in (seeds_at_0 & values_at_0)}
        else:
            visible |= {(0, v) for v in values_at_0}
        for o in range(1, self.sz):
            values_at_o = {v for (v, _) in self.get_val_counts_by_offset(o)}
            reachable = set()
            for row in self.get_edge_counts_by_offsets(o - 1, o):
                v_prev, v_cur, _ = row[0], row[1], row[2]
                if (o - 1, v_prev) in visible:
                    reachable.add(v_cur)
            seeds_at_o = {v for (o2, v) in seeds if o2 == o}
            if seeds_at_o:
                show_o = seeds_at_o & values_at_o
            else:
                show_o = reachable & values_at_o
            visible |= {(o, v) for v in show_o}
        return visible

    def build_aligned_table(self, structure: Structure, force: bool = False) -> None:
        """Build aligned records table from structure definition.

        Creates records_aligned table with virtual NULLs inserted for pad fields.
        The original records table is never modified.

        Uses caching to avoid rebuilding if the same structure is already loaded.
        """
        # Phase 8: Cache aligned table results
        if (
            not force
            and hasattr(self, "_aligned_structure")
            and self._aligned_structure == structure.name
        ):
            return  # Already built for this structure

        self.structure = structure
        self.parser = StructureParser(structure, self.sz)
        self._aligned_structure = structure.name

        cur = self.conn.cursor()

        # Determine max aligned width by aligning all records
        max_aligned_width = 0
        aligned_records = []

        for row in cur.execute("SELECT * FROM records"):
            record_id = row[0]
            record_bytes = list(row[1:])  # Skip id column
            aligned = self.parser.align_record(record_bytes, record_id)
            aligned_records.append((record_id, aligned))
            max_aligned_width = max(max_aligned_width, len(aligned))

        # Create aligned table with enough columns
        col_defs = '"id" INTEGER'
        for i in range(max_aligned_width):
            col_defs += f', "off_{i}" INTEGER'

        cur.execute(f"CREATE TABLE IF NOT EXISTS records_aligned ({col_defs})")
        cur.execute("DELETE FROM records_aligned")

        # Insert aligned records
        for record_id, aligned in aligned_records:
            # Pad aligned record to max width with None
            aligned_padded = aligned + [None] * (max_aligned_width - len(aligned))
            placeholders = ",".join(["?"] * (1 + len(aligned_padded)))
            cur.execute(
                f"INSERT INTO records_aligned VALUES ({placeholders})",
                [record_id] + aligned_padded,
            )

        self.conn.commit()
        self._aligned_sz = max_aligned_width

    def get_aligned_sz(self) -> int:
        """Return the width of the aligned table (may be > sz due to pad fields)."""
        if hasattr(self, "_aligned_sz"):
            return self._aligned_sz
        return self.sz

    def get_struct_field_at(self, offset: int) -> Optional[Field]:
        """Return which structure field covers an aligned offset, or None.

        Handles pad fields by computing effective end as next field's offset
        or align_to boundary. Returns None for gaps between fields.
        """
        if self.structure is None:
            return None

        fields = self.structure.fields
        if not fields:
            return None

        # Sort fields by offset for proper range checking
        sorted_fields = sorted(fields, key=lambda f: f.offset)

        for i, field in enumerate(sorted_fields):
            if field.type == "pad":
                # Pad fields span from their offset to the next field's offset
                # or to offset + align_to if specified
                if offset < field.offset:
                    continue
                # Find the end of this pad region
                if i + 1 < len(sorted_fields):
                    next_field = sorted_fields[i + 1]
                    pad_end = next_field.offset
                elif field.align_to:
                    # Align to boundary: pad extends to next align_to boundary
                    pad_end = field.offset + field.align_to
                else:
                    # Last field is pad with no align_to - extends to end of record
                    # For lookup purposes, consider it covers from offset onward
                    return field

                if offset < pad_end:
                    return field
            else:
                # Regular field: standard range check
                if field.offset <= offset < field.offset + field.size:
                    return field

        return None

    def get_val_counts_by_offset(self, o: int, use_aligned: bool = False):
        """Get value counts for offset o.

        If use_aligned=True and structure is active, queries records_aligned table.
        """
        table = "records_aligned" if use_aligned and self.structure else "records"
        cur = self.conn.cursor()
        res = cur.execute(
            f"SELECT off_{o}, count(*) FROM {table} GROUP BY off_{o} ORDER BY count(*) ASC;"
        )
        return res.fetchall()

    def get_edge_counts_by_offsets(self, o0: int, o1: int, use_aligned: bool = False):
        """Get edge counts between offsets o0 and o1.

        If use_aligned=True and structure is active, queries records_aligned table.
        """
        table = "records_aligned" if use_aligned and self.structure else "records"
        cur = self.conn.cursor()
        res = cur.execute(
            f"SELECT off_{o0}, off_{o1}, count(*) AS ect from {table} GROUP BY off_{o0}, off_{o1};"
        )
        return res.fetchall()


# ============================================================================
# Structure Editor Dialog (Phase 4)
# ============================================================================


class StructureEditorDialog:
    """Dialog for creating and editing structure definitions."""

    SUPPORTED_TYPES = [
        "u8",
        "s8",
        "u16be",
        "u16le",
        "s16be",
        "s16le",
        "u32be",
        "u32le",
        "s32be",
        "s32le",
        "u64be",
        "u64le",
        "s64be",
        "s64le",
        "f32be",
        "f32le",
        "f64be",
        "f64le",
        "ascii(1)",
        "ascii(4)",
        "ascii(8)",
        "ascii(16)",
        "raw(1)",
        "raw(4)",
        "raw(8)",
        "magic",
        "pad",
        "leb128",
    ]

    # Structure templates for common patterns (Phase 8)
    TEMPLATES = {
        "magic_header": {
            "name": "magic_header",
            "description": "Common magic header pattern",
            "fields": [
                {"name": "magic", "type": "magic", "offset": 0, "size": 4},
                {"name": "version", "type": "u16be", "offset": 4},
                {"name": "flags", "type": "u8", "offset": 6},
                {"name": "length", "type": "u16be", "offset": 7},
            ],
        },
        "tlv": {
            "name": "tlv",
            "description": "Type-Length-Value pattern",
            "fields": [
                {"name": "type", "type": "u8", "offset": 0},
                {"name": "length", "type": "u8", "offset": 1},
                {"name": "value", "type": "raw(8)", "offset": 2},
            ],
        },
        "fixed_header_payload": {
            "name": "fixed_header_payload",
            "description": "Fixed header with payload",
            "fields": [
                {"name": "header", "type": "u32be", "offset": 0},
                {"name": "pad", "type": "pad", "offset": 4, "align_to": 4},
                {"name": "payload_start", "type": "u8", "offset": 8},
            ],
        },
    }

    def __init__(
        self,
        parent,
        structure: Optional[Structure] = None,
        dag_sz: int = 8,
        on_apply: Optional[callable] = None,
    ):
        from tkinter import simpledialog, messagebox

        self.tk_module = tk
        self.ttk_module = ttk
        self.simpledialog = simpledialog
        self.messagebox = messagebox

        self.parent = parent
        self.structure = structure
        self.dag_sz = dag_sz
        self.on_apply = on_apply  # Callback when structure is applied
        self.current_field = None  # Currently selected field
        self.enum_entries = {}  # Enum value->label entries

        self.dialog = tk.Toplevel(parent)
        self.dialog.title("Structure Editor")
        self.dialog.transient(parent)
        self.dialog.geometry("900x600")

        self._build_ui()
        self._populate_fields()
        self._populate_enum_editor()

        # Center dialog
        self.dialog.update_idletasks()
        x = (self.dialog.winfo_screenwidth() // 2) - 450
        y = (self.dialog.winfo_screenheight() // 2) - 300
        self.dialog.geometry(f"900x600+{x}+{y}")

        # Modal
        self.dialog.grab_set()
        parent.wait_window(self.dialog)

    def _build_ui(self):
        tk = self.tk_module
        ttk = self.ttk_module

        # Main container
        main_frame = ttk.Frame(self.dialog, padding=10)
        main_frame.pack(fill="both", expand=True)

        # Top row: Structure name and description
        top_frame = ttk.Frame(main_frame)
        top_frame.pack(fill="x", pady=(0, 10))

        ttk.Label(top_frame, text="Structure Name:").pack(side="left", padx=(0, 5))
        self.name_var = tk.StringVar(
            value=self.structure.name if self.structure else ""
        )
        self.name_entry = ttk.Entry(top_frame, textvariable=self.name_var, width=30)
        self.name_entry.pack(side="left", padx=(0, 20))

        ttk.Label(top_frame, text="Description:").pack(side="left", padx=(0, 5))
        self.desc_var = tk.StringVar(
            value=self.structure.description if self.structure else ""
        )
        self.desc_entry = ttk.Entry(top_frame, textvariable=self.desc_var, width=40)
        self.desc_entry.pack(side="left")

        # Middle section: Two panels
        middle_frame = ttk.PanedWindow(main_frame, orient="horizontal")
        middle_frame.pack(fill="both", expand=True)

        # Left panel: Saved structures
        left_frame = ttk.Frame(middle_frame, padding=5)
        middle_frame.add(left_frame, weight=1)

        ttk.Label(left_frame, text="Saved Structures", font=("", 10, "bold")).pack(
            pady=(0, 5)
        )

        self.structure_listbox = tk.Listbox(left_frame, width=25)
        self.structure_listbox.pack(fill="both", expand=True, pady=(0, 5))

        # Populate structure list
        store = StructureStore()
        for name in store.list_structures():
            self.structure_listbox.insert("end", name)

        self.structure_listbox.bind("<<ListboxSelect>>", self._on_structure_selected)

        btn_frame = ttk.Frame(left_frame)
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="Load", command=self._load_structure, width=8).pack(
            side="left", padx=2
        )
        ttk.Button(
            btn_frame, text="Delete", command=self._delete_structure, width=8
        ).pack(side="left", padx=2)

        # Templates button (Phase 8)
        ttk.Button(
            btn_frame, text="Templates", command=self._show_templates, width=8
        ).pack(side="left", padx=2, pady=(5, 0))

        # Right panel: Field editor
        right_frame = ttk.Frame(middle_frame, padding=5)
        middle_frame.add(right_frame, weight=3)

        ttk.Label(right_frame, text="Fields", font=("", 10, "bold")).pack(pady=(0, 5))

        # Field table
        columns = ("name", "type", "offset", "size", "description")
        self.field_tree = ttk.Treeview(
            right_frame, columns=columns, show="headings", height=10
        )

        self.field_tree.heading("name", text="Name")
        self.field_tree.heading("type", text="Type")
        self.field_tree.heading("offset", text="Offset")
        self.field_tree.heading("size", text="Size")
        self.field_tree.heading("description", text="Description")

        self.field_tree.column("name", width=100)
        self.field_tree.column("type", width=80)
        self.field_tree.column("offset", width=60)
        self.field_tree.column("size", width=50)
        self.field_tree.column("description", width=200)

        self.field_tree.pack(fill="both", expand=True, pady=(0, 5))
        self.field_tree.bind("<<TreeviewSelect>>", self._on_field_selected)
        self.field_tree.bind("<Double-1>", self._on_field_double_click)

        # Field editor buttons
        field_btn_frame = ttk.Frame(right_frame)
        field_btn_frame.pack(fill="x", pady=(0, 10))

        ttk.Button(
            field_btn_frame, text="Add Field", command=self._add_field, width=12
        ).pack(side="left", padx=2)
        ttk.Button(
            field_btn_frame, text="Remove", command=self._remove_field, width=12
        ).pack(side="left", padx=2)
        ttk.Button(
            field_btn_frame, text="Move Up", command=self._move_field_up, width=12
        ).pack(side="left", padx=2)
        ttk.Button(
            field_btn_frame, text="Move Down", command=self._move_field_down, width=12
        ).pack(side="left", padx=2)

        # Enum editor panel
        enum_frame = ttk.LabelFrame(right_frame, text="Enum Values", padding=5)
        enum_frame.pack(fill="x", pady=(0, 10))

        self.enum_frame_inner = ttk.Frame(enum_frame)
        self.enum_frame_inner.pack(fill="both", expand=True)

        ttk.Label(
            self.enum_frame_inner, text="(Select an enum field to edit values)"
        ).pack()

        # Bottom section: Action buttons
        bottom_frame = ttk.Frame(main_frame)
        bottom_frame.pack(fill="x", pady=(10, 0))

        # Validation status
        self.status_var = tk.StringVar(value="")
        self.status_label = ttk.Label(
            bottom_frame, textvariable=self.status_var, foreground="red"
        )
        self.status_label.pack(side="left", padx=(0, 10))

        ttk.Button(
            bottom_frame, text="Save", command=self._save_structure, width=10
        ).pack(side="right", padx=2)
        ttk.Button(
            bottom_frame, text="Save As...", command=self._save_as_structure, width=10
        ).pack(side="right", padx=2)
        ttk.Button(
            bottom_frame,
            text="Apply to DAG",
            command=self._apply_structure,
            width=12,
        ).pack(side="right", padx=2)
        ttk.Button(
            bottom_frame, text="Cancel", command=self.dialog.destroy, width=10
        ).pack(side="right", padx=2)

    def _populate_fields(self):
        """Populate field tree with current structure's fields."""
        for item in self.field_tree.get_children():
            self.field_tree.delete(item)

        if self.structure:
            for field in sorted(self.structure.fields, key=lambda f: f.offset):
                self.field_tree.insert(
                    "",
                    "end",
                    values=(
                        field.name,
                        field.type,
                        field.offset,
                        field.size,
                        field.description,
                    ),
                )

    def _populate_enum_editor(self):
        """Populate enum editor panel for currently selected field."""
        for widget in self.enum_frame_inner.winfo_children():
            widget.destroy()

        if self.current_field and self.current_field.type.startswith("enum:"):
            ttk.Label(self.enum_frame_inner, text="Value -> Label mappings:").pack(
                anchor="w"
            )

            # Existing enum values
            enum_values = self.current_field.enum or {}
            self.enum_entries = {}

            for value, label in sorted(enum_values.items(), key=lambda x: int(x[0])):
                row_frame = ttk.Frame(self.enum_frame_inner)
                row_frame.pack(fill="x", pady=1)

                value_entry = ttk.Entry(row_frame, width=10)
                value_entry.insert(0, value)
                value_entry.pack(side="left", padx=(0, 5))

                label_entry = ttk.Entry(row_frame, width=20)
                label_entry.insert(0, label)
                label_entry.pack(side="left", padx=(0, 5))

                ttk.Button(
                    row_frame,
                    text="×",
                    width=2,
                    command=lambda f=row_frame: self._remove_enum_row(f),
                ).pack(side="left")

                self.enum_entries[f] = (value_entry, label_entry)

            # Add new row button
            ttk.Button(
                self.enum_frame_inner, text="Add Value", command=self._add_enum_row
            ).pack(pady=(5, 0))
        else:
            ttk.Label(
                self.enum_frame_inner, text="(Select an enum field to edit values)"
            ).pack()

    def _add_enum_row(self):
        """Add a new enum value row."""
        row_frame = ttk.Frame(self.enum_frame_inner)
        row_frame.pack(fill="x", pady=1)

        value_entry = ttk.Entry(row_frame, width=10)
        value_entry.pack(side="left", padx=(0, 5))

        label_entry = ttk.Entry(row_frame, width=20)
        label_entry.pack(side="left", padx=(0, 5))

        ttk.Button(
            row_frame,
            text="×",
            width=2,
            command=lambda f=row_frame: self._remove_enum_row(f),
        ).pack(side="left")

        self.enum_entries[row_frame] = (value_entry, label_entry)

    def _remove_enum_row(self, frame):
        """Remove an enum value row."""
        if frame in self.enum_entries:
            del self.enum_entries[frame]
        frame.destroy()

    def _get_enum_values(self) -> dict[str, str]:
        """Get enum values from editor."""
        result = {}
        for frame, (value_entry, label_entry) in self.enum_entries.items():
            value = value_entry.get().strip()
            label = label_entry.get().strip()
            if value and label:
                try:
                    result[str(int(value))] = label
                except ValueError:
                    pass
        return result

    def _on_structure_selected(self, event):
        """Handle structure selection from list."""
        selection = self.structure_listbox.curselection()
        if not selection:
            return

        name = self.structure_listbox.get(selection[0])
        store = StructureStore()
        self.structure = store.load(name)
        if self.structure:
            self.name_var.set(self.structure.name)
            self.desc_var.set(self.structure.description)
            self._populate_fields()

    def _load_structure(self):
        """Load selected structure."""
        selection = self.structure_listbox.curselection()
        if not selection:
            self.messagebox.showwarning("Warning", "Select a structure to load")
            return

        name = self.structure_listbox.get(selection[0])
        store = StructureStore()
        self.structure = store.load(name)
        if self.structure:
            self.name_var.set(self.structure.name)
            self.desc_var.set(self.structure.description)
            self._populate_fields()

    def _delete_structure(self):
        """Delete selected structure."""
        selection = self.structure_listbox.curselection()
        if not selection:
            self.messagebox.showwarning("Warning", "Select a structure to delete")
            return

        name = self.structure_listbox.get(selection[0])
        if self.messagebox.askyesno("Confirm", f"Delete structure '{name}'?"):
            store = StructureStore()
            store.delete(name)
            self.structure_listbox.delete(selection[0])
            if self.structure and self.structure.name == name:
                self.structure = None
                self._populate_fields()

    def _show_templates(self):
        """Show structure templates dialog (Phase 8)."""
        dialog = self.tk_module.Toplevel(self.dialog)
        dialog.title("Structure Templates")
        dialog.geometry("400x300")
        dialog.transient(self.dialog)

        ttk.Label(
            dialog,
            text="Select a template to use as a starting point:",
            font=("", 10, "bold"),
        ).pack(pady=10)

        # Template list
        template_list = tk.Listbox(dialog, width=50, height=10)
        template_list.pack(fill="both", expand=True, padx=20, pady=10)

        for name, template in self.TEMPLATES.items():
            template_list.insert("end", f"{name}: {template['description']}")

        def use_template():
            selection = template_list.curselection()
            if not selection:
                return

            name = list(self.TEMPLATES.keys())[selection[0]]
            template = self.TEMPLATES[name]

            # Create structure from template
            self.structure = Structure(
                name=template["name"],
                description=template.get("description", ""),
            )

            for fd in template["fields"]:
                self.structure.fields.append(
                    Field(
                        name=fd["name"],
                        type=fd["type"],
                        offset=fd["offset"],
                        size=fd.get("size", compute_field_size(fd["type"])),
                    )
                )

            self._populate_fields()
            dialog.destroy()

        ttk.Button(dialog, text="Use Template", command=use_template).pack(pady=10)
        ttk.Button(dialog, text="Cancel", command=dialog.destroy).pack()

        dialog.grab_set()

    def _on_field_selected(self, event):
        """Handle field selection."""
        selection = self.field_tree.selection()
        if not selection:
            self.current_field = None
            self._populate_enum_editor()
            return

        item = self.field_tree.item(selection[0])
        values = item["values"]
        field_name = values[0]

        # Find the field in current structure
        if self.structure:
            for f in self.structure.fields:
                if f.name == field_name:
                    self.current_field = f
                    break
        else:
            # Field not in structure yet (newly added)
            self.current_field = None

        self._populate_enum_editor()

    def _on_field_double_click(self, event):
        """Handle double-click to edit field properties."""
        selection = self.field_tree.selection()
        if not selection:
            return

        item = self.field_tree.item(selection[0])
        values = item["values"]

        # Create edit dialog
        edit_dialog = self.tk_module.Toplevel(self.dialog)
        edit_dialog.title("Edit Field")
        edit_dialog.transient(self.dialog)
        edit_dialog.geometry("400x300")

        form_frame = ttk.Frame(edit_dialog, padding=20)
        form_frame.pack(fill="both", expand=True)

        # Name
        ttk.Label(form_frame, text="Name:").grid(row=0, column=0, sticky="w", pady=5)
        name_var = self.tk_module.StringVar(value=values[0])
        ttk.Entry(form_frame, textvariable=name_var, width=30).grid(
            row=0, column=1, pady=5
        )

        # Type
        ttk.Label(form_frame, text="Type:").grid(row=1, column=0, sticky="w", pady=5)
        type_var = self.tk_module.StringVar(value=values[1])
        type_combo = ttk.Combobox(
            form_frame, textvariable=type_var, values=self.SUPPORTED_TYPES, width=27
        )
        type_combo.grid(row=1, column=1, pady=5)

        # Offset
        ttk.Label(form_frame, text="Offset:").grid(row=2, column=0, sticky="w", pady=5)
        offset_var = self.tk_module.StringVar(value=str(values[2]))
        ttk.Entry(form_frame, textvariable=offset_var, width=30).grid(
            row=2, column=1, pady=5
        )

        # Description
        ttk.Label(form_frame, text="Description:").grid(
            row=3, column=0, sticky="nw", pady=5
        )
        desc_var = self.tk_module.StringVar(value=values[4] if len(values) > 4 else "")
        ttk.Entry(form_frame, textvariable=desc_var, width=30).grid(
            row=3, column=1, pady=5
        )

        def save_edit():
            new_values = (
                name_var.get(),
                type_var.get(),
                int(offset_var.get()) if offset_var.get().isdigit() else 0,
                compute_field_size(type_var.get()),
                desc_var.get(),
            )
            self.field_tree.item(selection[0], values=new_values)

            # Update current field if it matches
            if self.current_field and self.current_field.name == values[0]:
                self.current_field.name = new_values[0]
                self.current_field.type = new_values[1]
                self.current_field.offset = new_values[2]
                self.current_field.size = new_values[3]
                self.current_field.description = new_values[4]
                self._populate_enum_editor()

            edit_dialog.destroy()

        ttk.Button(edit_dialog, text="Save", command=save_edit).pack(
            side="right", pady=10
        )
        ttk.Button(edit_dialog, text="Cancel", command=edit_dialog.destroy).pack(
            side="right", pady=10
        )

        edit_dialog.grab_set()
        self.dialog.wait_window(edit_dialog)

    def _add_field(self):
        """Add a new field."""
        # Suggest offset based on last field
        suggested_offset = 0
        if self.structure and self.structure.fields:
            last_field = max(self.structure.fields, key=lambda f: f.offset)
            suggested_offset = last_field.offset + last_field.size

        new_field = Field(
            name=f"field_{len(self.field_tree.get_children())}",
            type="u8",
            offset=suggested_offset,
            size=1,
            description="",
        )

        self.field_tree.insert(
            "",
            "end",
            values=(
                new_field.name,
                new_field.type,
                new_field.offset,
                new_field.size,
                new_field.description,
            ),
        )

        if not self.structure:
            self.structure = Structure(name="unnamed")
        self.structure.fields.append(new_field)
        self._validate_and_update_status()

    def _remove_field(self):
        """Remove selected field."""
        selection = self.field_tree.selection()
        if not selection:
            self.messagebox.showwarning("Warning", "Select a field to remove")
            return

        item = self.field_tree.item(selection[0])
        field_name = item["values"][0]

        if self.structure:
            self.structure.fields = [
                f for f in self.structure.fields if f.name != field_name
            ]

        self.field_tree.delete(selection[0])
        self.current_field = None
        self._populate_enum_editor()
        self._validate_and_update_status()

    def _move_field_up(self):
        """Move selected field up in the list."""
        selection = self.field_tree.selection()
        if not selection:
            return

        index = self.field_tree.index(selection[0])
        if index == 0:
            return

        # Swap with previous
        prev_index = index - 1
        prev_item = self.field_tree.item(self.field_tree.get_children()[prev_index])
        curr_item = self.field_tree.item(selection[0])

        self.field_tree.item(
            self.field_tree.get_children()[prev_index], values=curr_item["values"]
        )
        self.field_tree.item(selection[0], values=prev_item["values"])

        # Update structure if exists
        if self.structure:
            fields = sorted(self.structure.fields, key=lambda f: f.offset)
            if prev_index < len(fields) and index < len(fields):
                fields[prev_index], fields[index] = fields[index], fields[prev_index]

    def _move_field_down(self):
        """Move selected field down in the list."""
        selection = self.field_tree.selection()
        if not selection:
            return

        index = self.field_tree.index(selection[0])
        if index == len(self.field_tree.get_children()) - 1:
            return

        # Swap with next
        next_index = index + 1
        next_item = self.field_tree.item(self.field_tree.get_children()[next_index])
        curr_item = self.field_tree.item(selection[0])

        self.field_tree.item(
            self.field_tree.get_children()[next_index], values=curr_item["values"]
        )
        self.field_tree.item(selection[0], values=next_item["values"])

        # Update structure if exists
        if self.structure:
            fields = sorted(self.structure.fields, key=lambda f: f.offset)
            if next_index < len(fields) and index < len(fields):
                fields[next_index], fields[index] = fields[index], fields[next_index]

    def _validate_and_update_status(self):
        """Validate structure and update status label."""
        if not self.structure or not self.structure.fields:
            self.status_var.set("")
            return

        errors = validate_structure(self.structure, self.dag_sz)
        if errors:
            self.status_var.set("Errors: " + "; ".join(errors[:2]))
        else:
            self.status_var.set("Valid structure")

    def _save_structure(self):
        """Save current structure."""
        if not self.structure:
            self.structure = Structure(name=self.name_var.get() or "unnamed")

        # Update structure from UI
        self.structure.name = self.name_var.get() or "unnamed"
        self.structure.description = self.desc_var.get()

        # Update fields from tree
        self.structure.fields = []
        for item in self.field_tree.get_children():
            values = self.field_tree.item(item)["values"]
            field = Field(
                name=values[0],
                type=values[1],
                offset=int(values[2]),
                size=int(values[3]),
                description=values[4] if len(values) > 4 else "",
            )
            # Add enum values if this is an enum field
            if (
                field.type.startswith("enum:")
                and self.current_field
                and self.current_field.name == field.name
            ):
                field.enum = self._get_enum_values()
            self.structure.fields.append(field)

        # Validate
        errors = validate_structure(self.structure, self.dag_sz)
        if errors:
            self.messagebox.showerror(
                "Validation Error", "Cannot save:\n" + "\n".join(errors)
            )
            return

        store = StructureStore()
        if store.save(self.structure):
            # Refresh structure list
            self.structure_listbox.delete(0, "end")
            for name in store.list_structures():
                self.structure_listbox.insert("end", name)
            self.messagebox.showinfo(
                "Success", f"Structure '{self.structure.name}' saved"
            )
        else:
            self.messagebox.showerror("Error", "Failed to save structure")

    def _save_as_structure(self):
        """Save structure with new name."""
        new_name = self.simpledialog.askstring(
            "Save As", "Enter structure name:", initialvalue=self.name_var.get()
        )
        if new_name:
            self.name_var.set(new_name)
            self._save_structure()

    def _apply_structure(self):
        """Apply structure to DAG and close dialog."""
        # First save the structure
        self._save_structure()

        if self.structure and self.on_apply:
            self.on_apply(self.structure)

        self.dialog.destroy()


class CanvasApp(tk.Tk):
    # Theme: dark, modern palette
    THEME = {
        "bg": "#0f0f14",
        "toolbar_bg": "#16161e",
        "canvas_bg": "#1a1a24",
        "node_outline": "#3d3d5c",
        "node_outline_width": 2,
        "node_text": "#e4e4e7",
        "node_text_light": "#fafafa",
        "font": ("Consolas", 10),
        "toolbar_fg": "#a0a0b0",
        "accent": "#7c3aed",
    }

    def __init__(self, dag: Dag, structure: Optional[Structure] = None):
        super().__init__()
        self.tk_module = tk
        self.ttk_module = ttk
        self.asksaveasfilename = asksaveasfilename
        self.dag = dag
        self.structure = structure
        self.xpad = 150
        self.ypad = 150
        self.theme = self.THEME.copy()
        self.display_options = {
            "decimal": True,
            "hex": True,
            "binary": True,
            "ascii": True,
        }
        self.filter_seeds: set[tuple[int, int]] = set()
        self.structure_name = structure.name if structure else None
        self.use_aligned = (
            structure is not None
        )  # Phase 3: use aligned table when structure is active

        # Build aligned table if structure is provided
        if self.structure:
            self.dag.build_aligned_table(self.structure)

        self.layout_engine = LayoutEngine(xpad=self.xpad, ypad=self.ypad)
        self.canvas_renderer = None  # initialized after canvas is created

        self.title("DAGUIRE")
        self.configure(bg=self.theme["bg"])
        if sys.platform == "win32":
            self.state("zoomed")
        else:
            self.wm_attributes("-zoomed", 1)

        self._setup_styles()
        self._build_toolbar()
        self.frame = self.tk_module.Frame(self, bg=self.theme["bg"])
        self.frame.pack(fill="both", expand=True)
        self.canvas = self.tk_module.Canvas(
            self.frame, bg=self.theme["canvas_bg"], highlightthickness=0
        )
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<MouseWheel>", self.on_mousewheel)
        self.canvas.bind("<Button-4>", self.on_mousewheel)
        self.canvas.bind("<Button-5>", self.on_mousewheel)
        self.canvas.bind("<ButtonPress-1>", self.on_button_press)
        self.canvas.bind("<B1-Motion>", self.pan_canvas)
        self.canvas.bind("<ButtonRelease-1>", self.on_button_release)
        self.canvas.bind("<ButtonPress-3>", self.on_right_click)

        self.canvas_renderer = CanvasRenderer(
            self.canvas, self.theme, xpad=self.xpad, ypad=self.ypad
        )
        self.draw_dag()

    def _setup_styles(self):
        style = self.ttk_module.Style()
        style.theme_use("clam")
        style.configure(
            "Toolbar.TFrame",
            background=self.theme["toolbar_bg"],
        )
        style.configure(
            "Toolbar.TCheckbutton",
            background=self.theme["toolbar_bg"],
            foreground=self.theme["toolbar_fg"],
            font=self.theme["font"],
        )
        style.configure(
            "Toolbar.TButton",
            background=self.theme["toolbar_bg"],
            foreground=self.theme["toolbar_fg"],
            font=self.theme["font"],
        )
        style.map("Toolbar.TButton", background=[("active", self.theme["accent"])])
        style.configure(
            "Toolbar.TLabel",
            background=self.theme["toolbar_bg"],
            foreground=self.theme["toolbar_fg"],
            font=self.theme["font"],
        )

    def _build_toolbar(self):
        toolbar = self.ttk_module.Frame(self, style="Toolbar.TFrame", padding=(10, 8))
        toolbar.pack(fill="x")

        self.ttk_module.Button(
            toolbar,
            text="Save as SVG…",
            style="Toolbar.TButton",
            command=self.save_canvas_as_svg,
        ).pack(side="left", padx=(0, 16))
        self.ttk_module.Button(
            toolbar,
            text="Save as PS…",
            style="Toolbar.TButton",
            command=self.save_canvas_as_ps,
        ).pack(side="left", padx=(0, 16))
        self.ttk_module.Button(
            toolbar,
            text="Fit to Canvas",
            style="Toolbar.TButton",
            command=self.fit_to_canvas,
        ).pack(side="left", padx=(0, 16))

        self.ttk_module.Button(
            toolbar,
            text="Structure Editor...",
            style="Toolbar.TButton",
            command=self._open_structure_editor,
        ).pack(side="left", padx=(0, 16))

        # Structure filtering toggles (Phase 7)
        self.filter_match_var = self.tk_module.BooleanVar(value=False)
        self.filter_match_var.trace_add("write", self._on_filter_match_changed)
        self.filter_match_cb = self.ttk_module.Checkbutton(
            toolbar,
            text="Filter: structure match",
            variable=self.filter_match_var,
            style="Toolbar.TCheckbutton",
        )
        self.filter_match_cb.pack(side="left", padx=(0, 16))

        self.ttk_module.Button(
            toolbar,
            text="Find misalignments",
            style="Toolbar.TButton",
            command=self._find_misalignments,
        ).pack(side="left", padx=(0, 16))

        self.ttk_module.Button(
            toolbar,
            text="Field statistics",
            style="Toolbar.TButton",
            command=self._show_field_statistics,
        ).pack(side="left", padx=(0, 16))

        sep = self.tk_module.Frame(toolbar, width=1, bg=self.theme["node_outline"])
        sep.pack(side="left", fill="y", padx=8, pady=2)

        label = self.ttk_module.Label(
            toolbar, text="Node label:", style="Toolbar.TLabel"
        )
        label.pack(side="left", padx=(0, 6))
        for key, label_text in [
            ("decimal", "Dec"),
            ("hex", "Hex"),
            ("binary", "Bin"),
            ("ascii", "ASCII"),
        ]:
            var = self.tk_module.BooleanVar(value=self.display_options[key])
            var.trace_add("write", self._on_display_option_changed)
            self.display_options[f"_var_{key}"] = var
            cb = self.ttk_module.Checkbutton(
                toolbar, text=label_text, variable=var, style="Toolbar.TCheckbutton"
            )
            cb.pack(side="left", padx=2)

        # Filter bar (second row): shows filter seeds and Clear
        self.filter_bar = self.ttk_module.Frame(
            self, style="Toolbar.TFrame", padding=(10, 4)
        )
        self.filter_bar.pack(fill="x")
        self._filter_chips_frame = self.ttk_module.Frame(
            self.filter_bar, style="Toolbar.TFrame"
        )
        self._filter_chips_frame.pack(side="left", fill="x", expand=True)
        self._filter_placeholder = self.ttk_module.Label(
            self.filter_bar,
            text="Filter: click a node to show only downstream; Ctrl+click to add",
            style="Toolbar.TLabel",
        )
        self._filter_placeholder.pack(side="left")
        self._update_filter_bar()

    def _update_filter_bar(self):
        for w in self._filter_chips_frame.winfo_children():
            w.destroy()
        if not self.filter_seeds:
            self._filter_placeholder.pack(side="left")
            return
        self._filter_placeholder.pack_forget()
        self.ttk_module.Label(
            self._filter_chips_frame, text="Filter:", style="Toolbar.TLabel"
        ).pack(side="left", padx=(0, 6))
        for offset, val in sorted(self.filter_seeds):
            chip = self.ttk_module.Frame(
                self._filter_chips_frame, style="Toolbar.TFrame"
            )
            chip.pack(side="left", padx=2)
            lbl = self.ttk_module.Label(
                chip, text=f"0x{val:02X} @ {offset}", style="Toolbar.TLabel"
            )
            lbl.pack(side="left", padx=(4, 2), pady=2)
            btn = self.ttk_module.Button(
                chip,
                text="×",
                style="Toolbar.TButton",
                width=2,
                command=lambda o=offset, v=val: self._remove_filter_seed(o, v),
            )
            btn.pack(side="left", padx=(0, 4), pady=2)
        self.ttk_module.Button(
            self._filter_chips_frame,
            text="Clear",
            style="Toolbar.TButton",
            command=self._clear_filter,
        ).pack(side="left", padx=(8, 0))

    def _remove_filter_seed(self, offset: int, val: int):
        self.filter_seeds.discard((offset, val))
        self._update_filter_bar()
        self.redraw_dag()

    def _clear_filter(self):
        self.filter_seeds.clear()
        self._update_filter_bar()
        self.redraw_dag()

    def _on_display_option_changed(self, *args):
        for k in ("decimal", "hex", "binary", "ascii"):
            var = self.display_options.get(f"_var_{k}")
            if isinstance(var, self.tk_module.BooleanVar):
                self.display_options[k] = var.get()
        self.redraw_dag()

    def _open_structure_editor(self):
        """Open the structure editor dialog."""
        StructureEditorDialog(
            self,
            structure=self.structure,
            dag_sz=self.dag.sz,
            on_apply=self._on_structure_applied,
        )

    def _on_structure_applied(self, structure: Structure):
        """Callback when structure is applied from editor."""
        self.structure = structure
        self.structure_name = structure.name
        self.dag.build_aligned_table(structure)
        self.use_aligned = True
        self.redraw_dag()

    def _on_filter_match_changed(self, *args):
        """Handle structure match filter toggle."""
        # When enabled, filter to only records that match the structure
        if self.filter_match_var.get() and self.structure:
            # Get matching record IDs
            cur = self.dag.conn.cursor()
            matching_ids = set()
            for row in cur.execute("SELECT * FROM records"):
                record_id = row[0]
                record_bytes = list(row[1:])
                if self.dag.parser and self.dag.parser.match_record(
                    record_bytes, record_id
                ):
                    matching_ids.add(record_id)

            # Clear existing filter and add seeds from matching records
            self.filter_seeds.clear()
            # For each matching record, add the first non-NULL value as a seed
            for row in cur.execute(
                "SELECT * FROM records WHERE id IN ("
                + ",".join(str(i) for i in matching_ids)
                + ")"
            ):
                record_bytes = list(row[1:])
                for o, v in enumerate(record_bytes):
                    if v is not None:
                        self.filter_seeds.add((o, v))
                        break
        elif not self.filter_match_var.get():
            # Clear filter when disabled
            self.filter_seeds.clear()

        self._update_filter_bar()
        self.redraw_dag()

    def _find_misalignments(self):
        """Find records where structure alignment fails or magic bytes don't match."""
        if not self.structure or not self.dag.parser:
            self.messagebox.showwarning("No Structure", "Apply a structure first")
            return

        mismatches = []
        cur = self.dag.conn.cursor()

        for row in cur.execute("SELECT * FROM records"):
            record_id = row[0]
            record_bytes = list(row[1:])

            # Check if record matches structure
            if not self.dag.parser.match_record(record_bytes, record_id):
                # Find which field causes mismatch
                for field in self.structure.fields:
                    if field.type == "pad":
                        continue
                    if field.offset + field.size > len(record_bytes):
                        mismatches.append(
                            (record_id, field.offset, "Record too short", None, None)
                        )
                        break
                    raw_bytes = record_bytes[field.offset : field.offset + field.size]
                    if any(b is None for b in raw_bytes):
                        mismatches.append(
                            (record_id, field.offset, "Incomplete field", None, None)
                        )
                        break
                    if field.type.startswith("magic") and field.magic_value:
                        if bytes(raw_bytes) != field.magic_value:
                            mismatches.append(
                                (
                                    record_id,
                                    field.offset,
                                    "Magic mismatch",
                                    field.magic_value.hex().upper(),
                                    bytes(raw_bytes).hex().upper(),
                                )
                            )
                            break
                    if field.type.startswith("enum:"):
                        underlying = field.type[5:]
                        try:
                            value = self.dag.parser._unpack_bytes(
                                bytes(raw_bytes), underlying
                            )
                            if value is not None and str(value) not in field.enum:
                                mismatches.append(
                                    (
                                        record_id,
                                        field.offset,
                                        "Enum value not in mapping",
                                        str(value),
                                        list(field.enum.values()),
                                    )
                                )
                                break
                        except:
                            pass

        if not mismatches:
            self.messagebox.showinfo(
                "No Misalignments", "All records match the structure"
            )
            return

        # Show misalignments in a popup
        dialog = self.tk_module.Toplevel(self)
        dialog.title("Find Misalignments")
        dialog.geometry("600x400")
        dialog.transient(self)

        # Create treeview
        columns = ("record", "offset", "issue", "expected", "actual")
        tree = self.ttk_module.Treeview(dialog, columns=columns, show="headings")
        tree.heading("record", text="Record ID")
        tree.heading("offset", text="Offset")
        tree.heading("issue", text="Issue")
        tree.heading("expected", text="Expected")
        tree.heading("actual", text="Actual")

        tree.column("record", width=80)
        tree.column("offset", width=60)
        tree.column("issue", width=200)
        tree.column("expected", width=120)
        tree.column("actual", width=120)

        tree.pack(fill="both", expand=True, padx=10, pady=10)

        for record_id, offset, issue, expected, actual in mismatches:
            tree.insert(
                "",
                "end",
                values=(record_id, offset, issue, expected or "", actual or ""),
            )

        # Add click-to-filter binding
        def on_select(event):
            selection = tree.selection()
            if not selection:
                return
            item = tree.item(selection[0])
            record_id = item["values"][0]
            # Filter to this record
            self.filter_seeds.clear()
            cur2 = self.dag.conn.cursor()
            row = cur2.execute(
                "SELECT * FROM records WHERE id = ?", (record_id,)
            ).fetchone()
            if row:
                record_bytes = list(row[1:])
                for o, v in enumerate(record_bytes):
                    if v is not None:
                        self.filter_seeds.add((o, v))
                        break
            self._update_filter_bar()
            self.redraw_dag()
            dialog.destroy()

        tree.bind("<<TreeviewSelect>>", on_select)

        # Close button
        self.ttk_module.Button(dialog, text="Close", command=dialog.destroy).pack(
            pady=10
        )

        dialog.grab_set()

    def _show_field_statistics(self):
        """Show field-level statistics in a side panel."""
        if not self.structure or not self.dag.parser:
            self.messagebox.showwarning("No Structure", "Apply a structure first")
            return

        # Get statistics
        cur = self.dag.conn.cursor()
        records = []
        for row in cur.execute("SELECT * FROM records"):
            record_id = row[0]
            record_bytes = list(row[1:])
            records.append((record_id, record_bytes))

        stats = self.dag.parser.get_match_stats(records)

        # Show in dialog
        dialog = self.tk_module.Toplevel(self)
        dialog.title("Field Statistics")
        dialog.geometry("500x400")
        dialog.transient(self)

        # Summary at top
        summary_frame = self.ttk_module.Frame(dialog, padding=10)
        summary_frame.pack(fill="x")

        self.ttk_module.Label(
            summary_frame,
            text=f"Match Rate: {stats['match_rate']:.1f}% ({stats['match_count']}/{stats['total_count']} records)",
            font=("", 11, "bold"),
        ).pack()

        # Field stats table
        columns = ("field", "decoded", "total", "rate")
        tree = self.ttk_module.Treeview(
            dialog, columns=columns, show="headings", height=15
        )
        tree.heading("field", text="Field")
        tree.heading("decoded", text="Decoded")
        tree.heading("total", text="Total")
        tree.heading("rate", text="Rate")

        tree.column("field", width=150)
        tree.column("decoded", width=80)
        tree.column("total", width=80)
        tree.column("rate", width=80)

        tree.pack(fill="both", expand=True, padx=10, pady=10)

        for field_name, field_stats in stats["field_stats"].items():
            decoded = field_stats["decoded"]
            total = field_stats["total"]
            rate = (decoded / total * 100) if total > 0 else 0
            tree.insert("", "end", values=(field_name, decoded, total, f"{rate:.1f}%"))

        # Close button
        self.ttk_module.Button(dialog, text="Close", command=dialog.destroy).pack(
            pady=10
        )

        dialog.grab_set()

    def redraw_dag(self):
        self.canvas.delete("all")
        self.draw_dag()

    def save_canvas_as_svg(self):
        filepath = self.asksaveasfilename(
            defaultextension=".svg",
            filetypes=[("SVG files", "*.svg"), ("All Files", "*.*")],
        )
        if not filepath:
            return
        self._write_svg(filepath)

    def _write_svg(self, filepath: str):
        """Export current DAG view to lossless SVG using LayoutEngine + SVGRenderer."""
        layout = self.layout_engine.compute(
            self.dag,
            self.display_options,
            self.filter_seeds,
            use_aligned=self.use_aligned,
            show_structure=self.structure is not None,
        )
        renderer = SVGRenderer(self.theme, xpad=self.xpad, ypad=self.ypad)
        dag_sz = self.dag.get_aligned_sz() if self.use_aligned else self.dag.sz
        svg_content = renderer.render(
            layout, dag_sz, self.display_options, self.structure_name
        )
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(svg_content)

    def save_canvas_as_ps(self):
        filepath = self.asksaveasfilename(
            defaultextension=".eps",
            filetypes=[("PostScript files", "*.eps"), ("All Files", "*.*")],
        )
        if not filepath:
            return
        self.update()
        self.canvas.postscript(file=filepath, colormode="color")

    def fit_to_canvas(self):
        self.canvas.update_idletasks()
        bbox = self.canvas.bbox("all")
        if not bbox:
            return
        cw = self.canvas.winfo_width()
        ch = self.canvas.winfo_height()
        if cw <= 1 or ch <= 1:
            return
        content_w = bbox[2] - bbox[0]
        content_h = bbox[3] - bbox[1]
        if content_w <= 0 or content_h <= 0:
            return
        margin = 0.9
        scale = min(cw / content_w, ch / content_h) * margin
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        self.canvas.scale("all", cx, cy, scale, scale)
        bbox2 = self.canvas.bbox("all")
        if not bbox2:
            return
        self.canvas.configure(scrollregion=bbox2)
        sw = bbox2[2] - bbox2[0]
        sh = bbox2[3] - bbox2[1]
        center_x = (bbox2[0] + bbox2[2]) / 2
        center_y = (bbox2[1] + bbox2[3]) / 2
        fx = max(0, min(1, (center_x - cw / 2) / sw)) if sw > 0 else 0
        fy = max(0, min(1, (center_y - ch / 2) / sh)) if sh > 0 else 0
        self.canvas.xview_moveto(fx)
        self.canvas.yview_moveto(fy)

    def on_mousewheel(self, event):
        if event.delta > 0 or event.num == 4:
            scale_factor = 1.1
        else:
            scale_factor = 0.9
        x = self.canvas.canvasx(event.x)
        y = self.canvas.canvasy(event.y)
        self.canvas.scale("all", x, y, scale_factor, scale_factor)

    # Pan sensitivity: Tk multiplies scan delta by 10, so we scale coords for 1:1 feel
    PAN_GAIN = 0.1

    def on_button_press(self, event):
        self._pan_start = (event.x, event.y)
        self.canvas.scan_mark(event.x, event.y)

    def pan_canvas(self, event):
        sx, sy = self._pan_start
        # Pass coords so effective delta is (dx, dy) * PAN_GAIN; Tk then *10 → 1:1
        # scan_dragto requires integers
        x = int(sx + (event.x - sx) * self.PAN_GAIN)
        y = int(sy + (event.y - sy) * self.PAN_GAIN)
        self.canvas.scan_dragto(x, y)

    _CLICK_THRESHOLD = 5

    def on_button_release(self, event):
        dx = abs(event.x - self._pan_start[0])
        dy = abs(event.y - self._pan_start[1])
        if dx > self._CLICK_THRESHOLD or dy > self._CLICK_THRESHOLD:
            return  # was a pan, not a click
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        items = self.canvas.find_overlapping(cx, cy, cx, cy)
        for iid in reversed(items):
            tags = self.canvas.gettags(iid)
            for t in tags:
                if t.startswith("node_") and t != "node":
                    parts = t.split("_")
                    if len(parts) == 3:
                        try:
                            offset, val = int(parts[1]), int(parts[2])
                            # Always add clicked node to filter (Ctrl or not); use Clear to reset
                            self.filter_seeds.add((offset, val))
                            self._update_filter_bar()
                            self.redraw_dag()
                        except ValueError:
                            pass
                    return

    def on_right_click(self, event):
        """Handle right-click context menu on canvas nodes."""
        cx = self.canvas.canvasx(event.x)
        cy = self.canvas.canvasy(event.y)
        items = self.canvas.find_overlapping(cx, cy, cx, cy)

        clicked_node = None
        for iid in reversed(items):
            tags = self.canvas.gettags(iid)
            for t in tags:
                if t.startswith("node_") and t != "node":
                    parts = t.split("_")
                    if len(parts) == 3:
                        try:
                            offset, val = int(parts[1]), int(parts[2])
                            clicked_node = (offset, val)
                        except ValueError:
                            pass
                    break
            if clicked_node:
                break

        if not clicked_node:
            return

        offset, val = clicked_node

        # Build context menu
        menu = self.tk_module.Menu(self, tearoff=0)

        # Structure-related actions
        menu.add_command(
            label=f"Create structure at offset {offset}",
            command=lambda: self._create_structure_at_offset(offset, val),
        )

        if self.structure:
            menu.add_command(
                label=f"Add field to '{self.structure.name}' at offset {offset}",
                command=lambda: self._add_field_at_offset(offset, val),
            )

        # Mark as padding
        menu.add_separator()
        menu.add_command(
            label=f"Mark offset {offset} as padding",
            command=lambda: self._mark_as_padding(offset),
        )

        # Set as magic value
        menu.add_command(
            label=f"Set as magic value 0x{val:02X}",
            command=lambda: self._set_as_magic(offset, val),
        )

        # Create enum from values
        menu.add_separator()
        menu.add_command(
            label=f"Create enum from values at offset {offset}",
            command=lambda: self._create_enum_at_offset(offset),
        )

        # Decode as submenu
        menu.add_separator()
        decode_menu = self.tk_module.Menu(menu, tearoff=0)
        for dtype in ["u8", "u16be", "u32be", "ascii(4)"]:
            decode_menu.add_command(
                label=f"Decode as {dtype}",
                command=lambda o=offset, v=val, t=dtype: self._quick_decode(o, v, t),
            )
        menu.add_cascade(label="Decode as", menu=decode_menu)

        # Show menu at cursor position
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _create_structure_at_offset(self, offset: int, val: int):
        """Create a new structure starting at the given offset."""
        structure = Structure(name=f"struct_at_{offset}")
        structure.fields.append(
            Field(name=f"field_{offset}", type="u8", offset=offset, size=1)
        )
        StructureEditorDialog(
            self,
            structure=structure,
            dag_sz=self.dag.sz,
            on_apply=self._on_structure_applied,
        )

    def _add_field_at_offset(self, offset: int, val: int):
        """Add a field to the current structure at the given offset."""
        if not self.structure:
            return

        # Check if field already exists at this offset
        for f in self.structure.fields:
            if f.offset == offset:
                self.messagebox.showwarning(
                    "Warning", f"Field already exists at offset {offset}"
                )
                return

        new_field = Field(name=f"field_{offset}", type="u8", offset=offset, size=1)
        self.structure.fields.append(new_field)

        # Re-sort fields by offset
        self.structure.fields.sort(key=lambda f: f.offset)

        # Re-validate and update
        errors = validate_structure(self.structure, self.dag.sz)
        if errors:
            self.messagebox.showwarning("Validation", "\n".join(errors))

        # Rebuild aligned table
        self.dag.build_aligned_table(self.structure)
        self.redraw_dag()

    def _mark_as_padding(self, offset: int):
        """Mark an offset as a padding field."""
        if not self.structure:
            # Create new structure with pad field
            structure = Structure(name=f"pad_struct")
            structure.fields.append(
                Field(name="pad", type="pad", offset=offset, align_to=4)
            )
            StructureEditorDialog(
                self,
                structure=structure,
                dag_sz=self.dag.sz,
                on_apply=self._on_structure_applied,
            )
        else:
            # Add pad field to existing structure
            self.structure.fields.append(
                Field(name="pad", type="pad", offset=offset, align_to=4)
            )
            self.structure.fields.sort(key=lambda f: f.offset)
            self.dag.build_aligned_table(self.structure)
            self.redraw_dag()

    def _set_as_magic(self, offset: int, val: int):
        """Set a byte value as a magic field."""
        if not self.structure:
            structure = Structure(name=f"magic_struct")
            structure.fields.append(
                Field(
                    name="magic", type="magic", offset=offset, magic_value=bytes([val])
                )
            )
            StructureEditorDialog(
                self,
                structure=structure,
                dag_sz=self.dag.sz,
                on_apply=self._on_structure_applied,
            )
        else:
            self.structure.fields.append(
                Field(
                    name="magic", type="magic", offset=offset, magic_value=bytes([val])
                )
            )
            self.structure.fields.sort(key=lambda f: f.offset)
            self.dag.build_aligned_table(self.structure)
            self.redraw_dag()

    def _create_enum_at_offset(self, offset: int):
        """Create an enum from distinct values at an offset."""
        # Get distinct values at this offset
        val_counts = self.dag.get_val_counts_by_offset(
            offset, use_aligned=self.use_aligned
        )

        if not val_counts:
            self.messagebox.showwarning("No Data", f"No values at offset {offset}")
            return

        # Build enum mapping from most common values
        enum_map = {}
        for i, (val, count) in enumerate(val_counts[:10]):  # Top 10 values
            if val is not None:
                enum_map[str(val)] = f"value_{i}"

        # Create structure with enum field
        structure = Structure(name=f"enum_at_{offset}")
        structure.fields.append(
            Field(
                name=f"enum_{offset}",
                type="enum:u8",
                offset=offset,
                size=1,
                enum=enum_map,
            )
        )

        StructureEditorDialog(
            self,
            structure=structure,
            dag_sz=self.dag.sz,
            on_apply=self._on_structure_applied,
        )

    def _quick_decode(self, offset: int, val: int, dtype: str):
        """Show quick decode tooltip for a value."""
        import struct

        decoded = None
        try:
            if dtype == "u8":
                decoded = val
            elif dtype == "u16be":
                # Need next byte - get it from a record
                cur = self.dag.conn.cursor()
                row = cur.execute(
                    f"SELECT off_{offset}, off_{offset + 1} FROM records WHERE off_{offset} = ? LIMIT 1",
                    (val,),
                ).fetchone()
                if row and row[1] is not None:
                    decoded = struct.unpack(">H", bytes([val, row[1]]))[0]
            elif dtype == "u32be":
                cur = self.dag.conn.cursor()
                row = cur.execute(
                    f"SELECT off_{offset}, off_{offset + 1}, off_{offset + 2}, off_{offset + 3} FROM records WHERE off_{offset} = ? LIMIT 1",
                    (val,),
                ).fetchone()
                if row and all(b is not None for b in row[1:]):
                    decoded = struct.unpack(">I", bytes([val] + list(row[1:])))[0]
            elif dtype.startswith("ascii"):
                size = int(dtype[dtype.find("(") + 1 : dtype.find(")")])
                cur = self.dag.conn.cursor()
                cols = ", ".join(f"off_{offset + i}" for i in range(size))
                row = cur.execute(
                    f"SELECT {cols} FROM records WHERE off_{offset} = ? LIMIT 1", (val,)
                ).fetchone()
                if row and all(b is not None and 0x20 <= b <= 0x7E for b in row[1:]):
                    decoded = "".join(chr(b) for b in row if b is not None)
        except Exception:
            pass

        if decoded is not None:
            self.messagebox.showinfo(
                "Decoded Value", f"Offset {offset}, type {dtype}:\n{decoded}"
            )
        else:
            self.messagebox.showinfo(
                "Decoded Value",
                f"Offset {offset}, type {dtype}:\nCannot decode (need more bytes)",
            )

    def create_round_rectangle(self, x1, y1, x2, y2, r=25, tags=(), **kwargs):
        """Helper for CanvasRenderer - creates rounded rectangle on canvas."""
        if "tags" in kwargs:
            tags = kwargs.pop("tags")
        points = (
            x1 + r,
            y1,
            x1 + r,
            y1,
            x2 - r,
            y1,
            x2 - r,
            y1,
            x2,
            y1,
            x2,
            y1 + r,
            x2,
            y1 + r,
            x2,
            y2 - r,
            x2,
            y2 - r,
            x2,
            y2,
            x2 - r,
            y2,
            x2 - r,
            y2,
            x1 + r,
            y2,
            x1 + r,
            y2,
            x1,
            y2,
            x1,
            y2 - r,
            x1,
            y2 - r,
            x1,
            y1 + r,
            x1,
            y1 + r,
            x1,
            y1,
        )
        return self.canvas.create_polygon(points, tags=tags, **kwargs, smooth=True)

    def draw_dag(self):
        """Draw DAG using LayoutEngine + CanvasRenderer."""
        layout = self.layout_engine.compute(
            self.dag,
            self.display_options,
            self.filter_seeds,
            use_aligned=self.use_aligned,
            show_structure=self.structure is not None,
        )
        self.canvas_renderer.render(layout, self.display_options, self.filter_seeds)


def main():
    parser = argparse.ArgumentParser(
        description="DAGUIRE - Interactive DAG viewer for binary reverse engineering"
    )
    parser.add_argument(
        "fmt", nargs="?", help="input format data [hex, file]", default="hex"
    )
    parser.add_argument("sz", nargs="?", help="size of DAG [8]", default=8, type=int)
    parser.add_argument(
        "--svg", metavar="PATH", help="export SVG to file and exit (no GUI)"
    )
    parser.add_argument(
        "--structure", metavar="NAME", help="apply saved structure by name"
    )
    parser.add_argument(
        "--list-structures",
        action="store_true",
        help="list saved structures and exit (Phase 1+)",
    )
    parser.add_argument(
        "--decimal",
        action="store_true",
        default=True,
        help="show decimal in labels (default: on)",
    )
    parser.add_argument(
        "--no-decimal", action="store_true", help="hide decimal in labels"
    )
    parser.add_argument(
        "--hex",
        action="store_true",
        default=True,
        help="show hex in labels (default: on)",
    )
    parser.add_argument("--no-hex", action="store_true", help="hide hex in labels")
    parser.add_argument(
        "--binary",
        action="store_true",
        default=True,
        help="show binary in labels (default: on)",
    )
    parser.add_argument(
        "--no-binary", action="store_true", help="hide binary in labels"
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        default=True,
        help="show ASCII in labels (default: on)",
    )
    parser.add_argument("--no-ascii", action="store_true", help="hide ASCII in labels")
    parser.add_argument(
        "--filter-match",
        action="store_true",
        help="filter to only records matching the structure (Phase 7)",
    )

    args = parser.parse_args()

    # Handle --list-structures
    if args.list_structures:
        store = StructureStore()
        structures = store.list_structures()
        if structures:
            print("Saved structures:")
            for name in structures:
                print(f"  - {name}")
        else:
            print("No saved structures found.")
        sys.exit(0)

    # Load structure if specified
    structure = None
    if args.structure:
        store = StructureStore()
        structure = store.load(args.structure)
        if structure is None:
            print(f"Error: structure '{args.structure}' not found.", file=sys.stderr)
            sys.exit(1)
        # Validate structure against DAG size
        errors = validate_structure(structure, args.sz)
        if errors:
            print(
                f"Error: structure '{args.structure}' has validation errors:",
                file=sys.stderr,
            )
            for err in errors:
                print(f"  - {err}", file=sys.stderr)
            sys.exit(1)

    # Build display options
    display_options = {
        "decimal": not args.no_decimal,
        "hex": not args.no_hex,
        "binary": not args.no_binary,
        "ascii": not args.no_ascii,
    }

    if args.sz > 1999:
        print(f"Size limit 1999 exceeded.", file=sys.stderr)
        sys.exit(1)

    with sqlite3.connect(":memory:") as conn:
        d = Dag(conn, fmt=args.fmt, sz=args.sz)

        # Handle --svg (headless export)
        if args.svg:
            # Build aligned table if structure is provided
            if structure:
                d.build_aligned_table(structure)

            layout_engine = LayoutEngine()
            use_aligned = structure is not None
            layout = layout_engine.compute(
                d,
                display_options,
                filter_seeds=None,
                use_aligned=use_aligned,
                show_structure=structure is not None,
            )
            theme = CanvasApp.THEME
            renderer = SVGRenderer(theme)
            dag_sz = d.get_aligned_sz() if use_aligned else d.sz
            structure_name = structure.name if structure else None
            svg_content = renderer.render(
                layout, dag_sz, display_options, structure_name
            )
            with open(args.svg, "w", encoding="utf-8") as f:
                f.write(svg_content)
            print(f"SVG exported to {args.svg}")
            sys.exit(0)

        # GUI mode
        app = CanvasApp(d, structure=structure)
        app.mainloop()


if __name__ == "__main__":
    main()
