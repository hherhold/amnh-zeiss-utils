"""
subsample_segment.py
--------------------
Create a subsampled segmentation NRRD for biomedisa Smart Interpolation.

Reads a 3D Slicer .seg.nrrd segmentation, merges all segments into a single
label volume, then retains only every Nth slice along all three axes (zeroing
all voxels not on a kept slice in any axis).  The output is a plain .nrrd
that can be passed directly to biomedisa.interpolation with --allaxis.

Usage
-----
python subsample_segment.py CT_VOLUME.nrrd SEGMENTATION.seg.nrrd [options]

    --step [AXIS,STEP ...]  Keep one slice every STEP slices along AXIS.
                      Provide one or more AXIS,STEP pairs (no spaces around comma).
                      Valid axes: 0, 1, 2.  STEP must be >= 2.
                      Omit entirely to sample all 3 axes at step 15.
    -o OUTPUT         Output .seg.nrrd filename
                      (default: subsampled_<stem>.seg.nrrd next to the input).

Examples
--------
# Axis 0 only, every 10 slices:
python subsample_segment.py scan.nrrd brain.seg.nrrd --step 0,10

# Axis 0 every 10, axis 1 every 15:
python subsample_segment.py scan.nrrd brain.seg.nrrd --step 0,10 1,15

# All axes with mixed steps:
python subsample_segment.py scan.nrrd brain.seg.nrrd --step 0,10 1,15 2,10

# Then run biomedisa (--allaxis required for multi-axis seeds):
python -m biomedisa.interpolation scan.nrrd subsampled_brain.nrrd --allaxis
"""

import argparse
import re
import sys
from pathlib import Path

import numpy as np
import nrrd


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def read_segments(header):
    """Return a list of segment-metadata dicts parsed from a .seg.nrrd header."""
    pattern = re.compile(r"^Segment(\d+)_(.+)$")
    segments = {}
    for key, value in header.items():
        m = pattern.match(key)
        if m:
            idx, field = int(m.group(1)), m.group(2)
            segments.setdefault(idx, {"index": idx})[field] = value
    return [segments[i] for i in sorted(segments)]


def merge_segments(data, segments):
    """
    Merge a multi-layer .seg.nrrd data array into a single 3D label volume.

    In a 3D Slicer .seg.nrrd file several segments can share the same spatial
    volume (stored as separate 'layers').  This function collapses all layers
    into one array, assigning each segment a new unique positive integer label
    (1-based order over the sorted segment list).

    Parameters
    ----------
    data : ndarray
        4-D [num_layers, Z, Y, X] or 3-D [Z, Y, X] array from nrrd.read().
    segments : list[dict]
        Segment metadata dicts from read_segments().

    Returns
    -------
    label_vol : ndarray, dtype int16
        3-D label volume.  0 = background; each segment → a unique positive int.
    """
    is_4d = data.ndim == 4
    spatial_shape = data.shape[1:] if is_4d else data.shape
    label_vol = np.zeros(spatial_shape, dtype=np.int16)
    seg_label_pairs = []

    for new_label, seg in enumerate(segments, start=1):
        layer = int(seg.get("Layer", 0))
        label_value = int(seg.get("LabelValue", 1))
        layer_data = data[layer] if is_4d else data
        mask = layer_data == label_value
        label_vol[mask] = new_label
        seg_label_pairs.append((seg, new_label))
        print(
            f"  Segment {seg['index']}: {seg.get('Name', '<unnamed>')!r}"
            f"  (layer={layer}, original LabelValue={label_value}"
            f" → merged label={new_label},"
            f" voxels={int(mask.sum())})"
        )

    return label_vol, seg_label_pairs


def subsample_labels(label_vol, axis_steps):
    """
    Return a copy of label_vol keeping only seed slices for the specified axes.

    axis_steps is a list of (axis, step) tuples.  For each pair, slices at
    indices 0, step, 2*step, … are designated as "kept".  A voxel is retained
    if its index along *any* of the sampled axes falls in that axis's kept set;
    all other voxels are zeroed.  Use --allaxis in biomedisa when more than one
    axis is sampled.
    """
    assert label_vol.ndim == 3

    # Build a boolean keep-mask (True = voxel survives)
    keep = np.zeros(label_vol.shape, dtype=bool)
    for axis, step in axis_steps:
        n = label_vol.shape[axis]
        kept = set(range(0, n, step))
        idx = [slice(None)] * 3
        for i in kept:
            idx[axis] = i
            keep[tuple(idx)] = True
        n_nonzero = sum(
            int(np.any(np.take(label_vol, i, axis=axis))) for i in kept
        )
        print(
            f"  Axis {axis}: kept {len(kept)} of {n} slices"
            f" (step={step}), {n_nonzero} with non-zero labels."
        )

    subsampled = np.where(keep, label_vol, 0).astype(label_vol.dtype)
    total_nonzero = int(np.count_nonzero(subsampled))
    print(f"  Total non-zero voxels in subsampled volume: {total_nonzero}")
    return subsampled


def build_output_header(source_header, spatial_shape, seg_label_pairs=None):
    """
    Build a .seg.nrrd header for the merged 3-D label volume.

    Copies spatial metadata from the source header (stripping any 4th-
    dimension entries) and re-emits Segment<N>_* fields with updated
    LabelValue and Layer to match the merged single-layer layout.
    """
    out_header = {}

    if "space" in source_header:
        out_header["space"] = source_header["space"]

    if "space origin" in source_header:
        origin = source_header["space origin"]
        # If origin somehow has 4 components, keep only the last 3
        out_header["space origin"] = list(origin)[-3:]

    if "space directions" in source_header:
        dirs = source_header["space directions"]
        # The layer axis is represented by a [nan, nan, nan] row; drop it.
        spatial_dirs = [
            list(d) for d in dirs
            if not all(np.isnan(float(v)) for v in d)
        ]
        out_header["space directions"] = spatial_dirs

    out_header["type"] = "int16"
    out_header["dimension"] = 3
    out_header["sizes"] = list(spatial_shape)
    out_header["encoding"] = "gzip"

    # Embed segment metadata so the output is a valid .seg.nrrd readable
    # by 3D Slicer and biomedisa alike.
    if seg_label_pairs:
        for new_idx, (seg, new_label) in enumerate(seg_label_pairs):
            prefix = f"Segment{new_idx}"
            for field, value in seg.items():
                if field == "index":          # internal bookkeeping, not a header key
                    continue
                if field == "Layer":
                    out_header[f"{prefix}_Layer"] = "0"
                elif field == "LabelValue":
                    out_header[f"{prefix}_LabelValue"] = str(new_label)
                else:
                    out_header[f"{prefix}_{field}"] = str(value)
            # Guarantee required fields are present even if missing in source
            out_header.setdefault(f"{prefix}_Layer", "0")
            out_header.setdefault(f"{prefix}_LabelValue", str(new_label))

    return out_header


def parse_axis_step(value: str):
    """argparse type converter: parse 'AXIS,STEP' into a validated (axis, step) tuple."""
    parts = value.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError(
            f"Invalid value {value!r}. Expected AXIS,STEP with no spaces (e.g. 0,10)."
        )
    try:
        axis, step = int(parts[0]), int(parts[1])
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid value {value!r}. Both AXIS and STEP must be integers."
        )
    if axis not in (0, 1, 2):
        raise argparse.ArgumentTypeError(
            f"Invalid axis {axis} in {value!r}. Must be 0, 1, or 2."
        )
    if step < 2:
        raise argparse.ArgumentTypeError(
            f"Step {step} in {value!r} must be at least 2."
        )
    return (axis, step)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description=(
            "Create a subsampled segmentation NRRD for biomedisa Smart Interpolation. "
            "Merges all segments from a 3D Slicer .seg.nrrd into one label volume, "
            "then retains only every Nth slice (zeroing the rest)."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "ct_volume",
        metavar="CT_VOLUME.nrrd",
        help="Path to the CT scan NRRD file (passed unchanged to biomedisa).",
    )
    parser.add_argument(
        "seg_nrrd",
        metavar="SEGMENTATION.seg.nrrd",
        help="Path to the 3D Slicer segmentation file.",
    )
    parser.add_argument(
        "--step",
        type=parse_axis_step,
        nargs="*",
        default=[],
        metavar="AXIS,STEP",
        help=(
            "Keep one slice every STEP slices along AXIS. Provide one or more "
            "AXIS,STEP pairs with no spaces around the comma (e.g. 0,10 or 0,10 1,15 2,10). "
            "Valid axes: 0, 1, 2. STEP must be >= 2. "
            "Omit entirely to sample all 3 axes at step 15."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output .seg.nrrd filename. Defaults to subsampled_<stem>.seg.nrrd beside the input.",
    )
    args = parser.parse_args()

    ct_path = Path(args.ct_volume)
    seg_path = Path(args.seg_nrrd)

    if not ct_path.exists():
        sys.exit(f"Error: CT volume not found: {ct_path}")
    if not seg_path.exists():
        sys.exit(f"Error: Segmentation file not found: {seg_path}")

    # Expand --step to (axis, step) pairs
    axis_steps = args.step  # list of (axis, step) tuples, validated by parse_axis_step
    if not axis_steps:
        axis_steps = [(0, 15), (1, 15), (2, 15)]
    axes_used = [a for a, _ in axis_steps]
    if len(axes_used) != len(set(axes_used)):
        sys.exit("Error: duplicate axes in --step arguments.")

    # Determine output path
    if args.output:
        out_path = Path(args.output)
    else:
        stem = seg_path.name
        for suffix in (".seg.nrrd", ".nrrd"):
            if stem.endswith(suffix):
                stem = stem[: -len(suffix)]
                break
        out_path = seg_path.parent / f"subsampled_{stem}.seg.nrrd"

    axes_desc = ", ".join(f"axis-{a}={s}" for a, s in axis_steps)
    print(f"CT volume   : {ct_path}")
    print(f"Segmentation: {seg_path}")
    print(f"Steps       : {axes_desc}")
    print(f"Output      : {out_path}\n")

    # ------------------------------------------------------------------
    # Read segmentation
    # ------------------------------------------------------------------
    print("Reading segmentation file...")
    data, header = nrrd.read(str(seg_path))
    print(f"  Data shape : {data.shape}  dtype: {data.dtype}")

    segments = read_segments(header)
    if not segments:
        sys.exit("Error: No segments found in the segmentation file header.")
    print(f"\nFound {len(segments)} segment(s):")

    # ------------------------------------------------------------------
    # Merge all segments into a single 3-D label volume
    # ------------------------------------------------------------------
    label_vol, seg_label_pairs = merge_segments(data, segments)

    # ------------------------------------------------------------------
    # Subsample along all 3 axes
    # ------------------------------------------------------------------
    n_axes = len(axis_steps)
    axes_label = "all 3 axes" if n_axes == 3 else f"{n_axes} axis" if n_axes == 1 else f"{n_axes} axes"
    print(f"\nSubsampling along {axes_label}:")
    subsampled = subsample_labels(label_vol, axis_steps)

    if not np.any(subsampled):
        print(
            "\nWarning: The subsampled label volume is entirely zero. "
            "Consider reducing --step or checking that the segmentation covers "
            "more than one slice."
        )

    # ------------------------------------------------------------------
    # Write output
    # ------------------------------------------------------------------
    out_header = build_output_header(header, subsampled.shape, seg_label_pairs)
    print(f"\nWriting: {out_path}")
    nrrd.write(str(out_path), subsampled, out_header)
    print("Done.\n")

    # ------------------------------------------------------------------
    # Print the biomedisa command to run next
    # ------------------------------------------------------------------
    allaxis = " --allaxis" if len(axis_steps) > 1 else ""
    print("Run biomedisa Smart Interpolation with:")
    print(f"  python -m biomedisa.interpolation \"{ct_path}\" \"{out_path}\"{allaxis}")


if __name__ == "__main__":
    main()
