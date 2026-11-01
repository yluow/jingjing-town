# -*- coding: utf-8 -*-
"""生命周期、归属与回执语义的合成测试（规范 §3、§5、§14、§15、§16、§19.1）。

夹具：本地 127.0.0.1 上的假小镇服务（随机端口），会真实记录请求次数、头与正文，
所以不是"准备阶段就被拒"的空跑。测试通过公开入口 create/start/stop/tools 驱动；
个别用例用白盒手法替换 `_http_json` 或读取 `_pending` / `_worker`，
目的是让"未排空"这一时序可以被确定性地复现。
"""

import json
import os
import shutil
import sys
import threading
import time
import types
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import index  # noqa: E402

#: 临时目录建在工作区内（.gitignore 已排除）：部分沙箱环境里 tempfile 建出的
#: 目录会拿到自己都写不进去的权限，而 os.makedirs 建出的目录正常。
TMP_ROOT = os.path.join(ROOT, ".tmp-tests")


def make_scratch_dir():
    os.makedirs(TMP_ROOT, exist_ok=True)
    path = os.path.join(TMP_ROOT, uuid.uuid4().hex)
    os.makedirs(path)
    return path


def install_fake_host():
    """注入最小的 telyincore.tools，模拟宿主提供的工具声明类型。"""
    if "telyincore.tools" in sys.modules:
        return

    package = types.ModuleType("telyincore")
    package.__path__ = []
    tools = types.ModuleType("telyincore.tools")

    class ToolTag(object):
        READ = "READ"
        ACT = "ACT"
        SPEECH = "SPEECH"

    class ToolDef(object):
        def __init__(self, name, description, parameters, tags=(), handler=None):
            self.name = name
            self.description = description
            self.parameters = parameters
            self.tags = tuple(tags)
            self.handler = handler

    tools.ToolTag = ToolTag
    tools.ToolDef = ToolDef
    package.tools = tools
    sys.modules["telyincore"] = package
    sys.modules["telyincore.tools"] = tools


install_fake_host()


class TownHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *args):
        pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        try:
            parsed = urlsplit(self.path)
            body = None
            if method == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    body = {}
            self.server.record(method, parsed.path, parse_qs(parsed.query),
                               dict(self.headers.items()), body)
            self._route(method, parsed, body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 客户端超时后关闭连接是预期路径，不当作测试失败
            pass

    def _route(self, method, parsed, body):
        path = parsed.path
        if method == "GET" and path == "/v1/observations":
            cursor = (parse_qs(parsed.query).get("cursor") or ["0"])[0]
            if cursor == "0":
                self._send({
                    "ok": True,
                    "observations": [{
                        "event_id": "e-001",
                        "town_id": "t-01",
                        "town_time": "D3 06:00",
                        "text": "布告栏贴出新规则：每天 6:00 必须起床。",
                    }],
                    "next_cursor": "1",
                })
            else:
                self._send({"ok": True, "observations": [], "next_cursor": cursor})
            return
        if method == "GET" and path == "/v1/me":
            self._send({"ok": True, "summary": "你在广场（D3 06:00）"})
            return
        if method == "POST" and path in ("/v1/act", "/v1/say"):
            detail = (body or {}).get("detail")
            delay = 0.0
            if isinstance(detail, dict):
                try:
                    delay = float(detail.get("delay") or 0)
                except (TypeError, ValueError):
                    delay = 0.0
            if delay > 0:
                time.sleep(delay)
            self._send({"ok": True, "summary": "服务端已结算：%s" % (body or {}).get("action", "say")})
            return
        self._send({"ok": False, "error": {"code": "not_found"}}, 404)

    def _send(self, payload, status=200):
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class FakeTown(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self):
        ThreadingHTTPServer.__init__(self, ("127.0.0.1", 0), TownHandler)
        self.calls = []
        self._calls_lock = threading.Lock()

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.server_address[1]

    def record(self, method, path, query, headers, body):
        with self._calls_lock:
            self.calls.append((method, path, query, headers, body))

    def calls_to(self, path):
        with self._calls_lock:
            return [call for call in self.calls if call[1] == path]


class PluginTestBase(unittest.TestCase):
    TOKEN = "unit-test-token-9f3a"

    def setUp(self):
        self.server = FakeTown()
        self.server_thread = threading.Thread(target=self.server.serve_forever,
                                              name="fake-town", daemon=True)
        self.server_thread.start()
        self.tmp = make_scratch_dir()
        self.pkg_dir = os.path.join(self.tmp, "pkg")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(self.pkg_dir)
        os.makedirs(self.data_dir)
        self.logs = []
        self.events = []
        self.plugins = []

    def tearDown(self):
        for plugin in self.plugins:
            self.stop_fully(plugin)
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---------------------------------------------------------------- 夹具

    def config(self, **overrides):
        cfg = {
            "service_url": self.server.base_url,
            "pairing_token": self.TOKEN,
            "poll_interval_seconds": 5,
            "request_timeout_seconds": 2,
            "stop_budget_seconds": 5,
        }
        cfg.update(overrides)
        return cfg

    def write_config(self, cfg):
        with open(os.path.join(self.pkg_dir, "config.json"), "w", encoding="utf-8") as handle:
            json.dump(cfg, handle, ensure_ascii=False)

    def make_ctx(self, with_push=True, config=None):
        ctx = {
            "name": "test-bot",
            "packageDir": self.pkg_dir,
            "root": self.tmp,
            "botDir": self.tmp,
            "dataDir": self.data_dir,
            "log": self.logs.append,
        }
        if with_push:
            ctx["push_event"] = self.events.append
        if config is not None:
            ctx["config"] = config
        return ctx

    def make_plugin(self, with_push=True, config=None, **cfg_overrides):
        self.write_config(self.config(**cfg_overrides))
        plugin = index.create(self.make_ctx(with_push=with_push, config=config))
        self.plugins.append(plugin)
        return plugin

    def tool(self, plugin, name):
        for definition in plugin.tools():
            if definition.name == name:
                return definition
        raise AssertionError("未声明的工具：%s" % name)

    def stop_fully(self, plugin, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if plugin.stop():
                return True
            time.sleep(0.2)
        return False

    def wait_for_events(self, count=1, timeout=4.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.events) >= count:
                return True
            time.sleep(0.05)
        return False

    def worker_threads(self):
        return [t for t in threading.enumerate() if t.name == index.WORKER_NAME]

    # ---------------------------------------------------------------- 用例

    def test_create_and_tools_have_no_side_effects(self):
        plugin = self.make_plugin()
        self.assertEqual(self.worker_threads(), [], "create 不得启动后台线程")
        definitions = plugin.tools()
        self.assertEqual(sorted(d.name for d in definitions),
                         ["town_act", "town_observe", "town_say"])
        self.assertEqual(self.server.calls, [], "声明工具不得发请求")
        self.assertEqual(self.worker_threads(), [], "tools() 不得启动后台线程")
        self.assertTrue(plugin.stop())

    def test_tool_tags_follow_declared_side_effects(self):
        plugin = self.make_plugin()
        tags = {d.name: d.tags for d in plugin.tools()}
        self.assertEqual(tags["town_observe"], ("READ",))
        self.assertEqual(tags["town_act"], ("ACT",))
        self.assertEqual(tags["town_say"], ("SPEECH",))

    def test_start_requires_push_event(self):
        plugin = self.make_plugin(with_push=False)
        with self.assertRaises(index.TownUnavailable):
            plugin.start()
        self.assertEqual(self.server.calls, [])
        self.assertTrue(plugin.stop(), "启动失败后没有线程，应能正常释放")

    def test_start_requires_pairing_token_and_stays_silent(self):
        plugin = self.make_plugin(pairing_token="")
        with self.assertRaises(index.TownUnavailable):
            plugin.start()
        self.assertEqual(self.server.calls, [], "缺令牌时不得匿名发送")
        self.assertTrue(plugin.stop())

    def test_rejects_unsafe_service_url(self):
        for bad in ("file:///tmp/x", "http://user:pass@host/", "ftp://host", "http://host/?a=1"):
            plugin = self.make_plugin(service_url=bad)
            with self.assertRaises(index.TownUnavailable, msg=bad):
                plugin.start()
        self.assertEqual(self.server.calls, [])

    def test_tool_call_settles_with_idempotency_and_auth(self):
        plugin = self.make_plugin()
        plugin.start()
        result = self.tool(plugin, "town_act").handler(action="move", target="zone_west")
        self.assertIn("已定案", result, result)

        calls = self.server.calls_to("/v1/act")
        self.assertEqual(len(calls), 1)
        headers = calls[0][3]
        self.assertEqual(headers.get("Authorization"), "Bearer " + self.TOKEN)
        self.assertTrue(headers.get("Idempotency-Key"), "写请求必须带幂等键")
        self.assertEqual(calls[0][4]["action"], "move")

        self.assertTrue(plugin.stop())
        self.assertEqual(self.worker_threads(), [], "停止后不得留下本插件的线程")

    def test_observation_is_pushed_with_dedup_key(self):
        plugin = self.make_plugin()
        plugin.start()
        self.assertTrue(self.wait_for_events(1), "启动后应拉取到一次观察")
        event = self.events[0]
        self.assertEqual(event["type"], "town.observation")
        self.assertEqual(event["source"], "jingjing-town")
        self.assertEqual(event["dedupKey"], "e-001")
        self.assertEqual(event["conversation"], "town:t-01")
        self.assertIn("布告栏", event["text"])
        self.assertTrue(plugin.stop())

        cursor_file = os.path.join(self.data_dir, "jingjing-town", "cursor.json")
        self.assertTrue(os.path.isfile(cursor_file))
        with open(cursor_file, "r", encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["cursor"], "1")

    def test_cursor_survives_restart_and_avoids_redelivery(self):
        first = self.make_plugin()
        first.start()
        self.assertTrue(self.wait_for_events(1))
        self.assertTrue(self.stop_fully(first))
        delivered = len(self.events)

        before = len(self.server.calls_to("/v1/observations"))
        second = index.create(self.make_ctx())
        self.plugins.append(second)
        second.start()
        time.sleep(0.6)
        self.assertTrue(second.stop())
        self.assertEqual(len(self.events), delivered, "重启后不得重复投递同一观察")

        queries = [call[2] for call in self.server.calls_to("/v1/observations")[before:]]
        self.assertTrue(queries, "重启后应至少拉取一次")
        self.assertEqual(queries[0].get("cursor"), ["1"], "重启应从已保存游标继续")

    def test_not_admitted_after_stop(self):
        plugin = self.make_plugin()
        plugin.start()
        self.assertTrue(plugin.stop())
        result = self.tool(plugin, "town_say").handler(text="还在吗")
        self.assertIn("未准入", result)
        self.assertEqual(self.server.calls_to("/v1/say"), [])

    def test_stop_twice_does_not_resend(self):
        plugin = self.make_plugin()
        plugin.start()
        self.assertIn("已定案", self.tool(plugin, "town_say").handler(text="大家好"))
        sent = len(self.server.calls_to("/v1/say"))
        self.assertTrue(plugin.stop())
        self.assertTrue(plugin.stop(), "重复 stop 应仍然返回 True")
        self.assertEqual(len(self.server.calls_to("/v1/say")), sent, "关闭不得重发业务动作")

    def test_incomplete_stop_keeps_thread_and_ledger(self):
        plugin = self.make_plugin()
        plugin.start()

        gate = threading.Event()
        plugin._http_json = lambda *args, **kwargs: (gate.wait(30) and {"ok": True, "summary": "晚到"})

        result = {}
        caller = threading.Thread(
            target=lambda: result.setdefault("text", plugin._call("act", {"action": "hold"})),
            name="caller", daemon=True,
        )
        caller.start()
        time.sleep(0.3)

        self.assertFalse(plugin.stop(), "还有未定案工作时应报告未完成")
        self.assertIsNotNone(plugin._worker, "未排空时必须保留原线程句柄")
        self.assertEqual(len(plugin._pending), 1, "未定案台账不得被清空")
        self.assertIn("结果未确认", result.get("text", ""), "等待超时只能报未确认")

        gate.set()
        caller.join(10)
        self.assertTrue(self.stop_fully(plugin), "工作结束后再次 stop 应能确认释放")

    def test_timeout_reports_unknown_not_success(self):
        plugin = self.make_plugin()
        plugin.start()
        result = self.tool(plugin, "town_act").handler(action="sleep", detail={"delay": 6})
        self.assertIn("结果未确认", result, result)
        self.assertNotIn("已定案", result)
        self.assertTrue(self.stop_fully(plugin))

    def test_token_never_reaches_logs(self):
        plugin = self.make_plugin()
        plugin.start()
        self.tool(plugin, "town_act").handler(action="move")
        self.tool(plugin, "town_act").handler(action="broken", detail={"delay": 6})
        self.assertTrue(self.stop_fully(plugin))
        joined = "\n".join(str(line) for line in self.logs)
        self.assertNotIn(self.TOKEN, joined, "配对令牌不得写入日志")

    def test_ctx_config_overrides_file(self):
        ctx_config = self.config(poll_interval_seconds=5)
        self.write_config({"service_url": "http://127.0.0.1:1", "pairing_token": "x"})
        plugin = index.create(self.make_ctx(config=ctx_config))
        self.plugins.append(plugin)
        plugin.start()
        self.assertIn("已定案", self.tool(plugin, "town_observe").handler())
        calls = self.server.calls_to("/v1/me")
        self.assertEqual(len(calls), 1, "应使用 ctx 提供的配置快照")
        self.assertTrue(plugin.stop())


if __name__ == "__main__":
    unittest.main(verbosity=2)
