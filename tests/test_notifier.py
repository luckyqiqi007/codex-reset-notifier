"""Behavior tests: public-source parsing, durable delivery and secret boundaries."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "notifier.py"


def load_module():
    spec = importlib.util.spec_from_file_location("reset_notifier", SOURCE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def post(number=101, text="配额已全员重置！", date="2026-10-23T04:00:00+00:00"):
    return {"id": number, "text": text, "date": date, "links": ["https://x.com/example/status/123"]}


class NotifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = load_module()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.db"
        self.now = 1792728000  # 2026-10-23 04:00 UTC

    def store(self):
        db = self.app.Store(self.path)
        self.addCleanup(db.close)
        return db

    def test_parser_keeps_body_source_and_date_excludes_quotes_views(self):
        html = '''<div class="tgme_widget_message" data-post="codexreset/101">
          <a class="tgme_widget_message_author">Codex Reset Push</a>
          <div class="tgme_widget_message_reply">旧消息：额度加倍</div>
          <div class="tgme_widget_message_text js-message_text"><b>配额重置</b><br>正在发放
          <a href="https://x.com/example/status/123">Source</a></div>
          <span class="tgme_widget_message_views">125</span>
          <time datetime="2026-10-23T04:00:00+00:00">12:00</time></div>'''
        rows = self.app.parse_page(html, "codexreset")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], 101)
        self.assertIn("配额重置\n正在发放", rows[0]["text"])
        self.assertNotIn("125", rows[0]["text"])
        self.assertNotIn("旧消息", rows[0]["text"])
        self.assertEqual(rows[0]["links"], ["https://x.com/example/status/123"])
        self.assertEqual(rows[0]["date"], "2026-10-23T04:00:00+00:00")

    def test_login_page_and_wrong_channel_are_not_empty_success(self):
        for html in ("<html>Sign in</html>", '<div data-post="other/101"></div>'):
            with self.subTest(html=html), self.assertRaises(self.app.SourceError):
                self.app.parse_page(html, "codexreset")

    def test_initial_baseline_does_not_send_history(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        self.assertEqual(db.pending(self.now), [])
        self.assertEqual(db.cursor(), 101)

    def test_new_message_survives_restart_and_success_deduplicates(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        db.observe([post(), post(102)], self.now, enabled=True)
        reopened = self.store()
        pending = reopened.pending(self.now)
        self.assertEqual([x["id"] for x in pending], [102])
        reopened.delivered(102, pending[0]["hash"])
        reopened.observe([post(), post(102)], self.now, enabled=True)
        self.assertEqual(reopened.pending(self.now), [])

    def test_edit_is_requeued_but_unchanged_poll_is_not(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        db.observe([post(text="重置预告更正：明天到账")], self.now, enabled=True)
        item = db.pending(self.now)[0]
        self.assertIn("更正说明", json.loads(item["payload"])["markdown"]["content"])
        db.delivered(101, item["hash"])
        db.observe([post(text="重置预告更正：明天到账")], self.now, enabled=True)
        self.assertEqual(db.pending(self.now), [])

    def test_disable_send_never_enqueues_or_backfills_on_enable(self):
        db = self.store()
        db.observe([post()], self.now, enabled=False)
        db.observe([post(), post(102)], self.now, enabled=False)
        db.observe([post(), post(102)], self.now, enabled=True)
        self.assertEqual(db.pending(self.now), [])

    def test_unrelated_and_expired_posts_are_skipped(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        db.observe([post(), post(102, "新模型上线"), post(103, date="2026-10-01T04:00:00Z")], self.now, enabled=True)
        self.assertEqual(db.pending(self.now), [])

    def test_failure_is_retried_with_backoff_then_retained_as_dead(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        db.observe([post(102)], self.now, enabled=True)
        db.failed(102, self.now, max_attempts=2)
        self.assertEqual(db.pending(self.now), [])
        self.assertEqual(len(db.pending(self.now + 30)), 1)
        db.failed(102, self.now + 30, max_attempts=2)
        self.assertEqual(db.pending(self.now + 9999), [])
        self.assertEqual(db.counts()["dead"], 1)

    def test_old_cached_page_does_not_regress_cursor(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        with self.assertRaises(self.app.SourceError):
            db.observe([post(100)], self.now, enabled=True)
        self.assertEqual(db.cursor(), 101)

    def test_long_unicode_body_fits_byte_budget_keeps_permalink(self):
        payload = self.app.make_payload(post(text="重置🎁" * 2000), "codexreset", updated=False)
        content = payload["markdown"]["content"]
        self.assertLessEqual(len(content.encode("utf-8")), 4000)
        self.assertIn("https://t.me/codexreset/101", content)


    def test_untrusted_body_cannot_mention_everyone(self):
        content = self.app.make_payload(post(text="重置 <@all>"), "codexreset", False)["markdown"]["content"]
        self.assertNotIn("<@all>", content)

    def test_long_source_urls_do_not_overflow_payload(self):
        row = post()
        row["links"] = ["https://example.test/" + "字" * 700] * 3
        payload = self.app.make_payload(row, "codexreset", False)
        self.assertLessEqual(len(payload["markdown"]["content"].encode()), 4000)
        self.assertIn("https://t.me/codexreset/101", payload["markdown"]["content"])

    def test_pending_notification_expires_before_send(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        db.observe([post(102)], self.now, enabled=True)
        self.assertEqual(len(db.pending(self.now)), 1)
        self.assertEqual(db.pending(self.now + 49 * 3600), [])

    def test_queue_health_stays_failed_until_all_failures_recover(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        db.observe([post(102), post(103)], self.now, enabled=True)
        db.failed(102, self.now)
        other = next(x for x in db.pending(self.now) if x["id"] == 103)
        db.delivered(103, other["hash"])
        self.assertTrue(db.has_delivery_error())

    def test_retraction_of_sent_message_does_not_require_reset_keyword(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        db.observe([post(102)], self.now, enabled=True)
        item = db.pending(self.now)[0]
        db.delivered(102, item["hash"])
        db.observe([post(102, "更正：消息有误，请忽略，以官方通知为准。")], self.now, enabled=True)
        update = db.pending(self.now)
        self.assertEqual(len(update), 1)
        self.assertIn("请忽略", json.loads(update[0]["payload"])["markdown"]["content"])

    def test_dead_message_expires_instead_of_poisoning_readiness_forever(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        db.observe([post(102)], self.now, enabled=True)
        db.failed(102, self.now, max_attempts=1)
        self.assertTrue(db.has_delivery_error())
        self.assertEqual(db.pending(self.now + 49 * 3600), [])
        self.assertFalse(db.has_delivery_error())
        self.assertEqual(db.counts()["expired"], 1)

    def test_edit_scan_covers_recent_posts_outside_first_page(self):
        class Pages:
            def fetch(self, channel, before=None):
                if before is None:
                    return [post(102), post(103)]
                return [post(100, date="2026-10-01T04:00:00Z"), post(101, "更正：请忽略此前消息")]
        rows = self.app.fetch_since(Pages(), "codexreset", 103, now=self.now)
        self.assertIn("更正：请忽略此前消息", [row["text"] for row in rows])

    def test_sent_history_is_retained_across_disabled_observation(self):
        db = self.store()
        db.observe([post()], self.now, enabled=True)
        db.observe([post(102)], self.now, enabled=True)
        item = db.pending(self.now)[0]
        db.delivered(102, item["hash"])
        db.observe([post(102, "更正：暂时无法确认")], self.now, enabled=False)
        db.observe([post(102, "更正：此前消息有误，请忽略")], self.now, enabled=True)
        self.assertEqual(len(db.pending(self.now)), 1)

    def test_webhook_validation_does_not_echo_credential(self):
        for value in ("http://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=PRIVATE", "https://evil.test/send?key=PRIVATE"):
            with self.subTest(value=value), self.assertRaises(ValueError) as ctx:
                self.app.validate_webhook(value)
            self.assertNotIn("PRIVATE", str(ctx.exception))

    def test_wecom_ack_requires_zero_not_http_success(self):
        self.app.check_ack(b'{"errcode":0,"errmsg":"ok"}')
        for value in (b'{"errcode":40014,"errmsg":"PRIVATE"}', b'{}', b'not json', b'{"errcode":false}'):
            with self.subTest(value=value), self.assertRaises(self.app.DeliveryError) as ctx:
                self.app.check_ack(value)
            self.assertNotIn("PRIVATE", str(ctx.exception))

    def test_relay_request_uses_fixed_https_path_and_header_only(self):
        transport = self.app.Transport(relay_url="https://relay.example.com/codex-reset-feed",
                                       relay_authorization="Basic TEST_ONLY")
        request = transport.make_source_request("codexreset", before=123)
        self.assertEqual(request.full_url, "https://relay.example.com/codex-reset-feed?before=123")
        self.assertEqual(request.get_header("Authorization"), "Basic TEST_ONLY")
        self.assertNotIn("TEST_ONLY", request.full_url)
        with self.assertRaises(ValueError):
            transport.make_source_request("anotherchannel")

    def test_relay_rejects_insecure_url_and_missing_auth(self):
        for url, auth in (("http://relay.example.com/codex-reset-feed", "Basic TEST_ONLY"),
                          ("https://relay.example.com/codex-reset-feed?token=PRIVATE", "Basic TEST_ONLY"),
                          ("https://relay.example.com/codex-reset-feed", "")):
            with self.subTest(url=url), self.assertRaises(ValueError) as ctx:
                self.app.Transport(relay_url=url, relay_authorization=auth)
            self.assertNotIn("PRIVATE", str(ctx.exception))

    def test_wecom_request_never_carries_relay_authorization(self):
        transport = self.app.Transport(webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TEST_ONLY",
                                       relay_url="https://relay.example.com/codex-reset-feed",
                                       relay_authorization="Basic RELAY_ONLY")
        request = transport.make_wecom_request('{"msgtype":"markdown"}')
        self.assertIsNone(request.get_header("Authorization"))
        self.assertEqual(request.get_method(), "POST")

    def test_acceptance_notice_is_explicitly_marked_as_test(self):
        content = self.app.acceptance_payload([post()], "codexreset", "ACCEPTANCE-001")["markdown"]["content"]
        self.assertIn("验收测试", content)
        self.assertIn("不是新的额度重置公告", content)
        self.assertIn("ACCEPTANCE-001", content)
        self.assertIn("https://t.me/codexreset/101", content)


if __name__ == "__main__":
    unittest.main()
