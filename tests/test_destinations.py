"""Offline protocol, signature and credential-boundary regression tests."""
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
PAYLOAD = json.dumps({"msgtype": "markdown", "markdown": {
    "content": "**Codex 额度通知**\n重置消息\n[原帖](https://t.me/codexreset/101)"}}, ensure_ascii=False)


class DestinationTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("destinations"), "destination adapters not implemented")
        import destinations
        self.module = destinations

    def client(self, kind, **kwargs):
        return self.module.Destination(kind, **kwargs)

    def test_wecom_keeps_existing_markdown_wire_format(self):
        client = self.client("wecom", url="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TEST_ONLY")
        request = client.request(PAYLOAD, now=1700000000)
        self.assertEqual(json.loads(request.data), json.loads(PAYLOAD))
        self.assertEqual(request.get_method(), "POST")
        self.assertIsNone(request.get_header("Authorization"))

    def test_dingtalk_signature_uses_milliseconds_and_secret_as_key(self):
        client = self.client("dingtalk", url="https://oapi.dingtalk.com/robot/send?access_token=TEST_ONLY", secret="TEST_SECRET")
        request = client.request(PAYLOAD, now=1700000000)
        query = parse_qs(urlparse(request.full_url).query)
        self.assertEqual(query["timestamp"], ["1700000000000"])
        self.assertEqual(query["sign"], ["WwAtuBwf4fVjotxQozLR5l6vux36/TbgeWjH+uZFYAI="])
        body = json.loads(request.data)
        self.assertEqual(body["msgtype"], "markdown")
        self.assertFalse(body["at"]["isAtAll"])
        self.assertIn("https://t.me/codexreset/101", body["markdown"]["text"])
        self.assertNotIn("TEST_SECRET", request.full_url + request.data.decode())
        self.assertNotEqual(request.full_url, client.request(PAYLOAD, now=1700000010).full_url)

    def test_dingtalk_without_secret_allows_keyword_or_ip_security(self):
        client = self.client("dingtalk", url="https://oapi.dingtalk.com/robot/send?access_token=TEST_ONLY")
        query = parse_qs(urlparse(client.request(PAYLOAD).full_url).query)
        self.assertEqual(set(query), {"access_token"})

    def test_feishu_signature_uses_seconds_and_empty_message(self):
        client = self.client("feishu", url="https://open.feishu.cn/open-apis/bot/v2/hook/TEST_ONLY", secret="TEST_SECRET")
        request = client.request(PAYLOAD, now=1700000000)
        body = json.loads(request.data)
        self.assertEqual(body["timestamp"], "1700000000")
        self.assertEqual(body["sign"], "CFHVoLvMvRuBmDD+rphx40RQOoI2aFC5krd8oMRWxDc=")
        self.assertEqual(body["msg_type"], "text")
        self.assertIn("https://t.me/codexreset/101", body["content"]["text"])
        self.assertNotIn("TEST_SECRET", request.full_url + request.data.decode())
        self.assertEqual(urlparse(request.full_url).query, "")

    def test_feishu_neutralizes_untrusted_at_markup(self):
        client = self.client("feishu", url="https://open.feishu.cn/open-apis/bot/v2/hook/TEST_ONLY")
        raw = json.dumps({"markdown": {"content": '<at user_id="all">所有人</at>'}})
        body = json.loads(client.request(raw).data)
        self.assertNotIn("<at", body["content"]["text"])

    def test_each_provider_requires_its_own_explicit_success_code(self):
        successes = {"wecom": b'{"errcode":0}', "dingtalk": b'{"errcode":0}',
                     "feishu": b'{"code":0}'}
        for kind, data in successes.items():
            with self.subTest(kind=kind):
                self.module.check_response(kind, data)
                for failure in (b'{}', b'not-json', b'{"code":false,"errcode":false}',
                                b'{"code":1,"errcode":1,"msg":"PRIVATE_CREDENTIAL"}'):
                    with self.assertRaises(self.module.DeliveryError) as ctx:
                        self.module.check_response(kind, failure)
                    self.assertNotIn("PRIVATE_CREDENTIAL", str(ctx.exception))
        self.module.check_response("feishu", b'{"StatusCode":0}')
        with self.assertRaises(self.module.DeliveryError):
            self.module.check_response("feishu", b'{"code":1,"StatusCode":0}')

    def test_endpoints_are_provider_specific_and_signatures_are_not_stored_in_url(self):
        bad = [("dingtalk", "https://evil.example/robot/send?access_token=PRIVATE"),
               ("dingtalk", "https://oapi.dingtalk.com/robot/send?access_token=PRIVATE&sign=old"),
               ("dingtalk", "https://oapi.dingtalk.com/robot/send?access_token=A&access_token=B"),
               ("feishu", "http://open.feishu.cn/open-apis/bot/v2/hook/PRIVATE"),
               ("feishu", "https://open.feishu.cn/open-apis/bot/v2/hook/PRIVATE?token=EXTRA")]
        for kind, url in bad:
            with self.subTest(kind=kind, url=url), self.assertRaises(ValueError) as ctx:
                self.client(kind, url=url)
            self.assertNotIn("PRIVATE", str(ctx.exception))

    def test_http_errors_cannot_leak_webhook_credentials(self):
        client = self.client("dingtalk", url="https://oapi.dingtalk.com/robot/send?access_token=PRIVATE")
        with patch.object(client.opener, "open", side_effect=OSError("PRIVATE URL")):
            with self.assertRaises(self.module.DeliveryError) as ctx:
                client.send(PAYLOAD)
            self.assertNotIn("PRIVATE", str(ctx.exception))

    def test_destination_ignores_global_proxies(self):
        import os
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://proxy.example.com:3128"}):
            client = self.client("dingtalk", url="https://oapi.dingtalk.com/robot/send?access_token=TEST_ONLY")
            proxies = [h for h in client.opener.handlers if isinstance(h, self.module.ProxyHandler)]
            self.assertTrue(all(not h.proxies for h in proxies))


if __name__ == "__main__":
    unittest.main()
