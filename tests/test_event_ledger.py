"""Event identity is independent of source post and notification stage."""
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reset_events import EventLedger, scheduled_time


def ts(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


NOW = ts('2026-10-28T04:00:00Z')
PLAN = '2026-10-28T07:00:00Z'
TG = '''🎁 Day 23：重置将于美西时间今日结束前生效
(预计北京时间下午约 15:00 前)
🎁 Day 23: Rest and Reset Landing by Midnight PST
• Limit Reset: Quotas will refresh today by midnight PST (~07:00 UTC)
🇺🇸 00:00 · 🇬🇧 08:00 · 🇨🇳 15:00'''
SITE_X = 'https://x.com/thsottiaux/status/2115273253711409208'
TG_X = 'https://x.com/thsottiaux/status/2115273064481218829?s=46'


class EventLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.ledger = EventLedger(self.db)

    def remember(self, origin='telegram', ident='67', version='v1', text=TG,
                 stage='announced', published=NOW, kind='regular', links=(TG_X,), **kwargs):
        with self.db:
            self.ledger.remember(origin, ident, version, text=text, published=published,
                                 stage=stage, kind=kind, links=links, **kwargs)

    def site(self, version='s1', **kwargs):
        values = dict(origin='codex_resets', ident='2115273253711409208:announced',
                      version=version, text='Lands by midnight PST.', links=(SITE_X,),
                      published=ts('2026-10-28T02:43:38Z'), scheduled_for=PLAN)
        values.update(kwargs)
        self.remember(**values)

    def notice(self, origin='telegram', ident='67', version='v1'):
        return self.ledger.notice(origin, ident, version)

    def ack(self, origin='telegram', ident='67', version='v1'):
        with self.db:
            self.ledger.delivered(origin, ident, version)

    def test_real_day23_different_posts_merge_by_explicit_schedule(self):
        self.site(); self.remember()
        self.assertEqual(self.notice('codex_resets', '2115273253711409208:announced', 's1'), 'announced')
        self.ack('codex_resets', '2115273253711409208:announced', 's1')
        self.assertIsNone(self.notice())

    def test_telegram_first_and_later_site_also_deduplicate(self):
        self.remember(); self.ack(); self.site()
        self.assertIsNone(self.notice('codex_resets', '2115273253711409208:announced', 's1'))

    def test_observation_and_failed_delivery_do_not_claim_success(self):
        self.site(); self.remember()
        self.assertEqual(self.notice(), 'announced')
        self.assertEqual(self.notice('codex_resets', '2115273253711409208:announced', 's1'), 'announced')
        self.ack()
        self.assertIsNone(self.notice('codex_resets', '2115273253711409208:announced', 's1'))

    def test_same_pending_batch_is_rechecked_after_first_ack(self):
        self.remember(); self.remember(ident='68', links=(), text='Day 23: reset planned today')
        self.assertEqual(self.notice(ident='68'), 'announced')
        self.ack()
        self.assertIsNone(self.notice(ident='68'))

    def test_completion_has_own_stage_and_survives_midnight(self):
        self.remember(); self.ack()
        self.remember(ident='68', stage='reported_complete', text='Day 23: Codex usage limits have been reset.',
                      links=(), published=ts('2026-10-28T17:00:00Z'))
        self.assertEqual(self.notice(ident='68'), 'reported_complete')
        self.ack(ident='68')
        self.remember(origin='codex_resets', ident='new-complete', stage='reported_complete',
                      text='Day 23: Fresh quotas are now live.', links=(), published=NOW+600)
        self.assertIsNone(self.notice('codex_resets','new-complete'))

    def test_completed_event_does_not_send_old_announcement(self):
        self.remember(ident='68', stage='reported_complete', text='Day 23: Codex quotas are now live.')
        self.ack(ident='68'); self.remember()
        self.assertIsNone(self.notice())

    def test_first_explicit_completion_keeps_completion_label_despite_update_prefix(self):
        self.remember();self.ack()
        self.remember(version='v2',stage='reported_complete',text='Update: Day 23 Codex usage limits have been reset.')
        self.assertEqual(self.notice(version='v2'),'reported_complete')

    def test_same_day_different_event_days_are_not_merged_even_at_same_time(self):
        self.remember(); self.ack()
        self.remember(ident='68', text=TG.replace('Day 23', 'Day 24'), links=())
        self.assertEqual(self.notice(ident='68'), 'announced')

    def test_reset_kinds_are_separate_even_with_same_link(self):
        self.remember(); self.ack()
        self.remember(ident='68',kind='banked')
        self.assertEqual(self.notice(ident='68'),'announced')

    def test_day_number_is_not_a_permanent_global_key(self):
        self.remember(); self.ack()
        self.remember(ident='68', links=(), text='Day 23: reset planned today', published=NOW+366*86400)
        self.assertEqual(self.notice(ident='68'), 'announced')

    def test_same_calendar_day_without_shared_evidence_is_not_merged(self):
        self.remember(text='Reset planned today.',links=());self.ack()
        self.remember(ident='68',text='Another reset planned today.',links=(),published=NOW+30)
        self.assertEqual(self.notice(ident='68'),'announced')

    def test_explicit_time_change_once_and_stale_source_cannot_revert(self):
        self.site();self.ack('codex_resets','2115273253711409208:announced','s1')
        self.site('s2',scheduled_for='2026-10-28T08:00:00Z')
        self.assertEqual(self.notice('codex_resets','2115273253711409208:announced','s2'),'time_change')
        self.ack('codex_resets','2115273253711409208:announced','s2')
        self.remember()
        self.assertIsNone(self.notice())
        self.remember(version='v2',text=TG.replace('15:00','16:00'))
        self.assertIsNone(self.notice(version='v2'))

    def test_time_change_failed_then_other_source_success_stops_retry(self):
        self.site();self.remember();self.ack()
        self.site('s2',scheduled_for='2026-10-28T08:00:00Z')
        self.assertEqual(self.notice('codex_resets','2115273253711409208:announced','s2'),'time_change')
        self.remember(version='v2',text=TG.replace('15:00','16:00'))
        self.ack(version='v2')
        self.assertIsNone(self.notice('codex_resets','2115273253711409208:announced','s2'))

    def test_failed_old_schedule_cannot_revert_successful_revised_schedule(self):
        self.site()  # Observed 15:00, but delivery failed: no acknowledgement.
        self.site('s2',scheduled_for='2026-10-28T08:00:00Z')
        self.ack('codex_resets','2115273253711409208:announced','s2')
        self.remember()
        self.assertIsNone(self.notice())

    def test_multiple_unsent_revisions_stay_obsolete_after_restart(self):
        self.site()
        self.site('s2',scheduled_for='2026-10-28T08:00:00Z')
        self.site('s3',scheduled_for='2026-10-28T09:00:00Z')
        self.ack('codex_resets','2115273253711409208:announced','s3')
        self.ledger=EventLedger(self.db)
        self.remember()
        self.assertIsNone(self.notice())
        self.remember(ident='68',version='v2',links=(),text=TG.replace('15:00','16:00'))
        self.assertIsNone(self.notice(ident='68',version='v2'))

    def test_same_source_can_genuinely_change_time_back_again(self):
        self.site();self.ack('codex_resets','2115273253711409208:announced','s1')
        self.site('s2',scheduled_for='2026-10-28T08:00:00Z')
        self.ack('codex_resets','2115273253711409208:announced','s2')
        self.site('s1')
        self.assertEqual(self.notice('codex_resets','2115273253711409208:announced','s1'),'time_change')
        self.ack('codex_resets','2115273253711409208:announced','s1')
        self.assertIsNone(self.notice('codex_resets','2115273253711409208:announced','s1'))

    def test_missing_time_does_not_override_known_time(self):
        self.remember();self.ack()
        self.remember(version='v2',text='Day 23: no exact timeframe announced.')
        self.assertIsNone(self.notice(version='v2'))

    def test_unknown_time_then_explicit_time_is_not_lost(self):
        self.remember(text='Day 23: reset planned today.');self.ack()
        self.remember(version='v2')
        self.assertEqual(self.notice(version='v2'),'time_change')

    def test_plain_rewording_is_suppressed_but_explicit_correction_is_sent(self):
        self.remember();self.ack()
        self.remember(version='v2',text='Day 23: another way to announce today reset.')
        self.assertIsNone(self.notice(version='v2'))
        self.remember(version='v3',text='Day 23 更正：消息有误，请忽略。',stage='correction')
        self.assertEqual(self.notice(version='v3'),'correction');self.ack(version='v3')
        self.assertIsNone(self.notice(version='v3'))

    def test_scope_and_multiplier_changes_are_corrections(self):
        self.remember();self.ack()
        self.remember(version='v2',text='Day 23: Only Plus users get 2x credits.',stage='announced')
        self.assertEqual(self.notice(version='v2'),'correction');self.ack(version='v2')
        self.remember(version='v3',text='Day 23: Only Plus users get 3x credits.',stage='announced')
        self.assertEqual(self.notice(version='v3'),'correction')

    def test_postfix_exclusion_is_a_scope_correction(self):
        self.remember(text='Day 23 reset planned today for Pro users.');self.ack()
        self.remember(version='v2',text='Day 23 reset planned today. Pro users are excluded.')
        self.assertEqual(self.notice(version='v2'),'correction')

    def test_new_scope_on_existing_announcement_still_corrects_after_completion(self):
        self.remember();self.ack()
        self.remember(origin='codex_resets',ident='completed',stage='reported_complete',
                      text='Day 23: Codex quotas are now live.',links=())
        self.ack('codex_resets','completed')
        self.remember(version='v2',text='Day 23: Only Plus users are eligible.')
        self.assertEqual(self.notice(version='v2'),'correction')

    def test_eligibility_symbols_are_not_presentation_noise(self):
        self.remember(text='Day 23: reset planned today. Plus ✅ Pro ❌');self.ack()
        self.remember(version='v2',text='Day 23: reset planned today. Plus ❌ Pro ✅')
        self.assertEqual(self.notice(version='v2'),'correction')

    def test_chinese_adjacent_plan_names_preserve_scope_changes(self):
        self.remember();self.ack()
        self.remember(version='v2',text='Day 23：又要重置了，仅适用Plus用户。')
        self.assertEqual(self.notice(version='v2'),'correction');self.ack(version='v2')
        self.remember(version='v3',text='Day 23：又要重置了，仅适用Pro用户。')
        self.assertEqual(self.notice(version='v3'),'correction')

    def test_positive_eligibility_symbol_is_not_a_new_scope(self):
        self.remember(text='Day 23: reset planned today for Plus users.');self.ack()
        self.remember(version='v2',text='Day 23: reset planned today for Plus ✅ users.')
        self.assertIsNone(self.notice(version='v2'))

    def test_first_successful_announcement_correction_covers_announcement_phase(self):
        self.remember(text='Day 23 更正：预计北京时间15:00前重置。',stage='announced');self.ack()
        self.remember(ident='68',links=(),text=TG)
        self.assertIsNone(self.notice(ident='68'))

    def test_actual_time_edit_after_completion_is_not_a_late_old_announcement(self):
        self.remember();self.ack()
        self.remember(ident='68',stage='reported_complete',text='Day 23: Codex quotas are now live.')
        self.ack(ident='68')
        self.remember(version='v2',text=TG.replace('15:00','16:00'))
        self.assertEqual(self.notice(version='v2'),'time_change')

    def test_delivered_does_not_commit_callers_transaction(self):
        self.remember()
        self.ledger.delivered('telegram','67','v1')
        self.db.rollback()
        self.assertEqual(self.notice(),'announced')

    def test_ambiguous_shared_schedule_is_not_enough_to_join_two_days(self):
        self.remember();self.ack()
        self.remember(ident='68',links=(),text=TG.replace('Day 23','Day 24'));self.ack(ident='68')
        self.site()
        self.assertEqual(self.notice('codex_resets','2115273253711409208:announced','s1'),'announced')

    def test_unconfirmed_text_is_never_promoted_by_event_identity(self):
        self.remember();self.ack()
        self.remember(version='v2',text='Day 23: has landed',stage='unconfirmed')
        self.assertEqual(self.notice(version='v2'),'unconfirmed')

    def test_stale_ack_cannot_ack_new_version(self):
        self.remember();self.remember(version='v2',text=TG.replace('15:00','16:00'))
        self.ack(version='v1')
        self.assertEqual(self.notice(version='v2'),'announced')

    def test_restart_same_version_preserves_time_edit_and_receipts(self):
        self.site();self.ack('codex_resets','2115273253711409208:announced','s1')
        self.site('s2',scheduled_for='2026-10-28T08:00:00Z')
        self.ledger=EventLedger(self.db)
        self.site('s2',scheduled_for='2026-10-28T08:00:00Z')
        self.assertEqual(self.notice('codex_resets','2115273253711409208:announced','s2'),'time_change')
        self.ack('codex_resets','2115273253711409208:announced','s2')
        self.ledger=EventLedger(self.db)
        self.assertIsNone(self.notice('codex_resets','2115273253711409208:announced','s2'))

    def test_bridge_merges_previous_alias_groups_and_success_receipts(self):
        self.remember(text='Day 23: reset planned today.',links=(TG_X,));self.ack()
        self.site()
        self.remember(version='v2')
        self.assertEqual(self.notice(version='v2'),'time_change')
        self.ack(version='v2')
        self.assertIsNone(self.notice('codex_resets','2115273253711409208:announced','s1'))

    def test_multiple_unrelated_x_links_cannot_merge_independent_completions(self):
        first='https://x.com/thsottiaux/status/101'
        second='https://x.com/thsottiaux/status/102'
        self.remember(ident='71',text='预计北京时间15:00重置。',links=(first,));self.ack(ident='71')
        self.remember(ident='72',text='预计北京时间18:00重置。',links=(second,));self.ack(ident='72')
        self.remember(ident='73',text='Reset plans are linked here.',links=(first,second))
        self.remember(ident='74',stage='reported_complete',text='Codex quotas are now live.',links=(first,))
        self.ack(ident='74')
        self.remember(ident='75',stage='reported_complete',text='Codex quotas are now live.',links=(second,))
        self.assertEqual(self.notice(ident='75'),'reported_complete')
        self.assertNotEqual(self.db.execute("SELECT event_id FROM reset_event_messages WHERE ident='71'").fetchone()[0],
                            self.db.execute("SELECT event_id FROM reset_event_messages WHERE ident='72'").fetchone()[0])

    def test_destinations_have_independent_success(self):
        self.remember();self.ack()
        other=sqlite3.connect(':memory:');self.addCleanup(other.close)
        ledger=EventLedger(other)
        ledger.remember('telegram','67','v1',text=TG,published=NOW,stage='announced',links=(TG_X,))
        self.assertEqual(ledger.notice('telegram','67','v1'),'announced')

    def test_constructor_does_not_commit_callers_transaction(self):
        self.db.execute('CREATE TABLE user_changes (value TEXT)')
        self.db.commit()
        self.db.execute("INSERT INTO user_changes VALUES ('pending')")
        EventLedger(self.db)
        self.db.rollback()
        self.assertEqual(self.db.execute('SELECT count(*) FROM user_changes').fetchone()[0],0)


class ScheduleTests(unittest.TestCase):
    def test_source_timestamp_anchors_chinese_today_and_tomorrow(self):
        self.assertEqual(scheduled_time(TG,NOW),ts(PLAN))
        self.assertEqual(scheduled_time('预计北京时间明日凌晨约 01:00 前',NOW),ts('2026-10-28T17:00:00Z'))

    def test_date_before_timezone_is_used(self):
        self.assertEqual(scheduled_time('明天北京时间15:00前重置',NOW),ts('2026-10-29T07:00:00Z'))
        self.assertEqual(scheduled_time('后天北京时间15:00前重置',NOW),ts('2026-10-30T07:00:00Z'))

    def test_numeric_offsets_are_not_silently_read_as_plain_utc(self):
        # Rejecting an unsupported offset is safer than inventing the wrong instant.
        for text in ['Reset at 15:00 UTC+08:00', 'Reset at 15:00 GMT+8',
                     'Reset at 15:00 UTC-7', 'Reset at 15:00 UTC+0800']:
            with self.subTest(text=text):
                self.assertIsNone(scheduled_time(text,NOW))

    def test_afternoon_12_hour_notation_and_explicit_date(self):
        self.assertEqual(scheduled_time('预计北京时间今天下午约 3:00 前',NOW),ts(PLAN))
        self.assertEqual(scheduled_time('北京时间2026年10月29日凌晨01:00',NOW),ts('2026-10-28T17:00:00Z'))

    def test_utc_and_explicit_api_timestamp(self):
        self.assertEqual(scheduled_time('Reset at 10:00 UTC for Pro users.',NOW),ts('2026-10-28T10:00:00Z'))
        self.assertEqual(scheduled_time('Tomorrow by midnight PST',NOW,PLAN),ts(PLAN))

    def test_do_not_guess_from_country_list_or_pst(self):
        self.assertIsNone(scheduled_time('🇨🇳 15:00, 🇺🇸 00:00',NOW))
        self.assertIsNone(scheduled_time('Lands by midnight PST.',NOW))
        self.assertIsNone(scheduled_time('具体时间未公布。',NOW))


if __name__=='__main__':
    unittest.main()
