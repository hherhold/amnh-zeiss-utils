#!/bin/env python

'''
globus-tree.py

Generate a directory tree (similar to the unix "tree" command) for a path on a
Globus collection, using the Globus SDK. The tree is written to an output file.

Authentication uses the Globus Native App OAuth flow. On first run you will be
prompted to visit a URL, log in, and paste back an authorization code. Tokens are
cached in ~/.globus-tree-tokens.json so subsequent runs don't require re-login.
Collections that require an additional data-access consent (e.g. Globus Connect
Server v5 mapped collections) will prompt you to log in again with the extra
scopes.

Run with -gui to get a PySide6 window instead: fields for each parameter, a
live status readout, a Stop button, and the last few sets of parameters saved
(in globus-tree-recent.json next to this script) for easy re-running. In the
GUI, logging in opens the Globus login page in your browser and asks for the
authorization code in a dialog.

By Hollister Herhold, AMNH, 2026.
Claude Opus 4.8 used for initial authoring.

'''

import argparse
import datetime
import json
import os
import shutil
import sys
import threading
import time
import uuid
import webbrowser

import globus_sdk
from globus_sdk.scopes import TransferScopes

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QComboBox, QPushButton, QLabel, QSpinBox, QPlainTextEdit,
    QProgressBar, QFileDialog, QMessageBox, QDialog, QDialogButtonBox,
    QGroupBox, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QGuiApplication

# Native app client ID. This is the public Globus tutorial/CLI client ID; replace
# with your own registered app's client ID if you prefer.
CLIENT_ID = "61338d24-54d5-408f-a10d-66c06b59f6d2"

TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".globus-tree-tokens.json")

# Tree-drawing glyphs (same characters the unix "tree" command uses).
TEE = "├── "
ELBOW = "└── "
PIPE = "│   "
SPACE = "    "


class Status:
    """Single-line status readout printed to stderr while the tree is walked.

    There's no way to know up front how much work there is (the tree is
    discovered as it's walked), so rather than a progress bar this shows a
    spinner, the running counts, elapsed time, and the directory currently
    being listed. The line is rewritten in place with a carriage return, so it
    stays on one line and never lands in the output file.

    All methods become no-ops when stderr isn't a terminal or when --quiet is
    given, so redirected runs stay clean."""

    SPINNER = "|/-\\"

    def __init__(self, enabled=True):
        self.enabled = enabled and sys.stderr.isatty()
        self.start = time.time()
        self.tick = 0
        self.line_len = 0

    def update(self, path, counts):
        """Redraw the status line for the directory currently being listed."""
        if not self.enabled:
            return
        elapsed = int(time.time() - self.start)
        head = (f"{self.SPINNER[self.tick % len(self.SPINNER)]} "
                f"{counts['dirs']} dirs, {counts['files']} files, "
                f"{elapsed // 60}m{elapsed % 60:02d}s  ")
        self.tick += 1

        width = shutil.get_terminal_size((80, 24)).columns - 1
        room = max(width - len(head), 0)
        if len(path) > room:
            # Keep the tail of the path -- the deep end is the informative part.
            path = "..." + path[-(room - 3):] if room > 3 else ""
        line = head + path
        # Pad to the previous length so a longer old line is fully erased.
        sys.stderr.write("\r" + line.ljust(self.line_len))
        sys.stderr.flush()
        self.line_len = len(line)

    def clear(self):
        """Erase the status line so other output isn't written on top of it."""
        if not self.enabled or not self.line_len:
            return
        sys.stderr.write("\r" + " " * self.line_len + "\r")
        sys.stderr.flush()
        self.line_len = 0

    def warn(self, message):
        """Print a warning to stderr without mangling the status line."""
        self.clear()
        print(message, file=sys.stderr)


def print_err(message):
    print(message, file=sys.stderr)


def load_tokens():
    """Load cached transfer tokens, or None if not present."""
    if not os.path.exists(TOKEN_FILE):
        return None
    with open(TOKEN_FILE) as f:
        return json.load(f)


def save_tokens(token_response):
    """Persist the transfer tokens from an OAuth token response."""
    tokens = token_response.by_resource_server["transfer.api.globus.org"]
    with open(TOKEN_FILE, "w") as f:
        json.dump(tokens, f)
    # Tokens grant access to your files -- keep the cache private.
    try:
        os.chmod(TOKEN_FILE, 0o600)
    except OSError:
        pass


def prompt_for_code(authorize_url):
    """Show the login URL on the terminal and read back the authorization
    code the user pastes in."""
    print("Please go to this URL and log in:\n")
    print(authorize_url + "\n")
    return input("Enter the authorization code here: ").strip()


def do_login_flow(scopes=None, get_code=prompt_for_code):
    """Run the Native App OAuth flow and return freshly minted transfer tokens.

    `scopes` is a list of requested scopes; it defaults to the full transfer
    scope. Pass additional (e.g. data-access) scopes when a collection needs
    consent beyond the base transfer scope. `get_code` is called with the login
    URL and returns the authorization code (the GUI swaps in a dialog)."""
    if scopes is None:
        scopes = [TransferScopes.all]

    auth_client = globus_sdk.NativeAppAuthClient(CLIENT_ID)
    auth_client.oauth2_start_flow(requested_scopes=scopes, refresh_tokens=True)

    auth_code = get_code(auth_client.oauth2_get_authorize_url())

    token_response = auth_client.oauth2_exchange_code_for_tokens(auth_code)
    save_tokens(token_response)
    return token_response.by_resource_server["transfer.api.globus.org"]


def get_transfer_client(get_code=prompt_for_code):
    """Return an authenticated TransferClient, logging in if needed."""
    tokens = load_tokens()
    if tokens is None:
        tokens = do_login_flow(get_code=get_code)

    auth_client = globus_sdk.NativeAppAuthClient(CLIENT_ID)
    authorizer = globus_sdk.RefreshTokenAuthorizer(
        tokens["refresh_token"], auth_client,
        access_token=tokens["access_token"],
        expires_at=tokens["expires_at_seconds"],
        on_refresh=lambda resp: _on_refresh(resp),
    )
    return globus_sdk.TransferClient(authorizer=authorizer)


def _on_refresh(token_response):
    """Persist refreshed access tokens back to the cache."""
    tokens = token_response.by_resource_server["transfer.api.globus.org"]
    existing = load_tokens() or {}
    existing.update(tokens)
    with open(TOKEN_FILE, "w") as f:
        json.dump(existing, f)


def consent_required_scopes(tc, collection_id, path):
    """Probe a directory and return any extra scopes Globus says we must consent
    to before we can use this collection, or [] if none are needed.

    Globus Connect Server v5 mapped collections require a per-collection
    data_access consent on top of the base transfer scope. Errors other than
    "consent required" are ignored here -- they'll surface, if real, during the
    actual listing."""
    try:
        tc.operation_ls(collection_id, path=path)
    except globus_sdk.TransferAPIError as e:
        if e.info.consent_required:
            return list(e.info.consent_required.required_scopes)
    return []


def connect(collection_id, path, get_code=prompt_for_code, notify=print_err):
    """Return a TransferClient that's ready to walk `path` on the collection,
    logging in first if there are no cached tokens."""
    tc = get_transfer_client(get_code)

    # Mapped collections may require an extra data-access consent. Probe the
    # starting path and, if needed, re-login with the extra scopes before
    # walking the tree.
    needed = consent_required_scopes(tc, collection_id, path)
    if needed:
        notify("This collection needs additional consent; re-authenticating...")
        do_login_flow(scopes=[TransferScopes.all] + needed, get_code=get_code)
        tc = get_transfer_client(get_code)
    return tc


def collection_name(tc, collection_id):
    """Return a friendly name for the collection, for the tree header.

    Raises TransferAPIError if the collection can't be reached."""
    ep = tc.get_endpoint(collection_id)
    return ep["display_name"] or ep["canonical_name"] or collection_id


def list_dir(tc, collection_id, path, status=None):
    """Return (dirs, files) name lists for a directory, sorted, dirs first.

    Returns (None, None) if the directory can't be read (permissions, etc.)."""
    try:
        entries = tc.operation_ls(collection_id, path=path)
    except globus_sdk.TransferAPIError as e:
        message = f"  ! could not list {path}: {e.message}"
        if status is not None:
            status.warn(message)
        else:
            print_err(message)
        return None, None

    dirs = sorted(e["name"] for e in entries if e["type"] == "dir")
    files = sorted(e["name"] for e in entries if e["type"] != "dir")
    return dirs, files


def join_path(base, name):
    """Join a Globus (posix-style) path, keeping a single trailing separator."""
    if not base.endswith("/"):
        base += "/"
    return base + name


def write_tree(tc, collection_id, path, out, prefix="", counts=None,
               depth=0, max_depth=None, status=None):
    """Recursively write the tree for `path` into the `out` file handle.

    `max_depth` of None means unlimited; otherwise recursion stops descending
    into directories once `depth` reaches `max_depth` (the starting path is
    depth 0, its immediate children are depth 1, and so on)."""
    if status is not None:
        status.update(path, counts)
    dirs, files = list_dir(tc, collection_id, path, status)
    if dirs is None:
        return

    children = [(d, True) for d in dirs] + [(f, False) for f in files]
    for index, (name, is_dir) in enumerate(children):
        is_last = index == len(children) - 1
        connector = ELBOW if is_last else TEE
        out.write(prefix + connector + name + ("/" if is_dir else "") + "\n")

        if is_dir:
            counts["dirs"] += 1
            if max_depth is not None and depth + 1 >= max_depth:
                continue
            extension = SPACE if is_last else PIPE
            write_tree(tc, collection_id, join_path(path, name), out,
                       prefix + extension, counts, depth + 1, max_depth,
                       status)
        else:
            counts["files"] += 1



def generate_tree(tc, collection_id, path, output_file, ep_name, counts,
                  max_depth=None, status=None):
    """Write the whole listing to `output_file`: a header naming the collection
    and path, the tree itself, and a closing count line.

    `counts` is filled in as the walk goes, so the caller can still report it
    if the walk is cut short."""
    with open(output_file, "w", encoding="utf-8") as out:
        out.write(f"{ep_name}:{path}\n")
        write_tree(tc, collection_id, path, out, counts=counts,
                   max_depth=max_depth, status=status)
        out.write(f"\n{counts['dirs']} directories, "
                  f"{counts['files']} files\n")


# ---------------------------------------------------------------------------
# GUI (-gui)
# ---------------------------------------------------------------------------

# The last few sets of parameters are kept next to the script, so they can be
# picked from the "Recent runs" list and run again.
RECENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "globus-tree-recent.json")
RECENT_MAX = 5

# The keys that make up one set of run parameters.
PARAM_KEYS = ("collection_id", "path", "output_file", "max_depth")


class Cancelled(Exception):
    """Raised on the worker thread to abandon a run -- the GUI's Ctrl-C."""


def is_uuid(text):
    try:
        uuid.UUID(text)
    except ValueError:
        return False
    return True


def format_elapsed(seconds):
    seconds = int(seconds)
    return f"{seconds // 60}m{seconds % 60:02d}s"


def load_recent():
    """Return the saved parameter sets, most recent first ([] if none)."""
    try:
        with open(RECENT_FILE, encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)][:RECENT_MAX]


def save_recent(entries):
    with open(RECENT_FILE, "w", encoding="utf-8") as f:
        json.dump(entries, f, indent=2)


def comparable(key, value):
    """`value` as compared between runs: paths lose any trailing slash, so
    /John_Flynn/ and /John_Flynn count as the same run."""
    if key in ("path",) and isinstance(value, str):
        return value.rstrip("/") or "/"
    return value


def same_params(a, b):
    """True if two parameter sets describe the same run (their times and
    collection names aside)."""
    return all(comparable(k, a.get(k)) == comparable(k, b.get(k))
               for k in PARAM_KEYS)


def add_recent(entries, params):
    """Return `entries` with `params` moved (or added) to the front, stamped
    with the current time, and trimmed to RECENT_MAX."""
    entry = {k: params[k] for k in PARAM_KEYS}
    entry["last_run"] = datetime.datetime.now().isoformat(timespec="seconds")
    rest = [e for e in entries if not same_params(e, params)]
    return ([entry] + rest)[:RECENT_MAX]


def describe_recent(entry):
    """One-line summary of a saved parameter set, for the Recent runs list."""
    when = str(entry.get("last_run", "")).replace("T", " ")[:16]
    where = entry.get("collection_name") or entry.get("collection_id", "?")
    output = os.path.basename(str(entry.get("output_file", "")))
    text = f"{when}   {where}:{entry.get('path', '/')}  →  {output}"
    if entry.get("max_depth"):
        text += f"  (depth {entry['max_depth']})"
    return text


def recent_tooltip(entry):
    name = entry.get("collection_name")
    collection = entry.get("collection_id", "")
    if name:
        collection = f"{name} ({collection})"
    return (f"Collection: {collection}\n"
            f"Path: {entry.get('path', '/')}\n"
            f"Output file: {entry.get('output_file', '')}\n"
            f"Max depth: {entry.get('max_depth') or 'unlimited'}")


class TreeWorker(QThread):
    """Logs in, checks consent, and walks the tree off the GUI thread.

    The worker also stands in for the command line's Status object (update /
    clear / warn), so write_tree reports straight to the window -- and
    update(), called once per directory, is where Stop takes effect."""

    progressed = Signal(str, int, int)    # directory being listed, dirs, files
    message = Signal(str)                 # a line for the log
    collection_named = Signal(str)
    login_needed = Signal(str)            # the Globus login URL
    finished_run = Signal(bool, str)      # succeeded, summary

    def __init__(self, params):
        super().__init__()
        self.params = params
        self.walked = False
        self._stop = threading.Event()
        self._code = None
        self._code_ready = threading.Event()

    def request_stop(self):
        self._stop.set()
        self._code_ready.set()

    def provide_code(self, code):
        """Hand over the authorization code from the login dialog (None if
        the login was cancelled). Called on the GUI thread."""
        self._code = code
        self._code_ready.set()

    def ask_for_code(self, authorize_url):
        """get_code for do_login_flow: have the GUI thread show the login
        dialog, and wait for its answer."""
        self._code = None
        self._code_ready.clear()
        self.login_needed.emit(authorize_url)
        self._code_ready.wait()
        if self._stop.is_set() or not self._code:
            raise Cancelled("Login cancelled.")
        return self._code

    # -- the Status interface used by write_tree and list_dir -------------

    def update(self, path, counts):
        self.walked = True
        if self._stop.is_set():
            raise Cancelled("Stopped.")
        self.progressed.emit(path, counts["dirs"], counts["files"])

    def clear(self):
        pass

    def warn(self, message):
        self.message.emit(message.strip())

    # ---------------------------------------------------------------------

    def run(self):
        p = self.params
        counts = {"dirs": 0, "files": 0}
        try:
            self.message.emit("Connecting to Globus...")
            tc = connect(p["collection_id"], p["path"],
                         get_code=self.ask_for_code, notify=self.message.emit)
            try:
                ep_name = collection_name(tc, p["collection_id"])
            except globus_sdk.TransferAPIError as e:
                raise RuntimeError(f"Error accessing collection "
                                   f"{p['collection_id']}: {e.message}") from e
            self.collection_named.emit(ep_name)
            self.message.emit(f"Listing {ep_name}:{p['path']} into "
                              f"{p['output_file']}")
            generate_tree(tc, p["collection_id"], p["path"], p["output_file"],
                          ep_name, counts, max_depth=p["max_depth"],
                          status=self)
        except Exception as e:
            if isinstance(e, Cancelled):
                summary = str(e)
            else:
                summary = f"Error: {getattr(e, 'message', None) or e}"
            if self.walked:
                summary += (f" Partial tree left in {p['output_file']} "
                            f"({counts['dirs']} directories, "
                            f"{counts['files']} files).")
            self.finished_run.emit(False, summary)
        else:
            self.finished_run.emit(
                True, f"Wrote tree to {p['output_file']} "
                      f"({counts['dirs']} directories, "
                      f"{counts['files']} files).")


class ElidedLabel(QLabel):
    """One-line label that trims long text from the left, keeping the deep
    (informative) end of a path in view, like the command-line readout."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Preferred)
        self.setMinimumHeight(self.fontMetrics().height())

    def set_full_text(self, text):
        self._full = text
        self.setToolTip(text)
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        self.setText(self.fontMetrics().elidedText(
            self._full, Qt.TextElideMode.ElideLeft, self.width()))


class LoginDialog(QDialog):
    """The GUI version of the terminal login prompt: opens the Globus login
    page in the browser and asks for the authorization code it shows."""

    def __init__(self, authorize_url, parent=None):
        super().__init__(parent)
        self.url = authorize_url
        self.setWindowTitle("Log in to Globus")

        layout = QVBoxLayout(self)
        info = QLabel(
            "The Globus login page has been opened in your web browser. Log "
            "in, click <b>Allow</b>, then copy the authorization code Globus "
            "shows you and paste it below.<br><br>If the page didn't open, "
            "use the buttons to open it again or copy the link.")
        info.setWordWrap(True)
        layout.addWidget(info)

        link_row = QHBoxLayout()
        open_button = QPushButton("Open login page")
        open_button.clicked.connect(self.open_page)
        link_row.addWidget(open_button)
        copy_button = QPushButton("Copy link")
        copy_button.clicked.connect(
            lambda: QGuiApplication.clipboard().setText(self.url))
        link_row.addWidget(copy_button)
        link_row.addStretch(1)
        layout.addLayout(link_row)

        layout.addWidget(QLabel("Authorization code:"))
        self.code_edit = QLineEdit()
        layout.addWidget(self.code_edit)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                               | QDialogButtonBox.StandardButton.Cancel)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        ok = box.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(False)
        self.code_edit.textChanged.connect(
            lambda text: ok.setEnabled(bool(text.strip())))
        layout.addWidget(box)
        self.resize(520, self.sizeHint().height())

    def open_page(self):
        webbrowser.open(self.url)

    @classmethod
    def get_code(cls, authorize_url, parent=None):
        """Run the dialog; returns the pasted code, or None if cancelled."""
        dialog = cls(authorize_url, parent)
        dialog.open_page()
        if dialog.exec() == QDialog.DialogCode.Accepted:
            return dialog.code_edit.text().strip()
        return None


class TreeWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.worker = None
        self.run_params = None
        self.started = 0.0
        self.walking = False
        self.stopping = False
        self.dirs = self.files = 0
        self.confirmed_output = None   # a file Browse already OK'd replacing
        self.recent = load_recent()

        self.setWindowTitle("Globus Tree")
        self.resize(780, 580)
        self._build_ui()

        # Ticks the elapsed time along even while one slow directory listing
        # holds up the next progress update.
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self._show_counts)

        self._refresh_recent()
        if self.recent:
            self._load_params(self.recent[0])
        self._set_running(False)

    # -- construction ------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        form = QFormLayout()
        # Labels in a left-aligned column, fields stretching to the window
        # edge. (The macOS style otherwise centers the form, right-aligns the
        # labels, and leaves the fields at their narrow natural width.)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft
                              | Qt.AlignmentFlag.AlignTop)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.recent_combo = QComboBox()
        self.recent_combo.setPlaceholderText(
            "Pick a previous run to reload its settings")
        self.recent_combo.activated.connect(self._pick_recent)
        form.addRow("Recent runs:", self.recent_combo)

        self.collection_edit = QLineEdit()
        self.collection_edit.setPlaceholderText(
            "Collection UUID, e.g. a1b2c3d4-...")
        self.collection_edit.setToolTip("Globus collection (endpoint) ID")
        form.addRow("Collection ID:", self.collection_edit)

        self.path_edit = QLineEdit("/")
        self.path_edit.setToolTip(
            "Starting path on the collection (Globus paths always use "
            "forward slashes)")
        form.addRow("Starting path:", self.path_edit)

        self.output_edit = QLineEdit()
        self.output_edit.setToolTip("Output file for the tree")
        self.browse_button = QPushButton("Browse...")
        self.browse_button.clicked.connect(self._browse_output)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(self.browse_button)
        form.addRow("Output file:", output_row)

        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(0, 999)
        self.depth_spin.setSpecialValueText("Unlimited")
        self.depth_spin.setToolTip(
            "Maximum directory depth to descend. The starting path is depth 0.")
        # A number doesn't need the full width, but leave room for "Unlimited".
        self.depth_spin.setSizePolicy(QSizePolicy.Policy.Fixed,
                                      QSizePolicy.Policy.Fixed)
        self.depth_spin.setMinimumWidth(
            self.depth_spin.fontMetrics().horizontalAdvance("Unlimited") + 50)
        form.addRow("Max depth:", self.depth_spin)
        layout.addLayout(form)

        self.inputs = [self.recent_combo, self.collection_edit, self.path_edit,
                       self.output_edit, self.browse_button, self.depth_spin]

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop)
        buttons.addWidget(self.stop_button)
        layout.addLayout(buttons)

        status_box = QGroupBox("Status")
        status_layout = QVBoxLayout(status_box)
        # There's no telling how big the tree is, so this is a "busy" bar
        # rather than a progress bar.
        self.busy = QProgressBar()
        self.busy.setTextVisible(False)
        policy = self.busy.sizePolicy()
        policy.setRetainSizeWhenHidden(True)
        self.busy.setSizePolicy(policy)
        status_layout.addWidget(self.busy)
        self.status_label = QLabel("Idle.")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        status_layout.addWidget(self.status_label)
        self.path_label = ElidedLabel()
        status_layout.addWidget(self.path_label)
        layout.addWidget(status_box)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(10000)
        self.log.setFont(QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.log, 1)

        self.setCentralWidget(central)

    # -- parameters and recent runs ----------------------------------------

    def _load_params(self, entry):
        self.collection_edit.setText(str(entry.get("collection_id") or ""))
        self.path_edit.setText(str(entry.get("path") or "/"))
        self.output_edit.setText(str(entry.get("output_file") or ""))
        try:
            depth = int(entry.get("max_depth") or 0)
        except (TypeError, ValueError):
            depth = 0
        self.depth_spin.setValue(depth)

    def _read_params(self):
        """Collect the form's values, or explain what's wrong and return
        None."""
        collection_id = self.collection_edit.text().strip()
        output_file = self.output_edit.text().strip()
        problem = None
        if not collection_id:
            problem = "Enter the collection ID."
        elif not is_uuid(collection_id):
            problem = ("The collection ID should be a UUID, like "
                       "a1b2c3d4-e5f6-.... Copy it from the collection's "
                       "page in the Globus web app.")
        elif not output_file:
            problem = "Choose an output file for the tree."
        if problem:
            QMessageBox.warning(self, "Globus Tree", problem)
            return None
        return {
            "collection_id": collection_id,
            "path": self.path_edit.text().strip() or "/",
            "output_file": os.path.abspath(os.path.expanduser(output_file)),
            "max_depth": self.depth_spin.value() or None,
        }

    def _refresh_recent(self):
        self.recent_combo.clear()
        for index, entry in enumerate(self.recent):
            self.recent_combo.addItem(describe_recent(entry))
            self.recent_combo.setItemData(index, recent_tooltip(entry),
                                          Qt.ItemDataRole.ToolTipRole)
        self.recent_combo.setCurrentIndex(-1)

    def _pick_recent(self, index):
        if 0 <= index < len(self.recent):
            self._load_params(self.recent[index])

    def _save_recent(self):
        try:
            save_recent(self.recent)
        except OSError as e:
            self._log(f"Couldn't save recent runs to {RECENT_FILE}: {e}")

    def _browse_output(self):
        start = self.output_edit.text().strip() or os.getcwd()
        path, _ = QFileDialog.getSaveFileName(
            self, "Save tree listing as", start,
            "Text files (*.txt);;All files (*)")
        if path:
            self.output_edit.setText(path)
            # The save dialog has already asked about replacing it.
            self.confirmed_output = os.path.abspath(path)

    # -- running -----------------------------------------------------------

    def start(self):
        params = self._read_params()
        if params is None:
            return
        output_file = params["output_file"]
        if (os.path.exists(output_file)
                and output_file != self.confirmed_output):
            answer = QMessageBox.question(
                self, "Globus Tree",
                f"{output_file} already exists. Replace it?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        self.output_edit.setText(output_file)

        self.run_params = params
        self.recent = add_recent(self.recent, params)
        self._save_recent()
        self._refresh_recent()

        if self.log.blockCount() > 1:
            self._log("")
        self._log(f"--- {datetime.datetime.now():%Y-%m-%d %H:%M:%S} ---")

        self.dirs = self.files = 0
        self.walking = self.stopping = False
        self.started = time.time()
        self.worker = TreeWorker(params)
        self.worker.progressed.connect(self._on_progress)
        self.worker.message.connect(self._log)
        self.worker.collection_named.connect(self._on_collection_named)
        self.worker.login_needed.connect(self._on_login_needed)
        self.worker.finished_run.connect(self._on_finished)

        self._set_running(True)
        self.status_label.setStyleSheet("")
        self.status_label.setText("Connecting to Globus...")
        self.path_label.set_full_text("")
        self.clock.start()
        self.worker.start()

    def stop(self):
        if self.worker is None:
            return
        self.worker.request_stop()
        self.stopping = True
        self.stop_button.setEnabled(False)
        self._show_counts()

    def _set_running(self, running):
        for widget in self.inputs:
            widget.setEnabled(not running)
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.busy.setRange(0, 0)   # min == max: an endless "busy" animation
        self.busy.setVisible(running)

    def _show_counts(self):
        if self.walking:
            text = (f"{self.dirs:,} dirs, {self.files:,} files, "
                    f"{format_elapsed(time.time() - self.started)}")
            if self.stopping:
                text += "  -- stopping after the current directory..."
        elif self.stopping:
            text = "Stopping..."
        else:
            return
        self.status_label.setText(text)

    def _on_progress(self, path, dirs, files):
        self.walking = True
        self.dirs, self.files = dirs, files
        self.path_label.set_full_text(f"Listing {path}")
        self._show_counts()

    def _on_collection_named(self, name):
        # Put the friendly name in the Recent runs list instead of a UUID.
        for entry in self.recent:
            if same_params(entry, self.run_params):
                entry["collection_name"] = name
        self._save_recent()
        self._refresh_recent()

    def _on_login_needed(self, authorize_url):
        self.worker.provide_code(LoginDialog.get_code(authorize_url, self))

    def _on_finished(self, succeeded, summary):
        self.clock.stop()
        self._set_running(False)
        self.walking = False
        summary += f" [{format_elapsed(time.time() - self.started)}]"
        self.status_label.setStyleSheet("" if succeeded else "color: #c62828;")
        self.status_label.setText(summary)
        self.path_label.set_full_text("")
        self._log(summary)

    def _log(self, text):
        self.log.appendPlainText(text)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            answer = QMessageBox.question(
                self, "Globus Tree",
                "A listing is still running. Stop it and quit?\n"
                "(The partial tree is kept.)")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.request_stop()
            self.worker.wait(15000)
        event.accept()


def run_gui():
    app = QApplication(sys.argv[:1])
    window = TreeWindow()
    window.show()
    return app.exec()


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Generate a 'tree'-style directory listing for a Globus "
                    "collection path.")
    parser.add_argument("-c", "--collection-id",
                        help="Globus collection (endpoint) ID", required=True)
    parser.add_argument("-p", "--path", help="Starting path on the collection",
                        default="/")
    parser.add_argument("-o", "--output-file", help="Output file for the tree",
                        required=True)
    parser.add_argument("-d", "--max-depth", type=int, default=None,
                        help="Maximum directory depth to descend (default: "
                             "unlimited). The starting path is depth 0.")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Suppress the progress status line.")
    parser.add_argument("-gui", "--gui", action="store_true",
                        help="Open the graphical interface instead (any other "
                             "options are ignored; the window has fields for "
                             "them).")

    # The GUI has its own fields for the required options, so check for it
    # before argparse insists on them.
    if {"-gui", "--gui"} & set(sys.argv[1:]):
        sys.exit(run_gui())

    args = parser.parse_args()

    if args.max_depth is not None and args.max_depth < 1:
        parser.error("--max-depth must be 1 or greater")

    tc = connect(args.collection_id, args.path)

    # Confirm the collection is reachable and give a friendly name in the header.
    try:
        ep_name = collection_name(tc, args.collection_id)
    except globus_sdk.TransferAPIError as e:
        print(f"Error accessing collection {args.collection_id}: {e.message}",
              file=sys.stderr)
        sys.exit(1)

    counts = {"dirs": 0, "files": 0}
    status = Status(enabled=not args.quiet)
    try:
        generate_tree(tc, args.collection_id, args.path, args.output_file,
                      ep_name, counts, max_depth=args.max_depth, status=status)
    except KeyboardInterrupt:
        status.clear()
        print(f"Interrupted -- partial tree left in {args.output_file} "
              f"({counts['dirs']} directories, {counts['files']} files).",
              file=sys.stderr)
        sys.exit(1)
    finally:
        status.clear()

    print(f"Wrote tree to {args.output_file} "
          f"({counts['dirs']} directories, {counts['files']} files).")


if __name__ == "__main__":
    main()
