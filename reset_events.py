"""Conservative, per-destination reset identity and successful-notice ledger.

Observation never acknowledges delivery. Call notice immediately before sending,
and delivered only after a successful response, inside the queue's transaction.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import sqlite3
import unicodedata
from urllib.parse import urlparse

from message_content import normalized_body


DAY_WINDOW = 48 * 3600
BEIJING = timezone(timedelta(hours=8))
DAY = re.compile(r'\bday\s*(\d{1,4})\b|第\s*(\d{1,4})\s*天', re.I)
CORRECTION = re.compile(r'更正|纠正|撤回|误报|\bcorrection\b|\bcorrected\b|^\s*update\s*:', re.I | re.M)
URL = re.compile(r'https?://\S+', re.I)


def scheduled_time(text, published, scheduled_for=None):
    """Only explicit Beijing/UTC times; relative dates use publication time."""
    try:
        if scheduled_for:
            parsed = datetime.fromisoformat(scheduled_for.replace('Z', '+00:00'))
            return parsed.timestamp() if parsed.tzinfo else None
        text = unicodedata.normalize('NFKC', text)
        match = re.search(r'北京时间([^\n。；;)]{0,100})', text)
        if match:
            fragment = match[1]
            clock = re.search(r'(\d{1,2}):(\d{2})(?::(\d{2}))?', fragment)
            if not clock:
                return None
            date = datetime.fromtimestamp(published, BEIJING)
            prefix = re.split(r'[\n。；;()]',text[max(0,match.start()-24):match.start()])[-1]
            context = prefix + '北京时间' + fragment
            absolute = re.search(r'(?:(\d{4})[年/-])?(\d{1,2})[月/-](\d{1,2})日?', context)
            if absolute:
                date = date.replace(year=int(absolute[1] or date.year), month=int(absolute[2]), day=int(absolute[3]))
            elif re.search(r'明日|明天', context):
                date += timedelta(days=1)
            elif '后天' in context:
                date += timedelta(days=2)
            hour = int(clock[1])
            if re.search(r'下午|晚上', fragment[:clock.start()]) and 1 <= hour <= 11:
                hour += 12
            elif re.search(r'凌晨|早上|上午', fragment[:clock.start()]) and hour == 12:
                hour = 0
            return date.replace(hour=hour, minute=int(clock[2]), second=int(clock[3] or 0), microsecond=0).timestamp()
        match = re.search(r'\b(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(?:UTC|GMT)\b', text, re.I)
        if match:
            if re.match(r'\s*[+\-−]',text[match.end():]):
                return None  # Numeric offsets are not plain UTC.
            date = datetime.fromtimestamp(published, timezone.utc)
            absolute = re.search(r'\b(\d{4})-(\d{2})-(\d{2})\b', text)
            if absolute:
                date = date.replace(year=int(absolute[1]), month=int(absolute[2]), day=int(absolute[3]))
            elif re.search(r'\btomorrow\b', text, re.I):
                date += timedelta(days=1)
            return date.replace(hour=int(match[1]), minute=int(match[2]), second=int(match[3] or 0), microsecond=0).timestamp()
    except (ValueError, TypeError, OverflowError, AttributeError):
        pass
    return None


def _x_ids(links):
    result = set()
    for link in links:
        try:
            url = urlparse(link)
            match = re.fullmatch(r'/(?:thsottiaux/status|i/web/status)/(\d+)/?', url.path)
            if (url.scheme == 'https' and url.hostname in ('x.com','twitter.com','www.x.com','www.twitter.com')
                    and not url.username and not url.password and match):
                result.add(match[1])
        except (ValueError, TypeError):
            continue
    return result


def _scope(text):
    """Keep explicit eligibility and quota multipliers, not ordinary prose."""
    text = URL.sub('', unicodedata.normalize('NFKC', text)).casefold()
    plans = set()
    for match in re.finditer(r'(?<![a-z0-9_])(plus|pro|business|enterprise|team|free|go)(?![a-z0-9_])', text):
        before, after = text[max(0,match.start()-24):match.start()], text[match.end():match.end()+24]
        context = before + after
        if not re.search(r'users?|subscribers?|plans?|only|except|exclud|用户|套餐|适用|✅|❌', context):
            continue
        suffix = re.match(r'\s*([✅❌])', after)
        prefix = re.search(r'([✅❌])\s*$', before)
        sign = (suffix or prefix)[1] if (suffix or prefix) else '+'
        if re.search(r'(?:except|excluding|not|不含|排除|不适用)\s*$', before):
            sign = '❌'
        if re.match(r'\s*(?:(?:users?|subscribers?|plans?)\s+)?(?:(?:are|is)\s+)?'
                    r'(?:excluded|ineligible|not\s+(?:eligible|included))\b|'
                    r'\s*(?:用户|套餐)?\s*(?:不适用|不支持|不包括|排除)',after):
            sign = '❌'
        plans.add((match[1], '-' if sign=='❌' else '+'))
    factors = set()
    for match in re.finditer(r'\b(\d+(?:\.\d+)?)\s*x\b|(\d+(?:\.\d+)?)\s*倍', text):
        context = text[max(0,match.start()-24):match.end()+24]
        if re.search(r'credits?|quotas?|limits?|额度|配额|用量', context):
            factors.add(match[1] or match[2])
    if not plans and not factors:
        return None
    return json.dumps({'plans':sorted(plans), 'only':bool(plans and re.search(r'\bonly\b|仅|只适用',text)),
                       'multipliers':sorted(factors)},sort_keys=True,ensure_ascii=False)


class EventLedger:
    def __init__(self, db):
        self.db = db
        db.row_factory = sqlite3.Row
        # executescript() commits a caller's open transaction; individual DDL
        # statements preserve ownership of the surrounding queue transaction.
        for statement in (
            '''CREATE TABLE IF NOT EXISTS reset_events (
                id INTEGER PRIMARY KEY, kind TEXT NOT NULL, day INTEGER,
                first_published REAL NOT NULL, last_published REAL NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS reset_event_aliases (
                event_id INTEGER NOT NULL, alias_type TEXT NOT NULL, value TEXT NOT NULL,
                PRIMARY KEY(event_id,alias_type,value))''',
            '''CREATE INDEX IF NOT EXISTS reset_event_alias_lookup ON reset_event_aliases(alias_type,value)''',
            '''CREATE TABLE IF NOT EXISTS reset_event_messages (
                origin TEXT NOT NULL, ident TEXT NOT NULL, version TEXT NOT NULL,
                event_id INTEGER NOT NULL, revision INTEGER NOT NULL, stage TEXT NOT NULL,
                published REAL NOT NULL, plan REAL, scope TEXT, plan_edit INTEGER NOT NULL,
                scope_edit INTEGER NOT NULL, body_hash TEXT NOT NULL, correction INTEGER NOT NULL,
                PRIMARY KEY(origin,ident))''',
            '''CREATE TABLE IF NOT EXISTS reset_event_receipts (
                id INTEGER PRIMARY KEY, event_id INTEGER NOT NULL, origin TEXT NOT NULL,
                ident TEXT NOT NULL, message_revision INTEGER NOT NULL, notice TEXT NOT NULL,
                revision TEXT NOT NULL, plan REAL, scope TEXT, stage TEXT,
                UNIQUE(origin,ident,message_revision))''',
            '''CREATE INDEX IF NOT EXISTS reset_event_receipt_lookup ON reset_event_receipts(event_id,notice)''',
            '''CREATE TABLE IF NOT EXISTS reset_event_plan_changes (
                event_id INTEGER NOT NULL, origin TEXT NOT NULL, ident TEXT NOT NULL,
                revision INTEGER NOT NULL, old_plan REAL NOT NULL, new_plan REAL NOT NULL,
                PRIMARY KEY(origin,ident,revision))''',
        ):
            db.execute(statement)
        if 'stage' not in {row[1] for row in db.execute('PRAGMA table_info(reset_event_receipts)')}:
            db.execute('ALTER TABLE reset_event_receipts ADD COLUMN stage TEXT')

    def _compatible(self, event, kind, day):
        return event['kind'] == kind and (day is None or event['day'] is None or day == event['day'])

    def _event(self, event_id):
        return self.db.execute('SELECT * FROM reset_events WHERE id=?',(event_id,)).fetchone()

    def _aliases(self, alias_type, value):
        return self.db.execute('''SELECT e.* FROM reset_events e JOIN reset_event_aliases a
            ON e.id=a.event_id WHERE a.alias_type=? AND a.value=?''',(alias_type,value)).fetchall()

    def _same_identity(self, events):
        """A list of links may discuss multiple resets, not assert equivalence."""
        events = list(events)
        if len(events)<=1:
            return True
        days = {event['day'] for event in events}-{None}
        if len(days)>1:
            return False
        if (days and all(event['day'] is not None for event in events)
                and max(e['first_published'] for e in events)-min(e['first_published'] for e in events)<=DAY_WINDOW):
            return True
        shared = None
        for event in events:
            plans = {r[0] for r in self.db.execute("SELECT value FROM reset_event_aliases WHERE event_id=? AND alias_type='plan'",(event['id'],))}
            shared = plans if shared is None else shared & plans
        return bool(shared)

    def _merge(self, event_ids):
        keep = min(event_ids)
        for other in sorted(set(event_ids)-{keep}):
            previous, incoming = self._event(keep), self._event(other)
            self.db.execute('''UPDATE reset_events SET first_published=?,last_published=?,day=? WHERE id=?''',
                            (min(previous['first_published'],incoming['first_published']),
                             max(previous['last_published'],incoming['last_published']),
                             previous['day'] if previous['day'] is not None else incoming['day'],keep))
            self.db.execute('''INSERT OR IGNORE INTO reset_event_aliases SELECT ?,alias_type,value
                FROM reset_event_aliases WHERE event_id=?''',(keep,other))
            self.db.execute('UPDATE reset_event_messages SET event_id=? WHERE event_id=?',(keep,other))
            self.db.execute('UPDATE reset_event_receipts SET event_id=? WHERE event_id=?',(keep,other))
            self.db.execute('UPDATE reset_event_plan_changes SET event_id=? WHERE event_id=?',(keep,other))
            self.db.execute('DELETE FROM reset_event_aliases WHERE event_id=?',(other,))
            self.db.execute('DELETE FROM reset_events WHERE id=?',(other,))
        return keep

    def remember(self, origin, ident, version, *, text, published, stage, kind='regular', links=(), scheduled_for=None):
        origin, ident, version = str(origin), str(ident), str(version)
        old = self.db.execute('SELECT * FROM reset_event_messages WHERE origin=? AND ident=?',(origin,ident)).fetchone()
        clean = URL.sub('', unicodedata.normalize('NFKC',text))
        days = {int(a or b) for a,b in DAY.findall(clean)}
        day = next(iter(days)) if len(days)==1 else None
        plan = scheduled_time(clean,published,scheduled_for) if stage in ('announced','correction') else None
        aliases = [('x',value) for value in _x_ids(links)]
        if plan is not None:
            aliases.append(('plan',str(plan)))
        chosen = []

        def add(candidates):
            for event in candidates:
                if not self._compatible(event,kind,day):
                    continue
                existing_days = {self._event(i)['day'] for i in chosen} - {None}
                if event['day'] is not None and existing_days and event['day'] not in existing_days:
                    continue
                if event['id'] not in chosen:
                    chosen.append(event['id'])

        if old:
            add([self._event(old['event_id'])])
        x_matches = {r['id']:r for typ,value in aliases if typ=='x' for r in self._aliases(typ,value)
                     if self._compatible(r,kind,day)}
        if self._same_identity(x_matches.values()):
            add(x_matches.values())
        if day is not None:
            candidates = self.db.execute('''SELECT * FROM reset_events WHERE kind=? AND day=?
                AND ABS(first_published-?)<=?''',(kind,day,published,DAY_WINDOW)).fetchall()
            if len(candidates)==1:
                add(candidates)
        if plan is not None:
            candidates = [r for r in self._aliases('plan',str(plan)) if self._compatible(r,kind,day)
                          and abs(r['first_published']-published)<=DAY_WINDOW]
            if len(candidates)==1:
                add(candidates)
        if chosen:
            event_id = self._merge(chosen)
            event = self._event(event_id)
            self.db.execute('''UPDATE reset_events SET day=?,first_published=?,last_published=? WHERE id=?''',
                            (day if day is not None else event['day'],min(published,event['first_published']),
                             max(published,event['last_published']),event_id))
        else:
            event_id = self.db.execute('INSERT INTO reset_events(kind,day,first_published,last_published) VALUES(?,?,?,?)',
                                       (kind,day,published,published)).lastrowid
        for alias_type,value in aliases:
            if alias_type=='x' and any(r['kind']==kind and r['id']!=event_id for r in self._aliases('x',value)):
                continue  # Do not poison an existing event's X alias with an ambiguous bridge.
            self.db.execute('INSERT OR IGNORE INTO reset_event_aliases VALUES(?,?,?)',(event_id,alias_type,value))
        # Re-observation can discover an alias bridge, but must not lose the
        # source's actual edit metadata before its pending version succeeds.
        if old and old['version']==version:
            self.db.execute('UPDATE reset_event_messages SET event_id=? WHERE origin=? AND ident=?',(event_id,origin,ident))
            return
        scope = _scope(clean)
        revision = old['revision']+1 if old else 1
        same_event = old is not None and old['event_id'] in chosen
        plan_edit = bool(same_event and old['plan'] is not None and plan is not None and old['plan']!=plan)
        scope_edit = bool(same_event and scope is not None and old['scope']!=scope)
        if plan_edit:
            self.db.execute('INSERT OR IGNORE INTO reset_event_plan_changes VALUES(?,?,?,?,?,?)',
                            (event_id,origin,ident,revision,old['plan'],plan))
        body_hash = hashlib.sha256(normalized_body(clean).encode()).hexdigest()
        self.db.execute('''INSERT OR REPLACE INTO reset_event_messages
            (origin,ident,version,event_id,revision,stage,published,plan,scope,plan_edit,scope_edit,body_hash,correction)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (origin,ident,version,event_id,revision,stage,published,plan,scope,int(plan_edit),int(scope_edit),body_hash,
             int(stage=='correction' or bool(CORRECTION.search(clean)))))

    def _message(self, origin, ident, version):
        return self.db.execute('SELECT * FROM reset_event_messages WHERE origin=? AND ident=? AND version=?',
                               (str(origin),str(ident),str(version))).fetchone()

    def _superseded_plan(self, event_id, old, current):
        changes = self.db.execute('SELECT old_plan,new_plan FROM reset_event_plan_changes WHERE event_id=?',(event_id,)).fetchall()
        pending, seen = [old], set()
        while pending:
            value = pending.pop()
            if value==current:
                return True
            if value in seen:
                continue
            seen.add(value)
            pending.extend(r['new_plan'] for r in changes if r['old_plan']==value)
        return False

    def _changed(self, row, receipts, field):
        value = row[field]
        if value is None:
            return False
        known = [r for r in receipts if r[field] is not None]
        if not known:
            return True
        latest = known[-1]
        if value==latest[field]:
            return False
        # A previously sent time arriving from a lagging mirror is stale.
        # A real edit by the source of the current plan can change it back.
        if row[field+'_edit'] and row['origin']==latest['origin'] and row['ident']==latest['ident']:
            return True
        if field=='plan' and self._superseded_plan(row['event_id'],value,latest[field]):
            return False  # An observed edit supersedes the old plan even if its send failed.
        return not any(r[field]==value for r in known)

    def notice(self, origin, ident, version):
        row = self._message(origin,ident,version)
        if row is None:
            return None
        receipts = self.db.execute('SELECT * FROM reset_event_receipts WHERE event_id=? ORDER BY id',(row['event_id'],)).fetchall()
        if any(r['origin']==row['origin'] and r['ident']==row['ident'] and r['message_revision']==row['revision'] for r in receipts):
            return None
        stages = {r['stage'] or r['notice'] for r in receipts}
        plan_changed = self._changed(row,receipts,'plan')
        scope_changed = self._changed(row,receipts,'scope')
        correction_revision = 'correction:'+row['body_hash']
        if row['stage']=='reported_complete' and 'reported_complete' not in stages:
            return 'reported_complete'
        if row['correction']:
            if not any(r['revision']==correction_revision for r in receipts) or plan_changed or scope_changed:
                return 'correction'
            return None
        if row['stage']=='announced' and 'reported_complete' in stages:
            if scope_changed and row['scope_edit']:
                return 'correction'
            if plan_changed and row['plan_edit']:
                return 'time_change'
            return None
        if row['stage']=='announced' and 'announced' not in stages:
            return 'announced'
        if row['stage'] in ('announced','reported_complete'):
            if scope_changed:
                return 'correction'
            if row['stage']=='announced' and plan_changed:
                return 'time_change'
            return None
        revision = 'unconfirmed:'+row['body_hash']
        return None if any(r['revision']==revision for r in receipts) else 'unconfirmed'

    def delivered(self, origin, ident, version):
        notice = self.notice(origin,ident,version)
        row = self._message(origin,ident,version)
        if notice is None or row is None:
            return
        revision = (notice+':'+row['body_hash']) if notice in ('correction','unconfirmed') else notice
        self.db.execute('''INSERT OR IGNORE INTO reset_event_receipts
            (event_id,origin,ident,message_revision,notice,revision,plan,scope,stage) VALUES(?,?,?,?,?,?,?,?,?)''',
            (row['event_id'],row['origin'],row['ident'],row['revision'],notice,revision,row['plan'],row['scope'],row['stage']))
