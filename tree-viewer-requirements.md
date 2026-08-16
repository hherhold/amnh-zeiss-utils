# Tree viewer requirements

- The `globus-tree.py` command walks a directory structure via the Globus API to
  basically implement a "remote ls", so that files can be searched for offline.

- The output of these runs is typically very large, with many directories and files.

- Create a Python-based GUI for navigating the output of the "tree" command. It should
  implement some kind of tree expansion structure similar to that implemented in
  WinDirStat (screen capture at WinDirStat.png).

- Examples of the output from the 'tree' command to be parsed include `tree.txt`,
  `tree_Aug_6_2026.txt`, and `flynn_tree_d4_Aug_16_2026.txt`.

- Use `amnh-zeiss-utils` as the conda environment for this. `txrm-monitor.py` is an
  existing GUI, it's preferred to use the same frameworks/etc for this rather than
  adding more requirements to this conda environment.

## Implementation notes

Implemented as `tree-viewer.py`. PySide6, same as `txrm-monitor.py`; the only other
dependency is numpy, which the environment already has. Nothing new was added to the
conda environment.

### Scale

The listings are the binding constraint on the design:

| Listing | Size | Entries |
| --- | --- | --- |
| `tree.txt` | 45 KB | 942 |
| `tree_Aug_6_2026.txt` | 1.5 MB | 32,827 |
| `flynn_tree_d4_Aug_16_2026.txt` | 156 MB | 2,594,263 |
| `flynn_tree_d5_Aug_16_2026.txt` | 395 MB | 6,073,934 |

The largest listing has 6.07 million entries, with single directories holding as many
as 9,304 children. That rules out the item-based `QTreeWidget` (a widget item per
entry would exhaust memory), so the viewer uses a `QAbstractItemModel` over a flat,
array-backed index:

- The file is read once as raw bytes and kept in memory. Each entry stores an offset
  and length into that buffer rather than a Python string; names are decoded only for
  the rows actually on screen.
- Entry depth is recovered from the byte length of the box-drawing prefix plus a count
  of the vertical bars in it: each prefix unit is four characters, but a bar followed
  by three spaces is 6 UTF-8 bytes where four spaces is only 4.
- Because the listing is already depth-first pre-order, each node's subtree is a
  contiguous range, so file/folder counts for every directory come from prefix sums
  instead of a per-node walk.

Measured timings and memory:

| | 156 MB / 2.59M | 395 MB / 6.07M |
| --- | --- | --- |
| Load | 4.5 s | 8 s |
| Reload from cache | 0.5 s | 1.3 s |
| Re-sort a column | 0.4 s | 1.5 s |
| Full-listing search | 0.2–0.6 s | 0.4–1.6 s |
| Memory, loaded | 0.35 GB | 0.85 GB |
| Memory, after first search | 0.51 GB | 1.24 GB |

Expanding a 5,023-child folder takes 0.09 s. Memory runs to roughly three times the
size of the listing: the raw file stays resident, plus about 35 bytes of index per
entry, plus a lowercased copy of the file that is built on the first
case-insensitive search and then reused.

Parsed listings are cached under `~/.tree-viewer-cache`, keyed on path, size and
mtime, which cuts a reopen of the 156 MB listing to 0.5 s. `--no-cache` disables it.

### Features

- WinDirStat-style expandable tree, directories before files.
- Per-folder counts of the files and folders beneath it, and a proportional
  "share of parent" bar, so the directories holding the data stand out.
- Sort siblings by name or by item count from the column headers.
- Search across every name, either substring or shell-style glob (`*.nrrd`),
  optionally case-sensitive and restricted to files or folders only. Matching runs
  against the raw byte buffer rather than per-entry strings, which is what keeps a
  full search of 2.6 million names under a second.
- Search results arrive in tree order and can be sorted by name, type or location
  from their column headers. Type ascending puts folders before files, matching the
  tree's own ordering. The chosen sort persists across searches. Sorting ranks each
  row once per column and caches it, and the location sort builds one path per
  distinct parent folder — a few thousand — rather than one per row, so even a
  200,000-row result set sorts in 0.05–0.2 s.
- Double-click a search result to expand the tree down to it.
- Copy an entry's full path, or `collection:path`, for pasting into `globus-clone.py`.
- Open a listing from the File menu, the command line, drag-and-drop, or the recent
  files list.

### Depth-limited listings

`globus-tree.py --max-depth` lists a directory at the limit but does not descend into
it, so a childless folder there is ambiguous — it may be empty, or simply never looked
at. Those folders are shown as **not scanned** with a distinct icon rather than being
reported as empty.

### Verification

`tree-viewer.py` was checked against all four listings:

- Every entry's depth, name and type was compared against an independent reference
  parser — all 2,594,263 entries of the depth-4 listing and all 6,073,934 of the
  depth-5 listing match.
- The computed root totals match the `N directories, M files` summary line that
  `globus-tree.py` writes.
- Structural invariants: children are exactly one level below their parent, child
  weights sum to the parent's, and row indices stay dense after re-sorting.
- Search results were verified against brute-force matching, and the hit counts
  cross-checked with `grep`.
- Edge cases: CRLF and LF listings, a missing final newline, listings with no header
  line, non-ASCII names, names containing the tree glyphs themselves, and non-listing
  input (which raises a clear error rather than showing an empty tree).
- Results sorting: each column ascending and descending, ties holding their found
  order, no row lost or duplicated, the selected row and double-click-to-reveal both
  following the sort, and the sort carrying over to the next search.
