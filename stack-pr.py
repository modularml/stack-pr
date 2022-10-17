# stack-pr: a tool for working with stacked PRs on github.
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
import os
import re
import subprocess
from modular.utils.typing import Pattern, List, Optional, Union
import json

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

# A global used to suppress shell commands output
QUIET_MODE = False

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

    commit: Optional[CommitHeader]
    pr: Optional[str]
    base: Optional[str]
    head: Optional[str]
    need_update: bool

    def __init__(self):
        self.commit = None
        self.pr = None
        self.base = self.head = None
        self.need_update = False

    def pprint(self):
        s = ""
        s += b(self.commit.commit_id()[:8])
        pr_string = None
        if self.pr:
            pr_string = blue("#" + self.pr.split("/")[-1])
        else:
            pr_string = red("no PR")
        branch_string = None
        if self.head and self.base:
            branch_string = green(f"'{self.head}' -> '{self.base}'")
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
        s = ""
        s += "\nCommit: "
        if self.commit:
            s += self.commit.commit_id()[:12] + "\n"
            s += self.commit.commit_msg() + "\n"
        else:
            s += "None\n"
        if self.pr:
            s += f"PR: {self.pr}\n"
        else:
            s += "PR: None\n"
        s += f"{self.head} --> {self.base}\n"
        return s

    def read_metadata(self):
        self.commit.commit_msg()
        x = RE_STACK_INFO_LINE.search(self.commit.commit_msg())
        if not x:
            return
        self.pr = x.group(1)
        self.head = x.group(2)

    def add_or_update_metadata(self):
        m = self.commit.commit_msg()
        x = RE_STACK_INFO_LINE.search(m)
        needs_update = False
        if x:
            if self.pr != x.group(1) or self.head != x.group(2):
                needs_update = True
        if not x:
            m += "\n\nstack-info: PR: xxx, branch: xxx"
            needs_update = True

        sh(
            "git",
            "rebase",
            self.base,
            self.head,
            "--committer-date-is-author-date",
        )
        if needs_update:
            m = RE_STACK_INFO_LINE.sub(
                f"\nstack-info: PR: {self.pr}, branch: {self.head}", m
            )
            sh("git", "commit", "--amend", "-F", "-", input=m)

    def strip_metadata(self):
        m = self.commit.commit_msg()
        x = RE_STACK_INFO_LINE.search(m)
        if not x:
            return

        m = RE_STACK_INFO_LINE.sub("", m)
        sh("git", "checkout", self.head)
        sh("git", "rebase", self.base, "--committer-date-is-author-date")
        sh("git", "commit", "--amend", "-F", "-", input=m)


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


# ===----------------------------------------------------------------------=== #
# Utils for invoking shell commands, parsing output, etc.
# ===----------------------------------------------------------------------=== #

# TODO: raise exceptions on errors


def sh(
    *args: str, input: Optional[str] = None, raise_on_err: bool = True
) -> str:
    print("+", *args)
    serr = subprocess.PIPE
    sout = subprocess.PIPE
    if QUIET_MODE:
        serr = None

    sin = None
    inp = None
    if input:
        sin = subprocess.PIPE
        inp = input.encode()
    proc = subprocess.Popen(args, stdout=sout, stderr=serr, stdin=sin)
    (out, err) = proc.communicate(input=inp)
    exitcode = proc.returncode
    if exitcode and raise_on_err:
        msg = "Shell command failed:\n"
        msg += str(args)
        msg += "\nCommand stdout:\n"
        msg += out.decode()
        msg += f"\nError code: {exitcode}\n"
        raise RuntimeError(msg)

    return out.decode().rstrip()


# Copypaste from export-pr
# TODO: import it instead
def get_current_username():
    # Query the current user.
    user_query = subprocess.check_output(
        'gh api graphql -f owner="UserCurrent" -f query="query { viewer {'
        ' login } }"',
        shell=True,
    )

    # Extract the login name.
    m = re.search(r"\"login\":\"(.*?)\"", user_query.decode("utf-8"))
    if not m:
        print(
            "Unable to find current github user name when creating anonymous"
            " branch"
        )
        exit(1)

    return m.group(1)


# Copypaste from export-pr
# TODO: import it instead
def get_current_branch_name():
    return (
        subprocess.check_output("git rev-parse --abbrev-ref HEAD", shell=True)
        .decode("utf-8")
        .strip()
    )


# Copypaste from export-pr
# TODO: import it instead
def git_branch_exists(branch: str):
    if not os.system("git show-ref --quiet refs/heads/" + branch):
        return True
    return False


def split_header(s: str) -> List[CommitHeader]:
    return list(map(CommitHeader, s.split("\0")[:-1]))


def is_valid_ref(ref: str) -> bool:
    splits = ref.split("/")
    if len(splits) < 3:
        return False
    else:
        return splits[-1].isnumeric()


def create_pr(e: StackEntry, is_draft: bool):
    print(h("Creating PR " + green(f"'{e.head}' -> '{e.base}'")))
    if is_draft:
        r = sh(
            "gh", "pr", "create", "-B", e.base, "-H", e.head, "-f", "--draft"
        )
    else:
        r = sh("gh", "pr", "create", "-B", e.base, "-H", e.head, "-f")
    print(b("Created: ") + r)
    return r.split()[-1]


def get_stack(remote: str, main_branch: str) -> List[StackEntry]:
    # Find merge base.
    sh("git", "fetch", "--prune", remote)
    base = sh("git", "merge-base", f"{remote}/{main_branch}", "HEAD")
    base_obj = split_header(
        sh("git", "rev-list", "--header", "^" + base + "^@", base)
    )[0]

    # find list of commits since merge base
    st: List[StackEntry] = []
    stack = (
        split_header(sh("git", "rev-list", "--header", "^" + base, "HEAD"))
    )[::-1]

    for i in range(len(stack)):
        entry = StackEntry()
        entry.commit = stack[i]
        st.append(entry)

    for e in st:
        e.read_metadata()
    return st


def set_base_branches(st: List[StackEntry], main_branch: str):
    prev_branch = main_branch
    for e in st:
        e.base = prev_branch
        prev_branch = e.head


def init_branch(e: StackEntry, remote: str):
    if e.head:
        print(h(f"Resetting branch {e.head}"))
        sh("git", "checkout", e.head)
        sh("git", "reset", "--hard", e.commit.commit_id())
        return

    username = get_current_username()

    refs = sh(
        "git",
        "for-each-ref",
        f"refs/remotes/{remote}/{username}",
        "--format=%(refname)",
    ).split()

    refs = list(filter(is_valid_ref, refs))
    max_ref_num = max(int(ref.split("/")[-1]) for ref in refs) if refs else 0
    new_branch_id = max_ref_num + 1

    # TODO: check if local branch already exists
    r = f"{username}/stack/{new_branch_id}"
    e.head = r

    print(h(f"Creating branch {e.head}"))
    sh("git", "checkout", e.commit.commit_id(), "-b", r)
    sh("git", "push", remote, f"{r}:{r}")
    return r


def verify(st: List[StackEntry], strict=False):
    print(h("Verifying stack info"))
    for e in st:
        if e.pr == None or e.head == None or e.base == None:
            if strict:
                msg = "A stack entry is missing some information:"
                msg += f"Commit: {e.commit.commit_id()}, PR: {e.pr}, head: {e.head}, base: {e.base}"
                msg += "\nPlease file a bug!"
                raise RuntimeError(msg)
            else:
                continue

        if len(e.pr.split("/")) == 0 or not e.pr.split("/")[-1].isnumeric():
            msg = "Bad PR link in stack metadata!"
            msg += f"Commit: {e.commit.commit_id()}, PR: {e.pr}, head: {e.head}, base: {e.base}"
            msg += "\nPlease file a bug!"
            raise RuntimeError(msg)

        ghinfo = sh(
            "gh",
            "pr",
            "view",
            e.pr,
            "--json",
            "baseRefName,headRefName,number,state,body,title,url",
        )
        d = json.loads(ghinfo)
        for required_field in ["state", "number", "baseRefName", "headRefName"]:
            if required_field not in d:
                msg = "Malformed response from GH!"
                msg += (
                    f"Returned json object is missing a field {required_field}"
                )
                msg += f"Commit: {e.commit.commit_id()}, PR: {e.pr}, head: {e.head}, base: {e.base}"
                msg += "PR info from github: " + str(d)
                msg += "\nPlease file a bug!"
                raise RuntimeError(msg)

        if d["state"] != "OPEN":
            msg = "Associated PR is not in 'OPEN' state!"
            msg += f"Commit: {e.commit.commit_id()}, PR: {e.pr}, head: {e.head}, base: {e.base}"
            msg += "PR info from github: " + str(d)
            msg += "\nPlease file a bug!"
            raise RuntimeError(msg)

        if int(e.pr.split("/")[-1]) != int(d["number"]):
            msg = "PR number on github mismatches PR number in stack metadata!"
            msg += f"Commit: {e.commit.commit_id()}, PR: {e.pr}, head: {e.head}, base: {e.base}"
            msg += "PR info from github: " + str(d)
            msg += "\nPlease file a bug!"
            raise RuntimeError(msg)

        if e.head != d["headRefName"]:
            msg = "Head branch name on github mismatches head branch name in stack metadata!"
            msg += f"Commit: {e.commit.commit_id()}, PR: {e.pr}, head: {e.head}, base: {e.base}"
            msg += "PR info from github: " + str(d)
            msg += "\nPlease file a bug!"
            raise RuntimeError(msg)

        if e.base != d["baseRefName"]:
            msg = "Base branch name on github mismatches base branch name in stack metadata!"
            msg += f"Commit: {e.commit.commit_id()}, PR: {e.pr}, head: {e.head}, base: {e.base}"
            msg += "PR info from github: " + str(d)
            msg += "\nPlease file a bug!"
            raise RuntimeError(msg)


def land_pr(e: StackEntry, remote: str, main_branch: str):
    print(b("Landing ") + e.pprint())
    sh("git", "fetch", "--prune", remote)
    sh(
        "git",
        "rebase",
        f"{remote}/{main_branch}",
        e.head,
        "--committer-date-is-author-date",
    )
    # TODO: append PR number to the title, strip stack info
    # TODO: check for errors
    sh("git", "push", remote, "-f", f"{e.head}:{e.head}")
    sh("gh", "pr", "edit", e.pr, "-B", main_branch)
    sh("gh", "pr", "merge", e.pr, "-r")


def delete_branches(st: List[StackEntry], remote: str):
    for e in st:
        sh("git", "branch", "-D", e.head)
        sh("git", "push", "-f", remote, f":{e.head}")


def print_stack(st: List[StackEntry]):
    print(b("Stack:"))
    for e in st[::-1]:
        print("   * " + e.pprint())


def generate_toc(st: List[StackEntry], current: int):
    res = "Stacked PRs:\n"
    for e in st[::-1]:
        pr_id = e.pr.split("/")[-1]
        arrow = ""
        if pr_id == current:
            arrow = "__->__"
        res += f" * {arrow}#{pr_id}\n"
    res += "\n"
    return res


def add_cross_links(st: List[StackEntry]):
    for e in st:
        ghinfo = sh(
            "gh",
            "pr",
            "view",
            e.pr,
            "--json",
            "body,title",
        )
        d = json.loads(ghinfo)
        body = d["body"]
        title = d["title"]
        if not RE_PR_TOC.search(body):
            toc_placeholder = "Stacked PRs:\n * #0\n\n"
            body = toc_placeholder + body

        pr_id = e.pr.split("/")[-1]
        body = RE_PR_TOC.sub(generate_toc(st, pr_id), body)

        sh("gh", "pr", "edit", e.pr, "-t", title, "-F", "-", input=body)


def check_if_local_main_matches_origin(remote: str, main_branch: str):
    diff = sh("git", "diff", main_branch, f"{remote}/{main_branch}")
    if diff == "":
        return
    print(
        red("ERROR: ")
        + f"""Local '{main_branch}' does not match '{remote}/{main_branch}'.

Please fix that before submitting a stack:

    # Save the current '{main_branch}' branch:
    git checkout {main_branch} -b tmp_branch

    # Reset local '{main_branch}' to '{remote}/{main_branch}'
    git checkout {main_branch}
    git reset --hard {remote}/{main_branch}
"""
    )
    exit(0)


# ===----------------------------------------------------------------------=== #
# Entry point for 'submit' command
# ===----------------------------------------------------------------------=== #
def command_submit(args):
    print(h("SUBMIT"))
    # TODO: we should only care that local 'main' exists and stack commits can
    # be applied to it.
    # Divergence with 'origin/commit' should not be considered at 'submit' step
    # - it only matters for 'land'
    check_if_local_main_matches_origin(args.remote, args.main_branch)

    st = get_stack(args.remote, args.main_branch)
    print_stack(st)
    if not st:
        print(h(blue("SUCCESS!")))
        return

    current_branch = get_current_branch_name()

    for e in st:
        init_branch(e, args.remote)

    set_base_branches(st, args.main_branch)

    for e in st:
        if e.pr == None:
            try:
                e.pr = create_pr(e, args.draft)
            except RuntimeError as e:
                print(red("ERROR: "), " Couldn't create a PR for")
                print("    " + e.pprint())
                print("Please submit a bug!")
                raise e

    verify(st, strict=True)

    # Start writing out changes.
    print(h("Updating commit messages with stack metadata"))
    for e in st:
        try:
            e.add_or_update_metadata()
        except RuntimeError as e:
            print(red("ERROR: "), " Couldn't update stack metadata for")
            print("    " + e.pprint())
            print("Please submit a bug!")
            raise e

    print(h("Updating remote branches"))
    for e in st:
        try:
            sh("git", "push", args.remote, "-f", f"{e.head}:{e.head}")
        except RuntimeError as e:
            print(red("ERROR: "), " Couldn't push head branch to remote:")
            print("    " + e.pprint())
            print("Please submit a bug!")
            raise e

    print(h(f"Checking out the origin branch '{current_branch}'"))
    sh("git", "checkout", current_branch)
    sh("git", "reset", "--hard", st[-1].head)

    print(h("Adding cross-links to PRs"))
    add_cross_links(st)
    print(h(blue("SUCCESS!")))


# ===----------------------------------------------------------------------=== #
# Entry point for 'land' command
# ===----------------------------------------------------------------------=== #
def command_land(args):
    print(h("LAND"))
    check_if_local_main_matches_origin(args.remote, args.main_branch)
    st = get_stack(args.remote, args.main_branch)

    set_base_branches(st, args.main_branch)
    print_stack(st)
    if not st:
        print(h(blue("SUCCESS!")))
        return

    current_branch = get_current_branch_name()

    verify(st)

    # All good, land!
    for e in st:
        land_pr(e, args.remote, args.main_branch)

    # TODO: Gracefully undo whatever possible if landing fails

    sh("git", "fetch", "--prune", args.remote)
    sh("git", "checkout", current_branch)
    sh("git", "reset", "--hard", st[-1].head)

    print(h("Deleting local and remote branches"))
    sh("git", "checkout", f"{args.remote}/{args.main_branch}")
    delete_branches(st, args.remote)
    sh("git", "rebase", f"{args.remote}/{args.main_branch}", args.main_branch)
    print(h(blue("SUCCESS!")))


# ===----------------------------------------------------------------------=== #
# Entry point for 'abandon' command
# ===----------------------------------------------------------------------=== #
def command_abandon(args):
    print(h("ABANDON"))
    check_if_local_main_matches_origin(args.remote, args.main_branch)
    st = get_stack(args.remote, args.main_branch)

    set_base_branches(st, args.main_branch)
    print_stack(st)
    if not st:
        print(h(blue("SUCCESS!")))
        return
    current_branch = get_current_branch_name()

    print(h("Stripping stack metadata from commit messages"))
    for e in st:
        e.strip_metadata()

    print(h("Deleting local and remote branches"))
    last_branch = st[-1].head
    sh("git", "checkout", current_branch)
    sh("git", "reset", "--hard", st[-1].head)

    delete_branches(st, args.remote)
    print(h(blue("SUCCESS!")))


# ===----------------------------------------------------------------------=== #
# Main entry point
# ===----------------------------------------------------------------------=== #
def main():
    global QUIET_MODE
    parser = argparse.ArgumentParser()

    subparsers = parser.add_subparsers(help="sub-command help", dest="command")
    parser_submit = subparsers.add_parser(
        "submit", help="Submit a stack of PRs"
    )
    parser_submit.add_argument(
        "--main-branch", default="main", help="Target branch"
    )
    parser_submit.add_argument(
        "-R", "--remote", default="origin", help="Remote name"
    )
    parser_submit.add_argument(
        "-d",
        "--draft",
        action="store_true",
        default=False,
        help="Submit PRs in draft mode",
    )
    parser_submit.add_argument(
        "-q",
        "--quiet",
        action="store_false",
        default=True,
        help="Supress shell commands output",
    )

    parser_land = subparsers.add_parser("land", help="Land the current stack")
    parser_land.add_argument(
        "--main-branch", default="main", help="Target branch"
    )
    parser_land.add_argument(
        "-R", "--remote", default="origin", help="Remote name"
    )
    parser_land.add_argument(
        "-q",
        "--quiet",
        action="store_false",
        default=True,
        help="Supress shell commands output",
    )

    parser_abandon = subparsers.add_parser("abandon", help="b help")
    parser_abandon.add_argument(
        "--main-branch", default="main", help="Target branch"
    )
    parser_abandon.add_argument(
        "-R", "--remote", default="origin", help="Remote name"
    )
    parser_abandon.add_argument(
        "--head-branch-name", default="stack-head", help="Result branch name"
    )
    parser_abandon.add_argument(
        "-q",
        "--quiet",
        action="store_false",
        default=True,
        help="Supress shell commands output",
    )

    args, unknown = parser.parse_known_args()
    if args.quiet:
        QUIET_MODE = True

    if args.command == "submit":
        command_submit(args)
    elif args.command == "land":
        command_land(args)
    elif args.command == "abandon":
        command_abandon(args)


if __name__ == "__main__":
    main()
