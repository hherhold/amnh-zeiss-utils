#!/bin/env python

'''

get-metadata-from-txrm.py

Extract metadata from a Zeiss txrm file.

By Hollister Herhold, AMNH, 2025/2026. I used github copilot for much of the
boilerplate code. As of March 2026, much of the logic was refactored using
Claude Sonnet 4.6.

'''

import argparse
import struct
import sys
import olefile

# Handle command line arguments.

parser = argparse.ArgumentParser(description="Extract metadata from a Zeiss txrm file.")

parser.add_argument("-i", "--input-txrm-file", help="Input Zeiss txrm file", 
                    required=True)
parser.add_argument("-o", "--output-file", help="Output file to save metadata", 
                    required=False, default=None)
parser.add_argument("-v", "--verbose", help="Enable verbose output",
                    action="store_true", default=False)
parser.add_argument("-f", "--fields", help="Comma-separated list of fields to extract",
                    required=False, default=None)
parser.add_argument("-a", "--all", help="Extract all available metadata fields",
                    action="store_true", default=False)


args = parser.parse_args()

# Default fields shown when neither -f nor -a is specified.
# Keys are the OLE directory paths under ImageInfo/.
# Fields only in .txrm files will show "Not found in metadata" when a .txm is read.
#
# Objective ID:  3 = 4X,  5 = 20X
default_fields = [
    # --- fields present in both .txm and .txrm ---
    'ImageInfo/ImageWidth',
    'ImageInfo/ImageHeight',
    'ImageInfo/DataType',
    'ImageInfo/NoOfImages',
    'ImageInfo/PixelSize',
    'ImageInfo/ConeAngle',
    'ImageInfo/FanAngle',
    'ImageInfo/CameraOffset',
    'ImageInfo/Current',
    'ImageInfo/Voltage',
    'ImageInfo/ExpTimes',          # per-image array; first value reported
    'ImageInfo/StoRADistance',     # per-image array; first value reported
    'ImageInfo/DtoRADistance',     # per-image array; first value reported
    'ImageInfo/ObjectiveID',
    'ImageInfo/ObjectiveName',
    'ImageInfo/SourceFilterName',
    'ImageInfo/CameraBinning',
    'ImageInfo/CameraName',
    'ImageInfo/Date',
    'ImageInfo/SystemType',
    'ImageInfo/XrayCurrent',
    'ImageInfo/XrayVoltage',
    # --- fields present only in .txrm files ---
    'ImageInfo/CCVersion',
    'ImageInfo/SourceDriftTotal',
    'ImageInfo/SourceType',
    'ImageInfo/SourceSerialNumber',
    'ImageInfo/Filament',
    'ImageInfo/FilamentPercent',
    'ImageInfo/TubeEfficiency',
    'ImageInfo/TubeState',
]

# ---------------------------------------------------------------------------
# All ImageInfo fields, keyed by OLE path.
# Value tuple: (struct_fmt, 'value') for numeric streams,
#              (None, 'string') for text streams.
# For per-image array streams the first element is read via struct.unpack_from.
# Fields present only in .txrm return None when the file is a .txm.
# ---------------------------------------------------------------------------
_IMAGEINFO_FIELDS = {
    # -- common to .txm and .txrm --
    'ImageInfo/AcquisitionMode':        ('<I', 'value'),
    'ImageInfo/CamFullHeight':          ('<I', 'value'),
    'ImageInfo/CamFullWidth':           ('<I', 'value'),
    'ImageInfo/CamPixelSize':           ('<f', 'value'),
    'ImageInfo/CameraBinning':          ('<I', 'value'),
    'ImageInfo/CameraFineRotation':     ('<f', 'value'),
    'ImageInfo/CameraName':             (None, 'string'),
    'ImageInfo/CameraNo':               ('<I', 'value'),
    'ImageInfo/CameraOffset':           ('<f', 'value'),
    'ImageInfo/CameraTemperature':      ('<f', 'value'),
    'ImageInfo/CameraType':             (None, 'string'),
    'ImageInfo/ConeAngle':              ('<f', 'value'),
    'ImageInfo/Current':                ('<f', 'value'),
    'ImageInfo/DataType':               ('<I', 'value'),
    'ImageInfo/Date':                   (None, 'string'),
    'ImageInfo/DtoRADistance':          ('<f', 'value'),
    'ImageInfo/Energy':                 ('<f', 'value'),
    'ImageInfo/ExpTimes':               ('<f', 'value'),
    'ImageInfo/FanAngle':               ('<f', 'value'),
    'ImageInfo/FileType':               ('<I', 'value'),
    'ImageInfo/FocusTarget':            ('<f', 'value'),
    'ImageInfo/FramesAveraged':         ('<I', 'value'),
    'ImageInfo/FramesPerImage':         ('<I', 'value'),
    'ImageInfo/HorizontalBin':          ('<I', 'value'),
    'ImageInfo/ImageHeight':            ('<I', 'value'),
    'ImageInfo/ImageWidth':             ('<I', 'value'),
    'ImageInfo/ImagesPerProjection':    ('<I', 'value'),
    'ImageInfo/ImagesTaken':            ('<I', 'value'),
    'ImageInfo/IonChamberCurrent':      ('<f', 'value'),
    'ImageInfo/IsContMotion':           ('<I', 'value'),
    'ImageInfo/IsFlatPanel':            ('<I', 'value'),
    'ImageInfo/NoOfImages':             ('<I', 'value'),
    'ImageInfo/NoOfImagesAveraged':     ('<I', 'value'),
    'ImageInfo/ObjectiveID':            ('<I', 'value'),
    'ImageInfo/ObjectiveName':          (None, 'string'),
    'ImageInfo/OpticalMagnification':   ('<f', 'value'),
    'ImageInfo/PixelSize':              ('<f', 'value'),
    'ImageInfo/RefInterval':            ('<I', 'value'),
    'ImageInfo/SourceFilterID':         ('<I', 'value'),
    'ImageInfo/SourceFilterIndex':      ('<I', 'value'),
    'ImageInfo/SourceFilterName':       (None, 'string'),
    'ImageInfo/StoRADistance':          ('<f', 'value'),
    'ImageInfo/SystemType':             (None, 'string'),
    'ImageInfo/Temperature':            ('<f', 'value'),
    'ImageInfo/VerticalalBin':          ('<I', 'value'),
    'ImageInfo/Voltage':                ('<f', 'value'),
    'ImageInfo/XrayCurrent':            ('<f', 'value'),
    'ImageInfo/XrayMagnification':      ('<f', 'value'),
    'ImageInfo/XrayVoltage':            ('<f', 'value'),
    # -- present only in .txrm --
    'ImageInfo/AutoGridOn':             ('<I', 'value'),
    'ImageInfo/CCFilAdjustStep':        ('<f', 'value'),
    'ImageInfo/CCVersion':              (None, 'string'),
    'ImageInfo/ColdCathodeState':       ('<I', 'value'),
    'ImageInfo/Filament':               (None, 'string'),
    'ImageInfo/FilamentPercent':        ('<f', 'value'),
    'ImageInfo/GridOffset':             ('<f', 'value'),
    'ImageInfo/GridVoltage':            ('<f', 'value'),
    'ImageInfo/IsCCOn':                 ('<I', 'value'),
    'ImageInfo/RequestedFilament':      ('<f', 'value'),
    'ImageInfo/RequestedPower':         ('<f', 'value'),
    'ImageInfo/RequestedTargetCurrent': ('<f', 'value'),
    'ImageInfo/SourceDriftInterval':    ('<I', 'value'),
    'ImageInfo/SourceDriftTotal':       ('<f', 'value'),
    'ImageInfo/SourceSerialNumber':     (None, 'string'),
    'ImageInfo/SourceType':             (None, 'string'),
    'ImageInfo/SpotIndex':              ('<I', 'value'),
    'ImageInfo/TargetTurn':             ('<I', 'value'),
    'ImageInfo/TFMIsOn':                ('<I', 'value'),
    'ImageInfo/TubeEfficiency':         ('<f', 'value'),
    'ImageInfo/TubeState':              ('<I', 'value'),
}


def _read_ole_value(ole, path, fmt):
    """Read the first scalar value from an OLE stream using struct format fmt.
    Returns None if the stream does not exist or cannot be unpacked."""
    if not ole.exists(path):
        return None
    data = ole.openstream(path).read()
    try:
        return struct.unpack_from(fmt, data)[0]
    except struct.error:
        return None


def _read_ole_string(ole, path):
    """Read a null-terminated string from an OLE stream.

    Detects UTF-16-LE (alternating zero bytes) and falls back to UTF-8/ASCII.
    Returns None if the stream does not exist or cannot be decoded.
    """
    if not ole.exists(path):
        return None
    data = ole.openstream(path).read()
    if not data:
        return None
    # Detect UTF-16-LE by checking for alternating null bytes at the start.
    if len(data) >= 4 and data[1] == 0 and data[3] == 0 and data[0] != 0:
        # Find the double-null terminator on an even boundary.
        i = 0
        while i + 1 < len(data):
            if data[i] == 0 and data[i + 1] == 0:
                break
            i += 2
        try:
            return data[:i].decode('utf-16-le')
        except UnicodeDecodeError:
            pass
    # UTF-8 / ASCII: truncate at the first null byte.
    end = data.find(b'\x00')
    if end >= 0:
        data = data[:end]
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError:
        return None


def read_metadata(file_name):
    """Read ImageInfo metadata from a Zeiss .txrm or .txm file.

    Keys in the returned dictionary are OLE paths (e.g. 'ImageInfo/ImageWidth').
    Fields present only in .txrm files return None when a .txm file is read.
    """
    if not olefile.isOleFile(file_name):
        print(f"Error: '{file_name}' is not a valid OLE file.", file=sys.stderr)
        sys.exit(1)

    with olefile.OleFileIO(file_name) as ole:
        metadata = {}
        for path, (fmt, reader) in _IMAGEINFO_FIELDS.items():
            if reader == 'string':
                metadata[path] = _read_ole_string(ole, path)
            else:
                metadata[path] = _read_ole_value(ole, path, fmt)

    return metadata


def get_field_from_metadata(metadata, field_name):
    """
    Get a specific field from the metadata dictionary.
    
    :param metadata: Dictionary containing metadata fields.
    :param field_name: Name of the field to retrieve.
    :return: Value of the specified field or None if not found.
    """
    return metadata.get(field_name, None)

def print_all_available_fields(metadata):
    print("Available metadata fields:")
    for key in metadata.keys():
        print(f"{key}: {metadata[key]}")

def print_selected_fields(metadata, fields):
    print("Selected metadata fields:")
    for field in fields:
        value = get_field_from_metadata(metadata, field)
        if value is not None:
            print(f"{field}: {value}")
        else:
            print(f"{field}: Not found in metadata")

def main():
    if args.verbose:
        print("Verbose mode enabled.")
        print("Input file:", args.input_txrm_file)
        if args.output_file:
            print("Output file:", args.output_file)

    metadata = read_metadata(args.input_txrm_file)

    if args.verbose:
        print("Metadata read successfully.")

    if args.all:
        print_all_available_fields(metadata)
        return

    if args.fields:
        fields = [field.strip() for field in args.fields.split(',')]
    else:
        fields = default_fields

    print_selected_fields(metadata, fields)

if __name__ == "__main__":
    main()
