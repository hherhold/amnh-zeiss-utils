#!/bin/env python

'''
txm-to-tiff.py

Convert Zeiss txm metadata to NRRD format.

By Hollister Herhold, AMNH, 2025. I used github copilot for much of the boilerplate code.

'''

import argparse
import xrmreader
import tifffile
import os


# Handle command line arguments.
parser = argparse.ArgumentParser(description="Convert reconstructed Zeiss txm to TIFF format.")

parser.add_argument("-i", "--input-txm-file", help="Input Zeiss txm file", 
                    required=True)

parser.add_argument("-v", "--verbose", help="Enable verbose output",
                    action="store_true", default=False)

args = parser.parse_args()

def main():
    # Read the txm file using xrmreader
    scan_volume = xrmreader.read_txm(args.input_txm_file)

    # Strip the file extension from the input file name to create a prefix for output TIFF files.
    output_tiff_prefix = args.input_txm_file.rsplit('.', 1)[0]

    # Change the data type to 16 bit unsigned integers.
    scan_volume = scan_volume.astype('uint16')

    # Print out the shape of the scan volume.
    if args.verbose:
        print(f"Scan volume shape: {scan_volume.shape}")

    # Create the output directory if it does not exist.
    output_dir = "TIFF"
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    for i in range(scan_volume.shape[0]):
        slice_i = scan_volume[i, :, :]
        filename = f"TIFF/{output_tiff_prefix}_{i:04d}.tiff"
        tifffile.imwrite(filename, slice_i)

if __name__ == "__main__":
    main()
