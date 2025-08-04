#!/bin/env python

import argparse
import xrmreader

# Handle command line arguments.

parser = argparse.ArgumentParser(description="Extract metadata from a Zeiss txrm file.")

parser.add_argument("-i", "--input-txrm-file", help="Input Zeiss txrm file", 
                    required=True)
args = parser.parse_args()

def main():
    metadata = xrmreader.read_metadata(args.input_txrm_file)
    print(metadata)

if __name__ == "__main__":
    main()
