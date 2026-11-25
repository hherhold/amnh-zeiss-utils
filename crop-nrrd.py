#!/bin/env python

'''
crop-nrrd.py

Crop a 3D NRRD volume to a Slicer ROI (a "Crop Volume ROI.mrk.json" saved from
the Crop Volume module or the Markups module), optionally resampling it, and
write the result to a new NRRD file. Without a ROI the whole volume is used,
which with a spacing scale other than 1.0 just resamples it.

Slicer's Crop Volume module holds the whole input volume and the whole output
volume in memory at once, which is a problem for very large scans. This tool
streams instead: it reads only the slices of the input that the ROI covers,
and only the rows and columns of those it needs, and writes the output slice
by slice. Memory use stays around a few slices, whatever the size of the
input or output.

Like Slicer's Crop Volume module:
  - With a spacing scale of 1.0 (the default) the crop is voxel based: the
    output is an exact copy of the input voxels whose centers fall inside the
    ROI, with no resampling.
  - With any other spacing scale the output is resampled (linear
    interpolation, or nearest neighbor with --nearest) at the input spacing
    times the scale: 0.5 halves the voxel size (twice as many voxels along each
    axis), 2.0 doubles it. Samples outside the input are 0.

With --max-dimension N (N = 2048 if not given) the spacing scale is
computed instead: the smallest scale, in hundredths, at which no output
axis exceeds N voxels. That's the largest 3D texture Slicer's volume
rendering can use on Apple Silicon. The output name then defaults to the
input's with '-rescaled-<scale>x' inserted, e.g. 'scan-rescaled-1.25x.nrrd'.

The ROI's axes must be parallel to the volume's axes (a ROI rotated relative
to the volume isn't supported; use Slicer for those). The output keeps the
input volume's axis directions, so its voxels are laid out the same way as the
input's. The ROI's own position comes from its file only: a transform applied
to it in the Slicer scene isn't saved in the .mrk.json and isn't applied here.

Input NRRDs can be raw, gzip, or bzip2 encoded, with an attached or a
detached (.nhdr) header.

By Hollister Herhold, AMNH, 2026.
Claude Opus 5.5 used for initial authoring.

'''

import argparse
import bz2
import gzip
import json
import math
import os
import sys
import zlib

import numpy as np
import nrrd
from tqdm import tqdm

# NRRD type names (including the spec's aliases) to numpy dtypes.
_NRRD_TO_NUMPY = {}
for _names, _dtype in (
        (('signed char', 'int8', 'int8_t'), 'i1'),
        (('uchar', 'unsigned char', 'uint8', 'uint8_t'), 'u1'),
        (('short', 'short int', 'signed short', 'signed short int', 'int16',
          'int16_t'), 'i2'),
        (('ushort', 'unsigned short', 'unsigned short int', 'uint16',
          'uint16_t'), 'u2'),
        (('int', 'signed int', 'int32', 'int32_t'), 'i4'),
        (('uint', 'unsigned int', 'uint32', 'uint32_t'), 'u4'),
        (('longlong', 'long long', 'long long int', 'signed long long',
          'signed long long int', 'int64', 'int64_t'), 'i8'),
        (('ulonglong', 'unsigned long long', 'unsigned long long int',
          'uint64', 'uint64_t'), 'u8'),
        (('float',), 'f4'),
        (('double',), 'f8')):
    for _name in _names:
        _NRRD_TO_NUMPY[_name] = _dtype

# numpy dtype names back to the NRRD type names we write.
_NUMPY_TO_NRRD = {
    'int8': 'int8', 'uint8': 'uint8', 'int16': 'int16', 'uint16': 'uint16',
    'int32': 'int32', 'uint32': 'uint32', 'int64': 'int64', 'uint64': 'uint64',
    'float32': 'float', 'float64': 'double',
}

_LPS_NAMES = ('left-posterior-superior', 'lps')
_RAS_NAMES = ('right-anterior-superior', 'ras')

# Read and decompress in chunks of this size.
_CHUNK = 4 << 20


class CropError(Exception):
    """A problem with the inputs, reported to the user without a traceback."""


# ---------------------------------------------------------------------------
# Reading the inputs
# ---------------------------------------------------------------------------

def read_roi(path):
    """Read a Slicer ROI markup. Returns (center, axes, size, coordinate
    system), where `axes` holds the ROI's unit axis vectors as columns."""
    try:
        with open(path, encoding='utf-8') as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        raise CropError(f"can't read ROI file '{path}': {e}")

    rois = [m for m in doc.get('markups', []) if m.get('type') == 'ROI']
    if len(rois) != 1:
        raise CropError(f"'{path}' should hold exactly one ROI markup "
                        f"(found {len(rois)}).")
    roi = rois[0]
    if roi.get('insideOut'):
        raise CropError("the ROI is set to 'inside out', which cropping "
                        "doesn't support.")
    try:
        center = np.array(roi['center'], dtype=float)
        size = np.array(roi['size'], dtype=float)
        # Row-major 3x3; the columns are the ROI's axis directions.
        axes = np.array(roi.get('orientation', np.eye(3).ravel()),
                        dtype=float).reshape(3, 3)
    except (KeyError, ValueError) as e:
        raise CropError(f"'{path}' doesn't describe a box ROI: {e}")
    if center.shape != (3,) or size.shape != (3,) or np.any(size <= 0):
        raise CropError(f"'{path}' has an invalid ROI center or size.")
    units = roi.get('coordinateUnits', 'mm')
    if units != 'mm':
        raise CropError(f"ROI coordinates are in '{units}'; only mm is "
                        f"supported.")
    system = roi.get('coordinateSystem', 'LPS').upper()
    if system not in ('LPS', 'RAS'):
        raise CropError(f"unknown ROI coordinate system '{system}'.")
    return center, axes, size, system


class Volume:
    """A 3D NRRD's geometry and data layout, read from its header."""

    def __init__(self, path):
        with open(path, 'rb') as fh:
            try:
                header = nrrd.read_header(fh)
            except Exception as e:
                raise CropError(f"can't read NRRD header of '{path}': {e}")
            header_end = fh.tell()

        if int(header['dimension']) != 3:
            raise CropError(f"'{path}' has {header['dimension']} dimensions; "
                            f"only 3D scalar volumes are supported.")
        self.sizes = [int(n) for n in header['sizes']]   # x, y, z

        try:
            dtype = np.dtype(_NRRD_TO_NUMPY[header['type']])
        except KeyError:
            raise CropError(f"unsupported NRRD type '{header['type']}'.")
        endian = header.get('endian', 'little')
        self.file_dtype = dtype.newbyteorder('<' if endian == 'little'
                                             else '>')
        self.dtype = dtype.newbyteorder('=')

        self.encoding = header['encoding'].lower()
        if self.encoding in ('gz',):
            self.encoding = 'gzip'
        elif self.encoding in ('bz2',):
            self.encoding = 'bzip2'
        if self.encoding not in ('raw', 'gzip', 'bzip2'):
            raise CropError(f"unsupported NRRD encoding '{self.encoding}' "
                            f"(raw, gzip and bzip2 are supported).")

        self._read_geometry(header)
        self._locate_data(path, header, header_end)

    def _read_geometry(self, header):
        space = str(header.get('space', 'left-posterior-superior')).lower()
        if space in _LPS_NAMES:
            self.space = 'left-posterior-superior'
        elif space in _RAS_NAMES:
            self.space = 'right-anterior-superior'
        else:
            raise CropError(f"unsupported NRRD space '{space}' (LPS or RAS "
                            f"only).")

        if 'space directions' in header:
            # pynrrd gives one row per axis; we keep them as columns.
            directions = np.array(header['space directions'], dtype=float).T
            if directions.shape != (3, 3) or not np.all(np.isfinite(directions)):
                raise CropError("the volume's space directions aren't a 3x3 "
                                "matrix (is it a vector or RGB volume?).")
        else:
            spacings = header.get('spacings', [1.0, 1.0, 1.0])
            directions = np.diag([float(s) for s in spacings])
        if abs(np.linalg.det(directions)) < 1e-12:
            raise CropError("the volume's space directions are degenerate.")
        self.directions = directions
        self.origin = np.array(header.get('space origin', [0.0, 0.0, 0.0]),
                               dtype=float)

    def _locate_data(self, path, header, header_end):
        if int(header.get('line skip', 0)) != 0:
            raise CropError("NRRDs using 'line skip' aren't supported.")
        byte_skip = int(header.get('byte skip', 0))

        data_file = header.get('data file') or header.get('datafile')
        if data_file:
            if (not isinstance(data_file, str) or data_file.startswith('LIST')
                    or ' ' in data_file.strip()):
                raise CropError("NRRDs split across several data files "
                                "aren't supported.")
            self.data_path = os.path.join(os.path.dirname(path), data_file)
            offset = 0
        else:
            self.data_path = path
            offset = header_end

        total = math.prod(self.sizes) * self.dtype.itemsize
        if byte_skip == -1:
            if self.encoding != 'raw':
                raise CropError("'byte skip: -1' only works with raw data.")
            offset = os.path.getsize(self.data_path) - total
        elif byte_skip:
            if self.encoding != 'raw':
                raise CropError("'byte skip' with compressed data isn't "
                                "supported.")
            offset += byte_skip
        self.data_offset = offset

    def to_index(self, points):
        """Physical points (columns, in the volume's space) to continuous
        voxel indices (columns, x y z)."""
        return np.linalg.solve(self.directions,
                               points - self.origin[:, None])


class SliceReader:
    """Reads z slices of a volume in increasing order, returning only the
    requested rows and columns.

    Raw data is read by seeking straight to what's needed. Compressed data
    has to be decompressed from the start, but slices before the ones asked
    for are thrown away as they go by."""

    def __init__(self, volume, rows, cols, progress=None):
        self.volume = volume
        nx, ny, _ = volume.sizes
        self.row_bytes = nx * volume.dtype.itemsize
        self.slice_bytes = ny * self.row_bytes
        self.rows = rows            # (first, last) inclusive
        self.cols = cols
        self.progress = progress
        self.next_slice = 0
        self.file = open(volume.data_path, 'rb')
        self.file.seek(volume.data_offset)
        if volume.encoding != 'raw':
            self.buffer = bytearray()
            self.decompressor = self._new_decompressor()

    def close(self):
        self.file.close()

    def _new_decompressor(self):
        if self.volume.encoding == 'gzip':
            # 47 = 32 + 15: accept a gzip or zlib header automatically.
            return zlib.decompressobj(47)
        return bz2.BZ2Decompressor()

    def _fill(self, n):
        """Decompress until at least n bytes are buffered."""
        truncated = CropError("the volume's data ends early -- is the file "
                              "truncated?")
        while len(self.buffer) < n:
            d = self.decompressor
            if d.eof:
                # Concatenated compressed streams (e.g. from pigz) are valid.
                leftover = d.unused_data
                self.decompressor = d = self._new_decompressor()
                if leftover:
                    self.buffer += d.decompress(leftover, _CHUNK)
                    continue
            if isinstance(d, bz2.BZ2Decompressor):
                data = self.file.read(_CHUNK) if d.needs_input else b''
                if d.needs_input and not data:
                    raise truncated
            else:
                data = d.unconsumed_tail or self.file.read(_CHUNK)
                if not data:
                    # Input's exhausted; collect anything zlib still holds.
                    tail = d.flush()
                    if not tail:
                        raise truncated
                    self.buffer += tail
                    continue
            self.buffer += d.decompress(data, _CHUNK)

    def _take(self, n):
        self._fill(n)
        data = bytes(self.buffer[:n])
        del self.buffer[:n]
        return data

    def read(self, k):
        """Return rows/cols of slice k (k must not go backwards)."""
        if k < self.next_slice:
            raise RuntimeError("slices must be read in increasing order")
        first_row, last_row = self.rows
        n_rows = last_row - first_row + 1

        if self.volume.encoding == 'raw':
            self.file.seek(self.volume.data_offset + k * self.slice_bytes
                           + first_row * self.row_bytes)
            data = self.file.read(n_rows * self.row_bytes)
            if len(data) != n_rows * self.row_bytes:
                raise CropError("the volume's data ends early -- is the file "
                                "truncated?")
        else:
            while self.next_slice < k:      # skip unwanted slices
                self._fill(self.slice_bytes)
                del self.buffer[:self.slice_bytes]
                self.next_slice += 1
                if self.progress is not None:
                    self.progress.update()
            self._take(first_row * self.row_bytes)
            data = self._take(n_rows * self.row_bytes)
            self._take(self.slice_bytes - (last_row + 1) * self.row_bytes)

        if self.progress is not None:
            self.progress.update(k + 1 - self.next_slice)
        self.next_slice = k + 1
        image = np.frombuffer(data, dtype=self.volume.file_dtype)
        image = image.reshape(n_rows, -1)[:, self.cols[0]:self.cols[1] + 1]
        return image.astype(self.volume.dtype)


# ---------------------------------------------------------------------------
# Working out the crop
# ---------------------------------------------------------------------------

def whole_volume_bounds(volume):
    """The whole volume's extent, as roi_index_bounds() gives a ROI's: from
    the outer edge of the first voxel to the outer edge of the last."""
    sizes = np.array(volume.sizes, dtype=float)
    return np.full(3, -0.5), sizes - 0.5


def roi_index_bounds(volume, roi):
    """Return the ROI's extent along each volume axis, in continuous voxel
    index coordinates, as (low, high) arrays (x, y, z)."""
    center, axes, size, system = roi
    if (system == 'LPS') != (volume.space == 'left-posterior-superior'):
        flip = np.diag([-1.0, -1.0, 1.0])      # LPS <-> RAS
        center, axes = flip @ center, flip @ axes

    # The ROI's axes expressed as voxel-index steps. For a ROI parallel to
    # the volume, each column has a single non-zero entry.
    axes_in_index = np.linalg.solve(volume.directions, axes)
    for column in axes_in_index.T:
        magnitudes = np.abs(column) / np.abs(column).max()
        if np.sum(magnitudes > 1e-4) != 1:
            raise CropError("the ROI is rotated relative to the volume's "
                            "axes, which isn't supported. Use Slicer's Crop "
                            "Volume module for rotated ROIs.")
    half = np.abs(axes_in_index) @ (size / 2)
    middle = volume.to_index(center[:, None])[:, 0]
    return middle - half, middle + half


def voxel_crop(volume, low, high):
    """The input voxels whose centers lie inside the ROI, as an inclusive
    (first, last) index pair per axis, clipped to the volume."""
    ranges = []
    for lo, hi, n in zip(low, high, volume.sizes):
        first = max(0, math.ceil(lo - 1e-6))
        last = min(n - 1, math.floor(hi + 1e-6))
        if first > last:
            raise CropError("the ROI doesn't overlap the volume.")
        ranges.append((first, last))
    return ranges


def sample_count(extent, scale):
    """How many output samples an axis `extent` input voxels long gets at a
    spacing scale of `scale`."""
    return max(1, round(extent / scale))


def fit_scale(low, high, max_dimension):
    """The smallest spacing scale, in hundredths and at least 1.0, at which
    no output axis has more than `max_dimension` samples.

    Not simply the nearest hundredth to extent / max_dimension: rounding
    down can leave an axis over the limit (2570 voxels at 2570/2048 =
    1.2549 -> 1.25 gives 2056)."""
    extents = high - low
    # Below this the longest axis rounds to more than max_dimension samples,
    # so the search can start here.
    hundredths = max(100, math.floor(extents.max() / (max_dimension + 0.5)
                                     * 100))
    while any(sample_count(e, hundredths / 100) > max_dimension
              for e in extents):
        hundredths += 1
    return hundredths / 100


def default_output_name(input_file, scale):
    """The input's name with '-rescaled-<scale>x' (or '-cropped' when there's
    no rescaling) inserted before the extension, in the input's directory."""
    stem, ext = os.path.splitext(input_file)
    if ext.lower() not in ('.nrrd', '.nhdr'):
        stem = input_file
    tag = '-cropped' if scale == 1.0 else f'-rescaled-{scale:g}x'
    return f'{stem}{tag}.nrrd'


def resample_positions(low, high, scale):
    """The output sample positions along each axis, in input index
    coordinates: spaced `scale` input voxels apart and centered in the ROI."""
    positions = []
    for lo, hi in zip(low, high):
        extent = hi - lo
        n = sample_count(extent, scale)
        start = lo + (extent - n * scale) / 2 + scale / 2
        positions.append(start + scale * np.arange(n))
    return positions


class AxisSamples:
    """Where each output sample along one axis falls in the input: the two
    input indices either side of it, the weight of the second, and whether
    it's inside the input at all."""

    def __init__(self, positions, n, nearest):
        # Within half a voxel of the edge counts as inside (the edge voxel's
        # value is used); further out is background.
        self.valid = (positions >= -0.5) & (positions <= n - 0.5)
        clamped = np.clip(positions, 0, n - 1)
        if nearest:
            self.i0 = np.floor(clamped + 0.5).astype(np.int64).clip(0, n - 1)
            self.weight = np.zeros_like(clamped)
        else:
            self.i0 = np.floor(clamped).astype(np.int64).clip(0, max(n - 2, 0))
            self.weight = clamped - self.i0
        self.i1 = np.minimum(self.i0 + 1, n - 1)
        if not self.valid.any():
            raise CropError("the ROI doesn't overlap the volume.")
        self.first = int(self.i0[self.valid].min())
        self.last = int(self.i1[self.valid].max())


# ---------------------------------------------------------------------------
# Writing the output
# ---------------------------------------------------------------------------

def nrrd_header(sizes, dtype, space, directions, origin, gzipped):
    """NRRD header text. `directions` holds the axis vectors as columns."""
    vectors = ' '.join('(' + ','.join(repr(float(v)) for v in column) + ')'
                       for column in directions.T)
    lines = [
        'NRRD0004',
        '# Complete NRRD file format specification at:',
        '# http://teem.sourceforge.net/nrrd/format.html',
        '# Written by crop-nrrd.py',
        f'type: {_NUMPY_TO_NRRD[dtype.name]}',
        'dimension: 3',
        f'space: {space}',
        f'sizes: {sizes[0]} {sizes[1]} {sizes[2]}',
        f'space directions: {vectors}',
        'kinds: domain domain domain',
    ]
    if dtype.itemsize > 1:
        lines.append('endian: little')
    lines += [
        f'encoding: {"gzip" if gzipped else "raw"}',
        'space origin: (' + ','.join(repr(float(v)) for v in origin) + ')',
        'space units: "mm" "mm" "mm"',
    ]
    return '\n'.join(lines) + '\n\n'


def write_volume(output_file, header, slices, compression_level, dtype):
    """Write the header, then each 2D slice the `slices` iterator yields."""
    little = dtype.newbyteorder('<')
    with open(output_file, 'wb') as raw_out:
        raw_out.write(header.encode('ascii'))
        out = (gzip.GzipFile(fileobj=raw_out, mode='wb', filename='',
                             compresslevel=compression_level, mtime=0)
               if compression_level else raw_out)
        try:
            for image in slices:
                out.write(np.ascontiguousarray(image, dtype=little).data)
        finally:
            if compression_level:
                out.close()   # flushes the gzip trailer


def copy_slices(reader, z_range):
    """Voxel-based crop: the input slices, cropped, unchanged."""
    for k in range(z_range[0], z_range[1] + 1):
        yield reader.read(k)


def resampled_slices(reader, samples, dtype):
    """Interpolated crop: one resampled output slice at a time.

    Separable interpolation: blend the two input slices either side of the
    output slice, then the two rows either side of each output row, then
    the two columns either side of each output column."""
    sx, sy, sz = samples
    work = np.float64 if dtype == np.float64 else np.float32
    # Indices relative to the block of rows/columns the reader returns.
    # Samples outside the input are zeroed below; clipping just keeps their
    # (unused) indices inside the block.
    def block_index(s, i):
        return np.clip(i - s.first, 0, s.last - s.first)
    x0, x1 = block_index(sx, sx.i0), block_index(sx, sx.i1)
    y0, y1 = block_index(sy, sy.i0), block_index(sy, sy.i1)
    wx = sx.weight.astype(work)[None, :]
    wy = sy.weight.astype(work)[:, None]
    empty = np.zeros((len(sy.valid), len(sx.valid)), dtype=dtype)
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)

    cache = {}      # the last couple of input slices, by index
    def input_slice(k):
        if k not in cache:
            for old in [key for key in cache if key < k - 1]:
                del cache[old]
            cache[k] = reader.read(k).astype(work)
        return cache[k]

    for t in range(len(sz.valid)):
        if not sz.valid[t]:
            yield empty
            continue
        plane = input_slice(sz.i0[t])
        if sz.weight[t] > 0:
            w = work(sz.weight[t])
            plane = plane * (1 - w) + input_slice(sz.i1[t]) * w
        rows = plane[y0] * (1 - wy) + plane[y1] * wy
        image = rows[:, x0] * (1 - wx) + rows[:, x1] * wx
        image[~sy.valid, :] = 0
        image[:, ~sx.valid] = 0
        if np.issubdtype(dtype, np.integer):
            image = np.clip(np.rint(image), info.min, info.max)
        yield image.astype(dtype)


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Crop a 3D NRRD volume to a Slicer ROI (.mrk.json), "
                    "optionally resampling it, without loading the whole "
                    "volume into memory.")
    parser.add_argument("-r", "--roi",
                        help="Slicer ROI markup file (.mrk.json), e.g. "
                             "'Crop Volume ROI.mrk.json'. Optional: without "
                             "it the whole volume is used (resampled if the "
                             "spacing scale isn't 1.0).")
    parser.add_argument("-i", "--input-nrrd-file", required=True,
                        help="Input NRRD volume (.nrrd or .nhdr)")
    parser.add_argument("-o", "--output-nrrd-file",
                        help="Output NRRD file. Required unless -m is given; "
                             "with -m the default is the input's name with "
                             "'-rescaled-<scale>x' inserted (e.g. "
                             "'-rescaled-1.25x'), or '-cropped' if only a ROI "
                             "crop was needed.")
    scaling = parser.add_mutually_exclusive_group()
    scaling.add_argument("-s", "--spacing-scale", type=float, default=1.0,
                         help="Output voxel size as a multiple of the input's, "
                              "as in Slicer's Crop Volume module: 1.0 (the "
                              "default) crops without resampling; 0.5 halves "
                              "the voxel size (twice as many voxels along each "
                              "axis); 2.0 doubles it.")
    scaling.add_argument("-m", "--max-dimension", type=int, nargs="?",
                         const=2048, metavar="N",
                         help="Downsample just enough that no output axis has "
                              "more than N voxels (N defaults to 2048, the "
                              "largest 3D texture Slicer's volume rendering "
                              "gets on Apple Silicon). The spacing scale is "
                              "computed from the volume (or ROI): the "
                              "smallest one, in hundredths, that fits. A "
                              "volume that already fits isn't resampled.")
    parser.add_argument("-n", "--nearest", action="store_true",
                        help="Resample with nearest-neighbor instead of "
                             "linear interpolation (use this for label maps / "
                             "segmentations). Only matters when the spacing "
                             "scale isn't 1.0.")
    parser.add_argument("-c", "--compression-level", type=int, default=None,
                        choices=range(0, 10), metavar="{0-9}",
                        help="gzip compression level for the output; 0 writes "
                             "uncompressed (raw) data. Default: match the "
                             "input (raw stays raw, compressed becomes gzip "
                             "level 1).")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable verbose output")
    args = parser.parse_args()

    if not args.spacing_scale > 0:
        parser.error("--spacing-scale must be greater than 0")
    if args.max_dimension is not None and args.max_dimension < 1:
        parser.error("--max-dimension must be at least 1")
    if args.output_nrrd_file is None and args.max_dimension is None:
        parser.error("-o/--output-nrrd-file is required unless -m is given")

    try:
        volume = Volume(args.input_nrrd_file)
        if args.roi:
            low, high = roi_index_bounds(volume, read_roi(args.roi))
        else:
            low, high = whole_volume_bounds(volume)
    except CropError as e:
        sys.exit(f"Error: {e}")

    nx, ny, nz = volume.sizes
    print(f"Input:  {nx} x {ny} x {nz} {volume.dtype.name}, "
          f"{volume.encoding}")

    scale = args.spacing_scale
    if args.max_dimension is not None:
        scale = fit_scale(low, high, args.max_dimension)
        if scale == 1.0 and not args.roi:
            print(f"Every axis is already at most {args.max_dimension} "
                  f"voxels; nothing to do.")
            return
        if scale == 1.0:
            print(f"The ROI is already at most {args.max_dimension} voxels "
                  f"along every axis; cropping without rescaling.")
        else:
            print(f"Spacing scale {scale:g} keeps every axis at most "
                  f"{args.max_dimension} voxels.")
    if args.output_nrrd_file is None:
        args.output_nrrd_file = default_output_name(args.input_nrrd_file,
                                                    scale)
    if (os.path.exists(args.output_nrrd_file)
            and os.path.samefile(args.output_nrrd_file, args.input_nrrd_file)):
        sys.exit("Error: the output file can't be the input file")

    compression = args.compression_level
    if compression is None:
        compression = 0 if volume.encoding == 'raw' else 1

    try:
        if scale == 1.0:
            ranges = voxel_crop(volume, low, high)
            out_sizes = [last - first + 1 for first, last in ranges]
            directions = volume.directions
            origin = volume.origin + directions @ [first for first, _ in ranges]
            rows, cols, z_range = ranges[1], ranges[0], ranges[2]
            if args.verbose:
                for name, (first, last) in zip('xyz', ranges):
                    print(f"  {name}: input voxels {first}-{last}")
        else:
            positions = resample_positions(low, high, scale)
            samples = [AxisSamples(p, n, args.nearest)
                       for p, n in zip(positions, volume.sizes)]
            out_sizes = [len(p) for p in positions]
            directions = volume.directions * scale
            origin = volume.origin + volume.directions @ [p[0] for p in positions]
            rows = (samples[1].first, samples[1].last)
            cols = (samples[0].first, samples[0].last)
            z_range = (samples[2].first, samples[2].last)
            if args.verbose:
                for name, p in zip('xyz', positions):
                    print(f"  {name}: {len(p)} samples at input index "
                          f"{p[0]:.3f} to {p[-1]:.3f}")
    except CropError as e:
        sys.exit(f"Error: {e}")

    out_gb = math.prod(out_sizes) * volume.dtype.itemsize / 1e9
    spacing = np.linalg.norm(directions, axis=0)
    mode = ("voxel copy" if scale == 1.0 else
            f"resampled x{scale:g}, {'nearest' if args.nearest else 'linear'}")
    print(f"Output: {out_sizes[0]} x {out_sizes[1]} x {out_sizes[2]} "
          f"({mode}), voxel size "
          f"{' x '.join(f'{s:.6g}' for s in spacing)} mm, "
          f"{out_gb:.2f} GB uncompressed, "
          f"{'gzip level ' + str(compression) if compression else 'raw'}")

    header = nrrd_header(out_sizes, volume.dtype, volume.space, directions,
                         origin, compression > 0)
    # Progress counts input slices read, including any skipped on the way
    # (compressed input has to be read from the start).
    first_slice = 0 if volume.encoding != 'raw' else z_range[0]
    progress = tqdm(total=z_range[1] + 1 - first_slice, desc="Reading",
                    unit="slice")
    reader = SliceReader(volume, rows, cols, progress)
    if first_slice:
        reader.next_slice = first_slice
    slices = (copy_slices(reader, z_range) if scale == 1.0
              else resampled_slices(reader, samples, volume.dtype))
    try:
        write_volume(args.output_nrrd_file, header, slices, compression,
                     volume.dtype)
    except (Exception, KeyboardInterrupt) as e:
        progress.close()
        # Don't leave a truncated file that looks like a finished volume.
        if os.path.exists(args.output_nrrd_file):
            os.remove(args.output_nrrd_file)
        if isinstance(e, KeyboardInterrupt):
            sys.exit("Interrupted -- incomplete output file removed.")
        sys.exit(f"Error: {e}\nIncomplete output file removed.")
    finally:
        reader.close()
    progress.close()
    print(f"Wrote {args.output_nrrd_file}")


if __name__ == "__main__":
    main()
