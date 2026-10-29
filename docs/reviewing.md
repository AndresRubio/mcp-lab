# Reviewing a branch

An independent review runs in a checkout it must not change: the authoring session may be editing
there. Read and run in place; write only in a scratch directory. To change anything, add a worktree
on a branch of your own (`git worktree add`); the shared checkout keeps its branch. A review commits,
pushes and posts nothing.

## Steps

1. Read the PR body and the CI results: `gh pr view N`, then `gh pr checks N`. CI runs on Ubuntu,
   so a job log (`gh run view RUN --job JOB --log`) is the evidence for anything the review machine
   cannot run. Done when every claim in the PR body is matched to a job or to a command you will run.
2. Build the diff: `scripts/review-package.sh BASE HEAD OUTFILE`. Done when the commit list and the
   stat describe the same work (the package warns when they cannot).
3. Run the check once: `scripts/check.sh`. Done when it is green, so every later failure is yours.
4. Run the new tests against the base: `scripts/tests-against-base.sh BASE HEAD -k "<names>"`.
   A test that proves a fix fails there. One that passes is a drift guard or coverage, and the
   report says which. A collection error names an import the base lacks, which counts as a fail.
   The interpreter and the dependencies are the checkout's, so run it from a checkout at HEAD
   (the script warns otherwise); a test that starts `uv run` gets the same venv. A test path is
   relative to the repository root. Tests of a package are left out with a warning when the base
   lacks its `src/` or a top-level module in it: they would run the checkout's code and pass for
   the wrong reason. Done when every new test is classified.
5. Verify each candidate finding with a command, a scratch test or a quoted line, and drop what
   cannot be shown. A platform claim names the platform it ran on, or carries the word
   "unverified". Done when every finding that stays has its evidence written next to it.
6. Report: the findings ranked by severity, each with file and line, what breaks and how it was
   shown; then what was checked and found sound. Say plainly when nothing was found.
