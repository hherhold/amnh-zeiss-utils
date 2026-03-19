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

# The list of fields to grab by default. This can be extended as needed. You also
# can grab any fields by specifying them in the command line arguments. Using the
# -f/--fields option, you can provide a comma-separated list of fields to extract.
default_fields = [
    'image_width', 'image_height', 'data_type', 'number_of_images',
    'pixel_size', 'reference_exposure_time', 'reference_current',
    'reference_voltage', 'reference_data_type', 'image_data_type',
    'align-mode', 'center_shift', 'rotation_angle',
    'source_isocenter_distance', 'detector_isocenter_distance', 'cone_angle',
    'fan_angle', 'camera_offset', 'source_drift', 'current', 'voltage',
    'power', 'exposure_time', 'binning', 'filter', 
    'scaling_min', 'scaling_max', 'objective_id', 'objective_mag'
]

# Objective ID
# 3 = 4X
# 5 = 20X


def _read_ole_value(ole, path, fmt):
    """Read a single scalar value from an OLE stream using struct format fmt.
    Returns None if the stream does not exist or cannot be unpacked."""
    if not ole.exists(path):
        return None
    data = ole.openstream(path).read()
    try:
        return struct.unpack_from(fmt, data)[0]
    except struct.error:
        return None


def _read_ole_string(ole, path):
    """Read a string value from an OLE stream, stripping trailing null bytes.
    Tries UTF-8 first, then UTF-16-LE. Returns None if the stream does not exist."""
    if not ole.exists(path):
        return None
    data = ole.openstream(path).read()
    try:
        return data.decode('utf-8').rstrip('\x00')
    except UnicodeDecodeError:
        try:
            return data.decode('utf-16-le').rstrip('\x00')
        except UnicodeDecodeError:
            return None


def _detect_reference_prefix(ole):
    """Detect which reference data prefix is present in the OLE directory.
    Returns 'ReferenceData', 'MultiReferenceData', or None."""
    multi_found = False
    single_found = False
    for entry in ole.listdir():
        if 'MultiReferenceData' in entry:
            multi_found = True
        if 'ReferenceData' in entry:
            single_found = True
    # Prefer single-image reference over multi when both are present.
    if single_found:
        return 'ReferenceData'
    elif multi_found:
        return 'MultiReferenceData'
    return None


def read_metadata(file_name):
    """Read metadata from a Zeiss .txrm or .txm file using direct OLE calls.

    Returns a dictionary of metadata fields, or exits on error.
    """
    if not olefile.isOleFile(file_name):
        print(f"Error: '{file_name}' is not a valid OLE file.", file=sys.stderr)
        sys.exit(1)

    with olefile.OleFileIO(file_name) as ole:
        ref_prefix = _detect_reference_prefix(ole)

        # For multi-value streams (e.g. per-image distances) struct.unpack_from
        # with '<f' reads only the first float, matching the original behaviour.
        metadata = {
            'facility':                    _read_ole_string(ole, 'SampleInfo/Facility'),
            'image_width':                 _read_ole_value(ole, 'ImageInfo/ImageWidth', '<I'),
            'image_height':                _read_ole_value(ole, 'ImageInfo/ImageHeight', '<I'),
            'data_type':                   _read_ole_value(ole, 'ImageInfo/DataType', '<I'),
            'number_of_images':            _read_ole_value(ole, 'ImageInfo/NoOfImages', '<I'),
            'pixel_size':                  _read_ole_value(ole, 'ImageInfo/pixelsize', '<f'),
            'image_data_type':             _read_ole_value(ole, 'ImageInfo/DataType', '<I'),
            'align-mode':                  _read_ole_value(ole, 'alignment/AlignMode', '<I'),
            'center_shift':                _read_ole_value(ole, 'ReconSettings/CenterShift', '<f'),
            'rotation_angle':              _read_ole_value(ole, 'ReconSettings/RotationAngle', '<f'),
            'source_isocenter_distance':   _read_ole_value(ole, 'ImageInfo/StoRADistance', '<f'),
            'detector_isocenter_distance': _read_ole_value(ole, 'ImageInfo/DtoRADistance', '<f'),
            'cone_angle':                  _read_ole_value(ole, 'ImageInfo/ConeAngle', '<f'),
            'fan_angle':                   _read_ole_value(ole, 'ImageInfo/FanAngle', '<f'),
            'camera_offset':               _read_ole_value(ole, 'ImageInfo/CameraOffset', '<f'),
            'source_drift':                _read_ole_value(ole, 'ImageInfo/SourceDriftTotal', '<f'),
            'current':                     _read_ole_value(ole, 'ImageInfo/Current', '<f'),
            'voltage':                     _read_ole_value(ole, 'ImageInfo/Voltage', '<f'),
            'power':                       _read_ole_value(ole, 'AcquisitionSettings/SrcPower', '<f'),
            # ImageInfo/ExpTimes is an array (one value per image); all values are
            # identical so we read only the first float.
            'exposure_time': _read_ole_value(ole, 'ImageInfo/ExpTimes', '<f'),
            'binning':       _read_ole_value(ole, 'AcquisitionSettings/Binning', '<I'),
            'filter':        _read_ole_string(ole, 'AcquisitionSettings/SourceFilterName'),
            'scaling_min':   _read_ole_value(ole, 'GlobalMinMax/GlobalMin', '<f'),
            'scaling_max':   _read_ole_value(ole, 'GlobalMinMax/GlobalMax', '<f'),
            'objective_id':  _read_ole_value(ole, 'AcquisitionSettings/ObjectiveID', '<I'),
            'objective_mag': _read_ole_value(ole, 'AcquisitionSettings/ObjectiveMag', '<f'),
        }

        # Reference-data fields depend on which prefix was detected.
        if ref_prefix is not None:
            metadata['reference_exposure_time'] = _read_ole_value(ole, ref_prefix + '/ExpTime', '<f')
            metadata['reference_current']        = _read_ole_value(ole, ref_prefix + '/XrayCurrent', '<f')
            metadata['reference_voltage']        = _read_ole_value(ole, ref_prefix + '/XrayVoltage', '<f')
            metadata['reference_data_type']      = _read_ole_value(ole, ref_prefix + '/DataType', '<I')
        else:
            metadata['reference_exposure_time'] = None
            metadata['reference_current']        = None
            metadata['reference_voltage']        = None
            metadata['reference_data_type']      = None

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
