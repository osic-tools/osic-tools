#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Harald Pretl
# Johannes Kepler University, Department for Integrated Circuits
# SPDX-License-Identifier: Apache-2.0
"""Fast-forward every fork listed in repos.yaml to its upstream.

Uses the GitHub "merge upstream" endpoint, which fast-forwards a fork's
default branch to the upstream default branch. Only the default branch is
synced -- that is all the forks were created with.

Outcomes per repo:
    updated    fast-forwarded to upstream
    current    already up to date
    missing    listed in repos.yaml but not forked into the org yet
    diverged   the fork has commits upstream does not; left untouched
    error      API rejected the request

Usage:
    sync_forks.py [--repos-yaml repos.yaml] [--dry-run] [--fork-missing]

GITHUB_TOKEN must be able to write to the org's repositories. The default
GITHUB_TOKEN of an Actions run is scoped to its own repository only, so a PAT
or GitHub App token is required. It also needs workflow write permission
(`workflow` scope on a classic PAT, Workflows read+write on a fine-grained
one): a fast-forward carries the upstream commits into the fork, and GitHub
refuses those that touch .github/workflows/ without it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

API = "https://api.github.com"


def log(msg: str) -> None:
    print(msg, flush=True)


def request(method: str, path: str, body: dict | None = None) -> tuple[int, dict | None]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            return exc.code, {"message": raw.decode("utf-8", "replace")[:200]}
    except urllib.error.URLError as exc:
        return 0, {"message": str(exc)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repos-yaml", default="repos.yaml", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fork-missing", action="store_true",
                    help="create forks for entries that have none yet")
    ap.add_argument("--sleep", type=float, default=0.5,
                    help="pause between API writes (default: 0.5s)")
    args = ap.parse_args()

    doc = yaml.safe_load(args.repos_yaml.read_text()) or {}
    org = doc.get("org", "osic-tools")
    entries = doc.get("repos") or []

    counts = {"updated": 0, "current": 0, "missing": 0,
              "diverged": 0, "error": 0, "forked": 0}
    problems: list[str] = []

    for entry in entries:
        upstream = entry["upstream"]
        fork = entry.get("fork") or f"{org}/{upstream.split('/', 1)[1]}"

        status, data = request("GET", f"/repos/{fork}")
        if status == 404:
            if args.fork_missing and not args.dry_run:
                name = fork.split("/", 1)[1]
                # A renamed or transferred upstream answers a GET with a
                # redirect, which urllib follows -- but the POST below comes
                # back as "Moved Permanently" and fails. Resolve it first.
                st, meta = request("GET", f"/repos/{upstream}")
                if st == 200 and (meta or {}).get("full_name"):
                    upstream = meta["full_name"]
                st, payload = request(
                    "POST", f"/repos/{upstream}/forks",
                    {"organization": org, "name": name, "default_branch_only": True},
                )
                if st in (200, 202):
                    log(f"FORKED    {upstream} -> {fork}")
                    counts["forked"] += 1
                else:
                    msg = (payload or {}).get("message", st)
                    log(f"ERROR     {upstream}: fork failed: {msg}")
                    counts["error"] += 1
                    problems.append(f"{upstream}: fork failed: {msg}")
                time.sleep(args.sleep)
            else:
                log(f"MISSING   {fork} (upstream {upstream})")
                counts["missing"] += 1
                problems.append(f"{fork}: not forked yet")
            continue
        if status != 200:
            msg = (data or {}).get("message", status)
            log(f"ERROR     {fork}: {msg}")
            counts["error"] += 1
            problems.append(f"{fork}: {msg}")
            continue

        branch = (data or {}).get("default_branch") or "main"

        if args.dry_run:
            log(f"WOULD SYNC {fork} ({branch}) <- {upstream}")
            continue

        status, data = request("POST", f"/repos/{fork}/merge-upstream",
                               {"branch": branch})
        msg = (data or {}).get("message", "")
        if status == 200:
            merge_type = (data or {}).get("merge_type", "")
            if merge_type == "none":
                log(f"CURRENT   {fork}")
                counts["current"] += 1
            else:
                log(f"UPDATED   {fork} ({branch}, {merge_type})")
                counts["updated"] += 1
        elif status == 409:
            # Fork carries commits upstream does not; a fast-forward is not
            # possible and merging would rewrite someone's work.
            log(f"DIVERGED  {fork}: {msg}")
            counts["diverged"] += 1
            problems.append(f"{fork}: diverged from upstream")
        else:
            if status == 422 and "workflow" in msg:
                # The fast-forward would write upstream commits touching
                # .github/workflows/, which the token may not do. The fork
                # stays behind until the token is fixed, so this is a real
                # error rather than something to skip over.
                msg += ("  -- OSIC_TOOLS_TOKEN needs the `workflow` scope "
                        "(classic PAT) or Workflows read+write (fine-grained)")
            log(f"ERROR     {fork}: HTTP {status} {msg}")
            counts["error"] += 1
            problems.append(f"{fork}: HTTP {status} {msg}")
        time.sleep(args.sleep)

    log("")
    log("--- summary ---")
    for key, val in counts.items():
        if val:
            log(f"{key}: {val}")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as fh:
            fh.write("## Fork sync\n\n")
            fh.write(" | ".join(f"**{k}** {v}" for k, v in counts.items() if v) + "\n\n")
            if problems:
                fh.write("### Needs attention\n\n")
                for line in problems:
                    fh.write(f"- {line}\n")

    # Missing forks and divergence are reported, not failures of the run
    # itself; only API errors fail the job.
    return 1 if counts["error"] else 0


if __name__ == "__main__":
    sys.exit(main())
