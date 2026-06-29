#!/bin/env python

'''
globus-tree.py

Generate a directory tree (similar to the unix "tree" command) for a path on a
Globus collection, using the Globus SDK. The tree is written to an output file.

Authentication uses the Globus Native App OAuth flow. On first run you will be
prompted to visit a URL, log in, and paste back an authorization code. Tokens are
cached in ~/.globus-tree-tokens.json so subsequent runs don't require re-login.

By Hollister Herhold, AMNH, 2026.
Claude Opus 4.8 used for initial authoring.

'''

import argparse
import json
import os
import sys

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


def do_login_flow():
    """Run the Native App OAuth flow and return freshly minted transfer tokens."""
    auth_client = globus_sdk.NativeAppAuthClient(CLIENT_ID)
    auth_client.oauth2_start_flow(requested_scopes=TransferScopes.all,
                                  refresh_tokens=True)

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


def write_tree(tc, collection_id, path, out, prefix="", counts=None,
               depth=0, max_depth=None):
    """Recursively write the tree for `path` into the `out` file handle.

    `max_depth` of None means unlimited; otherwise recursion stops descending
    into directories once `depth` reaches `max_depth` (the starting path is
    depth 0, its immediate children are depth 1, and so on)."""
    dirs, files = list_dir(tc, collection_id, path)
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
                       prefix + extension, counts, depth + 1, max_depth)
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

    args = parser.parse_args()

    if args.max_depth is not None and args.max_depth < 1:
        parser.error("--max-depth must be 1 or greater")

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
    with open(args.output_file, "w", encoding="utf-8") as out:
        out.write(f"{ep_name}:{args.path}\n")
        write_tree(tc, args.collection_id, args.path, out, counts=counts,
                   max_depth=args.max_depth)
        out.write(f"\n{counts['dirs']} directories, {counts['files']} files\n")

    print(f"Wrote tree to {args.output_file} "
          f"({counts['dirs']} directories, {counts['files']} files).")


if __name__ == "__main__":
    main()
