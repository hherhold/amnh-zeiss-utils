#!/usr/bin/env python

'''

dump-ole-directory.py

Read an OLE file and dump the directory structure to a human-readable text file.

By Hollister Herhold, AMNH, 2026.

'''

import argparse
import sys
import olefile

parser = argparse.ArgumentParser(description="Dump the OLE directory structure of a file.")

parser.add_argument("-i", "--input-file", help="Input OLE file (e.g. .txrm, .txm, .ole)",
                    required=True)
parser.add_argument("-o", "--output-file", help="Output text file (default: stdout)",
                    required=False, default=None)

args = parser.parse_args()

if not olefile.isOleFile(args.input_file):
    print(f"Error: '{args.input_file}' is not a valid OLE file.", file=sys.stderr)
    sys.exit(1)

with olefile.OleFileIO(args.input_file) as ole:
    entries = ole.listdir(streams=True, storages=True)

lines = []
lines.append(f"OLE directory listing for: {args.input_file}")
lines.append(f"Total entries: {len(entries)}")
lines.append("")

for entry in sorted(entries):
    path = "/".join(entry)
    indent = "  " * (len(entry) - 1)
    name = entry[-1]
    lines.append(f"{indent}{name}  [{path}]")

output = "\n".join(lines)

if args.output_file:
    with open(args.output_file, "w", encoding="utf-8") as f:
        f.write(output)
    print(f"Directory listing written to '{args.output_file}'.")
else:
    print(output)
