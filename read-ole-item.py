#!/usr/bin/env python

'''

read-ole-item.py

Read and print the value(s) stored at a given OLE directory path in an OLE file.

By Hollister Herhold, AMNH, 2026.

'''

import argparse
import struct
import sys
import olefile

STRING_TYPES = {'str', 'string', 'utf8', 'utf-8', 'utf16', 'utf-16'}

SUPPORTED_TYPES = {
    'int8':    ('<b', 1),
    'uint8':   ('<B', 1),
    'int16':   ('<h', 2),
    'uint16':  ('<H', 2),
    'int32':   ('<i', 4),
    'uint32':  ('<I', 4),
    'int64':   ('<q', 8),
    'uint64':  ('<Q', 8),
    'float32': ('<f', 4),
    'float64': ('<d', 8),
    # Convenience aliases
    'int':     ('<i', 4),
    'float':   ('<f', 4),
    'double':  ('<d', 8),
}

parser = argparse.ArgumentParser(
    description="Read and print the value(s) at an OLE directory path in an OLE file."
)
parser.add_argument("-i", "--input-file",
                    help="Input OLE file (e.g. .txrm, .txm, .ole)",
                    required=True)
parser.add_argument("-p", "--path",
                    help="OLE directory path, slash-separated (e.g. 'ImageInfo/ImageWidth')",
                    required=True)
all_types = sorted(STRING_TYPES) + sorted(SUPPORTED_TYPES)
parser.add_argument("-t", "--type",
                    help=f"Data type to interpret the bytes as. Supported: {', '.join(all_types)}",
                    required=True)
parser.add_argument("--raw", action="store_true", default=False,
                    help="Also print the raw bytes as hex")

args = parser.parse_args()

dtype = args.type.lower()
if dtype not in SUPPORTED_TYPES and dtype not in STRING_TYPES:
    print(f"Error: unsupported type '{args.type}'. Supported types: {', '.join(all_types)}", file=sys.stderr)
    sys.exit(1)

if not olefile.isOleFile(args.input_file):
    print(f"Error: '{args.input_file}' is not a valid OLE file.", file=sys.stderr)
    sys.exit(1)

# OLE paths can be provided as "A/B/C"; olefile expects a list or slash-separated string.
ole_path = args.path.strip("/")

with olefile.OleFileIO(args.input_file) as ole:
    if not ole.exists(ole_path):
        print(f"Error: OLE path '{ole_path}' not found in '{args.input_file}'.", file=sys.stderr)
        sys.exit(1)

    data = ole.openstream(ole_path).read()

if args.raw:
    print(f"Raw bytes ({len(data)} bytes): {data.hex()}")

if dtype in STRING_TYPES:
    encoding = 'utf-16-le' if dtype in ('utf16', 'utf-16') else 'utf-8'
    try:
        print(data.decode(encoding).rstrip('\x00'))
    except UnicodeDecodeError:
        # Fall back to the other encoding before giving up.
        fallback = 'utf-8' if encoding != 'utf-8' else 'utf-16-le'
        try:
            print(data.decode(fallback).rstrip('\x00'))
        except UnicodeDecodeError:
            print(f"Error: could not decode stream as {encoding}.", file=sys.stderr)
            sys.exit(1)
    sys.exit(0)

fmt, item_size = SUPPORTED_TYPES[dtype]

n_items, remainder = divmod(len(data), item_size)
if remainder != 0:
    print(
        f"Warning: stream length {len(data)} bytes is not a multiple of "
        f"{item_size} (size of {dtype}). Trailing {remainder} byte(s) ignored.",
        file=sys.stderr,
    )

if n_items == 0:
    print("No complete items found in stream.")
    sys.exit(0)

values = struct.unpack_from(fmt[0] + str(n_items) + fmt[1], data)

if n_items == 1:
    print(values[0])
else:
    for i, v in enumerate(values):
        print(f"[{i}] {v}")
