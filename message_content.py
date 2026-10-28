"""Conservative status labels and material-change fingerprints for public posts.

Only known equivalent announcement templates and presentation noise are folded.
Unknown wording, changed numbers, scope and source URLs remain material changes.
"""
from datetime import datetime, timezone
import hashlib
import json
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

STATUS_LABELS = {"announced": "重置预告", "reported_complete": "来源称已完成",
                 "correction": "更正说明", "unconfirmed": "状态未确认"}
RESET = re.compile(r"重置|配额|额度|\breset\b|\bquotas?\b|\blimits?\b", re.I)
RETRACTION = re.compile(r"消息有误|请忽略|撤回|误报|并未发生|\b(?:retract(?:ed)?|false alarm|disregard)\b", re.I)
UNCERTAIN = re.compile(r"尚未确认|未获确认|无法确认|未经证实|据说|[?？]|"
                      r"\b(?:unconfirmed|unverified|rumou?r)\b|\bno\s+(?:evidence|confirmation|proof)\b", re.I)
NEGATED = re.compile(r"(?:尚未|还未|没有|并未|未曾|暂未|未能|不会|不再|不).{0,18}(?:重置|生效|完成)|"
                     r"\b(?:not|never|hasn't|haven't|isn't|aren't)\b.{0,45}\b(?:reset|live|complete|completed|propagated)\b|"
                     r"\b(?:no|none)\b[^\n.!?]{0,65}\b(?:reset|live|complete|completed|propagated)\b", re.I)
CONDITIONAL = re.compile(r"如果|假如|若(?:已|未)|\b(?:if|unless|assuming|once)\b", re.I)
FUTURE = re.compile(r"预计|将于|将会|即将|明天|明日|又要重置|具体时间|\b(?:will|would|should|tomorrow|planned|scheduled|landing)\b", re.I)
COMPLETE = re.compile(r"(?:已|已经)(?:为所有人|全员|全部|完成|提前|陆续|\s)*重置|"
                      r"重置(?:已经|已)(?:陆续|逐步|全部|全量)?(?:完成|生效)|(?:配额|额度).{0,10}(?:已经|已)(?:生效|刷新)|"
                      r"\b(?:has|have)\s+(?:now\s+|already\s+)?(?:been\s+)?reset\b|"
                      r"\b(?:reset|resets?)\s+(?:is\s+|has\s+been\s+)?(?:now\s+)?complete(?:d)?\b|"
                      r"\b(?:fresh\s+)?quotas?\s+(?:are|is)\s+(?:now\s+)?live\b|"
                      r"\b(?:all|fully)\s+(?:is\s+|has\s+|been\s+)*propagated\b|"
                      r"\b(?:limit|quota)\s+reset\s*:\s*(?:finished|completed|done)\s*$", re.I)


def classify_status(text):
    text = unicodedata.normalize("NFKC", text)
    if RETRACTION.search(text):
        return "correction"
    if not RESET.search(text) or UNCERTAIN.search(text) or NEGATED.search(text):
        return "unconfirmed"
    # Evaluate detail clauses: a stale 'today will reset' heading must not
    # override a later explicit completion, but a conditional is not evidence.
    clauses = re.split(r"[\n。.!；;]", text)
    for clause in clauses:
        if COMPLETE.search(clause) and not FUTURE.search(clause) and not CONDITIONAL.search(clause):
            return "reported_complete"
    if FUTURE.search(text) or re.search(r"reset.{0,60}\b(?:today|timeframe|time)\b", text, re.I):
        return "announced"
    return "unconfirmed"


def clean_body(text):
    return "\n".join(line.strip() for line in text.splitlines()
                     if line.strip() and not re.fullmatch(r"(?:source|follow\s+@[A-Za-z0-9_]+)", line.strip(), re.I))


def normalized_body(text):
    lines = []
    for line in clean_body(text).splitlines():
        line = unicodedata.normalize("NFKC", line).casefold()
        line = line.replace("\u200b", "").replace("\ufeff", "").strip()
        # Only known title decorations are ignored. ✅/❌, arrows, math and
        # other body symbols can change eligibility or scope and are retained.
        line = re.sub(r"^(?:🎁|🚨|⚡\ufe0f?)\s*", "", line)
        if re.fullmatch(r"[─━—_\-=\s]+", line):
            continue
        line = re.sub(r"^[•·]\s*", "", line)
        # Exact, audited equivalences; do not discard arbitrary English text.
        match = re.fullmatch(r"day\s*(\d+)\s*:\s*(?:quota reset confirmed for today|yet another reset today)[.!]?", line)
        if match:
            line = "day " + match[1] + ": reset announced today"
        if re.fullmatch(r"(?:limit reset:\s*)?(?:no specific timeframe announced|no exact time from tibo yet)[.!]?", line):
            line = "reset time unspecified"
        # Preserve numbers, decimal points, schedules, currency and units.
        line = re.sub(r"\s+", " ", line)
        lines.append(line)
    return "\n".join(lines)


def normalized_links(links, channel):
    result = []
    for value in links:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            continue
        host = parsed.hostname.lower()
        if host in ("x.com", "www.x.com", "twitter.com", "www.twitter.com"):
            match = re.fullmatch(r"/(?:[^/]+/status|i/web/status)/(\d+)/?", parsed.path)
            if match:
                value = "https://x.com/i/web/status/" + match[1]
        else:
            query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
                     if not k.lower().startswith("utm_") and k.lower() not in ("fbclid", "gclid")]
            value = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, urlencode(query), parsed.fragment))
            if host == "t.me" and parsed.path.rstrip("/") == "/" + channel and not query:
                continue
        if value not in result:
            result.append(value)
    return result


def fingerprint(row, channel):
    material = {"status": classify_status(row["text"]), "body": normalized_body(row["text"]),
                "links": sorted(normalized_links(row.get("links", []), channel))}
    return "v1:" + hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def raw_fingerprint(row):
    return hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def previous_row(record):
    """Read a valid snapshot, or recover a non-truncated legacy queue payload.

    Old releases ignore additive columns. Checking the raw hash detects a stale
    snapshot left by an old writer after a rollback, before comparing meaning.
    """
    try:
        snapshot = json.loads(record["row_snapshot"])
        if raw_fingerprint(snapshot) == record["hash"]:
            return snapshot
    except (ValueError, TypeError, KeyError):
        pass
    try:
        content = json.loads(record["payload"])["markdown"]["content"]
        head, separator, tail = content.rpartition("\n\n[频道原帖](")
        if not separator or "…详见原帖" in head:
            return None
        body = head.split("\n\n", 1)[1]
        return {"id": record["id"], "text": body,
                "date": datetime.fromtimestamp(record["published"], timezone.utc).isoformat(),
                "links": re.findall(r"\[来源\d+\]\(([^)]+)\)", tail)}
    except (ValueError, TypeError, KeyError, IndexError, OverflowError):
        return None
