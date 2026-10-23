"""Independent durable queues: partial success must not duplicate other channels."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notifier
from test_notifier import post


class FakeDestination:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []
        self.configured = True
        self.signing_enabled = False

    def send(self, payload):
        self.calls.append(json.loads(payload))
        if self.fail:
            raise notifier.DeliveryError("test_failure")


class MultiChannelTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(hasattr(notifier, "open_stores"), "independent destination queues are not implemented")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 1792728000

    def stores(self, names):
        stores = notifier.open_stores(self.root, "codexreset", names)
        for store in stores.values():
            self.addCleanup(store.close)
        return stores

    def observe(self, stores, rows):
        for store in stores.values():
            store.observe(rows, self.now, True)

    def test_partial_success_only_retries_failed_destination_after_restart(self):
        stores = self.stores(("wecom", "feishu"))
        self.observe(stores, [post()])
        self.observe(stores, [post(), post(102)])
        destinations = {"wecom": FakeDestination(), "feishu": FakeDestination(fail=True)}
        notifier.deliver_pending(stores, destinations, clock=lambda: self.now)
        self.assertEqual(stores["wecom"].counts()["sent"], 1)
        self.assertTrue(stores["feishu"].has_delivery_error())
        reopened = self.stores(("wecom", "feishu"))
        destinations["feishu"].fail = False
        notifier.deliver_pending(reopened, destinations, clock=lambda: self.now + 31)
        self.assertEqual(len(destinations["wecom"].calls), 1)
        self.assertEqual(len(destinations["feishu"].calls), 2)
        self.assertEqual(reopened["feishu"].counts()["sent"], 1)

    def test_new_destination_sets_baseline_without_replaying_existing_posts(self):
        stores = self.stores(("wecom",))
        self.observe(stores, [post()])
        self.observe(stores, [post(), post(102)])
        destinations = {"wecom": FakeDestination()}
        notifier.deliver_pending(stores, destinations, clock=lambda: self.now)
        expanded = self.stores(("wecom", "dingtalk"))
        self.observe(expanded, [post(), post(102)])
        self.assertEqual(expanded["dingtalk"].pending(self.now), [])
        self.observe(expanded, [post(), post(102), post(103)])
        self.assertEqual([x["id"] for x in expanded["dingtalk"].pending(self.now)], [103])

    def test_legacy_wecom_database_and_pending_queue_are_reused(self):
        legacy = notifier.Store(self.root / "state.db")
        legacy.observe([post()], self.now, True)
        legacy.observe([post(102)], self.now, True)
        legacy.close()
        stores = self.stores(("dingtalk", "wecom"))
        self.assertEqual([x["id"] for x in stores["wecom"].pending(self.now)], [102])
        self.assertIsNone(stores["dingtalk"].cursor())
        self.assertTrue((self.root / "state-dingtalk.db").exists())

    def test_disabled_delivery_updates_baseline_without_sending(self):
        stores = self.stores(("wecom", "feishu"))
        self.observe(stores, [post()])
        for store in stores.values():
            store.observe([post(), post(102)], self.now, False)
            store.observe([post(), post(102)], self.now, True)
            self.assertEqual(store.pending(self.now), [])

    def test_aggregate_health_exposes_each_channel_and_errors(self):
        stores = self.stores(("wecom", "feishu"))
        self.observe(stores, [post()])
        self.observe(stores, [post(102)])
        notifier.deliver_pending(stores, {"wecom": FakeDestination(), "feishu": FakeDestination(True)}, clock=lambda: self.now)
        state = notifier.delivery_status(stores)
        self.assertEqual(state["sent"], 1)
        self.assertEqual(state["pending"], 1)
        self.assertTrue(state["delivery_error"])
        self.assertFalse(state["destinations"]["wecom"]["delivery_error"])
        self.assertTrue(state["destinations"]["feishu"]["delivery_error"])

    def test_heartbeat_is_refreshed_for_each_delivery(self):
        stores = self.stores(("wecom", "feishu"))
        self.observe(stores, [post()])
        self.observe(stores, [post(102)])
        calls = []
        notifier.deliver_pending(stores, {name: FakeDestination() for name in stores},
                                 clock=lambda: self.now, heartbeat=lambda: calls.append(1))
        self.assertGreaterEqual(len(calls), 2)

    def test_lagging_reenabled_destination_does_not_block_current_destination(self):
        self.assertTrue(hasattr(notifier, "fetch_observations"), "per-channel source coverage is not implemented")
        stores = self.stores(("wecom", "dingtalk"))
        stores["wecom"].observe([post(399)], self.now, True)
        stores["dingtalk"].observe([post(1)], self.now, True)

        class Pages:
            calls = []

            def fetch(self, channel, before=None):
                self.calls.append(before)
                if before is None:
                    return [post(399), post(400)]
                return [post(before - 2, date="2026-10-01T04:00:00Z"),
                        post(before - 1, date="2026-10-01T04:00:00Z")]

        source = Pages()
        observations, errors = notifier.fetch_observations(source, "codexreset", stores, now=self.now)
        self.assertEqual(set(observations), {"wecom"})
        self.assertEqual(errors, {"dingtalk": "catchup_limit_reached"})
        self.assertLessEqual(len(source.calls), 10)
        stores["wecom"].observe(observations["wecom"], self.now, True)
        self.assertEqual([x["id"] for x in stores["wecom"].pending(self.now)], [400])
        self.assertEqual(stores["dingtalk"].cursor(), 1)

    def test_readiness_reports_channel_source_failure(self):
        import time
        health = self.root / "health.json"
        health.write_text(json.dumps({"heartbeat": time.time(), "source_success": time.time(),
                                      "source_errors": {"dingtalk": "catchup_limit_reached"}}))
        self.assertEqual(notifier.health_check(health, "ready"), 1)
        self.assertEqual(notifier.health_check(health, "live"), 0)

    def test_main_loop_preserves_partial_success_across_restart(self):
        for store in notifier.open_stores(self.root, "codexreset", ("wecom", "feishu")).values():
            store.observe([post()], self.now, True)
            store.close()
        config = self.root / "config.toml"
        config.write_text('[service]\ndelivery_enabled = true\nnotification_channels = ["wecom", "feishu"]\n'
                          + 'state_dir = '+json.dumps(str(self.root))+'\n')
        destinations = {"wecom": FakeDestination(), "feishu": FakeDestination(True)}
        rows = [post(100, date="2026-10-01T04:00:00Z"), post(101), post(102)]

        class OnePoll:
            finished = False

            def is_set(self):
                return self.finished

            def wait(self, seconds):
                if seconds >= 30:
                    self.finished = True

            def set(self):
                self.finished = True

        for attempt in range(2):
            destinations["feishu"].fail = attempt == 0
            with patch.dict("os.environ", {}, clear=True), \
                    patch.object(sys, "argv", ["notifier.py", "--config", str(config)]), \
                    patch.object(notifier, "STOP", OnePoll()), \
                    patch.object(notifier.signal, "signal"), \
                    patch.object(notifier.logging, "basicConfig"), \
                    patch.object(notifier.time, "time", return_value=self.now + attempt * 60), \
                    patch.object(notifier.Transport, "fetch", return_value=rows) as fetch, \
                    patch.object(notifier, "from_settings", return_value=destinations), \
                    self.assertLogs(notifier.LOG, level="INFO"):
                self.assertEqual(notifier.main(), 0)
                self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(destinations["wecom"].calls), 1)
        self.assertEqual(len(destinations["feishu"].calls), 2)
        health = json.loads((self.root / "health.json").read_text())
        self.assertEqual(health["sent"], 2)
        self.assertFalse(health["delivery_error"])

    def test_send_test_can_target_one_destination_without_sending_to_peers(self):
        import io
        config = self.root / "config.toml"
        config.write_text('[service]\nnotification_channels = ["wecom", "feishu"]\n')
        destinations = {"wecom": FakeDestination(), "feishu": FakeDestination()}
        output = io.StringIO()
        with patch.dict("os.environ", {}, clear=True), \
                patch.object(notifier.logging, "basicConfig"), \
                patch.object(sys, "argv", ["notifier.py", "--config", str(config), "--send-test", "OFFLINE-001", "--destination", "feishu"]), \
                patch.object(notifier.Transport, "fetch", return_value=[post()]), \
                patch.object(notifier, "from_settings", return_value=destinations), \
                patch("sys.stdout", output):
            self.assertEqual(notifier.main(), 0)
        self.assertEqual(destinations["wecom"].calls, [])
        self.assertEqual(len(destinations["feishu"].calls), 1)
        self.assertEqual(json.loads(output.getvalue())["destination_results"], {"feishu": True})


if __name__ == "__main__":
    unittest.main()
