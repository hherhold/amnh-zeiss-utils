#!/usr/bin/env python

"""
tree-viewer.py

A PySide6 GUI for browsing and searching the directory listings produced by
globus-tree.py. The listing files are plain "tree"-style text, but they can be
very large (the John_Flynn depth-4 listing is 156 MB / 2.6 million entries), so
the whole viewer is built around a compact, array-backed index rather than
per-node Python objects.

Features:
  - WinDirStat-style expandable tree with per-folder item counts and a
    proportional "share of parent" bar.
  - Fast offline search (substring or glob) across every name in the listing.
  - Copy full Globus paths for feeding back into globus-clone.py / globus-find.py.

By Hollister Herhold, AMNH, 2026.
Claude Opus 5 used for initial authoring.

"""

import argparse
import hashlib
import os
import re
import sys
from pathlib import Path

import numpy as np

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTreeView, QTableView, QLineEdit, QComboBox, QPushButton, QLabel,
    QCheckBox, QSplitter, QFileDialog, QMessageBox, QProgressDialog,
    QStatusBar, QMenu, QStyle, QStyledItemDelegate, QHeaderView,
    QAbstractItemView, QSizePolicy,
)
from PySide6.QtCore import (
    Qt, QAbstractItemModel, QAbstractTableModel, QModelIndex, QThread,
    Signal, QRect, QSettings,
)
from PySide6.QtGui import QAction, QColor, QKeySequence, QGuiApplication


# ---------------------------------------------------------------------------
# Listing file format
# ---------------------------------------------------------------------------

# globus-tree.py draws each entry as <prefix><connector><name>, where the
# prefix is a run of "|   " / "    " units (4 characters each) and the
# connector is "|-- " or "`-- ". In UTF-8 the box-drawing characters are three
# bytes each, so a "|   " unit is 6 bytes while a "    " unit is only 4. We
# therefore recover the depth from the byte length of the prefix plus a count
# of the vertical bars in it (see parse_tree_file).
CONN = b"\xe2\x94\x80\xe2\x94\x80 "   # "-- " (the two dashes and the space)
PIPE = b"\xe2\x94\x82"               # "|"   (vertical bar in a prefix)
CONN_LEN = len(CONN)                 # bytes from the first dash to the name
CONNECTOR_HEAD = 3                   # bytes of the "|" / "`" before the dashes

CACHE_VERSION = 1
CACHE_DIR = Path.home() / ".tree-viewer-cache"

# Columns
COL_NAME, COL_FILES, COL_FOLDERS, COL_SHARE = range(4)
COLUMN_LABELS = ["Name", "Files", "Folders", "Share of parent"]

SHARE_ROLE = Qt.ItemDataRole.UserRole + 1
NODE_ROLE = Qt.ItemDataRole.UserRole + 2


def human(n):
    """Format an integer with thousands separators."""
    return f"{n:,}"


def percent(frac):
    """Format a fraction, keeping tiny-but-nonzero shares distinguishable."""
    if frac <= 0:
        return ""
    if frac >= 0.0995:
        return f"{frac:.0%}"
    if frac >= 0.001:
        return f"{frac:.1%}"
    return "<0.1%"


# ---------------------------------------------------------------------------
# The parsed listing
# ---------------------------------------------------------------------------

class TreeIndex:
    """A parsed tree listing, stored as parallel numpy arrays.

    Node 0 is a synthetic root representing the header line of the listing
    (e.g. "AMNH Collections - VP Research:/John_Flynn"). Every other node is
    one entry from the file, in the order it appears -- which is depth-first
    pre-order, a property several of the derived arrays rely on.

    Names are not materialised as Python strings; each node stores an offset
    and length into `raw`, the raw bytes of the file, and is decoded on demand.
    """

    def __init__(self, path, raw, name_off, name_len, depth, is_dir, parent,
                 root_label):
        self.path = path
        self.raw = raw
        self.name_off = name_off
        self.name_len = name_len
        self.depth = depth
        self.is_dir = is_dir
        self.parent = parent
        self.root_label = root_label

        self.count = len(depth)
        self.scan_depth = int(depth.max()) if self.count else 0

        # The collection name and base path from the header line, used to
        # rebuild absolute Globus paths.
        sep = root_label.rfind(":/")
        if sep >= 0:
            self.collection = root_label[:sep]
            self.base_path = root_label[sep + 1:]
        else:
            self.collection = root_label
            self.base_path = "/"

        self._build_children()
        self._build_counts()

    # -- derived arrays ----------------------------------------------------

    def _build_children(self):
        """Build the child lists, grouped by parent, in original file order.

        A stable argsort by parent id groups every node's children together
        while preserving the file's ordering within each group (directories
        first, then files, both alphabetical).
        """
        n = self.count
        self.child_count = np.bincount(self.parent[1:], minlength=n).astype(np.int32)
        self.child_start = np.zeros(n, np.int64)
        self.child_start[1:] = np.cumsum(self.child_count[:-1], dtype=np.int64)

        self.natural_order = (np.argsort(self.parent[1:], kind="stable") + 1).astype(np.int32)
        self.child_idx = self.natural_order.copy()
        self._recompute_rows()

    def _recompute_rows(self):
        """Recompute each node's row number within its parent."""
        self.row_in_parent = np.zeros(self.count, np.int32)
        if self.count > 1:
            parents = self.parent[self.child_idx]
            rows = np.arange(len(self.child_idx), dtype=np.int64) - self.child_start[parents]
            self.row_in_parent[self.child_idx] = rows.astype(np.int32)

    def _build_counts(self):
        """Compute the number of files and folders beneath every node.

        Because the nodes are in pre-order, a node's subtree occupies a
        contiguous range [i, subtree_end[i]). The end of that range is the
        next node at the same or shallower depth, which we find with one
        vectorised pass per depth level.
        """
        n = self.count
        subtree_end = np.full(n, n, np.int64)
        for d in range(self.scan_depth + 1):
            at_d = np.flatnonzero(self.depth == d)
            if not len(at_d):
                continue
            le_d = np.flatnonzero(self.depth <= d)
            pos = np.searchsorted(le_d, at_d, side="right")
            ends = np.where(pos < len(le_d), le_d[np.minimum(pos, len(le_d) - 1)], n)
            subtree_end[at_d] = ends
        self.subtree_end = subtree_end

        # Prefix sums let us read off any subtree's totals in constant time.
        cum_files = np.zeros(n + 1, np.int64)
        cum_files[1:] = np.cumsum(~self.is_dir, dtype=np.int64)
        cum_dirs = np.zeros(n + 1, np.int64)
        cum_dirs[1:] = np.cumsum(self.is_dir, dtype=np.int64)

        idx = np.arange(n)
        self.desc_files = (cum_files[subtree_end] - cum_files[idx + 1]).astype(np.int64)
        self.desc_dirs = (cum_dirs[subtree_end] - cum_dirs[idx + 1]).astype(np.int64)

        # "Weight" is what the share bars are drawn from: a file counts as one
        # item, a folder counts as every file beneath it. A node's children's
        # weights therefore sum to its own.
        self.weight = self.desc_files + (~self.is_dir)

    # -- accessors ---------------------------------------------------------

    def name(self, i):
        """The display name of node `i`."""
        if i == 0:
            return self.root_label
        o = int(self.name_off[i])
        return self.raw[o:o + int(self.name_len[i])].decode("utf-8", "replace")

    def children(self, i):
        """The child node ids of node `i`, in current sort order."""
        s = int(self.child_start[i])
        return self.child_idx[s:s + int(self.child_count[i])]

    def ancestors(self, i):
        """Node ids from the root down to (but excluding) `i`."""
        chain = []
        p = int(self.parent[i])
        while p >= 0:
            chain.append(p)
            p = int(self.parent[p])
        chain.reverse()
        return chain

    def full_path(self, i):
        """The absolute Globus path of node `i`."""
        parts = [self.name(a) for a in self.ancestors(i)[1:]]
        if i != 0:
            parts.append(self.name(i))
        base = self.base_path.rstrip("/")
        return base + "/" + "/".join(parts) if parts else (base or "/")

    def is_unscanned(self, i):
        """True if this folder is empty *or* was cut off by the depth limit.

        globus-tree.py's --max-depth lists a directory but does not descend
        into it, so a childless folder at the deepest scanned level is
        ambiguous; we cannot tell "empty" from "not looked at".
        """
        return (bool(self.is_dir[i]) and self.child_count[i] == 0
                and int(self.depth[i]) >= self.scan_depth)

    def sort_children(self, column, descending):
        """Reorder every node's children by the given column."""
        if self.count <= 1:
            return
        nodes = np.arange(1, self.count, dtype=np.int32)
        natural = self.row_in_parent_natural
        parents = self.parent[1:]

        if column == COL_NAME:
            # The file's own order is already directories-first alphabetical;
            # reversing it per parent is what a descending name sort means.
            key = natural if not descending else -natural.astype(np.int64)
            perm = np.lexsort((key, parents))
        else:
            if column == COL_FILES:
                key = self.desc_files[1:]
            elif column == COL_FOLDERS:
                key = self.desc_dirs[1:]
            else:
                key = self.weight[1:]
            key = -key if descending else key
            perm = np.lexsort((natural, key, parents))

        self.child_idx = nodes[perm]
        self._recompute_rows()

    @property
    def row_in_parent_natural(self):
        """Row numbers in the listing's original order (the sort tiebreaker)."""
        if not hasattr(self, "_natural_rows"):
            parents = self.parent[self.natural_order]
            rows = (np.arange(len(self.natural_order), dtype=np.int64)
                    - self.child_start[parents])
            full = np.zeros(self.count, np.int64)
            full[self.natural_order] = rows
            self._natural_rows = full
        return self._natural_rows[1:]


def parse_tree_file(path, progress=None):
    """Parse a globus-tree.py listing into a TreeIndex.

    `progress` is called as progress(lines_done, lines_total) and may return
    False to abort the parse (in which case None is returned).
    """
    with open(path, "rb") as f:
        raw = f.read()

    # Line ends first; everything else is expressed as offsets into `raw`.
    line_ends = np.flatnonzero(np.frombuffer(raw, dtype=np.uint8) == 0x0A).tolist()
    if not line_ends or line_ends[-1] != len(raw) - 1:
        line_ends.append(len(raw))          # tolerate a missing final newline
    total = len(line_ends)

    find, count = raw.find, raw.count

    # The first line is the "collection:/path" header unless the file starts
    # straight in on the tree, in which case we name the root after the file.
    first_end = line_ends[0]
    if first_end and raw[first_end - 1] == 0x0D:
        first_end -= 1
    if find(CONN, 0, first_end) < 0:
        root_label = raw[:first_end].decode("utf-8", "replace").strip()
        start_line = 1
        start = line_ends[0] + 1
    else:
        root_label = Path(path).name
        start_line = 0
        start = 0

    offs, lens, deps, dirs, pars = [], [], [], [], []
    ao, al, ad, adir, ap = (offs.append, lens.append, deps.append,
                            dirs.append, pars.append)

    # Node 0 is the synthetic root; its name is held separately in root_label.
    ao(0); al(0); ad(0); adir(True); ap(-1)

    last_at = [0] + [-1] * 63     # deepest node seen so far at each depth
    node = 1

    for li in range(start_line, total):
        end = line_ends[li]
        if end > start and raw[end - 1] == 0x0D:
            end -= 1

        j = find(CONN, start, end)
        if j >= 0:
            # Depth from the prefix: p = 6 * (bars) + 4 * (blank units), and
            # depth is one more than the number of units.
            bars = count(PIPE, start, j)
            prefix_bytes = j - start - CONNECTOR_HEAD
            d = 1 + bars + (prefix_bytes - 6 * bars) // 4

            o = j + CONN_LEN
            ln = end - o
            is_directory = ln > 0 and raw[end - 1] == 0x2F   # trailing "/"
            if is_directory:
                ln -= 1

            if 1 <= d < 64:
                # A malformed prefix could point at a depth we have not seen;
                # hang such an entry off the root rather than corrupt the tree.
                parent_node = last_at[d - 1]
                ao(o); al(ln); ad(d); adir(is_directory)
                ap(parent_node if parent_node >= 0 else 0)
                last_at[d] = node
                node += 1

        start = line_ends[li] + 1

        if progress is not None and (li & 0x1FFFF) == 0:
            if progress(li, total) is False:
                return None

    if node == 1:
        raise ValueError("No tree entries found -- is this a globus-tree.py "
                         "listing?")

    return TreeIndex(
        path=path,
        raw=raw,
        name_off=np.array(offs, np.int64),
        name_len=np.array(lens, np.int32),
        depth=np.array(deps, np.int8),
        is_dir=np.array(dirs, bool),
        parent=np.array(pars, np.int32),
        root_label=root_label,
    )


# ---------------------------------------------------------------------------
# Parse cache
# ---------------------------------------------------------------------------

def cache_path_for(path):
    """Where the parsed index for `path` is cached."""
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}|{CACHE_VERSION}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"{Path(path).stem}-{digest}.npz"


def load_cached(path):
    """Return a TreeIndex rebuilt from cache, or None if unavailable."""
    try:
        cache = cache_path_for(path)
        if not cache.exists():
            return None
        with np.load(cache, allow_pickle=False) as z:
            with open(path, "rb") as f:
                raw = f.read()
            return TreeIndex(
                path=path, raw=raw,
                name_off=z["name_off"], name_len=z["name_len"],
                depth=z["depth"], is_dir=z["is_dir"], parent=z["parent"],
                root_label=str(z["root_label"]),
            )
    except Exception:
        return None


def save_cache(ti):
    """Persist the primary arrays; derived ones are cheap to recompute."""
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        np.savez(cache_path_for(ti.path),
                 name_off=ti.name_off, name_len=ti.name_len, depth=ti.depth,
                 is_dir=ti.is_dir, parent=ti.parent,
                 root_label=np.array(ti.root_label))
    except Exception:
        pass      # a missing cache only costs us a re-parse


# ---------------------------------------------------------------------------
# Background workers
# ---------------------------------------------------------------------------

class ParseWorker(QThread):
    """Parses a listing off the GUI thread."""

    progressed = Signal(int, int)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(self, path, use_cache=True):
        super().__init__()
        self.path = path
        self.use_cache = use_cache
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            ti = load_cached(self.path) if self.use_cache else None
            if ti is None:
                ti = parse_tree_file(self.path, progress=self._progress)
                if ti is None:
                    return                      # cancelled
                if self.use_cache:
                    save_cache(ti)
            self.finished_ok.emit(ti)
        except Exception as e:
            self.failed.emit(str(e))

    def _progress(self, done, total):
        self.progressed.emit(done, total)
        return not self._cancel


class SearchWorker(QThread):
    """Searches every name in the listing, off the GUI thread.

    Matching runs against the raw bytes of the file rather than per-node
    Python strings, which keeps a full search of 2.6 million names well under
    a second.
    """

    finished_ok = Signal(object, bool)      # node ids, hit the result cap
    failed = Signal(str)

    MAX_RESULTS = 200_000

    def __init__(self, ti, text, mode, case_sensitive, kind, lower_buf):
        super().__init__()
        self.ti = ti
        self.text = text
        self.mode = mode                    # "contains" or "glob"
        self.case_sensitive = case_sensitive
        self.kind = kind                    # "all", "files", "folders"
        self.lower_buf = lower_buf          # cached lowercased copy, or None
        self.built_lower = None

    def run(self):
        try:
            ti = self.ti
            if self.case_sensitive:
                buf = ti.raw
            else:
                if self.lower_buf is None:
                    self.lower_buf = ti.raw.lower()
                    self.built_lower = self.lower_buf
                buf = self.lower_buf

            needle = self.text if self.case_sensitive else self.text.lower()
            pattern = needle.encode("utf-8")

            if self.mode == "glob":
                nodes = self._search_glob(ti, buf, pattern)
            else:
                nodes = self._search_contains(ti, buf, pattern)

            # Narrow to the requested kind before capping, so that asking for
            # "folders only" cannot be starved by a flood of matching files.
            if self.kind == "files":
                nodes = nodes[~ti.is_dir[nodes]]
            elif self.kind == "folders":
                nodes = nodes[ti.is_dir[nodes]]

            capped = len(nodes) > self.MAX_RESULTS
            if capped:
                nodes = nodes[:self.MAX_RESULTS]

            self.finished_ok.emit(nodes, capped)
        except Exception as e:
            self.failed.emit(str(e))

    def _search_contains(self, ti, buf, pattern):
        """Find every occurrence of `pattern`, then map offsets back to nodes."""
        hits = []
        pos = buf.find(pattern)
        while pos >= 0:
            hits.append(pos)
            pos = buf.find(pattern, pos + 1)
        if not hits:
            return np.empty(0, np.int64)

        hits = np.array(hits, np.int64)
        # name_off is ascending (nodes are in file order), so the node owning
        # an offset is the last one starting at or before it.
        idx = np.searchsorted(ti.name_off, hits, side="right") - 1
        idx = np.clip(idx, 0, ti.count - 1)
        # Discard hits that landed in the tree glyphs rather than in a name.
        ends = ti.name_off[idx] + ti.name_len[idx]
        keep = (hits >= ti.name_off[idx]) & (hits + len(pattern) <= ends) & (idx > 0)
        return np.unique(idx[keep])

    def _search_glob(self, ti, buf, pattern):
        """Match a shell-style glob against whole names.

        Anchoring the regex between the "-- " connector and the end of the
        line makes it match a complete entry name and nothing else.
        """
        out = []
        for ch in pattern.decode("utf-8", "replace"):
            if ch == "*":
                out.append(r"[^\r\n/]*")
            elif ch == "?":
                out.append(r"[^\r\n/]")
            else:
                out.append(re.escape(ch))
        body = "".join(out).encode("utf-8")
        rx = re.compile(re.escape(CONN) + body + rb"/?\r?\n")

        nodes = []
        for m in rx.finditer(buf):
            off = m.start() + CONN_LEN
            i = int(np.searchsorted(ti.name_off, off, side="right")) - 1
            if 0 < i < ti.count and ti.name_off[i] == off:
                nodes.append(i)
        return np.array(nodes, np.int64) if nodes else np.empty(0, np.int64)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class TreeModel(QAbstractItemModel):
    """Exposes a TreeIndex to a QTreeView.

    Every QModelIndex carries its node id in `internalId`, so navigating the
    tree is pure array lookup -- nothing is materialised per node.
    """

    def __init__(self, ti, parent=None):
        super().__init__(parent)
        self.ti = ti
        style = QApplication.style()
        self.dir_icon = style.standardIcon(QStyle.StandardPixmap.SP_DirIcon)
        self.file_icon = style.standardIcon(QStyle.StandardPixmap.SP_FileIcon)
        self.unscanned_icon = style.standardIcon(
            QStyle.StandardPixmap.SP_DirLinkIcon)
        self._sort_column = COL_NAME
        self._sort_order = Qt.SortOrder.AscendingOrder

    # -- structure ---------------------------------------------------------

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            return self.createIndex(row, column, 0)
        pid = parent.internalId()
        node = int(self.ti.child_idx[int(self.ti.child_start[pid]) + row])
        return self.createIndex(row, column, node)

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        node = index.internalId()
        if node == 0:
            return QModelIndex()
        p = int(self.ti.parent[node])
        if p <= 0:
            return self.createIndex(0, 0, 0)
        return self.createIndex(int(self.ti.row_in_parent[p]), 0, p)

    def rowCount(self, parent=QModelIndex()):
        if parent.column() > 0:
            return 0
        if not parent.isValid():
            return 1
        return int(self.ti.child_count[parent.internalId()])

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMN_LABELS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (orientation == Qt.Orientation.Horizontal
                and role == Qt.ItemDataRole.DisplayRole):
            return COLUMN_LABELS[section]
        return None

    # -- content -----------------------------------------------------------

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        ti = self.ti
        node = index.internalId()
        col = index.column()

        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_NAME:
                return ti.name(node)
            if not ti.is_dir[node]:
                return None
            if col == COL_FILES:
                if ti.is_unscanned(node):
                    return "not scanned"
                return human(int(ti.desc_files[node]))
            if col == COL_FOLDERS:
                if ti.is_unscanned(node):
                    return None
                return human(int(ti.desc_dirs[node]))
            return None

        if role == Qt.ItemDataRole.DecorationRole and col == COL_NAME:
            if not ti.is_dir[node]:
                return self.file_icon
            return self.unscanned_icon if ti.is_unscanned(node) else self.dir_icon

        if role == SHARE_ROLE and col == COL_SHARE:
            p = int(ti.parent[node])
            if p < 0:
                return 1.0
            total = int(ti.weight[p])
            return (int(ti.weight[node]) / total) if total else 0.0

        if role == NODE_ROLE:
            return node

        if role == Qt.ItemDataRole.TextAlignmentRole and col in (COL_FILES,
                                                                 COL_FOLDERS):
            return int(Qt.AlignmentFlag.AlignRight
                       | Qt.AlignmentFlag.AlignVCenter)

        if role == Qt.ItemDataRole.ToolTipRole:
            bits = [ti.full_path(node)]
            if ti.is_dir[node]:
                if ti.is_unscanned(node):
                    bits.append("Empty, or beyond the listing's depth limit "
                                f"(depth {ti.scan_depth}).")
                else:
                    bits.append(f"{human(int(ti.desc_files[node]))} files, "
                                f"{human(int(ti.desc_dirs[node]))} folders "
                                "beneath")
            total = int(ti.weight[0])
            if total:
                bits.append(f"{int(ti.weight[node]) / total:.3%} of all files")
            return "\n".join(bits)

        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        return (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)

    def hasChildren(self, parent=QModelIndex()):
        if not parent.isValid():
            return True
        return int(self.ti.child_count[parent.internalId()]) > 0

    # -- sorting -----------------------------------------------------------

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        if self.ti.count <= 1:
            return
        self._sort_column = column
        self._sort_order = order

        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        kept = [(i.internalId(), i.column()) for i in old]

        self.ti.sort_children(column, order == Qt.SortOrder.DescendingOrder)

        new = [self.createIndex(int(self.ti.row_in_parent[n]), c, n)
               if n != 0 else self.createIndex(0, c, 0)
               for n, c in kept]
        self.changePersistentIndexList(old, new)
        self.layoutChanged.emit()

    # -- helpers -----------------------------------------------------------

    def index_for_node(self, node, column=COL_NAME):
        """Build a QModelIndex for a node id."""
        if node == 0:
            return self.createIndex(0, column, 0)
        return self.createIndex(int(self.ti.row_in_parent[node]), column, node)


class ResultsModel(QAbstractTableModel):
    """The search-results table."""

    HEADERS = ["Name", "Type", "Location"]

    def __init__(self, ti, nodes, parent=None):
        super().__init__(parent)
        self.ti = ti
        self.nodes = nodes
        self.natural = nodes          # the order the search found them in
        self._ranks = {}              # column -> sort rank, computed once

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.nodes)

    def columnCount(self, parent=QModelIndex()):
        return len(self.HEADERS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if (orientation == Qt.Orientation.Horizontal
                and role == Qt.ItemDataRole.DisplayRole):
            return self.HEADERS[section]
        return None

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        node = int(self.nodes[index.row()])
        if role == Qt.ItemDataRole.DisplayRole:
            col = index.column()
            if col == 0:
                return self.ti.name(node)
            if col == 1:
                return "Folder" if self.ti.is_dir[node] else "File"
            parent = int(self.ti.parent[node])
            return self.ti.full_path(parent) if parent >= 0 else "/"
        if role == NODE_ROLE:
            return node
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.ti.full_path(node)
        return None

    def node_at(self, row):
        return int(self.nodes[row])

    # -- sorting -----------------------------------------------------------

    def sort(self, column, order=Qt.SortOrder.AscendingOrder):
        """Reorder the results by a column, or restore the found order.

        A column outside the table (Qt passes -1 when no sort indicator is
        set) means "as the search found them", which is tree order.
        """
        if len(self.natural) < 2:
            return
        if 0 <= column < len(self.HEADERS):
            rank = self._rank_for(column)
            if order == Qt.SortOrder.DescendingOrder:
                rank = -rank
            new = self.natural[np.argsort(rank, kind="stable")]
        else:
            new = self.natural
        if np.array_equal(new, self.nodes):
            return

        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()
        kept = [(int(self.nodes[i.row()]), i.column()) for i in old]
        self.nodes = new
        row_of = {int(n): r for r, n in enumerate(new)}
        self.changePersistentIndexList(
            old, [self.createIndex(row_of[n], c) for n, c in kept])
        self.layoutChanged.emit()

    def _rank_for(self, column):
        """An integer sort rank per row; equal ranks keep the found order.

        Ranks rather than the values themselves, so that descending is just a
        negation and ties stay stable in both directions.
        """
        if column in self._ranks:
            return self._ranks[column]

        ti, nodes = self.ti, self.natural
        if column == 1:
            # Folders before files, matching the order the tree itself uses.
            rank = (~ti.is_dir[nodes]).astype(np.int64)
        elif column == 0:
            rank = self._rank_of_strings(
                [ti.name(int(n)).lower() for n in nodes])
        else:
            # One path per distinct parent folder rather than one per row:
            # a listing has a few thousand folders but the results can run to
            # hundreds of thousands of rows.
            uniq, inverse = np.unique(ti.parent[nodes], return_inverse=True)
            folders = self._rank_of_strings(
                [ti.full_path(int(p)).lower() for p in uniq])
            rank = folders[inverse]

        self._ranks[column] = rank
        return rank

    @staticmethod
    def _rank_of_strings(values):
        """Map a list of strings to their 0..n-1 sorted positions."""
        order = sorted(range(len(values)), key=values.__getitem__)
        rank = np.empty(len(values), np.int64)
        rank[order] = np.arange(len(values), dtype=np.int64)
        return rank


class ShareBarDelegate(QStyledItemDelegate):
    """Draws the WinDirStat-style proportional bar in the Share column."""

    def paint(self, painter, option, index):
        frac = index.data(SHARE_ROLE)
        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else QApplication.style()
        # Let the style paint the row background (and selection) first.
        option.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option,
                          painter, option.widget)
        if frac is None:
            return

        frac = max(0.0, min(1.0, float(frac)))
        rect = option.rect.adjusted(4, 4, -4, -4)
        if rect.width() <= 0 or rect.height() <= 0:
            return

        painter.save()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(210, 210, 210, 120))
        painter.drawRect(rect)
        filled = QRect(rect)
        filled.setWidth(max(1, int(rect.width() * frac)) if frac > 0 else 0)
        if filled.width() > 0:
            painter.setBrush(QColor(70, 130, 190))
            painter.drawRect(filled)

        painter.setPen(option.palette.text().color())
        painter.drawText(rect, int(Qt.AlignmentFlag.AlignRight
                                   | Qt.AlignmentFlag.AlignVCenter),
                         percent(frac) + " ")
        painter.restore()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class TreeViewerWindow(QMainWindow):

    EXPAND_WARN_ROWS = 200_000

    def __init__(self, use_cache=True):
        super().__init__()
        self.ti = None
        self.model = None
        self.results_model = None
        self.use_cache = use_cache
        self.parse_worker = None
        self.search_worker = None
        self.progress = None
        self.lower_buf = None
        self.results_sort = None      # column and order, once the user picks one
        self.settings = QSettings("AMNH", "tree-viewer")

        self.setWindowTitle("Tree Viewer")
        self.resize(1150, 780)
        self.setAcceptDrops(True)

        self._build_ui()
        self._build_menus()
        self._set_enabled(False)

    # -- construction ------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(6, 6, 6, 6)

        # Search bar
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Find:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(
            "Name to search for -- press Enter (try *.nrrd in Glob mode)")
        self.search_edit.returnPressed.connect(self.run_search)
        self.search_edit.setSizePolicy(QSizePolicy.Policy.Expanding,
                                       QSizePolicy.Policy.Fixed)
        bar.addWidget(self.search_edit, 1)

        self.mode_combo = QComboBox()
        self.mode_combo.addItems(["Contains", "Glob"])
        bar.addWidget(self.mode_combo)

        self.kind_combo = QComboBox()
        self.kind_combo.addItems(["All", "Files only", "Folders only"])
        bar.addWidget(self.kind_combo)

        self.case_check = QCheckBox("Match case")
        bar.addWidget(self.case_check)

        self.search_button = QPushButton("Search")
        self.search_button.clicked.connect(self.run_search)
        bar.addWidget(self.search_button)

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_search)
        bar.addWidget(self.clear_button)
        layout.addLayout(bar)

        # Tree above, search results below
        self.splitter = QSplitter(Qt.Orientation.Vertical)

        self.tree = QTreeView()
        self.tree.setUniformRowHeights(True)          # essential at this scale
        self.tree.setAlternatingRowColors(True)
        self.tree.setSortingEnabled(True)
        self.tree.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self.show_tree_menu)
        self.tree.setItemDelegateForColumn(COL_SHARE, ShareBarDelegate(self))
        self.splitter.addWidget(self.tree)

        results_box = QWidget()
        rlayout = QVBoxLayout(results_box)
        rlayout.setContentsMargins(0, 0, 0, 0)
        self.results_label = QLabel("No search run yet.")
        rlayout.addWidget(self.results_label)
        self.results = QTableView()
        self.results.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.results.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.results.verticalHeader().setVisible(False)
        self.results.setSortingEnabled(True)
        results_header = self.results.horizontalHeader()
        # Start with no sort indicator, so results first appear in the order
        # the search found them (tree order) until a header is clicked.
        results_header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        results_header.sortIndicatorChanged.connect(self.on_results_sort_changed)
        self.results.doubleClicked.connect(self.reveal_result)
        self.results.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.results.customContextMenuRequested.connect(self.show_results_menu)
        rlayout.addWidget(self.results)
        self.results_panel = results_box
        self.splitter.addWidget(results_box)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        results_box.hide()

        layout.addWidget(self.splitter, 1)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.status_totals = QLabel("")
        self.statusBar().addPermanentWidget(self.status_totals)
        self.statusBar().showMessage("Open a globus-tree.py listing to begin.")

    def _build_menus(self):
        file_menu = self.menuBar().addMenu("&File")
        act_open = QAction("&Open listing...", self)
        act_open.setShortcut(QKeySequence.StandardKey.Open)
        act_open.triggered.connect(self.choose_file)
        file_menu.addAction(act_open)

        self.recent_menu = file_menu.addMenu("Open &recent")
        self._rebuild_recent_menu()

        file_menu.addSeparator()
        act_quit = QAction("E&xit", self)
        act_quit.setShortcut(QKeySequence.StandardKey.Quit)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        view_menu = self.menuBar().addMenu("&View")
        for depth in (1, 2, 3, 4):
            act = QAction(f"Expand to depth &{depth}", self)
            act.triggered.connect(lambda _=False, d=depth: self.expand_to_depth(d))
            view_menu.addAction(act)
        view_menu.addSeparator()
        act_collapse = QAction("&Collapse all", self)
        act_collapse.triggered.connect(self.collapse_all)
        view_menu.addAction(act_collapse)

        act_focus = QAction("&Find", self)
        act_focus.setShortcut(QKeySequence.StandardKey.Find)
        act_focus.triggered.connect(self.search_edit.setFocus)
        view_menu.addAction(act_focus)

        help_menu = self.menuBar().addMenu("&Help")
        act_about = QAction("&About", self)
        act_about.triggered.connect(self.show_about)
        help_menu.addAction(act_about)

    def _set_enabled(self, on):
        for w in (self.search_edit, self.search_button, self.clear_button,
                  self.mode_combo, self.kind_combo, self.case_check):
            w.setEnabled(on)

    # -- opening files -----------------------------------------------------

    def choose_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Open tree listing", "",
            "Tree listings (*.txt);;All files (*)")
        if path:
            self.open_file(path)

    def open_file(self, path):
        if self.parse_worker is not None and self.parse_worker.isRunning():
            return
        path = str(Path(path))
        if not os.path.exists(path):
            QMessageBox.warning(self, "Tree Viewer", f"No such file:\n{path}")
            return

        size_mb = os.path.getsize(path) / (1024 * 1024)
        self.progress = QProgressDialog(
            f"Reading {Path(path).name} ({size_mb:,.0f} MB)...",
            "Cancel", 0, 100, self)
        self.progress.setWindowTitle("Tree Viewer")
        self.progress.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress.setMinimumDuration(300)
        self.progress.setValue(0)

        self.parse_worker = ParseWorker(path, use_cache=self.use_cache)
        self.parse_worker.progressed.connect(self.on_parse_progress)
        self.parse_worker.finished_ok.connect(self.on_parse_done)
        self.parse_worker.failed.connect(self.on_parse_failed)
        self.progress.canceled.connect(self.parse_worker.cancel)
        self.parse_worker.start()

    def on_parse_progress(self, done, total):
        if self.progress is not None and total:
            self.progress.setValue(int(100 * done / total))

    def on_parse_done(self, ti):
        if self.progress is not None:
            self.progress.reset()
            self.progress = None

        self.ti = ti
        self.lower_buf = None
        self.model = TreeModel(ti, self)
        self.tree.setModel(self.model)
        self.tree.selectionModel().currentChanged.connect(self.on_current_changed)

        header = self.tree.header()
        header.setSectionResizeMode(COL_NAME, QHeaderView.ResizeMode.Interactive)
        # Never size-to-contents here: it would walk every one of the rows.
        self.tree.setColumnWidth(COL_NAME, 560)
        self.tree.setColumnWidth(COL_FILES, 110)
        self.tree.setColumnWidth(COL_FOLDERS, 110)
        self.tree.setColumnWidth(COL_SHARE, 160)
        header.setSortIndicator(COL_NAME, Qt.SortOrder.AscendingOrder)

        self.clear_search()
        self._set_enabled(True)
        self.tree.expand(self.model.index(0, 0, QModelIndex()))
        self.tree.setCurrentIndex(self.model.index(0, 0, QModelIndex()))

        self.setWindowTitle(f"Tree Viewer -- {Path(ti.path).name}")
        self.status_totals.setText(
            f"{human(int(ti.desc_files[0]))} files, "
            f"{human(int(ti.desc_dirs[0]))} folders, "
            f"scanned to depth {ti.scan_depth}")
        self.statusBar().showMessage(ti.root_label)
        self._remember_recent(ti.path)

    def on_parse_failed(self, message):
        if self.progress is not None:
            self.progress.reset()
            self.progress = None
        QMessageBox.critical(self, "Tree Viewer",
                             f"Could not read that listing:\n\n{message}")

    # -- recent files ------------------------------------------------------

    def _remember_recent(self, path):
        recent = [p for p in self.settings.value("recent", [], list)
                  if p != path]
        recent.insert(0, path)
        self.settings.setValue("recent", recent[:8])
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self):
        self.recent_menu.clear()
        recent = self.settings.value("recent", [], list) or []
        if not recent:
            act = QAction("(none)", self)
            act.setEnabled(False)
            self.recent_menu.addAction(act)
            return
        for path in recent:
            act = QAction(path, self)
            act.triggered.connect(lambda _=False, p=path: self.open_file(p))
            self.recent_menu.addAction(act)

    # -- searching ---------------------------------------------------------

    def run_search(self):
        if self.ti is None:
            return
        text = self.search_edit.text().strip()
        if not text:
            self.clear_search()
            return
        if self.search_worker is not None and self.search_worker.isRunning():
            return

        mode = "glob" if self.mode_combo.currentText() == "Glob" else "contains"
        kind = {"All": "all", "Files only": "files",
                "Folders only": "folders"}[self.kind_combo.currentText()]

        self.results_label.setText("Searching...")
        self.results_panel.show()
        self.search_button.setEnabled(False)
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        self.search_worker = SearchWorker(
            self.ti, text, mode, self.case_check.isChecked(), kind,
            self.lower_buf)
        self.search_worker.finished_ok.connect(self.on_search_done)
        self.search_worker.failed.connect(self.on_search_failed)
        self.search_worker.start()

    def on_search_done(self, nodes, capped):
        QGuiApplication.restoreOverrideCursor()
        self.search_button.setEnabled(True)
        if self.search_worker is not None:
            # Reuse the lowercased buffer the worker built, if any.
            self.lower_buf = self.search_worker.built_lower or self.lower_buf

        self.results_model = ResultsModel(self.ti, nodes, self)
        self.results.setModel(self.results_model)
        self.results.setColumnWidth(0, 340)
        self.results.setColumnWidth(1, 70)
        header = self.results.horizontalHeader()
        header.setStretchLastSection(True)
        # Carry whatever sort the user last picked over to the new results.
        if self.results_sort is not None:
            self.results.sortByColumn(*self.results_sort)

        note = f"{human(len(nodes))} match" + ("" if len(nodes) == 1 else "es")
        if capped:
            note += f" (stopped at the first {human(SearchWorker.MAX_RESULTS)})"
        note += " -- double-click a row to show it in the tree."
        self.results_label.setText(note)

    def on_search_failed(self, message):
        QGuiApplication.restoreOverrideCursor()
        self.search_button.setEnabled(True)
        self.results_label.setText("Search failed.")
        QMessageBox.warning(self, "Tree Viewer", f"Search failed:\n\n{message}")

    def on_results_sort_changed(self, column, order):
        """Remember the chosen sort so later searches come back the same way."""
        self.results_sort = (column, order) if column >= 0 else None

    def clear_search(self):
        self.search_edit.clear()
        self.results_panel.hide()
        self.results.setModel(None)
        self.results_model = None
        self.results_label.setText("No search run yet.")

    def reveal_result(self, index):
        if self.results_model is None or not index.isValid():
            return
        self.reveal_node(self.results_model.node_at(index.row()))

    def reveal_node(self, node):
        """Expand the tree down to `node` and select it."""
        for ancestor in self.ti.ancestors(node):
            self.tree.expand(self.model.index_for_node(ancestor))
        idx = self.model.index_for_node(node)
        self.tree.setCurrentIndex(idx)
        self.tree.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtCenter)
        self.tree.setFocus()

    # -- view actions ------------------------------------------------------

    def expand_to_depth(self, depth):
        if self.ti is None:
            return
        rows = int(np.count_nonzero(self.ti.depth <= depth))
        if rows > self.EXPAND_WARN_ROWS:
            answer = QMessageBox.question(
                self, "Tree Viewer",
                f"Expanding to depth {depth} would show {human(rows)} rows, "
                "which may take a while.\n\nExpand anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.tree.expandToDepth(depth - 1)
        finally:
            QGuiApplication.restoreOverrideCursor()

    def collapse_all(self):
        self.tree.collapseAll()
        if self.model is not None:
            self.tree.expand(self.model.index(0, 0, QModelIndex()))

    def on_current_changed(self, current, _previous):
        if not current.isValid() or self.ti is None:
            return
        node = current.internalId()
        path = self.ti.full_path(node)
        if self.ti.is_dir[node] and not self.ti.is_unscanned(node):
            path += (f"    [{human(int(self.ti.desc_files[node]))} files, "
                     f"{human(int(self.ti.desc_dirs[node]))} folders]")
        self.statusBar().showMessage(path)

    # -- context menus -----------------------------------------------------

    def show_tree_menu(self, pos):
        index = self.tree.indexAt(pos)
        if not index.isValid():
            return
        node = index.internalId()
        menu = QMenu(self)
        menu.addAction("Copy name",
                       lambda: self._to_clipboard(self.ti.name(node)))
        menu.addAction("Copy full path",
                       lambda: self._to_clipboard(self.ti.full_path(node)))
        menu.addAction(
            "Copy collection:path",
            lambda: self._to_clipboard(
                f"{self.ti.collection}:{self.ti.full_path(node)}"))
        if self.ti.is_dir[node] and self.ti.child_count[node]:
            menu.addSeparator()
            menu.addAction("Expand this branch",
                           lambda: self._expand_branch(index))
            menu.addAction("Collapse this branch",
                           lambda: self.tree.collapse(index))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _expand_branch(self, index):
        node = index.internalId()
        rows = int(self.ti.subtree_end[node] - node)
        if rows > self.EXPAND_WARN_ROWS:
            answer = QMessageBox.question(
                self, "Tree Viewer",
                f"That branch holds {human(rows)} entries.\n\nExpand anyway?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.tree.expandRecursively(index)
        finally:
            QGuiApplication.restoreOverrideCursor()

    def show_results_menu(self, pos):
        index = self.results.indexAt(pos)
        if not index.isValid() or self.results_model is None:
            return
        node = self.results_model.node_at(index.row())
        menu = QMenu(self)
        menu.addAction("Show in tree", lambda: self.reveal_node(node))
        menu.addAction("Copy full path",
                       lambda: self._to_clipboard(self.ti.full_path(node)))
        menu.addAction("Copy all matching paths", self._copy_all_results)
        menu.exec(self.results.viewport().mapToGlobal(pos))

    def _copy_all_results(self):
        if self.results_model is None:
            return
        nodes = self.results_model.nodes
        if len(nodes) > 50_000:
            answer = QMessageBox.question(
                self, "Tree Viewer",
                f"Copy all {human(len(nodes))} paths to the clipboard?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return
        self._to_clipboard("\n".join(self.ti.full_path(int(n)) for n in nodes))

    def _to_clipboard(self, text):
        QGuiApplication.clipboard().setText(text)
        self.statusBar().showMessage(f"Copied: {text[:120]}", 4000)

    # -- misc --------------------------------------------------------------

    def show_about(self):
        QMessageBox.about(
            self, "About Tree Viewer",
            "<b>Tree Viewer</b><br><br>"
            "Browses and searches the directory listings written by "
            "globus-tree.py.<br><br>"
            "Folders show the number of files and folders beneath them, and "
            "the share bar gives each entry's fraction of its parent's file "
            "count.<br><br>"
            "AMNH, 2026.")

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if urls:
            self.open_file(urls[0].toLocalFile())

    def closeEvent(self, event):
        if self.parse_worker is not None and self.parse_worker.isRunning():
            self.parse_worker.cancel()
            self.parse_worker.wait(3000)
        if self.search_worker is not None and self.search_worker.isRunning():
            self.search_worker.wait(3000)
        event.accept()


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Browse and search a globus-tree.py directory listing.")
    parser.add_argument("listing", nargs="?",
                        help="Tree listing file to open on startup")
    parser.add_argument("--no-cache", action="store_true",
                        help="Do not read or write the parsed-listing cache "
                             f"in {CACHE_DIR}")
    args = parser.parse_args()

    app = QApplication(sys.argv)
    window = TreeViewerWindow(use_cache=not args.no_cache)
    window.show()
    if args.listing:
        window.open_file(args.listing)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
