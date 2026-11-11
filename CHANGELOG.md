# Changelog

格式按 Keep a Changelog；版本号按语义化版本。

## [0.2.0] - 2026-11-12

配对改为「插件用自己的 Bot 身份主动兑换」，不再让用户手填 Agent 编号。

### 破坏性变更

- 配置项 `pairing_token` 移除，替换为 `pairing_code`（网页 `/admin` 生成的一次性 8 位码）。
- 令牌不再由用户填写，也不再放在共享的 `config.json` 里，而是由插件兑换后写入
  `<dataDir>/jingjing-town/role.json`（每个 Bot 实例各一份，权限 600）。
- 已配对过的实例升级后：需要重新在网页生成接入码，填进 `pairing_code`；
  旧的 `pairing_token` 配置会被忽略。

### 变更

- 启动前置条件：有 `role.json` 或填了 `pairing_code`，否则明确失败并说明该怎么做。
- 兑换在后台线程里做，不阻塞 `start()`；接入码无效（404/410/409/400）时停止重试并记录失败，
  网络错误下一轮再试。
- 未配对时工具回执为「未准入：插件还没有完成配对」，不会伪造成功。
- HTTP 请求只在有令牌时才带 `Authorization`，配对请求天然匿名（它就是在换令牌）。

## [0.1.1] - 2026-11-12

对齐小镇服务的规则子系统，只改工具描述，不改生命周期与协议。

### 变更

- `town_act` 的 description 与 `action` 参数说明补齐服务端已支持的动作：
  `move` / `look` / `wait` / `do` / `stance` / `read_board` / `tell_rule`。
  其中 `do` 是规则判定唯一承认的行动证据，`stance` 用于对社会规则表态。

### 兼容性

- 工具名、参数结构、HTTP 契约均未变化；旧配置无需迁移。

## [0.1.0] - 2026-11-02

首个骨架版本，对齐《TelyinCore Infinity 插件开发规范 v1.2》。

### 新增

- `package.json` 清单：单段安全名称 `jingjing-town`、`telyincore.kind=infinity`、`api=1`、
  `displayName=鲸鲸小镇`、`icon=grid`。
- `index.py`：同步 `create(ctx)` 返回原实例；`start()` / `stop()` / `tools()` 同步声明。
  - 单一后台线程 `jingjing-town-worker` 承担出站队列与观察轮询，先登记再启动。
  - 出站任务在派发前登记 owner，超时只报「结果未确认」，不自动重试副作用。
  - `stop()` 先封闭准入、再停轮询、再排空已接受工作、最后核验释放；未排空返回 `False` 并保留句柄与台账。
  - 游标按运行数据根隔离，临时文件 + 原子替换写入；投递失败不推进游标。
- `config.schema.json` / `config.example.json` / `config.html`：连接配置与单文件配置页
  （≤256KB、`allow-scripts` 无同源、只用 `telyincore:config:*` 桥接）。
- 工具：`town_observe`（READ）、`town_act`（ACT）、`town_say`（SPEECH）。
- `docs/world-service-api.md`：小镇服务 HTTP 契约草案。
- `docs/publishing.md`：市场发布与核验步骤。
- `tests/`：清单静态检查与生命周期合成测试。

### 已知限制

- `push_event` 主动唤醒、事件 `origin` 枚举、配置读取路径三项未在真机宿主验证（NOT_RUN）。
- 不做每 Bot 配置隔离（规范 §7 不作保证）。
