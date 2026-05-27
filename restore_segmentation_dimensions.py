#!/usr/bin/env python3
"""
restore_segmentation_dimensions.py

Restores a cropped .seg.nrrd to the full dimensions of an original
segmentation, reversing the crop that Slicer applies on save.

Usage:
    python restore_segmentation_dimensions.py ORIGINAL CROPPED OUTPUT

    ORIGINAL  - path to Segmentation.seg.nrrd (provides target dimensions)
    CROPPED   - path to the cropped file to restore (e.g. subsampled_Segmentation.seg.nrrd)
    OUTPUT    - path for the restored output file

The Slicer bug shifts the space origin away from (0,0,0) and crops the
volume extent.  This script:
  1. Reads the target dimensions from the original segmentation file.
  2. Computes the voxel-space offset from the shifted space origin.
  3. Embeds the cropped data in a zero-padded array of the correct size.
  4. Writes a corrected .seg.nrrd with the origin restored to (0,0,0).

Dependencies: pynrrd, numpy.
"""

import argparse
import nrrd
import numpy as np


def read_custom_fields(path):
    """
    Read the raw header and return all := custom fields as an ordered list
    of (key, value) tuples.  pynrrd does not write := fields, so we carry
    them through manually.
    """
    with open(path, "rb") as f:
        content = f.read()
    header_text = content[: content.index(b"\n\n")].decode("ascii")
    fields = []
    for line in header_text.splitlines():
        stripped = line.strip()
        if stripped and ":=" in stripped and not stripped.startswith("#"):
            key, _, val = stripped.partition(":=")
            fields.append((key.strip(), val.strip()))
    return fields


def inject_custom_fields(path, custom_fields):
    """
    Append := custom fields into the header of a pynrrd-written file.
    Reads the file, inserts lines before the blank-line separator, rewrites.
    """
    with open(path, "rb") as f:
        content = f.read()
    sep = b"\n\n"
    idx = content.index(sep)
    header = content[:idx].decode("ascii")
    data_blob = content[idx + len(sep):]

    extra = "\n".join(f"{k}:={v}" for k, v in custom_fields)
    new_header = header + "\n" + extra

    with open(path, "wb") as f:
        f.write(new_header.encode("ascii"))
        f.write(sep)
        f.write(data_blob)


def main():
    parser = argparse.ArgumentParser(
        description="Restore a Slicer-cropped .seg.nrrd to its original dimensions."
    )
    parser.add_argument("original", help="Original segmentation file (provides target dimensions)")
    parser.add_argument("cropped",  help="Cropped segmentation file to restore")
    parser.add_argument("output",   help="Output path for the restored file")
    args = parser.parse_args()

    # 1. Read target dimensions from the original segmentation.
    #    index_order='F': axis 0 of the array = first NRRD dimension = space direction 0.
    #    We only need the header here, but pynrrd always reads the data too.
    _, orig_header = nrrd.read(args.original, index_order="F")
    target_sizes = list(map(int, orig_header["sizes"]))
    print(f"Target dimensions (from original): {target_sizes}")

    # 2. Read the cropped segmentation.
    crop_data, crop_header = nrrd.read(args.cropped, index_order="F")
    print(f"Cropped array shape:               {list(crop_data.shape)}")

    # If the original has more dimensions than the cropped data (e.g., the original
    # is a 4D multi-segment file with a leading segment-count axis while the cropped
    # file is a plain 3D spatial volume), use only the trailing spatial dimensions.
    ndim = crop_data.ndim
    if len(target_sizes) > ndim:
        target_sizes = target_sizes[-ndim:]
        print(f"Spatial target sizes (trimmed):    {target_sizes}")

    # 3. Compute voxel offsets from the shifted space origin.
    #    With index_order='F', axis i of the array corresponds to space direction i,
    #    so origin[i] / voxel_size[i] gives the voxel offset along axis i.
    origin = crop_header["space origin"]          # shape (3,), physical units
    dirs   = crop_header["space directions"]      # shape (3,3); row i = direction for axis i
    voxel_sizes = np.diag(dirs)                   # valid for axis-aligned (diagonal) volumes

    offsets = np.round(origin / voxel_sizes).astype(int)
    ox, oy, oz = offsets
    dx, dy, dz = crop_data.shape
    print(f"Physical origin:                   {list(origin)}")
    print(f"Voxel size:                        {voxel_sizes[0]:.8f}")
    print(f"Voxel offsets (x, y, z):           {list(offsets)}")

    # Sanity check: cropped data must fit inside the target volume.
    for axis, (o, d, t) in enumerate(zip(offsets, (dx, dy, dz), target_sizes)):
        if o + d > t:
            raise ValueError(
                f"Axis {axis}: offset {o} + size {d} = {o + d} "
                f"exceeds target size {t}"
            )

    # 4. Embed cropped data in a zero-padded array of the target size.
    restored = np.zeros(target_sizes, dtype=crop_data.dtype)
    restored[ox:ox+dx, oy:oy+dy, oz:oz+dz] = crop_data
    print(f"Restored array shape:              {list(restored.shape)}")

    # 5. Build the output header from the crop header, updating key fields.
    x_end = ox + dx - 1
    y_end = oy + dy - 1
    z_end = oz + dz - 1

    out_header = dict(crop_header)
    out_header["sizes"]        = np.array(target_sizes)
    out_header["space origin"] = np.zeros(3)

    # 6. Read := custom fields from the raw cropped header and update Segment0_Extent.
    custom_fields = read_custom_fields(args.cropped)
    custom_fields = [
        (k, f"{ox} {x_end} {oy} {y_end} {oz} {z_end}") if k == "Segment0_Extent" else (k, v)
        for k, v in custom_fields
    ]

    # 7. Write with pynrrd (handles encoding, byte order, compression).
    nrrd.write(args.output, restored, out_header, index_order="F")

    # 8. Patch the := custom fields back into the written file.
    inject_custom_fields(args.output, custom_fields)

    print(f"\nSaved restored segmentation to:\n  {args.output}")


if __name__ == "__main__":
    main()
