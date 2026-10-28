"""A second source must preserve baseline, delivery isolation and event stages."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import codex_resets as cr

NOW = 1793156400
def stamp(seconds=NOW):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
def event(ident='123', **changes):
    obj = dict(id=ident, reset_type='regular', announced_at=stamp(NOW-60), text='Lands by midnight PST.',
               source=dict(type='x_post', author='thsottiaux', url='https://x.com/thsottiaux/status/'+ident))
    obj.update(changes)
    return obj
def document(latest=None, scheduled=None):
    return dict(data=dict(latest_reset=latest, scheduled_reset=scheduled, active_watch={'reset_chance_percent':99}, stats={}),
                meta=dict(api_version='v1', generated_at=stamp()))

class SupplementTests(unittest.TestCase):
    def setUp(self):
        import sqlite3
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.store = cr.Supplement(self.db)
    def observe(self, doc, enabled=True):
        self.store.observe(cr.parse_status(doc, NOW), NOW, enabled)
    def test_initial_baseline_and_watch_do_not_send(self):
        self.observe(document(latest=event()))
        self.assertEqual(self.store.pending(NOW), [])
        self.assertEqual(cr.parse_status(document(), NOW), [])
    def test_scheduled_never_completes_just_because_deadline_passed(self):
        self.observe(document())
        self.observe(document(scheduled=event(status='scheduled', scheduled_for=stamp(NOW-1))))
        item = self.store.pending(NOW)[0]
        self.assertEqual(item['stage'], 'announced')
        text=json.loads(item['payload'])['markdown']['content']
        self.assertIn('重置预告',text)
        self.assertIn('codex-resets.com',text)
        self.assertIn('不代表已完成',text)
    def test_announcement_and_completion_each_notify_once(self):
        self.observe(document())
        self.observe(document(scheduled=event(status='scheduled', scheduled_for=stamp(NOW+3600))))
        first=self.store.pending(NOW)[0]; self.store.delivered(first)
        self.observe(document(latest=event(text='Done')))
        second=self.store.pending(NOW)[0]
        self.assertEqual(second['stage'],'reported_complete')
        self.store.delivered(second)
        self.observe(document(latest=event(text='Done')))
        self.assertEqual(self.store.pending(NOW),[])
    def test_telegram_receipt_suppresses_same_stage_but_not_completion(self):
        self.observe(document())
        cr.record_receipts(self.db, ['x:123:regular:announced'], 'telegram')
        self.observe(document(scheduled=event(status='scheduled', scheduled_for=None)))
        self.assertEqual(self.store.pending(NOW),[])
        self.observe(document(latest=event()))
        self.assertEqual(len(self.store.pending(NOW)),1)
    def test_website_receipt_matches_telegram_tracking_urls(self):
        self.observe(document())
        self.observe(document(scheduled=event(status='scheduled', scheduled_for=None)))
        self.store.delivered(self.store.pending(NOW)[0])
        row={'text':'又要重置了，具体时间未公布', 'links':['https://twitter.com/thsottiaux/status/123?s=46']}
        keys=cr.telegram_keys(row)
        self.assertTrue(cr.seen_elsewhere(self.db, keys, 'telegram'))
        row['text']='14:10已经重置了'
        self.assertFalse(cr.seen_elsewhere(self.db, cr.telegram_keys(row), 'telegram'))
    def test_changed_schedule_still_notifies_after_cross_source_dedup(self):
        self.observe(document())
        cr.record_receipts(self.db, ['x:123:regular:announced'], 'telegram')
        self.observe(document(scheduled=event(status='scheduled', scheduled_for=None)))
        self.assertEqual(self.store.pending(NOW),[])
        self.observe(document(scheduled=event(status='scheduled', scheduled_for=stamp(NOW+3600))))
        self.assertEqual(len(self.store.pending(NOW)),1)
    def test_material_edit_to_baseline_is_not_hidden_by_other_source_receipt(self):
        self.observe(document(scheduled=event(status='scheduled',scheduled_for=None)))
        cr.record_receipts(self.db,['x:123:regular:announced'],'telegram')
        self.observe(document(scheduled=event(status='scheduled',scheduled_for=stamp(NOW+3600))))
        self.assertEqual(len(self.store.pending(NOW)),1)
    def test_disabled_baseline_is_not_replayed_when_enabled(self):
        self.observe(document())
        self.observe(document(latest=event()),False)
        self.observe(document(latest=event()),True)
        self.assertEqual(self.store.pending(NOW),[])
    def test_old_event_is_not_replayed(self):
        self.observe(document())
        self.observe(document(latest=event(announced_at=stamp(NOW-49*3600))))
        self.assertEqual(self.store.pending(NOW),[])
    def test_retry_survives_restart_and_does_not_record_success(self):
        self.observe(document()); self.observe(document(latest=event()))
        item=self.store.pending(NOW)[0]; self.store.failed(item,NOW)
        reopened=cr.Supplement(self.db)
        self.assertEqual(reopened.pending(NOW),[])
        self.assertEqual(len(reopened.pending(NOW+30)),1)
        self.assertFalse(cr.seen_elsewhere(self.db,[item['event_key']],'telegram'))
    def test_stale_invalid_or_unsafe_response_is_rejected(self):
        bads=[{},document(latest=event(source={'type':'x_post','author':'someone','url':'https://evil.example/'})),
              document(latest=event(announced_at='yesterday'))]
        stale=document();stale['meta']['generated_at']=stamp(NOW-3600);bads.append(stale)
        for d in bads:
            with self.subTest(d=d),self.assertRaises(cr.FeedError): cr.parse_status(d,NOW)

    def test_bad_unicode_and_unrenderable_time_are_feed_errors(self):
        for d in [document(latest=event(text='\ud800')),
                  document(scheduled=event(status='scheduled',scheduled_for='9999-12-31T23:59:59Z')),
                  document(latest=event(announced_at=stamp(NOW+60)))]:
            with self.assertRaises(cr.FeedError):cr.parse_status(d,NOW)

    def test_trailing_slash_source_url_has_same_identity(self):
        self.assertEqual(cr.x_id('https://x.com/thsottiaux/status/123/?s=46'),'123')

    def test_feed_failure_does_not_clear_pending_or_block_other_source(self):
        self.observe(document()); self.observe(document(latest=event()))
        def fail(): raise cr.FeedError('temporary_failure')
        self.assertEqual(cr.poll([self.store],NOW,True,fetch=fail),'temporary_failure')
        self.assertEqual(len(self.store.pending(NOW)),1)

    def test_event_published_during_fetch_is_queued_after_response(self):
        self.observe(document())
        events=cr.parse_status(document(latest=event(announced_at=stamp(NOW+1))),NOW+2)
        self.assertIsNone(cr.poll([self.store],NOW,True,fetch=lambda:events,clock=lambda:NOW+2))
        self.assertEqual(len(self.store.pending(NOW+3)),1)

    def test_failed_delivery_is_retried_without_success_receipt(self):
        self.observe(document()); self.observe(document(latest=event()))
        def fail(_): raise OSError('network failed')
        self.assertEqual(cr.deliver(self.store,fail,NOW),{'sent':0,'failed':1})
        sent=[]
        self.assertEqual(cr.deliver(self.store,sent.append,NOW+30),{'sent':1,'failed':0})
        self.assertEqual(len(sent),1)

    def test_telegram_queue_deduplicates_first_post_but_keeps_later_edits(self):
        import notifier
        with tempfile.TemporaryDirectory() as tmp:
            store=notifier.Store(Path(tmp)/'state.db')
            try:
                site=cr.Supplement(store.db)
                baseline={'id':1,'date':stamp(NOW-100),'text':'配额已重置','links':[]}
                store.observe([baseline],NOW,True)
                site.observe([],NOW,True)
                site.observe(cr.parse_status(document(scheduled=event(status='scheduled',scheduled_for=None)),NOW),NOW,True)
                site.delivered(site.pending(NOW)[0])
                row={'id':2,'date':stamp(NOW-30),'text':'又要重置了，具体时间未公布','links':['https://x.com/thsottiaux/status/123']}
                store.observe([baseline,row],NOW,True)
                self.assertEqual(store.pending(NOW),[])
                row['text']='又要重置了，预计15:00完成'
                store.observe([row],NOW,True)
                self.assertEqual(len(store.pending(NOW)),1)
                store.delivered(2,store.pending(NOW)[0]['hash'])
                row['text']='14:10已经重置了'
                store.observe([row],NOW,True)
                self.assertEqual(len(store.pending(NOW)),1)
                # A website completion already sent must also dedup a TG stage transition.
                cr.record_receipts(store.db,['x:123:regular:reported_complete'],'codex_resets')
                self.assertEqual(store.pending(NOW),[])
                row['text']='14:10已经重置了，仅适用Plus用户'
                store.observe([row],NOW,True)
                self.assertEqual(len(store.pending(NOW)),1)
            finally: store.close()

    def test_restart_does_not_claim_failed_completion_was_delivered(self):
        import notifier
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.db'
            store=notifier.Store(path)
            first={'id':1,'date':stamp(NOW-100),'text':'配额已重置','links':[]}
            row={'id':2,'date':stamp(NOW-30),'text':'又要重置了，具体时间未公布','links':['https://x.com/thsottiaux/status/123']}
            store.observe([first],NOW,True);store.observe([row],NOW,True)
            item=store.pending(NOW)[0];store.delivered(2,item['hash'])
            row['text']='14:10已经重置了';store.observe([row],NOW,True);store.failed(2,NOW)
            store.close()
            store=notifier.Store(path)
            try:
                site=cr.Supplement(store.db);site.observe([],NOW,True)
                site.observe(cr.parse_status(document(latest=event(text='Done')),NOW),NOW,True)
                self.assertEqual(len(site.pending(NOW)),1)
            finally:store.close()

if __name__=='__main__': unittest.main()
