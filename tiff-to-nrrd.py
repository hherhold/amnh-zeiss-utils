#!/bin/env python

'''
tiff-to-nrrd.py

Convert a directory of 2D TIFF slices into a single NRRD volume.

The volume is streamed: each slice is read, written to the NRRD file, and
dropped before the next one is read, so memory use stays around one slice no
matter how big the volume is (stacks of 80 GB and more are routine). The NRRD
header is written directly rather than through pynrrd, whose writer needs the
whole volume in memory.

Slices are taken in "natural" file-name order (slice_2 before slice_10), the
first file becoming slice 0. Every slice must have the same size and data
type; this is checked for all files before anything is written, so a bad file
can't spoil a long conversion part way through. The data type is kept as-is
(uint8, uint16, float32, ...).

By Hollister Herhold, AMNH, 2026.
Claude Opus 5.5 used for initial authoring.

'''

import argparse
import fnmatch
import gzip
import os
import re
import sys

import numpy as np
import tifffile
from tqdm import tqdm

# NRRD type names for the numpy dtypes a TIFF slice can have.
_NRRD_TYPES = {
    'int8': 'int8', 'uint8': 'uint8',
    'int16': 'int16', 'uint16': 'uint16',
    'int32': 'int32', 'uint32': 'uint32',
    'int64': 'int64', 'uint64': 'uint64',
    'float32': 'float', 'float64': 'double',
}


def natural_key(name):
    """Sort key that orders embedded numbers numerically: slice_2 < slice_10."""
    return [int(part) if part.isdigit() else part.lower()
            for part in re.split(r'(\d+)', name)]


def find_slices(input_dir, pattern):
    """Return the paths of the TIFF slices in `input_dir`, in slice order.

    `pattern` is a shell-style glob matched case-insensitively against file
    names. Hidden files are skipped -- in particular the "._" AppleDouble
    files macOS leaves next to every file on exFAT/FAT external drives, which
    would otherwise match *.tif."""
    match = re.compile(fnmatch.translate(pattern), re.IGNORECASE).match
    names = [name for name in os.listdir(input_dir)
             if match(name) and not name.startswith('.')
             and os.path.isfile(os.path.join(input_dir, name))]
    return [os.path.join(input_dir, name)
            for name in sorted(names, key=natural_key)]


def check_slices(paths, verbose=False):
    """Check that every file is a single 2D image of the same size and type,
    reading only the TIFF headers. Returns (height, width, dtype).

    Exits with a message naming the first file that doesn't fit."""
    expected = None
    for path in tqdm(paths, desc="Checking", unit="file", disable=not verbose):
        try:
            with tifffile.TiffFile(path) as tif:
                n_pages = len(tif.pages)
                page = tif.pages[0]
                shape, dtype = page.shape, page.dtype
        except Exception as e:
            sys.exit(f"Error: can't read '{path}' as a TIFF: {e}")
        if n_pages != 1 or len(shape) != 2:
            sys.exit(f"Error: '{path}' isn't a single 2D grayscale image "
                     f"({n_pages} page(s), shape {shape}). Each file must hold "
                     f"exactly one slice.")
        if dtype.name not in _NRRD_TYPES:
            sys.exit(f"Error: '{path}' has data type {dtype}, which NRRD "
                     f"can't store.")
        if expected is None:
            expected = (shape, dtype)
        elif (shape, dtype) != expected:
            sys.exit(f"Error: '{path}' is {shape[1]}x{shape[0]} {dtype}, but "
                     f"'{paths[0]}' is {expected[0][1]}x{expected[0][0]} "
                     f"{expected[1]}. All slices must match.")
    (height, width), dtype = expected
    return height, width, dtype


def nrrd_header(width, height, depth, dtype, voxel_size_mm, gzipped):
    """Build the NRRD header text for a (depth, height, width) volume.

    NRRD lists axes fastest-varying first, so sizes are width, height, depth.
    `voxel_size_mm` is an (x, y, z) tuple, or None to leave the volume without
    a physical size."""
    lines = [
        'NRRD0004',
        '# Complete NRRD file format specification at:',
        '# http://teem.sourceforge.net/nrrd/format.html',
        '# Written by tiff-to-nrrd.py',
        f'type: {_NRRD_TYPES[dtype.name]}',
        'dimension: 3',
    ]
    if voxel_size_mm is not None:
        # A full LPS coordinate space, as txm-to-nrrd.py writes, so ITK-based
        # readers (3D Slicer) accept the file.
        x, y, z = voxel_size_mm
        lines += [
            'space: left-posterior-superior',
            f'sizes: {width} {height} {depth}',
            f'space directions: ({x!r},0,0) (0,{y!r},0) (0,0,{z!r})',
        ]
    else:
        lines.append(f'sizes: {width} {height} {depth}')
    lines.append('kinds: domain domain domain')
    if dtype.itemsize > 1:
        lines.append('endian: little')
    lines.append(f'encoding: {"gzip" if gzipped else "raw"}')
    if voxel_size_mm is not None:
        lines += [
            'space origin: (0,0,0)',
            'space units: "mm" "mm" "mm"',
        ]
    # A blank line ends the header; the data follows immediately.
    return '\n'.join(lines) + '\n\n'


def write_nrrd(paths, output_file, height, width, dtype, voxel_size_mm,
               compression_level, verbose=False):
    """Stream the slices into `output_file`, one at a time.

    compression_level 0 writes raw data; 1-9 gzip-compresses it."""
    gzipped = compression_level > 0
    little = dtype.newbyteorder('<')
    header = nrrd_header(width, height, len(paths), dtype, voxel_size_mm,
                         gzipped)

    with open(output_file, 'wb') as raw_out:
        raw_out.write(header.encode('ascii'))
        out = (gzip.GzipFile(fileobj=raw_out, mode='wb', filename='',
                             compresslevel=compression_level, mtime=0)
               if gzipped else raw_out)
        try:
            for path in tqdm(paths, desc="Writing", unit="slice"):
                image = tifffile.imread(path)
                if image.shape != (height, width) or image.dtype != dtype:
                    # check_slices() vetted the headers; this catches a file
                    # that changed since.
                    raise ValueError(f"'{path}' no longer matches the other "
                                     f"slices ({image.shape}, {image.dtype}).")
                out.write(np.ascontiguousarray(image, dtype=little).data)
        finally:
            if gzipped:
                out.close()   # flushes the gzip trailer; leaves raw_out open


def main():
    parser = argparse.ArgumentParser(
        description="Convert a directory of 2D TIFF slices into one NRRD "
                    "volume, streaming slice by slice so even very large "
                    "stacks don't need to fit in memory.")
    parser.add_argument("-i", "--input-dir", required=True,
                        help="Directory holding the TIFF slices, one slice "
                             "per file. Files are taken in natural name order "
                             "(slice_2 before slice_10).")
    parser.add_argument("-o", "--output-nrrd-file", required=True,
                        help="Output NRRD file")
    parser.add_argument("-s", "--voxel-size", type=float, nargs='+',
                        metavar="MM",
                        help="Voxel size in mm: one value for isotropic "
                             "voxels, or three for X Y Z. If omitted, the NRRD "
                             "has no physical size (Slicer will assume 1 mm).")
    parser.add_argument("-g", "--glob", default="*.tif*",
                        help="Which files in the directory are slices, as a "
                             "case-insensitive shell-style pattern "
                             "(default: *.tif*, i.e. .tif and .tiff). Hidden "
                             "files are always skipped.")
    parser.add_argument("-c", "--compression-level", type=int, default=1,
                        choices=range(0, 10), metavar="{0-9}",
                        help="gzip compression level; 0 writes uncompressed "
                             "(raw) data, which is fastest to write and to "
                             "load (default: 1)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose output")
    args = parser.parse_args()

    voxel_size_mm = None
    if args.voxel_size is not None:
        if len(args.voxel_size) == 1:
            voxel_size_mm = tuple(args.voxel_size * 3)
        elif len(args.voxel_size) == 3:
            voxel_size_mm = tuple(args.voxel_size)
        else:
            parser.error("--voxel-size takes one value (isotropic) or three "
                         "(X Y Z)")
        if min(voxel_size_mm) <= 0:
            parser.error("--voxel-size values must be greater than 0")

    if not os.path.isdir(args.input_dir):
        sys.exit(f"Error: '{args.input_dir}' is not a directory.")
    paths = find_slices(args.input_dir, args.glob)
    if not paths:
        sys.exit(f"Error: no files matching '{args.glob}' in "
                 f"'{args.input_dir}'.")

    height, width, dtype = check_slices(paths, args.verbose)
    size_gb = len(paths) * height * width * dtype.itemsize / 1e9
    print(f"{len(paths)} slices of {width}x{height} {dtype} "
          f"({size_gb:.1f} GB uncompressed)")
    if args.verbose:
        print(f"First slice: {os.path.basename(paths[0])}")
        print(f"Last slice:  {os.path.basename(paths[-1])}")
    if voxel_size_mm is None:
        print("WARNING - no --voxel-size given; voxel size not set.",
              file=sys.stderr)
    elif args.verbose:
        print(f"Voxel size (mm): {voxel_size_mm[0]} x {voxel_size_mm[1]} x "
              f"{voxel_size_mm[2]}")

    try:
        write_nrrd(paths, args.output_nrrd_file, height, width, dtype,
                   voxel_size_mm, args.compression_level, args.verbose)
    except (Exception, KeyboardInterrupt) as e:
        # Don't leave a truncated file that looks like a finished volume.
        if os.path.exists(args.output_nrrd_file):
            os.remove(args.output_nrrd_file)
        if isinstance(e, KeyboardInterrupt):
            sys.exit("Interrupted -- incomplete output file removed.")
        sys.exit(f"Error: {e}\nIncomplete output file removed.")

    print(f"Wrote {args.output_nrrd_file}")


if __name__ == "__main__":
    main()
