"""Synthetic, clock-controlled durable planner regressions."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from mdcattendance.schedules import (
    Planner,
    PlannerBlocked,
    PlannerConflict,
    PlannerInvalid,
)
from mdcattendance.storage import SINGAPORE, StateStore

STAMP = "2026-10-02T08:00:00+08:00"


@contextmanager
def startup_locks(store):
    descriptors = []
    try:
        for name in ("scheduler.lock", "run.lock"):
            fd = os.open(Path(store.state_dir) / name, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            descriptors.append(fd)
        yield
    finally:
        for fd in reversed(descriptors):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def legacy(store, uid, *, time=None, days=None, dispatch=None):
    """Write immutable legacy scheduler history directly, bypassing the removed API."""
    assert time is not None or days is not None or dispatch is not None
    with store._db:
        if time is not None:
            store._db.execute(
                """INSERT INTO schedules(uid,time) VALUES (?,?)
                   ON CONFLICT(uid) DO UPDATE SET time=excluded.time""",
                (uid, time),
            )
        for day, status in (days or {}).items():
            store._db.execute(
                """INSERT INTO schedule_days(uid,day,status) VALUES (?,?,?)
                   ON CONFLICT(uid,day) DO UPDATE SET status=excluded.status""",
                (uid, day, status),
            )
        for day, status in (dispatch or {}).items():
            store._db.execute(
                """INSERT INTO schedule_dispatch(uid,day,status,detail,created_at,updated_at)
                   VALUES (?,?,?,'',?,?)""",
                (uid, day, status, STAMP, STAMP),
            )


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = StateStore(self.directory.name)
        self.addCleanup(lambda: self.store.close())
        self.now = datetime(2026, 10, 2, 8, 31, tzinfo=SINGAPORE)
        self.today = self.now.date().isoformat()
        with startup_locks(self.store):
            self.store.upgrade_planner(today=self.now.date())
        self.planner = Planner(self.store, clock=lambda: self.now)

    def change(self, profile="normal", day=None, details=None):
        return {"date": day or self.today, "profile": profile, "details": details or {}}

    def plan(self, profile="normal", details=None):
        revision = self.planner.get_plan(7)["revision"]
        return self.planner.save_plan(
            7, [self.change(profile, details=details)], expected_revision=revision
        )

    def onboard(self, uid=7, name=" test   person "):
        revision = self.planner.get_settings(uid)["revision"]
        settings = self.planner.set_name(uid, name, expected_revision=revision)
        settings = self.planner.confirm_name(uid, expected_revision=settings["revision"])
        return self.planner.save_settings(
            uid,
            expected_revision=settings["revision"],
            enabled=True,
            prompt_time="08:00",
            auto_time="08:30",
            otp_ready=True,
            policy_accepted=True,
        )

    def check(self, records=(), label="empty", status=None):
        return SimpleNamespace(
            status=status or ("found" if records else "not_found"),
            records=tuple(records),
            checked_at=self.now,
            digest=hashlib.sha256(label.encode()).hexdigest(),
            error_code=None,
        )

    def record(self):
        return {
            "timestamp": self.now.isoformat(),
            "status": "Synthetic status",
            "details": {"Detail": "Example"},
        }

    def test_upgrade_requires_startup_and_import_is_once(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(directory)
            try:
                with self.assertRaises(RuntimeError):
                    Planner(store)
                future = (self.now.date() + timedelta(days=1)).isoformat()
                ma_history = (self.now.date() + timedelta(days=2)).isoformat()
                legacy(
                    store,
                    7,
                    time="08:20",
                    days={self.today: "normal", future: "none", ma_history: "mc"},
                    dispatch={self.today: "unknown"},
                )
                legacy(
                    store,
                    8,
                    time="07:59",
                    days={self.today: "wfh"},
                    dispatch={self.today: "collecting"},
                )
                attempt = store.prepare(
                    "synthetic-account", "https://example.invalid/form", self.now.date(), "submit"
                )
                store.transition(attempt, "running")
                store.transition(attempt, "submitting")
                with startup_locks(store):
                    store.upgrade_planner(today=self.now.date(), attendance_deadline="09:00")
                planner = Planner(store, clock=lambda: self.now)
                settings = planner.get_settings(7)
                self.assertFalse(settings["enabled"])
                self.assertFalse(settings["name_confirmed"])
                self.assertEqual(settings["auto_time"], "08:20")
                self.assertEqual(planner.get_settings(8)["auto_time"], "08:30")
                self.assertTrue(planner.get_settings(8)["needs_review"])
                self.assertEqual(planner.get_decision(7)["state"], "unknown")
                self.assertEqual(planner.get_decision(8)["state"], "missed")
                plans = planner.get_plan(7)["days"]
                self.assertEqual([row["profile"] for row in plans[:3]], ["normal", "skip", None])
                legacy(store, 7, days={future: "wfh"})
                with startup_locks(store):
                    store.upgrade_planner(today=self.now.date())
                self.assertEqual(planner.get_plan(7)["days"][1]["profile"], "skip")
                self.assertEqual(store.get_attempt(attempt)["status"], "submitting")
                self.assertEqual(
                    store._db.execute(
                        "SELECT status FROM schedule_days WHERE uid=7 AND day=?", (ma_history,)
                    ).fetchone()[0],
                    "mc",
                )
            finally:
                store.close()

    def test_plan_conflicts_atomicity_and_horizon(self):
        saved = self.plan()
        tomorrow = (self.now.date() + timedelta(days=1)).isoformat()
        with self.assertRaises(PlannerConflict):
            self.planner.save_plan(7, [self.change("wfh")], expected_revision=0)
        with self.assertRaises(ValueError):
            self.planner.save_plan(
                7, [self.change("wfh"), self.change("mc", tomorrow)], expected_revision=saved["revision"]
            )
        self.assertEqual(self.planner.get_plan(7)["days"][0]["profile"], "normal")
        self.assertEqual(self.planner.get_plan(7)["revision"], saved["revision"])
        for offset in (-1, 14):
            with self.assertRaises(ValueError):
                self.planner.save_plan(
                    7,
                    [self.change(day=(self.now.date() + timedelta(days=offset)).isoformat())],
                    expected_revision=saved["revision"],
                )
        deleted = self.planner.save_plan(7, [self.change(None)], expected_revision=saved["revision"])
        self.assertIsNone(deleted["days"][0]["profile"])
        skipped = self.plan("skip")
        self.assertEqual(skipped["days"][0]["profile"], "skip")
        self.assertFalse(skipped["days"][0]["ready"])

    def test_hold_abandonment_restart_and_explicit_restore(self):
        self.onboard()
        self.plan()
        decision = self.planner.ensure_decision(7)
        held = self.planner.action(7, "hold", expected_revision=decision["revision"])
        self.assertEqual(held["reason"], "user_edit")
        with self.assertRaises(PlannerBlocked):
            self.planner.claim_decision(7, self.today, expected_revision=held["revision"])
        self.store.close()
        self.store = StateStore(self.directory.name)
        with startup_locks(self.store):
            self.store.recover()
        self.planner = Planner(self.store, clock=lambda: self.now)
        reopened = self.planner.get_decision(7)
        self.assertEqual(reopened["state"], "held")
        restored = self.planner.action(7, "restore", expected_revision=reopened["revision"])
        self.assertEqual((restored["state"], restored["profile"]), ("awaiting", "normal"))
        self.assertEqual(self.planner.get_plan(7)["days"][0]["profile"], "normal")

    def test_claim_race_terminal_bulk_edits_and_deadline(self):
        self.onboard()
        self.plan()
        decision = self.planner.ensure_decision(7)
        ready = self.planner.action(7, "submit_now", expected_revision=decision["revision"])
        with self.assertRaises(PlannerConflict):
            self.planner.action(7, "skip", expected_revision=decision["revision"])
        claimed = self.planner.claim_decision(7, self.today, expected_revision=ready["revision"])
        with self.assertRaises(PlannerConflict):
            self.planner.claim_decision(7, self.today, expected_revision=ready["revision"])
        tomorrow = (self.now.date() + timedelta(days=1)).isoformat()
        revision = self.planner.get_plan(7)["revision"]
        with self.assertRaises(PlannerBlocked):
            self.planner.save_plan(
                7, [self.change("wfh", tomorrow), self.change("wfh")], expected_revision=revision
            )
        self.assertIsNone(self.planner.get_plan(7)["days"][1]["profile"])
        with self.assertRaises(PlannerBlocked):
            self.planner.action(7, "hold", expected_revision=claimed["revision"])
        with startup_locks(self.store):
            self.store.recover()
        recovered = self.planner.get_decision(7)
        self.assertEqual(recovered["state"], "failed")
        with self.assertRaises(PlannerBlocked):
            self.planner.claim_decision(7, self.today, expected_revision=recovered["revision"])
        self.onboard(8, "OTHER SYNTHETIC PERSON")
        self.planner.save_plan(8, [self.change()], expected_revision=0)
        eighth = self.planner.ensure_decision(8)
        self.now = self.now.replace(hour=9, minute=0)
        with self.assertRaises(PlannerBlocked):
            self.planner.claim_decision(8, self.today, expected_revision=eighth["revision"])

    def test_attempt_journal_recovery_and_cancellation_boundary(self):
        for uid, outcome in ((7, "unknown"), (8, "confirmed")):
            with self.subTest(outcome=outcome):
                self.onboard(uid, f"SYNTHETIC OWNER {uid}")
                self.planner.save_plan(uid, [self.change()], expected_revision=0)
                decision = self.planner.ensure_decision(uid)
                claimed = self.planner.claim_decision(
                    uid, self.today, expected_revision=decision["revision"]
                )
                attempt_id = self.store.prepare(
                    f"synthetic-account-{uid}",
                    "https://example.invalid/form",
                    self.now.date(),
                    "submit",
                )
                bound = self.planner.bind_attempt(
                    uid, self.today, attempt_id, expected_revision=claimed["revision"]
                )
                self.store.transition(attempt_id, "running")
                self.store.transition(attempt_id, "submitting")
                with self.assertRaises(PlannerBlocked):
                    self.planner.update_decision(
                        uid,
                        self.today,
                        expected_revision=bound["revision"],
                        state="held",
                        reason="user_edit",
                    )
                if outcome == "confirmed":
                    self.store.transition(attempt_id, "confirmed")
                with startup_locks(self.store):
                    self.store.recover()
                recovered = self.planner.get_decision(uid)
                self.assertEqual(recovered["state"], outcome)
                self.assertEqual(self.store.get_attempt(attempt_id)["status"], outcome)
                with self.assertRaises(PlannerBlocked):
                    self.planner.claim_decision(uid, self.today, expected_revision=recovered["revision"])

    def test_retained_observation_duplicates_and_owner_isolation(self):
        self.plan()
        self.planner.ensure_decision(7)
        row = self.record()
        found = self.planner.merge_observation(7, self.today, self.check([row, row], "found"))
        self.assertEqual(found["records"], [row, row])
        self.assertEqual(self.planner.get_decision(7)["state"], "recorded")
        disappeared = self.planner.merge_observation(7, self.today, self.check())
        self.assertEqual(disappeared["records"], [row, row])
        self.assertNotEqual(disappeared["digest"], found["digest"])
        unavailable = self.planner.merge_observation(7, self.today, self.check(status="unavailable"))
        self.assertEqual(unavailable, disappeared)
        self.assertIsNone(self.planner.get_observation(8, self.today))
        kept = self.planner.action(7, "keep", expected_revision=self.planner.get_decision(7)["revision"])
        self.assertEqual(kept["state"], "recorded")
        self.assertTrue(kept["acknowledged"])

    def test_consent_changed_answers_evidence_and_confirmation_reuse(self):
        self.onboard()
        self.plan()
        self.planner.ensure_decision(7)
        self.planner.merge_observation(7, self.today, self.check([self.record()], "found"))
        revision = self.planner.get_decision(7)["revision"]
        answer_hash = "a" * 64
        review = self.planner.review_consent(
            7, expected_revision=revision, answer_hash=answer_hash, local_attempt_ids=[]
        )
        with self.assertRaises(PlannerBlocked):
            self.planner.consume_consent(
                7,
                expected_revision=review["revision"],
                consent_digest=review["consent_digest"],
                answer_hash="b" * 64,
                local_attempt_ids=[],
            )
        authorized = self.planner.consume_consent(
            7,
            expected_revision=review["revision"],
            consent_digest=review["consent_digest"],
            answer_hash=answer_hash,
            local_attempt_ids=[],
        )
        self.assertEqual(authorized["state"], "ready")
        self.assertTrue(authorized["consent"]["authorized"])
        self.assertFalse(authorized["consent"]["consumed"])
        with self.assertRaises(PlannerBlocked):
            self.planner.consume_consent(
                7,
                expected_revision=authorized["revision"],
                consent_digest=review["consent_digest"],
                answer_hash=answer_hash,
                local_attempt_ids=[],
            )
        self.planner.merge_observation(7, self.today, self.check(label="changed"))
        changed = self.planner.get_decision(7)
        self.assertIsNone(changed["consent"])
        self.assertEqual(changed["state"], "recorded")
        with self.assertRaises(PlannerBlocked):
            self.planner.claim_decision(7, self.today, expected_revision=changed["revision"])

    def test_name_policy_and_collision_suspend_execution(self):
        self.plan()
        self.planner.ensure_decision(7)
        with self.assertRaises(PlannerInvalid):
            self.planner.save_settings(
                7,
                expected_revision=0,
                enabled=True,
                prompt_time="08:00",
                auto_time="08:30",
                otp_ready=True,
                policy_accepted=True,
            )
        settings = self.onboard()
        self.assertEqual(settings["attendance_name"], "TEST PERSON")
        changed = self.planner.set_name(7, "new person", expected_revision=settings["revision"])
        self.assertFalse(changed["enabled"])
        self.assertFalse(changed["name_confirmed"])
        self.assertEqual(self.planner.get_decision(7)["state"], "held")
        self.onboard(7, "SAME SYNTHETIC NAME")
        self.onboard(8, "SAME SYNTHETIC NAME")
        with self.assertRaises(PlannerBlocked):
            self.planner.assert_identity_unique(
                7, "Synthetic department", {7: "Synthetic department", 8: "synthetic department"}
            )
        self.assertFalse(self.planner.get_settings(7)["name_confirmed"])
        self.assertFalse(self.planner.get_settings(8)["enabled"])

    def test_settings_pause_never_resumes_user_edit_hold(self):
        settings = self.onboard()
        self.plan()
        decision = self.planner.ensure_decision(7)
        self.planner.action(7, "hold", expected_revision=decision["revision"])
        paused = self.planner.save_settings(
            7,
            expected_revision=settings["revision"],
            enabled=False,
            prompt_time="08:00",
            auto_time="08:30",
        )
        self.planner.save_settings(
            7,
            expected_revision=paused["revision"],
            enabled=True,
            prompt_time="08:00",
            auto_time="08:30",
            otp_ready=True,
            policy_accepted=True,
        )
        held = self.planner.get_decision(7)
        self.assertEqual((held["state"], held["reason"]), ("held", "user_edit"))

    def test_incomplete_ma_never_executes_and_prompt_bookkeeping_survives(self):
        self.onboard()
        self.plan("ma", {"period": "both", "timing": "0830"})
        plan = self.planner.get_plan(7)["days"][0]
        self.assertEqual(plan["details"]["timing"], "08:30")
        self.assertFalse(plan["ready"])
        decision = self.planner.ensure_decision(7)
        self.assertEqual((decision["state"], decision["reason"]), ("held", "incomplete"))
        with self.assertRaises(PlannerInvalid):
            self.planner.action(7, "submit_now", expected_revision=decision["revision"])
        prompted = self.planner.mark_prompt(
            7, self.today, expected_revision=decision["revision"], message_id=42
        )
        self.assertEqual(
            prompted["revision"],
            decision["revision"],
            "notification bookkeeping must not bump the owner decision revision",
        )
        with self.assertRaises(PlannerBlocked):
            self.planner.claim_decision(7, self.today, expected_revision=prompted["revision"])
        # A later plan edit bumps the decision revision; the stale button revision must reject
        # rather than rebase onto the newer plan.
        plan_revision = self.planner.get_plan(7)["revision"]
        self.planner.save_plan(
            7, [self.change("normal")], expected_revision=plan_revision
        )
        bumped = self.planner.get_decision(7)
        self.assertNotEqual(bumped["revision"], prompted["revision"])
        with self.assertRaises(PlannerConflict):
            self.planner.mark_prompt(
                7, self.today, expected_revision=prompted["revision"], message_id=44
            )
        with self.assertRaises(PlannerConflict):
            self.planner.action(7, "keep", expected_revision=prompted["revision"])


    def test_name_change_preserves_user_hold_and_invalidates_evidence(self):
        settings = self.onboard()
        self.plan()
        decision = self.planner.ensure_decision(7)
        self.assertEqual(decision["state"], "awaiting")
        held = self.planner.action(7, "hold", expected_revision=decision["revision"])
        self.assertEqual((held["state"], held["reason"]), ("held", "user_edit"))
        changed = self.planner.set_name(7, "renamed person", expected_revision=settings["revision"])
        self.assertFalse(changed["enabled"])
        self.assertFalse(changed["name_confirmed"])
        kept = self.planner.get_decision(7)
        self.assertEqual((kept["state"], kept["reason"]), ("held", "user_edit"))

    def test_identity_change_clears_observations_but_keeps_terminal_state(self):
        self.onboard()
        self.plan()
        self.planner.ensure_decision(7)
        self.planner.merge_observation(7, self.today, self.check([self.record()], "found"))
        recorded = self.planner.get_decision(7)
        self.assertEqual(recorded["state"], "recorded")
        settings = self.planner.get_settings(7)
        self.planner.set_name(7, "renamed person", expected_revision=settings["revision"])
        self.assertIsNone(self.planner.get_observation(7, self.today))
        after = self.planner.get_decision(7)
        self.assertEqual(after["state"], "recorded")
        self.assertIsNone(after["consent"])

    def test_same_confirmed_name_preserves_evidence_consent_and_settings(self):
        settings = self.onboard()
        self.plan()
        self.planner.ensure_decision(7)
        row = self.record()
        self.planner.merge_observation(7, self.today, self.check([row], "found"))
        decision = self.planner.get_decision(7)
        self.assertEqual(decision["state"], "recorded")
        same = self.planner.set_name(7, "TEST PERSON", expected_revision=settings["revision"])
        self.assertTrue(same["name_confirmed"])
        self.assertTrue(same["enabled"])
        self.assertEqual(same["revision"], settings["revision"])
        self.assertEqual(self.planner.get_observation(7, self.today)["records"], [row])
        refreshed = self.planner.get_decision(7)
        self.assertEqual(refreshed["state"], "recorded")
        self.assertEqual(refreshed["revision"], decision["revision"])
        with self.assertRaises(PlannerConflict):
            self.planner.set_name(7, "TEST PERSON", expected_revision=settings["revision"] + 1)

    def test_paused_auto_holds_plan_restore_and_manual_submit(self):
        settings = self.onboard()
        self.plan("normal")
        decision = self.planner.ensure_decision(7)
        self.assertEqual(decision["state"], "awaiting")
        paused = self.planner.save_settings(
            7,
            expected_revision=settings["revision"],
            enabled=False,
            prompt_time="08:00",
            auto_time="08:30",
        )
        held = self.planner.get_decision(7)
        self.assertEqual((held["state"], held["reason"]), ("held", "disabled"))
        # Re-saving the plan must not re-arm automatic execution while paused.
        self.planner.save_plan(
            7, [self.change("wfh")], expected_revision=paused["plan_revision"]
        )
        replanned = self.planner.get_decision(7)
        self.assertEqual((replanned["state"], replanned["reason"]), ("held", "disabled"))
        # An explicit manual submission remains available while paused.
        ready = self.planner.action(7, "submit_now", expected_revision=replanned["revision"])
        self.assertEqual(ready["state"], "ready")
        claimed = self.planner.claim_decision(7, self.today, expected_revision=ready["revision"])
        self.assertEqual(claimed["state"], "running")

        # ensure_decision and restore honour the pause for a second owner.
        other = self.onboard(8, "SECOND SYNTHETIC")
        self.planner.save_settings(
            8,
            expected_revision=other["revision"],
            enabled=False,
            prompt_time="08:00",
            auto_time="08:30",
        )
        self.planner.save_plan(8, [self.change()], expected_revision=0)
        fresh = self.planner.ensure_decision(8)
        self.assertEqual((fresh["state"], fresh["reason"]), ("held", "disabled"))
        owner_hold = self.planner.action(8, "hold", expected_revision=fresh["revision"])
        self.assertEqual(owner_hold["reason"], "user_edit")
        restored = self.planner.action(8, "restore", expected_revision=owner_hold["revision"])
        self.assertEqual((restored["state"], restored["reason"]), ("held", "disabled"))

    def test_delivered_reminder_binds_message_without_revision_bump(self):
        self.onboard()
        self.plan()
        decision = self.planner.ensure_decision(7)
        self.planner.mark_prompt(
            7, self.today, expected_revision=decision["revision"], message_id=11
        )
        reminded = self.planner.mark_reminder(
            7, self.today, expected_revision=decision["revision"], message_id=77
        )
        self.assertTrue(reminded["reminder_sent"])
        self.assertEqual(reminded["prompt_message_id"], 77)
        self.assertEqual(reminded["revision"], decision["revision"])
        with self.assertRaises(PlannerBlocked):
            self.planner.mark_reminder(
                7, self.today, expected_revision=decision["revision"], message_id=88
            )
        plan_revision = self.planner.get_plan(7)["revision"]
        self.planner.save_plan(7, [self.change("wfh")], expected_revision=plan_revision)
        with self.assertRaises(PlannerConflict):
            self.planner.mark_reminder(
                7, self.today, expected_revision=decision["revision"], message_id=99
            )

    def test_expire_decisions_terminalizes_once_and_preserves_other_states(self):
        prior = self.today
        self.onboard(7, "OWNER SEVEN")
        self.planner.save_plan(7, [self.change()], expected_revision=0)
        self.planner.ensure_decision(7)  # awaiting

        self.onboard(8, "OWNER EIGHT")
        self.planner.save_plan(8, [self.change()], expected_revision=0)
        held = self.planner.ensure_decision(8)
        self.planner.action(8, "hold", expected_revision=held["revision"])  # held/user_edit

        self.onboard(9, "OWNER NINE")
        self.planner.save_plan(9, [self.change()], expected_revision=0)
        self.planner.ensure_decision(9)
        record = self.record()
        self.planner.merge_observation(9, self.today, self.check([record], "found"))  # recorded

        self.onboard(10, "OWNER TEN")
        self.planner.save_plan(10, [self.change()], expected_revision=0)
        verification = self.planner.ensure_decision(10)
        self.planner.update_decision(
            10,
            self.today,
            expected_revision=verification["revision"],
            state="held",
            reason="verification",
        )  # held/verification

        self.onboard(11, "OWNER ELEVEN")
        self.planner.save_plan(11, [self.change()], expected_revision=0)
        running = self.planner.ensure_decision(11)
        self.planner.claim_decision(11, self.today, expected_revision=running["revision"])

        self.now = self.now + timedelta(days=1)
        expired = self.planner.expire_decisions(self.now.date())
        self.assertEqual(
            sorted((row["uid"], row["day"]) for row in expired), [(7, prior), (10, prior)]
        )
        missed = self.planner.get_decision(7, prior)
        self.assertEqual((missed["state"], missed["reason"]), ("missed", "deadline"))
        self.assertEqual(self.planner.get_decision(10, prior)["state"], "missed")
        # Excluded states and evidence are untouched by expiration.
        for uid, state, reason in (
            (8, "held", "user_edit"),
            (9, "recorded", "records_found"),
            (11, "running", ""),
        ):
            row = self.planner.get_decision(uid, prior)
            self.assertEqual((row["state"], row["reason"]), (state, reason))
        self.assertEqual(self.planner.get_observation(9, prior)["records"], [record])
        # A second call is a no-op: already-missed rows are not rewritten.
        first_revision = self.planner.get_decision(7, prior)["revision"]
        self.assertEqual(self.planner.expire_decisions(self.now.date()), [])
        self.assertEqual(self.planner.get_decision(7, prior)["revision"], first_revision)


if __name__ == "__main__":
    unittest.main()
