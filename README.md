
# Stacked PRs Workflow

## What exactly are stacked PRs, and why are they useful?

Imagine that we have a change `A` and a change `B` depending on `A`, and we
would like to get them both reviewed. Without stacked PRs one would have to
create two PRs: `A`, `A+B`. The second PR would be difficult to review as it
includes all the changes simultaneously. With stacked PRs the first PR will
have only the change `A`, and the second PR will only have the change `B`. With
stacked PRs one can group related changes together making them easier to
review.

Example:
![StackedPRExample1](img/StackedPRExample1.png)

## `stack-pr.py` User Guide

### Workflow

`utils/stack-pr.py` is a script allowing you to work with stacked PRs: submit,
view, and land them.

`stack-pr.py` tool has four commands:

- `submit` (or `export`) - create a new stack of PRs from the given set of
  commits. One can think of this as “push my local changes to the corresponding
  remote branches and update the corresponding PRs (or create new PRs if they
  don’t exist yet)”.
- `view` - inspect the given set of commits and find the linked PRs. This
  command does not push any changes anywhere and does not change any commits.
  It can be used to examine what other commands did or will do.
- `abandon` - remove all stack metadata from the given set of commits. Apart
  from removing the metadata from the affected commits, this command deletes
  the corresponding local and remote branches and closes the PRs.
- `land` - merge PRs from the stack corresponding to the given set of commits.
  This command attempts to merge PRs from the stack one by one, and if
  succeeded deletes the corresponding branches from local and remote repos.

A usual workflow is the following:

```bash
while not ready to merge:
    make local changes
    commit to local git repo or amend existing commits
    create or update the stack with `stack-pr.py submit`
merge changes with `stack-pr.py land`
```

You can also use `view` at any point to examine the current state, and
`abandon` to drop the stack.

Under the hood the tool creates and maintains branches named
`$USERNAME/stack/$BRANCH_NUM` and embeds stack metadata into commit messages,
but you are not supposed to work with those branches or edit that metadata
manually. I.e. instead of pushing to these branches you should use `submit`,
instead of deleting them you should use `abandon` and instead of merging them
you should use `land`.

The tool looks at commits in the range `BASE..HEAD` and creates a stack of PRs
to apply these commits to `TARGET`. By default, `BASE` is `main` (local
branch), `HEAD` is the git revision `HEAD`, and `TARGET` is `main` on remote
(i.e. `origin/main`). These parameters can be changed with options `-B`, `-H`,
and `-T` respectively and accept the standard git notation: e.g. one can use
`-B HEAD~2`, to create a stack from the last two commits.

#### Example

The first step before creating a stack of PRs is to double-check the changes
we’re going to post. 

By default the tool will look at commits in `main..HEAD` range and will create
a PR for every commit in that range.

For instance, if we have

```bash
# git checkout my-feature
# git log -n 3  --format=oneline
**fcc6727ce** (**my-feature**)        [KGEN] POP: Switch pop.memset from scalar<ui8> to simd<1,ui8>.
**e43233f6f**                     [KGEN] Switch ZAP_DebugAssert from scalar<t> to simd<1,t>.
**8ea45c371** (**origin/main, main**) [Lit] Split core expr data structures out to LitExprs.h, NFC. (#4083)
```

Then the tool will consider the top two commits as changes, for which we’re
trying to create a stack.

> **Pro-tip**: a convenient way to see what commits will be considered by
> default is the following command:
> 

```bash
alias githist='git log --abbrev-commit --oneline $(git merge-base origin/main HEAD)^..HEAD'
```

We can double-check that by running the script with `view` command - it is
always a safe command to run:

```bash
# utils/stack-pr.py view
...
**Stack:**
   * **fcc6727c** (No PR): [KGEN] POP: Switch pop.memset from scalar<ui8> to simd<1,ui8>.
   * **e43233f6** (No PR): [KGEN] Switch ZAP_DebugAssert from scalar<t> to simd<1,t>.
SUCCESS!
```

If everything looks correct, we can now submit the stack, i.e. create all the
corresponding PRs and cross-link them. To do that, we run the tool with
`submit` command:

```bash
# utils/stack-pr.py submit
...
SUCCESS!
```

The command accepts a couple of options that might be useful, namely:

- `--draft` - mark all created PRs as draft. This helps to avoid over-burdening
  CI.
- `--reviewer="handle1,handle2"` - assign specified reviewers.

If the command succeeded, we should see “SUCCESS!” in the end, and we can now
run `view` again to look at the new stack:

```
# utils/stack-pr.py view
...
**Stack**:
   * **00093421** (#4085, 'ZolotukhinM/stack/2' -> 'ZolotukhinM/stack/1'): [KGEN] POP: Switch pop.memset from scalar<ui8> to simd<1,ui8>.
   * **50bdb483** (#4084, 'ZolotukhinM/stack/1' -> 'main'): [KGEN] Switch ZAP_DebugAssert from scalar<t> to simd<1,t>.
SUCCESS!
```

We can also go to github and check our PRs there:

![StackedPRExample2](img/StackedPRExample2.png)

If we need to make changes to any of the PRs (e.g. to address the review
feedback), we simply amend the desired changes to the appropriate git commits
and run `submit` again. If needed, we can rearrange commits or add new ones.

When we are ready to merge our changes, we use `land` command.

```
# utils/stack-pr.py land
...
**Stack**:
   * **00093421** (#4085, 'ZolotukhinM/stack/2' -> 'ZolotukhinM/stack/1'): [KGEN] POP: Switch pop.memset from scalar<ui8> to simd<1,ui8>.
   * **50bdb483** (#4084, 'ZolotukhinM/stack/1' -> 'main'): [KGEN] Switch ZAP_DebugAssert from scalar<t> to simd<1,t>.
...
SUCCESS!
```

That’s it!

If we inspect `origin/main` now we will see our changes on top:

```bash
# git log origin/main -n 3 --format=oneline

**46e840e98**   [KGEN] POP: Switch pop.memset from scalar<ui8> to simd<1,ui8>. (#4085)
**f1b82f6a4**   [KGEN] Switch ZAP_DebugAssert from scalar<t> to simd<1,t>. (#4084)
**9ae059a93**   [MOP] Placeholder for MOPPrimitives. (#3984)
```

### Specifying Custom Commit Ranges

The example above used the default commit range - `main..HEAD`, but you can
specify a custom range too. Below are several commonly useful invocations of
the script:

```bash
# Submit a stack of last 5 commits
utils/stack-pr.py -B HEAD~5 submit

# Use 'origin/main' instead of 'main' as the base for the stack
utils/stack-pr.py -B origin/main submit

# Do not include last two commits to the stack
utils/stack-pr.py -H HEAD~2 submit
```

These options work for all script commands (and it’s recommended to first use
them with `view` to double check the result). It is possible to mix and match
them too - e.g. one can first submit the stack for the last 5 commits and then
land first three of them:

```bash
# Inspect what commits will be included HEAD~5..HEAD
utils/stack-pr.py -B HEAD~5 view
# Create a stack from last five commits
utils/stack-pr.py -B HEAD~5 submit

# Inspect what commits will be included into the range HEAD~5..HEAD~2
utils/stack-pr.py -B HEAD~5 -H HEAD~2 view
# Land first three PRs from the stack
utils/stack-pr.py -B HEAD~5 -H HEAD~2 land
```
