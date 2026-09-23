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

By Hollister Herhold, AMNH, 2026.
Claude Opus 4.8 used for initial authoring.

'''

import argparse
import json
import os
import shutil
import sys
import time

import globus_sdk
from globus_sdk.scopes import TransferScopes

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


def do_login_flow(scopes=None):
    """Run the Native App OAuth flow and return freshly minted transfer tokens.

    `scopes` is a list of requested scopes; it defaults to the full transfer
    scope. Pass additional (e.g. data-access) scopes when a collection needs
    consent beyond the base transfer scope."""
    if scopes is None:
        scopes = [TransferScopes.all]

    auth_client = globus_sdk.NativeAppAuthClient(CLIENT_ID)
    auth_client.oauth2_start_flow(requested_scopes=scopes, refresh_tokens=True)

    authorize_url = auth_client.oauth2_get_authorize_url()
    print("Please go to this URL and log in:\n")
    print(authorize_url + "\n")
    auth_code = input("Enter the authorization code here: ").strip()

    token_response = auth_client.oauth2_exchange_code_for_tokens(auth_code)
    save_tokens(token_response)
    return token_response.by_resource_server["transfer.api.globus.org"]


def get_transfer_client():
    """Return an authenticated TransferClient, logging in if needed."""
    tokens = load_tokens()
    if tokens is None:
        tokens = do_login_flow()

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


def list_dir(tc, collection_id, path, status=None):
    """Return (dirs, files) name lists for a directory, sorted, dirs first.

    Returns (None, None) if the directory can't be read (permissions, etc.)."""
    try:
        entries = tc.operation_ls(collection_id, path=path)
    except globus_sdk.TransferAPIError as e:
        if status is not None:
            status.clear()
        print(f"  ! could not list {path}: {e.message}", file=sys.stderr)
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

    args = parser.parse_args()

    if args.max_depth is not None and args.max_depth < 1:
        parser.error("--max-depth must be 1 or greater")

    tc = get_transfer_client()

    # Mapped collections may require an extra data-access consent. Probe the
    # starting path and, if needed, re-login with the extra scopes before
    # walking the tree.
    needed = consent_required_scopes(tc, args.collection_id, args.path)
    if needed:
        print("This collection needs additional consent; re-authenticating...",
              file=sys.stderr)
        do_login_flow(scopes=[TransferScopes.all] + needed)
        tc = get_transfer_client()

    # Confirm the collection is reachable and give a friendly name in the header.
    try:
        ep = tc.get_endpoint(args.collection_id)
        ep_name = ep["display_name"] or ep["canonical_name"] or args.collection_id
    except globus_sdk.TransferAPIError as e:
        print(f"Error accessing collection {args.collection_id}: {e.message}",
              file=sys.stderr)
        sys.exit(1)

    counts = {"dirs": 0, "files": 0}
    status = Status(enabled=not args.quiet)
    try:
        with open(args.output_file, "w", encoding="utf-8") as out:
            out.write(f"{ep_name}:{args.path}\n")
            write_tree(tc, args.collection_id, args.path, out, counts=counts,
                       max_depth=args.max_depth, status=status)
            out.write(f"\n{counts['dirs']} directories, "
                      f"{counts['files']} files\n")
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
