#!/usr/bin/env python
"""Build and query a SQLite database of GE / phoenix|x-ray CT scan metadata.

Scans a directory tree of ``.pca`` (acquisition) and ``.pcr`` (reconstruction)
metadata files and produces one database record per *scan*.

A "scan" is a scan-root: a ``.pca`` file that is NOT inside a ``ScanN``
subdirectory. Larger specimens are captured as several ``Scan1/``, ``Scan2/``
tiles that are stitched together -- these are recorded as sub-scans of a single
multi-scan record (``multiscan = 1``).

Usage:
    python ge_scan_db.py build  --root pca_test --db scans.sqlite [--force]
    python ge_scan_db.py report --db scans.sqlite
    python ge_scan_db.py query  --db scans.sqlite --sql "SELECT ..."

Runs in the amnh-zeiss-utils conda environment; standard library only.
"""

from __future__ import annotations

import argparse
import configparser
import os
import re
import sqlite3
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

SCAN_DIR_RE = re.compile(r"^Scan\d+$", re.IGNORECASE)
SCAN_INDEX_RE = re.compile(r"^Scan(\d+)$", re.IGNORECASE)
AMNH_RE = re.compile(r"AMNH\s*#?\s*(\d+)", re.IGNORECASE)
# container folders whose name is not the specimen name
CONTAINER_NAMES = {"raw and reconstructed", "reconstructions", "fused_tif"}

# Sections/keys promoted to dedicated columns on the `scan` table.
#
# Each column maps to a list of candidate (section, key) locations tried in
# order; the first present wins. Lookups are case-insensitive so both the
# modern ([Xray], [Image]) and legacy ([XRAY], [ACQUISITION]) phoenix|x-ray
# layouts resolve to the same column.
PCA_COLUMNS = {
    "datosx_version":     [("General",    "Version")],
    "voltage_kv":         [("Xray",       "Voltage")],
    "current_ua":         [("Xray",       "Current")],
    "xray_name":          [("Xray",       "Name"), ("Xray", "Tube")],
    "xray_filter":        [("Xray",       "Filter"), ("Xray", "XRayFilter")],
    "voxel_size_x":       [("Geometry",   "VoxelSizeX"), ("Geometry", "Voxelsize")],
    "voxel_size_y":       [("Geometry",   "VoxelSizeY"), ("Geometry", "Voxelsize")],
    "fdd":                [("Geometry",   "FDD")],
    "fod":                [("Geometry",   "FOD")],
    "magnification":      [("Geometry",   "Magnification")],
    "number_images":      [("CT",         "NumberImages"), ("Acquisition", "NumberImages")],
    "rotation_sector":    [("CT",         "RotationSector"), ("Acquisition", "TotalRotation")],
    "detector_name":      [("Detector",   "Name"), ("Detector", "Sensor")],
    "detector_pixels_x":  [("Detector",   "NrPixelsX")],
    "detector_pixels_y":  [("Detector",   "NrPixelsY")],
    "detector_timing":    [("Detector",   "TimingVal")],
    "image_dim_x":        [("Image",      "DimX"), ("Acquisition", "DimX")],
    "image_dim_y":        [("Image",      "DimY"), ("Acquisition", "DimY")],
    "bit_pp":             [("Image",      "BitPP"), ("Acquisition", "ImgBitspp")],
    "multiscan_flag":     [("Multiscan",  "Active"), ("Acquisition", "Multiscan_Activ")],
    "multiscan_nrscans":  [("Multiscan",  "NrScans"), ("Acquisition", "Multiscan_NrScans")],
}

PCR_COLUMNS = {
    "pcr_version":        [("Versions",                "Version-datos|x")],
    "recon_voxel_size":   [("VolumeData",              "VoxelSizeRec")],
    "volume_size_x":      [("VolumeData",              "Volume_SizeX")],
    "volume_size_y":      [("VolumeData",              "Volume_SizeY")],
    "volume_size_z":      [("VolumeData",              "Volume_SizeZ")],
    "volume_format":      [("VolumeData",              "Format")],
    "roi_size_x":         [("ROI",                     "ROI_SizeX")],
    "roi_size_y":         [("ROI",                     "ROI_SizeY")],
    "roi_size_z":         [("ROI",                     "ROI_SizeZ")],
    "recon_filter_kernel":[("Reconstruction Settings", "RecFilterKernel")],
    "recon_last_image":   [("Reconstruction Settings", "LastImage")],
    "pca_file_ref":       [("ImageData",               "PCA_File")],
}

# Relative tolerance for comparing overlapping numeric parameters.
NUM_TOL = 1e-6


def parse_ini(path):
    """Parse an INI-style .pca/.pcr file into {(section, key): value}.

    Keys preserve their original case for faithful raw storage.
    """
    cp = configparser.RawConfigParser(strict=False)
    cp.optionxform = str  # preserve key case
    try:
        cp.read(path, encoding="utf-8")
    except UnicodeDecodeError:
        cp.read(path, encoding="latin-1")
    out = {}
    for section in cp.sections():
        for key, value in cp.items(section):
            out[(section, key)] = value
    return out


def ci_index(params):
    """Case-insensitive {(section_lower, key_lower): value} view of params."""
    return {(s.lower(), k.lower()): v for (s, k), v in params.items()}


def lookup(ci_params, candidates):
    """First matching value among candidate (section, key) pairs, else None."""
    for section, key in candidates:
        val = ci_params.get((section.lower(), key.lower()))
        if val is not None:
            return val
    return None


def as_number(value):
    """Return a float if the string looks numeric, else None."""
    if value is None:
        return None
    try:
        return float(value.strip())
    except (ValueError, AttributeError):
        return None


def values_match(a, b):
    """True if two raw string values are equal (numeric-aware)."""
    if a == b:
        return True
    na, nb = as_number(a), as_number(b)
    if na is None or nb is None:
        return False
    if na == nb:
        return True
    scale = max(abs(na), abs(nb), 1.0)
    return abs(na - nb) <= NUM_TOL * scale


# --------------------------------------------------------------------------- #
# Scan discovery
# --------------------------------------------------------------------------- #

@dataclass
class SubScan:
    index: int
    dir: str
    pca_path: str | None = None
    pcr_path: str | None = None


@dataclass
class Scan:
    root: str                       # absolute root of the crawl
    scan_dir: str                   # dir holding the scan-root .pca
    pca_path: str                   # the scan-root .pca
    pcr_path: str | None = None     # the (merged) scan-root .pcr, if any
    multiscan: bool = False
    subscans: list[SubScan] = field(default_factory=list)

    @property
    def rel_dir(self):
        return os.path.relpath(self.scan_dir, self.root)

    @property
    def specimen(self):
        """Best-effort specimen label from the folder structure."""
        d = self.scan_dir
        name = os.path.basename(d)
        if name.strip().lower() in CONTAINER_NAMES:
            name = os.path.basename(os.path.dirname(d))
        return name

    @property
    def amnh_catalog(self):
        m = AMNH_RE.search(self.rel_dir)
        return m.group(1) if m else None


def _first(paths, ext):
    for p in sorted(paths):
        if p.lower().endswith(ext):
            return p
    return None


def discover_scans(root):
    """Yield Scan objects for every scan-root .pca under `root`."""
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root):
        base = os.path.basename(dirpath)
        if SCAN_DIR_RE.match(base):
            continue  # sub-scan dirs are handled via their parent

        pcas = [os.path.join(dirpath, f) for f in filenames if f.lower().endswith(".pca")]
        if not pcas:
            continue

        pca_path = sorted(pcas)[0]
        pcrs = [os.path.join(dirpath, f) for f in filenames if f.lower().endswith(".pcr")]
        pcr_path = sorted(pcrs)[0] if pcrs else None

        scan = Scan(root=root, scan_dir=dirpath, pca_path=pca_path, pcr_path=pcr_path)

        # Detect ScanN subdirectories -> multi-scan tiles.
        for sub in sorted(dirnames):
            m = SCAN_INDEX_RE.match(sub)
            if not m:
                continue
            sub_dir = os.path.join(dirpath, sub)
            sub_files = os.listdir(sub_dir)
            sub_pca = _first([os.path.join(sub_dir, f) for f in sub_files], ".pca")
            sub_pcr = _first([os.path.join(sub_dir, f) for f in sub_files], ".pcr")
            if sub_pca or sub_pcr:
                scan.subscans.append(
                    SubScan(index=int(m.group(1)), dir=sub_dir,
                            pca_path=sub_pca, pcr_path=sub_pcr))

        scan.multiscan = len(scan.subscans) > 0
        scan.subscans.sort(key=lambda s: s.index)
        yield scan


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE scan (
    id                  INTEGER PRIMARY KEY,
    specimen            TEXT,
    amnh_catalog        TEXT,
    rel_dir             TEXT UNIQUE,
    pca_path            TEXT,
    pcr_path            TEXT,
    multiscan           INTEGER NOT NULL DEFAULT 0,
    n_subscans          INTEGER NOT NULL DEFAULT 0,
    -- acquisition (.pca)
    datosx_version      TEXT,
    voltage_kv          REAL,
    current_ua          REAL,
    xray_name           TEXT,
    xray_filter         TEXT,
    voxel_size_x        REAL,
    voxel_size_y        REAL,
    fdd                 REAL,
    fod                 REAL,
    magnification       REAL,
    number_images       INTEGER,
    rotation_sector     REAL,
    detector_name       TEXT,
    detector_pixels_x   INTEGER,
    detector_pixels_y   INTEGER,
    detector_timing     TEXT,
    image_dim_x         INTEGER,
    image_dim_y         INTEGER,
    bit_pp              INTEGER,
    multiscan_flag      INTEGER,
    multiscan_nrscans   INTEGER,
    -- reconstruction (.pcr)
    pcr_version         TEXT,
    recon_voxel_size    REAL,
    volume_size_x       INTEGER,
    volume_size_y       INTEGER,
    volume_size_z       INTEGER,
    volume_format       TEXT,
    roi_size_x          INTEGER,
    roi_size_y          INTEGER,
    roi_size_z          INTEGER,
    recon_filter_kernel TEXT,
    recon_last_image    INTEGER,
    pca_file_ref        TEXT
);

CREATE TABLE subscan (
    id          INTEGER PRIMARY KEY,
    scan_id     INTEGER NOT NULL REFERENCES scan(id),
    scan_index  INTEGER,
    rel_dir     TEXT,
    pca_path    TEXT,
    pcr_path    TEXT
);

CREATE TABLE raw_param (
    id         INTEGER PRIMARY KEY,
    scan_id    INTEGER NOT NULL REFERENCES scan(id),
    file_role  TEXT,     -- root_pca | root_pcr | subscan_pca | subscan_pcr
    sub_index  INTEGER,  -- NULL for root files
    file_path  TEXT,
    section    TEXT,
    key        TEXT,
    value      TEXT
);

CREATE TABLE parse_issue (
    id         INTEGER PRIMARY KEY,
    scan_id    INTEGER REFERENCES scan(id),
    issue_type TEXT,     -- overlap_mismatch | parse_error | missing_pcr
    file_a     TEXT,
    file_b     TEXT,
    section    TEXT,
    key        TEXT,
    value_a    TEXT,
    value_b    TEXT,
    detail     TEXT
);

CREATE INDEX idx_scan_amnh      ON scan(amnh_catalog);
CREATE INDEX idx_scan_multiscan ON scan(multiscan);
CREATE INDEX idx_raw_scan       ON raw_param(scan_id);
CREATE INDEX idx_raw_key        ON raw_param(section, key);
"""


def coerce_int(value):
    n = as_number(value)
    return int(n) if n is not None else None


def coerce_real(value):
    return as_number(value)


# Which promoted columns are numeric, so we store them typed.
INT_COLS = {
    "number_images", "detector_pixels_x", "detector_pixels_y",
    "image_dim_x", "image_dim_y", "bit_pp", "multiscan_flag",
    "multiscan_nrscans", "volume_size_x", "volume_size_y", "volume_size_z",
    "roi_size_x", "roi_size_y", "roi_size_z", "recon_last_image",
}
REAL_COLS = {
    "voltage_kv", "current_ua", "voxel_size_x", "voxel_size_y", "fdd", "fod",
    "magnification", "rotation_sector", "recon_voxel_size",
}


def build_scan_row(scan, pca_params, pcr_params):
    row = {
        "specimen": scan.specimen,
        "amnh_catalog": scan.amnh_catalog,
        "rel_dir": scan.rel_dir,
        "pca_path": scan.pca_path,
        "pcr_path": scan.pcr_path,
        "multiscan": int(scan.multiscan),
        "n_subscans": len(scan.subscans),
    }
    pca_ci = ci_index(pca_params)
    pcr_ci = ci_index(pcr_params) if pcr_params else {}
    for col, candidates in PCA_COLUMNS.items():
        row[col] = _typed(col, lookup(pca_ci, candidates))
    for col, candidates in PCR_COLUMNS.items():
        row[col] = _typed(col, lookup(pcr_ci, candidates))
    return row


def _typed(col, raw):
    if raw is None:
        return None
    if col in INT_COLS:
        return coerce_int(raw)
    if col in REAL_COLS:
        return coerce_real(raw)
    return raw


def compare_overlap(scan_id, path_a, params_a, path_b, params_b, issues):
    """Log a parse_issue for every (section,key) in both files that disagrees."""
    for (section, key) in set(params_a) & set(params_b):
        va, vb = params_a[(section, key)], params_b[(section, key)]
        if not values_match(va, vb):
            issues.append((scan_id, "overlap_mismatch", path_a, path_b,
                           section, key, va, vb, "pca/pcr value disagreement"))


def ingest(db_path, root, verbose=True):
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    cur = conn.cursor()

    n_scans = n_multi = n_issues = 0
    for scan in discover_scans(root):
        try:
            pca_params = parse_ini(scan.pca_path)
            pcr_params = parse_ini(scan.pcr_path) if scan.pcr_path else {}
        except Exception as exc:  # pragma: no cover - defensive
            cur.execute(
                "INSERT INTO parse_issue (scan_id, issue_type, file_a, detail) "
                "VALUES (NULL, 'parse_error', ?, ?)", (scan.pca_path, str(exc)))
            continue

        row = build_scan_row(scan, pca_params, pcr_params)
        cols = ", ".join(row)
        placeholders = ", ".join("?" for _ in row)
        cur.execute(f"INSERT INTO scan ({cols}) VALUES ({placeholders})",
                    list(row.values()))
        scan_id = cur.lastrowid
        n_scans += 1
        if scan.multiscan:
            n_multi += 1

        issues = []
        # raw params + overlap check for the root pair
        _store_raw(cur, scan_id, "root_pca", None, scan.pca_path, pca_params)
        if scan.pcr_path:
            _store_raw(cur, scan_id, "root_pcr", None, scan.pcr_path, pcr_params)
            compare_overlap(scan_id, scan.pca_path, pca_params,
                            scan.pcr_path, pcr_params, issues)

        # sub-scans
        for sub in scan.subscans:
            cur.execute(
                "INSERT INTO subscan (scan_id, scan_index, rel_dir, pca_path, pcr_path) "
                "VALUES (?, ?, ?, ?, ?)",
                (scan_id, sub.index, os.path.relpath(sub.dir, scan.root),
                 sub.pca_path, sub.pcr_path))
            sub_pca = parse_ini(sub.pca_path) if sub.pca_path else {}
            sub_pcr = parse_ini(sub.pcr_path) if sub.pcr_path else {}
            if sub.pca_path:
                _store_raw(cur, scan_id, "subscan_pca", sub.index, sub.pca_path, sub_pca)
            if sub.pcr_path:
                _store_raw(cur, scan_id, "subscan_pcr", sub.index, sub.pcr_path, sub_pcr)
            if sub.pca_path and sub.pcr_path:
                compare_overlap(scan_id, sub.pca_path, sub_pca,
                                sub.pcr_path, sub_pcr, issues)

        if issues:
            cur.executemany(
                "INSERT INTO parse_issue "
                "(scan_id, issue_type, file_a, file_b, section, key, value_a, value_b, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", issues)
            n_issues += len(issues)

    conn.commit()
    conn.close()
    if verbose:
        print(f"Ingested {n_scans} scans ({n_multi} multi-scan) into {db_path}")
        print(f"Logged {n_issues} overlap mismatch(es) to parse_issue.")


def _store_raw(cur, scan_id, role, sub_index, path, params):
    cur.executemany(
        "INSERT INTO raw_param (scan_id, file_role, sub_index, file_path, section, key, value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(scan_id, role, sub_index, path, sec, key, val)
         for (sec, key), val in params.items()])


# --------------------------------------------------------------------------- #
# Reporting / querying
# --------------------------------------------------------------------------- #

def report(db_path):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    def scalar(sql):
        return cur.execute(sql).fetchone()[0]

    print(f"Database: {db_path}")
    print(f"  scans           : {scalar('SELECT COUNT(*) FROM scan')}")
    print(f"  multi-scans     : {scalar('SELECT COUNT(*) FROM scan WHERE multiscan=1')}")
    print(f"  sub-scan tiles  : {scalar('SELECT COUNT(*) FROM subscan')}")
    print(f"  with .pcr recon : {scalar('SELECT COUNT(*) FROM scan WHERE pcr_path IS NOT NULL')}")
    print(f"  distinct AMNH # : {scalar('SELECT COUNT(DISTINCT amnh_catalog) FROM scan WHERE amnh_catalog IS NOT NULL')}")
    print(f"  raw params      : {scalar('SELECT COUNT(*) FROM raw_param')}")

    n_issue = scalar("SELECT COUNT(*) FROM parse_issue")
    print(f"  parse issues    : {n_issue}")
    if n_issue:
        print("\n  Overlap mismatches by (section, key):")
        rows = cur.execute(
            "SELECT section, key, COUNT(*) FROM parse_issue "
            "WHERE issue_type='overlap_mismatch' GROUP BY section, key "
            "ORDER BY COUNT(*) DESC LIMIT 15").fetchall()
        for sec, key, n in rows:
            print(f"    {n:4d}  [{sec}] {key}")

    print("\n  kV distribution:")
    for kv, n in cur.execute(
        "SELECT voltage_kv, COUNT(*) FROM scan GROUP BY voltage_kv ORDER BY voltage_kv"):
        print(f"    {kv} kV : {n}")
    conn.close()


def run_query(db_path, sql):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows = cur.execute(sql).fetchall()
    if not rows:
        print("(no rows)")
        return
    headers = rows[0].keys()
    widths = [len(h) for h in headers]
    data = []
    for r in rows:
        vals = ["" if r[h] is None else str(r[h]) for h in headers]
        widths = [max(w, len(v)) for w, v in zip(widths, vals)]
        data.append(vals)
    print(" | ".join(h.ljust(w) for h, w in zip(headers, widths)))
    print("-+-".join("-" * w for w in widths))
    for vals in data:
        print(" | ".join(v.ljust(w) for v, w in zip(vals, widths)))
    print(f"\n({len(rows)} rows)")
    conn.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build", help="scan a tree and build the database")
    b.add_argument("--root", required=True, help="root directory to crawl")
    b.add_argument("--db", required=True, help="SQLite database to create")
    b.add_argument("--force", action="store_true", help="overwrite an existing db")

    r = sub.add_parser("report", help="print a summary of the database")
    r.add_argument("--db", required=True)

    q = sub.add_parser("query", help="run a SQL SELECT against the database")
    q.add_argument("--db", required=True)
    q.add_argument("--sql", required=True, help="SQL query to run")

    args = p.parse_args(argv)

    if args.cmd == "build":
        if os.path.exists(args.db):
            if not args.force:
                p.error(f"{args.db} exists; use --force to overwrite")
            os.remove(args.db)
        if not os.path.isdir(args.root):
            p.error(f"root not found: {args.root}")
        ingest(args.db, args.root)
    elif args.cmd == "report":
        report(args.db)
    elif args.cmd == "query":
        run_query(args.db, args.sql)


if __name__ == "__main__":
    sys.exit(main())
