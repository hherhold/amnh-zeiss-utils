# GE scan database requirements

- For the following task, you can examine the files in the pca_test subdirectory.

- This subdirectory is a clone of a data repository that contains the scans and all
  images, however this clone only contains the directory structure and .pca and .pcr
  files, which hold metadata for the scans: .PCA files for acquisition parameters, and
  .PCR are reconsruction parameters.

- I need a tool that scans all the files in this directory tree and creates a database
  from the files. The goal is to be able to store metadata for the scans to enable reports
  and searches on metadata parameters.

- The goal is to have a single record per scan. Some scans have multiple subdirectories,
    named 'Scan1', 'Scan2', and so on that are multi-scans. These are for larger specimens
    that are scanned in pieces and then stitched together. There is typically a .pca file
    at the same level as the 'Scan1', 'Scan2', etc directories that has a section that
    has: 
      [Multiscan]
      Active=1
    pca_test\AMNH 647 Hyaenodon paucidens is an example of this.

 - Multi-scanned specimens should be notated as such. (Not every scan is a multi-scan, but
   many are.)

 - .pca and .pcr files have some overlapping parameters. Verify that they match before
   merging into the database.

 - A small SQLite database is probably appropriate for this. It needs to be runnable from
   the command line in the amnh-zeiss-utils conda environment (which can be added to as
   necessary).