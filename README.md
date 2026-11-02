# MaiBot_DevinSearch

用免费 Devin 账号授权一次，给 MaiBot 挂**联网搜索**和**网页正文抓取**两个 Planner 工具。

## 快速上手

**装插件 → 授权 → 使用，三步：**

```
1. 安装：MaiBot WebUI → 插件市场 → 搜「DevinSearch」→ 安装并启用
```

```
2. 授权（私聊 bot，管理员）：
/devin_login          → 返回授权链接
   ↓ 浏览器打开链接，登录 Devin 账号（没有就去 app.devin.ai 免费注册）
/devin_code <授权码>  → 显示「登录成功」即完成，365 天有效

管理员账号遵从MaiBot配置文件中已配置的管理员账号
```

```
3. 使用（群里直接@她或说话触发，不需要命令）：
"查一下杀戮尖塔2最新消息"
"这个链接讲了啥 https://..."
"最近xxx怎么样了"
```

工具调用条件不满足时她会直接说"联网搜索未配置"或"搜索失败"——查 `/devin_status` 定位。

> **手动部署**（不走市场）：整目录丢进 `plugins/CharTyr_DevinSearch/`，watchfiles 自动热加载。

## 致谢 / Attribution

本项目协议层移植自 **[dsh-devin-search](https://github.com/mimimaster/dsh-devin-search)**
（作者 [mimimaster](https://github.com/mimimaster)，MIT License）——OAuth PKCE 流程、
Windsurf `GetWebSearchResults` 请求格式、双 host 容灾、`devin-session-token$` token 前缀
与 365 天 fallback 有效期等关键 wire 协议细节均源自该项目及其上游 piwin。

本仓库仅保留 web_search 与新增 devin_fetch（未移植 code_search 的本地代码检索沙箱）。
若上游协议有变，请优先参照原仓库最新实现修正。

## 工具

| 工具 | 说明 |
| :-- | :-- |
| `devin_search` | 联网搜索实时信息，返回标题+摘要+来源链接（1-10 条） |
| `devin_fetch` | 抓网页正文（r.jina.ai 优先过反爬，trafilatura 直连兜底，8000 字上限） |

两个都强制 `visibility=visible`，planner 看到"查一下/搜一下/什么是/详细讲讲那个链接"会自己调。

## 管理员命令（`permission="operator"`）

私聊 bot：

```
/devin_login          → 出授权链接，浏览器登录 Devin
/devin_code <授权码>  → 完成登录
/devin_status         → 查看登录状态/到期时间
/devin_logout         → 清除本地会话
/devin_cancel         → 取消在途授权
```

### 权限判定

`permission="operator"` 由宿主在调用插件前拦截，**非管理员发命令插件一行代码都跑不到**，只收到「你没有权限使用此命令。」。放行规则三条路任一命中：

1. **本地终端**（console 直发，`bot_console` 平台）→ 无条件放行
2. **全局 operator 名单** — `config/bot_config.toml` 的 `[plugin] permission`：
   ```toml
   permission = ["qq:你的QQ号"]   # 格式 platform:user_id，跨平台
   ```
   命中即所有 operator 命令全放行。
3. **单命令白名单** — `[plugin] command_permissions`，给某命令单独开小灶不动全局：
   ```toml
   [plugin.command_permissions."chartyr.devinsearch.devin_status"]
   allow_users = ["qq:别的号"]
   allow_chats = ["群session_id"]
   ```

授权码兑换的 session token 落盘 `devin_session.json`（0600），opaque token 按
365 天 fallback 记。到期/被服务端拒绝后重走 `/devin_login` 即可。

## 文件

```
plugin.py      — lifecycle + Tool/Command + create_plugin()
client.py      — OAuth + SessionStore + GetWebSearchResults 客户端
_manifest.json — manifest v2（i18n/plugin_type/display.icon 齐全）
config.toml    — 运行时生成（plugins/<dir>/config.toml），非源文件
```

## 配置（`config.toml`）

```toml
[plugin]
enabled = true
config_version = "0.1.0"

[webui]                        # 可选 WebUI 页，默认全部关闭
enable_status_page = false     # 「登录状态」workspace 页
enable_history_page = false    # 「搜索历史」sidebar 页
history_limit = 50             # 历史保留条数（10-200）

[search]
timeout_seconds = 20   # 单节点 HTTP 超时
max_results = 5        # devin_search 默认返回条数
```

### WebUI 页面（默认关闭）

两个页面组件与 `_manifest.json`/`webui.json` 一起注册，但 **API 端默认返回 `enabled=false` 占位**，
关掉时页面只显示「未启用」提示、不出数据。需要时改 `[webui]` 对应开关为 `true` 即生效。

- **登录状态**（workspace）：会话状态、到期时间、本次运行搜索/抓取次数，含登出按钮
- **搜索历史**（sidebar）：devin_search 调用记录表（重启即清空），含清空按钮

## 依赖

无额外依赖（urllib + 宿主自带 trafilatura/readability/bs4 兜底）。
`devin_fetch` 首选 `r.jina.ai` 免费 reader。
