"""Git repository/worktree operations (M8).

All git commands are read-only or scoped to ADCC-owned worktrees.  Never
force-push, never delete unmerged user branches, never reset user
worktrees (SPEC §11.3).
"""

import os
import re
import subprocess

GIT_TIMEOUT = 10.0
WORKTREE_BRANCH_PREFIX = "adcc/"
# 分支名：adcc/<workflow-or-task>/<short-run-id>
BRANCH_RE = re.compile(r"^adcc/[a-z0-9_-]+/[a-z0-9]{8}$")


def run_git(cwd, *args, timeout=GIT_TIMEOUT):
    """Run git in ``cwd``; return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True,
            errors="replace", timeout=timeout)
        return result.returncode, result.stdout or "", result.stderr or ""
    except (OSError, subprocess.TimeoutExpired):
        return -1, "", "git 执行失败"


def detect_repo(path):
    """Return the repository root for ``path`` or None (read-only)."""
    if not path or not os.path.isdir(path):
        return None
    code, out, _ = run_git(path, "rev-parse", "--show-toplevel")
    if code != 0:
        return None
    value = out.strip()
    return os.path.normpath(value) if value else None


def current_branch(path):
    code, out, _ = run_git(path, "branch", "--show-current")
    return out.strip() or None if code == 0 else None


def list_worktrees(repo):
    """Parse ``git worktree list --porcelain`` into worktree records."""
    code, out, _ = run_git(repo, "worktree", "list", "--porcelain")
    if code != 0:
        return []
    worktrees = []
    current = {}
    for line in out.splitlines():
        if not line.strip():
            if current.get("path"):
                worktrees.append(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        key = key.lower().strip()
        if key == "worktree":
            key = "path"
        if key in ("bare", "detached"):
            current[key] = True
        else:
            current[key] = value.strip()
    if current.get("path"):
        worktrees.append(current)
    return [{
        "path": os.path.normpath(wt.get("path")) if wt.get("path") else None,
        "branch": (wt.get("branch") or "").replace("refs/heads/", ""),
        "head": wt.get("head"),
        "bare": bool(wt.get("bare")),
        "detached": bool(wt.get("detached")),
    } for wt in worktrees]


def adcc_worktree_branch(workflow_or_task, run_id):
    """ADCC-owned branch name; sanitized and collision-safe."""
    slug = re.sub(r"[^a-z0-9_-]+", "-", (workflow_or_task or "wf").lower())
    slug = re.sub(r"-{2,}", "-", slug).strip("-")[:40] or "wf"
    return "%s%s/%s" % (WORKTREE_BRANCH_PREFIX, slug, run_id[:8])


def is_adcc_branch(branch):
    return bool(branch and BRANCH_RE.fullmatch(branch))


def create_worktree(repo, branch, path, base_ref=None):
    """Create an ADCC-owned worktree at ``path`` on ``branch``.

    Returns (worktree_path, error).  ``base_ref`` defaults to the current
    HEAD; the new branch never touches the user's existing branches.
    """
    if not is_adcc_branch(branch):
        return None, "分支名必须是 ADCC 命名空间: %s" % branch
    os.makedirs(path, exist_ok=True)
    args = ["worktree", "add", "-b", branch, path]
    if base_ref:
        args.append(base_ref)
    code, out, err = run_git(repo, *args)
    if code != 0:
        return None, (err or out or "git worktree add 失败").strip()
    return os.path.normpath(path), None


def remove_worktree(repo, worktree_path, branch, force=False):
    """Remove an ADCC-owned worktree.  Refuses unsafe targets.

    Only branches matching the ADCC namespace may be removed; unmerged
    worktrees require an explicit ``force`` (still limited to ADCC
    branches).  ``git worktree remove`` + safe branch delete.
    """
    if not is_adcc_branch(branch):
        return False, "拒绝清理非 ADCC 分支: %s" % branch
    code, _, err = run_git(repo, "worktree", "remove",
                           *(("--force",) if force else ()), worktree_path)
    if code != 0:
        # 未合并且非 force：拒绝而不是冒险删除
        return False, (err or "worktree remove 失败").strip()
    code, _, err = run_git(repo, "branch", "-d", branch)
    if code != 0 and force:
        run_git(repo, "branch", "-D", branch)
    return True, None


def worktree_of(path):
    """Return the worktree record owning ``path`` (any worktree)."""
    repo = detect_repo(path)
    if repo is None:
        return None, None
    for wt in list_worktrees(repo):
        try:
            if os.path.realpath(wt.get("path")) == os.path.realpath(path):
                return repo, wt
        except OSError:
            continue
    return repo, None
