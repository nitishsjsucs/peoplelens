# Build progress

Tracks the SPEC.md section 19 commit plan. Updated at every commit.

## Position

Commit 1 of 17 (core cut line after commit 14).

## Done

1. `chore: scaffold repo with README, spec, license, tooling pins`

## Next

2. `feat(data): seeded synthetic HR generator with exact counts`

## Check status (at this commit)

| Check | Result |
|---|---|
| `uv sync --frozen` | pass (131 packages in uv.lock) |
| `npm ci` | pass |

## Deviations from SPEC.md

- npm workspaces list only `worker` until the web UI commit (15) adds `web`, so `npm ci` works without an empty placeholder package.
- Makefile passes extra arguments through `ARGS` (for example `make bootstrap ARGS="--phase b"`), because make cannot forward flags such as `--phase` directly.
