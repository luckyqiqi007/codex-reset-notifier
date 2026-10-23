"""Outbound notification adapters. Credentials never enter delivery state or logs."""
import base64
import hmac
import json
import re
import time
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

PROVIDERS = ("wecom", "dingtalk", "feishu")
TITLE = "Codex 额度通知"


class DeliveryError(Exception):
    pass


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
    if (parsed.netloc != "qyapi.weixin.qq.com" or parsed.path != "/cgi-bin/webhook/send"
            or parsed.fragment or not parse_qs(parsed.query).get("key")):
        raise ValueError("invalid_wecom_webhook")
    return value


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect_refused")


def check_response(provider, data):
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError):
        raise DeliveryError(provider + "_invalid_json") from None
    code = None
    if isinstance(result, dict):
        field = "errcode" if provider in ("wecom", "dingtalk") else "code"
        if provider == "feishu" and "code" not in result:
            field = "StatusCode"
        code = result.get(field)
    if type(code) is not int or code != 0:
        label = str(code) if type(code) is int else "missing"
        raise DeliveryError(provider + "_error_" + label)


def check_ack(data):
    """Compatibility helper for the original WeCom sender."""
    check_response("wecom", data)


class Destination:
    def __init__(self, provider, url="", secret=""):
        if provider not in PROVIDERS:
            raise ValueError("unknown_notification_provider")
        self.provider = provider
        self._url = url
        self._secret = secret
        self.configured = bool(url)
        self.signing_enabled = bool(secret)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        if secret and re.search(r"[\x00-\x20\x7f]", secret):
            raise ValueError("invalid_signing_secret")
        if not url:
            return
        parsed = validated_url(url)
        if provider == "wecom":
            validate_webhook(url)
        elif provider == "dingtalk":
            query = parse_qs(parsed.query, keep_blank_values=True)
            if (parsed.netloc != "oapi.dingtalk.com" or parsed.path != "/robot/send"
                    or parsed.fragment or set(query) != {"access_token"}
                    or len(query["access_token"]) != 1 or not query["access_token"][0]):
                raise ValueError("invalid_dingtalk_webhook")
        elif provider == "feishu":
            if (parsed.netloc != "open.feishu.cn" or parsed.query or parsed.fragment
                    or not re.fullmatch(r"/open-apis/bot/v2/hook/[A-Za-z0-9_-]+", parsed.path)):
                raise ValueError("invalid_feishu_webhook")

    def request(self, payload, now=None):
        if not self.configured:
            raise DeliveryError(self.provider + "_credential_missing")
        try:
            source = json.loads(payload) if isinstance(payload, str) else payload
            content = source["markdown"]["content"]
            if not isinstance(content, str):
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise DeliveryError("invalid_notification_payload") from None
        now = time.time() if now is None else now
        url = self._url
        content_type = "application/json"
        if self.provider == "wecom":
            body = {"msgtype": "markdown", "markdown": {"content": content}}
        elif self.provider == "dingtalk":
            body = {"msgtype": "markdown", "markdown": {"title": TITLE, "text": content},
                    "at": {"isAtAll": False}}
            if self._secret:
                stamp = str(int(now * 1000))
                digest = hmac.digest(self._secret.encode(), (stamp + "\n" + self._secret).encode(), "sha256")
                url += "&" + urlencode({"timestamp": stamp, "sign": base64.b64encode(digest).decode()})
        elif self.provider == "feishu":
            plain = re.sub(r"\[([^\]]+)\]\((https://[^)]+)\)", r"\1：\2", content).replace("**", "")
            plain = plain.replace("<", "＜").replace(">", "＞")
            body = {"msg_type": "text", "content": {"text": plain}}
            if self._secret:
                stamp = str(int(now))
                digest = hmac.digest((stamp + "\n" + self._secret).encode(), b"", "sha256")
                body.update(timestamp=stamp, sign=base64.b64encode(digest).decode())
        data = json.dumps(body, ensure_ascii=False).encode()
        return Request(url, data=data, headers={"Content-Type": content_type}, method="POST")

    def send(self, payload):
        try:
            with self.opener.open(self.request(payload), timeout=15) as response:
                check_response(self.provider, response.read(65536))
        except DeliveryError:
            raise
        except Exception as exc:
            raise DeliveryError(self.provider + "_request_" + type(exc).__name__) from None


def from_settings(cfg):
    arguments = {
        "wecom": {"url": cfg.webhook_url},
        "dingtalk": {"url": cfg.dingtalk_webhook_url, "secret": cfg.dingtalk_secret},
        "feishu": {"url": cfg.feishu_webhook_url, "secret": cfg.feishu_secret},
    }
    return {name: Destination(name, **arguments[name]) for name in cfg.notification_channels}
