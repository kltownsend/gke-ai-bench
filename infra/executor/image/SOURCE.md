# Sources for the Executor agent image

- `google-ax-harness`: github.com/agentexecutor/images, `agents/antigravity/ax-harness` at
  `f4ffb902` (2026-11-20), unmodified. sha256 `b7489965be7d9cd483fabf8646a071434d6211ce907f70b6f10c260c8e6f060b`.
- Base image and packages: the same file's Dockerfile at the same commit
  (`debian:bookworm-slim@sha256:734a72b3...`).
- agy: the lab's vendored 1.5.13 Linux x64 build (agent/vendor/agy-1.5.13-x64, git-ignored; its
  digest check is in the notes, 2026-11-21).
- AX itself: google/ax v0.4.0-rc.1 (`2701b10b459e6fe69ae3e4e0a1422641f349e229`). The AX runner is
  mounted into the task by AX (the runner volume image), not built in here.
