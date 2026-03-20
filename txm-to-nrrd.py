#!/bin/env python

'''
txm-to-nrrd.py

Convert Zeiss txm metadata to NRRD format.

By Hollister Herhold, AMNH, 2025/2026.
Claude Sonnet 4.6 used for major refactoring. 

'''

import argparse
import math
import struct
import sys

import numpy as np
import nrrd
import olefile

# Handle command line arguments.
parser = argparse.ArgumentParser(description="Convert reconstructed Zeiss txm to NRRD format.")

parser.add_argument("-i", "--input-txm-file", help="Input Zeiss txm file",
                    required=True)
parser.add_argument("-o", "--output-nrrd-file", help="Output NRRD file to save data",
                    required=True)
parser.add_argument("-v", "--verbose", help="Enable verbose output",
                    action="store_true", default=False)

args = parser.parse_args()


def _read_ole_uint32(ole, path):
    """Read a single little-endian uint32 from an OLE stream."""
    if not ole.exists(path):
        return None
    data = ole.openstream(path).read()
    return struct.unpack_from('<I', data)[0]


# DataType values used by Zeiss: 5 = uint16, 10 = float32.
_DATA_TYPE_MAP = {5: np.uint16, 10: np.float32}


def read_txm(file_name):
    """
    Read image volume from a Zeiss .txm (or .txrm) OLE file.

    Returns a numpy array of shape (n_images, height, width) in the file's
    native dtype (uint16 or float32), or None on error.
    """
    if not olefile.isOleFile(file_name):
        print(f"Error: '{file_name}' is not a valid OLE file.", file=sys.stderr)
        return None

    with olefile.OleFileIO(file_name) as ole:
        n_images = _read_ole_uint32(ole, 'ImageInfo/NoOfImages')
        width    = _read_ole_uint32(ole, 'ImageInfo/ImageWidth')
        height   = _read_ole_uint32(ole, 'ImageInfo/ImageHeight')
        dtype_id = _read_ole_uint32(ole, 'ImageInfo/DataType')

        if None in (n_images, width, height, dtype_id):
            print("Error: could not read required ImageInfo fields.", file=sys.stderr)
            return None

        img_dtype = _DATA_TYPE_MAP.get(dtype_id)
        if img_dtype is None:
            print(f"Error: unsupported DataType value {dtype_id}.", file=sys.stderr)
            return None

        volume = np.empty((n_images, height, width), dtype=img_dtype)

        for i in range(n_images):
            group = math.ceil((i + 1) / 100.0)
            path  = f"ImageData{group}/Image{i + 1}"
            if not ole.exists(path):
                print(f"Warning: missing OLE stream '{path}', skipping slice {i}.",
                      file=sys.stderr)
                volume[i] = 0
                continue
            raw  = ole.openstream(path).read()
            arr  = np.frombuffer(raw, dtype=np.dtype(img_dtype).newbyteorder('<'))
            volume[i] = arr.reshape(height, width)

    return volume


def main():
    scan_volume = read_txm(args.input_txm_file)
    if scan_volume is None:
        print(f"Error: Could not read input file {args.input_txm_file}. Exiting.")
        return

    # Change the data type to 16-bit unsigned integers.
    scan_volume = scan_volume.astype('uint16')

    if args.verbose:
        print(f"Scan volume shape: {scan_volume.shape}")

    nrrd.write(args.output_nrrd_file, scan_volume, compression_level=1, index_order='C')

    print("WARNING - Voxel size has not been set. This may be fixed in future versions.") of this script.")

if __name__ == "__main__":
    main()
