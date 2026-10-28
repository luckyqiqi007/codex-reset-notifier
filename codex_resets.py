"""Optional, unauthenticated Codex Resets v1 status feed and durable event queue."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import re
import sqlite3
import time
from urllib.parse import urlparse
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler, Request

from message_content import classify_status

API_URL = 'https://codex-resets.com/api/v1/status'
MAX_AGE = 48 * 3600


class FeedError(ValueError):
    pass


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        parsed.astimezone(timezone(timedelta(hours=8)))  # Must be renderable in notification timezone.
        return parsed.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise FeedError('invalid_feed_time') from None


def x_id(url):
    try:
        p = urlparse(url)
        if p.scheme != 'https' or p.hostname not in ('x.com', 'twitter.com', 'www.x.com', 'www.twitter.com') or p.username or p.password:
            return None
        match = re.fullmatch(r'/(?:thsottiaux/status|i/web/status)/(\d+)/?', p.path)
        return match[1] if match else None
    except (ValueError, TypeError):
        return None


def telegram_keys(row):
    text = row.get('text', '')
    stage = classify_status(text)
    if stage not in ('announced', 'reported_complete'):
        return []
    kind = 'banked' if re.search(r'banked|重置卡|备用重置', text, re.I) else 'regular'
    return [f'x:{ident}:{kind}:{stage}' for url in row.get('links', []) if (ident := x_id(url))]


def init_ledger(db):
    db.execute('CREATE TABLE IF NOT EXISTS source_receipts (event_key TEXT, origin TEXT, PRIMARY KEY(event_key, origin))')


def record_receipts(db, keys, origin):
    for key in keys:
        db.execute('INSERT OR IGNORE INTO source_receipts VALUES (?,?)', (key, origin))


def seen_elsewhere(db, keys, origin):
    return any(db.execute('SELECT 1 FROM source_receipts WHERE event_key=? AND origin<>?', (key, origin)).fetchone() for key in keys)


def parse_status(document, now=None):
    now = time.time() if now is None else now
    try:
        meta, data = document['meta'], document['data']
        if meta['api_version'] != 'v1' or not -300 <= now - timestamp(meta['generated_at']) <= 600:
            raise FeedError('stale_or_unsupported_feed')
        result = []
        for field, stage in (('latest_reset', 'reported_complete'), ('scheduled_reset', 'announced')):
            item = data[field]
            if item is None:
                continue
            ident, kind, text = item['id'], item['reset_type'], item['text']
            if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', ident):
                raise FeedError('invalid_feed_id')
            if kind not in ('regular', 'banked') or not isinstance(text, str) or not text or len(text) > 16000:
                raise FeedError('invalid_feed_event')
            text.encode('utf-8')
            source = item['source']
            url = source.get('url', '')
            url.encode('utf-8')
            if source['type'] == 'x_post':
                if source.get('author') != 'thsottiaux' or not x_id(url) or x_id(url) != ident:
                    raise FeedError('invalid_feed_source')
            elif source['type'] == 'observed':
                if url and not x_id(url):
                    raise FeedError('invalid_feed_source')
            else:
                raise FeedError('invalid_feed_source')
            published = timestamp(item['announced_at'])
            if published > now:
                raise FeedError('future_feed_event')
            scheduled = item.get('scheduled_for') if stage == 'announced' else None
            if stage == 'announced' and item.get('status') != 'scheduled':
                raise FeedError('invalid_scheduled_status')
            if scheduled is not None:
                timestamp(scheduled)
            key = f'x:{x_id(url)}:{kind}:{stage}' if x_id(url) else f'observed:{ident}:{kind}:{stage}'
            result.append(dict(id=ident + ':' + stage, stage=stage, reset_type=kind, text=text,
                               published=published, scheduled_for=scheduled, source_type=source['type'],
                               source_url=url, event_key=key))
        return result  # active_watch is an AI forecast, never a reset notification.
    except FeedError:
        raise
    except (KeyError, TypeError, AttributeError, UnicodeError):
        raise FeedError('invalid_feed_schema') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise FeedError('feed_redirect_refused')


def fetch_status():
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        request = Request(API_URL, headers={'User-Agent': 'CodexResetNotifier/1.0', 'Accept': 'application/json'})
        with opener.open(request, timeout=15) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise FeedError('feed_too_large')
        return parse_status(json.loads(raw))
    except FeedError:
        raise
    except Exception as exc:
        raise FeedError('feed_request_' + type(exc).__name__) from None


def payload(event, now):
    def local(value):
        return datetime.fromtimestamp(value, timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
    label = '重置预告' if event['stage'] == 'announced' else '网站称已完成'
    kind = '重置卡' if event['reset_type'] == 'banked' else '常规重置'
    content = (f'**Codex 额度通知｜{label}（{kind}）**\n'
               f'公告/观测时间：{local(event["published"])}（北京时间）\n'
               f'本次发现时间：{local(now)}（北京时间）\n')
    if event['scheduled_for']:
        content += f'来源计划时间：{local(timestamp(event["scheduled_for"]))}（北京时间；到时不代表已完成）\n'
    elif event['stage'] == 'announced':
        content += '具体重置时间尚未公布。\n'
    body = event['text'].replace('<', '＜').replace('>', '＞')
    content += '\n' + body.encode()[:2300].decode('utf-8', 'ignore')
    content += '\n\n[数据来源：Codex Resets](https://codex-resets.com/zh-CN)'
    if event['source_url']:
        content += f' · [关联 X 原帖]({event["source_url"]})'
    content += '\n第三方网站整理/观测，未独立核验个人账号到账。'
    return json.dumps({'msgtype': 'markdown', 'markdown': {'content': content}}, ensure_ascii=False)


class Supplement:
    def __init__(self, db):
        self.db = db
        db.row_factory = sqlite3.Row
        init_ledger(db)
        db.executescript('''
            CREATE TABLE IF NOT EXISTS source_meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS website_events (
                id TEXT PRIMARY KEY, digest TEXT, payload TEXT, event_key TEXT, stage TEXT,
                published REAL, state TEXT, attempts INTEGER DEFAULT 0, next_retry REAL DEFAULT 0,
                ever_sent INTEGER DEFAULT 0, source_update INTEGER NOT NULL DEFAULT 0);
        ''')

    def observe(self, events, now, enabled):
        initialized = self.db.execute("SELECT 1 FROM source_meta WHERE key='codex_resets_initialized'").fetchone()
        with self.db:
            for event in events:
                digest = hashlib.sha256(json.dumps(event, sort_keys=True).encode()).hexdigest()
                old = self.db.execute('SELECT * FROM website_events WHERE id=?', (event['id'],)).fetchone()
                if old and old['digest'] == digest:
                    continue
                eligible = initialized and enabled and 0 <= now - event['published'] <= MAX_AGE
                self.db.execute('''INSERT INTO website_events(id,digest,payload,event_key,stage,published,state)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET digest=excluded.digest,
                    payload=excluded.payload,event_key=excluded.event_key,published=excluded.published,
                    state=excluded.state,attempts=0,next_retry=0,source_update=1''',
                    (event['id'], digest, payload(event, now), event['event_key'], event['stage'],
                     event['published'], 'pending' if eligible else 'ignored'))
            self.db.execute("INSERT OR IGNORE INTO source_meta VALUES('codex_resets_initialized','1')")

    def pending(self, now):
        result = []
        with self.db:
            self.db.execute("UPDATE website_events SET state='expired' WHERE state IN ('pending','dead') AND published<?", (now-MAX_AGE,))
            rows = self.db.execute("SELECT * FROM website_events WHERE state='pending' AND next_retry<=? ORDER BY published,id LIMIT 5", (now,)).fetchall()
            for row in rows:
                if not row['ever_sent'] and not row['source_update'] and seen_elsewhere(self.db, [row['event_key']], 'codex_resets'):
                    self.db.execute("UPDATE website_events SET state='deduped',ever_sent=1 WHERE id=?", (row['id'],))
                else:
                    result.append(dict(row))
        return result

    def delivered(self, item):
        with self.db:
            changed = self.db.execute("UPDATE website_events SET state='sent',ever_sent=1 WHERE id=? AND digest=?", (item['id'],item['digest'])).rowcount
            if changed:
                record_receipts(self.db, [item['event_key']], 'codex_resets')

    def failed(self, item, now):
        attempts = item['attempts'] + 1
        with self.db:
            self.db.execute('UPDATE website_events SET state=?,attempts=?,next_retry=? WHERE id=? AND digest=?',
                            ('dead' if attempts >= 8 else 'pending', attempts,
                             now + min(900, 30 * 2 ** (attempts-1)), item['id'], item['digest']))

    def has_error(self):
        return self.db.execute("SELECT 1 FROM website_events WHERE state='dead' OR (state='pending' AND attempts>0)").fetchone() is not None


def enabled_from_env(env):
    value = env.get('CODEX_RESETS_ENABLED', 'false').lower()
    if value not in ('true', 'false'):
        raise ValueError('invalid_codex_resets_enabled')
    return value == 'true'


def poll(supplements, now, enabled, fetch=None, clock=time.time):
    try:
        events = (fetch or fetch_status)()
        observed_at = max(now, clock())
        for event in events:
            payload(event, observed_at)  # Validate all content before writing any source queue.
    except FeedError as exc:
        return str(exc)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        return 'invalid_feed_content'
    for supplement in supplements:
        supplement.observe(events, observed_at, enabled)
    return None


def deliver(supplement, send, now, pause=lambda _: None, stopped=lambda: False):
    counts = {'sent': 0, 'failed': 0}
    for item in supplement.pending(now):
        if stopped():
            break
        try:
            send(item['payload'])
        except Exception:
            supplement.failed(item, now)
            counts['failed'] += 1
        else:
            supplement.delivered(item)
            counts['sent'] += 1
        pause(3.5)
    return counts
