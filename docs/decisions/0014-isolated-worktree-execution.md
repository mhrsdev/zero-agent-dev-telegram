# ADR 0014 — Isolated Worktree Execution with Path Validation

- Status: ACCEPTED
- Date: 2026-08-08
- Milestone: 6 (Isolated Execution with Branches and Worktrees)
- Skills applied: `zero-agent-execution-lifecycle`, `zero-recovery-consistency`,
  `zero-project-isolation-evidence`, `zero-artifact-provenance-model`

## Context

`PLAN.md` §11 (Milestone 6) requires:
- Every coding task receives an isolated branch and working tree.
- The target repository and base revision are explicit.
- Commands are scoped, time-bounded, and audited.
- A task returns diff, checks, artifacts, and status.
- No task pushes, merges, or deploys without explicit authority.
- Cleanup never deletes an unknown path, mount, active workspace, or
  uncommitted human work.

`zero-agent-execution-lifecycle` §"A worktree is a safety boundary":
"Concurrent coding tasks must not write into one working directory. A
branch names a history line; a worktree provides a separate filesystem
view. Both matter."

`zero-recovery-consistency` §"Cleanup requires proof of non-ownership":
"Before a worktree, artifact, cache, or temporary path is removed, Zero
needs evidence that it belongs to the intended task, has no active
process/service/mount dependency, and has preserved required human work
or recovery artifacts. Validated exact paths and lineage are safer than
broad age-based directory deletion."

## Decision

Adopt an isolated worktree execution model with five layers:

1. **Repository registration**: target repositories are registered with
   a validated absolute filesystem path. The path must exist, be a
   directory, be a git repository, and contain no ``..`` components.
2. **Worktree lifecycle**: each task receives a unique branch
   (``zero/<worktree_id>``) and a worktree directory under a configured
   root. The worktree state machine is:
   ``allocated → active → succeeded/failed/interrupted → cleanup_eligible
   → removed``.
3. **Command runner**: commands run with ``cwd`` set to the worktree
   path, with a timeout. stdout, stderr, and exit status are captured as
   artifacts with SHA-256 content hashes. No ``git push``, ``git merge``,
   or deployment commands are run.
4. **Artifact capture**: diffs (tracked + untracked), stdout, stderr,
   and exit status are stored as ``task_artifacts`` with content hashes
   for integrity.
5. **Safe cleanup**: ``remove_worktree`` only proceeds when:
   - the worktree is in ``cleanup_eligible`` state;
   - the worktree path is inside the configured root;
   - ``git worktree remove`` succeeds without ``--force`` (refuses if
     there are uncommitted changes — preserving human work).

### Path validation

Per PLAN.md M6: "Path traversal and repository escape attempts fail."
Three validation functions enforce this:

- ``validate_repository_path``: absolute, no ``..``, exists, is a
  directory, is a git repository.
- ``validate_worktree_path``: the root must be absolute and contain no
  ``..``; the worktree ID is sanitized to alphanumeric + ``-``/``_``.
- ``is_path_inside``: checks that a child path is inside a parent path
  by resolving both and checking the relative path.

### One active worktree per task

A partial unique index (``WHERE state IN ('allocated','active','interrupted')``)
ensures that a task has at most one active worktree at a time. Creating
a second worktree for a task that already has an active one raises
``WorktreeAlreadyExistsError``.

### Restart recovery

Per PLAN.md M6: "Restart identifies orphaned running work safely."
``recover_worktrees_after_restart`` finds worktrees in ``active`` state
and marks them ``interrupted``. The worktree directory is NOT deleted;
the caller decides whether to resume or clean up.

## Rejected alternatives

- **Container scheduler**: explicitly rejected by PLAN.md M6: "Do not
  build a container scheduler unless actual isolation requirements
  demand it." Git worktrees provide sufficient filesystem isolation.
- **Shared working directory with file locking**: rejected by
  ``zero-agent-execution-lifecycle`` §"A worktree is a safety boundary".
  Conventions do not prevent generated files, formatters, migrations,
  lockfiles, or broad commands from colliding.
- **``--force`` cleanup**: rejected by PLAN.md M6: "Cleanup preserves
  untracked or uncommitted human work unless explicitly authorized."
  ``git worktree remove`` without ``--force`` refuses to delete
  worktrees with uncommitted changes, which is the correct safety
  behavior.
- **Age-based cleanup**: rejected by ``zero-recovery-consistency``.
  Validated exact paths and lineage are safer than broad age-based
  directory deletion.

## Consequences

- Two independent tasks can run concurrently in separate worktrees
  without colliding.
- One failed task cannot corrupt another worktree (each has its own
  filesystem view).
- Path traversal and repository escape attempts are rejected at
  validation time.
- Uncommitted human work is preserved: ``git worktree remove`` refuses
  to delete worktrees with uncommitted changes.
- Restart recovery marks active worktrees as interrupted without
  deleting them.
- Artifacts have content hashes for integrity verification.
