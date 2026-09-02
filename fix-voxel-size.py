#!/usr/bin/env python3
"""
fix-voxel-size.py

Correct a mis-recorded voxel size across every file of a 3D Slicer scene.

When a volume is imported with the wrong voxel size the error propagates:
the source .nrrd, every .seg.nrrd, every exported model, the markups files
and the .mrml scene all end up describing the specimen at the wrong physical
scale.  Editing the .nrrd header by hand fixes the volume but leaves the
models and markups behind, so the scene no longer lines up.

This script fixes all of them at once.  It reads the *current* (wrong) voxel
size from the scene's source volume, works out the scale factor

    s = correct voxel size / current voxel size

and then applies a uniform scale of s about the world origin to every
physical (mm) quantity it can find:

    .nrrd / .seg.nrrd   space directions, space origin, spacings, and the
                        "Reference image geometry" matrix embedded in a
                        segmentation's Segmentation_ConversionParameters
    .ply / .stl / .obj  vertex coordinates (normals are unit vectors and are
                        left alone)
    .mrk.json           control point positions, ROI/plane geometry,
                        object-to-world matrices, and measurement values
    *.metrics.json      values whose key names their unit (mm, mm^3, um, ...)
    .mrml               volume spacing/origin, camera, slice and crosshair
                        geometry, and markup interaction handles

Voxel counts, extents, direction cosines and surface normals are unaffected
by a uniform scale and are left untouched.

Idempotence
    The scale factor always comes from the reference volume's .nrrd header,
    so once that file is correct a second run computes s = 1 and does
    nothing.  The reference volume is therefore written *last*: if a run is
    interrupted, re-running it picks up where it left off.

Safety
    Nothing is written unless --apply is given; the default is a dry run
    that prints exactly what would change.  Large binary files (.nrrd data,
    .ply meshes) are edited in place rather than copied, so an interrupted
    write would leave a half-scaled file.  While such a file is open a
    ".fixvoxel-inprogress" marker sits beside it, and the script refuses to
    touch a file whose marker is still present.  Use --backup to keep a copy
    of every file before it is modified (needs as much free space again).

Usage
    python fix-voxel-size.py SCENE.mrml --voxel-size 0.05272172
    python fix-voxel-size.py SCENE.mrml --voxel-size 52.72172 --units um --apply

Dependencies: numpy (only for binary mesh files).
"""

import argparse
import json
import os
import re
import shutil
import sys
from urllib.parse import unquote, urlparse

# Meshes are scaled with numpy; everything else is plain text handling.
try:
    import numpy as np
except ImportError:
    np = None

NRRD_HEADER_MAX = 8 << 20        # a NRRD header will never be this big
NRRD_PAD_MARKER = "# voxel size corrected by fix-voxel-size.py"
MESH_CHUNK = 1 << 20             # vertices per read/modify/write pass
SPACING_TOL = 1e-6               # relative tolerance for "same spacing"

MODEL_EXTS = (".ply", ".stl", ".obj")
VOLUME_EXTS = (".nrrd", ".nhdr")
UNIT_SCALE_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "um": 1e-3, "nm": 1e-6}


class FixError(Exception):
    """A problem that should abort work on one file, with an explanation."""


# --------------------------------------------------------------------------
# number formatting
# --------------------------------------------------------------------------

FMT_REL_TOL = 1e-12


def fmt_num(x):
    """
    Shortest decimal for x, to within FMT_REL_TOL.

    Plain repr() of a scaled value tends to trail floating-point noise
    (0.05272172 becomes "0.052721720000000005"), which bloats NRRD headers
    past the point where they can be patched in place and makes the files
    unpleasant to read.  A relative tolerance of 1e-12 is a fraction of a
    nanometre on a specimen-sized coordinate.
    """
    x = float(x)
    if x == 0.0:
        return "0"
    for precision in range(1, 18):
        s = f"{x:.{precision}g}"
        if abs(float(s) - x) <= FMT_REL_TOL * abs(x):
            return s
    return repr(x)


def scale_token(token, s):
    """Scale one numeric token, leaving anything unparseable alone."""
    try:
        return fmt_num(float(token) * s)
    except ValueError:
        return token


def scale_number_list(text, s, indices=None):
    """
    Scale the numbers in a whitespace-separated list.

    indices, if given, selects which positions to scale; the rest are copied
    through verbatim so the file diff stays small.
    """
    parts = text.split()
    out = []
    for i, part in enumerate(parts):
        out.append(scale_token(part, s) if indices is None or i in indices
                   else part)
    return " ".join(out)


# Row-major 4x4: only the translation column is a length.
MATRIX4_TRANSLATION = frozenset((3, 7, 11))


def scale_matrix4(text, s):
    """Scale the translation column of a row-major 4x4 matrix."""
    parts = text.split()
    if len(parts) != 16:
        raise FixError(f"expected 16 numbers in a 4x4 matrix, got {len(parts)}")
    return scale_number_list(text, s, MATRIX4_TRANSLATION)


# --------------------------------------------------------------------------
# NRRD
# --------------------------------------------------------------------------

def read_nrrd_header(path):
    """
    Return (header_text, region_len, newline).

    region_len is the byte offset at which the data begins, i.e. the length
    of the header region including its terminating blank line.
    """
    with open(path, "rb") as f:
        chunk = f.read(NRRD_HEADER_MAX)
    if not chunk.startswith(b"NRRD"):
        raise FixError("not a NRRD file (bad magic)")
    found = None
    for sep in (b"\n\n", b"\r\n\r\n"):
        i = chunk.find(sep)
        if i != -1 and (found is None or i < found[0]):
            found = (i, sep)
    if found is None:
        raise FixError("no blank line terminating the NRRD header "
                       "(detached .nhdr headers are not supported)")
    i, sep = found
    newline = "\r\n" if sep == b"\r\n\r\n" else "\n"
    return chunk[:i].decode("utf-8"), i + len(sep), newline


def nrrd_fields(header_text):
    """Map field name -> value for the plain 'key: value' header fields."""
    fields = {}
    for line in header_text.splitlines():
        if line.startswith("#") or ":=" in line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


_VECTOR_RE = re.compile(r"\(([^)]*)\)")


def scale_vector_field(value, s):
    """Scale each (a,b,c) vector in a NRRD field, leaving 'none' entries."""
    def one(m):
        comps = [scale_token(t.strip(), s) for t in m.group(1).split(",")]
        return "(" + ",".join(comps) + ")"
    return _VECTOR_RE.sub(one, value)


def nrrd_spacings(header_text):
    """Per-axis spacing in mm, from 'space directions' or 'spacings'."""
    fields = nrrd_fields(header_text)
    if "space directions" in fields:
        out = []
        for m in _VECTOR_RE.finditer(fields["space directions"]):
            comps = [float(t) for t in m.group(1).split(",")]
            out.append(sum(c * c for c in comps) ** 0.5)
        if out:
            return out
    if "spacings" in fields:
        out = []
        for tok in fields["spacings"].split():
            try:
                out.append(float(tok))
            except ValueError:
                pass          # 'nan' marks a non-spatial axis
        if out:
            return out
    raise FixError("header has neither 'space directions' nor 'spacings'")


def scale_reference_image_geometry(value, s):
    """
    Scale the "Reference image geometry" entry of a segmentation's
    Segmentation_ConversionParameters field.

    Its value is a ';'-separated row-major 4x4 IJK-to-RAS matrix followed by
    six voxel extents.  The first three rows (twelve numbers: three direction
    components and one translation each) are lengths; the homogeneous row and
    the extents are not.
    """
    parts = value.split("&")
    for i, part in enumerate(parts):
        bits = part.split("|")
        if len(bits) < 2 or bits[0] != "Reference image geometry":
            continue
        nums = bits[1].split(";")
        for j in range(min(12, len(nums))):
            nums[j] = scale_token(nums[j], s)
        bits[1] = ";".join(nums)
        parts[i] = "|".join(bits)
    return "&".join(parts)


def plan_nrrd(path, s):
    """Return (lines, region_len, newline, description), or None if no change."""
    header_text, region_len, newline = read_nrrd_header(path)
    lines = [ln for ln in header_text.splitlines()
             if not ln.startswith(NRRD_PAD_MARKER)]

    changes = []
    for i, line in enumerate(lines):
        if line.startswith("#"):
            continue
        if ":=" in line:
            key, _, value = line.partition(":=")
            if key.strip() == "Segmentation_ConversionParameters":
                new_value = scale_reference_image_geometry(value, s)
                if new_value != value:
                    lines[i] = f"{key}:={new_value}"
                    changes.append("Reference image geometry")
            continue
        key, marker, value = line.partition(":")
        if not marker:
            continue
        name, value = key.strip(), value.strip()
        if name in ("space directions", "space origin"):
            new_value = scale_vector_field(value, s)
        elif name == "spacings":
            new_value = scale_number_list(value, s)
        else:
            continue
        if new_value != value:
            lines[i] = f"{name}: {new_value}"
            changes.append(name)

    if not changes:
        return None
    return lines, region_len, newline, ", ".join(changes)


def write_nrrd_header(path, lines, region_len, newline, backup):
    """
    Replace a NRRD header in place when the new one can be padded out to the
    original byte length, otherwise rewrite the whole file.

    In place is much preferred: these volumes routinely run to several
    gigabytes and the data block is not being changed.
    """
    sep = (newline + newline).encode("utf-8")
    base = newline.join(lines + [NRRD_PAD_MARKER]).encode("utf-8")
    slack = region_len - (len(base) + len(sep))

    if backup:
        make_backup(path)

    if slack >= 0:
        region = base + b" " * slack + sep
        with guard(path):
            with open(path, "r+b") as f:
                f.write(region)
        return "header rewritten in place"

    # The new header is longer than the old one, so the data has to move.
    tmp = path + ".fixvoxel.tmp"
    with guard(path):
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(base + sep)
            src.seek(region_len)
            shutil.copyfileobj(src, dst, 1 << 22)
        os.replace(tmp, path)
    return "whole file rewritten (header grew)"


# --------------------------------------------------------------------------
# meshes: PLY, STL, OBJ
# --------------------------------------------------------------------------

PLY_TYPES = {
    "char": "i1", "int8": "i1", "uchar": "u1", "uint8": "u1",
    "short": "i2", "int16": "i2", "ushort": "u2", "uint16": "u2",
    "int": "i4", "int32": "i4", "uint": "u4", "uint32": "u4",
    "float": "f4", "float32": "f4", "double": "f8", "float64": "f8",
}


def parse_ply_header(path):
    """Return (format, elements, data_offset); elements is a list of dicts."""
    with open(path, "rb") as f:
        if f.readline().strip() != b"ply":
            raise FixError("not a PLY file")
        fmt, elements = None, []
        while True:
            raw = f.readline()
            if not raw:
                raise FixError("PLY header has no end_header")
            line = raw.decode("ascii", "replace").strip()
            if line == "end_header":
                break
            parts = line.split()
            if not parts:
                continue
            if parts[0] == "format":
                fmt = parts[1]
            elif parts[0] == "element":
                elements.append({"name": parts[1], "count": int(parts[2]),
                                 "props": []})
            elif parts[0] == "property" and elements:
                if parts[1] == "list":
                    elements[-1]["props"].append({"list": True})
                else:
                    elements[-1]["props"].append(
                        {"list": False, "type": parts[1], "name": parts[2]})
        return fmt, elements, f.tell()


def ply_element_dtype(element, endian):
    """numpy dtype for a fixed-size PLY element, or None if it has lists."""
    fields = []
    for i, prop in enumerate(element["props"]):
        if prop["list"]:
            return None
        code = PLY_TYPES.get(prop["type"])
        if code is None:
            raise FixError(f"unsupported PLY property type '{prop['type']}'")
        fields.append((prop["name"] or f"p{i}", endian + code))
    return np.dtype(fields)


def ply_vertex_layout(path):
    """Return (data_offset_of_vertices, count, dtype) for a binary PLY."""
    fmt, elements, data_offset = parse_ply_header(path)
    if fmt not in ("binary_little_endian", "binary_big_endian"):
        raise FixError(f"unsupported PLY format '{fmt}'")
    require_numpy()
    endian = "<" if fmt == "binary_little_endian" else ">"

    offset = data_offset
    for element in elements:
        dtype = ply_element_dtype(element, endian)
        if element["name"] == "vertex":
            if dtype is None:
                raise FixError("vertex element has a list property")
            for axis in ("x", "y", "z"):
                if axis not in (dtype.names or ()):
                    raise FixError(f"vertex element has no '{axis}' property")
            return offset, element["count"], dtype
        if dtype is None:
            raise FixError("cannot skip past variable-size element "
                           f"'{element['name']}' to reach the vertices")
        offset += element["count"] * dtype.itemsize
    raise FixError("no vertex element in PLY")


def scale_ply(path, s, backup):
    fmt, elements, data_offset = parse_ply_header(path)
    if fmt == "ascii":
        return scale_ply_ascii(path, s, elements, data_offset, backup)

    offset, count, dtype = ply_vertex_layout(path)
    if backup:
        make_backup(path)
    with guard(path):
        with open(path, "r+b") as f:
            for start in range(0, count, MESH_CHUNK):
                n = min(MESH_CHUNK, count - start)
                pos = offset + start * dtype.itemsize
                f.seek(pos)
                buf = bytearray(f.read(n * dtype.itemsize))
                arr = np.frombuffer(buf, dtype=dtype, count=n)
                for axis in ("x", "y", "z"):
                    arr[axis] = arr[axis] * s
                f.seek(pos)
                f.write(buf)
    return f"{count} vertices scaled in place"


def scale_ply_ascii(path, s, elements, data_offset, backup):
    if not elements or elements[0]["name"] != "vertex":
        raise FixError("ascii PLY support requires vertices to come first")
    count = elements[0]["count"]
    if backup:
        make_backup(path)
    tmp = path + ".fixvoxel.tmp"
    with guard(path):
        with open(path, "rb") as src, open(tmp, "wb") as dst:
            dst.write(src.read(data_offset))
            for _ in range(count):
                parts = src.readline().decode("ascii").split()
                parts[:3] = [scale_token(t, s) for t in parts[:3]]
                dst.write((" ".join(parts) + "\n").encode("ascii"))
            shutil.copyfileobj(src, dst, 1 << 22)
        os.replace(tmp, path)
    return f"{count} vertices scaled"


def stl_triangle_count(path):
    """Triangle count if this is a binary STL, else None."""
    size = os.path.getsize(path)
    if size < 84:
        return None
    with open(path, "rb") as f:
        f.seek(80)
        count = int.from_bytes(f.read(4), "little")
    return count if size == 84 + 50 * count else None


def scale_stl(path, s, backup):
    count = stl_triangle_count(path)
    if count is None:
        return scale_ascii_lines(path, s, backup, prefixes=("vertex",))
    require_numpy()
    dtype = np.dtype([("normal", "<3f4"), ("verts", "<9f4"), ("attr", "<u2")])
    if backup:
        make_backup(path)
    with guard(path):
        with open(path, "r+b") as f:
            for start in range(0, count, MESH_CHUNK):
                n = min(MESH_CHUNK, count - start)
                pos = 84 + start * dtype.itemsize
                f.seek(pos)
                buf = bytearray(f.read(n * dtype.itemsize))
                arr = np.frombuffer(buf, dtype=dtype, count=n)
                arr["verts"] = arr["verts"] * s
                f.seek(pos)
                f.write(buf)
    return f"{count} triangles scaled in place"


def scale_ascii_lines(path, s, backup, prefixes):
    """Scale the three numbers following a keyword on matching text lines."""
    if backup:
        make_backup(path)
    tmp = path + ".fixvoxel.tmp"
    touched = 0
    with guard(path):
        with open(path, "r", encoding="utf-8", errors="surrogateescape") as src, \
             open(tmp, "w", encoding="utf-8", errors="surrogateescape",
                  newline="\n") as dst:
            for line in src:
                parts = line.split()
                if parts and parts[0] in prefixes and len(parts) >= 4:
                    parts[1:4] = [scale_token(t, s) for t in parts[1:4]]
                    line = " ".join(parts) + "\n"
                    touched += 1
                dst.write(line)
        os.replace(tmp, path)
    return f"{touched} vertex lines scaled"


def scale_obj(path, s, backup):
    return scale_ascii_lines(path, s, backup, prefixes=("v",))


def require_numpy():
    if np is None:
        raise FixError("numpy is required to scale binary mesh files")


# --------------------------------------------------------------------------
# JSON: markups and metrics sidecars
# --------------------------------------------------------------------------

# Markups keys holding lengths, and those holding a row-major 4x4 matrix.
MARKUP_LENGTH_KEYS = frozenset((
    "position", "center", "size", "planeBounds", "planeSize", "roiSize",
    "controlPointPositions",
))
MARKUP_MATRIX_KEYS = frozenset((
    "objectToBase", "baseToNode", "objectToNode",
    "interactionHandleToWorldMatrix",
))
# Display settings mix relative and absolute sizes; leave them alone.
MARKUP_SKIP_KEYS = frozenset(("display",))

_UNIT_PATTERNS = (
    (re.compile(r"(?:mm|cm|um|µm|nm|m)\s*\^?\s*3\b", re.I), 3),
    (re.compile(r"(?:mm|cm|um|µm|nm|m)\s*\^?\s*2\b", re.I), 2),
    (re.compile(r"\bin\s+(?:mm|cm|um|µm|nm|m)\b", re.I), 1),
    (re.compile(r"^(?:mm|cm|um|µm|nm|m)$", re.I), 1),
)


def unit_exponent(text):
    """Dimension implied by a unit string or a key name, or None."""
    for pattern, exponent in _UNIT_PATTERNS:
        if pattern.search(text):
            return exponent
    return None


def is_number_list(value):
    return (isinstance(value, list) and value
            and all(isinstance(v, (int, float)) and not isinstance(v, bool)
                    for v in value))


def scale_markups_tree(node, s, stats):
    """Scale lengths in a parsed .mrk.json document, in place."""
    if isinstance(node, list):
        for item in node:
            scale_markups_tree(item, s, stats)
        return
    if not isinstance(node, dict):
        return

    for key, value in node.items():
        if key in MARKUP_SKIP_KEYS:
            continue
        if key in MARKUP_LENGTH_KEYS and is_number_list(value):
            node[key] = [v * s for v in value]
            stats[key] = stats.get(key, 0) + 1
        elif (key in MARKUP_MATRIX_KEYS and is_number_list(value)
              and len(value) == 16):
            node[key] = [v * s if i in MATRIX4_TRANSLATION else v
                         for i, v in enumerate(value)]
            stats[key] = stats.get(key, 0) + 1
        elif key == "measurements" and isinstance(value, list):
            for measurement in value:
                scale_measurement(measurement, s, stats)
        else:
            scale_markups_tree(value, s, stats)


def scale_measurement(measurement, s, stats):
    if not isinstance(measurement, dict):
        return
    exponent = unit_exponent(str(measurement.get("units", "")))
    value = measurement.get("value")
    if exponent and isinstance(value, (int, float)) and not isinstance(value, bool):
        measurement["value"] = value * (s ** exponent)
        stats["measurements"] = stats.get("measurements", 0) + 1


def summarise(stats):
    return ", ".join(f"{k} x{v}" for k, v in sorted(stats.items())) or None


def scale_markups_json(path, s, backup):
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    stats = {}
    scale_markups_tree(doc, s, stats)
    if not stats:
        return None
    if backup:
        make_backup(path)
    write_json(path, doc)
    return summarise(stats)


def metrics_changes(doc, s):
    """
    Work out the rescaled values of a *.metrics.json sidecar.

    Keys name their own units, e.g. "Voxel size in um" or "Endocast volume in
    mm^3", so the exponent comes from the key.  Numeric values whose key names
    no unit are left alone.
    """
    changed, skipped = {}, []
    for key, value in doc.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        exponent = unit_exponent(key)
        if exponent is None:
            skipped.append(key)
        else:
            changed[key] = (value, value * (s ** exponent), exponent)
    return changed, skipped


def scale_metrics_json(path, s, backup):
    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if not isinstance(doc, dict):
        raise FixError("metrics file is not a JSON object")
    changed, skipped = metrics_changes(doc, s)
    if not changed:
        return None
    for key, (_, new_value, _) in changed.items():
        doc[key] = new_value
    if backup:
        make_backup(path)
    write_json(path, doc)
    note = "; ".join(f"{k} (^{e})" for k, (_, _, e) in changed.items())
    if skipped:
        note += f"  [left alone: {', '.join(skipped)}]"
    return note


def write_json(path, doc):
    tmp = path + ".fixvoxel.tmp"
    with guard(path):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=4)
            f.write("\n")
        os.replace(tmp, path)


# --------------------------------------------------------------------------
# MRML
# --------------------------------------------------------------------------

# tag -> {attribute: kind}, where kind is 'lengths' (scale every number) or
# 'matrix4' (scale the translation column only).
#
# Every mm quantity in the scene is scaled, slice and slab settings included.
# Display sizes such as glyphScale and textScale are deliberately left alone:
# they may be relative rather than absolute, depending on sibling attributes.
MRML_RULES = {
    "Volume": {"spacing": "lengths", "origin": "lengths"},
    "ScalarVolume": {"spacing": "lengths", "origin": "lengths"},
    "VectorVolume": {"spacing": "lengths", "origin": "lengths"},
    "LabelMapVolume": {"spacing": "lengths", "origin": "lengths"},
    "SequenceVolume": {"spacing": "lengths", "origin": "lengths"},
    "DiffusionWeightedVolume": {"spacing": "lengths", "origin": "lengths"},
    "DiffusionTensorVolume": {"spacing": "lengths", "origin": "lengths"},
    "Camera": {"position": "lengths", "focalPoint": "lengths",
               "parallelScale": "lengths", "appliedTransform": "matrix4"},
    "View": {"fieldOfView": "lengths"},
    "Slice": {"fieldOfView": "lengths", "uvwExtents": "lengths",
              "xyzOrigin": "lengths", "uvwOrigin": "lengths",
              "sliceToRAS": "matrix4",
              "prescribedSliceSpacing": "lengths",
              "slabReconstructionThickness": "lengths"},
    "Crosshair": {"crosshairRAS": "lengths"},
    "LinearTransform": {"matrixTransformToParent": "matrix4"},
}
MARKUP_TAG_RULES = {"interactionHandleToWorldMatrix": "matrix4"}

_ATTR_RE = re.compile(r'([A-Za-z_][\w:.\-]*)\s*=\s*"([^"]*)"')


def iter_start_tags(text):
    """
    Yield (tag, {attr: (value, start, end)}) for every start tag in an XML
    document, start/end being offsets of the attribute's value in text.

    ElementTree would lose the scene file's original formatting, so edits are
    spliced into the original text instead.
    """
    i, n = 0, len(text)
    while True:
        i = text.find("<", i)
        if i < 0:
            return
        j = i + 1
        if j >= n or text[j] in "?!/":
            i = j
            continue
        m = re.match(r"[A-Za-z_][\w:.\-]*", text[j:])
        if not m:
            i = j
            continue
        tag = m.group(0)
        k = j + len(tag)
        p, in_quote = k, False
        while p < n:
            c = text[p]
            if c == '"':
                in_quote = not in_quote
            elif c == ">" and not in_quote:
                break
            p += 1
        body = text[k:p]
        attrs = {am.group(1): (am.group(2), k + am.start(2), k + am.end(2))
                 for am in _ATTR_RE.finditer(body)}
        yield tag, attrs
        i = p + 1


def rules_for_tag(tag):
    if tag in MRML_RULES:
        return MRML_RULES[tag]
    if tag.startswith("Markups") and not tag.endswith("Display"):
        return MARKUP_TAG_RULES
    return None


def mrml_edits(text, s):
    """Return ([(start, end, new_value)], {label: count}) for a scene file."""
    edits, notes = [], {}
    for tag, attrs in iter_start_tags(text):
        rules = rules_for_tag(tag)
        if not rules:
            continue
        for attr, kind in rules.items():
            if attr not in attrs:
                continue
            value, start, end = attrs[attr]
            try:
                new_value = (scale_matrix4(value, s) if kind == "matrix4"
                             else scale_number_list(value, s))
            except FixError as exc:
                warn(f"{tag}/{attr}: {exc}")
                continue
            if new_value != value:
                edits.append((start, end, new_value))
                label = f"{tag}/{attr}"
                notes[label] = notes.get(label, 0) + 1
    return edits, notes


def scale_mrml(path, s, backup):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    edits, notes = mrml_edits(text, s)
    if not edits:
        return None
    for start, end, new_value in sorted(edits, reverse=True):
        text = text[:start] + new_value + text[end:]

    if backup:
        make_backup(path)
    tmp = path + ".fixvoxel.tmp"
    with guard(path):
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    return summarise(notes)


def mrml_referenced_files(path):
    """Absolute paths of the data files a scene's storage nodes point at."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    base = os.path.dirname(os.path.abspath(path))
    found = []
    for tag, attrs in iter_start_tags(text):
        if "Storage" not in tag:
            continue
        for attr, (value, _, _) in attrs.items():
            if attr != "fileName" and not attr.startswith("fileListMember"):
                continue
            name = unquote(value)
            if urlparse(name).scheme in ("http", "https"):
                continue
            found.append(os.path.normpath(os.path.join(base, name)))
    return found


def mrml_reference_volume(path):
    """
    The scene's source volume file: the storage node referenced by the first
    Volume node, which is what the rest of the scene is registered against.
    """
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    base = os.path.dirname(os.path.abspath(path))

    storage_files, wanted = {}, None
    for tag, attrs in iter_start_tags(text):
        node_id = attrs.get("id", (None,))[0]
        if "Storage" in tag and "fileName" in attrs and node_id:
            storage_files[node_id] = os.path.normpath(
                os.path.join(base, unquote(attrs["fileName"][0])))
        if tag.endswith("Volume") and wanted is None:
            for ref in attrs.get("references", ("",))[0].split(";"):
                if ref.startswith("storage:"):
                    wanted = ref.split(":", 1)[1]
    return storage_files.get(wanted) if wanted else None


# --------------------------------------------------------------------------
# write guards
# --------------------------------------------------------------------------

class guard:
    """
    Drop a marker beside a file while it is being modified.

    In-place edits of multi-gigabyte meshes and volumes cannot be rolled back,
    so a leftover marker is the signal that a file may be half-written.
    """

    def __init__(self, path):
        self.marker = path + ".fixvoxel-inprogress"

    def __enter__(self):
        with open(self.marker, "w", encoding="utf-8") as f:
            f.write("fix-voxel-size.py is modifying the matching file.\n"
                    "If this marker outlived the run, that file may be "
                    "partially written; restore it from a backup.\n")
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            try:
                os.remove(self.marker)
            except OSError:
                pass
        return False


def check_guard(path):
    marker = path + ".fixvoxel-inprogress"
    if os.path.exists(marker):
        raise FixError("a previous run left an in-progress marker "
                       f"({os.path.basename(marker)}); this file may be "
                       "partially scaled - restore it and delete the marker")


def make_backup(path):
    dest = path + ".orig"
    if os.path.exists(dest):
        raise FixError(f"backup {os.path.basename(dest)} already exists")
    shutil.copy2(path, dest)


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------

def discover(scene, scan_root):
    """Ordered, de-duplicated list of candidate files to fix."""
    seen, files = set(), []

    def add(path):
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and os.path.isfile(path):
            seen.add(key)
            files.append(os.path.abspath(path))

    add(scene)
    for path in mrml_referenced_files(scene):
        add(path)
    if scan_root:
        for dirpath, dirnames, filenames in os.walk(scan_root):
            dirnames.sort()
            for name in sorted(filenames):
                if name.lower().endswith(
                        VOLUME_EXTS + MODEL_EXTS + (".json", ".mrml")):
                    add(os.path.join(dirpath, name))
    return files


def classify(path):
    """Return a handler name for a discovered file, or None to ignore it."""
    lower = path.lower()
    if lower.endswith(".mrml"):
        return "mrml"
    if lower.endswith(VOLUME_EXTS):
        return "nrrd"
    if lower.endswith(".ply"):
        return "ply"
    if lower.endswith(".stl"):
        return "stl"
    if lower.endswith(".obj"):
        return "obj"
    if lower.endswith(".mrk.json"):
        return "markups"
    if lower.endswith(".metrics.json"):
        return "metrics"
    return None


# --------------------------------------------------------------------------
# handlers and previews
# --------------------------------------------------------------------------

def handle_nrrd(path, s, backup, expected_spacing, apply_changes):
    """
    Rescale a .nrrd / .seg.nrrd header.

    A volume whose spacing does not match the reference's wrong spacing came
    from somewhere else, and is left alone rather than silently rescaled.
    """
    header_text, _, _ = read_nrrd_header(path)
    spacings = nrrd_spacings(header_text)
    if any(abs(v - expected_spacing) > SPACING_TOL * expected_spacing
           for v in spacings):
        raise FixError(
            f"spacing {[fmt_num(v) for v in spacings]} does not match the "
            f"source volume's {fmt_num(expected_spacing)}")

    plan = plan_nrrd(path, s)
    if plan is None:
        return None
    lines, region_len, newline, changes = plan
    if not apply_changes:
        return (f"{changes} ({fmt_num(spacings[0])} -> "
                f"{fmt_num(spacings[0] * s)} mm)")
    return f"{changes}; {write_nrrd_header(path, lines, region_len, newline, backup)}"


def preview(kind, path, s):
    """Describe what a handler would do, without writing anything."""
    if kind == "ply":
        fmt, elements, _ = parse_ply_header(path)
        count = next((e["count"] for e in elements if e["name"] == "vertex"), 0)
        if fmt != "ascii":
            ply_vertex_layout(path)          # validate the layout up front
        return f"scale {count} vertex positions ({fmt})"
    if kind == "stl":
        count = stl_triangle_count(path)
        return (f"scale {count} triangles (binary)" if count is not None
                else "scale vertex lines (ascii)")
    if kind == "obj":
        return "scale 'v' vertex lines"
    if kind == "mrml":
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        return summarise(mrml_edits(text, s)[1])

    with open(path, "r", encoding="utf-8") as f:
        doc = json.load(f)
    if kind == "markups":
        stats = {}
        scale_markups_tree(doc, s, stats)     # on the in-memory copy only
        return summarise(stats)
    if not isinstance(doc, dict):
        raise FixError("metrics file is not a JSON object")
    changed, _ = metrics_changes(doc, s)
    return "; ".join(f"{k}: {fmt_num(old)} -> {fmt_num(new)}"
                     for k, (old, new, _) in changed.items()) or None


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def warn(message):
    print(f"  ! {message}", file=sys.stderr)


def human_size(n):
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Correct a mis-recorded voxel size across a Slicer scene "
                    "and all of its volume, segmentation, model and markups "
                    "files.",
        epilog="By default nothing is written; pass --apply to make changes.")
    parser.add_argument("scene", help="the .mrml scene file")
    parser.add_argument("--voxel-size", type=float, required=True,
                        help="the correct (isotropic) voxel size")
    parser.add_argument("--units", default="mm",
                        choices=sorted(UNIT_SCALE_TO_MM),
                        help="units of --voxel-size (default: mm)")
    parser.add_argument("--reference-volume",
                        help="volume file holding the wrong voxel size "
                             "(default: the scene's source volume)")
    parser.add_argument("--scan-root",
                        help="directory tree to search for related files "
                             "(default: the parent of the scene's folder)")
    parser.add_argument("--no-scan", action="store_true",
                        help="only touch files the scene itself references")
    parser.add_argument("--apply", action="store_true",
                        help="write the changes (default is a dry run)")
    parser.add_argument("--backup", action="store_true",
                        help="copy each file to <name>.orig before writing")
    args = parser.parse_args()

    scene = os.path.abspath(args.scene)
    if not os.path.isfile(scene):
        sys.exit(f"scene not found: {scene}")

    target_mm = args.voxel_size * UNIT_SCALE_TO_MM[args.units]
    if target_mm <= 0:
        sys.exit("--voxel-size must be positive")

    # The wrong spacing comes from the volume the scene is built on, so a
    # second run over already-corrected data computes a scale factor of 1.
    reference = args.reference_volume or mrml_reference_volume(scene)
    if not reference:
        sys.exit("could not identify the scene's source volume; "
                 "pass --reference-volume")
    reference = os.path.abspath(reference)
    if not os.path.isfile(reference):
        sys.exit(f"source volume not found: {reference}")

    try:
        spacings = nrrd_spacings(read_nrrd_header(reference)[0])
    except FixError as exc:
        sys.exit(f"{reference}: {exc}")

    current = spacings[0]
    if any(abs(v - current) > SPACING_TOL * current for v in spacings):
        sys.exit(f"source volume spacing is anisotropic "
                 f"({[fmt_num(v) for v in spacings]}); this script only "
                 "rescales isotropic voxels")

    scale = target_mm / current
    print(f"Scene         : {scene}")
    print(f"Source volume : {os.path.basename(reference)}")
    print(f"Current voxel : {fmt_num(current)} mm")
    print(f"Correct voxel : {fmt_num(target_mm)} mm")
    print(f"Scale factor  : {scale!r}")

    if abs(scale - 1.0) <= SPACING_TOL:
        print("\nThe source volume already carries the correct voxel size; "
              "nothing to do.")
        return 0

    scan_root = None
    if not args.no_scan:
        scan_root = os.path.abspath(
            args.scan_root or os.path.dirname(os.path.dirname(scene)))
        print(f"Scanning      : {scan_root}")
    print(f"Mode          : {'APPLY' if args.apply else 'dry run'}")

    files = discover(scene, scan_root)
    # The source volume goes last: while it still holds the wrong spacing an
    # interrupted run can be restarted without double-scaling anything.
    files.sort(key=lambda p: os.path.normcase(p) == os.path.normcase(reference))

    handlers = {"ply": scale_ply, "stl": scale_stl, "obj": scale_obj,
                "markups": scale_markups_json, "metrics": scale_metrics_json,
                "mrml": scale_mrml}

    print()
    changed = unchanged = failed = 0
    for path in files:
        kind = classify(path)
        if kind is None:
            continue
        print(f"{os.path.relpath(path, os.path.dirname(scene))}  "
              f"[{kind}, {human_size(os.path.getsize(path))}]")
        try:
            check_guard(path)
            if kind == "nrrd":
                note = handle_nrrd(path, scale, args.backup, current,
                                   args.apply)
            elif args.apply:
                note = handlers[kind](path, scale, args.backup)
            else:
                note = preview(kind, path, scale)
            if note is None:
                print("    nothing to change")
                unchanged += 1
            else:
                print(f"    {'' if args.apply else 'would change: '}{note}")
                changed += 1
        except FixError as exc:
            warn(f"skipped: {exc}")
            failed += 1
        except Exception as exc:                        # noqa: BLE001
            warn(f"failed: {exc.__class__.__name__}: {exc}")
            failed += 1

    print(f"\n{changed} file(s) {'changed' if args.apply else 'would change'}, "
          f"{unchanged} unchanged, {failed} skipped or failed.")
    if not args.apply and changed:
        print("Re-run with --apply to write these changes.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
