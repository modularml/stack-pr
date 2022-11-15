# ===----------------------------------------------------------------------=== #
#
# This file is Modular Inc proprietary.
#
# ===----------------------------------------------------------------------=== #
#
# ------------------
# stack-pr.py submit
# ------------------
#
# Semantics:
#  1. Find merge-base (the most recent commit from 'main' in the current branch)
#  2. For each commit since merge base do:
#       a. If it doesnt have stack info:
#           - create a new head branch for it
#           - create a new PR for it
#           - base branch will be the previous commit in the stack
#       b. If it has stack info: verify its correctness.
#  3. Make sure all commits in the stack are annotated with stack info
#  4. Push all the head branches
#
# If 'submit' succeeds, you'll get all commits annotated with links to the
# corresponding PRs and names of the head branches. All the branches will be
# pushed to remote, and PRs are properly created and interconnected. Base
# branch of each PR will be the head branch of the previous PR, or 'main' for
# the first PR in the stack.
#
# ----------------
# stack-pr.py land
# ----------------
#
# Semantics:
#  1. Find merge-base (the most recent commit from 'main' in the current branch)
#  2. Check that all commits in the stack have stack info. If not, bail.
#  3. Check that the stack info is valid. If not, bail.
#  4. For each commit in the stack, from oldest to newest:
#     - set base branch to point to main
#     - merge the corresponding PR
#
# If 'land' succeeds, all the PRs from the stack will be merged into 'main',
# all the corresponding remote and local branches deleted.
#
# -------------------
# stack-pr.py abandon
# -------------------
#
# Semantics:
# For all commits in the stack that have valid stack-info:
# Close the corresponding PR, delete the remote and local branch, remove the
# stack-info from commit message.
#
# ===----------------------------------------------------------------------=== #

import argparse
import json
import os
import re

from git import (
    branch_exists,
    check_gh_installed,
    get_current_branch_name,
    get_gh_username,
    get_uncommitted_changes,
)
from shell_commands import get_command_output, run_shell_command
from typing import List, NamedTuple, Optional, Pattern

# A bunch of regexps for parsing commit messages and PR descriptions
RE_RAW_COMMIT_ID = re.compile(r"^(?P<commit>[a-f0-9]+)$", re.MULTILINE)
RE_RAW_AUTHOR = re.compile(
    r"^author (?P<author>(?P<name>[^<]+?) <(?P<email>[^>]+)>)", re.MULTILINE
)
RE_RAW_PARENT = re.compile(r"^parent (?P<commit>[a-f0-9]+)$", re.MULTILINE)
RE_RAW_TREE = re.compile(r"^tree (?P<tree>.+)$", re.MULTILINE)
RE_RAW_COMMIT_MSG_LINE = re.compile(r"^    (?P<line>.*)$", re.MULTILINE)

# stack-info: PR: https://github.com/modularml/test-ghstack/pull/30, branch: mvz/stack/7
RE_STACK_INFO_LINE = re.compile(
    r"\n^stack-info: PR: (.+), branch: (.+)\n?", re.MULTILINE
)
RE_PR_TOC = re.compile(
    r"^Stacked PRs:\r?\n(^ \* (__->__)?#\d+\r?\n)*\r?\n", re.MULTILINE
)

# ===----------------------------------------------------------------------=== #
# Error message templates
# ===----------------------------------------------------------------------=== #
ERROR_CANT_UPDATE_META = """Couldn't update stack metadata for
    {e}
"""
ERROR_CANT_CREATE_PR = """Could not create a new PR for:
    {e}

Failed trying to execute {cmd}
"""
ERROR_CANT_REBASE = """Could not rebase the PR on '{target}'. Failed to land PR:
    {e}

Failed trying to execute {cmd}
"""
ERROR_STACKINFO_MISSING = """A stack entry is missing some information:
    {e}

If you wanted to land a part of the stack, please use -B and -H options to
specify base and head revisions.
If you wanted to land the entire stack, please use 'submit' first.
If you hit this error trying to submit, please report a bug!
"""
ERROR_STACKINFO_BAD_LINK = """Bad PR link in stack metadata!
    {e}
"""
ERROR_STACKINFO_MALFORMED_RESPONSE = """Malformed response from GH!

Returned json object is missing a field {required_field}
PR info from github: {d}

Failed verification for:
     {e}
"""
ERROR_STACKINFO_PR_NOT_OPEN = """Associated PR is not in 'OPEN' state!
     {e}

PR info from github: {d}
"""
ERROR_STACKINFO_PR_NUMBER_MISMATCH = """PR number on github mismatches PR number in stack metadata!
     {e}

PR info from github: {d}
"""
ERROR_STACKINFO_PR_HEAD_MISMATCH = """Head branch name on github mismatches head branch name in stack metadata!
     {e}

PR info from github: {d}
"""
ERROR_STACKINFO_PR_BASE_MISMATCH = """Base branch name on github mismatches base branch name in stack metadata!
     {e}

If you are trying land the stack, please update it first by calling 'submit'.

PR info from github: {d}
"""
ERROR_REPO_DIRTY = """There are uncommitted changes.

Please commit or stash them before working with stacks.
"""

# ===----------------------------------------------------------------------=== #
# Class to work with git commit contents
# ===----------------------------------------------------------------------=== #
class CommitHeader:
    """
    Represents the information extracted from `git rev-list --header`
    """

    # The unparsed output from git rev-list --header
    raw_header: str

    def __init__(self, raw_header: str):
        self.raw_header = raw_header

    def _search_group(self, regex: Pattern[str], group: str) -> str:
        m = regex.search(self.raw_header)
        assert m
        return m.group(group)

    def tree(self) -> str:
        return self._search_group(RE_RAW_TREE, "tree")

    def title(self) -> str:
        return self._search_group(RE_RAW_COMMIT_MSG_LINE, "line")

    def commit_id(self) -> str:
        return self._search_group(RE_RAW_COMMIT_ID, "commit")

    def parents(self) -> List[str]:
        return [
            m.group("commit") for m in RE_RAW_PARENT.finditer(self.raw_header)
        ]

    def author(self) -> str:
        return self._search_group(RE_RAW_AUTHOR, "author")

    def author_name(self) -> str:
        return self._search_group(RE_RAW_AUTHOR, "name")

    def author_email(self) -> str:
        return self._search_group(RE_RAW_AUTHOR, "email")

    def commit_msg(self) -> str:
        return "\n".join(
            m.group("line")
            for m in RE_RAW_COMMIT_MSG_LINE.finditer(self.raw_header)
        )


# ===----------------------------------------------------------------------=== #
# Class to work with PR stack entries
# ===----------------------------------------------------------------------=== #
class StackEntry:
    """
    Represents an entry in a stack of PRs and contains associated info, such as
    linked PR, head and base branches, original git commit.
    """

    def __init__(self, commit: CommitHeader):
        self.commit = commit
        self._pr: Optional[str] = None
        self._base: Optional[str] = None
        self._head: Optional[str] = None
        self.need_update: bool = False

    @property
    def pr(self) -> str:
        if self._pr is None:
            raise ValueError("pr is not set")
        return self._pr

    @pr.setter
    def pr(self, pr: str):
        self._pr = pr

    def has_pr(self) -> bool:
        return self._pr is not None

    @property
    def head(self) -> str:
        if self._head is None:
            raise ValueError("head is not set")
        return self._head

    @head.setter
    def head(self, head: str):
        self._head = head

    def has_head(self) -> bool:
        return self._head is not None

    @property
    def base(self) -> str:
        if self._base is None:
            raise ValueError("base is not set")
        return self._base

    @base.setter
    def base(self, base: str):
        self._base = base

    def has_missing_info(self) -> bool:
        return None in (self._pr, self._head, self._base)

    def pprint(self):
        s = b(self.commit.commit_id()[:8])
        pr_string = None
        if self.has_pr():
            pr_string = blue("#" + self.pr.split("/")[-1])
        else:
            pr_string = red("no PR")
        branch_string = None
        if self._head or self._base:
            head_str = green(self._head) if self._head else red(str(self._head))
            base_str = green(self._base) if self._base else red(str(self._base))
            branch_string = f"'{head_str}' -> '{base_str}'"
        if pr_string or branch_string:
            s += " ("
        s += pr_string if pr_string else ""
        if branch_string:
            s += ", " if pr_string else ""
            s += branch_string
        if pr_string or branch_string:
            s += ")"
        s += ": " + self.commit.title()
        return s

    def __repr__(self):
        return self.pprint()

    def read_metadata(self):
        self.commit.commit_msg()
        x = RE_STACK_INFO_LINE.search(self.commit.commit_msg())
        if not x:
            return
        self.pr = x.group(1)
        self.head = x.group(2)


# ===----------------------------------------------------------------------=== #
# Utils for color printing
# ===----------------------------------------------------------------------=== #


class bcolors:
    HEADER = "\033[95m"
    OKBLUE = "\033[94m"
    OKCYAN = "\033[96m"
    OKGREEN = "\033[92m"
    WARNING = "\033[93m"
    FAIL = "\033[91m"
    ENDC = "\033[0m"
    BOLD = "\033[1m"
    UNDERLINE = "\033[4m"


def b(s: str):
    return bcolors.BOLD + s + bcolors.ENDC


def h(s: str):
    return bcolors.HEADER + s + bcolors.ENDC


def green(s: str):
    return bcolors.OKGREEN + s + bcolors.ENDC


def blue(s: str):
    return bcolors.OKBLUE + s + bcolors.ENDC


def red(s: str):
    return bcolors.FAIL + s + bcolors.ENDC


def error(msg):
    print(red("\nERROR: ") + msg)


# TODO: replace this with modular.utils.logging
def log(msg, level=0):
    print(msg)


# ===----------------------------------------------------------------------=== #
# Common utility functions
# ===----------------------------------------------------------------------=== #
def split_header(s: str) -> List[CommitHeader]:
    return [CommitHeader(h) for h in s.split("\0")[:-1]]


def is_valid_ref(ref: str) -> bool:
    splits = ref.rsplit("/", 2)
    if len(splits) < 3:
        return False
    return splits[-2] == "stack" and splits[-1].isnumeric()


def last(ref: str, sep: str = "/") -> str:
    return ref.rsplit("/", 1)[1]


# TODO: Move to 'modular.utils.git'
def is_ancestor(commit1: str, commit2: str) -> bool:
    """
    Returns true if 'commit1' is an ancestor of 'commit2'.
    """
    # TODO: We need to check returncode of this command more carefully, as the
    # command simply might fail (rc != 0 and rc != 1).
    p = run_shell_command(
        ["git", "merge-base", "--is-ancestor", commit1, commit2], check=False
    )
    return p.returncode == 0


def is_repo_clean() -> bool:
    """
    Returns true if there are no uncommitted changes in the repo.
    """
    changes = get_uncommitted_changes()
    changes.pop("??", [])  # We don't care about untracked files
    return not bool(changes)


def get_stack(base: str, head: str) -> List[StackEntry]:
    if not is_ancestor(base, head):
        error(
            f"{base} is not an ancestor of {head}.\n"
            "Could not find commits for the stack."
        )
        exit(1)

    # Find list of commits since merge base.
    st: List[StackEntry] = []
    stack = (
        split_header(
            get_command_output(
                ["git", "rev-list", "--header", "^" + base, head]
            )
        )
    )[::-1]

    for i in range(len(stack)):
        entry = StackEntry(stack[i])
        st.append(entry)

    for e in st:
        e.read_metadata()
    return st


def set_base_branches(st: List[StackEntry], target: str):
    prev_branch = target
    for e in st:
        e.base, prev_branch = prev_branch, e._head


def verify(st: List[StackEntry], check_base: bool = False):
    log(h("Verifying stack info"), level=1)
    for e in st:
        if e.has_missing_info():
            error(ERROR_STACKINFO_MISSING.format(**locals()))
            raise RuntimeError

        if len(e.pr.split("/")) == 0 or not last(e.pr).isnumeric():
            error(ERROR_STACKINFO_BAD_LINK.format(**locals()))
            raise RuntimeError

        ghinfo = get_command_output(
            [
                "gh",
                "pr",
                "view",
                e.pr,
                "--json",
                "baseRefName,headRefName,number,state,body,title,url",
            ]
        )
        d = json.loads(ghinfo)
        for required_field in ["state", "number", "baseRefName", "headRefName"]:
            if required_field not in d:
                error(ERROR_STACKINFO_MALFORMED_RESPONSE.format(**locals()))
                raise RuntimeError

        if d["state"] != "OPEN":
            error(ERROR_STACKINFO_PR_NOT_OPEN.format(**locals()))
            raise RuntimeError

        if int(last(e.pr)) != d["number"]:
            error(ERROR_STACKINFO_PR_NUMBER_MISMATCH.format(**locals()))
            raise RuntimeError

        if e.head != d["headRefName"]:
            error(ERROR_STACKINFO_PR_HEAD_MISMATCH.format(**locals()))
            raise RuntimeError

        # 'Base' branch might diverge when the stack is modified (e.g. when a
        # new commit is added to the middle of the stack). It is not an issue
        # if we're updating the stack (i.e. in 'submit'), but it is an issue if
        # we are trying to land it.
        if check_base and e.base != d["baseRefName"]:
            error(ERROR_STACKINFO_PR_BASE_MISMATCH.format(**locals()))
            raise RuntimeError


def print_stack(st: List[StackEntry], level=1):
    log(b("Stack:"), level=level)
    for e in reversed(st):
        log("   * " + e.pprint(), level=level)


# ===----------------------------------------------------------------------=== #
# SUBMIT
# ===----------------------------------------------------------------------=== #
def add_or_update_metadata(e: StackEntry, needs_rebase: bool) -> bool:
    if needs_rebase:
        run_shell_command(
            [
                "git",
                "rebase",
                e.base,
                e.head,
                "--committer-date-is-author-date",
            ]
        )
    else:
        run_shell_command(["git", "checkout", e.head])

    commit_msg = e.commit.commit_msg()
    found_metadata = RE_STACK_INFO_LINE.search(commit_msg)
    if found_metadata:
        # Metadata is already there, skip this commit
        return needs_rebase

    # Add the stack info metadata to the commit message
    commit_msg += f"\n\nstack-info: PR: {e.pr}, branch: {e.head}"
    run_shell_command(
        ["git", "commit", "--amend", "-F", "-"],
        shell=False,
        input=commit_msg.encode(),
    )
    return True


def get_available_branch_name(remote: str) -> str:
    username = get_gh_username()

    refs = get_command_output(
        [
            "git",
            "for-each-ref",
            f"refs/remotes/{remote}/{username}/stack",
            "--format='%(refname)'",
        ]
    ).split()

    max_ref_num = (
        max(int(last(ref)) for ref in filter(is_valid_ref, refs)) if refs else 0
    )
    new_branch_id = max_ref_num + 1

    return f"{username}/stack/{new_branch_id}"


def get_next_available_branch_name(name: str) -> str:
    base, id = name.rsplit("/", 1)
    return f"{base}/{int(id) + 1}"


def set_head_branches(st: List[StackEntry], remote: str):
    """Set the head ref for each stack entry if it doesn't already have one."""

    run_shell_command(["git", "fetch", "--prune", remote])
    available_name = get_available_branch_name(remote)
    for e in filter(lambda e: not e.has_head(), st):
        e.head = available_name
        available_name = get_next_available_branch_name(available_name)


def init_local_branches(st: List[StackEntry], remote: str):
    log(h("Initializing local branches"), level=1)
    set_head_branches(st, remote)
    for e in st:
        run_shell_command(
            ["git", "checkout", e.commit.commit_id(), "-B", e.head]
        )


def push_branches(st: List[StackEntry], remote):
    log(h("Updating remote branches"), level=1)
    cmd = ["git", "push", "-f", remote]
    cmd.extend([f"{e.head}:{e.head}" for e in st])
    run_shell_command(cmd)


def create_pr(e: StackEntry, is_draft: bool, reviewer: str = ""):
    # Don't do anything if the PR already exists
    if e.has_pr():
        return
    log(h("Creating PR " + green(f"'{e.head}' -> '{e.base}'")), level=1)
    cmd = [
        "gh",
        "pr",
        "create",
        "-B",
        e.base,
        "-H",
        e.head,
        "-t",
        e.commit.title(),
        "-F",
        "-",
    ]
    if reviewer:
        cmd.extend(["--reviewer", reviewer])
    if is_draft:
        cmd.append("--draft")

    try:
        r = get_command_output(
            cmd, shell=False, input=e.commit.commit_msg().encode()
        )
    except Exception:
        error(ERROR_CANT_CREATE_PR.format(**locals()))
        raise

    log(b("Created: ") + r, level=2)
    e.pr = r.split()[-1]


def generate_toc(st: List[StackEntry], current: str) -> str:
    def toc_entry(se: StackEntry) -> str:
        pr_id = last(se.pr)
        arrow = "__->__" if pr_id == current else ""
        return f" * {arrow}#{pr_id}\n"

    entries = (toc_entry(se) for se in st[::-1])
    return f"Stacked PRs:\n{''.join(entries)}\n"


def add_cross_links(st: List[StackEntry]):
    for e in st:
        pr_id = last(e.pr)
        pr_toc = generate_toc(st, pr_id)

        title = e.commit.title()
        body = e.commit.commit_msg()

        # Strip title from the body - we will print it separately.
        body = "\n".join(body.splitlines()[1:])

        # Strip stack-info from the body, nothing interesting there.
        body = RE_STACK_INFO_LINE.sub("", body)
        pr_body = "\n".join(
            [
                f"{pr_toc}",
                f"### {title}",
                "",
                f"{body}\n",
            ]
        )

        run_shell_command(
            ["gh", "pr", "edit", e.pr, "-t", title, "-F", "-", "-B", e.base],
            shell=False,
            input=pr_body.encode(),
        )


# Temporarily set base branches of existing PRs to the bottom of the stack.
# This needs to be done to avoid PRs getting closed when commits are
# rearranged.
#
# For instance, if we first had
#
# Stack:
#    * #2 (stack/2 -> stack/1)  aaaa
#    * #1 (stack/1 -> main)     bbbb
#
# And then swapped the order of the commits locally and tried submitting again
# we would have:
#
# Stack:
#    * #1 (stack/1 -> main)     bbbb
#    * #2 (stack/2 -> stack/1)  aaaa
#
# Now we need to 1) change bases of the PRs, 2) push branches stack/1 and
# stack/2. If we push stack/1, then PR #2 gets automatically closed, since its
# head branch will contain all the commits from its base branch.
#
# To avoid this, we temporarily set all base branches to point to 'main' - once
# all the branches are pushed we can set the actual base branches.
def reset_remote_base_branches(st: List[StackEntry], target: str):
    log(h("Resetting remote base branches"), level=1)

    for e in filter(lambda e: e.has_pr(), st):
        run_shell_command(["gh", "pr", "edit", e.pr, "-B", target], shell=False)


# If local 'main' lags behind 'origin/main', and 'head' contains all commits
# from 'main' to 'origin/main', then we can just move 'main' forward.
#
# It is a common user mistake to not update their local branch, run 'submit',
# and end up with a huge stack of changes that are already merged.
# We could've told users to update their local branch in that scenario, but why
# not to do it for them?
# In the very unlikely case when they indeed wanted to include changes that are
# already in remote into their stack, they can use a different notation for the
# base (e.g. explicit hash of the commit) - but most probably nobody ever would
# need that.
def should_update_local_base(head: str, base: str, remote: str, target: str):
    base_hash = get_command_output(["git", "rev-parse", base], shell=False)
    target_hash = get_command_output(
        ["git", "rev-parse", f"{remote}/{target}"], shell=False
    )
    return (
        is_ancestor(base, f"{remote}/{target}")
        and is_ancestor(f"{remote}/{target}", head)
        and base_hash != target_hash
    )


def update_local_base(base: str, remote: str, target: str):
    log(h(f"Updating local branch {base} to {remote}/{target}"), level=1)
    run_shell_command(["git", "rebase", f"{remote}/{target}", base])


class CommonArgs(NamedTuple):
    """Class to help type checkers and separate implementation for CLI args."""

    base: str
    head: str
    remote: str
    target: str

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> "CommonArgs":
        return cls(args.base, args.head, args.remote, args.target)


# ===----------------------------------------------------------------------=== #
# Entry point for 'submit' command
# ===----------------------------------------------------------------------=== #
def command_submit(args: CommonArgs, draft: bool, reviewer: str):
    log(h("SUBMIT"), level=1)

    current_branch = get_current_branch_name()

    if should_update_local_base(args.head, args.base, args.remote, args.target):
        update_local_base(args.base, args.remote, args.target)
        run_shell_command(["git", "checkout", current_branch])

    # Determine what commits belong to the stack
    st = get_stack(args.base, args.head)
    if not st:
        log(h("Empty stack!"), level=1)
        log(h(blue("SUCCESS!")), level=1)
        return

    # Create local branches and initialize base and head fields in the stack
    # elements
    init_local_branches(st, args.remote)
    set_base_branches(st, args.target)
    print_stack(st)

    # If the current branch contains commits from the stack, we will need to
    # rebase it in the end since the commits will be modified.
    top_branch = st[-1].head
    need_to_rebase_current = is_ancestor(top_branch, current_branch)

    reset_remote_base_branches(st, args.target)

    # Push local branches to remote
    push_branches(st, args.remote)

    # Now we have all the branches, so we can create the corresponding PRs
    log(h("Submitting PRs"), level=1)
    for e in st:
        create_pr(e, draft, reviewer)

    # Verify consistency in everything we have so far
    verify(st)

    # Embed stack-info into commit messages
    log(h("Updating commit messages with stack metadata"), level=1)
    needs_rebase = False
    for e in st:
        try:
            needs_rebase = add_or_update_metadata(e, needs_rebase)
        except Exception:
            error(ERROR_CANT_UPDATE_META.format(**locals()))
            raise

    push_branches(st, args.remote)

    log(h("Adding cross-links to PRs"), level=1)
    add_cross_links(st)

    if need_to_rebase_current:
        log(h(f"Rebasing the original branch '{current_branch}'"), level=1)
        run_shell_command(
            [
                "git",
                "rebase",
                top_branch,
                current_branch,
                "--committer-date-is-author-date",
            ]
        )
    else:
        log(h(f"Checking out the original branch '{current_branch}'"), level=1)
        run_shell_command(["git", "checkout", current_branch])

    log(h(blue("SUCCESS!")), level=1)


# ===----------------------------------------------------------------------=== #
# LAND
# ===----------------------------------------------------------------------=== #
def land_pr(e: StackEntry, remote: str, target: str):
    log(b("Landing ") + e.pprint(), level=2)
    # Rebase the head branch to the most recent 'origin/main'
    run_shell_command(["git", "fetch", "--prune", remote])
    cmd = [
        "git",
        "rebase",
        f"{remote}/{target}",
        e.head,
        "--committer-date-is-author-date",
    ]
    try:
        run_shell_command(cmd)
    except Exception:
        error(ERROR_CANT_REBASE.format(**locals()))
        raise
    run_shell_command(["git", "push", remote, "-f", f"{e.head}:{e.head}"])

    # Switch PR base branch to 'main'
    run_shell_command(["gh", "pr", "edit", e.pr, "-B", target])

    # Form the commit message: it should contain the original commit message
    # and nothing else.
    pr_body = RE_STACK_INFO_LINE.sub("", e.commit.commit_msg())

    # Since title is passed separately, we need to strip the first line from the
    # body:
    lines = pr_body.splitlines()
    pr_id = last(e.pr)
    title = f"{lines[0]} (#{pr_id})"
    pr_body = "\n".join(lines[1:]) or " "
    run_shell_command(
        ["gh", "pr", "merge", e.pr, "--squash", "-t", title, "-F", "-"],
        shell=False,
        input=pr_body.encode(),
    )


def delete_branches(st: List[StackEntry], remote: str):
    # Delete local branches
    cmd = ["git", "branch", "-D"]
    cmd.extend([e.head for e in st if e.head])
    run_shell_command(cmd, check=False)

    # Delete remote branches
    username = get_gh_username()
    refs = get_command_output(
        [
            "git",
            "for-each-ref",
            f"refs/remotes/{remote}/{username}/stack",
            "--format='%(refname)'",
        ]
    ).split()
    refs = [x.replace(f"refs/remotes/{remote}/", "") for x in refs]
    remote_branches_to_delete = [e.head for e in st if e.head in refs]

    if remote_branches_to_delete:
        cmd = ["git", "push", "-f", remote]
        cmd.extend([f":{branch}" for branch in remote_branches_to_delete])
        run_shell_command(cmd, check=False)


# ===----------------------------------------------------------------------=== #
# Entry point for 'land' command
# ===----------------------------------------------------------------------=== #
def command_land(args: CommonArgs):
    log(h("LAND"), level=1)

    current_branch = get_current_branch_name()

    if should_update_local_base(args.head, args.base, args.remote, args.target):
        update_local_base(args.base, args.remote, args.target)
        run_shell_command(["git", "checkout", current_branch])

    # Determine what commits belong to the stack
    st = get_stack(args.base, args.head)
    if not st:
        log(h("Empty stack!"), level=1)
        log(h(blue("SUCCESS!")), level=1)
        return

    # Initialize base branches of elements in the stack. Head branches should
    # already be there from the metadata that commits need to have by that
    # point.
    set_base_branches(st, args.target)
    print_stack(st)

    # Verify that the stack is correct before trying to land it.
    verify(st, check_base=True)

    # All good, land!
    for e in st:
        land_pr(e, args.remote, args.target)

    # Delete local and remote stack branches
    run_shell_command(["git", "fetch", "--prune", args.remote])
    run_shell_command(["git", "checkout", current_branch])

    log(h("Deleting local and remote branches"), level=1)
    delete_branches(st, args.remote)

    # If local branch {target} exists, rebase it on the remote/target
    if branch_exists(args.target):
        run_shell_command(
            ["git", "rebase", f"{args.remote}/{args.target}", args.target]
        )
    run_shell_command(
        ["git", "rebase", f"{args.remote}/{args.target}", current_branch]
    )

    log(h(blue("SUCCESS!")), level=1)


# ===----------------------------------------------------------------------=== #
# ABANDON
# ===----------------------------------------------------------------------=== #
def strip_metadata(e: StackEntry) -> str:
    m = e.commit.commit_msg()

    m = RE_STACK_INFO_LINE.sub("", m)
    run_shell_command(
        ["git", "rebase", e.base, e.head, "--committer-date-is-author-date"]
    )
    run_shell_command(
        ["git", "commit", "--amend", "-F", "-"],
        shell=False,
        input=m.encode(),
    )

    return get_command_output(["git", "rev-parse", e.head], shell=False)


# ===----------------------------------------------------------------------=== #
# Entry point for 'abandon' command
# ===----------------------------------------------------------------------=== #
def command_abandon(args: CommonArgs):
    log(h("ABANDON"), level=1)
    st = get_stack(args.base, args.head)
    if not st:
        log(h("Empty stack!"), level=1)
        log(h(blue("SUCCESS!")), level=1)
        return
    current_branch = get_current_branch_name()

    init_local_branches(st, args.remote)
    set_base_branches(st, args.target)
    print_stack(st)

    log(h("Stripping stack metadata from commit messages"), level=1)

    last_hash = ""
    for e in st:
        last_hash = strip_metadata(e)

    log(h("Rebasing the current branch on top of updated top branch"), level=1)
    run_shell_command(["git", "rebase", last_hash, current_branch], shell=False)

    log(h("Deleting local and remote branches"), level=1)
    delete_branches(st, args.remote)
    log(h(blue("SUCCESS!")), level=1)


# ===----------------------------------------------------------------------=== #
# Entry point for 'view' command
# ===----------------------------------------------------------------------=== #
def command_view(args: CommonArgs):
    log(h("VIEW"), level=1)

    if should_update_local_base(args.head, args.base, args.remote, args.target):
        log(
            red(
                f"\nWarning: Local '{args.base}' is behind"
                f" '{args.remote}/{args.target}'!"
            ),
            level=1,
        )
        log(
            "Consider updating your local branch by"
            " running the following commands:",
            level=1,
        )
        log(
            b(f"   git rebase {args.remote}/{args.target} {args.base}"),
            level=1,
        )
        log(
            b(f"   git checkout {get_current_branch_name()}\n"),
            level=1,
        )

    st = get_stack(args.base, args.head)

    set_head_branches(st, args.remote)
    set_base_branches(st, args.target)
    print_stack(st)
    log(h(blue("SUCCESS!")), level=1)


# ===----------------------------------------------------------------------=== #
# Main entry point
# ===----------------------------------------------------------------------=== #
def parse_args() -> argparse.Namespace:
    """Helper for CL option definition and parsing logic."""
    parser = argparse.ArgumentParser()
    parser.add_argument("-R", "--remote", default="origin", help="Remote name")
    parser.add_argument(
        "-B", "--base", default="main", help="Local base branch"
    )
    parser.add_argument(
        "-H", "--head", default="HEAD", help="Local head branch"
    )
    parser.add_argument(
        "-T", "--target", default="main", help="Remote target branch"
    )

    subparsers = parser.add_subparsers(help="sub-command help", dest="command")
    parser_submit = subparsers.add_parser(
        "submit", aliases=["export"], help="Submit a stack of PRs"
    )
    parser_submit.add_argument(
        "-d",
        "--draft",
        action="store_true",
        default=False,
        help="Submit PRs in draft mode",
    )
    parser_submit.add_argument(
        "--reviewer",
        default=os.getenv("STACK_PR_DEFAULT_REVIEWER", default=""),
        help="List of reviewers for the PR",
    )

    subparsers.add_parser("land", help="Land the current stack")
    subparsers.add_parser("abandon", help="Abandon the current stack")
    subparsers.add_parser("view", help="Inspect the current stack")

    return parser.parse_args()


def main():
    args = parse_args()
    common_args = CommonArgs.from_args(args)

    check_gh_installed()

    current_branch = get_current_branch_name()
    try:
        if args.command != "view":
            if not is_repo_clean():
                error(ERROR_REPO_DIRTY)
                return

        if args.command in ["submit", "export"]:
            command_submit(common_args, args.draft, args.reviewer)
        elif args.command == "land":
            command_land(common_args)
        elif args.command == "abandon":
            command_abandon(common_args)
        elif args.command == "view":
            command_view(common_args)
        else:
            raise Exception(f"Unknown command {args.command}")
    except Exception:
        # If something failed, checkout the original branch
        run_shell_command(["git", "checkout", current_branch])
        raise


if __name__ == "__main__":
    main()
