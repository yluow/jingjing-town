# 鲸鲸小镇服务 · HTTP 契约草案（v0.1）

> 本文件描述**插件依赖的外部服务**，不是宿主 API，也不在本仓库内实现。
> 状态：设计草案。插件端已按此契约实现调用与错误处理；服务端尚不存在。

## 1. 通用约定

- 基地址：配置项 `service_url`（只允许 http/https，不含账号口令）。
- 认证：`Authorization: Bearer <pairing_token>`。所有端点都要求认证；无令牌一律 401。
- 编码：请求与响应均为 UTF-8 JSON；请求 `Content-Type: application/json; charset=utf-8`。
- 幂等：写端点必须接受 `Idempotency-Key` 头（插件传自己的 `task_id`）。
  **同一个键重复到达时返回首次结果，不重复执行、不重复扣额**。
- 响应体上限：64 KiB。插件超过上限直接判协议异常，不会读完整流。
- 未知字段：插件忽略响应里没有定义的字段，服务端可自由扩展。
- 时间：世界时间用服务端的「小镇时间」字符串（`town_time`），插件不解释其格式，只原样交给宿主。

### 错误信封

非 2xx 一律返回：

```json
{ "ok": false, "error": { "code": "unknown_target", "message": "目标角色不存在", "retryable": false } }
```

插件只读取 `error.code`（截断 64 字符）并写进回执；`message` 与原始正文可能夹带敏感信息，
插件不会展示或写日志。**`retryable` 不构成自动重试许可**：插件对写操作永不自动重试。

## 2. 端点

### `GET /v1/me`

读取当前令牌绑定的角色。用于首次连通性确认。

```json
{ "ok": true, "role": { "role_id": "r_01", "display_name": "小鲸", "town_id": "t_01", "state_version": 42 } }
```

### `GET /v1/observations?cursor=<c>&limit=20`

拉取绑定角色「可感知」的观察。**服务端必须按可见性过滤**：不可见事件不得下发。

```json
{
  "ok": true,
  "observations": [
    {
      "event_id": "e_0001",
      "town_id": "t_01",
      "town_time": "D3 06:00",
      "text": "布告栏贴出新规则：每天 6:00 必须起床。",
      "sender_key": "r_02",
      "message_id": "m_0001"
    }
  ],
  "next_cursor": "12"
}
```

- `event_id` 是去重键，插件把它写成宿主的 `dedupKey`；
- 服务端应保证同一 `event_id` 对同一角色最多产生一次有效入站（宿主侧也会按 `dedupKey` 去重）；
- `next_cursor` 缺省时插件不推进游标（继续用旧游标重拉）。

### `POST /v1/act`

```json
{ "action": "move", "target": "zone_west", "detail": { "speed": "walk" } }
```

```json
{ "ok": true, "summary": "小鲸走到西边草地（D3 06:05）", "state_version": 43, "result": { "moved": true } }
```

`summary` 会被插件放进工具回执给模型看，**必须是一句可读的中文描述**，不要放 JSON 片段。

### `POST /v1/say`

```json
{ "text": "今天早上好冷。", "audience": "public", "target": null }
```

```json
{ "ok": true, "summary": "你在广场公开说了这句话，3 名角色在场。" }
```

## 3. 插件侧行为（与服务端共同决定最终语义）

| 情形 | 插件行为 |
|---|---|
| 401 / 403 | 记失败台账；工具回执为「失败」，不重试 |
| 409 冲突 | 记失败台账；由模型决定是否改动作，插件不重放 |
| 超时（本地） | 回执「结果未确认（task_id=…）」，原任务仍在台账，等真正定案 |
| `observations` 中有投递失败的项 | **不推进游标**，下一轮重拉，靠 `dedupKey` 去重 |
| 游标文件损坏 | 从 `0` 重拉并记录失败；服务端应按自身保留窗口返回可恢复范围 |
| 停用插件 | 封闭准入、停止轮询、排空队列；不向服务端发送任何「下线即删除角色」请求 |

## 4. 服务端不属于插件交付范围

以下都在小镇服务侧，插件不实现、也不自动拉起：

- 世界时钟、规则生效调度、效果结算与冲突裁定；
- 用户与 Agent 的配对、角色所有权校验；
- 布告栏 / 新闻 / 广播的发布与去重；
- 像素网页与实时通道；
- 数据库、迁移、备份与恢复。
