Write a script that extracts data from files ending in .pca and assembles this information into a CSV file.

 - The .pca files are located in subdirectories. The script should take a path as an argument and begin searching there.
 - You can use the file "Anthribidae_.pca" in the current directory as an example .pca file.
 - The quantities to be extracted from the .pca file are the following:
     - in the [Geometry] section: VoxelSizeX. (VoxelSizeY is identical, these are isometric voxels.)
     - In the [Detector] section: TimingVal, Avg, and Skip.
     - In the [Xray] section: Voltage, Current
 - The first column of the CSV file should be the full path to the .pca file. The following columns should be the extracted quantities mentioned above.
 
  