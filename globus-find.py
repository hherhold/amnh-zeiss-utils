#!/bin/env python

'''
globus-find.py

Recursively search a path on a Globus collection for files whose names match a
shell-style glob pattern (e.g. "*.pc[a,r]" to find files ending in .pca or .pcr),
using the Globus SDK. Matching paths are printed to stdout and, optionally, to an
output file.

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
import fnmatch
import json
import os
import re
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


def find_matches(tc, collection_id, path, pattern, on_match, counts,
                 depth=0, max_depth=None, case_insensitive=False):
    """Recursively search `path`, calling `on_match(full_path)` for each file
    whose name matches `pattern`.

    `max_depth` of None means unlimited; otherwise recursion stops descending
    into directories once `depth` reaches `max_depth` (the starting path is
    depth 0, its immediate children are depth 1, and so on)."""
    dirs, files = list_dir(tc, collection_id, path)
    if dirs is None:
        return

    # Not fnmatch.fnmatch for -i: it only ignores case on Windows (it goes
    # through os.path.normcase), so ask the regex engine to do it instead.
    flags = re.IGNORECASE if case_insensitive else 0
    match = re.compile(fnmatch.translate(pattern), flags).match
    for name in files:
        if match(name):
            on_match(join_path(path, name))
            counts["matches"] += 1

    counts["dirs"] += len(dirs)
    if max_depth is not None and depth + 1 >= max_depth:
        return
    for name in dirs:
        find_matches(tc, collection_id, join_path(path, name), pattern,
                     on_match, counts, depth + 1, max_depth, case_insensitive)


def main():
    parser = argparse.ArgumentParser(
        description="Recursively find files matching a glob pattern on a Globus "
                    "collection path.")
    parser.add_argument("-c", "--collection-id",
                        help="Globus collection (endpoint) ID", required=True)
    parser.add_argument("-p", "--path", help="Starting path on the collection",
                        default="/")
    parser.add_argument("pattern",
                        help="Shell-style glob pattern to match file names "
                             "against, e.g. \"*.pc[a,r]\". Quote it so the shell "
                             "doesn't expand it.")
    parser.add_argument("-o", "--output-file",
                        help="Optional file to also write matching paths to")
    parser.add_argument("-d", "--max-depth", type=int, default=None,
                        help="Maximum directory depth to descend (default: "
                             "unlimited). The starting path is depth 0.")
    parser.add_argument("-i", "--ignore-case", action="store_true",
                        help="Match the pattern case-insensitively")

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

    # Confirm the collection is reachable before we start walking it.
    try:
        ep = tc.get_endpoint(args.collection_id)
        ep_name = ep["display_name"] or ep["canonical_name"] or args.collection_id
    except globus_sdk.TransferAPIError as e:
        print(f"Error accessing collection {args.collection_id}: {e.message}",
              file=sys.stderr)
        sys.exit(1)

    print(f"Searching {ep_name}:{args.path} for '{args.pattern}' ...",
          file=sys.stderr)

    counts = {"dirs": 0, "matches": 0}
    out = open(args.output_file, "w", encoding="utf-8") if args.output_file \
        else None

    def on_match(full_path):
        print(full_path)
        if out is not None:
            out.write(full_path + "\n")

    try:
        find_matches(tc, args.collection_id, args.path, args.pattern,
                     on_match, counts, max_depth=args.max_depth,
                     case_insensitive=args.ignore_case)
    finally:
        if out is not None:
            out.close()

    summary = (f"{counts['matches']} match(es) in {counts['dirs']} "
               f"directories searched.")
    if args.output_file:
        summary += f" Wrote matches to {args.output_file}."
    print(summary, file=sys.stderr)


if __name__ == "__main__":
    main()
