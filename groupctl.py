"""Mechanical operations for the group collaboration protocol 0.1."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from uuid import uuid4

sys.dont_write_bytecode = True

from group_state import StatusError, git, read_json, status


def emit(value):
    print(json.dumps(value, ensure_ascii=True, indent=2))


def ancestor(old, new):
    result = git("merge-base", "--is-ancestor", old, new, check=False)
    if result.returncode not in (0, 1):
        raise StatusError(result.stderr.strip())
    return result.returncode == 0


def require_clean(report):
    if not report["branch"] or not report["commit"]:
        raise StatusError("請先切換到有提交的分支。")
    if report["uncommitted_changes"]:
        raise StatusError("有未提交工作。請自行檢查並提交，再重試。")
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge",
                   "rebase-apply", "sequencer", "BISECT_START"):
        path = git("rev-parse", "--git-path", marker).stdout.strip()
        if Path(path).exists():
            raise StatusError(f"請先完成進行中的 Git 操作（{marker}）。")


def require_personal(report):
    require_clean(report)
    branch = report["branch"]
    person = report["person_id"]
    if not branch.startswith(f"people/{person}/") or branch == f"people/{person}/events":
        raise StatusError("請切換到自己的工作分支 people/<person_id>/<topic>。")


def push(repo, remote, commit, branch):
    # Disable mirror/followTags behavior and use a single explicit refspec.
    git("-c", f"remote.{remote}.mirror=false", "-c", "push.followTags=false",
        "push", "--", remote, f"{commit}:refs/heads/{branch}", cwd=repo)


@contextmanager
def checkout(commit):
    source = git("rev-parse", "--show-toplevel").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="groupctl-") as folder:
        clone = Path(folder) / "repo"
        git("clone", "--no-local", "--no-checkout", "--", source, str(clone))
        if git("cat-file", "-e", f"{commit}^{{commit}}", cwd=clone, check=False).returncode:
            git("fetch", "--no-tags", "--", source, commit, cwd=clone)
        git("checkout", "--detach", commit, cwd=clone)
        git("remote", "remove", "origin", cwd=clone)
        yield clone


def write_file(repo, relative, text):
    path = repo
    parts = Path(relative).parts
    for part in parts[:-1]:
        path = path / part
        if path.is_symlink() or not path.resolve().is_relative_to(repo.resolve()):
            raise StatusError(f"拒絕寫入連結路徑 {path}。")
        path.mkdir(exist_ok=True)
    path = path / parts[-1]
    if path.is_symlink():
        raise StatusError(f"拒絕寫入連結 {path}。")
    path.write_text(text, encoding="utf-8")


def json_text(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def event(report, kind, related=(), **fields):
    now = datetime.now(timezone.utc)
    person = report["person_id"]
    return {
        "protocol_version": "0.1",
        "event_id": f"E-{now:%Y%m%dT%H%M%SZ}-{person}-{uuid4().hex[:12]}",
        "event_type": kind,
        "group_id": report["group_id"],
        "person_id": person,
        "created_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "related_events": list(related),
        **fields,
    }


def commit_and_push(report, refs, branch, base, record, files=None):
    remote = report["remote"]
    url = git("remote", "get-url", "--push", remote).stdout.strip()
    if "://" not in url and (":" not in url or Path(url).drive):
        root = Path(git("rev-parse", "--show-toplevel").stdout.strip())
        url = str((root / url).resolve())
    name = git("config", "--get", "user.name").stdout.strip()
    email = git("config", "--get", "user.email").stdout.strip()
    files = dict(files or {})
    event_path = f".group/events/{record['event_id']}.json"
    files[event_path] = json_text(record)
    expected = refs.get(branch, "")
    if expected and expected != base:
        raise StatusError("發布提交須延伸遠端目前版本。")
    with checkout(base) as repo:
        if (repo / event_path).exists():
            raise StatusError("事件 ID 已存在。")
        for path, content in files.items():
            write_file(repo, path, content)
        git("add", "--", *files, cwd=repo)
        git("-c", f"user.name={name}", "-c", f"user.email={email}",
            "commit", "-m", record["event_type"], cwd=repo)
        git("remote", "add", "publish", url, cwd=repo)
        # The expected ref must still match. The new commit always extends that tip.
        git("push", f"--force-with-lease=refs/heads/{branch}:{expected}", "--",
            "publish", f"HEAD:refs/heads/{branch}", cwd=repo)


def publish_event(report, refs, record, files=None):
    branch = f"people/{report['person_id']}/events"
    commit_and_push(report, refs, branch, refs.get(branch, report["canonical_commit"]),
                    record, files)


def change_by_id(report, refs, change_id):
    matches = [item for item in report["published_events_for_review"]
               if item["event_type"] == "change_published" and item.get("change_id") == change_id]
    if len(matches) != 1:
        raise StatusError(f"找不到唯一的已發布變更 {change_id}。")
    item = matches[0]
    branch, revision = item.get("branch"), item.get("commit")
    if not isinstance(branch, str) or branch not in refs:
        raise StatusError("事件指向的遠端分支不存在。")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-fA-F]{7,64}", revision):
        raise StatusError("事件中的提交 ID 無效。")
    commit = git("rev-parse", "--verify", f"{revision}^{{commit}}").stdout.strip()
    if not ancestor(commit, refs[branch]):
        raise StatusError("指定提交不在發布分支的歷史中。")
    return item, commit


def checked_diff(base, commit):
    return git("diff", "--no-ext-diff", "--check", base, commit, check=False)


def run_checks(commit, commands, timeout):
    argv_list = []
    for command in commands:
        argv = json.loads(command)
        if not isinstance(argv, list) or not argv or not all(
            isinstance(arg, str) and arg and "\0" not in arg for arg in argv
        ):
            raise StatusError("--check 必須是非空的 JSON 參數陣列。")
        argv_list.append(argv)
    results = []
    if not argv_list:
        return results
    with checkout(commit) as repo:
        for argv in argv_list:
            if argv[0] == "{python}":
                argv[0] = sys.executable
            executable = shutil.which(argv[0])
            if executable and Path(executable).suffix.lower() in {".bat", ".cmd"}:
                raise StatusError("請直接指定可執行檔或 Python 模組，以免批次檔經過命令殼層。")
            try:
                result = subprocess.run(
                    argv, cwd=repo, stdin=subprocess.DEVNULL,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=timeout,
                )
                results.append({
                    "command": argv, "outcome": "pass" if result.returncode == 0 else "fail",
                    "exit_code": result.returncode,
                })
                for stream in (result.stdout, result.stderr):
                    if stream:
                        print(stream.rstrip(), file=sys.stderr)
            except (OSError, subprocess.TimeoutExpired) as error:
                print(str(error), file=sys.stderr)
                results.append({"command": argv, "outcome": "unavailable", "exit_code": None})
    return results


def check_passed(results):
    return bool(results) and all(item["outcome"] == "pass" for item in results)


def sync(args, report, refs):
    canonical, head = report["canonical_commit"], report["commit"]
    root = Path(git("rev-parse", "--show-toplevel").stdout.strip())
    local = root / ".group/local"
    cursor = local / "sync.json"
    state_key = f"{report['group_id']}:{report['remote']}:{report['person_id']}"
    previous = {}
    if cursor.is_file():
        previous = read_json(cursor.read_text(encoding="utf-8"))
    events = report["published_events_for_review"]
    seen = set(previous.get(state_key, []))
    new = [item for item in events if item["event_id"] not in seen]
    action = "already_contains_canonical"
    checks = []
    if report["uncommitted_changes"] or not head or not report["branch"]:
        action = "metadata_only_local_work"
    elif not ancestor(canonical, head):
        require_clean(report)
        if not ancestor(head, canonical):
            action = "metadata_only_diverged"
        elif not args.check:
            action = "metadata_only_validation_required"
        else:
            checks = run_checks(canonical, args.check, args.timeout)
            action = "metadata_only_validation_failed"
            if check_passed(checks):
                git("merge", "--ff-only", "--no-overwrite-ignore", canonical)
                action = "fast_forwarded"
    emit({"before": head, "branch": report["branch"], "canonical_commit": canonical,
          "uncommitted_changes": report["uncommitted_changes"], "action": action,
          "new_events": new, "checks": checks})
    # The cursor means "seen by SYNC", and remains optional and local.
    path = ".group/local/sync.json"
    if git("check-ignore", "--quiet", "--", path, check=False).returncode == 0:
        previous[state_key] = sorted(seen | {item["event_id"] for item in events})
        write_file(root, path, json_text(previous))
    return int(action == "metadata_only_validation_failed")


def pub(args, report, refs):
    require_personal(report)
    head, branch = report["commit"], report["branch"]
    if args.inspect:
        base = git("merge-base", report["canonical_commit"], head).stdout.strip()
        emit({"commit": head, "base": base,
              "diff": git("diff", "--no-ext-diff", "--no-textconv", base, head).stdout})
        return 0
    for item in report["published_local_changes"]:
        if item.get("commit") == head and item.get("branch") == branch:
            emit(item)
            return 0
    if args.confirm_commit != head or not args.summary or not args.summary.strip():
        raise StatusError("先用 PUB --inspect 審閱差異，再傳入 --confirm-commit 與 --summary。")
    base = git("merge-base", report["canonical_commit"], head).stdout.strip()
    paths = list(filter(None, git("diff", "--name-only", "-z", base, head).stdout.split("\0")))
    if not paths or checked_diff(base, head).returncode:
        raise StatusError("找不到可發布的差異，或 diff --check 未通過。")
    checks = run_checks(head, args.check, args.timeout)
    if not check_passed(checks):
        raise StatusError("發布需要至少一項成功的驗證，且所有指定檢查都須通過。")
    record = event(
        report, "change_published", change_id=f"C-{uuid4().hex[:12]}",
        branch=branch, commit=head, summary=args.summary, files_changed=paths,
        interface_changes=args.interface, tests_run=[json.dumps(item["command"]) for item in checks],
        known_issues=args.issue, proposed_for_integration=not args.draft,
    )
    push(None, report["remote"], head, branch)
    try:
        publish_event(report, refs, record)
    except StatusError as error:
        raise StatusError(f"程式分支已推送，事件發布失敗。可對同一提交重試 PUB。{error}") from error
    emit(record)
    return 0


def inspect_change(item, commit, canonical):
    base = git("merge-base", canonical, commit).stdout.strip()
    emit({"event": item, "commit": commit, "base": base,
          "diff": git("diff", "--no-ext-diff", "--no-textconv", base, commit).stdout})


def rev(args, report, refs):
    item, commit = change_by_id(report, refs, args.change_id)
    if not args.verdict:
        inspect_change(item, commit, report["canonical_commit"])
        return 0
    if args.confirm_commit != commit or not args.summary:
        raise StatusError("請先審閱，再填入確切提交與審查摘要。")
    results = run_checks(commit, args.check, args.timeout)
    record = event(report, "review", [item["event_id"]], change_id=args.change_id,
                   verdict=args.verdict, summary=args.summary, issues=args.issue,
                   tests_run=results_as_strings(results))
    publish_event(report, refs, record)
    emit(record)
    return 0


def results_as_strings(results):
    return [f"{json.dumps(item['command'])} [{item['outcome']}]" for item in results]


def test(args, report, refs):
    related = []
    if args.scope == "current":
        commit = report["commit"]
        if report["uncommitted_changes"]:
            raise StatusError("current 測試以已提交版本為準。請先處理未提交的工作。")
    elif args.scope == "main":
        commit = report["canonical_commit"]
    else:
        item, commit = change_by_id(report, refs, args.scope)
        related.append(item["event_id"])
    if not commit:
        raise StatusError("沒有可測試的提交。")
    results = run_checks(commit, args.check, args.timeout)
    outcomes = [item["outcome"] for item in results]
    if "fail" in outcomes:
        outcome = "fail"
    elif check_passed(results):
        outcome = "pass"
    elif "pass" in outcomes:
        outcome = "partial"
    else:
        outcome = "not_available"
    scope = "current_branch" if args.scope == "current" else args.scope
    record = event(
        report, "test_result", related, scope=scope, commit=commit,
        commands=results_as_strings(results), automated_result=outcome,
        passed=outcomes.count("pass"), failed=outcomes.count("fail"),
        skipped=outcomes.count("unavailable"), count_unit="commands",
        human_checks_required=args.human_check, summary=args.summary or f"Automated checks: {outcome}.",
    )
    publish_event(report, refs, record)
    emit(record)
    return int(outcome != "pass")


def integrate(args, report, refs):
    item, source = change_by_id(report, refs, args.change_id)
    if not (args.apply or args.record):
        inspect_change(item, source, report["canonical_commit"])
        return 0
    if args.apply or args.record == "success":
        require_personal(report)
    elif not report["branch"] or not report["commit"]:
        raise StatusError("請在有提交的目標分支記錄整合結果。")
    head = report["commit"]
    if args.confirm_commit != source:
        raise StatusError("請先審查差異，再用 --confirm-commit 指定來源提交。")
    if args.apply:
        result = git("merge", "--no-ff", "--no-edit", "--no-overwrite-ignore", source, check=False)
        if result.returncode:
            conflicts = git("diff", "--name-only", "--diff-filter=U", "-z").stdout
            emit({"result": "conflict_or_error", "conflicts": list(filter(None, conflicts.split("\0"))),
                  "detail": result.stderr.strip() or result.stdout.strip()})
            return 1
        emit({"result": "merged_pending_verification",
              "commit": git("rev-parse", "HEAD").stdout.strip()})
        return 0
    incorporated = ancestor(source, head)
    if args.record == "success" and (not incorporated or not args.semantic_note):
        raise StatusError("成功紀錄需要已納入來源提交及 AI 提供語意審查摘要。")
    results = run_checks(head, args.check, args.timeout)
    markers = git("grep", "-n", "-E", "^(<<<<<<<( |$)|=======$|>>>>>>>( |$))", head, "--", ":/", check=False)
    if markers.returncode not in (0, 1):
        raise StatusError(markers.stderr.strip())
    if args.record == "success" and (markers.returncode == 0 or not check_passed(results)):
        raise StatusError("存在疑似衝突標記，或驗證未通過。請檢查後再記錄成功。")
    record = event(
        report, "integration_result", [item["event_id"]], change_id=args.change_id,
        target_branch=report["branch"], result=args.record, merge_commit=head if incorporated else None,
        conflicts=args.conflict, conflict_resolution_summary=args.resolution or "",
        verification={"build": "not_available", "tests": "pass" if check_passed(results) else
                      ("fail" if any(item["outcome"] == "fail" for item in results) else "not_available"),
                      "semantic_review": "pass" if args.record == "success" else "not_available"},
        summary=args.semantic_note or args.record, checks=results,
    )
    if incorporated and args.record == "success":
        push(None, report["remote"], head, report["branch"])
    publish_event(report, refs, record)
    emit(record)
    return int(args.record != "success")


def final(args, report, refs):
    canonical = report["canonical_commit"]
    canonical_branch = report["canonical_branch"]
    events = report["published_events_for_review"]
    if args.confirm_candidate:
        candidates = [item for item in events if item["event_id"] == args.confirm_candidate
                      and item["event_type"] == "final_candidate"]
        if len(candidates) != 1:
            raise StatusError("找不到指定的已發布候選紀錄。")
        candidate = candidates[0]
        personal_events = f"people/{report['person_id']}/events"
        source = refs.get(personal_events)
        if not source or candidate["person_id"] != report["person_id"] or candidate.get("canonical_commit") != canonical:
            raise StatusError("候選人或標準提交已變更，請重新產生候選報告。")
        content = git("show", f"{source}:.group/final/candidate.md").stdout
        if candidate.get("report_blob") != git("rev-parse", f"{source}:.group/final/candidate.md").stdout.strip():
            raise StatusError("候選報告已變更，請選擇新的候選紀錄。")
        if candidate.get("evidence_event_ids") != [item["event_id"] for item in events
                                                   if item["event_type"] not in {"final_candidate", "final_result"}]:
            raise StatusError("候選後有新的協作紀錄，請重新評估。")
        report_file = ".group/final/final_report.md"
        record = event(report, "final_result", [candidate["event_id"]], canonical_commit=canonical,
                       report_file=report_file, test_evidence=candidate["test_evidence"],
                       known_limitations=candidate["known_issues"], initiated_by=report["person_id"])
        envelope = {
            "protocol_version": "0.1", "group_id": report["group_id"],
            "canonical_branch": canonical_branch, "canonical_commit": canonical,
            "report_file": report_file, "initiated_by": report["person_id"],
            "created_at": record["created_at"],
            "status": "completed_with_known_limitations" if record["known_limitations"] else "completed",
            "known_limitations": record["known_limitations"],
        }
        destination = canonical_branch
        if args.via_pr:
            destination = f"people/{report['person_id']}/final-{uuid4().hex[:12]}"
        commit_and_push(report, refs, destination, canonical, record, {
            ".group/final/candidate.md": content, report_file: content,
            ".group/final/final.json": json_text(envelope),
        })
        if args.via_pr:
            emit({"pull_request_required": True, "branch": destination,
                  "base": canonical_branch, "proposed_final": envelope})
        else:
            emit(envelope)
        return 0

    pending = []
    for item in events:
        if item["event_type"] != "change_published":
            continue
        change_id = item.get("change_id")
        try:
            _, commit = change_by_id(report, refs, change_id)
            if not ancestor(commit, canonical):
                pending.append(change_id)
        except StatusError:
            pending.append(change_id)
    blockers = [item["event_id"] for item in report["reported_blockers"]]
    if not args.report:
        emit({"canonical_commit": canonical, "pending_changes": pending, "reported_blockers": blockers,
              "evidence": events})
        return 0
    if args.confirm_commit != canonical:
        raise StatusError("請先核對標準提交，再傳入 --confirm-commit。")
    if (pending or blockers) and not args.disposition:
        raise StatusError("有待處理變更或阻塞紀錄，請先評估並以 --disposition 填入處理結果。")
    if set(args.disposition_id) != set(pending + blockers):
        raise StatusError("請以 --disposition-id 逐一列出已評估的待處理變更與阻塞 ID。")
    content = args.report.read_text(encoding="utf-8").rstrip() + "\n"
    for heading in ("Assignment", "Result", "Canonical Commit", "Important Changes",
                    "Validation", "Known Limitations", "Contributors"):
        if f"## {heading}" not in content.splitlines():
            raise StatusError(f"報告缺少標題 ## {heading}。")
    if canonical not in content:
        raise StatusError("報告須包含完整標準提交 ID。")
    checks = run_checks(canonical, args.check, args.timeout)
    if not check_passed(checks):
        raise StatusError("最終自動驗證未通過或尚未執行。")
    content += "\n### Automated checks\n\n" + "\n".join(f"- {line}" for line in results_as_strings(checks)) + "\n"
    if args.disposition:
        content += f"\n### Pending changes and blockers\n\n{args.disposition}\n"
    if args.issue:
        content += "\n### Recorded limitations\n\n" + "\n".join(f"- {line}" for line in args.issue) + "\n"
    # Git normalizes text on checkout/add depending on local settings. Hash the
    # UTF-8/LF content used for the candidate through the same add filters.
    with checkout(refs.get(f"people/{report['person_id']}/events", canonical)) as repo:
        write_file(repo, ".group/final/candidate.md", content)
        blob = git("hash-object", "--path=.group/final/candidate.md",
                   ".group/final/candidate.md", cwd=repo).stdout.strip()
    record = event(report, "final_candidate", canonical_commit=canonical,
                   report_file=".group/final/candidate.md", report_blob=blob,
                   test_evidence=checks, known_issues=args.issue,
                   evidence_event_ids=[item["event_id"] for item in events
                                       if item["event_type"] not in {"final_candidate", "final_result"}])
    publish_event(report, refs, record, {".group/final/candidate.md": content})
    emit({"candidate": record, "report": content})
    return 0


def parser():
    cli = argparse.ArgumentParser(prog="groupctl")
    commands = cli.add_subparsers(dest="command", required=True)
    handlers = {"SYNC": sync, "PUB": pub, "INT": integrate, "REV": rev, "TEST": test, "FINAL": final}
    for name in ("STATUS", *handlers):
        command = commands.add_parser(name)
        command.set_defaults(handler=handlers.get(name))
        command.add_argument("--remote", default="origin")
        command.add_argument("--person")
        if name != "STATUS":
            command.add_argument("--check", action="append", default=[], help="JSON argv array, repeatable")
            command.add_argument("--timeout", type=int, default=300)
        if name in {"PUB", "REV", "TEST"}:
            command.add_argument("--summary")
        if name in {"PUB", "REV", "FINAL"}:
            command.add_argument("--issue", action="append", default=[])
        if name in {"PUB", "REV", "INT", "FINAL"}:
            command.add_argument("--confirm-commit")
        if name in {"REV", "INT"}:
            command.add_argument("change_id")
        if name == "PUB":
            command.add_argument("--inspect", action="store_true")
            command.add_argument("--interface", action="append", default=[])
            command.add_argument("--draft", action="store_true")
        if name == "REV":
            command.add_argument("--verdict", choices=("accept", "accept_with_changes", "reject", "needs_more_evidence"))
        if name == "INT":
            modes = command.add_mutually_exclusive_group()
            modes.add_argument("--apply", action="store_true")
            modes.add_argument("--record", choices=("success", "failed", "needs_human"))
            command.add_argument("--semantic-note")
            command.add_argument("--conflict", action="append", default=[])
            command.add_argument("--resolution")
        if name == "TEST":
            command.add_argument("scope", nargs="?", default="current")
            command.add_argument("--human-check", action="append", default=[])
        if name == "FINAL":
            modes = command.add_mutually_exclusive_group()
            modes.add_argument("--report", type=Path)
            modes.add_argument("--confirm-candidate")
            command.add_argument("--disposition")
            command.add_argument("--disposition-id", action="append", default=[])
            command.add_argument("--via-pr", action="store_true")
    return cli


def main():
    args = parser().parse_args()
    try:
        report, refs = status(args.remote, args.person, tags=args.command == "SYNC")
        usable = report["remote_fresh"] and report["canonical_commit"]
        if args.command == "STATUS":
            root = Path(git("rev-parse", "--show-toplevel").stdout.strip())
            cursor = root / ".group/local/sync.json"
            if cursor.is_file():
                try:
                    previous = read_json(cursor.read_text(encoding="utf-8"))
                    key = f"{report['group_id']}:{report['remote']}:{report['person_id']}"
                    if key in previous:
                        seen = set(previous[key])
                        report["event_processing_state"] = "since_last_sync"
                        report["events_since_last_sync"] = [
                            item for item in report["published_events_for_review"]
                            if item["event_id"] not in seen
                        ]
                except (OSError, TypeError, ValueError) as error:
                    report["warnings"].append(f"無法讀取本機 SYNC 紀錄：{error}")
            emit(report)
            return 0 if usable else 1
        if not usable:
            raise StatusError("；".join(report["warnings"]))
        if args.timeout <= 0:
            raise StatusError("--timeout 必須大於零。")
        if args.command != "SYNC" and (not report["member"] or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]*", report["person_id"]
        )):
            raise StatusError("請用 --person 選擇標準分支已有描述檔的成員。")
        return args.handler(args, report, refs)
    except (OSError, ValueError, StatusError) as error:
        print(f"{args.command}：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
