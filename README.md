# amnh-zeiss-utils

Utilities for handling Zeiss txm and txrm files. These python scripts allow display of
metadata from unreconstructed ('`txrm`') files, as well as converting reconstructed
('`txm`') files to TIFF stacks or NRRD files.

It is assumed that you are familiar with running python scripts from the command line and
setting up conda environments. A YAML file (`amnh-zeiss-utils.yaml`) is included with the
required packages.

## Programs

### `get-metadata-from-txrm.py`

```
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


### `txm-to-nrrd.py`

```
usage: txm-to-nrrd.py [-h] -i INPUT_TXM_FILE -o OUTPUT_NRRD_FILE [-v]

Convert reconstructed Zeiss txm to NRRD format.

options:
  -h, --help            show this help message and exit
  -i INPUT_TXM_FILE, --input-txm-file INPUT_TXM_FILE
                        Input Zeiss txm file
  -o OUTPUT_NRRD_FILE, --output-nrrd-file OUTPUT_NRRD_FILE
                        Output NRRD file to save data
  -v, --verbose         Enable verbose output
```

### `txm-to-tiff.py`

```
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


## Requirements

This repo contains a `amnh-zeiss-utils.yaml` file for creating an environment with the
required packages (and some extras that were used in development, such as Jupyter notebook
support). In particular, `xrmreader` is required, along with anything else that it
requires. TIFF file handling also requires `tifffile`.

## Known issues

The "Objective" field in the txrm file is not yet handled.

## Contact

Hollister Herhold, PhD  
Research Associate, Division of Invertebrate Zoology  
Research Scientist, Department of Astrophysics  
American Museum of Natural History  
hherhold@amnh.org  


