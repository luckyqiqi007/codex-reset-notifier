"""Replay the three Day 21 revisions and preserve substantive corrections."""
from datetime import datetime, timezone
import importlib.util
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import notifier

ANNOUNCEMENT = '''🎁 Day 21：又要重置了
（具体时间 Tibo 没公布）
────────────
🎁 Day 21: Quota Reset Confirmed for Today
• Limit Reset: No specific timeframe announced.
Source
Follow @codexreset'''
REWORDING = '''🎁 Day 21：又要重置了
（具体时间 Tibo 没公布）
────────────
🎁 Day 21: Yet Another Reset Today
• Limit Reset: No exact time from Tibo yet.
Source
Follow @codexreset'''
COMPLETION = '''🎁 Day 21：又要重置了
（14:10 已经重置了）
────────────
🎁 Day 21: Yet Another Reset Today
• Limit Reset: Fresh quotas are now live.
Source
Follow @codexreset'''


def row(text=ANNOUNCEMENT, number=66, links=None):
    return {"id": number, "text": text, "date": "2026-10-26T05:42:00+00:00",
            "links": links if links is not None else ["https://x.com/thsottiaux/status/2114588174249071102?s=46", "https://t.me/codexreset"]}


class MessageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.db"
        self.now = datetime(2026, 10, 26, 5, 43, tzinfo=timezone.utc).timestamp()

    def store(self):
        store = notifier.Store(self.path)
        self.addCleanup(store.close)
        return store

    def baseline(self, store):
        store.observe([row(number=65)], self.now, True)

    def consume(self, store):
        messages = store.pending(self.now + 3600)
        for message in messages:
            store.delivered(message["id"], message["hash"])
        return messages

    def test_three_revisions_deliver_only_announcement_and_completion(self):
        store = self.store(); self.baseline(store)
        store.observe([row()], self.now, True)
        first = self.consume(store)
        self.assertEqual(len(first), 1)
        store.observe([row(REWORDING)], self.now + 120, True)
        self.assertEqual(store.pending(self.now + 120), [], "English-only equivalent wording must not resend")
        store.observe([row(COMPLETION)], self.now + 1800, True)
        last = self.consume(store)
        self.assertEqual(len(last), 1)
        self.assertIn("重置预告", json.loads(first[0]["payload"])["markdown"]["content"])
        self.assertIn("来源称已完成", json.loads(last[0]["payload"])["markdown"]["content"])

    def test_cosmetic_edit_preserves_backoff_and_attempt_count(self):
        store = self.store(); self.baseline(store)
        store.observe([row()], self.now, True)
        store.failed(66, self.now)
        store.observe([row(REWORDING)], self.now + 1, True)
        self.assertEqual(store.pending(self.now + 1), [])
        item = store.pending(self.now + 31)[0]
        self.assertEqual(item["attempts"], 1)

    def test_tracking_links_reordering_and_follow_footer_do_not_resend(self):
        store = self.store(); self.baseline(store)
        store.observe([row()], self.now, True); self.consume(store)
        links = ["https://t.me/codexreset", "https://twitter.com/thsottiaux/status/2114588174249071102?s=20"]
        store.observe([row(ANNOUNCEMENT.replace("Follow @codexreset", ""), links=links)], self.now + 1, True)
        self.assertEqual(store.pending(self.now + 1), [])

    def test_english_only_new_schedule_is_not_suppressed(self):
        store = self.store(); self.baseline(store)
        store.observe([row()], self.now, True); self.consume(store)
        store.observe([row(REWORDING.replace("No exact time from Tibo yet.", "Reset at 10:00 UTC for Pro users."))], self.now + 1, True)
        self.assertEqual(len(store.pending(self.now + 1)), 1)

    def test_english_only_scope_or_limit_change_is_not_suppressed(self):
        store = self.store(); self.baseline(store)
        store.observe([row()], self.now, True); self.consume(store)
        store.observe([row(REWORDING + "\nOnly Plus users get 2x credits.")], self.now + 1, True)
        self.assertEqual(len(store.pending(self.now + 1)), 1)

    def test_symbols_that_reverse_eligibility_are_material(self):
        store = self.store(); self.baseline(store)
        store.observe([row("Quota reset eligibility: Plus ✅ / Pro ❌")], self.now, True); self.consume(store)
        store.observe([row("Quota reset eligibility: Plus ❌ / Pro ✅")], self.now + 1, True)
        self.assertEqual(len(store.pending(self.now + 1)), 1)

    def test_new_link_for_same_day_event_does_not_repeat_announcement(self):
        store = self.store(); self.baseline(store)
        store.observe([row()], self.now, True); self.consume(store)
        store.observe([row(links=["https://x.com/thsottiaux/status/2114588174249071103"])], self.now + 1, True)
        self.assertEqual(store.pending(self.now + 1), [])

    def test_retraction_without_keyword_is_still_delivered(self):
        store = self.store(); self.baseline(store)
        store.observe([row()], self.now, True); self.consume(store)
        store.observe([row("更正：此前消息有误，请忽略，以官方通知为准。")], self.now + 1, True)
        items = store.pending(self.now + 1)
        self.assertEqual(len(items), 1)
        self.assertIn("更正", json.loads(items[0]["payload"])["markdown"]["content"])

    def test_restarts_and_upgrade_do_not_replay_observed_messages(self):
        store = self.store(); self.baseline(store)
        store.observe([row()], self.now, True); self.consume(store)
        reopened = self.store()
        reopened.observe([row(REWORDING)], self.now + 120, True)
        self.assertEqual(reopened.pending(self.now + 120), [])

    def test_payload_distinguishes_channel_publish_and_observation_time(self):
        store = self.store(); self.baseline(store)
        store.observe([row()], self.now, True)
        content = json.loads(store.pending(self.now)[0]["payload"])["markdown"]["content"]
        self.assertIn("频道发布时间：2026-10-26 13:42:00", content)
        self.assertIn("本次发现时间：2026-10-26 13:43:00", content)
        self.assertNotIn("原帖发布时间", content)
        self.assertNotIn("[来源2]", content)
        self.assertNotIn("Follow @codexreset", content)
        self.assertLessEqual(len(content.encode()), 4000)

    def test_different_post_id_with_same_day_and_link_is_same_event(self):
        store = self.store(); self.baseline(store)
        store.observe([row()], self.now, True); self.consume(store)
        store.observe([row(REWORDING, number=67)], self.now + 1, True)
        self.assertEqual(store.pending(self.now + 1), [])

    def legacy_payload(self, item):
        content = ("**Codex 额度通知｜频道转发**\n原帖发布时间：2026-10-26 13:42（北京时间）\n\n"
                   + item["text"] + "\n\n[频道原帖](https://t.me/codexreset/66)"
                   + " · [来源1]("+item["links"][0]+") · [来源2](https://t.me/codexreset)"
                   + "\n频道消息转发，未独立核验账号到账。")
        return json.dumps({"msgtype": "markdown", "markdown": {"content": content}}, ensure_ascii=False)

    def seed_legacy(self, state="sent", attempts=0, retry=0):
        db = sqlite3.connect(self.path)
        db.executescript('''CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE posts (id INTEGER PRIMARY KEY, hash TEXT NOT NULL, payload TEXT NOT NULL,
            state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_retry REAL NOT NULL DEFAULT 0,
            published REAL NOT NULL, ever_sent INTEGER NOT NULL DEFAULT 0);''')
        item = row()
        digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with db:
            db.executemany("INSERT INTO meta VALUES(?,?)", [("channel", "codexreset"), ("cursor", "66")])
            db.execute("INSERT INTO posts VALUES(?,?,?,?,?,?,?,?)", (66, digest, self.legacy_payload(item), state,
                       attempts, retry, self.now - 60, int(state == "sent")))
        db.close()

    def test_legacy_sent_message_with_reworded_body_does_not_replay_on_upgrade(self):
        self.seed_legacy()
        store = self.store()
        store.observe([row(REWORDING)], self.now + 120, True)
        self.assertEqual(store.pending(self.now + 120), [])
        self.assertEqual(store.counts()["sent"], 1)

    def test_legacy_pending_queue_retains_retry_state_after_upgrade(self):
        self.seed_legacy(state="pending", attempts=2, retry=self.now + 600)
        store = self.store()
        store.observe([row(REWORDING)], self.now + 120, True)
        self.assertEqual(store.pending(self.now + 120), [])
        self.assertEqual(store.pending(self.now + 601)[0]["attempts"], 2)

    def test_legacy_completion_update_is_not_silenced_by_migration(self):
        self.seed_legacy()
        store = self.store()
        store.observe([row(COMPLETION)], self.now + 1800, True)
        self.assertEqual(len(store.pending(self.now + 1800)), 1)

    def test_old_writer_after_rollback_cannot_leave_stale_semantic_snapshot(self):
        store = self.store(); self.baseline(store)
        store.observe([row(COMPLETION)], self.now, True); self.consume(store)
        item = row()
        digest = hashlib.sha256(json.dumps(item, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        with store.db:
            store.db.execute("UPDATE posts SET hash=?,payload=?,state='sent' WHERE id=66", (digest, self.legacy_payload(item)))
        store.observe([row(REWORDING)], self.now + 120, True)
        self.assertEqual(store.pending(self.now + 120), [])


class StatusTests(unittest.TestCase):
    def classify(self, text):
        self.assertIsNotNone(importlib.util.find_spec("message_content"), "status classifier is not implemented")
        import message_content
        return message_content.classify_status(text)

    def test_confirmed_for_today_is_an_announcement_not_completion(self):
        self.assertEqual(self.classify(ANNOUNCEMENT), "announced")
        self.assertEqual(self.classify(REWORDING), "announced")
        self.assertEqual(self.classify(COMPLETION), "reported_complete")

    def test_negations_and_conditionals_cannot_claim_completion(self):
        for text in ("The quota has not been reset yet.", "Not all quotas have been reset.",
                     "If quotas have been reset, you can continue.", "配额尚未重置。",
                     "如果已经重置，请再尝试。", "据说已重置，但尚未确认。",
                     "No reset today.", "今天不会重置。", "No quotas have been reset.",
                     "None of the quotas have been reset.", "There is no evidence that quotas have been reset.",
                     "Quotas have been reset?", "配额已经重置了吗？"):
            with self.subTest(text=text):
                self.assertNotEqual(self.classify(text), "reported_complete")

    def test_denial_is_not_labeled_as_a_new_announcement(self):
        for text in ("No reset today.", "There will be no reset today.", "今天不会重置。", "今日不重置。"):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "unconfirmed")

    def test_future_completion_is_only_an_announcement(self):
        for text in ("Quota reset will be completed tomorrow.", "预计明天完成重置。", "Quotas will have been reset by 10:00."):
            with self.subTest(text=text):
                self.assertEqual(self.classify(text), "announced")


if __name__ == "__main__":
    unittest.main()
