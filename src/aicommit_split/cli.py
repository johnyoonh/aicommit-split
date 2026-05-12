#!/usr/bin/env python3

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? \+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?: (?P<header>.*))?$"
)
DAILY_NOTE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?: \d+)?$")
IGNORED_GLOBS = {
    "*.log",
    "*.pyc",
    ".DS_Store",
    "__pycache__/",
}


@dataclass
class Hunk:
    header_line: str
    lines: List[str]
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    context: str


@dataclass
class FilePatch:
    path: str
    old_path: str
    new_path: str
    header_lines: List[str] = field(default_factory=list)
    hunks: List[Hunk] = field(default_factory=list)
    is_new_file: bool = False
    is_deleted_file: bool = False


@dataclass
class Group:
    key: str
    paths: List[str] = field(default_factory=list)
    patches: List[FilePatch] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def run(cmd, cwd=None, capture=True, check=True):
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        capture_output=capture,
        check=check,
    )


def git(*args, cwd=None, capture=True, check=True):
    return run(["git", *args], cwd=cwd, capture=capture, check=check)


def repo_root():
    return git("rev-parse", "--show-toplevel").stdout.strip()


def has_staged_changes(root):
    return git("diff", "--cached", "--quiet", cwd=root, check=False).returncode != 0


def is_ignored_by_git(root, path):
    return git("check-ignore", "-q", "--", path, cwd=root, capture=True, check=False).returncode == 0


def aicommit_bin():
    candidate = os.environ.get("AICOMMIT_BIN")
    if candidate:
        return candidate
    for probe in (
        ["zsh", "-lc", "command -v aicommit"],
        ["bash", "-lc", "command -v aicommit"],
    ):
        found = subprocess.run(
            probe,
            text=True,
            capture_output=True,
            check=False,
        ).stdout.strip()
        if found:
            return found
    return ""


def bucket_for(path):
    p = Path(path)
    parts = p.parts
    if not parts:
        return "misc"
    first = parts[0]
    if first in {
        ".config",
        ".gitconfig",
        ".p10k.zsh",
        ".zshrc",
        ".zprofile",
        ".zshenv",
        ".vimrc",
        ".bashrc",
    }:
        return "shell-config"
    if first in {"keybinding", "iterm2"}:
        return "terminal-ui"
    if first in {"scripts"}:
        return "scripts"
    if first in {"gemini-tools"}:
        return "gemini-tools"
    if first in {"hammerspoon", "hammerspoon_init.lua"}:
        return "hammerspoon"
    if first in {"Library", "LaunchAgents"}:
        return "system-config"
    return first


def should_skip_path(root, path):
    normalized = path.replace("\\", "/")
    if is_ignored_by_git(root, path):
        return True
    for pattern in IGNORED_GLOBS:
        if fnmatch.fnmatch(normalized, pattern) or f"/{pattern}" in normalized:
            return True
    return False


def semantic_label(path, context):
    ctx = (context or "").strip()
    if not ctx:
        return "file"
    ctx = re.sub(r"\s+", " ", ctx)
    ctx = re.sub(r"[^A-Za-z0-9_.:/ -]+", "", ctx).strip()
    if not ctx:
        return "file"
    return ctx[:80]


def should_split_by_hunk(path: str) -> bool:
    p = Path(path)
    name = p.name
    suffix = p.suffix.lower()

    if name.startswith(".") and suffix in {"", ".zsh", ".md", ".toml", ".yaml", ".yml", ".json", ".plist"}:
        return False
    if name in {".gitconfig", ".p10k.zsh", ".zshrc", ".zprofile", ".zshenv", "GEMINI.md", "README.md"}:
        return False
    if suffix in {".md", ".txt", ".plist", ".json", ".yaml", ".yml", ".toml"}:
        return False
    return suffix in {".py", ".lua", ".js", ".ts", ".tsx", ".jsx", ".sh", ".zsh", ".rb", ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".hpp"}


def changed_hunk_lines(hunk: Hunk) -> List[str]:
    return [
        line[1:].strip()
        for line in hunk.lines
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]


def added_hunk_lines(hunk: Hunk) -> List[str]:
    return [
        line[1:].rstrip("\n")
        for line in hunk.lines
        if line.startswith("+") and not line.startswith("+++")
    ]


def is_python_import_line(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and (
        re.match(r"^import\s+[A-Za-z_][A-Za-z0-9_.]*(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?(?:\s*,\s*[A-Za-z_][A-Za-z0-9_.]*(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?)*$", stripped)
        or re.match(r"^from\s+[A-Za-z_][A-Za-z0-9_.]*\s+import\s+.+$", stripped)
    )


def is_python_import_hunk(path: str, hunk: Hunk) -> bool:
    if Path(path).suffix.lower() != ".py":
        return False
    changed = changed_hunk_lines(hunk)
    return bool(changed) and all(is_python_import_line(line) for line in changed)


def imported_names_from_line(line: str) -> List[str]:
    stripped = line.strip()
    names: List[str] = []
    if stripped.startswith("import "):
        for item in stripped[len("import ") :].split(","):
            item = item.strip()
            if not item:
                continue
            if " as " in item:
                names.append(item.rsplit(" as ", 1)[1].strip())
                continue
            names.append(item.split(".", 1)[0])
            names.append(item)
    elif stripped.startswith("from ") and " import " in stripped:
        imported = stripped.split(" import ", 1)[1]
        if imported.startswith("(") and imported.endswith(")"):
            imported = imported[1:-1]
        for item in imported.split(","):
            item = item.strip()
            if not item or item == "*":
                continue
            if " as " in item:
                names.append(item.rsplit(" as ", 1)[1].strip())
            else:
                names.append(item)
    return [name for name in OrderedDict.fromkeys(names) if name]


def added_import_names(hunks: List[Hunk]) -> List[str]:
    names: List[str] = []
    for hunk in hunks:
        for line in added_hunk_lines(hunk):
            if is_python_import_line(line):
                names.extend(imported_names_from_line(line))
    return list(OrderedDict.fromkeys(names))


def is_addition_only_hunk(hunk: Hunk) -> bool:
    changed = [
        line
        for line in hunk.lines
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    return bool(changed) and all(line.startswith("+") for line in changed)


def hunk_header(old_start: int, old_count: int, new_start: int, new_count: int, context: str) -> str:
    old_range = str(old_start) if old_count == 1 else f"{old_start},{old_count}"
    new_range = str(new_start) if new_count == 1 else f"{new_start},{new_count}"
    suffix = f" {context}" if context else ""
    return f"@@ -{old_range} +{new_range} @@{suffix}\n"


def atomic_import_patches(group: Group) -> List[FilePatch]:
    patches: List[FilePatch] = []
    for patch in group.patches:
        for hunk in patch.hunks:
            if is_addition_only_hunk(hunk):
                offset = 0
                for line in hunk.lines:
                    if not line.startswith("+") or line.startswith("+++"):
                        continue
                    atomic_hunk = Hunk(
                        header_line=hunk_header(hunk.old_start, 0, hunk.new_start + offset, 1, hunk.context),
                        lines=[line],
                        old_start=hunk.old_start,
                        old_count=0,
                        new_start=hunk.new_start + offset,
                        new_count=1,
                        context=hunk.context,
                    )
                    patches.append(
                        FilePatch(
                            path=patch.path,
                            old_path=patch.old_path,
                            new_path=patch.new_path,
                            header_lines=list(patch.header_lines),
                            hunks=[atomic_hunk],
                            is_new_file=False,
                            is_deleted_file=False,
                        )
                    )
                    offset += 1
            else:
                patches.append(patch)
    return patches


def added_code_text(group: Group) -> str:
    lines: List[str] = []
    for patch in group.patches:
        for hunk in patch.hunks:
            for line in added_hunk_lines(hunk):
                if not is_python_import_line(line):
                    lines.append(line)
    return "\n".join(lines)


def references_import(code_text: str, import_name: str) -> bool:
    if not code_text or not import_name:
        return False
    if "." in import_name:
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(import_name)}(?![A-Za-z0-9_])", code_text) is not None
    return re.search(rf"\b{re.escape(import_name)}\b", code_text) is not None


def group_paths(group: Group) -> set[str]:
    return {patch.path for patch in group.patches} or set(group.paths)


def is_import_only_group(group: Group) -> bool:
    if not group.patches:
        return False
    seen_hunks = False
    for patch in group.patches:
        if Path(patch.path).suffix.lower() != ".py":
            return False
        for hunk in patch.hunks:
            seen_hunks = True
            if not is_python_import_hunk(patch.path, hunk):
                return False
    return seen_hunks


def merge_patch_into_group(target: Group, support_patch: FilePatch) -> None:
    for patch in target.patches:
        if patch.path == support_patch.path and patch.old_path == support_patch.old_path and patch.new_path == support_patch.new_path:
            patch.hunks = sorted([*patch.hunks, *support_patch.hunks], key=lambda hunk: hunk.new_start)
            return
    target.patches.append(support_patch)


def coalesce_support_hunks(groups: OrderedDict[str, Group]) -> OrderedDict[str, Group]:
    result: OrderedDict[str, Group] = OrderedDict()
    import_groups: List[Group] = []

    for key, group in groups.items():
        if is_import_only_group(group):
            import_groups.append(group)
        else:
            result[key] = group

    for import_group in import_groups:
        paths = group_paths(import_group)
        if len(paths) != 1:
            result[import_group.key] = import_group
            continue
        path = next(iter(paths))
        candidates = [group for group in result.values() if path in group_paths(group)]
        if not candidates:
            result[import_group.key] = import_group
            continue

        unmatched: List[FilePatch] = []
        for import_patch in atomic_import_patches(import_group):
            names = added_import_names(import_patch.hunks)
            matching = [
                group
                for group in candidates
                if any(references_import(added_code_text(group), name) for name in names)
            ]
            if len(matching) == 1:
                target = matching[0]
            elif not matching and len(candidates) == 1:
                target = candidates[0]
            else:
                unmatched.append(import_patch)
                continue

            merge_patch_into_group(target, import_patch)
            label = ", ".join(names) if names else "imports"
            target.notes.append(f"import support: {label}")

        if unmatched:
            import_group.patches = unmatched
            result[import_group.key] = import_group

    return result


def parse_patch(text: str) -> List[FilePatch]:
    patches: List[FilePatch] = []
    current: Optional[FilePatch] = None
    current_hunk: Optional[Hunk] = None

    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git "):
            if current_hunk and current:
                current.hunks.append(current_hunk)
                current_hunk = None
            if current:
                patches.append(current)

            parts = line.rstrip("\n").split(" ")
            old_path = parts[2][2:]
            new_path = parts[3][2:]
            current = FilePatch(path=new_path, old_path=old_path, new_path=new_path, header_lines=[line])
            continue

        if current is None:
            continue

        if line.startswith("@@ "):
            if current_hunk:
                current.hunks.append(current_hunk)
            m = HUNK_RE.match(line.rstrip("\n"))
            if not m:
                raise ValueError(f"failed to parse hunk header: {line!r}")
            current_hunk = Hunk(
                header_line=line,
                lines=[],
                old_start=int(m.group("old_start")),
                old_count=int(m.group("old_count") or "1"),
                new_start=int(m.group("new_start")),
                new_count=int(m.group("new_count") or "1"),
                context=m.group("header") or "",
            )
            continue

        if current_hunk is not None:
            current_hunk.lines.append(line)
            continue

        current.header_lines.append(line)
        if line.startswith("new file mode "):
            current.is_new_file = True
        if line.startswith("deleted file mode "):
            current.is_deleted_file = True
        if line.startswith("+++ /dev/null"):
            current.is_deleted_file = True
        if line.startswith("--- /dev/null"):
            current.is_new_file = True

    if current_hunk and current:
        current.hunks.append(current_hunk)
    if current:
        patches.append(current)
    return patches


def get_changed_paths(root):
    out = git("status", "--porcelain=v1", cwd=root).stdout.splitlines()
    results = []
    for line in out:
        if not line:
            continue
        status = line[:2]
        pathspec = line[3:]
        if " -> " in pathspec:
            pathspec = pathspec.split(" -> ", 1)[1]
        if should_skip_path(root, pathspec):
            continue
        results.append((status, pathspec))
    return results


def summarize_paths(paths):
    names = sorted({Path(path).stem for path in paths})
    summary = ", ".join(names[:3])
    if len(names) > 3:
        summary += "..."
    return summary or "update"


def deterministic_commit_message(group: Group):
    paths = sorted(group.paths)
    if not paths:
        return None

    if len(paths) == 1:
        path = paths[0]
        p = Path(path)
        if p.name == ".gitignore":
            return "chore: update gitignore"
        if p.name == "README.md":
            return f"docs({p.parent.name or 'root'}): update README"
        if p.name == "pyproject.toml":
            return f"build({p.parent.name or 'root'}): update pyproject"
        if p.suffix == ".md" and DAILY_NOTE_RE.match(p.stem):
            return f"content({p.parent.as_posix()}): {p.stem}"
        if p.name == "data.json" and "plugins" in p.parts:
            try:
                plugin_index = p.parts.index("plugins")
                plugin_name = p.parts[plugin_index + 1]
                return f"build(workspace): {plugin_name}"
            except (ValueError, IndexError):
                pass

    top_levels = {Path(path).parts[0] for path in paths if Path(path).parts}
    if len(top_levels) == 1:
        top = next(iter(top_levels))
        if top == ".obsidian":
            return "build(workspace): workspace settings"
        if top in {"99_meta", "outputs"}:
            return f"chore({top}): {summarize_paths(paths)}"

    return None


def build_groups(root) -> OrderedDict[str, Group]:
    groups: OrderedDict[str, Group] = OrderedDict()
    changed = get_changed_paths(root)

    tracked_paths = [path for status, path in changed if not status.startswith("??")]
    if tracked_paths:
        diff = git("diff", "--no-color", "--unified=0", "--", *tracked_paths, cwd=root).stdout
        for patch in parse_patch(diff):
            bucket = bucket_for(patch.path)
            if patch.is_new_file or patch.is_deleted_file or len(patch.hunks) <= 1 or not should_split_by_hunk(patch.path):
                label = semantic_label(patch.path, patch.hunks[0].context if patch.hunks else "file")
                key = f"{bucket}::{patch.path}::{label}"
                group = groups.setdefault(key, Group(key=key))
                group.paths.append(patch.path)
                group.patches.append(patch)
                continue

            hunks_by_label: OrderedDict[str, List[Hunk]] = OrderedDict()
            for hunk in patch.hunks:
                label = semantic_label(patch.path, hunk.context)
                hunks_by_label.setdefault(label, [])
                hunks_by_label[label].append(hunk)

            for label, hunks in hunks_by_label.items():
                partial = FilePatch(
                    path=patch.path,
                    old_path=patch.old_path,
                    new_path=patch.new_path,
                    header_lines=list(patch.header_lines),
                    hunks=hunks,
                    is_new_file=False,
                    is_deleted_file=False,
                )
                key = f"{bucket}::{patch.path}::{label}"
                group = groups.setdefault(key, Group(key=key))
                group.paths.append(patch.path)
                group.patches.append(partial)

    for status, path in changed:
        if not status.startswith("??"):
            continue
        bucket = bucket_for(path)
        key = f"{bucket}::{path}::new-file"
        group = groups.setdefault(key, Group(key=key))
        group.paths.append(path)

    groups = coalesce_support_hunks(groups)

    for group in groups.values():
        group.paths = list(OrderedDict.fromkeys(group.paths))
    return groups


def render_patch(patch: FilePatch) -> str:
    pieces = []
    pieces.extend(patch.header_lines)
    for hunk in patch.hunks:
        pieces.append(hunk.header_line)
        pieces.extend(hunk.lines)
    return "".join(pieces)


def stage_group(root, group: Group):
    patch_texts = [render_patch(p) for p in group.patches]
    if patch_texts:
        proc = subprocess.run(
            ["git", "apply", "--cached", "--unidiff-zero", "-"],
            cwd=root,
            input="".join(patch_texts),
            text=True,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or "git apply failed")

    untracked_only = [p for p in group.paths if not any(fp.path == p for fp in group.patches)]
    if untracked_only:
        git("add", "--", *untracked_only, cwd=root)


def commit_group(root, aic_bin, group: Group, extra_args):
    label = group.key.split("::", 2)[-1]
    deterministic_message = deterministic_commit_message(group)
    if deterministic_message:
        print(f"[aic-split] local commit: {deterministic_message}", file=sys.stderr)
        proc = subprocess.run(["git", "commit", "-m", deterministic_message], cwd=root)
        return proc.returncode
    print(f"[aic-split] committing group: {label}", file=sys.stderr)
    proc = subprocess.run([aic_bin, *extra_args], cwd=root)
    return proc.returncode


def reset_index(root):
    git("reset", cwd=root)


def print_group(group: Group):
    label = group.key.split("::", 2)[-1]
    suffix = f" (+ {'; '.join(group.notes)})" if group.notes else ""
    print(f"[aic-split] group {label}{suffix}:", file=sys.stderr)
    for path in group.paths:
        print(f"  - {path}", file=sys.stderr)


def parse_args():
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("-p", "--preview", action="store_true")
    parser.add_argument("aicommit_args", nargs=argparse.REMAINDER)
    ns = parser.parse_args()
    extra = ns.aicommit_args
    if extra and extra[0] == "--":
        extra = extra[1:]
    ns.aicommit_args = extra
    return ns


def main():
    args = parse_args()
    root = repo_root()

    if has_staged_changes(root):
        print("aic-split: staged changes already present; unstage or commit them first", file=sys.stderr)
        return 1

    groups = build_groups(root)
    if not groups:
        print("aic-split: no changes to commit", file=sys.stderr)
        return 0

    for group in groups.values():
        print_group(group)
    if args.preview:
        return 0

    aic_bin = aicommit_bin()
    if not aic_bin:
        print("aic-split: aicommit not found on PATH", file=sys.stderr)
        return 1

    for group in groups.values():
        try:
            stage_group(root, group)
        except Exception as exc:
            print(f"aic-split: failed to stage group {group.key}: {exc}", file=sys.stderr)
            reset_index(root)
            return 1

        rc = commit_group(root, aic_bin, group, args.aicommit_args)
        if rc != 0:
            print(f"aic-split: aicommit failed for group {group.key}", file=sys.stderr)
            return rc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
