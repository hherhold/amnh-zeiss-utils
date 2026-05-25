# amnh-zeiss-utils

Warning: This repo is somewhat mis-named as there are utilities for other tasks such as
handling slicer segmentation files, etc. It's a bit of a dumping ground for scripts used
to facilitate CT analysis. With that out of the way...

Utilities for handling Zeiss txm and txrm files, specifically micro-CT files.
These python scripts allow extraction of metadata from unreconstructed
('`txrm`') and reconstructed ('`txm`') files, as well as converting
reconstructed ('`txm`') files to TIFF stacks or NRRD files. (You can convert
unreconstruced files too, not sure why you'd want to.) There are also some
utilities for OLE files (the format Zeiss uses for these files.) 

It is assumed that you are familiar with running python scripts from the command
line and setting up conda environments. A YAML file (`amnh-zeiss-utils.yaml`) is
included with the required packages. (But see Requirements section, below.)

Note that this repo does not rely on any Zeiss proprietary libraries and is
standalone (apart from setting up python dependencies in an environment, see
below) and you can run scripts here on any machine. This means that changes to
Zeiss' proprietary file format may break this code.

## Programs

### `txrm-monitor.py`

A PySide6 GUI application that monitors directories for new `.txrm` and/or `.txm`
files and automatically extracts metadata when files are stable. This was implemented
using Claude Sonnet 4.5 and 4.6 inside VS Code with Github Copilot.

**Features:**
- Monitors configured directories recursively (scans subdirectories)
- Configurable scan interval (default 5 minutes) and file-stability window (default 10 minutes)
- Monitors `.txrm` files, `.txm` files, or both — selectable in Preferences
- Automatically extracts metadata when a file has not changed size for the stability duration
- Saves metadata to `.txrm.txt` / `.txm.txt` files alongside source files
- Daily-rotated logging to a configurable log directory (default `logs/`)
- Real-time log viewer in GUI
- Status bar updated after each scan or processing operation
- Countdown timer for next scan
- Manual "Scan Now" button for immediate scanning
- "Process Selected Now" button to force immediate metadata extraction for a selected file
- Drag-and-drop support: drop `.txrm` / `.txm` files **or folders** onto the window for immediate processing (no stability wait); folders are scanned recursively and all matching files are processed
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
5. Drag and drop `.txrm` / `.txm` files **or folders** onto the window to process them immediately (folders are scanned recursively)

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

### `subsample_segment.py`

Creates a subsampled segmentation NRRD for [biomedisa](https://biomedisa.info/) Smart Interpolation.
Reads a 3D Slicer `.seg.nrrd` segmentation, merges all segments into a single label volume, then
retains only every Nth slice along the specified axes (zeroing all other voxels). The output can be
passed directly to `biomedisa.interpolation` with `--allaxis`.

```text
usage: subsample_segment.py [-h] [--step [AXIS,STEP ...]] [-o OUTPUT]
                            CT_VOLUME.nrrd SEGMENTATION.seg.nrrd

positional arguments:
    CT_VOLUME.nrrd        Path to the CT scan NRRD file (passed unchanged to biomedisa)
    SEGMENTATION.seg.nrrd Path to the 3D Slicer segmentation file

options:
    -h, --help            show this help message and exit
    --step [AXIS,STEP ...]
                          Keep one slice every STEP slices along AXIS. Provide one or
                          more AXIS,STEP pairs (no spaces around comma). Valid axes: 0,
                          1, 2. STEP must be >= 2. Omit entirely to sample all 3 axes
                          at step 15.
    -o OUTPUT, --output OUTPUT
                          Output .seg.nrrd filename (default: subsampled_<stem>.seg.nrrd
                          next to the input)
```

**Examples:**

```bash
# All 3 axes at default step of 15:
python subsample_segment.py scan.nrrd brain.seg.nrrd

# Axis 0 only, every 10 slices:
python subsample_segment.py scan.nrrd brain.seg.nrrd --step 0,10

# Axis 0 every 10, axis 1 every 15:
python subsample_segment.py scan.nrrd brain.seg.nrrd --step 0,10 1,15

# All axes with mixed steps, custom output name:
python subsample_segment.py scan.nrrd brain.seg.nrrd --step 0,10 1,15 2,10 -o seeds.seg.nrrd

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


