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

By Hollister Herhold, AMNH, 2026.
Claude Opus 4.8 used for initial authoring.

'''

import argparse
import fnmatch
import json
import os
import sys

import globus_sdk
from globus_sdk.scopes import TransferScopes

# Native app client ID. This is the public Globus tutorial/CLI client ID; replace
# with your own registered app's client ID if you prefer.
CLIENT_ID = "61338d24-54d5-408f-a10d-66c06b59f6d2"

TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".globus-tree-tokens.json")


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
    scope. Pass additional (e.g. data-access) scopes when a transfer needs
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

    Errors other than "consent required" (e.g. a path that doesn't exist yet on
    the destination) are ignored here -- they'll surface, if real, during the
    actual listing or transfer."""
    try:
        tc.operation_ls(collection_id, path=path)
    except globus_sdk.TransferAPIError as e:
        if e.info.consent_required:
            return list(e.info.consent_required.required_scopes)
    return []


def list_dir(tc, collection_id, path):
    """Return (dirs, files) name lists for a directory, sorted, dirs first.

    Returns (None, None) if the directory can't be read (permissions, etc.)."""
    try:
        entries = tc.operation_ls(collection_id, path=path)
    except globus_sdk.TransferAPIError as e:
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


def relative_path(base, full):
    """Return `full` expressed relative to `base` (both posix-style paths).

    `full` is always built by descending from `base`, so it begins with it."""
    base = base.rstrip("/")
    return full[len(base):].lstrip("/")


def find_matches(tc, collection_id, path, pattern, matches, counts,
                 depth=0, max_depth=None, case_insensitive=False):
    """Recursively search `path`, appending the full path of every file whose
    name matches `pattern` to the `matches` list.

    `max_depth` of None means unlimited; otherwise recursion stops descending
    into directories once `depth` reaches `max_depth` (the starting path is
    depth 0, its immediate children are depth 1, and so on)."""
    dirs, files = list_dir(tc, collection_id, path)
    if dirs is None:
        return

    match = fnmatch.fnmatch if case_insensitive else fnmatch.fnmatchcase
    for name in files:
        if match(name, pattern):
            matches.append(join_path(path, name))

    counts["dirs"] += len(dirs)
    if max_depth is not None and depth + 1 >= max_depth:
        return
    for name in dirs:
        find_matches(tc, collection_id, join_path(path, name), pattern,
                     matches, counts, depth + 1, max_depth, case_insensitive)


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

    args = parser.parse_args()

    if args.max_depth is not None and args.max_depth < 1:
        parser.error("--max-depth must be 1 or greater")

    tc = get_transfer_client()

    # Both source and (for real transfers) destination may require an extra
    # data-access consent. Probe them and, if needed, re-login with the extra
    # scopes before doing any real work.
    needed = consent_required_scopes(tc, args.collection_id, args.path)
    if not args.dry_run:
        needed += consent_required_scopes(tc, args.dest_collection_id,
                                          args.dest_path)
    if needed:
        print("This transfer needs additional consent; re-authenticating...",
              file=sys.stderr)
        do_login_flow(scopes=[TransferScopes.all] + needed)
        tc = get_transfer_client()

    # Confirm the source collection is reachable and name it for messages.
    try:
        ep = tc.get_endpoint(args.collection_id)
        ep_name = ep["display_name"] or ep["canonical_name"] or args.collection_id
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

    dest_base = args.dest_path.rstrip("/")
    plan = [(src, dest_base + "/" + relative_path(args.path, src))
            for src in matches]

    if args.dry_run:
        for src, dst in plan:
            print(f"{src}  ->  {dst}")
        print(f"\nDry run: would clone {len(plan)} file(s). No transfer "
              f"submitted.", file=sys.stderr)
        return

    tdata = globus_sdk.TransferData(
        tc, args.collection_id, args.dest_collection_id,
        label=args.label, sync_level="checksum", verify_checksum=True)
    for src, dst in plan:
        tdata.add_item(src, dst)

    try:
        result = tc.submit_transfer(tdata)
    except globus_sdk.TransferAPIError as e:
        print(f"Error submitting transfer: {e.message}", file=sys.stderr)
        sys.exit(1)

    task_id = result["task_id"]
    print(f"Submitted transfer of {len(plan)} file(s). Task ID: {task_id}")
    print(f"  Track it at https://app.globus.org/activity/{task_id}")

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
