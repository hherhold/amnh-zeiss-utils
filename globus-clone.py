#!/bin/env python

'''
globus-clone.py

Recursively search a path on a Globus collection for files whose names match a
shell-style glob pattern (e.g. "*.pc[a,r]" to find files ending in .pca or .pcr),
then transfer the matched files to a destination Globus collection, recreating the
original directory structure but containing only the matched files.

The intended use is to pull a scattered set of files (e.g. all the .pca/.pcr files
under some tree) off of a Globus collection and onto local disk so they can be
processed as ordinary local files. Because Globus is endpoint-to-endpoint, "local"
means a path on a destination Globus endpoint -- in practice a Globus Connect
Personal (GCP) collection running on your machine. Install GCP, note its collection
(endpoint) ID, and pass it as --dest-collection-id along with a --dest-path that
maps to a local directory GCP is allowed to write to.

Globus Transfer creates any missing intermediate directories on the destination, so
the cloned tree appears automatically. Transfers use a "checksum" sync level, so
re-running the script skips files that already copied successfully.

Authentication uses the Globus Native App OAuth flow. On first run you will be
prompted to visit a URL, log in, and paste back an authorization code. Tokens are
cached in ~/.globus-tree-tokens.json so subsequent runs don't require re-login.
Transferring between collections may require an additional data-access consent; if
so the script will prompt you to log in again with the extra scopes.

Run with -gui to get a PySide6 window instead: fields for each parameter, a
live status readout for the search and the transfer, a Stop button, and the
last few sets of parameters saved (in globus-clone-recent.json next to this
script) for easy re-running. In the GUI, logging in opens the Globus login page
in your browser and asks for the authorization code in a dialog.

By Hollister Herhold, AMNH, 2026.
Claude Opus 4.8 used for initial authoring.

'''

import argparse
import datetime
import fnmatch
import json
import os
import re
import sys
import threading
import time
import uuid
import webbrowser

import globus_sdk
from globus_sdk.scopes import TransferScopes

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QComboBox, QPushButton, QLabel, QSpinBox, QCheckBox,
    QPlainTextEdit, QProgressBar, QMessageBox, QDialog, QDialogButtonBox,
    QGroupBox, QSizePolicy,
)
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFontDatabase, QGuiApplication

# Native app client ID. This is the public Globus tutorial/CLI client ID; replace
# with your own registered app's client ID if you prefer.
CLIENT_ID = "61338d24-54d5-408f-a10d-66c06b59f6d2"

TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".globus-tree-tokens.json")


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
    scope. Pass additional (e.g. data-access) scopes when a transfer needs
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

    Errors other than "consent required" (e.g. a path that doesn't exist yet on
    the destination) are ignored here -- they'll surface, if real, during the
    actual listing or transfer."""
    try:
        tc.operation_ls(collection_id, path=path)
    except globus_sdk.TransferAPIError as e:
        if e.info.consent_required:
            return list(e.info.consent_required.required_scopes)
    return []


def connect(targets, get_code=prompt_for_code, notify=print_err):
    """Return a TransferClient that's ready to use every (collection_id, path)
    in `targets`, logging in first if there are no cached tokens."""
    tc = get_transfer_client(get_code)

    # Both source and (for real transfers) destination may require an extra
    # data-access consent. Probe them and, if needed, re-login with the extra
    # scopes before doing any real work.
    needed = []
    for collection_id, path in targets:
        needed += consent_required_scopes(tc, collection_id, path)
    if needed:
        notify("This transfer needs additional consent; re-authenticating...")
        do_login_flow(scopes=[TransferScopes.all] + needed, get_code=get_code)
        tc = get_transfer_client(get_code)
    return tc


def collection_name(tc, collection_id):
    """Return a friendly name for the collection, for messages.

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


def relative_path(base, full):
    """Return `full` expressed relative to `base` (both posix-style paths).

    `full` is always built by descending from `base`, so it begins with it."""
    base = base.rstrip("/")
    return full[len(base):].lstrip("/")


def find_matches(tc, collection_id, path, pattern, matches, counts,
                 depth=0, max_depth=None, case_insensitive=False, status=None):
    """Recursively search `path`, appending the full path of every file whose
    name matches `pattern` to the `matches` list.

    `max_depth` of None means unlimited; otherwise recursion stops descending
    into directories once `depth` reaches `max_depth` (the starting path is
    depth 0, its immediate children are depth 1, and so on). `status`, if
    given, is told about each directory as it's searched (the GUI uses this)."""
    if status is not None:
        status.update(path, counts, matches)
    dirs, files = list_dir(tc, collection_id, path, status)
    if dirs is None:
        return

    # Not fnmatch.fnmatch for -i: it only ignores case on Windows (it goes
    # through os.path.normcase), so ask the regex engine to do it instead.
    flags = re.IGNORECASE if case_insensitive else 0
    match = re.compile(fnmatch.translate(pattern), flags).match
    for name in files:
        if match(name):
            matches.append(join_path(path, name))

    counts["dirs"] += len(dirs)
    if max_depth is not None and depth + 1 >= max_depth:
        return
    for name in dirs:
        find_matches(tc, collection_id, join_path(path, name), pattern,
                     matches, counts, depth + 1, max_depth, case_insensitive,
                     status)


def clone_plan(matches, source_path, dest_path):
    """Pair each matched file with its destination: the same path relative to
    the source starting path, placed under `dest_path`."""
    dest_base = dest_path.rstrip("/")
    return [(src, dest_base + "/" + relative_path(source_path, src))
            for src in matches]


def submit_clone(tc, collection_id, dest_collection_id, plan, label):
    """Submit one transfer task for the whole plan and return its task ID."""
    tdata = globus_sdk.TransferData(
        collection_id, dest_collection_id,
        label=label, sync_level="checksum", verify_checksum=True)
    for src, dst in plan:
        tdata.add_item(src, dst)
    return tc.submit_transfer(tdata)["task_id"]


def activity_url(task_id):
    return f"https://app.globus.org/activity/{task_id}"


# ---------------------------------------------------------------------------
# GUI (-gui)
# ---------------------------------------------------------------------------

# The last few sets of parameters are kept next to the script, so they can be
# picked from the "Recent runs" list and run again.
RECENT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "globus-clone-recent.json")
RECENT_MAX = 5

# The keys that make up one set of run parameters. "Dry run" is left out on
# purpose: it's a per-run choice, and a dry run followed by the real thing
# should count as one entry.
PARAM_KEYS = ("collection_id", "path", "pattern", "ignore_case", "max_depth",
              "dest_collection_id", "dest_path", "label", "wait")

# How often to check on a transfer being monitored.
POLL_SECONDS = 15


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


def human_bytes(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def describe_task(task):
    """One-line summary of a transfer task's progress."""
    text = (f"{task['status']}: {task.get('files_transferred', 0)} of "
            f"{task.get('files', 0)} file(s) transferred, "
            f"{task.get('files_skipped', 0)} skipped, "
            f"{human_bytes(task.get('bytes_transferred') or 0)}")
    nice_status = task.get("nice_status")
    if nice_status and nice_status not in ("OK", "Queued"):
        text += f" -- {nice_status}"
    return text


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
    if key in ("path", "dest_path") and isinstance(value, str):
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
    text = (f"{when}   '{entry.get('pattern', '')}' in "
            f"{where}:{entry.get('path', '/')}  →  "
            f"{entry.get('dest_path', '')}")
    # The search settings change what gets found, so show them here rather
    # than only in the tooltip.
    extras = []
    if entry.get("max_depth"):
        extras.append(f"depth {entry['max_depth']}")
    if entry.get("ignore_case"):
        extras.append("ignore case")
    if extras:
        text += f"  ({', '.join(extras)})"
    return text


def recent_tooltip(entry):
    name = entry.get("collection_name")
    collection = entry.get("collection_id", "")
    if name:
        collection = f"{name} ({collection})"
    return (f"Source: {collection}\n"
            f"Path: {entry.get('path', '/')}\n"
            f"Pattern: {entry.get('pattern', '')}"
            f"{' (ignore case)' if entry.get('ignore_case') else ''}\n"
            f"Max depth: {entry.get('max_depth') or 'unlimited'}\n"
            f"Destination: {entry.get('dest_collection_id', '')}\n"
            f"Destination path: {entry.get('dest_path', '')}\n"
            f"Label: {entry.get('label', '')}\n"
            f"Monitor transfer: {'yes' if entry.get('wait') else 'no'}")


class CloneWorker(QThread):
    """Logs in, checks consent, searches, and submits (and optionally
    monitors) the transfer off the GUI thread.

    The worker is also the `status` for find_matches (update / warn), so the
    search reports straight to the window -- and update(), called once per
    directory, is where Stop takes effect."""

    progressed = Signal(str, int, int)    # directory being searched, dirs, matches
    message = Signal(str)                 # a line for the log
    collection_named = Signal(str)
    login_needed = Signal(str)            # the Globus login URL
    submitted = Signal(str)               # transfer task ID
    transfer_status = Signal(str)         # describe_task() of the transfer
    finished_run = Signal(bool, str)      # succeeded, summary

    def __init__(self, params):
        super().__init__()
        self.params = params
        self.task_id = None
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

    # -- the status interface used by find_matches and list_dir -----------

    def update(self, path, counts, matches):
        if self._stop.is_set():
            raise Cancelled("Stopped.")
        self.progressed.emit(path, counts["dirs"], len(matches))

    def warn(self, message):
        self.message.emit(message.strip())

    # ---------------------------------------------------------------------

    def run(self):
        try:
            self._run()
        except Exception as e:
            if isinstance(e, Cancelled):
                summary = str(e)
            else:
                summary = f"Error: {getattr(e, 'message', None) or e}"
            if self.task_id is None:
                summary += " No transfer was submitted."
            self.finished_run.emit(False, summary)

    def _run(self):
        p = self.params
        self.message.emit("Connecting to Globus...")
        targets = [(p["collection_id"], p["path"])]
        if not p["dry_run"]:
            targets.append((p["dest_collection_id"], p["dest_path"]))
        tc = connect(targets, get_code=self.ask_for_code,
                     notify=self.message.emit)

        try:
            ep_name = collection_name(tc, p["collection_id"])
        except globus_sdk.TransferAPIError as e:
            raise RuntimeError(f"Error accessing collection "
                               f"{p['collection_id']}: {e.message}") from e
        self.collection_named.emit(ep_name)

        self.message.emit(f"Searching {ep_name}:{p['path']} for "
                          f"'{p['pattern']}' ...")
        matches = []
        counts = {"dirs": 0}
        find_matches(tc, p["collection_id"], p["path"], p["pattern"], matches,
                     counts, max_depth=p["max_depth"],
                     case_insensitive=p["ignore_case"], status=self)
        self.message.emit(f"Found {len(matches)} file(s) in {counts['dirs']} "
                          f"directories searched.")
        if not matches:
            self.finished_run.emit(True, "No matching files found; nothing "
                                         "to clone.")
            return

        plan = clone_plan(matches, p["path"], p["dest_path"])
        if p["dry_run"]:
            self.message.emit("\n".join(f"{src}  ->  {dst}"
                                        for src, dst in plan))
            self.finished_run.emit(True, f"Dry run: would clone {len(plan)} "
                                         f"file(s). No transfer submitted.")
            return

        try:
            self.task_id = submit_clone(tc, p["collection_id"],
                                        p["dest_collection_id"], plan,
                                        p["label"])
        except globus_sdk.TransferAPIError as e:
            raise RuntimeError(f"Error submitting transfer: {e.message}") from e
        self.submitted.emit(self.task_id)
        self.message.emit(f"Submitted transfer of {len(plan)} file(s). "
                          f"Task ID: {self.task_id}")
        self.message.emit(f"  Track it at {activity_url(self.task_id)}")
        if not p["wait"]:
            self.finished_run.emit(True, f"Submitted transfer of {len(plan)} "
                                         f"file(s).")
            return

        task = self._monitor(tc)
        self.finished_run.emit(
            task["status"] == "SUCCEEDED",
            f"Transfer {task['status']}: {task['files_transferred']} file(s) "
            f"transferred, {task['files_skipped']} skipped.")

    def _monitor(self, tc):
        """Check on the transfer every POLL_SECONDS until it finishes. Stop
        only stops the watching -- the transfer carries on at Globus."""
        while True:
            try:
                task = tc.get_task(self.task_id)
            except globus_sdk.NetworkError as e:
                self.message.emit(f"Couldn't check on the transfer ({e}); "
                                  f"will try again.")
            else:
                self.transfer_status.emit(describe_task(task))
                if task["status"] in ("SUCCEEDED", "FAILED"):
                    return task
            if self._stop.wait(POLL_SECONDS):
                raise Cancelled("Stopped monitoring. The transfer carries on "
                                "at Globus; follow it on the activity page.")


class ElidedLabel(QLabel):
    """One-line label that trims long text from the left, keeping the deep
    (informative) end of a path in view."""

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


def left_aligned_form(parent=None):
    """A QFormLayout with its labels in a left-aligned column and fields that
    stretch to the right edge. (The macOS style otherwise centers the form,
    right-aligns the labels, and leaves the fields at their narrow natural
    width.)"""
    form = QFormLayout(parent)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
    form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
    form.setFieldGrowthPolicy(
        QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    return form


class CloneWindow(QMainWindow):

    def __init__(self):
        super().__init__()
        self.worker = None
        self.run_params = None
        self.started = 0.0
        self.phase = None           # "search" or "transfer" once under way
        self.stopping = False
        self.dirs = self.found = 0
        self.transfer_text = ""
        self.recent = load_recent()

        self.setWindowTitle("Globus Clone")
        self.resize(820, 720)
        self._build_ui()

        # Ticks the elapsed time along even while one slow directory listing
        # (or the wait between transfer checks) holds up the next update.
        self.clock = QTimer(self)
        self.clock.setInterval(1000)
        self.clock.timeout.connect(self._show_status)

        self._refresh_recent()
        if self.recent:
            self._load_params(self.recent[0])
        self._set_running(False)

    # -- construction ------------------------------------------------------

    def _build_ui(self):
        central = QWidget()
        layout = QVBoxLayout(central)

        recent_form = left_aligned_form()
        self.recent_combo = QComboBox()
        self.recent_combo.setPlaceholderText(
            "Pick a previous run to reload its settings")
        self.recent_combo.activated.connect(self._pick_recent)
        recent_form.addRow("Recent runs:", self.recent_combo)
        layout.addLayout(recent_form)

        source_box = QGroupBox("Source")
        source_form = left_aligned_form(source_box)
        self.collection_edit = QLineEdit()
        self.collection_edit.setPlaceholderText(
            "Collection UUID, e.g. a1b2c3d4-...")
        self.collection_edit.setToolTip("Source Globus collection (endpoint) ID")
        source_form.addRow("Collection ID:", self.collection_edit)

        self.path_edit = QLineEdit("/")
        self.path_edit.setToolTip(
            "Source starting path on the collection (Globus paths always use "
            "forward slashes)")
        source_form.addRow("Starting path:", self.path_edit)

        self.pattern_edit = QLineEdit()
        self.pattern_edit.setPlaceholderText("e.g. *.pc[a,r]")
        self.pattern_edit.setToolTip(
            "Shell-style glob pattern to match file names against")
        self.ignore_case_check = QCheckBox("Ignore case")
        pattern_row = QHBoxLayout()
        pattern_row.addWidget(self.pattern_edit, 1)
        pattern_row.addWidget(self.ignore_case_check)
        source_form.addRow("Pattern:", pattern_row)

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
        source_form.addRow("Max depth:", self.depth_spin)
        layout.addWidget(source_box)

        dest_box = QGroupBox("Destination")
        dest_form = left_aligned_form(dest_box)
        self.dest_collection_edit = QLineEdit()
        self.dest_collection_edit.setPlaceholderText(
            "Collection UUID -- typically your Globus Connect Personal "
            "collection")
        self.dest_collection_edit.setToolTip(
            "Destination Globus collection (endpoint) ID")
        dest_form.addRow("Collection ID:", self.dest_collection_edit)

        self.dest_path_edit = QLineEdit()
        self.dest_path_edit.setPlaceholderText("e.g. /~/pca_files")
        self.dest_path_edit.setToolTip(
            "Destination base path. The matched files are placed under here, "
            "recreating their paths relative to the source starting path.")
        dest_form.addRow("Path:", self.dest_path_edit)

        self.label_edit = QLineEdit("globus-clone")
        self.label_edit.setToolTip("Label for the Globus transfer task")
        dest_form.addRow("Transfer label:", self.label_edit)
        layout.addWidget(dest_box)

        # Each form has its own label column, so give every label the widest
        # one's width to line the fields up from one box to the next.
        labels = [form.itemAt(row, QFormLayout.ItemRole.LabelRole).widget()
                  for form in (recent_form, source_form, dest_form)
                  for row in range(form.rowCount())]
        label_width = max(label.sizeHint().width() for label in labels)
        for label in labels:
            label.setMinimumWidth(label_width)

        options = QHBoxLayout()
        # Dry run is deliberately not restored from the recent runs, and
        # starts checked: look before you copy.
        self.dry_run_check = QCheckBox(
            "Dry run (list what would be copied, transfer nothing)")
        self.dry_run_check.setChecked(True)
        self.dry_run_check.toggled.connect(self._update_start_label)
        options.addWidget(self.dry_run_check)
        self.wait_check = QCheckBox("Monitor the transfer until it finishes")
        options.addWidget(self.wait_check)
        options.addStretch(1)
        layout.addLayout(options)

        self.inputs = [self.recent_combo, source_box, dest_box,
                       self.dry_run_check, self.wait_check]

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.start_button = QPushButton()
        self.start_button.clicked.connect(self.start)
        buttons.addWidget(self.start_button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.clicked.connect(self.stop)
        buttons.addWidget(self.stop_button)
        layout.addLayout(buttons)
        self._update_start_label()

        status_box = QGroupBox("Status")
        status_layout = QVBoxLayout(status_box)
        # There's no telling how big the search is, so this is a "busy" bar
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
        self.task_label = QLabel()
        self.task_label.setOpenExternalLinks(True)
        self.task_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextBrowserInteraction)
        self.task_label.hide()
        status_layout.addWidget(self.task_label)
        layout.addWidget(status_box)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log.setFont(QFontDatabase.systemFont(
            QFontDatabase.SystemFont.FixedFont))
        layout.addWidget(self.log, 1)

        self.setCentralWidget(central)

    def _update_start_label(self):
        self.start_button.setText("Dry run" if self.dry_run_check.isChecked()
                                  else "Start transfer")

    # -- parameters and recent runs ----------------------------------------

    def _load_params(self, entry):
        self.collection_edit.setText(str(entry.get("collection_id") or ""))
        self.path_edit.setText(str(entry.get("path") or "/"))
        self.pattern_edit.setText(str(entry.get("pattern") or ""))
        self.ignore_case_check.setChecked(bool(entry.get("ignore_case")))
        try:
            depth = int(entry.get("max_depth") or 0)
        except (TypeError, ValueError):
            depth = 0
        self.depth_spin.setValue(depth)
        self.dest_collection_edit.setText(
            str(entry.get("dest_collection_id") or ""))
        self.dest_path_edit.setText(str(entry.get("dest_path") or ""))
        self.label_edit.setText(str(entry.get("label") or "globus-clone"))
        self.wait_check.setChecked(bool(entry.get("wait")))

    def _read_params(self):
        """Collect the form's values, or explain what's wrong and return
        None."""
        dry_run = self.dry_run_check.isChecked()
        collection_id = self.collection_edit.text().strip()
        pattern = self.pattern_edit.text().strip()
        dest_collection_id = self.dest_collection_edit.text().strip()
        dest_path = self.dest_path_edit.text().strip()
        uuid_help = ("should be a UUID, like a1b2c3d4-e5f6-.... Copy it from "
                     "the collection's page in the Globus web app.")
        problem = None
        if not collection_id:
            problem = "Enter the source collection ID."
        elif not is_uuid(collection_id):
            problem = f"The source collection ID {uuid_help}"
        elif not pattern:
            problem = "Enter a file name pattern, e.g. *.pc[a,r]"
        elif not dest_collection_id and not dry_run:
            problem = "Enter the destination collection ID."
        elif dest_collection_id and not is_uuid(dest_collection_id):
            problem = f"The destination collection ID {uuid_help}"
        elif not dest_path:
            problem = "Enter the destination path."
        if problem:
            QMessageBox.warning(self, "Globus Clone", problem)
            return None
        return {
            "collection_id": collection_id,
            "path": self.path_edit.text().strip() or "/",
            "pattern": pattern,
            "ignore_case": self.ignore_case_check.isChecked(),
            "max_depth": self.depth_spin.value() or None,
            "dest_collection_id": dest_collection_id,
            "dest_path": dest_path,
            "label": self.label_edit.text().strip() or "globus-clone",
            "wait": self.wait_check.isChecked(),
            "dry_run": dry_run,
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

    # -- running -----------------------------------------------------------

    def start(self):
        params = self._read_params()
        if params is None:
            return

        self.run_params = params
        self.recent = add_recent(self.recent, params)
        self._save_recent()
        self._refresh_recent()

        if self.log.blockCount() > 1:
            self._log("")
        kind = "dry run" if params["dry_run"] else "clone"
        self._log(f"--- {kind}, {datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                  f"---")

        self.dirs = self.found = 0
        self.phase = None
        self.stopping = False
        self.transfer_text = ""
        self.started = time.time()
        self.worker = CloneWorker(params)
        self.worker.progressed.connect(self._on_progress)
        self.worker.message.connect(self._log)
        self.worker.collection_named.connect(self._on_collection_named)
        self.worker.login_needed.connect(self._on_login_needed)
        self.worker.submitted.connect(self._on_submitted)
        self.worker.transfer_status.connect(self._on_transfer_status)
        self.worker.finished_run.connect(self._on_finished)

        self._set_running(True)
        self.status_label.setStyleSheet("")
        self.status_label.setText("Connecting to Globus...")
        self.path_label.set_full_text("")
        self.task_label.hide()
        self.clock.start()
        self.worker.start()

    def stop(self):
        if self.worker is None:
            return
        self.worker.request_stop()
        self.stopping = True
        self.stop_button.setEnabled(False)
        self._show_status()

    def _set_running(self, running):
        for widget in self.inputs:
            widget.setEnabled(not running)
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.busy.setRange(0, 0)   # min == max: an endless "busy" animation
        self.busy.setVisible(running)

    def _show_status(self):
        elapsed = format_elapsed(time.time() - self.started)
        if self.phase == "search":
            text = (f"Searching: {self.dirs:,} dirs searched, "
                    f"{self.found:,} matches, {elapsed}")
            if self.stopping:
                text += "  -- stopping after the current directory..."
        elif self.phase == "transfer":
            text = f"Transfer {self.transfer_text}  [{elapsed}]"
            if self.stopping:
                text += "  -- stopping monitoring..."
        elif self.stopping:
            text = "Stopping..."
        else:
            return
        self.status_label.setText(text)

    def _on_progress(self, path, dirs, found):
        self.phase = "search"
        self.dirs, self.found = dirs, found
        self.path_label.set_full_text(f"Searching {path}")
        self._show_status()

    def _on_collection_named(self, name):
        # Put the friendly name in the Recent runs list instead of a UUID.
        for entry in self.recent:
            if same_params(entry, self.run_params):
                entry["collection_name"] = name
        self._save_recent()
        self._refresh_recent()

    def _on_login_needed(self, authorize_url):
        self.worker.provide_code(LoginDialog.get_code(authorize_url, self))

    def _on_submitted(self, task_id):
        self.phase = "transfer"
        self.transfer_text = "submitted; waiting for its first status..."
        self.path_label.set_full_text("")
        self.task_label.setText(f'Task <a href="{activity_url(task_id)}">'
                                f'{task_id}</a> (opens the Globus activity '
                                f'page)')
        self.task_label.show()
        self._show_status()

    def _on_transfer_status(self, text):
        self.transfer_text = text
        self._show_status()

    def _on_finished(self, succeeded, summary):
        self.clock.stop()
        self._set_running(False)
        self.phase = None
        summary += f" [{format_elapsed(time.time() - self.started)}]"
        self.status_label.setStyleSheet("" if succeeded else "color: #c62828;")
        self.status_label.setText(summary)
        self.path_label.set_full_text("")
        self._log(summary)

    def _log(self, text):
        self.log.appendPlainText(text)

    def closeEvent(self, event):
        if self.worker is not None and self.worker.isRunning():
            if self.phase == "transfer":
                question = ("Stop monitoring the transfer and quit?\n"
                            "(The transfer carries on at Globus.)")
            else:
                question = ("A search is still running. Stop it and quit?\n"
                            "(Nothing has been transferred yet.)")
            answer = QMessageBox.question(self, "Globus Clone", question)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.request_stop()
            self.worker.wait(15000)
        event.accept()


def run_gui():
    app = QApplication(sys.argv[:1])
    window = CloneWindow()
    window.show()
    return app.exec()


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Find files matching a glob pattern on a Globus collection "
                    "and clone them, preserving directory structure, to a "
                    "destination Globus collection (e.g. a local Globus Connect "
                    "Personal endpoint).")
    parser.add_argument("-c", "--collection-id",
                        help="Source Globus collection (endpoint) ID",
                        required=True)
    parser.add_argument("-p", "--path", help="Source starting path on the "
                        "collection", default="/")
    parser.add_argument("pattern",
                        help="Shell-style glob pattern to match file names "
                             "against, e.g. \"*.pc[a,r]\". Quote it so the shell "
                             "doesn't expand it.")
    parser.add_argument("-C", "--dest-collection-id", required=True,
                        help="Destination Globus collection (endpoint) ID -- "
                             "typically your local Globus Connect Personal "
                             "collection.")
    parser.add_argument("-P", "--dest-path", required=True,
                        help="Destination base path. The matched files are placed "
                             "under here, recreating their paths relative to the "
                             "source --path.")
    parser.add_argument("-d", "--max-depth", type=int, default=None,
                        help="Maximum directory depth to descend (default: "
                             "unlimited). The starting path is depth 0.")
    parser.add_argument("-i", "--ignore-case", action="store_true",
                        help="Match the pattern case-insensitively")
    parser.add_argument("-n", "--dry-run", action="store_true",
                        help="List the matched files and where they would be "
                             "cloned to, but don't submit a transfer.")
    parser.add_argument("-w", "--wait", action="store_true",
                        help="Wait for the transfer to finish before exiting.")
    parser.add_argument("-l", "--label", default="globus-clone",
                        help="Label for the Globus transfer task "
                             "(default: globus-clone).")
    parser.add_argument("-gui", "--gui", action="store_true",
                        help="Open the graphical interface instead (any other "
                             "arguments are ignored; the window has fields for "
                             "them).")

    # The GUI has its own fields for the required arguments, so check for it
    # before argparse insists on them.
    if {"-gui", "--gui"} & set(sys.argv[1:]):
        sys.exit(run_gui())

    args = parser.parse_args()

    if args.max_depth is not None and args.max_depth < 1:
        parser.error("--max-depth must be 1 or greater")

    targets = [(args.collection_id, args.path)]
    if not args.dry_run:
        targets.append((args.dest_collection_id, args.dest_path))
    tc = connect(targets)

    # Confirm the source collection is reachable and name it for messages.
    try:
        ep_name = collection_name(tc, args.collection_id)
    except globus_sdk.TransferAPIError as e:
        print(f"Error accessing collection {args.collection_id}: {e.message}",
              file=sys.stderr)
        sys.exit(1)

    print(f"Searching {ep_name}:{args.path} for '{args.pattern}' ...",
          file=sys.stderr)

    matches = []
    counts = {"dirs": 0}
    find_matches(tc, args.collection_id, args.path, args.pattern, matches,
                 counts, max_depth=args.max_depth,
                 case_insensitive=args.ignore_case)

    print(f"Found {len(matches)} file(s) in {counts['dirs']} directories "
          f"searched.", file=sys.stderr)

    if not matches:
        return

    plan = clone_plan(matches, args.path, args.dest_path)

    if args.dry_run:
        for src, dst in plan:
            print(f"{src}  ->  {dst}")
        print(f"\nDry run: would clone {len(plan)} file(s). No transfer "
              f"submitted.", file=sys.stderr)
        return

    try:
        task_id = submit_clone(tc, args.collection_id, args.dest_collection_id,
                               plan, args.label)
    except globus_sdk.TransferAPIError as e:
        print(f"Error submitting transfer: {e.message}", file=sys.stderr)
        sys.exit(1)

    print(f"Submitted transfer of {len(plan)} file(s). Task ID: {task_id}")
    print(f"  Track it at {activity_url(task_id)}")

    if args.wait:
        print("Waiting for the transfer to complete...", file=sys.stderr)
        done = tc.task_wait(task_id, timeout=86400, polling_interval=15)
        if done:
            task = tc.get_task(task_id)
            print(f"Transfer {task['status']}: "
                  f"{task['files_transferred']} file(s) transferred, "
                  f"{task['files_skipped']} skipped.")
            if task["status"] != "SUCCEEDED":
                sys.exit(1)
        else:
            print("Transfer still running after timeout; check the activity "
                  "page.", file=sys.stderr)


if __name__ == "__main__":
    main()
