"""Real cross-source reset announcements represent a single event."""
import json
import hashlib
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import notifier
import codex_resets as cr

NOW = datetime(2026,10,28,4,14,tzinfo=timezone.utc).timestamp()
TG_TEXT = '''🎁 Day 23：重置将于美西时间今日结束前生效
（预计北京时间下午约 15:00 前）
────────────
🎁 Day 23: Rest and Reset Landing by Midnight PST
• Limit Reset: Quotas will refresh today by midnight PST (~07:00 UTC)
🇺🇸 00:00 · 🇬🇧 08:00
🇨🇳 15:00
Source
Follow @codexreset'''

def tg(text=TG_TEXT, ident=67):
    return dict(id=ident,date='2026-10-28T04:13:17+00:00',text=text,
                links=['https://x.com/thsottiaux/status/2115273064481218829?s=46','https://t.me/codexreset'])

def website(complete=False, schedule='2026-10-28T07:00:00Z'):
    item=dict(id='2115273253711409208',reset_type='regular',announced_at='2026-10-28T02:43:38Z',
              text='Codex usage limits have been reset.' if complete else 'Lands by midnight PST.',
              source=dict(type='x_post',author='thsottiaux',url='https://x.com/thsottiaux/status/2115273253711409208'))
    if not complete:item.update(status='scheduled',scheduled_for=schedule)
    doc=dict(meta=dict(api_version='v1',generated_at=datetime.fromtimestamp(NOW,timezone.utc).isoformat()),
             data=dict(latest_reset=item if complete else None,scheduled_reset=None if complete else item))
    return cr.parse_status(doc,NOW)

class EventDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'state.db'
        self.store=notifier.Store(self.path);self.addCleanup(self.store.close)
        self.site=cr.Supplement(self.store.db)
        self.store.observe([dict(id=60,date='2026-10-20T00:00:00Z',text='Old quota announcement',links=[])],NOW,True)
        self.site.observe([],NOW,True)

    def test_real_website_reply_and_telegram_main_post_announce_once(self):
        self.site.observe(website(),NOW,True)
        messages=[]
        self.assertEqual(cr.deliver(self.site,messages.append,NOW)['sent'],1)
        self.store.observe([tg()],NOW,True)
        self.assertEqual(self.store.pending(NOW),[])
        self.assertEqual(len(messages),1)

    def test_telegram_first_also_suppresses_website_reply(self):
        self.store.observe([tg()],NOW,True)
        item=self.store.pending(NOW)[0];self.store.delivered(item['id'],item['hash'])
        self.site.observe(website(),NOW,True)
        self.assertEqual(self.site.pending(NOW),[])

    def test_completion_is_separate_and_cross_source_once(self):
        self.site.observe(website(),NOW,True)
        cr.deliver(self.site,lambda _:None,NOW)
        self.store.observe([tg()],NOW,True);self.store.pending(NOW)
        self.site.observe(website(complete=True),NOW,True)
        self.assertEqual(cr.deliver(self.site,lambda _:None,NOW)['sent'],1)
        self.store.observe([tg(TG_TEXT+'\n14:10 已经重置了。Fresh quotas are now live.')],NOW,True)
        self.assertEqual(self.store.pending(NOW),[])

    def test_failure_does_not_suppress_other_source_and_success_stops_retry(self):
        self.site.observe(website(),NOW,True)
        item=self.site.pending(NOW)[0];self.site.failed(item,NOW)
        self.store.observe([tg()],NOW,True)
        item=self.store.pending(NOW)[0];self.store.delivered(item['id'],item['hash'])
        self.assertEqual(self.site.pending(NOW+31),[])

    def test_schedule_change_notifies_once_and_stale_schedule_stays_silent(self):
        self.site.observe(website(),NOW,True);cr.deliver(self.site,lambda _:None,NOW)
        self.site.observe(website(schedule='2026-10-28T08:00:00Z'),NOW,True)
        out=[];self.assertEqual(cr.deliver(self.site,out.append,NOW)['sent'],1)
        self.assertIn('时间变更',json.loads(out[0])['markdown']['content'])
        self.store.observe([tg()],NOW,True)
        self.assertEqual(self.store.pending(NOW),[])
        self.store.observe([tg(TG_TEXT.replace('15:00','16:00').replace('07:00 UTC','08:00 UTC'))],NOW,True)
        self.assertEqual(self.store.pending(NOW),[])

    def test_restart_preserves_event_success(self):
        self.site.observe(website(),NOW,True);cr.deliver(self.site,lambda _:None,NOW)
        reopened=notifier.Store(self.path)
        try:
            cr.Supplement(reopened.db)
            reopened.observe([tg()],NOW,True)
            self.assertEqual(reopened.pending(NOW),[])
        finally:reopened.close()

    def test_same_pending_batch_rechecks_before_each_send(self):
        self.store.observe([tg(),tg(ident=68)],NOW,True)
        candidates=self.store.pending(NOW);sent=[]
        for candidate in candidates:
            payload=self.store.prepare_delivery(candidate)
            if payload is not None:
                sent.append(payload);self.store.delivered(candidate['id'],candidate['hash'])
        self.assertEqual(len(sent),1)

    def test_same_cycle_prefers_earlier_website_announcement(self):
        self.store.observe([tg()],NOW,True)
        self.site.observe(website(),NOW,True)
        sent=[]
        counts=cr.deliver_events(self.store,self.site,sent.append,clock=lambda:NOW)
        self.assertEqual(counts['codex_resets']['sent'],1)
        self.assertEqual(counts['telegram']['sent'],0)
        self.assertEqual(len(sent),1)
        self.assertIn('数据来源：Codex Resets',sent[0])

    def test_first_source_does_not_wait_for_other_source(self):
        self.site.observe(website(),NOW,True)
        sent=[]
        counts=cr.deliver_events(self.store,self.site,sent.append,clock=lambda:NOW)
        self.assertEqual(counts['codex_resets']['sent'],1)
        self.store.observe([tg()],NOW+600,True)
        counts=cr.deliver_events(self.store,self.site,sent.append,clock=lambda:NOW+600)
        self.assertEqual(counts['telegram']['sent'],0)
        self.assertEqual(len(sent),1)

    def test_weak_completion_retraction_does_not_block_later_valid_other_source(self):
        self.site.observe(website(),NOW,True);cr.deliver(self.site,lambda _:None,NOW)
        self.store.observe([tg()],NOW,True);self.store.pending(NOW)
        self.site.observe(website(complete=True),NOW,True);cr.deliver(self.site,lambda _:None,NOW)
        weak=website(complete=True)[0];weak.update(text='Done',stage='unconfirmed')
        self.site.observe([weak],NOW,True)
        self.store.observe([tg(TG_TEXT+'\n额度已经重置。Fresh quotas are now live.')],NOW,True)
        self.assertEqual(len(self.store.pending(NOW)),1)

    def test_old_writer_changed_plan_and_failed_does_not_lose_retry_on_upgrade(self):
        self.site.observe(website(),NOW,True);cr.deliver(self.site,lambda _:None,NOW)
        changed=website(schedule='2026-10-28T08:00:00Z')[0]
        digest=hashlib.sha256(json.dumps(changed,sort_keys=True).encode()).hexdigest()
        # The previous release updates its own columns, leaving the new snapshot stale.
        with self.store.db:
            self.store.db.execute('''UPDATE website_events SET digest=?,payload=?,source_text=?,
                state='pending',attempts=1,next_retry=?,source_update=1 WHERE id=?''',
                (digest,cr.payload(changed,NOW),changed['text'],NOW+30,changed['id']))
        upgraded=cr.Supplement(self.store.db)
        self.assertEqual(upgraded.pending(NOW),[])
        pending=upgraded.pending(NOW+30)
        self.assertEqual(len(pending),1)
        self.assertEqual(pending[0]['attempts'],1)
        self.assertIn('16:00:00',pending[0]['payload'])
        upgraded.observe([changed],NOW,True)
        self.assertEqual(len(upgraded.pending(NOW+30)),1)
        self.assertEqual(self.store.db.execute('SELECT event_snapshot FROM website_events WHERE id=?',(changed['id'],)).fetchone()[0],
                         json.dumps(changed,ensure_ascii=False,sort_keys=True))

    def test_explicit_website_retraction_notifies_and_reopens_valid_completion(self):
        self.site.observe(website(),NOW,True);cr.deliver(self.site,lambda _:None,NOW)
        self.site.observe(website(complete=True),NOW,True);cr.deliver(self.site,lambda _:None,NOW)
        item=dict(id='2115273253711409208',reset_type='regular',announced_at='2026-10-28T02:43:38Z',
                  text='Correction: Codex usage limits have not been reset. Disregard the earlier completion.',
                  source=dict(type='x_post',author='thsottiaux',url='https://x.com/thsottiaux/status/2115273253711409208'))
        document=dict(meta=dict(api_version='v1',generated_at=datetime.fromtimestamp(NOW,timezone.utc).isoformat()),
                      data=dict(latest_reset=item,scheduled_reset=None))
        self.site.observe(cr.parse_status(document,NOW),NOW,True)
        sent=[];self.assertEqual(cr.deliver(self.site,sent.append,NOW)['sent'],1)
        self.assertIn('更正说明',sent[0])
        self.site.observe(website(complete=True),NOW,True)
        self.assertEqual(cr.deliver(self.site,sent.append,NOW)['sent'],1)

    def test_telegram_retraction_releases_its_completion_for_other_source(self):
        self.store.observe([tg(TG_TEXT+'\n额度已经重置。Fresh quotas are now live.')],NOW,True)
        item=self.store.pending(NOW)[0];self.store.delivered(item['id'],item['hash'])
        retraction=tg('Day 23 更正：此前消息有误，请忽略；额度尚未重置。');retraction['links']=[]
        self.store.observe([retraction],NOW,True)
        item=self.store.pending(NOW)[0];self.store.delivered(item['id'],item['hash'])
        events=website(complete=True)
        events[0].update(id='2115273064481218829:reported_complete',source_url='https://x.com/thsottiaux/status/2115273064481218829',
                         event_key='x:2115273064481218829:regular:reported_complete')
        self.site.observe(events,NOW,True)
        self.assertEqual(len(self.site.pending(NOW)),1)

    def test_old_writer_downgrade_invalidates_event_receipt_after_upgrade(self):
        self.site.observe(website(),NOW,True);cr.deliver(self.site,lambda _:None,NOW)
        self.store.observe([tg()],NOW,True);self.store.pending(NOW)
        self.site.observe(website(complete=True),NOW,True);cr.deliver(self.site,lambda _:None,NOW)
        weak=website(complete=True)[0];weak.update(text='Done',stage='unconfirmed')
        digest=hashlib.sha256(json.dumps(weak,sort_keys=True).encode()).hexdigest()
        with self.store.db:
            self.store.db.execute("UPDATE website_events SET digest=?,payload=?,source_text=?,stage='unconfirmed',state='rejected' WHERE id=?",
                                  (digest,cr.payload(weak,NOW),'Done',weak['id']))
            self.store.db.execute("DELETE FROM source_receipts WHERE origin='codex_resets' AND event_key=?",(weak['event_key'],))
        self.site=cr.Supplement(self.store.db)
        self.site.observe([weak],NOW,True)
        self.store.observe([tg(TG_TEXT+'\n额度已经重置。Fresh quotas are now live.')],NOW,True)
        self.assertEqual(len(self.store.pending(NOW)),1)

if __name__=='__main__':unittest.main()
