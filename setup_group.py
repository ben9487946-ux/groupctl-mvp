"""Create the initial protocol 0.1 files without committing them."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True

from group_state import StatusError, git


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", required=True)
    parser.add_argument("--branch", default="main")
    parser.add_argument("--assignment", required=True, type=Path)
    parser.add_argument("--member", nargs=2, action="append", required=True,
                        metavar=("ID", "NAME"))
    args = parser.parse_args()
    root = Path(git("rev-parse", "--show-toplevel").stdout.strip())
    git("check-ref-format", f"refs/heads/{args.branch}")
    assignment = ".group/assignment/assignment.md"
    files = {
        ".group/group.json": {
            "protocol_version": "0.1", "group_id": args.group,
            "canonical_branch": args.branch, "assignment_file": assignment,
        },
        assignment: args.assignment.read_text(encoding="utf-8-sig"),
        ".group/local/.gitkeep": "",
    }
    joined = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for person, name in args.member:
        path = f".group/members/{person}.json"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", person) or path in files:
            parser.error(f"Invalid or duplicate member ID: {person}")
        files[path] = {"protocol_version": "0.1", "person_id": person,
                       "display_name": name, "joined_at": joined}

    ignore = root / ".gitignore"
    if (root / ".group").exists() or (root / ".group").is_symlink():
        raise StatusError(".group 已存在。請保留原設定並依老師規格檢查。")
    if ignore.is_symlink():
        raise StatusError(".gitignore 是連結，請自行新增忽略規則。")
    for relative, value in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n"
                         if isinstance(value, dict) else value)
    with ignore.open("a", encoding="utf-8") as stream:
        stream.write("\n/.group/local/*\n!/.group/local/.gitkeep\n")
    print("已建立小組設定。請先檢查 git status，再提交到小組標準分支。")


if __name__ == "__main__":
    try:
        main()
    except (OSError, StatusError) as error:
        print(error, file=sys.stderr)
        sys.exit(1)
