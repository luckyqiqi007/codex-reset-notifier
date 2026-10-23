"""Public Telegram channel to WeCom. Python 3.12+, standard library only."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
import json
import logging
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from configuration import ConfigError, load_config

LOG = logging.getLogger("codex-reset-notifier")
STOP = threading.Event()
KEYWORDS = re.compile(r"重置|额度|配额|\breset\b|\bresets\b|\bquota\b|\blimits?\b|\bcredits?\b", re.I)
VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "wbr"}


class SourceError(Exception):
    pass


class DeliveryError(Exception):
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


def make_payload(row, channel, updated=False):
    stamp = message_time(row).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    title = "内容更新" if updated else "频道转发"
    head = f"**Codex 额度通知｜{title}**\n原帖发布时间：{stamp}（北京时间）\n\n"
    tail = f"\n\n[频道原帖](https://t.me/{channel}/{row['id']})"
    for index, url in enumerate(row["links"][:3], 1):
        safe_url = url.replace("(", "%28").replace(")", "%29").replace("\n", "").replace("\r", "")
        link = f" · [来源{index}]({safe_url})"
        if len((tail + link).encode("utf-8")) <= 1500:
            tail += link
    tail += "\n频道消息转发，未独立核验账号到账。"
    body = row["text"].replace("<", "＜").replace(">", "＞")
    budget = 4000 - len((head + tail).encode("utf-8"))
    if len(body.encode("utf-8")) > budget:
        body = body.encode("utf-8")[:max(0, budget - 30)].decode("utf-8", "ignore") + "\n…详见原帖"
    return {"msgtype": "markdown", "markdown": {"content": head + body + tail}}


def acceptance_payload(rows, channel, test_id):
    latest = max(row["id"] for row in rows)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    content = (f"**Codex 通知服务｜验收测试**\n验收编号：{test_id}\n时间：{stamp}（北京时间）\n\n"
               f"已成功读取 {len(rows)} 条公开频道消息，并验证企业微信投递链路。\n"
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
            raise ValueError("state_channel_mismatch")

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
                digest = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                old = self.db.execute("SELECT hash, state, ever_sent FROM posts WHERE id=?", (row["id"],)).fetchone()
                if old and old["hash"] == digest:
                    continue
                eligible = (cursor is not None and enabled and 0 <= now - published <= self.max_age
                            and (KEYWORDS.search(row["text"]) is not None or (old is not None and old["ever_sent"]))
                            and (old is not None or row["id"] > cursor))
                payload = json.dumps(make_payload(row, self.channel, updated=old is not None), ensure_ascii=False)
                state = "pending" if eligible else "ignored"
                self.db.execute("""INSERT INTO posts(id,hash,payload,state,published) VALUES(?,?,?,?,?)
                    ON CONFLICT(id) DO UPDATE SET hash=excluded.hash,payload=excluded.payload,
                    state=excluded.state,published=excluded.published,attempts=0,next_retry=0""",
                                (row["id"], digest, payload, state, published))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('cursor',?)", (str(latest),))
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('channel',?)", (self.channel,))

    def expire(self, now):
        with self.db:
            self.db.execute("UPDATE posts SET state='expired' WHERE state IN ('pending','dead') AND published<?",
                            (now - self.max_age,))

    def pending(self, now):
        self.expire(now)
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM posts WHERE state='pending' AND next_retry<=? ORDER BY id LIMIT 5", (now,))]

    def delivered(self, msg_id, digest):
        with self.db:
            self.db.execute("UPDATE posts SET state='sent',ever_sent=1 WHERE id=? AND hash=?", (msg_id, digest))

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


def validated_url(value, schemes=("https",)):
    if re.search(r"[\x00-\x20\x7f]", unquote(value)):
        raise ValueError("url_contains_whitespace_or_controls")
    try:
        parsed = urlparse(value)
        if parsed.scheme not in schemes or not parsed.hostname:
            raise ValueError("invalid_url")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("invalid_port")
        return parsed
    except ValueError:
        raise ValueError("invalid_url") from None


def validate_webhook(value):
    parsed = validated_url(value)
    if (parsed.scheme != "https" or parsed.netloc != "qyapi.weixin.qq.com"
            or parsed.path != "/cgi-bin/webhook/send" or parsed.fragment
            or not parse_qs(parsed.query).get("key")):
        raise ValueError("invalid_wecom_webhook")
    return value


def check_ack(data):
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise DeliveryError("wecom_invalid_json") from None
    code = result.get("errcode") if isinstance(result, dict) else None
    if type(code) is not int or code != 0:
        label = str(code) if type(code) is int else "missing"
        raise DeliveryError("wecom_error_" + label)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect_refused")


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
                 and status.get("dead", 0) == 0 and not status.get("delivery_error", False))
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
    args = parser.parse_args()
    if args.send_test and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", args.send_test):
        parser.error("--send-test requires a simple test ID")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(args.config)
    state_dir = cfg.state_dir
    health_path = state_dir / "health.json"
    if args.health_check:
        return health_check(health_path, args.health_check)
    channel = cfg.channel
    enabled = cfg.delivery_enabled and not args.dry_run
    interval = cfg.poll_interval_seconds
    if (enabled or args.send_test) and not cfg.webhook_url:
        raise ConfigError("webhook_missing")
    try:
        transport = Transport(cfg.proxy_url, cfg.webhook_url, cfg.relay_url, cfg.relay_authorization)
        transport.make_source_request(channel)  # Validate relay channel before any network access.
    except ValueError:
        raise ConfigError("invalid_transport_configuration") from None
    if args.check_config:
        print(json.dumps({"config_ok": True, "channel": channel, "poll_interval_seconds": interval,
                          "delivery_enabled": cfg.delivery_enabled, "webhook_configured": bool(cfg.webhook_url),
                          "relay_configured": bool(cfg.relay_url), "proxy_configured": bool(cfg.proxy_url)}))
        return 0
    if args.send_test:
        rows = transport.fetch(channel)
        transport.send(json.dumps(acceptance_payload(rows, channel, args.send_test), ensure_ascii=False))
        print(json.dumps({"test_message_accepted": True, "test_id": args.send_test, "source_messages": len(rows)}))
        return 0
    if args.dry_run:
        rows = transport.fetch(channel)
        print(json.dumps({"source_ok": True, "messages": len(rows), "latest_id": max(r["id"] for r in rows),
                          "matching_messages": sum(bool(KEYWORDS.search(r["text"])) for r in rows), "delivery_enabled": False}))
        return 0
    state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(state_dir / "state.db", channel)
    status = {"heartbeat": time.time(), "source_success": 0, "delivery_enabled": enabled, "delivery_error": False}
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: STOP.set())
    LOG.info("started channel=%s delivery_enabled=%s relay_enabled=%s", channel, enabled, bool(transport.relay_url))
    try:
        while not STOP.is_set():
            status["heartbeat"] = time.time()
            write_health(health_path, status)
            try:
                rows = fetch_since(transport, channel, store.cursor())
                store.observe(rows, time.time(), enabled)
                status["source_success"] = time.time()
                LOG.info("source_ok messages=%d latest_id=%d", len(rows), store.cursor())
            except SourceError as exc:
                LOG.warning("%s", exc)
            if enabled:
                for item in store.pending(time.time()):
                    if STOP.is_set():
                        break
                    try:
                        transport.send(item["payload"])
                        store.delivered(item["id"], item["hash"])
                        status["delivery_error"] = False
                        LOG.info("delivery_ok message_id=%d", item["id"])
                    except DeliveryError as exc:
                        store.failed(item["id"], time.time())
                        status["delivery_error"] = True
                        LOG.warning("delivery_failed message_id=%d reason=%s", item["id"], exc)
                    STOP.wait(3.5)
            store.expire(time.time())
            status.update(store.counts(), heartbeat=time.time())
            status["delivery_error"] = store.has_delivery_error()
            write_health(health_path, status)
            STOP.wait(interval)
    finally:
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
