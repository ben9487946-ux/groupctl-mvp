"""Group collaboration CLI, protocol 0.1."""

from datetime import datetime
import json
from pathlib import Path
import subprocess


EVENT_TYPES = {
    "change_published", "finding", "question", "answer", "review",
    "test_result", "integration_result", "blocker", "final_candidate", "final_result",
}


class StatusError(Exception):
    pass


def git(*args, check=True, cwd=None):
    result = subprocess.run(
        ["git", *args], stdin=subprocess.DEVNULL, capture_output=True,
        encoding="utf-8", errors="surrogateescape", cwd=cwd,
    )
    if check and result.returncode:
        raise StatusError(result.stderr.strip() or f"git {args[0]} 失敗")
    return result


def read_json(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("預期為 JSON 物件")
    return value


def published_events(refs, group_id):
    # Branches can share the same tip and immutable event blobs.
    events, blobs, invalid_ids, warnings = {}, {}, set(), []
    for tip in dict.fromkeys(refs.values()):
        tree = git("ls-tree", "-rz", tip, "--", ".group/events/").stdout
        for entry in filter(None, tree.split("\0")):
            metadata, path = entry.split("\t", 1)
            mode, kind, oid = metadata.split()
            parts = path.split("/")
            if len(parts) != 3 or not path.endswith(".json"):
                continue
            event_id = parts[-1][:-5]
            if kind != "blob" or mode not in {"100644", "100755"}:
                warnings.append(f"{path}（{tip[:12]}）：事件必須是一般檔案")
                invalid_ids.add(event_id)
                continue
            try:
                if oid not in blobs:
                    blobs[oid] = read_json(git("cat-file", "blob", oid).stdout)
                event = blobs[oid]
                if (
                    event.get("protocol_version") != "0.1"
                    or event.get("event_id") != event_id
                    or event.get("group_id") != group_id
                    or event.get("event_type") not in EVENT_TYPES
                    or not isinstance(event.get("person_id"), str)
                    or not event["person_id"]
                    or not isinstance(event.get("related_events"), list)
                    or not all(isinstance(item, str) for item in event["related_events"])
                ):
                    raise ValueError("事件共同欄位、群組或檔名不符")
                created = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
                if created.utcoffset() is None:
                    raise ValueError("created_at 需要時區")
                if event_id in events and events[event_id][0] != oid:
                    raise ValueError("同一事件 ID 在不同遠端分支的內容不一致")
                events[event_id] = (oid, created, event)
            except (AttributeError, KeyError, TypeError, ValueError) as error:
                invalid_ids.add(event_id)
                warnings.append(f"{path}（{tip[:12]}）：{error}")
    ordered = sorted(events.items(), key=lambda item: (item[1][1], item[0]), reverse=True)
    return [entry[2] for event_id, entry in ordered if event_id not in invalid_ids], warnings


def status(remote, explicit_person, tags=False):
    root = Path(git("rev-parse", "--show-toplevel").stdout.strip())
    group = read_json((root / ".group/group.json").read_text(encoding="utf-8"))
    required = ("group_id", "canonical_branch", "assignment_file")
    if group.get("protocol_version") != "0.1" or any(
        not isinstance(group.get(key), str) or not group[key] for key in required
    ):
        raise StatusError(".group/group.json 不符合 0.1 版必要欄位")
    canonical_branch = group["canonical_branch"]
    git("check-ref-format", f"refs/heads/{canonical_branch}")
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip() or None
    head = git("rev-parse", "--verify", "HEAD", check=False).stdout.strip() or None
    # Disable optional index refresh writes and preserve file names with NUL separators.
    dirty = git("--no-optional-locks", "status", "--porcelain=v1", "-z",
                "--untracked-files=all", "--ignore-submodules=none").stdout
    changes = []
    entries = iter(dirty.split("\0")[:-1])
    for entry in entries:
        change = {"status": entry[:2], "path": entry[3:]}
        if "R" in entry[:2] or "C" in entry[:2]:
            change["from"] = next(entries)
        changes.append(change)

    if remote not in git("remote").stdout.splitlines():
        raise StatusError(f"找不到遠端 {remote}，請設定 Git 遠端後再執行 STATUS")
    prefix = f"refs/remotes/{remote}/"
    git("check-ref-format", f"{prefix}metadata")
    fetch = git("fetch", "--atomic", "--prune", "--tags" if tags else "--no-tags", "--no-recurse-submodules",
                "--no-write-fetch-head", "--refmap=", "--",
                remote, f"+refs/heads/*:{prefix}*", check=False)
    refs = {}
    listing = git("for-each-ref", "--format=%(refname)%09%(objectname)%09%(symref)", prefix)
    for line in listing.stdout.splitlines():
        ref, oid, symref = line.split("\t")
        if not symref:
            refs[ref.removeprefix(prefix)] = oid
    canonical = refs.get(canonical_branch)
    warnings = []
    if fetch.returncode:
        warnings.append("無法更新遠端資訊，以下遠端資料來自本機快取。"
                        + fetch.stderr.strip())
    if not canonical:
        warnings.append(f"遠端資料找不到標準分支 {canonical_branch}")
    if git("rev-parse", "--is-shallow-repository").stdout.strip() == "true":
        warnings.append("淺層複製缺少部分歷史，提交差距暫不計算")
        counts = None
    else:
        counts = git("rev-list", "--left-right", "--count", f"{head}...{canonical}").stdout.split() \
            if head and canonical else None
    events, event_warnings = published_events(refs, group["group_id"])
    # A finalization proposed in a pull request becomes effective on canonical.
    canonical_events, _ = published_events({canonical_branch: canonical}, group["group_id"]) if canonical else ([], [])
    accepted_final_ids = {item["event_id"] for item in canonical_events}
    events = [item for item in events
              if item["event_type"] != "final_result" or item["event_id"] in accepted_final_ids]
    warnings.extend(event_warnings)
    branch_parts = (branch or "").split("/")
    inferred = branch_parts[1] if len(branch_parts) >= 3 and branch_parts[0] == "people" else None
    person = explicit_person or inferred
    member = None
    if person:
        if person in {".", ".."} or "/" in person or "\\" in person or ":" in person:
            raise StatusError("person_id 必須是單一檔名")
        if canonical:
            path = f".group/members/{person}.json"
            result = git("show", f"{canonical}:{path}", check=False)
            if not result.returncode:
                member = read_json(result.stdout)
                if member.get("person_id") != person or member.get("protocol_version") != "0.1":
                    raise StatusError(f"{path} 與成員規格不符")
        if member is None:
            warnings.append(f"標準分支未確認成員 {person} 的描述檔")
    else:
        warnings.append("無法從目前分支識別成員，可傳入 --person")

    tests = [event for event in events if event["event_type"] == "test_result"]
    report = {
        "protocol_version": "0.1",
        "group_id": group["group_id"],
        "assignment_file": group["assignment_file"],
        "person_id": person,
        "member": member,
        "branch": branch,
        "commit": head,
        "uncommitted_changes": changes,
        "remote": remote,
        "remote_fresh": fetch.returncode == 0,
        "canonical_branch": canonical_branch,
        "canonical_commit": canonical,
        "ahead": int(counts[0]) if counts else None,
        "behind": int(counts[1]) if counts else None,
        "event_processing_state": "unknown",
        "published_events_for_review": events,
        "published_local_changes": [
            event for event in events
            if person and event["person_id"] == person and event["event_type"] == "change_published"
        ],
        "last_known_test": next((event for event in tests if event.get("commit") == head), None) if head else None,
        "latest_group_test": next(iter(tests), None),
        "reported_blockers": [event for event in events if event["event_type"] == "blocker"],
        "warnings": warnings,
    }
    return report, refs
