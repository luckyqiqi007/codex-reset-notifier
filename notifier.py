"""Public Telegram announcements to configurable notification channels. Python 3.12+."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import logging
import os
import codex_resets
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

from configuration import ConfigError, load_config
from message_content import (STATUS_LABELS, classify_status, clean_body, fingerprint,
                             normalized_links, previous_row, raw_fingerprint)
from destinations import (PROVIDERS, DeliveryError, NoRedirect, check_ack, from_settings,
                          validate_webhook, validated_url)

LOG = logging.getLogger("codex-reset-notifier")
STOP = threading.Event()
KEYWORDS = re.compile(r"重置|额度|配额|\breset\b|\bresets\b|\bquota\b|\blimits?\b|\bcredits?\b", re.I)
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "wbr"}


class SourceError(Exception):
    pass


class ChannelParser(HTMLParser):
    def __init__(self, channel):
        super().__init__(convert_charrefs=True)
        self.channel = channel
        self.depth = 0
        self.current = None
        self.root_depth = None
        self.text_depth = None
        self.rows = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "").split()
        if tag not in VOID_TAGS:
            self.depth += 1
        if "tgme_widget_message" in classes:
            match = re.fullmatch(re.escape(self.channel) + r"/(\d+)", attrs.get("data-post", ""))
            if match:
                self.current = {"id": int(match[1]), "text": "", "date": "", "links": []}
                self.root_depth = self.depth
        if self.current is None:
            return
        if "tgme_widget_message_text" in classes:
            self.text_depth = self.depth
        if tag == "time" and attrs.get("datetime"):
            self.current["date"] = attrs["datetime"]
        if self.text_depth is not None:
            if tag == "br":
                self.current["text"] += "\n"
            elif tag == "img":
                self.current["text"] += attrs.get("alt", "")
            elif tag == "a":
                url = attrs.get("href", "")
                parsed = urlparse(url)
                if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
                    if url not in self.current["links"] and len(url) < 800:
                        self.current["links"].append(url)

    def handle_endtag(self, tag):
        if tag in VOID_TAGS:
            return
        if self.current is not None:
            if self.depth == self.text_depth:
                self.text_depth = None
            if self.depth == self.root_depth:
                self.current["text"] = "\n".join(line.strip() for line in self.current["text"].splitlines()).strip()
                if not self.current["date"]:
                    raise SourceError("message_timestamp_missing")
                self.rows.append(self.current)
                self.current = None
                self.root_depth = None
        self.depth = max(0, self.depth - 1)

    def handle_data(self, value):
        if self.current is not None and self.text_depth is not None:
            self.current["text"] += value


def parse_page(html, channel):
    parser = ChannelParser(channel)
    parser.feed(html)
    parser.close()
    if not parser.rows or parser.current is not None:
        raise SourceError("channel_page_missing_or_incomplete")
    return sorted(parser.rows, key=lambda row: row["id"])


def message_time(row):
    try:
        value = datetime.fromisoformat(row["date"].replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("timezone missing")
        return value
    except (ValueError, KeyError):
        raise SourceError("invalid_message_timestamp") from None


def make_payload(row, channel, updated=False, observed_at=None):
    zone = timezone(timedelta(hours=8))
    stamp = message_time(row).astimezone(zone).strftime("%Y-%m-%d %H:%M:%S")
    observed = (datetime.fromtimestamp(observed_at, zone).strftime("%Y-%m-%d %H:%M:%S")
                if observed_at is not None else "未记录")
    status = STATUS_LABELS[classify_status(row["text"])]
    update = "（内容更新）" if updated else ""
    head = (f"**Codex 额度通知｜{status}{update}**\n"
            f"频道发布时间：{stamp}（北京时间）\n"
            f"本次发现时间：{observed}（北京时间）\n\n")
    tail = f"\n\n[频道原帖](https://t.me/{channel}/{row['id']})"
    for index, url in enumerate(normalized_links(row["links"], channel)[:3], 1):
        safe_url = url.replace("(", "%28").replace(")", "%29").replace("\n", "").replace("\r", "")
        link = f" · [来源{index}]({safe_url})"
        if len((tail + link).encode("utf-8")) <= 1500:
            tail += link
    tail += "\n频道消息转发，未独立核验账号到账。"
    body = clean_body(row["text"]).replace("<", "＜").replace(">", "＞")
    budget = 4000 - len((head + tail).encode("utf-8"))
    if len(body.encode("utf-8")) > budget:
        body = body.encode("utf-8")[:max(0, budget - 30)].decode("utf-8", "ignore") + "\n…详见原帖"
    return {"msgtype": "markdown", "markdown": {"content": head + body + tail}}


def acceptance_payload(rows, channel, test_id):
    latest = max(row["id"] for row in rows)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    content = (f"**Codex 通知服务｜验收测试**\n验收编号：{test_id}\n时间：{stamp}（北京时间）\n\n"
               f"已成功读取 {len(rows)} 条公开频道消息，此消息用于验证所选通知渠道。\n"
               "这是部署验收通知，**不是新的额度重置公告**。\n\n"
               f"[查看最新频道原帖](https://t.me/{channel}/{latest})")
    return {"msgtype": "markdown", "markdown": {"content": content}}


class Store:
    def __init__(self, path, channel="codexreset", max_age_hours=48):
        self.db = sqlite3.connect(path, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.channel = channel
        self.max_age = max_age_hours * 3600
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS posts (
                id INTEGER PRIMARY KEY, hash TEXT NOT NULL, payload TEXT NOT NULL,
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_retry REAL NOT NULL DEFAULT 0, published REAL NOT NULL,
                ever_sent INTEGER NOT NULL DEFAULT 0
            );
        """)
        old_channel = self.db.execute("SELECT value FROM meta WHERE key='channel'").fetchone()
        if old_channel and old_channel[0] != channel:
            self.db.close()
            raise ValueError("state_channel_mismatch")
        columns = {item[1] for item in self.db.execute("PRAGMA table_info(posts)")}
        with self.db:
            for name in ("semantic_hash", "row_snapshot"):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE posts ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
            if 'source_update' not in columns:
                self.db.execute('ALTER TABLE posts ADD COLUMN source_update INTEGER NOT NULL DEFAULT 0')
            codex_resets.init_ledger(self.db)
            for item in self.db.execute("SELECT * FROM posts WHERE state='sent'"):
                codex_resets.record_receipts(self.db, codex_resets.telegram_keys(previous_row(dict(item)) or {}), "telegram")

    def close(self):
        self.db.close()

    def cursor(self):
        row = self.db.execute("SELECT value FROM meta WHERE key='cursor'").fetchone()
        return int(row[0]) if row else None

    def observe(self, rows, now, enabled):
        if not rows:
            raise SourceError("empty_observation")
        cursor = self.cursor()
        latest = max(row["id"] for row in rows)
        if cursor is not None and latest < cursor:
            raise SourceError("source_older_than_checkpoint")
        with self.db:
            for row in rows:
                published = message_time(row).timestamp()
                digest = raw_fingerprint(row)
                meaning = fingerprint(row, self.channel)
                snapshot = json.dumps(row, ensure_ascii=False, sort_keys=True)
                old = self.db.execute("SELECT * FROM posts WHERE id=?", (row["id"],)).fetchone()
                if old:
                    prior = previous_row(old)
                    same_raw = old["hash"] == digest
                    same_meaning = prior is not None and fingerprint(prior, self.channel) == meaning
                    if same_raw or same_meaning:
                        # Observe formatting changes without changing delivery state,
                        # payload, retries, or a previously sent message's identity.
                        if old["row_snapshot"] != snapshot or old["semantic_hash"] != meaning or not same_raw:
                            self.db.execute("UPDATE posts SET hash=?,semantic_hash=?,row_snapshot=? WHERE id=?",
                                            (digest, meaning, snapshot, row["id"]))
                        if not same_raw:
                            LOG.info("source_edit_suppressed message_id=%d reason=non_material", row["id"])
                        continue
                eligible = (cursor is not None and enabled and 0 <= now - published <= self.max_age
                            and (KEYWORDS.search(row["text"]) is not None or (old is not None and old["ever_sent"]))
                            and (old is not None or row["id"] > cursor))
                payload = json.dumps(make_payload(row, self.channel, updated=old is not None, observed_at=now), ensure_ascii=False)
                state = "pending" if eligible else "ignored"
                same_stage_update = old is not None and (prior is None or classify_status(prior['text']) == classify_status(row['text']))
                self.db.execute("""INSERT INTO posts(id,hash,payload,state,published,semantic_hash,row_snapshot) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET hash=excluded.hash,payload=excluded.payload,
                    state=excluded.state,published=excluded.published,semantic_hash=excluded.semantic_hash,
                    row_snapshot=excluded.row_snapshot,attempts=0,next_retry=0,source_update=?""",
                                (row["id"], digest, payload, state, published, meaning, snapshot, int(same_stage_update)))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('cursor',?)", (str(latest),))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('channel',?)", (self.channel,))

    def expire(self, now):
        with self.db:
            self.db.execute("UPDATE posts SET state='expired' WHERE state IN ('pending','dead') AND published<?",
                            (now - self.max_age,))

    def pending(self, now):
        self.expire(now)
        result = []
        with self.db:
            for row in self.db.execute("SELECT * FROM posts WHERE state='pending' AND next_retry<=? ORDER BY id LIMIT 5", (now,)).fetchall():
                keys = codex_resets.telegram_keys(previous_row(dict(row)) or {})
                if not row['source_update'] and codex_resets.seen_elsewhere(self.db, keys, 'telegram'):
                    self.db.execute("UPDATE posts SET state='deduped',ever_sent=1 WHERE id=?", (row['id'],))
                else:
                    result.append(dict(row))
        return result

    def delivered(self, msg_id, digest):
        with self.db:
            self.db.execute("UPDATE posts SET state='sent',ever_sent=1 WHERE id=? AND hash=?", (msg_id, digest))
            row = self.db.execute("SELECT * FROM posts WHERE id=? AND hash=?", (msg_id, digest)).fetchone()
            if row:
                codex_resets.record_receipts(self.db, codex_resets.telegram_keys(previous_row(dict(row)) or {}), 'telegram')

    def failed(self, msg_id, now, max_attempts=8):
        row = self.db.execute("SELECT attempts FROM posts WHERE id=?", (msg_id,)).fetchone()
        attempts = row[0] + 1
        state = "dead" if attempts >= max_attempts else "pending"
        delay = min(900, 30 * 2 ** (attempts - 1))
        with self.db:
            self.db.execute("UPDATE posts SET state=?,attempts=?,next_retry=? WHERE id=?",
                            (state, attempts, now + delay, msg_id))

    def counts(self):
        result = {key: 0 for key in ("sent", "pending", "dead", "ignored", "expired")}
        result.update(dict(self.db.execute("SELECT state,count(*) FROM posts GROUP BY state")))
        return result

    def has_delivery_error(self):
        return self.db.execute("SELECT 1 FROM posts WHERE state='dead' OR (state='pending' AND attempts>0) LIMIT 1").fetchone() is not None



class Transport:
    def __init__(self, proxy="", webhook="", relay_url="", relay_authorization=""):
        if proxy:
            parsed = validated_url(proxy, ("http", "https"))
            if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
                raise ValueError("invalid_connect_proxy")
        if relay_url:
            parsed = validated_url(relay_url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.path != "/codex-reset-feed" or parsed.query or parsed.fragment
                    or not relay_authorization.startswith("Basic ") or not relay_authorization[6:].strip()
                    or re.search(r"[\x00-\x1f\x7f]", relay_authorization) or proxy):
                raise ValueError("invalid_relay_configuration")
        self.relay_url = relay_url
        self.relay_authorization = relay_authorization
        self.source = build_opener(ProxyHandler({"https": proxy} if proxy else {}), NoRedirect())
        self.wecom = build_opener(ProxyHandler({}), NoRedirect())
        self.webhook = validate_webhook(webhook) if webhook else ""

    def make_source_request(self, channel, before=None):
        if self.relay_url and channel != "codexreset":
            raise ValueError("relay_channel_mismatch")
        if before is not None and (type(before) is not int or before <= 0 or before > 999999999999):
            raise ValueError("invalid_before")
        url = (self.relay_url or f"https://t.me/s/{channel}") + (f"?before={before}" if before is not None else "")
        headers = {"User-Agent": "CodexResetNotifier/1.0", "Cache-Control": "no-cache"}
        if self.relay_url:
            headers["Authorization"] = self.relay_authorization
        return Request(url, headers=headers)

    def make_wecom_request(self, payload):
        return Request(self.webhook, data=payload.encode("utf-8"),
                       headers={"Content-Type": "application/json"}, method="POST")

    def fetch(self, channel, before=None):
        request = self.make_source_request(channel, before)
        try:
            with self.source.open(request, timeout=15) as response:
                data = response.read(2_000_001)
                if len(data) > 2_000_000:
                    raise SourceError("page_too_large")
                return parse_page(data.decode("utf-8"), channel)
        except SourceError:
            raise
        except Exception as exc:
            raise SourceError("source_request_" + type(exc).__name__) from None

    def send(self, payload):
        if not self.webhook:
            raise DeliveryError("webhook_missing")
        request = self.make_wecom_request(payload)
        try:
            with self.wecom.open(request, timeout=15) as response:
                check_ack(response.read(65536))
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError("delivery_request_" + type(exc).__name__) from None


def open_stores(state_dir, channel, targets):
    stores = {}
    try:
        for name in targets:
            if name not in PROVIDERS or name in stores:
                raise ValueError("invalid_notification_channels")
            filename = "state.db" if name == "wecom" else "state-" + name + ".db"
            stores[name] = Store(Path(state_dir) / filename, channel)
        return stores
    except Exception:
        for store in stores.values():
            store.close()
        raise


def delivery_status(stores):
    details = {name: dict(store.counts(), delivery_error=store.has_delivery_error())
               for name, store in stores.items()}
    totals = {key: sum(state[key] for state in details.values())
              for key in ("sent", "pending", "dead", "ignored", "expired")}
    return dict(totals, destinations=details,
                delivery_error=any(state["delivery_error"] for state in details.values()))


def deliver_pending(stores, destinations, clock=None, heartbeat=lambda: None,
                    pause=lambda seconds: None, stopped=lambda: False):
    clock = time.time if clock is None else clock
    for name, destination in destinations.items():
        store = stores[name]
        for item in store.pending(clock()):
            if stopped():
                return
            heartbeat()
            try:
                destination.send(item["payload"])
                store.delivered(item["id"], item["hash"])
                LOG.info("delivery_ok destination=%s message_id=%d", name, item["id"])
            except DeliveryError as exc:
                store.failed(item["id"], clock())
                LOG.warning("delivery_failed destination=%s message_id=%d reason=%s", name, item["id"], exc)
            heartbeat()
            pause(3.5)


def fetch_since(transport, channel, cursor, now=None):
    rows = transport.fetch(channel)
    if cursor is None:
        return rows
    cutoff = (time.time() if now is None else now) - 48 * 3600
    # Cover both the previous cursor and the edit window, with bounded work.
    for _ in range(9):
        oldest = min(row["id"] for row in rows)
        if oldest <= cursor and min(message_time(row).timestamp() for row in rows) < cutoff:
            return rows
        previous = transport.fetch(channel, before=oldest)
        if min(row["id"] for row in previous) >= oldest:
            raise SourceError("pagination_did_not_advance")
        rows = list({row["id"]: row for row in previous + rows}.values())
    if min(row["id"] for row in rows) > cursor or min(message_time(row).timestamp() for row in rows) >= cutoff:
        raise SourceError("catchup_limit_reached")
    return rows


def fetch_observations(transport, channel, stores, now=None):
    # One in-memory page cache per poll; lagging channels cannot veto peers.
    class CachedPages:
        def __init__(self):
            self.pages = {}
            self.errors = {}

        def fetch(self, channel, before=None):
            if before in self.errors:
                raise SourceError(self.errors[before])
            if before not in self.pages:
                try:
                    self.pages[before] = transport.fetch(channel, before)
                except SourceError as exc:
                    self.errors[before] = str(exc)
                    raise
            return self.pages[before]

    cache = CachedPages()
    observations, errors = {}, {}
    for name, store in stores.items():
        try:
            observations[name] = fetch_since(cache, channel, store.cursor(), now=now)
        except SourceError as exc:
            errors[name] = str(exc)
    return observations, errors


def write_health(path, status):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(status))
    temporary.replace(path)


def health_check(path, kind):
    try:
        status = json.loads(path.read_text())
        now = time.time()
        alive = now - status.get("heartbeat", 0) < 300
        ready = (alive and now - status.get("source_success", 0) < 600
                 and status.get("dead", 0) == 0 and not status.get("delivery_error", False)
                 and not status.get("source_errors") and not status.get("codex_resets_error"))
        return 0 if (alive if kind == "live" else ready) else 1
    except (OSError, ValueError):
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", metavar="FILE", help="TOML file; defaults to NOTIFIER_CONFIG environment variable")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-config", action="store_true", help="validate configuration offline; output only a redacted summary")
    mode.add_argument("--dry-run", action="store_true", help="read public source once; never send or mutate delivery state")
    mode.add_argument("--health-check", choices=("live", "ready"))
    mode.add_argument("--send-test", metavar="TEST_ID", help="explicitly send one labelled acceptance notice after a real source fetch")
    parser.add_argument("--destination", choices=PROVIDERS, help="with --send-test, send only to this configured channel")
    args = parser.parse_args()
    if args.send_test and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.send_test):
        parser.error("--send-test requires a simple test ID")
    if args.destination and not args.send_test:
        parser.error("--destination requires --send-test")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    state_dir = cfg.state_dir
    health_path = state_dir / "health.json"
    if args.health_check:
        return health_check(health_path, args.health_check)
    channel = cfg.channel
    enabled = cfg.delivery_enabled and not args.dry_run
    interval = cfg.poll_interval_seconds
    website_enabled = codex_resets.enabled_from_env(os.environ)
    try:
        transport = Transport(cfg.proxy_url, "", cfg.relay_url, cfg.relay_authorization)
        transport.make_source_request(channel)  # Validate relay channel before any network access.
        destinations = from_settings(cfg)
    except ValueError:
        raise ConfigError("invalid_transport_configuration") from None
    if args.destination:
        if args.destination not in destinations:
            raise ConfigError("test_destination_not_selected")
        destinations = {args.destination: destinations[args.destination]}
    if enabled or args.send_test:
        for name, destination in destinations.items():
            if not destination.configured:
                raise ConfigError("webhook_missing" if name == "wecom" else name + "_credential_missing")
    if args.check_config:
        print(json.dumps({"config_ok": True, "channel": channel, "poll_interval_seconds": interval,
                          "delivery_enabled": cfg.delivery_enabled, "webhook_configured": bool(cfg.webhook_url),
                          "relay_configured": bool(cfg.relay_url), "proxy_configured": bool(cfg.proxy_url),
                          "notification_channels": list(destinations), "codex_resets_enabled": website_enabled,
                          "destinations": {name: {"configured": d.configured, "signing_enabled": d.signing_enabled}
                                           for name, d in destinations.items()}}))
        return 0
    if args.send_test:
        rows = transport.fetch(channel)
        payload = json.dumps(acceptance_payload(rows, channel, args.send_test), ensure_ascii=False)
        results = {}
        for name, destination in destinations.items():
            try:
                destination.send(payload)
                results[name] = True
            except DeliveryError as exc:
                LOG.warning("test_delivery_failed destination=%s reason=%s", name, exc)
                results[name] = False
        accepted = all(results.values())
        print(json.dumps({"test_message_accepted": accepted, "test_id": args.send_test,
                          "source_messages": len(rows), "destination_results": results}))
        return 0 if accepted else 1
    if args.dry_run:
        rows = transport.fetch(channel)
        extra = {}
        if website_enabled:
            events = codex_resets.fetch_status()
            extra = {"codex_resets_ok": True, "codex_resets_events": len(events)}
        print(json.dumps({"source_ok": True, "messages": len(rows), "latest_id": max(r["id"] for r in rows),
                          "matching_messages": sum(bool(KEYWORDS.search(r["text"])) for r in rows), "delivery_enabled": False, **extra}))
        return 0
    state_dir.mkdir(parents=True, exist_ok=True)
    stores = open_stores(state_dir, channel, cfg.notification_channels)
    websites = {name: codex_resets.Supplement(store.db) for name, store in stores.items()} if website_enabled else {}
    status = {"heartbeat": time.time(), "source_success": 0, "delivery_enabled": enabled, "delivery_error": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    LOG.info("started channel=%s delivery_enabled=%s relay_enabled=%s destinations=%s",
             channel, enabled, bool(transport.relay_url), ",".join(destinations))

    def heartbeat():
        status.update(delivery_status(stores), heartbeat=time.time())
        status["delivery_error"] |= any(site.has_error() for site in websites.values())
        write_health(health_path, status)

    try:
        while not STOP.is_set():
            heartbeat()
            observations, errors = fetch_observations(transport, channel, stores)
            for name, rows in observations.items():
                store = stores[name]
                try:
                    store.observe(rows, time.time(), enabled)
                    status["source_success"] = time.time()
                    LOG.info("source_ok destination=%s messages=%d latest_id=%d", name, len(rows), store.cursor())
                except SourceError as exc:
                    errors[name] = str(exc)
            if websites:
                status["codex_resets_error"] = codex_resets.poll(websites.values(), time.time(), enabled)
                if status["codex_resets_error"]:
                    LOG.warning("codex_resets_failed reason=%s", status["codex_resets_error"])
                else:
                    LOG.info("codex_resets_ok")
            status["source_errors"] = errors
            for name, error in errors.items():
                LOG.warning("source_failed destination=%s reason=%s", name, error)
            if enabled:
                deliver_pending(stores, destinations, heartbeat=heartbeat, pause=STOP.wait, stopped=STOP.is_set)
                for name, site in websites.items():
                    counts = codex_resets.deliver(site, destinations[name].send, time.time(), STOP.wait, STOP.is_set)
                    if counts["sent"] or counts["failed"]:
                        LOG.info("codex_resets_delivery destination=%s sent=%d failed=%d", name, counts["sent"], counts["failed"])
                    heartbeat()
            for store in stores.values():
                store.expire(time.time())
            heartbeat()
            STOP.wait(interval)
    finally:
        for store in stores.values():
            store.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ConfigError as error:
        LOG.error("configuration_error code=%s", error)
        raise SystemExit(1) from None
    except Exception as error:
        # Exceptions from HTTP libraries can contain URLs with credentials.
        LOG.error("fatal category=%s", type(error).__name__)
        raise SystemExit(1) from None
