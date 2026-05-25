#!/usr/bin/env python3
"""
Extract data from .pca files and assemble into a CSV file.

Usage: python extract-pca-data.py <search_path> [output.csv]
"""

import argparse
import configparser
import csv
import sys
from pathlib import Path

FIELDS = [
    ("Geometry", "VoxelSizeX"),
    ("Detector", "TimingVal"),
    ("Detector", "Avg"),
    ("Detector", "Skip"),
    ("Xray", "Voltage"),
    ("Xray", "Current"),
]

CSV_HEADERS = ["FilePath", "FileName", "VoxelSizeX", "TimingVal", "Avg", "Skip", "Voltage", "Current"]


def parse_pca(path: Path) -> dict:
    parser = configparser.ConfigParser()
    # .pca files use standard INI syntax; read with UTF-8, fall back to latin-1
    try:
        parser.read(path, encoding="utf-8")
    except UnicodeDecodeError:
        parser.read(path, encoding="latin-1")

    row = {"FilePath": str(path), "FileName": path.name}
    for section, key in FIELDS:
        try:
            row[key] = parser.get(section, key)
        except (configparser.NoSectionError, configparser.NoOptionError):
            row[key] = ""
    return row


def main():
    parser = argparse.ArgumentParser(description="Extract data from .pca files into a CSV.")
    parser.add_argument("search_path", help="Root directory to search for .pca files")
    parser.add_argument(
        "output",
        nargs="?",
        default="pca_data.csv",
        help="Output CSV file path (default: pca_data.csv)",
    )
    args = parser.parse_args()

    # Strip stray quotes and trailing path separators that Windows shells can
    # inject when a path argument ends with a backslash (e.g. "C:\path\").
    root = Path(args.search_path.strip('"\'').rstrip('/\\'))
    if not root.is_dir():
        print(f"Error: '{root}' is not a directory.", file=sys.stderr)
        sys.exit(1)

    print(f"Searching for .pca files under '{root}'...")
    pca_files = sorted(root.rglob("*.pca"))
    if not pca_files:
        print(f"No .pca files found under '{root}'.", file=sys.stderr)
        sys.exit(1)

    output_path = Path(args.output)
    with output_path.open("w", newline="", encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=CSV_HEADERS)
        writer.writeheader()
        for pca_file in pca_files:
            msg = f"Processing: {pca_file.name}"
            print(f"\r{msg:<80}", end="", flush=True)
            row = parse_pca(pca_file)
            writer.writerow(row)

    print(f"\rWrote {len(pca_files)} record(s) to '{output_path}'.{' ' * 20}")


if __name__ == "__main__":
    main()
