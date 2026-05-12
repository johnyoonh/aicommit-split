# aicommit-split

`aicommit-split` splits your current Git working-tree changes into small,
deterministic groups and commits each group with `aicommit`.

It is intended for the common workflow where you have a good mixed worktree but
want cleaner commit boundaries than one large AI-generated commit.

## Install

From a checkout:

```sh
python -m pip install -e .
```

Then add a Git alias:

```sh
git config --global alias.aics '!aicommit-split'
```

If `aicommit` is not on your normal shell `PATH`, point to it explicitly:

```sh
git config --global alias.aics '!f() { AICOMMIT_BIN="/path/to/aicommit" aicommit-split "$@"; }; f'
```

## Usage

Preview the groups without staging or committing:

```sh
git aics --preview
```

Commit each group:

```sh
git aics
```

Arguments after `--` are passed to `aicommit`:

```sh
git aics -- --type conventional
```

## How grouping works

- Ignored files, logs, bytecode, `.DS_Store`, and `__pycache__` are skipped.
- New and deleted files are kept as whole-file groups.
- Docs and config files are kept as whole-file groups.
- Code files are split by diff hunk context.
- Python import-only hunks are treated as support hunks:
  - If an added import is referenced by exactly one same-file substantive hunk,
    the import is merged into that group.
  - If there is exactly one same-file substantive group, unmatched imports are
    merged into that group.
  - If the target is ambiguous, the import remains separate.

The splitter refuses to run if changes are already staged. That keeps each run's
commit boundaries explicit and avoids mixing old staged state with new grouping.

## Safety

`--preview` is the best way to inspect the planned groups. During real runs,
`aicommit-split` stages and commits one group at a time. If staging a generated
patch fails, the index is reset before the command exits.
