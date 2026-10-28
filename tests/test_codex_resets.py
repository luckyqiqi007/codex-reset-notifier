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
    obj = dict(id=ident, reset_type='regular', announced_at=stamp(NOW-60), text='Codex usage limits have been reset.',
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
        self.observe(document(latest=event()))
        second=self.store.pending(NOW)[0]
        self.assertEqual(second['stage'],'reported_complete')
        self.store.delivered(second)
        self.observe(document(latest=event()))
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
                row['text']='又要重置了，预计北京时间15:00完成'
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
                site.observe(cr.parse_status(document(latest=event()),NOW),NOW,True)
                self.assertEqual(len(site.pending(NOW)),1)
            finally:store.close()

    def test_ambiguous_latest_never_sends_but_schedule_survives(self):
        self.observe(document())
        self.observe(document(latest=event('2115297093879472332', text='https://t.co/Fgy0dBAHsJ has landed'),
                              scheduled=event(status='scheduled', scheduled_for=stamp(NOW+3600),
                                              text='Lands by midnight PST.')))
        pending=self.store.pending(NOW)
        self.assertEqual([r['stage'] for r in pending], ['announced'])
        weak=self.db.execute("SELECT * FROM website_events WHERE id='2115297093879472332:reported_complete'").fetchone()
        self.assertEqual(weak['state'], 'rejected')
        self.assertNotIn('网站称已完成',weak['payload'])

    def test_completion_requires_explicit_unambiguous_reset_evidence(self):
        weak=['Done', 'https://t.co/Fgy0dBAHsJ has landed', 'Reset has not happened.',
              'If quotas are now live, enjoy.', 'Are quotas now live?',
              'Quotas will have been reset tomorrow.', 'https://example.com/reset It is all propagated.',
              'Today is a reset. The iCloud feature is fully propagated.',
              'Codex limits have not yet been reset.', 'My password has been reset.',
              'The demo database has been reset.', 'I have reset the counter.']
        for text in weak:
            with self.subTest(text=text):
                events=cr.parse_status(document(latest=event(text=text)),NOW)
                self.assertEqual(events[0]['stage'],'unconfirmed')
        for text in ['Codex usage limits have been reset.', 'Fresh quotas are now live.', '额度已经重置。']:
            with self.subTest(text=text):
                self.assertEqual(cr.parse_status(document(latest=event(text=text)),NOW)[0]['stage'],'reported_complete')

    def test_weak_edit_cancels_retry_and_later_completion_sends_once(self):
        self.observe(document()); self.observe(document(latest=event()))
        old=self.store.pending(NOW)[0];self.store.failed(old,NOW)
        self.observe(document(latest=event(text='Done')))
        self.assertEqual(self.store.pending(NOW+60),[])
        self.observe(document(latest=event()))
        self.assertEqual(cr.deliver(self.store,lambda _:None,NOW),{'sent':1,'failed':0})
        self.assertEqual(self.store.pending(NOW),[])

    def test_observed_source_has_same_evidence_gate(self):
        self.observe(document())
        self.observe(document(latest=event('observed-123',text='Done',source={'type':'observed'})))
        self.assertEqual(self.store.pending(NOW),[])

    def test_pending_rechecks_source_text_even_when_feed_unavailable(self):
        self.observe(document());self.observe(document(latest=event()))
        self.db.execute("UPDATE website_events SET source_text='Done'")
        self.assertEqual(cr.deliver(self.store,lambda _:self.fail('must not send'),NOW),{'sent':0,'failed':0})

    def test_legacy_completion_without_full_evidence_is_quarantined_on_restart(self):
        self.observe(document());self.observe(document(latest=event()))
        self.db.execute("UPDATE website_events SET source_text=NULL")
        reopened=cr.Supplement(self.db)
        self.assertEqual(reopened.pending(NOW),[])
        self.assertEqual(self.db.execute('SELECT state FROM website_events').fetchone()[0],'unverified')

    def test_fresh_full_evidence_restores_legacy_retry_without_resetting_backoff(self):
        self.observe(document());self.observe(document(latest=event()))
        self.store.failed(self.store.pending(NOW)[0],NOW)
        self.db.execute('UPDATE website_events SET source_text=NULL')
        self.store=cr.Supplement(self.db)
        self.assertEqual(self.store.pending(NOW+60),[])
        self.observe(document(latest=event()))
        self.assertEqual(self.store.pending(NOW),[])
        item=self.store.pending(NOW+30)[0]
        self.assertEqual(item['attempts'],1)
        self.assertEqual(item['next_retry'],NOW+30)

    def test_invalid_legacy_receipt_removed_but_sent_audit_and_other_receipts_kept(self):
        self.observe(document());self.observe(document(latest=event()))
        old=self.store.pending(NOW)[0];self.store.delivered(old)
        body=json.loads(old['payload'])
        body['markdown']['content']=body['markdown']['content'].replace('Codex usage limits have been reset.','https://t.co/Fgy0dBAHsJ has landed')
        self.db.execute('UPDATE website_events SET source_text=NULL,payload=?',(json.dumps(body),))
        cr.record_receipts(self.db,['x:456:regular:reported_complete'],'codex_resets')
        cr.record_receipts(self.db,[old['event_key']],'telegram')
        cr.Supplement(self.db)
        self.assertFalse(cr.seen_elsewhere(self.db,[old['event_key']],'telegram'))
        self.assertTrue(cr.seen_elsewhere(self.db,[old['event_key']],'codex_resets'))
        self.assertTrue(cr.seen_elsewhere(self.db,['x:456:regular:reported_complete'],'telegram'))
        self.assertEqual(self.db.execute('SELECT state FROM website_events').fetchone()[0],'sent')

    def test_unconfirmed_edit_removes_only_invalid_receipt(self):
        self.observe(document());self.observe(document(latest=event()))
        self.store.delivered(self.store.pending(NOW)[0])
        self.observe(document(latest=event(text='Done')))
        self.assertFalse(cr.seen_elsewhere(self.db,['x:123:regular:reported_complete'],'telegram'))
        self.assertEqual(self.store.pending(NOW),[])

if __name__=='__main__': unittest.main()
