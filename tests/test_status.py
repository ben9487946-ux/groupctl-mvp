import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CLI = Path(__file__).resolve().parents[1] / "groupctl.py"


class StatusTests(unittest.TestCase):
    def run_git(self, cwd, *args):
        return subprocess.run(
            ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
        ).stdout.strip()

    def write(self, repo, name, content):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(content), encoding="utf-8")

    def identity(self, repo):
        self.run_git(repo, "config", "user.name", "Fixture")
        self.run_git(repo, "config", "user.email", "fixture@example.invalid")

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.remote = base / "remote.git"
        self.alice = base / "alice"
        self.bob = base / "bob"
        self.run_git(base, "init", "--bare", "-b", "main", str(self.remote))
        self.run_git(base, "clone", str(self.remote), str(self.alice))
        self.identity(self.alice)
        self.write(self.alice, ".group/group.json", {
            "protocol_version": "0.1", "group_id": "G-12", "canonical_branch": "main",
            "assignment_file": ".group/assignment/assignment.md",
        })
        self.write(self.alice, ".group/members/P17.json", {
            "protocol_version": "0.1", "person_id": "P17", "display_name": "Alice",
            "joined_at": "2026-11-13T02:00:00Z",
        })
        for person, name in (("P22", "Bob"), ("P31", "Carol")):
            self.write(self.alice, f".group/members/{person}.json", {
                "protocol_version": "0.1", "person_id": person, "display_name": name,
                "joined_at": "2026-11-13T02:00:00Z",
            })
        (self.alice / "app.txt").write_text("initial\n", encoding="utf-8")
        self.run_git(self.alice, "add", ".")
        self.run_git(self.alice, "commit", "-m", "Initial")
        self.run_git(self.alice, "push", "origin", "main")
        self.run_git(base, "clone", str(self.remote), str(self.bob))
        self.identity(self.bob)
        self.run_git(self.alice, "switch", "-c", "people/P17/parser")

    def event(self, repo, kind, suffix, **fields):
        event_id = f"E-20261113T020000Z-P17-{suffix}"
        self.write(repo, f".group/events/{event_id}.json", {
            "protocol_version": "0.1", "event_id": event_id, "event_type": kind,
            "group_id": "G-12", "person_id": "P17",
            "created_at": "2026-11-13T02:00:00Z", "related_events": [], **fields,
        })
        return event_id

    def status(self, repo, expected=0, *args):
        return self.cli(repo, "STATUS", *args, expected=expected)

    def cli(self, repo, *args, expected=0):
        result = subprocess.run(
            [sys.executable, str(CLI), *args], cwd=repo,
            capture_output=True, encoding="utf-8", errors="replace",
        )
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return json.loads(result.stdout) if result.stdout else None

    def commit_file(self, repo, name, contents):
        (repo / name).write_text(contents, encoding="utf-8")
        self.run_git(repo, "add", "--", name)
        self.run_git(repo, "commit", "-m", f"Update {name}")
        return self.run_git(repo, "rev-parse", "HEAD")

    def publish(self, repo, commit):
        return self.cli(repo, "PUB", "--confirm-commit", commit, "--summary", "Fixture contribution",
                        "--check", '["{python}", "-c", "raise SystemExit(0)"]')

    def test_sync_fast_forward_cursor_and_dirty_guard(self):
        self.commit_file(self.bob, ".gitignore", "/.group/local/*\n!/.group/local/.gitkeep\n")
        self.event(self.bob, "finding", "a111", summary="Group notice", evidence=[])
        self.run_git(self.bob, "add", ".group/events")
        self.run_git(self.bob, "commit", "-m", "Notice")
        self.run_git(self.bob, "push", "origin", "main")
        scratch = self.alice / "unfinished.txt"
        scratch.write_text("keep me", encoding="utf-8")
        self.assertEqual(self.cli(self.alice, "SYNC")["action"], "metadata_only_local_work")
        self.assertEqual(scratch.read_text(encoding="utf-8"), "keep me")
        scratch.unlink()
        self.assertEqual(self.cli(self.alice, "SYNC")["action"], "metadata_only_validation_required")
        result = self.cli(self.alice, "SYNC", "--check", '["{python}", "-c", "from pathlib import Path; assert Path(\'.gitignore\').is_file()"]')
        self.assertEqual(result["action"], "fast_forwarded")
        self.assertEqual(len(result["new_events"]), 1)
        self.assertEqual(self.cli(self.alice, "SYNC")["new_events"], [])
        self.assertEqual(self.status(self.alice)["event_processing_state"], "since_last_sync")

    def test_publish_review_integrate_and_test(self):
        commit = self.commit_file(self.alice, "feature.txt", "feature")
        self.assertIn("feature.txt", self.cli(self.alice, "PUB", "--inspect")["diff"])
        self.cli(self.alice, "PUB", "--confirm-commit", commit, "--summary", "No tests", expected=1)
        publication = self.publish(self.alice, commit)
        self.assertEqual(self.publish(self.alice, commit)["event_id"], publication["event_id"])
        change_id = publication["change_id"]
        self.run_git(self.bob, "switch", "-c", "people/P22/integration")
        self.commit_file(self.bob, "other.txt", "other")
        old = self.run_git(self.bob, "rev-parse", "HEAD")
        self.assertIn("feature.txt", self.cli(self.bob, "REV", change_id)["diff"])
        review = self.cli(self.bob, "REV", change_id, "--verdict", "accept",
                          "--confirm-commit", commit, "--summary", "Inspected fixture")
        self.assertEqual(review["related_events"], [publication["event_id"]])
        self.assertEqual(self.run_git(self.bob, "rev-parse", "HEAD"), old)
        merged = self.cli(self.bob, "INT", change_id, "--apply", "--confirm-commit", commit)
        self.assertEqual(merged["result"], "merged_pending_verification")
        self.assertEqual(len(self.run_git(self.bob, "rev-list", "--parents", "-n", "1", "HEAD").split()), 3)
        check = '["{python}", "-c", "from pathlib import Path; assert Path(\'feature.txt\').is_file() and Path(\'other.txt\').is_file()"]'
        verified = self.cli(self.bob, "INT", change_id, "--record", "success",
                            "--confirm-commit", commit, "--semantic-note", "Independent files.", "--check", check)
        self.assertEqual(verified["verification"]["tests"], "pass")
        tested = self.cli(self.bob, "TEST", "--check", check)
        self.assertEqual(tested["automated_result"], "pass")
        self.assertEqual(tested["commit"], merged["commit"])
        failed = self.cli(self.bob, "TEST", change_id,
                          "--check", '["{python}", "-c", "raise SystemExit(5)"]', expected=1)
        self.assertEqual(failed["automated_result"], "fail")
        self.assertEqual(failed["commit"], commit)
        unavailable = self.cli(self.bob, "TEST", "main", expected=1)
        self.assertEqual(unavailable["automated_result"], "not_available")
        # Simulate approval of a pull request into canonical, then a third
        # member takes over with only a fresh clone and the shared records.
        self.run_git(self.bob, "push", "origin", "HEAD:main")
        carol = self.remote.parent / "carol"
        self.run_git(self.remote.parent, "clone", str(self.remote), str(carol))
        self.identity(carol)
        self.run_git(carol, "switch", "-c", "people/P31/verification")
        recovered = self.status(carol)
        self.assertEqual(recovered["person_id"], "P31")
        self.assertEqual(recovered["canonical_commit"], merged["commit"])
        self.assertEqual(recovered["last_known_test"]["event_id"], tested["event_id"])
        self.assertIn(review["event_id"], {item["event_id"] for item in recovered["published_events_for_review"]})

    def test_integration_preserves_conflict_for_review(self):
        incoming = self.commit_file(self.alice, "app.txt", "Alice")
        change_id = self.publish(self.alice, incoming)["change_id"]
        self.run_git(self.bob, "switch", "-c", "people/P22/conflict")
        before = self.commit_file(self.bob, "app.txt", "Bob")
        result = self.cli(self.bob, "INT", change_id, "--apply", "--confirm-commit", incoming, expected=1)
        self.assertEqual(result["conflicts"], ["app.txt"])
        self.assertEqual(self.run_git(self.bob, "rev-parse", "HEAD"), before)
        self.assertIn("<<<<<<<", (self.bob / "app.txt").read_text(encoding="utf-8"))
        record = self.cli(self.bob, "INT", change_id, "--record", "needs_human",
                          "--confirm-commit", incoming, "--conflict", "app.txt",
                          "--semantic-note", "Need input on intended output.", expected=1)
        self.assertEqual(record["result"], "needs_human")
        (self.bob / "app.txt").write_text("Alice + Bob", encoding="utf-8")
        self.run_git(self.bob, "add", "app.txt")
        self.run_git(self.bob, "commit", "-m", "Preserve both intended outputs")
        resolved = self.cli(
            self.bob, "INT", change_id, "--record", "success", "--confirm-commit", incoming,
            "--conflict", "app.txt", "--resolution", "Combined intended outputs.",
            "--semantic-note", "The agreed combined output preserves both contributions.",
            "--check", """["{python}", "-c", "from pathlib import Path; assert Path('app.txt').read_text() == 'Alice + Bob'"]""",
        )
        self.assertEqual(resolved["result"], "success")

    def test_final_requires_candidate_then_confirmation(self):
        canonical = self.run_git(self.bob, "rev-parse", "HEAD")
        preview = self.cli(self.alice, "FINAL")
        self.assertEqual(preview["canonical_commit"], canonical)
        report_file = self.remote.parent / "report.md"
        report_file.write_text(
            "# Group Final Report\n\n" + "\n\n".join(f"## {heading}\n{content}" for heading, content in (
                ("Assignment", "Fixture"), ("Result", "Built fixture"),
                ("Canonical Commit", canonical), ("Important Changes", "Initial fixture"),
                ("Validation", "Check app.txt"), ("Known Limitations", "None"), ("Contributors", "Alice"),
            )), encoding="utf-8",
        )
        result = self.cli(self.alice, "FINAL", "--report", str(report_file),
                          "--confirm-commit", canonical,
                          "--check", '["{python}", "-c", "from pathlib import Path; assert Path(\'app.txt\').is_file()"]')
        candidate = result["candidate"]["event_id"]
        self.run_git(self.bob, "fetch", "origin")
        self.assertEqual(self.run_git(self.bob, "rev-parse", "origin/main"), canonical)
        proposal = self.cli(self.alice, "FINAL", "--confirm-candidate", candidate, "--via-pr")
        self.assertTrue(proposal["pull_request_required"])
        self.assertFalse(any(item["event_type"] == "final_result"
                             for item in self.status(self.alice)["published_events_for_review"]))
        envelope = self.cli(self.alice, "FINAL", "--confirm-candidate", candidate)
        self.assertEqual(envelope["canonical_commit"], canonical)
        self.run_git(self.bob, "fetch", "origin")
        self.assertEqual(json.loads(self.run_git(self.bob, "show", "origin/main:.group/final/final.json")), envelope)
        self.assertEqual(self.run_git(self.bob, "rev-parse", "origin/main^"), canonical)
        self.cli(self.alice, "FINAL", "--confirm-candidate", candidate, expected=1)

    def test_dirty_work_and_diverged_branch_are_preserved(self):
        (self.alice / "feature.txt").write_bytes(b"staged\n")
        self.run_git(self.alice, "add", "feature.txt")
        self.run_git(self.alice, "commit", "-m", "Personal feature")
        (self.alice / "app.txt").write_bytes(b"staged\n")
        self.run_git(self.alice, "add", "app.txt")
        (self.alice / "app.txt").write_bytes(b"unstaged\n")
        (self.alice / "scratch.txt").write_bytes(b"untracked\n")
        (self.bob / "canonical.txt").write_bytes(b"accepted\n")
        self.run_git(self.bob, "add", ".")
        self.run_git(self.bob, "commit", "-m", "Canonical update")
        self.run_git(self.bob, "push", "origin", "main")
        before = self.run_git(self.alice, "status", "--porcelain")
        head = self.run_git(self.alice, "rev-parse", "HEAD")
        index_path = self.alice / ".git/index"
        index = index_path.read_bytes()

        report = self.status(self.alice)
        self.assertTrue(report["remote_fresh"])
        self.assertEqual((report["ahead"], report["behind"]), (1, 1))
        self.assertEqual(report["canonical_commit"], self.run_git(self.bob, "rev-parse", "HEAD"))
        self.assertEqual(report["member"]["display_name"], "Alice")
        self.assertEqual(report["commit"], head)
        self.assertEqual(index_path.read_bytes(), index)
        self.assertEqual(self.run_git(self.alice, "status", "--porcelain"), before)
        self.assertEqual(self.run_git(self.alice, "rev-parse", "HEAD"), head)
        self.assertEqual((self.alice / "app.txt").read_bytes(), b"unstaged\n")
        self.assertEqual((self.alice / "scratch.txt").read_bytes(), b"untracked\n")
        self.assertFalse((self.alice / "canonical.txt").exists())

    def test_fresh_clone_recovers_published_events_and_ignores_local_file(self):
        commit = self.run_git(self.alice, "rev-parse", "HEAD")
        change = self.event(
            self.alice, "change_published", "a001", change_id="C-17",
            branch="people/P17/parser", commit=commit, summary="Parser",
            files_changed=[], interface_changes=[], tests_run=[], known_issues=[],
            proposed_for_integration=True,
        )
        tested = self.event(
            self.alice, "test_result", "a002", scope="current_branch", commit=commit,
            commands=["fixture"], automated_result="pass", passed=1, failed=0, skipped=0,
            human_checks_required=[], summary="Fixture passed",
        )
        blocker = self.event(self.alice, "blocker", "a003", summary="Review pending",
                             blocking_change_id="C-17", requested_action="Review")
        self.run_git(self.alice, "add", ".group/events")
        self.run_git(self.alice, "commit", "-m", "Publish event files")
        self.run_git(self.alice, "push", "origin", "people/P17/parser")
        fresh = self.remote.parent / "fresh"
        self.run_git(self.remote.parent, "clone", "--single-branch", str(self.remote), str(fresh))
        self.event(fresh, "finding", "a004", summary="Local only", evidence=[])

        report = self.status(fresh, 0, "--person", "P17")
        self.assertEqual(
            {e["event_id"] for e in report["published_events_for_review"]}, {change, tested, blocker}
        )
        self.assertEqual(report["published_local_changes"][0]["change_id"], "C-17")
        self.assertEqual(report["last_known_test"]["event_id"], tested)
        self.assertEqual(report["reported_blockers"][0]["event_id"], blocker)
        self.assertEqual(report["event_processing_state"], "unknown")

    def test_unavailable_remote_marks_cached_snapshot_stale(self):
        self.run_git(self.alice, "remote", "set-url", "origin", str(self.remote.parent / "missing"))
        report = self.status(self.alice, 1)
        self.assertFalse(report["remote_fresh"])
        self.assertTrue(report["canonical_commit"])
        self.assertTrue(report["warnings"])

    def test_malformed_remote_event_is_reported(self):
        self.write(self.bob, ".group/events/invalid.json", {"event_id": "invalid"})
        self.run_git(self.bob, "add", ".")
        self.run_git(self.bob, "commit", "-m", "Publish invalid event")
        self.run_git(self.bob, "push", "origin", "main")
        report = self.status(self.alice)
        self.assertEqual(report["published_events_for_review"], [])
        self.assertTrue(any("invalid.json" in warning for warning in report["warnings"]))


if __name__ == "__main__":
    unittest.main()
