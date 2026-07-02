# amnh-zeiss-utils

**Warning:** This repo is somewhat mis-named as there are utilities for other tasks such
as handling slicer segmentation files, tracking GE metadata, etc. It's a bit of a dumping
ground for scripts used to facilitate CT analysis, much of it designed specifically for
the Microscopy and Imaging Facility at AMNH. What this means is:
 - No warranties expressed or implied
 - No suitability of purpose
 - This code may screw up your data (unlikely but the risk is on you)
 - Your mileage may vary
 - You get the idea
 - It is assumed that you are familiar with running python scripts from the command line
and setting up conda environments. A YAML file (`amnh-zeiss-utils.yaml`) is included with
the required packages. (But see Requirements section, below.)

With that out of the way...

This repo contains for:

 - Handling Zeiss txm and txrm files, specifically micro-CT files. These python scripts
allow extraction of metadata from unreconstructed ('`txrm`') and reconstructed ('`txm`')
files, as well as converting reconstructed ('`txm`') files to TIFF stacks or NRRD files.
(You can convert unreconstruced files too, not sure why you'd want to.) There are also
some utilities for OLE files (the format Zeiss uses for these files.) 

 - Walking globus directory trees/collections to find specific file types. This is used
   for hunting for (and retrieving) PCA and PCR files.

 - Scanning PCA and PCR files (from a GE scanner) for metadata and putting it in a small
   local database for report generation, etc. 

Note that this repo does not rely on any proprietary libraries (such as Zeiss) and is
standalone (apart from setting up python dependencies in an environment, see below) and
you can run scripts here on any machine. This means that changes to Zeiss' (or anybody
else's) proprietary file format may break this code.

## TL;DR - `txrm-monitor.py` at AMNH MIF

If you're here for the Zeiss metadata scanner, `txrm-monitor.py`, here's the short
version. 

 - Nearly everything is run from an anaconda powershell prompt, available from
   the Start menu on the Zeiss machine (if you're logged in as 'Zeiss'.)
 - It is installed in `c:\Users\Zeiss\amnh-zeiss-utils`
 - To update the repo with the latest version: 
   - `cd c:\Users\Zeiss\amnh-zeiss-utils`
   - `git pull`
 - To run the program:
   - `cd c:\Users\Zeiss\amnh-zeiss-utils`
   - `conda activate amnh-zeiss-utils`
   - `python txrm-monitor.py`

OR

Unzip the txrm-monitor.zip folder and double-click on it. This is relatively
untested, however - I don't have a lot of experience with pyinstaller, which is
what I used to make the .exe file.

## Programs

### `txrm-monitor.py`

A PySide6 GUI application that monitors directories for new `.txrm` and/or `.txm`
files and automatically extracts metadata when files are stable. This was implemented
using Claude Sonnet 4.5 and 4.6 inside VS Code with Github Copilot.

**Features:**
- Monitors configured directories recursively (scans subdirectories)
- Configurable scan interval (default 5 minutes) and file-stability window (default 10 
  minutes)
- Monitors `.txrm` files, `.txm` files, or both — selectable in Preferences
- Automatically extracts metadata when a file has not changed size for the stability
  duration
- Saves metadata to `.txrm.txt` / `.txm.txt` files alongside source files
- Daily-rotated logging to a configurable log directory (default `logs/`)
- Real-time log viewer in GUI
- Status bar updated after each scan or processing operation
- Countdown timer for next scan
- Manual "Scan Now" button for immediate scanning
- "Process Selected Now" button to force immediate metadata extraction for a selected file
- Drag-and-drop support: drop `.txrm` / `.txm` files **or folders** onto the window for
  immediate processing (no stability wait); folders are scanned recursively and all
  matching files are processed
- Preferences panel for all configurable settings (see below)
- All settings persisted in `txrm-monitor-config.json`

**Preferences Panel:**

Open with the **Preferences…** button. Settings are saved when you click OK.

| Section | Setting | Default |
|---|---|---|
| File Types | Scan `.txrm` files | ✓ enabled |
| File Types | Scan `.txm` files | disabled |
| Timing | Scan interval | 5 minutes |
| Timing | Stability duration | 10 minutes |
| Log Directory | Path for log files | `logs/` next to script |
| Output Fields | Which `ImageInfo` OLE fields to write | common set (see below) |

- If neither file type is selected a warning is displayed; the application will
  not monitor any files until at least one type is enabled.
- Output Fields shows all known `ImageInfo` fields as checkboxes. Fields
  available only in `.txrm` files are grouped in a separate section and are
  greyed out (and unchecked) when `.txrm` scanning is disabled.
- Changing the log directory takes effect immediately without restarting.

**Usage:**

```bash
python txrm-monitor.py
```

The application provides a graphical interface where you can:
1. Add/remove directories to monitor
2. View the list of monitored files and their status
3. See real-time log output
4. Trigger manual scans or force-process a selected file
5. Drag and drop `.txrm` / `.txm` files **or folders** onto the window to process them
   immediately (folders are scanned recursively)

The window can be minimized while the application continues to run in the
background. Closing the window exits the application.

### `get-metadata-from-txrm.py`

```text
usage: get-metadata-from-txrm.py [-h] -i INPUT_TXRM_FILE [-o OUTPUT_FILE] [-v] [-f FIELDS]

Extract metadata from a Zeiss txrm file.

options:
    -h, --help            show this help message and exit
    -i INPUT_TXRM_FILE, --input-txrm-file INPUT_TXRM_FILE
                            Input Zeiss txrm file
    -o OUTPUT_FILE, --output-file OUTPUT_FILE
                            Output file to save metadata
    -v, --verbose         Enable verbose output
    -f FIELDS, --fields FIELDS
                            Comma-separated list of fields to extract
```


### `dump-ole-directory.py`

Diagnostic tool that prints (or saves) the full OLE directory tree of any
`.txrm`, `.txm`, or other OLE-structured file. Useful for exploring what streams
are available inside a file before reading them with `read-ole-item.py` or
`get-metadata-from-txrm.py`.

```text
usage: dump-ole-directory.py [-h] -i INPUT_FILE [-o OUTPUT_FILE]

Dump the OLE directory structure of a file.

options:
    -h, --help            show this help message and exit
    -i INPUT_FILE, --input-file INPUT_FILE
                            Input OLE file (e.g. .txrm, .txm, .ole)
    -o OUTPUT_FILE, --output-file OUTPUT_FILE
                            Output text file (default: stdout)
```

**Example:**

```bash
python dump-ole-directory.py -i "scan.txrm" -o scan_tree.txt
```

### `read-ole-item.py`

Diagnostic tool that reads and prints the value(s) stored at a specific OLE
stream path inside a `.txrm`, `.txm`, or other OLE file. Supports numeric types
(int8–int64, uint8–uint64, float32, float64) and string types (UTF-8,
UTF-16-LE). When an OLE stream contains an array of values (e.g. per-image
exposure times), all values are printed. Use `--raw` to also see the underlying
hex bytes.

```text
usage: read-ole-item.py [-h] -i INPUT_FILE -p PATH -t TYPE [--raw]

Read and print the value(s) at an OLE directory path in an OLE file.

options:
    -h, --help            show this help message and exit
    -i INPUT_FILE, --input-file INPUT_FILE
                            Input OLE file (e.g. .txrm, .txm, .ole)
    -p PATH, --path PATH  OLE directory path, slash-separated
                            (e.g. 'ImageInfo/ImageWidth')
    -t TYPE, --type TYPE  Data type: int8, uint8, int16, uint16, int32, uint32,
                            int64, uint64, float32, float64, str, utf8, utf16
    --raw                 Also print the raw bytes as hex
```

**Examples:**

```bash
# Read a 32-bit integer field
python read-ole-item.py -i "scan.txrm" -p "ImageInfo/ImageWidth" -t int32

# Read a UTF-16-LE string field
python read-ole-item.py -i "scan.txrm" -p "ImageInfo/ObjectiveName" -t utf16

# Read an array of floats and also show raw bytes
python read-ole-item.py -i "scan.txrm" -p "ImageInfo/ExpTimes" -t float32 --raw
```


### `txm-to-nrrd.py`

```text
usage: txm-to-nrrd.py [-h] -i INPUT_TXM_FILE -o OUTPUT_NRRD_FILE [-v]

Convert reconstructed Zeiss txm to NRRD format.

NOTE that this does not set the voxel size properly - I need to do some investigating on this.

options:
    -h, --help            show this help message and exit
    -i INPUT_TXM_FILE, --input-txm-file INPUT_TXM_FILE
                            Input Zeiss txm file
    -o OUTPUT_NRRD_FILE, --output-nrrd-file OUTPUT_NRRD_FILE
                            Output NRRD file to save data
    -v, --verbose         Enable verbose output
```

### `txm-to-tiff.py`

```text
usage: txm-to-tiff.py [-h] -i INPUT_TXM_FILE [-p PREFIX] [-o OUTPUT_DIR] [-v]

Convert reconstructed Zeiss txm to TIFF format.

options:
    -h, --help            show this help message and exit
    -i INPUT_TXM_FILE, --input-txm-file INPUT_TXM_FILE
                            Input Zeiss txm file
    -p PREFIX, --prefix PREFIX
                            Filename prefix for output TIFF files
    -o OUTPUT_DIR, --output-dir OUTPUT_DIR
                            Output directory for TIFF files
    -v, --verbose         Enable verbose output
```

### `extract-pca-data.py`

Recursively searches a directory for Zeiss `.pca` files (INI-format scan parameter files) and
assembles selected fields into a single CSV. Extracted fields are: voxel size, exposure time,
frame averaging count, skip frames, X-ray voltage, and X-ray current.

```text
usage: extract-pca-data.py [-h] search_path [output]

positional arguments:
    search_path  Root directory to search for .pca files (searched recursively)
    output       Output CSV file path (default: pca_data.csv)

options:
    -h, --help   show this help message and exit
```

**Example:**

```bash
python extract-pca-data.py /data/scans all_scans.csv
```

### `subsample_segment.py`

Creates a subsampled segmentation NRRD for [biomedisa](https://biomedisa.info/) Smart Interpolation.
Reads a 3D Slicer `.seg.nrrd` segmentation, merges all segments into a single label volume, then
retains only every Nth slice along the specified axes (zeroing all other voxels). The output can be
passed directly to `biomedisa.interpolation` with `--allaxis`.

```text
usage: subsample_segment.py [-h] [--step [AXIS,STEP ...]]
                            [--segments NAME [NAME ...]] [-o OUTPUT]
                            SEGMENTATION.seg.nrrd

positional arguments:
    SEGMENTATION.seg.nrrd Path to the 3D Slicer segmentation file

options:
    -h, --help            show this help message and exit
    --step [AXIS,STEP ...]
                          Keep one slice every STEP slices along AXIS. Provide one or
                          more AXIS,STEP pairs (no spaces around comma). Valid axes: 0,
                          1, 2. STEP must be >= 2. Omit entirely to sample all 3 axes
                          at step 15.
    --segments NAME [NAME ...]
                          Names of segments to include in the merged volume.
                          If omitted, all segments are included.
    -o OUTPUT, --output OUTPUT
                          Output .seg.nrrd filename (default: subsampled_<stem>.seg.nrrd
                          next to the input)
```

**Examples:**

```bash
# All 3 axes at default step of 15:
python subsample_segment.py brain.seg.nrrd

# Axis 0 only, every 10 slices:
python subsample_segment.py brain.seg.nrrd --step 0,10

# Axis 0 every 10, axis 1 every 15:
python subsample_segment.py brain.seg.nrrd --step 0,10 1,15

# All axes with mixed steps, custom output name:
python subsample_segment.py brain.seg.nrrd --step 0,10 1,15 2,10 -o seeds.seg.nrrd

# Include only specific segments by name:
python subsample_segment.py brain.seg.nrrd --segments "Endocast" "Cranium"

# Then run biomedisa (--allaxis required for multi-axis seeds):
python -m biomedisa.interpolation scan.nrrd subsampled_brain.seg.nrrd --allaxis
```

### `restore_segmentation_dimensions.py`

Restores a cropped `.seg.nrrd` to the full dimensions of an original segmentation,
reversing the crop that 3D Slicer applies on save. This is needed before running biomedisa
Smart Interpolation, which requires the input label segmentation to be the same dimensions
as the volume. For some reason, Slicer crops the edited subsampled segmentation on save
even when directed not to (this is a bug).

The script reads the target dimensions from the original segmentation, computes the voxel-space
offset from the shifted space origin, embeds the cropped data in a zero-padded array of the correct
size, and writes a corrected `.seg.nrrd` with the origin restored to `(0, 0, 0)`.

```text
usage: restore_segmentation_dimensions.py [-h] ORIGINAL CROPPED OUTPUT

positional arguments:
    ORIGINAL    Original segmentation file (provides target dimensions)
    CROPPED     Cropped segmentation file to restore (e.g. output from biomedisa)
    OUTPUT      Output path for the restored file

options:
    -h, --help  show this help message and exit
```

**Example:**

```bash
python restore_segmentation_dimensions.py \
    Segmentation.seg.nrrd \
    biomedisa_result.seg.nrrd \
    restored_Segmentation.seg.nrrd
```

**Typical workflow:**

1. `subsample_segment.py` — thin out a Slicer segmentation to create sparse seeds
2. `biomedisa.interpolation` — run Smart Interpolation to fill in the full volume
3. `restore_segmentation_dimensions.py` — pad the biomedisa output back to the original dimensions so it re-loads correctly in Slicer


### `ge_scan_db.py`

**What it does**

Crawls a directory tree of GE / phoenix|x-ray CT metadata files (`.pca`
acquisition parameters, `.pcr` reconstruction parameters) and builds a SQLite
database with **one record per scan**, to enable reports and searches on scan
metadata. Standard library only — no extra packages beyond a base Python.

- **One record per scan-root.** Every `.pca` that is *not* inside a `ScanN`
  subdirectory becomes one record. Sibling folders for the same specimen (e.g.
  `… fem`, `… fib`, `… tibia`) are recorded as separate scans.
- **Multi-scan detection by directory structure.** Larger specimens scanned in
  pieces and stitched together have `Scan1/`, `Scan2/`, … subdirectories. Those
  records are flagged `multiscan = 1` and each tile is stored as a sub-scan.
  (Detection uses the `ScanN` directories rather than the `[Multiscan] Active`
  flag, which is set even on single scans.)
- **pca/pcr overlap verification.** For each `.pca`/`.pcr` pair, every parameter
  present in both files is compared (numeric-aware, with tolerance) and any
  disagreement is logged to a `parse_issue` table rather than aborting the crawl.
- **Handles both file layouts.** Modern (`[Xray]`, `[Image]`, …) and legacy 2013
  (`[XRAY]`, `[ACQUISITION]`, `Voxelsize`, …) phoenix|x-ray layouts resolve to
  the same columns via case-insensitive lookup with fallback keys.

**Usage**

```bash
# Build the database from a tree of .pca/.pcr files
python ge_scan_db.py build  --root pca_test --db scans.sqlite [--force]

# Print a summary (scan counts, kV distribution, overlap mismatches)
python ge_scan_db.py report --db scans.sqlite

# Run an arbitrary SQL SELECT against the database
python ge_scan_db.py query  --db scans.sqlite \
    --sql "SELECT specimen, voltage_kv, voxel_size_x FROM scan WHERE multiscan=1"
```

**Schema**

- `scan` — one row per scan, with promoted acquisition columns (kV, µA, voxel
  size, FDD/FOD, magnification, image count, detector, …) and reconstruction
  columns (recon voxel size, volume/ROI dimensions, filter kernel).
- `subscan` — one row per `ScanN` tile of a multi-scan, linked to its `scan`.
- `raw_param` — every `section.key = value` from every file, so no metadata is
  lost and any field is queryable even if it was not promoted to a column.
- `parse_issue` — pca/pcr overlap mismatches and any parse problems.

### Globus utilities — `globus-tree.py`, `globus-find.py`, `globus-clone.py`

A small family of tools for browsing and pulling files from a
[Globus](https://www.globus.org/) collection (endpoint) using the Globus SDK,
without needing the data mounted locally. They are useful for inspecting a remote
data repository and selectively cloning files (e.g. just the `.pca`/`.pcr`
metadata files) to a local Globus Connect Personal endpoint.

**Authentication.** All three use the Globus Native App OAuth flow. On first run
you are prompted to visit a URL, log in, and paste back an authorization code.
Tokens are then cached in `~/.globus-tree-tokens.json` and shared by all three
tools, so subsequent runs don't require re-login. These scripts require the
`globus_sdk` package.

Common options: `-c/--collection-id` selects the source collection, `-p/--path`
sets the starting path on it, and `-d/--max-depth` limits how deep the recursion
descends (the starting path is depth 0).

#### `globus-tree.py`

Generates a `tree`-style directory listing for a path on a Globus collection and
writes it to a file.

```text
usage: globus-tree.py [-h] -c COLLECTION_ID [-p PATH] -o OUTPUT_FILE
                      [-d MAX_DEPTH]

options:
    -h, --help            show this help message and exit
    -c, --collection-id COLLECTION_ID
                          Globus collection (endpoint) ID
    -p, --path PATH       Starting path on the collection
    -o, --output-file OUTPUT_FILE
                          Output file for the tree
    -d, --max-depth MAX_DEPTH
                          Maximum directory depth to descend (default: unlimited)
```

#### `globus-find.py`

Recursively finds files matching a shell-style glob pattern on a Globus
collection path and prints the matching paths (optionally also writing them to a
file).

```text
usage: globus-find.py [-h] -c COLLECTION_ID [-p PATH] [-o OUTPUT_FILE]
                      [-d MAX_DEPTH] [-i]
                      pattern

positional arguments:
    pattern               Shell-style glob pattern to match file names against,
                          e.g. "*.pc[a,r]". Quote it so the shell doesn't expand it.

options:
    -h, --help            show this help message and exit
    -c, --collection-id COLLECTION_ID
                          Globus collection (endpoint) ID
    -p, --path PATH       Starting path on the collection
    -o, --output-file OUTPUT_FILE
                          Optional file to also write matching paths to
    -d, --max-depth MAX_DEPTH
                          Maximum directory depth to descend (default: unlimited)
    -i, --ignore-case     Match the pattern case-insensitively
```

#### `globus-clone.py`

Finds files matching a glob pattern on a source collection and clones them —
preserving their directory structure — to a destination Globus collection,
typically your local Globus Connect Personal collection. The matched files are
placed under `--dest-path`, recreating their paths relative to the source
`--path`. Use `-n/--dry-run` to preview what would be transferred.

```text
usage: globus-clone.py [-h] -c COLLECTION_ID [-p PATH] -C DEST_COLLECTION_ID
                       -P DEST_PATH [-d MAX_DEPTH] [-i] [-n] [-w] [-l LABEL]
                       pattern

positional arguments:
    pattern               Shell-style glob pattern to match file names against,
                          e.g. "*.pc[a,r]". Quote it so the shell doesn't expand it.

options:
    -h, --help            show this help message and exit
    -c, --collection-id COLLECTION_ID
                          Source Globus collection (endpoint) ID
    -p, --path PATH       Source starting path on the collection
    -C, --dest-collection-id DEST_COLLECTION_ID
                          Destination Globus collection (endpoint) ID -- typically
                          your local Globus Connect Personal collection.
    -P, --dest-path DEST_PATH
                          Destination base path. Matched files are placed under
                          here, recreating their paths relative to the source --path.
    -d, --max-depth MAX_DEPTH
                          Maximum directory depth to descend (default: unlimited)
    -i, --ignore-case     Match the pattern case-insensitively
    -n, --dry-run         List the matched files and where they would be cloned to,
                          but don't submit a transfer.
    -w, --wait            Wait for the transfer to finish before exiting.
    -l, --label LABEL     Label for the Globus transfer task (default: globus-clone)
```

**Example** — clone every `.pca` and `.pcr` metadata file from a remote
collection to a local endpoint, mirroring the directory structure:

```bash
python globus-clone.py "*.pc[a,r]" \
    -c SOURCE_COLLECTION_ID -p /data/scans \
    -C LOCAL_COLLECTION_ID  -P /home/me/pca_test
```

## Requirements

This repo contains a `amnh-zeiss-utils.yaml` file for creating an environment
with the required packages (and some extras that were used in development, such
as Jupyter notebook support). To be honest, I've had varying success with these
setups. If this doesn't work, just keep running the scripts, installing what is
missing as you go.

## Contact

Hollister Herhold, PhD  
Research Associate, Division of Invertebrate Zoology  
Post-doctoral Research Scientist, Department of Vertebrate Paleontology  
Research Scientist, Department of Astrophysics  
American Museum of Natural History  
hherhold@amnh.org  


