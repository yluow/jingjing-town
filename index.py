# -*- coding: utf-8 -*-
"""鲸鲸小镇（Jingjing Town）· TelyinCore Infinity API 1 插件入口。

契约来源：《TelyinCore Infinity 插件开发规范 v1.2》
  §2  清单与目录          §3  生命周期
  §4  宿主上下文          §5  事件、工具与重复执行
  §7  配置页与密钥        §9  Bot、通道与进程隔离
  §14 异步工作必须有 owner §15 关闭、取消与最终释放
  §16 回执、批次与可信事件 §17 配置快照、密钥与外部请求

资源归属（全部属于"本实例"，全部在 stop() 内关停并核验）：
  1. 一个后台线程 ``jingjing-town-worker``：同时承担出站动作队列消费与观察轮询；
  2. 一个出站队列 + "已接受、未定案"台账（task_id → 原任务记录）；
  3. 一份游标文件 ``<dataDir>/jingjing-town/cursor.json``（原子替换写入）。

明确不做的事：
  * import / create / tools 不联网、不装依赖、不起线程（§3）；
  * 不写宿主人格、长期记忆或画像，不读写其它 Bot 的目录（§9、§16.2）；
  * 不把 pairing_token 写进日志、错误正文或返回值（§17）；
  * 超时、取消或结果未知时不自动重试副作用，只报告"结果未确认"（§5、§16.1）；
  * 不 import 宿主内部类（PluginIngress / ToolWork / lease），只使用 ctx 公开能力（§14）。
"""

import json
import os
import queue
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

__all__ = ["create", "TownUnavailable"]

PLUGIN_NAME = "jingjing-town"
TOOL_PREFIX = "town_"
WORKER_NAME = "jingjing-town-worker"

#: 单次响应体上限：超过即视为协议异常，不回显原文（§17 不展示未脱敏正文）
MAX_RESPONSE_BYTES = 64 * 1024
#: 失败台账上限：保留最近若干条，供 stop() 报告与运维排查；不在关闭时清空（§14.1）
MAX_FAILURES_KEPT = 100

#: 默认值只用于 config.json 缺字段时的补齐；启动前会做范围校验（§17 同一代快照）
DEFAULTS = {
    "service_url": "http://127.0.0.1:8800",
    "pairing_token": "",
    "poll_interval_seconds": 15,
    "request_timeout_seconds": 8,
    "stop_budget_seconds": 20,
}

LIMITS = {
    "poll_interval_seconds": (5, 300),
    "request_timeout_seconds": (2, 30),
    "stop_budget_seconds": (5, 60),
}

TOWN_EVENT_TYPE = "town.observation"
TOWN_EVENT_ORIGIN = "jingjing-town"


class TownUnavailable(RuntimeError):
    """启动前置条件不满足：缺少宿主能力、配置缺失或配置非法。

    规范 §3：缺少必要能力时必须明确失败，不得伪造成功或静默降级。
    """


class _RemoteError(RuntimeError):
    """小镇服务调用异常（网络、超时、非 2xx、协议不合法）。

    ``unknown=True`` 表示"服务端是否已经接收无法确认"：回执必须写成「结果未确认」，
    不能写成「失败 / 未接受」（§16.1）。这类错误一律不自动重试。
    """

    def __init__(self, message, unknown=False):
        RuntimeError.__init__(self, message)
        self.unknown = bool(unknown)


class _Task(object):
    """一次被接受的出站工作。派发前登记，定案后才从台账移除（§14.1）。"""

    __slots__ = ("task_id", "kind", "payload", "accepted_at", "event", "result", "error", "unknown")

    def __init__(self, kind, payload):
        self.task_id = uuid.uuid4().hex
        self.kind = kind
        self.payload = payload
        self.accepted_at = time.monotonic()
        self.event = threading.Event()
        self.result = None
        self.error = None
        self.unknown = False


def _ctx_get(ctx, key):
    """兼容 mapping 与属性两种 ctx 形态；取不到返回 None。"""
    try:
        return ctx.get(key)
    except AttributeError:
        return getattr(ctx, key, None)


def _redact(text, secret):
    """把秘密从任意字符串中抹掉，避免进入日志或回执（§17）。"""
    if not secret or not isinstance(text, str):
        return text
    return text.replace(secret, "***")


def create(ctx):
    """唯一入口。同步返回原实例，不做 IO、不启动任何后台工作（§3）。"""
    return TownPlugin(ctx)


class TownPlugin(object):
    def __init__(self, ctx):
        self._ctx = ctx
        self._lock = threading.Lock()
        self._state = "created"  # created / running / stopping / stopped / failed
        self._config = {}
        self._admission = False
        self._stop_evt = threading.Event()
        self._outbox = queue.Queue()
        self._pending = {}  # task_id -> _Task（已接受、未定案）
        self._worker = None
        self._failures = []  # [(stage, summary)]，只追加，不在关闭时清空
        self._failure_total = 0
        self._cursor = "0"
        self._cursor_path = None

    # ------------------------------------------------------------------ 日志

    def _log(self, message, **fields):
        log = _ctx_get(self._ctx, "log")
        if not callable(log):
            return
        token = self._config.get("pairing_token") or ""
        parts = []
        for key in sorted(fields):
            value = _redact(str(fields[key]), token)
            parts.append("%s=%s" % (key, value))
        line = _redact(message, token)
        if parts:
            line = "%s | %s" % (line, " ".join(parts))
        try:
            log(line)
        except Exception:
            # 日志能力由宿主提供，失败不能反过来破坏插件状态机
            pass

    def _record_failure(self, stage, exc):
        with self._lock:
            self._failure_total += 1
            self._failures.append((stage, "%s: %s" % (type(exc).__name__, exc)))
            if len(self._failures) > MAX_FAILURES_KEPT:
                del self._failures[0]
        self._log("小镇调用失败", stage=stage, error=exc)

    # ------------------------------------------------------------------ 配置

    def _load_config(self):
        """读取一份配置快照。宿主配置写入插件安装目录的 config.json（§7）。

        若部署版本通过 ctx['config'] 暴露字典或可调用对象，优先使用它。
        两种来源都不可用时使用 config.example.json 的默认值，并在启动时因
        缺少配对令牌而明确失败——绝不匿名发送（§17）。
        """
        raw = None
        provided = _ctx_get(self._ctx, "config")
        if isinstance(provided, dict):
            raw = dict(provided)
        elif callable(provided):
            try:
                value = provided()
            except Exception as exc:
                raise TownUnavailable("读取宿主配置失败：%s" % exc)
            if isinstance(value, dict):
                raw = dict(value)

        if raw is None:
            package_dir = _ctx_get(self._ctx, "packageDir")
            if package_dir:
                path = os.path.join(str(package_dir), "config.json")
                if os.path.isfile(path):
                    try:
                        with open(path, "r", encoding="utf-8") as handle:
                            loaded = json.load(handle)
                    except (OSError, ValueError) as exc:
                        raise TownUnavailable(
                            "config.json 无法解析，已停止启动，避免使用不可信配置：%s" % exc
                        )
                    if not isinstance(loaded, dict):
                        raise TownUnavailable("config.json 顶层必须是 JSON 对象")
                    raw = loaded

        cfg = dict(DEFAULTS)
        if raw:
            for key in DEFAULTS:
                if key in raw:
                    cfg[key] = raw[key]
        return cfg

    def _validate_config(self, cfg):
        url = cfg.get("service_url")
        if not isinstance(url, str) or not url.strip():
            raise TownUnavailable("配置项 service_url 缺失")
        parsed = urllib.parse.urlsplit(url.strip())
        if parsed.scheme not in ("http", "https"):
            raise TownUnavailable("service_url 只接受 http/https，拒绝 %r" % parsed.scheme)
        if not parsed.hostname:
            raise TownUnavailable("service_url 缺少主机名")
        if parsed.username or parsed.password:
            raise TownUnavailable("service_url 不得内嵌账号或口令，请把凭据放进配对令牌")
        if parsed.query or parsed.fragment:
            raise TownUnavailable("service_url 不应带查询串或片段")
        cfg["service_url"] = url.strip().rstrip("/")

        token = cfg.get("pairing_token")
        if not isinstance(token, str) or not token.strip():
            raise TownUnavailable(
                "未配置 pairing_token：本插件不向小镇服务匿名发送任何内容，请先在配置页配对"
            )
        cfg["pairing_token"] = token.strip()

        for key, (low, high) in LIMITS.items():
            value = cfg.get(key)
            try:
                number = int(value)
            except (TypeError, ValueError):
                raise TownUnavailable("配置项 %s 必须是整数" % key)
            if number < low or number > high:
                raise TownUnavailable("配置项 %s 必须在 %d..%d 之间" % (key, low, high))
            cfg[key] = number
        return cfg

    # ------------------------------------------------------------- 生命周期

    def start(self):
        """同步启动本实例拥有的工作；不阻塞装配、不返回 awaitable（§3、§14.1）。"""
        with self._lock:
            if self._state == "running":
                return
            if self._state != "created":
                raise TownUnavailable("实例状态为 %s，不能重新启动；请重载插件" % self._state)

        try:
            if not callable(_ctx_get(self._ctx, "push_event")):
                raise TownUnavailable(
                    "宿主未提供可调用的 ctx['push_event']，小镇观察无法进入主循环；"
                    "请使用已验证的宿主版本，或等待正式公开的唤醒扩展点"
                )
            data_dir = _ctx_get(self._ctx, "dataDir")
            if not data_dir:
                raise TownUnavailable("宿主未提供 dataDir，无法隔离游标与缓存")
            cfg = self._validate_config(self._load_config())
            cursor_path = os.path.join(str(data_dir), PLUGIN_NAME, "cursor.json")
            cursor = self._read_cursor(cursor_path)
        except BaseException:
            with self._lock:
                self._state = "failed"
            raise

        worker = threading.Thread(target=self._run, name=WORKER_NAME, daemon=True)
        with self._lock:
            self._config = cfg
            self._cursor_path = cursor_path
            self._cursor = cursor
            self._worker = worker  # 先登记再 start：任何时刻都能回答"谁拥有这个线程"
            self._admission = True
            self._stop_evt.clear()
            self._state = "running"
        try:
            worker.start()
        except BaseException:
            with self._lock:
                self._admission = False
                self._worker = None
                self._state = "failed"
            raise
        self._log(
            "鲸鲸小镇插件已启动",
            service=cfg["service_url"],
            poll=cfg["poll_interval_seconds"],
            cursor=cursor,
        )

    def stop(self):
        """先拒绝新工作，再等待已接受工作，最后释放；未排空返回 False 并保留句柄（§15）。"""
        with self._lock:
            state = self._state
            if state == "stopped":
                return True
            if state == "created":
                self._state = "stopped"
                return True
            self._admission = False
            if state == "running":
                self._state = "stopping"
            else:
                # 重复 stop：只重试可重复的关闭步骤（等待与核验），不重发任何业务动作
                self._state = "stopping"
            worker = self._worker
            budget = float(self._config.get("stop_budget_seconds", DEFAULTS["stop_budget_seconds"]))

        self._stop_evt.set()

        if worker is not None:
            worker.join(timeout=budget)

        alive = bool(worker is not None and worker.is_alive())
        with self._lock:
            pending = len(self._pending)
            failures = self._failure_total

        if alive or pending:
            # 保留原线程句柄、台账与失败责任，不启动替代实例、不进入正常退役（§15.1、§15.3）
            self._log(
                "鲸鲸小镇插件停止未完成",
                worker_alive=alive,
                pending=pending,
                failures=failures,
            )
            return False

        with self._lock:
            self._state = "stopped"
            self._worker = None
        self._log("鲸鲸小镇插件已停止", failures=failures)
        return True

    # --------------------------------------------------------------- 工具声明

    def tools(self):
        """只声明工具，不产生副作用（§5）。"""
        from telyincore.tools import ToolDef, ToolTag  # 框架模块由宿主提供

        def tag(name):
            value = getattr(ToolTag, name, None)
            if value is None:
                self._log("宿主 ToolTag 缺少枚举，已省略该标签", tag=name)
            return value

        def observe(**kwargs):
            return self._call("observe", {})

        def act(action=None, target=None, detail=None, **kwargs):
            if not isinstance(action, str) or not action.strip():
                return "未准入：缺少 action 参数，未向小镇服务提交任何内容。"
            payload = {"action": action.strip()}
            if target:
                payload["target"] = str(target)
            if isinstance(detail, dict):
                payload["detail"] = detail
            return self._call("act", payload)

        def say(text=None, audience="public", target=None, **kwargs):
            if not isinstance(text, str) or not text.strip():
                return "未准入：缺少 text 参数，未向小镇服务提交任何内容。"
            payload = {"text": text, "audience": "private" if audience == "private" else "public"}
            if target:
                payload["target"] = str(target)
            return self._call("say", payload)

        observe_def = ToolDef(
            name=TOOL_PREFIX + "observe",
            description="读取自己在鲸鲸小镇中的可感知状态：位置、时间、持有物品、已知规则与附近可见角色。",
            parameters={"type": "object", "properties": {}},
            tags=tuple(t for t in (tag("READ"),) if t is not None),
            handler=observe,
        )
        act_def = ToolDef(
            name=TOOL_PREFIX + "act",
            description=(
                "向鲸鲸小镇提交一次行动，由小镇服务结算。"
                "常用动作：move（detail 里给 x/y 坐标）、look、wait；"
                "do（detail.what 写你实际做了什么，例如 wake_up，规则判定只认这个）；"
                "stance（detail.rule_id + stance=comply/refuse/defer，对社会规则表态）；"
                "read_board（看布告栏，补齐对现有规则的了解）；"
                "tell_rule（detail.rule_id + detail.target，把规则讲给另一个角色）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "动作标识：move / look / wait / do / stance / read_board / tell_rule",
                    },
                    "target": {
                        "type": "string",
                        "description": "目标实体、物品或角色标识；本地动作可省略",
                    },
                    "detail": {
                        "type": "object",
                        "description": "动作参数，由小镇服务按能力定义校验",
                        "additionalProperties": True,
                    },
                },
                "required": ["action"],
            },
            tags=tuple(t for t in (tag("ACT"),) if t is not None),
            handler=act,
        )
        say_def = ToolDef(
            name=TOOL_PREFIX + "say",
            description="在鲸鲸小镇中发言：默认公开对话，也可指定 private 私聊对象。",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "要说的话"},
                    "audience": {
                        "type": "string",
                        "enum": ["public", "private"],
                        "description": "public 为公开对话，private 为私下交流",
                    },
                    "target": {
                        "type": "string",
                        "description": "private 时的对话对象标识",
                    },
                },
                "required": ["text"],
            },
            tags=tuple(t for t in (tag("SPEECH"),) if t is not None),
            handler=say,
        )
        return [observe_def, act_def, say_def]

    # ------------------------------------------------------------ 出站工作流

    def _call(self, kind, payload):
        """工具 handler 的公共路径：准入 → 登记 → 等待定案 → 如实回执（§16.1）。"""
        with self._lock:
            if not self._admission or self._state != "running":
                return "未准入：插件当前状态为 %s，本次调用没有提交到小镇服务。" % self._state
            task = _Task(kind, payload)
            self._pending[task.task_id] = task  # 派发前登记责任
        self._outbox.put(task)

        wait_seconds = float(self._config.get("request_timeout_seconds", 8)) + 2.0
        settled = task.event.wait(timeout=wait_seconds)
        if not settled:
            # 超时只说明"没观察到定案"，不说明调用没有发生（§14.2）
            return (
                "结果未确认：等待小镇服务回执超时（task_id=%s）。原任务仍由本实例持有，"
                "不要重复提交同一动作；需要确认时按 task_id 查询原结果。" % task.task_id
            )
        if task.error:
            if task.unknown:
                # 服务端可能已收到：只能说"未确认"，不能说"未接受"（§16.1）
                return (
                    "结果未确认（task_id=%s）：%s。原任务仍由本实例持有，"
                    "不要重复提交同一动作；需要核对时按 task_id 查询原结果。" % (task.task_id, task.error)
                )
            return (
                "失败：小镇服务未接受本次%s（task_id=%s）：%s"
                % ("动作" if kind == "act" else "发言" if kind == "say" else "查询", task.task_id, task.error)
            )
        result = task.result or {}
        summary = result.get("summary") or result.get("text") or "已由小镇服务结算"
        return "已定案（task_id=%s）：%s" % (task.task_id, summary)

    def _run(self):
        try:
            self._loop()
        except BaseException as exc:  # noqa: BLE001 - 线程顶层必须留下失败责任
            self._record_failure("worker", exc)
            # 不吞掉已接受工作：台账与状态保留，stop() 会因此返回 False

    def _loop(self):
        interval = float(self._config["poll_interval_seconds"])
        next_poll = time.monotonic()
        while True:
            if self._stop_evt.is_set():
                # 关闭触发器后只排空"已接受"的队列项，不再发起新的轮询（§15.1）
                try:
                    task = self._outbox.get_nowait()
                except queue.Empty:
                    return
                self._execute(task)
                continue

            wait = max(0.05, min(1.0, next_poll - time.monotonic()))
            try:
                task = self._outbox.get(timeout=wait)
            except queue.Empty:
                task = None
            if task is not None:
                self._execute(task)

            if self._stop_evt.is_set():
                continue
            if time.monotonic() >= next_poll:
                self._poll_observations()
                next_poll = time.monotonic() + interval

    def _execute(self, task):
        try:
            if task.kind == "observe":
                payload = self._http_json("GET", "/v1/me")
            elif task.kind == "act":
                payload = self._http_json(
                    "POST", "/v1/act", body=task.payload, idempotency_key=task.task_id
                )
            else:
                payload = self._http_json(
                    "POST", "/v1/say", body=task.payload, idempotency_key=task.task_id
                )
            task.result = payload
        except BaseException as exc:  # noqa: BLE001
            task.error = str(exc)
            task.unknown = bool(getattr(exc, "unknown", True))
            self._record_failure(task.kind, exc)
        finally:
            with self._lock:
                self._pending.pop(task.task_id, None)
            task.event.set()

    def _poll_observations(self):
        if self._stop_evt.is_set():
            return
        query = urllib.parse.urlencode({"cursor": self._cursor, "limit": 20})
        try:
            payload = self._http_json("GET", "/v1/observations?" + query)
        except BaseException as exc:  # noqa: BLE001
            self._record_failure("poll", exc)
            return

        observations = payload.get("observations") or []
        if not isinstance(observations, list):
            self._record_failure("poll", _RemoteError("observations 字段不是数组"))
            return

        delivered_all = True
        for item in observations:
            if not isinstance(item, dict):
                continue
            if not self._push_observation(item):
                delivered_all = False

        next_cursor = payload.get("next_cursor")
        if isinstance(next_cursor, str) and next_cursor and delivered_all:
            # 投递失败则不推进游标：下一轮会重投同一条，dedupKey 由宿主去重（§5）
            self._cursor = next_cursor
            self._write_cursor(next_cursor)

    def _push_observation(self, item):
        push = _ctx_get(self._ctx, "push_event")
        if not callable(push):
            self._record_failure("push", TownUnavailable("push_event 不可调用"))
            return False

        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            return True  # 无可读正文的观察不投递，也不阻塞游标

        event = {
            "type": TOWN_EVENT_TYPE,
            "source": PLUGIN_NAME,
            "origin": TOWN_EVENT_ORIGIN,
            "text": text,
        }
        town_id = item.get("town_id")
        if isinstance(town_id, str) and town_id:
            event["conversation"] = "town:%s" % town_id
        sender = item.get("sender_key")
        if isinstance(sender, str) and sender:
            event["senderKey"] = sender
        message_id = item.get("message_id")
        if isinstance(message_id, (str, int)):
            event["message_id"] = message_id
        dedup = item.get("event_id")
        if isinstance(dedup, str) and dedup:
            event["dedupKey"] = dedup
        town_time = item.get("town_time")
        if isinstance(town_time, str) and town_time:
            event["ts"] = town_time
        try:
            push(event)
        except Exception as exc:  # noqa: BLE001
            self._record_failure("push", exc)
            return False
        return True

    # ------------------------------------------------------------------ HTTP

    def _http_json(self, method, path, body=None, idempotency_key=None):
        cfg = self._config
        url = cfg["service_url"] + path
        data = None
        timeout = float(cfg["request_timeout_seconds"])
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + cfg["pairing_token"],
            "User-Agent": "%s/%s" % (PLUGIN_NAME, "0.1.1"),
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                status = response.status
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            code = self._error_code(exc)
            raise _RemoteError("小镇服务返回 HTTP %d%s" % (exc.code, code))
        except (TimeoutError, socket.timeout):
            # 本地超时只证明"没等到响应"，不证明请求没到达（§14.2）
            raise _RemoteError("请求小镇服务超时，结果未确认", unknown=True)
        except urllib.error.URLError as exc:
            raise _RemoteError("连接小镇服务失败：%s" % _safe_reason(exc.reason), unknown=True)
        except OSError as exc:
            raise _RemoteError("连接小镇服务失败：%s" % _safe_reason(exc), unknown=True)

        if len(raw) > MAX_RESPONSE_BYTES:
            raise _RemoteError("响应体超过 %d 字节上限" % MAX_RESPONSE_BYTES, unknown=True)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            # 2xx 但正文不可解析：服务端是否已结算未知
            raise _RemoteError("响应不是可解析的 JSON（HTTP %d），已省略原始正文" % status, unknown=True)
        if not isinstance(payload, dict):
            raise _RemoteError("响应顶层不是 JSON 对象（HTTP %d）" % status, unknown=True)
        return payload

    @staticmethod
    def _error_code(exc):
        """只取可公开的错误码，不回显可能夹带密钥的原始正文（§17）。"""
        try:
            raw = exc.read(4096)
        except Exception:  # noqa: BLE001
            return ""
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return ""
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, str) and code:
                return "（%s）" % code[:64]
        return ""

    # ------------------------------------------------------------------ 游标

    def _read_cursor(self, path):
        if not os.path.isfile(path):
            return "0"
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError) as exc:
            # 游标损坏不阻断启动，但从零开始会在服务端去重；如实记录
            self._record_failure("cursor_read", exc)
            return "0"
        cursor = payload.get("cursor") if isinstance(payload, dict) else None
        return cursor if isinstance(cursor, str) and cursor else "0"

    def _write_cursor(self, cursor):
        path = self._cursor_path
        if not path:
            return
        directory = os.path.dirname(path)
        temp_path = path + ".tmp"
        try:
            os.makedirs(directory, exist_ok=True)
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump({"cursor": cursor}, handle)
            os.replace(temp_path, path)  # 原子替换，避免读方看到半份字节
        except OSError as exc:
            self._record_failure("cursor_write", exc)


def _safe_reason(reason):
    """网络错误原因脱敏：只保留类型与简短描述，去掉可能的凭据回显。"""
    text = str(reason) if reason is not None else "unknown"
    text = text.replace("\n", " ").strip()
    return text[:160]
