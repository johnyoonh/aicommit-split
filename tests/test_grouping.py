from __future__ import annotations

import subprocess
from pathlib import Path

from aicommit_split.cli import build_groups, render_patch, stage_group


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=True)


def init_repo(path: Path) -> None:
    run(["git", "init", "-b", "main"], path)
    run(["git", "config", "user.name", "Test User"], path)
    run(["git", "config", "user.email", "test@example.com"], path)


def commit_file(repo: Path, relpath: str, content: str) -> None:
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    run(["git", "add", relpath], repo)
    run(["git", "commit", "-m", "baseline"], repo)


def groups_for(repo: Path):
    return list(build_groups(str(repo)).values())


def test_python_import_hunk_attaches_to_matching_function_group(tmp_path: Path) -> None:
    init_repo(tmp_path)
    commit_file(
        tmp_path,
        "tool.py",
        "import argparse\n\n\ndef format_name(name):\n    return name.title()\n",
    )
    (tmp_path / "tool.py").write_text(
        "import argparse\n"
        "import os\n"
        "\n\n"
        "def format_name(name):\n"
        "    return os.fspath(name).title()\n"
    )

    groups = groups_for(tmp_path)

    assert len(groups) == 1
    assert groups[0].notes == ["import support: os"]
    assert "import os" in "".join(render_patch(patch) for patch in groups[0].patches)


def test_multiple_imports_attach_to_their_matching_groups(tmp_path: Path) -> None:
    init_repo(tmp_path)
    commit_file(
        tmp_path,
        "tool.py",
        (
            "import argparse\n"
            "\n\n"
            "def normalize(path):\n"
            "    return str(path)\n"
            "\n\n"
            "def exit_code(value):\n"
            "    return int(value)\n"
        ),
    )
    (tmp_path / "tool.py").write_text(
        "import argparse\n"
        "import os\n"
        "import sys\n"
        "\n\n"
        "def normalize(path):\n"
        "    return os.fspath(path)\n"
        "\n\n"
        "def exit_code(value):\n"
        "    print(value, file=sys.stderr)\n"
        "    return int(value)\n"
    )

    groups = groups_for(tmp_path)
    notes = sorted(note for group in groups for note in group.notes)

    assert len(groups) == 2
    assert notes == ["import support: os", "import support: sys"]


def test_ambiguous_import_hunk_remains_separate(tmp_path: Path) -> None:
    init_repo(tmp_path)
    commit_file(
        tmp_path,
        "tool.py",
        (
            "import argparse\n"
            "\n\n"
            "def left(value):\n"
            "    return value\n"
            "\n\n"
            "def right(value):\n"
            "    return value\n"
        ),
    )
    (tmp_path / "tool.py").write_text(
        "import argparse\n"
        "import os\n"
        "\n\n"
        "def left(value):\n"
        "    return os.fspath(value)\n"
        "\n\n"
        "def right(value):\n"
        "    return os.fspath(value)\n"
    )

    groups = groups_for(tmp_path)

    assert len(groups) == 3
    assert any("import os" in "".join(render_patch(patch) for patch in group.patches) for group in groups)
    assert all(not group.notes for group in groups)


def test_import_only_change_stays_separate(tmp_path: Path) -> None:
    init_repo(tmp_path)
    commit_file(tmp_path, "tool.py", "import argparse\n")
    (tmp_path / "tool.py").write_text("import argparse\nimport os\n")

    groups = groups_for(tmp_path)

    assert len(groups) == 1
    assert groups[0].notes == []


def test_docs_and_config_do_not_split_by_hunk(tmp_path: Path) -> None:
    init_repo(tmp_path)
    commit_file(tmp_path, "README.md", "# Project\n\nOne\n\nTwo\n")
    (tmp_path / "README.md").write_text("# Project\n\nOne changed\n\nTwo changed\n")

    groups = groups_for(tmp_path)

    assert len(groups) == 1


def test_untracked_file_still_forms_new_file_group(tmp_path: Path) -> None:
    init_repo(tmp_path)
    (tmp_path / "new.py").write_text("print('new')\n")

    groups = groups_for(tmp_path)

    assert len(groups) == 1
    assert groups[0].key.endswith("::new-file")


def test_split_import_support_groups_stage_sequentially(tmp_path: Path) -> None:
    init_repo(tmp_path)
    commit_file(
        tmp_path,
        "tool.py",
        (
            "import argparse\n"
            "\n\n"
            "def normalize(path):\n"
            "    return str(path)\n"
            "\n\n"
            "def exit_code(value):\n"
            "    return int(value)\n"
        ),
    )
    (tmp_path / "tool.py").write_text(
        "import argparse\n"
        "import os\n"
        "import sys\n"
        "\n\n"
        "def normalize(path):\n"
        "    return os.fspath(path)\n"
        "\n\n"
        "def exit_code(value):\n"
        "    print(value, file=sys.stderr)\n"
        "    return int(value)\n"
    )

    for index, group in enumerate(groups_for(tmp_path), start=1):
        stage_group(str(tmp_path), group)
        run(["git", "commit", "-m", f"group {index}"], tmp_path)

    assert run(["git", "status", "--short"], tmp_path).stdout == ""
