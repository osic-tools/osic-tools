# osic-tools

Bookkeeping for the `osic-tools` mirror organisation, which holds a fork of
every GitHub repository the [IIC-OSIC-TOOLS](https://github.com/iic-jku/IIC-OSIC-TOOLS)
Docker build depends on.

The point of the mirror is availability: if an upstream disappears, is renamed,
force-pushes, or goes offline, the build still has a copy of the source it uses.
Forks do not track upstream on their own, so this repo also carries the
automation that keeps them current.

## `repos.yaml`

The inventory. One entry per upstream repository:

```yaml
- upstream: YosysHQ/yosys       # the source of truth
  fork: osic-tools/yosys        # our mirror
  origin: tool_metadata         # how the build reaches it
  tool: yosys                   # tool name, for tool_metadata entries
  pinned: v0.67                 # the commit or tag the build checks out
  added: '2026-08-04'           # first seen by the scan
```

`origin` records where the dependency was found:

| origin | meaning |
| --- | --- |
| `tool_metadata` | listed in `_build/tool_metadata.yml` with a pinned commit |
| `build_script` | cloned or downloaded by a build script or Dockerfile |
| `submodule` | a direct submodule of one of the above |
| `source` | the IIC-OSIC-TOOLS repository itself |
| `manual` | added by hand; the scan will not remove it |

Two hand-maintained keys are preserved across regeneration:

- **`ignore`** — upstreams the scan should never track. Currently the GCC and
  LLVM sources reached through `riscv-gnu-toolchain` (huge, and fetched straight
  from upstream by the build), plus duplicate slugs left behind by upstream
  renames.
- **`fork_name_overrides`** — where two upstreams would want the same name in
  the org. `The-OpenROAD-Project/abc` and `YosysHQ/abc` are different projects,
  so they become `or-abc` and `yosys-abc`; OpenROAD-flow-scripts pins its own
  yosys fork, which becomes `orfs-yosys`.

Non-GitHub sources are out of scope and are not listed: VACASK (Codeberg),
Surfer (GitLab), PyOPUS (university server), and the sourceware repositories
that `riscv-gnu-toolchain` redirects to GitHub mirrors.

## Nightly workflow

`.github/workflows/nightly.yml` runs at 03:17 UTC and can also be started by
hand from the Actions tab.

**Job 1 — `discover`** runs `scripts/discover_repos.py`, which shallow-clones
IIC-OSIC-TOOLS, re-derives the full dependency list, and commits `repos.yaml` if
it changed. New upstreams are listed in the run summary. It does **not** fork
them automatically: a new dependency is worth a look before it is mirrored.

**Job 2 — `sync`** runs `scripts/sync_forks.py`, which fast-forwards each fork's
default branch to its upstream through the GitHub merge-upstream endpoint. Per
repo the outcome is `updated`, `current`, `missing`, `diverged`, or `error`; the
counts and anything needing attention land in the run summary. A fork carrying
commits upstream does not have is reported as `diverged` and left untouched —
the sync never rewrites local work.

### Required secret

The sync job writes to other repositories in the org, which the built-in
`GITHUB_TOKEN` cannot do — it is scoped to this repository alone. Store an
org-scoped token as the **`OSIC_TOOLS_TOKEN`** secret.

For a fine-grained PAT, set the resource owner to `osic-tools`, grant it all
repositories, and give it:

- **Contents** — read and write (the fast-forward)
- **Metadata** — read
- **Administration** — read and write (only if you use the `fork_missing` input)

## Manual use

Both scripts are plain CLI tools; they need `pyyaml` and a `GITHUB_TOKEN`.

```bash
pip install pyyaml
export GITHUB_TOKEN=$(gh auth token)

# Refresh the inventory
python scripts/discover_repos.py --repos-yaml repos.yaml

# See what a sync would do
python scripts/sync_forks.py --dry-run

# Sync, and create any fork listed in repos.yaml that does not exist yet
python scripts/sync_forks.py --fork-missing
```

GitHub rate-limits fork creation at roughly 60–80 per hour, so a large
`--fork-missing` run will hit `HTTP 403: was submitted too quickly` and needs to
be repeated later. Re-running is safe — existing forks are skipped.
