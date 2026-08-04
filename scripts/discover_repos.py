#!/usr/bin/env python3
"""Regenerate repos.yaml from the IIC-OSIC-TOOLS build definition.

Discovers every GitHub repository the build depends on:

  1. ``_build/tool_metadata.yml``             -- the pinned tool sources
  2. ``_build/images/**`` shell + Dockerfiles -- deps cloned or fetched during
     the image build (cudd, swig, spdlog, or-tools, ...)
  3. ``.gitmodules`` of every repo found above -- direct submodules, one level
     deep (OpenSTA, UHDM, antlr4, the openEMS family, ...)

Entries already present in repos.yaml keep their ``added`` date and any
hand-edited fields. Anything listed under ``ignore`` is never added back.

Usage:
    discover_repos.py [--repos-yaml repos.yaml] [--source-dir DIR]

Without --source-dir the source repository is cloned into a temp directory.
Set GITHUB_TOKEN to raise the API rate limit (required in CI).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import yaml

API = "https://api.github.com"

# Repos referenced only from the runtime "swiss army knife" scripts under
# skel/ are pulled by the user at runtime, not by the image build.
SKIP_PATH_PARTS = ("/skel/",)

BUILD_FILE_SUFFIXES = (".sh", ".py", ".yml", ".yaml")
BUILD_FILE_NAMES = ("Dockerfile",)

GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?=[\s\"'/)\]]|$)"
)


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def api_get(path: str) -> dict | None:
    """GET an API path. Returns None on 404 or transport error."""
    req = urllib.request.Request(f"{API}{path}")
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        log(f"  ! API {path} -> HTTP {exc.code}")
        return None
    except urllib.error.URLError as exc:
        log(f"  ! API {path} -> {exc}")
        return None


def api_raw(path: str) -> str | None:
    """GET a file's raw content. Returns None if it does not exist."""
    req = urllib.request.Request(f"{API}{path}")
    req.add_header("Accept", "application/vnd.github.raw")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError as exc:
        log(f"  ! API {path} -> {exc}")
        return None


def clone_source(repo: str, ref: str) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="osic-src-"))
    log(f"Cloning {repo}@{ref} ...")
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", ref,
         f"https://github.com/{repo}.git", str(tmp / "src")],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return tmp / "src"


def norm(slug: str) -> str:
    return slug.lower()


def from_tool_metadata(src: Path) -> dict[str, dict]:
    """Parse _build/tool_metadata.yml -> {slug: {origin, tool, pinned}}."""
    path = src / "_build" / "tool_metadata.yml"
    found: dict[str, dict] = {}
    if not path.is_file():
        log(f"  ! {path} not found")
        return found
    for entry in yaml.safe_load(path.read_text()) or []:
        url = str(entry.get("repo", ""))
        m = GITHUB_URL_RE.match(url)
        if not m:
            continue  # non-GitHub source (codeberg, gitlab, tarball, ...)
        slug = f"{m.group(1)}/{m.group(2)}"
        found[norm(slug)] = {
            "slug": slug,
            "origin": "tool_metadata",
            "tool": entry.get("name"),
            "pinned": str(entry.get("commit")) if entry.get("commit") else None,
        }
    return found


def from_build_scripts(src: Path) -> dict[str, dict]:
    """Scan build scripts and Dockerfiles for GitHub URLs."""
    found: dict[str, dict] = {}
    build = src / "_build"
    if not build.is_dir():
        return found
    for path in build.rglob("*"):
        if not path.is_file():
            continue
        rel = "/" + str(path.relative_to(src))
        if any(part in rel for part in SKIP_PATH_PARTS):
            continue
        if path.name not in BUILD_FILE_NAMES and path.suffix not in BUILD_FILE_SUFFIXES:
            continue
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            # Skip commented-out clones/downloads; they are not built.
            if line.lstrip().startswith("#"):
                continue
            for owner, name in GITHUB_URL_RE.findall(line):
                slug = f"{owner}/{name}"
                found.setdefault(
                    norm(slug),
                    {"slug": slug, "origin": "build_script", "tool": None, "pinned": None},
                )
    return found


def from_submodules(slugs: list[str]) -> dict[str, dict]:
    """Read .gitmodules of each repo and resolve its submodule URLs."""
    found: dict[str, dict] = {}
    for slug in slugs:
        text = api_raw(f"/repos/{slug}/contents/.gitmodules")
        if not text:
            continue
        owner = slug.split("/", 1)[0]
        for line in text.splitlines():
            if "=" not in line or not line.strip().startswith("url"):
                continue
            url = line.split("=", 1)[1].strip()
            if url.endswith(".git"):
                url = url[: -len(".git")]
            if url.startswith("../../"):
                # ../../owner/name -> owner/name
                sub = url[len("../../"):]
            elif url.startswith("../"):
                # ../name -> same owner as the parent repo
                sub = f"{owner}/{url[len('../'):]}"
            else:
                m = GITHUB_URL_RE.match(url)
                if not m:
                    continue  # codeberg / gitlab / sourceware / ...
                sub = f"{m.group(1)}/{m.group(2)}"
            if sub.count("/") != 1:
                continue
            found.setdefault(
                norm(sub),
                {"slug": sub, "origin": "submodule", "tool": None, "pinned": None},
            )
    return found


def resolve_fork_name(slug: str, overrides: dict[str, str]) -> str:
    """The name a fork of ``slug`` gets in the org.

    Upstreams get renamed (povik/yosys-slang -> sv-elab, mole99/ota-5t ->
    sky130_leo_ip__ota5t); GitHub redirects the API but a fork takes the
    *current* name, so ask the API rather than using the basename.
    """
    if slug in overrides:
        return overrides[slug]
    data = api_get(f"/repos/{slug}")
    if data and data.get("name"):
        return data["name"]
    return slug.split("/", 1)[1]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repos-yaml", default="repos.yaml", type=Path)
    ap.add_argument("--source-dir", type=Path,
                    help="existing checkout of the source repo (skips cloning)")
    args = ap.parse_args()

    doc = yaml.safe_load(args.repos_yaml.read_text()) if args.repos_yaml.is_file() else {}
    doc = doc or {}
    source = doc.get("source") or {}
    src_repo = source.get("repo", "iic-jku/IIC-OSIC-TOOLS")
    src_ref = source.get("ref", "main")
    org = doc.get("org", "osic-tools")
    ignore = {norm(s) for s in (doc.get("ignore") or [])}
    overrides = doc.get("fork_name_overrides") or {}
    existing = {norm(r["upstream"]): r for r in (doc.get("repos") or [])}

    tmp_parent = None
    if args.source_dir:
        src = args.source_dir
    else:
        src = clone_source(src_repo, src_ref)
        tmp_parent = src.parent

    try:
        log("Scanning tool_metadata.yml ...")
        found = from_tool_metadata(src)
        log(f"  {len(found)} tool source(s)")

        log("Scanning build scripts and Dockerfiles ...")
        scripts = from_build_scripts(src)
        for key, val in scripts.items():
            found.setdefault(key, val)
        log(f"  {len(scripts)} repo(s) referenced")

        log("Reading .gitmodules of each repo ...")
        subs = from_submodules(sorted(v["slug"] for v in found.values()))
        for key, val in subs.items():
            found.setdefault(key, val)
        log(f"  {len(subs)} submodule(s)")
    finally:
        if tmp_parent:
            shutil.rmtree(tmp_parent, ignore_errors=True)

    # The org's own copy of the source repo is tracked too.
    found.setdefault(
        norm(src_repo),
        {"slug": src_repo, "origin": "source", "tool": None, "pinned": None},
    )

    today = dt.date.today().isoformat()
    repos, added = [], []
    for key in sorted(found, key=lambda k: found[k]["slug"].lower()):
        if key in ignore:
            continue
        info = found[key]
        slug = info["slug"]
        prev = existing.get(key)
        if prev:
            entry = dict(prev)
            entry["upstream"] = slug
            # Refresh values the build owns; keep everything hand-edited.
            entry["origin"] = prev.get("origin") or info["origin"]
            # An override added after the entry was first written must win,
            # otherwise a rename never reaches the existing rows.
            if slug in overrides:
                entry["fork"] = f"{org}/{overrides[slug]}"
            if info["tool"]:
                entry["tool"] = info["tool"]
            if info["pinned"]:
                entry["pinned"] = info["pinned"]
        else:
            entry = {
                "upstream": slug,
                "fork": f"{org}/{resolve_fork_name(slug, overrides)}",
                "origin": info["origin"],
                "tool": info["tool"],
                "pinned": info["pinned"],
                "added": today,
            }
            added.append(slug)
        repos.append({k: v for k, v in entry.items() if v is not None})

    removed = [existing[k]["upstream"] for k in existing if k not in found and k not in ignore]

    doc["org"] = org
    doc["source"] = {"repo": src_repo, "ref": src_ref}
    doc["ignore"] = sorted(doc.get("ignore") or [], key=str.lower)
    doc["fork_name_overrides"] = overrides
    doc["repos"] = repos

    header = (
        "# Every GitHub repository the IIC-OSIC-TOOLS build depends on.\n"
        "#\n"
        "# Generated by scripts/discover_repos.py, which runs nightly. Hand edits to\n"
        "# `ignore` and `fork_name_overrides` are preserved, as is any field on an\n"
        "# existing entry that the build does not own.\n"
        "#\n"
        "# origin:  tool_metadata | build_script | submodule | source | manual\n"
        "# pinned:  the commit or tag the build checks out, where one is pinned.\n"
    )
    body = yaml.safe_dump(doc, sort_keys=False, width=100, allow_unicode=True)
    args.repos_yaml.write_text(header + body)

    log(f"\n{len(repos)} repo(s) written to {args.repos_yaml}")
    if added:
        log(f"NEW ({len(added)}):")
        for slug in added:
            log(f"  + {slug}")
    if removed:
        log(f"NO LONGER REFERENCED ({len(removed)}) -- kept, remove by hand if intended:")
        for slug in removed:
            log(f"  - {slug}")

    # Surface new repos to the workflow so it can name them in the commit.
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"new_count={len(added)}\n")
            fh.write(f"new_repos={' '.join(added)}\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
