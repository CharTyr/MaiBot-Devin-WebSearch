# MaiBot_DevinSearch

用免费 Devin 账号授权一次，给 MaiBot 挂**联网搜索**和**网页正文抓取**两个 Planner 工具。

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

[search]
timeout_seconds = 20   # 单节点 HTTP 超时
max_results = 5        # devin_search 默认返回条数
```

## 依赖

无额外依赖（urllib + 宿主自带 trafilatura/readability/bs4 兜底）。
`devin_fetch` 首选 `r.jina.ai` 免费 reader。
