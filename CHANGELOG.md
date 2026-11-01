# Changelog

格式按 Keep a Changelog；版本号按语义化版本。

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
