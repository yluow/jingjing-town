# 发布到 TelyinCore 插件市场

> 依据《Infinity 插件开发规范 v1.2》§10、§11、§18、§19。
> 市场不是托管站、也不是审核背书：它通过 GitHub **话题**发现仓库，再读取 **tag 源码归档**。

## 0. 发布前必改（缺一项就不要公开）

- [x] `package.json` 的 `author` 已填真实维护者 `yluow`（规范 §2 要求写实际作者或维护者名称，不得用匿名占位）。
- [x] `LICENSE` 版权行已同步为 `Copyright (c) 2026 yluow`。
- [ ] 确认 `LICENSE` 选的 MIT 是你有权授予的许可；若你另有安排，改 `package.json.license` 与正文**一起**改。
- [ ] 把 README「当前状态」里仍是 NOT_RUN 的项要么补测，要么保留原样——
      不要为了好看写成"已通过"（规范 §19.3）。

## 1. 仓库命名与可见性

- 仓库名：`jingjing-town`（小写英文 + 连字符；规范 §11.1）。
- 可见性：Public。
- 根目录**直接**放 `package.json` 和 `index.py`，不要再套一层同名目录（§11.2）。

## 2. 首次提交

```bash
git init
git add package.json index.py config.example.json config.schema.json config.html \
        README.md LICENSE CHANGELOG.md .gitignore docs tests
git status
git diff --cached
git commit -m "Initial plugin release"
git branch -M main
git remote add origin https://github.com/OWNER/jingjing-town.git   # 换成你的仓库
git push -u origin main
```

不要把密码或令牌写进 remote URL。

## 3. 打标签

市场读取 **tags** 并下载 tag 源码 `tar.gz`，**不读你上传的 Release 附件**；
当前实现取 tags 接口返回的**首项**，不保证语义版本排序、不做预发布过滤。
没有 tag 时回退默认分支，并在页面标注为分支来源。

```bash
# 确认 package.json.version == 0.1.0 后再打
git tag -a v0.1.0 -m "Release 0.1.0"
git push origin v0.1.0
```

后续每次发布：先改 `version` 与 `CHANGELOG.md`，再提交，再打**新的**标签。
**不要移动已经公开的标签**（§11.3）。

## 4. 设置市场发现话题

在仓库 About → Topics 添加：

```text
core-infinity-plugin
```

两个东西不要混：

| 位置 | 值 | 作用 |
|---|---|---|
| GitHub Topics | `core-infinity-plugin` | 让市场搜索发现仓库 |
| `package.json.keywords` | 含 `core-infinity` | 让下载后的包通过声明检查 |

不需要 npm 发布，也不需要把配置页传到独立网站。

## 5. 归档体积

安装器解包总量上限 **64 MiB**。模型权重、运行环境、截图都不要塞进插件归档；
本仓库当前只有源码与文档，远低于上限（`tests/test_manifest.py` 会检查）。

## 6. 从全新目录验收（§11.5）

不要只在开发目录里试跑：

1. 在没有私人配置的目录下载 `v0.1.0` 的 tag 源码，确认根目录有 `package.json`、`index.py`、配置页；
2. 打开插件市场 → 刷新 → 找到仓库 → 核对来源、版本、说明 → 安装；
3. **安装 ≠ 启用**：先填 `service_url` 与 `pairing_token`，再手动启用；
4. 验证配置页能读、能存、刷新后保留，且令牌不出现在页面与日志里；
5. 验证停用后线程与队列真的释放（日志应出现「已停止」；若出现「停止未完成」要看 `pending` / `worker_alive`）；
6. 重载多次不重复入站、不重复发送；
7. 断网时工具回执应是「失败」或「结果未确认」，不是成功。

市场搜不到但本地能跑，先查分发条件（公开性、话题拼写、tag、64 MiB），
**不要改业务代码绕过包校验**（§11.5）。

## 7. 不要做的事

- 不提交 `config.json`、`.config-backups/`、`.env`、日志、聊天记录、账号绑定、模型权重；
- 不捆绑 TelyinCore 本体或官方内置插件；
- 不为"方便安装"关闭鉴权、开公网端口，或要求用户交出平台账号密码；
- 不在插件启动时静默 `pip install` 或拉起外部软件；
- 不把 `license` 写成 MIT 却随包带别人的受限代码——修改他人插件需先取得授权并注明来源。
