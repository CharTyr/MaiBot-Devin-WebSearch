"""MaiBot Devin 联网搜索插件

用免费 Devin 账号 OAuth 授权一次，给 Planner 挂 `devin_search` 工具：
群里问"查一下/搜一下/什么是/最近xxx"时自动联网检索并带回来源。

管理员命令（私聊，operator 权限）：
- /devin_login          → 出授权链接，浏览器登录后粘授权码
- /devin_code <code>    → 提交授权码完成登录
- /devin_cancel         → 取消在途登录
- /devin_status         → 查看登录状态
- /devin_logout         → 清除本地会话
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from maibot_sdk import API, Command, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from . import client as devin_client
from .client import DevinError, DevinSearchClient, SessionStore

logger = logging.getLogger(__name__)

PLUGIN_ID = "chartyr.devinsearch"
PLUGIN_VERSION = "0.2.0"


class PluginSectionConfig(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default=PLUGIN_VERSION, description="配置版本")


class WebUISectionConfig(PluginConfigBase):
    __ui_label__ = "页面"
    __ui_icon__ = "layout-dashboard"
    __ui_order__ = 1

    enable_status_page: bool = Field(default=False, description="开启 WebUI「登录状态」页")
    enable_history_page: bool = Field(default=False, description="开启 WebUI「搜索历史」页")
    history_limit: int = Field(default=50, description="搜索历史保留条数（10-200）")


class SearchSectionConfig(PluginConfigBase):
    __ui_label__ = "搜索"
    __ui_icon__ = "search"
    __ui_order__ = 2

    timeout_seconds: int = Field(default=20, description="单节点 HTTP 超时秒数")
    max_results: int = Field(default=5, description="默认返回条数（1-10）")


class DevinSearchConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    webui: WebUISectionConfig = Field(default_factory=WebUISectionConfig)
    search: SearchSectionConfig = Field(default_factory=SearchSectionConfig)


def _tool_param(
    name: str,
    param_type: ToolParamType,
    description: str,
    required: bool = False,
) -> ToolParameterInfo:
    return ToolParameterInfo(
        name=name,
        param_type=param_type,
        description=description,
        required=required,
    )


class DevinSearchPlugin(MaiBotPlugin):
    """Devin 联网搜索：planner 工具 + 管理员登录命令。"""

    config_model = DevinSearchConfig

    def __init__(self) -> None:
        super().__init__()
        self._store: Optional[SessionStore] = None
        self._client: Optional[DevinSearchClient] = None
        self._timeout = 20
        self._max_results = 5
        # 在途登录：code_verifier 留在内存，不落盘
        self._login_verifier: Optional[str] = None
        # WebUI 页开关与统计
        self._webui_status = False
        self._webui_history = False
        self._history_limit = 50
        self._search_history: list[dict] = []
        self._search_count = 0
        self._fetch_count = 0

    # ---- 生命周期 ----

    def _session_path(self) -> str:
        try:
            base = os.path.dirname(os.path.abspath(__file__))
        except Exception:  # noqa: BLE001
            base = os.getcwd()
        return os.path.join(base, "devin_session.json")

    async def on_load(self) -> None:
        self._apply_config()
        self._store = SessionStore(self._session_path())
        self._client = DevinSearchClient(self._store, timeout=self._timeout)
        logger.info(
            "Devin 搜索插件已加载（会话：%s）",
            "有效" if self._store.available() else "未登录/失效",
        )

    async def on_unload(self) -> None:
        self._client = None
        self._store = None
        self._login_verifier = None
        logger.info("Devin 搜索插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        self._apply_config(config_data)
        if self._store is not None:
            self._client = DevinSearchClient(self._store, timeout=self._timeout)
        logger.info("Devin 搜索插件配置已更新")

    def _apply_config(self, config_data: Optional[dict] = None) -> None:
        section: Any = None
        webui_sec: Any = None
        if config_data is not None:
            section = (config_data or {}).get("search")
            webui_sec = (config_data or {}).get("webui")
        else:
            try:
                section = getattr(self.config, "search", None)
                webui_sec = getattr(self.config, "webui", None)
            except Exception:  # noqa: BLE001 — 未注入配置时用默认
                section = None
        if isinstance(section, dict):
            self._timeout = int(section.get("timeout_seconds", 20) or 20)
            self._max_results = int(section.get("max_results", 5) or 5)
        elif section is not None:
            self._timeout = int(getattr(section, "timeout_seconds", 20) or 20)
            self._max_results = int(getattr(section, "max_results", 5) or 5)
        if isinstance(webui_sec, dict):
            self._webui_status = bool(webui_sec.get("enable_status_page", False))
            self._webui_history = bool(webui_sec.get("enable_history_page", False))
            self._history_limit = max(10, min(200, int(webui_sec.get("history_limit", 50) or 50)))
        elif webui_sec is not None:
            self._webui_status = bool(getattr(webui_sec, "enable_status_page", False))
            self._webui_history = bool(getattr(webui_sec, "enable_history_page", False))
            self._history_limit = max(
                10, min(200, int(getattr(webui_sec, "history_limit", 50) or 50))
            )

    def get_components(self) -> list[dict[str, Any]]:
        """devin_search 常驻 visible（@Tool kwargs 进不了 planner 白名单）。"""
        components = super().get_components()
        for comp in components:
            metadata = comp.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
                comp["metadata"] = metadata
            if str(comp.get("name") or "") in ("devin_search", "devin_fetch"):
                metadata["visibility"] = "visible"
        return components


    # ---- Planner 工具 ----

    @Tool(
        "devin_search",
        description=(
            "联网搜索实时信息。触发场景：用户问时事新闻、梗、新出的东西、"
            "'查一下/搜一下/什么是xxx/最近xxx怎么样'等需要最新资料的问题；"
            "也用于核实你不确定的事实。参数 query 为搜索关键词（中英文皆可）。"
            "返回标题+摘要+来源链接列表；回复时把要点用口语概括给用户，"
            "需要引用时附来源链接，不要照搬大段原文。"
        ),
        parameters=[
            _tool_param(
                "query",
                ToolParamType.STRING,
                "搜索关键词，如 'CS2 最新更新'、'iPhone 17 发布日期'",
                required=True,
            ),
            _tool_param(
                "max_results",
                ToolParamType.INTEGER,
                "返回条数 1-10，默认 5",
            ),
        ],
    )
    async def devin_search(
        self,
        query: str = "",
        max_results: Any = None,
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        if self._client is None or self._store is None:
            return False, "搜索服务尚未初始化。", 0
        if not self._store.available():
            return False, "联网搜索未配置：管理员私聊发送 /devin_login 完成授权后即可使用。", 0

        limit = self._max_results
        if max_results not in (None, ""):
            try:
                limit = max(1, min(10, int(max_results)))
            except (TypeError, ValueError):
                pass

        try:
            result = await asyncio.to_thread(self._client.search, str(query), limit)
        except DevinError as exc:
            logger.warning("[DevinSearch] 搜索失败(%s): %s", exc.code, exc)
            if exc.code == "login":
                return False, f"联网搜索不可用：{exc}", 0
            return False, f"搜索失败：{exc}，稍后再试。", 0
        except Exception as exc:  # noqa: BLE001
            logger.error("[DevinSearch] 搜索异常: %s", exc)
            return False, f"搜索失败：{exc}，稍后再试。", 0

        sources = result.get("sources") or []
        self._search_count += 1
        self._record_history(str(query), len(sources))
        if not sources:
            return True, f"没有搜到「{query}」的相关结果，换个关键词试试。", 1

        lines = [f"搜索「{query}」的结果："]
        for i, src in enumerate(sources, 1):
            title = src.get("title") or src.get("url") or "无标题"
            snippet = src.get("snippet") or ""
            url = src.get("url") or ""
            lines.append(f"{i}. {title}")
            if snippet:
                lines.append(f"   {snippet[:300]}")
            if url:
                lines.append(f"   来源: {url}")
        if result.get("truncated"):
            lines.append("（还有更多结果未列出）")
        return True, "\n".join(lines), 1

    @Tool(
        "devin_fetch",
        description=(
            "抓取网页正文。配合 devin_search 使用：搜到想深入看的链接后，"
            "用此工具传入 url 拉取全文（自动去广告/导航只留正文，最多约 8000 字）。"
            "触发场景：'这篇文章讲了什么''详细看看那个链接''把xx页面的内容读出来'。"
            "返回纯文本正文；回复时概括要点，不要整段照搬。"
        ),
        parameters=[
            _tool_param(
                "url",
                ToolParamType.STRING,
                "要抓取的完整网页地址（http/https）",
                required=True,
            ),
        ],
    )
    async def devin_fetch(
        self,
        url: str = "",
        **kwargs: Any,
    ) -> tuple[bool, str, int]:
        url = str(url or "").strip()
        if not url.startswith(("http://", "https://")):
            return False, "url 必须是完整的 http/https 地址。", 0
        from urllib.parse import urlparse

        host = (urlparse(url).hostname or "").strip()
        if _is_private_host(host):
            return False, "该地址指向内网/回环，已拦截（防 SSRF）。", 0
        try:
            text = await asyncio.to_thread(_fetch_page_text, url, self._timeout)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DevinSearch] 抓取失败 %s: %s", url, exc)
            return False, f"抓取失败：{exc}。可能是页面需要登录或有反爬。", 0
        if not text:
            return False, "抓到的页面没有可读正文（可能是纯 JS 页面或图片站）。", 0
        self._fetch_count += 1
        return True, text, 1

    # ---- 管理员命令 ----

    async def _reply(self, content: str, kwargs: dict[str, Any]) -> tuple[bool, str, bool]:
        stream_id = str(kwargs.get("stream_id") or kwargs.get("chat_id") or "").strip()
        if stream_id:
            try:
                await self.ctx.send.text(content, stream_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[DevinSearch] 命令回执发送失败: %s", exc)
        return True, content, True

    @Command(
        "devin_login",
        description="Devin 搜索授权：生成登录链接（管理员）",
        pattern=r"^/devin_login\s*$",
        permission="operator",
    )
    async def cmd_devin_login(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if self._store is None:
            return await self._reply("插件未初始化。", kwargs)
        if self._store.available():
            return await self._reply(
                f"已登录，无需重复授权。\n{self._store.status_text()}", kwargs
            )
        verifier, challenge = devin_client.make_pkce()
        self._login_verifier = verifier
        url = devin_client.auth_url(challenge)
        return await self._reply(
            "Devin 授权流程开始（5分钟内有效）：\n"
            f"1. 浏览器打开：{url}\n"
            "2. 登录 Devin 账号并同意授权\n"
            "3. 复制页面给出的一次性授权码\n"
            "4. 回到这里发送：/devin_code <授权码>\n"
            "取消请发 /devin_cancel",
            kwargs,
        )

    @Command(
        "devin_code",
        description="提交 Devin 一次性授权码（管理员）",
        pattern=r"^/devin_code\s+(?P<code>\S+)\s*$",
        permission="operator",
    )
    async def cmd_devin_code(
        self, matched_groups: dict[str, Any] | None = None, **kwargs: Any
    ) -> tuple[bool, str, bool]:
        if self._store is None:
            return await self._reply("插件未初始化。", kwargs)
        verifier = self._login_verifier
        if not verifier:
            return await self._reply(
                "没有在途的登录流程，请先发送 /devin_login。", kwargs
            )
        code_raw = (matched_groups or {}).get("code") or ""
        try:
            code = devin_client.validate_code(code_raw)
            token = await asyncio.to_thread(devin_client.exchange_code, code, verifier)
        except DevinError as exc:
            self._login_verifier = None
            return await self._reply(f"授权失败：{exc}", kwargs)
        session_token = devin_client.to_devin_session_token(token)
        self._store.save(session_token)
        self._login_verifier = None
        return await self._reply(
            f"登录成功！{self._store.status_text()}\n联网搜索已可用。", kwargs
        )

    @Command(
        "devin_cancel",
        description="取消在途的 Devin 授权（管理员）",
        pattern=r"^/devin_cancel\s*$",
        permission="operator",
    )
    async def cmd_devin_cancel(self, **kwargs: Any) -> tuple[bool, str, bool]:
        self._login_verifier = None
        return await self._reply("已取消在途的 Devin 授权流程。", kwargs)

    @Command(
        "devin_status",
        description="查看 Devin 搜索登录状态（管理员）",
        pattern=r"^/devin_status\s*$",
        permission="operator",
    )
    async def cmd_devin_status(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if self._store is None:
            return await self._reply("插件未初始化。", kwargs)
        pending = "有（等待授权码）" if self._login_verifier else "无"
        return await self._reply(
            f"{self._store.status_text()}\n在途授权流程：{pending}", kwargs
        )

    @Command(
        "devin_logout",
        description="清除 Devin 会话（管理员）",
        pattern=r"^/devin_logout\s*$",
        permission="operator",
    )
    async def cmd_devin_logout(self, **kwargs: Any) -> tuple[bool, str, bool]:
        if self._store is None:
            return await self._reply("插件未初始化。", kwargs)
        self._store.clear()
        self._login_verifier = None
        return await self._reply("已清除本地 Devin 会话，联网搜索停用。", kwargs)

    # ---- WebUI 数据源（页面由 config [webui] 控制开关，API 关时返回 enabled=false）----

    def _record_history(self, query: str, count: int) -> None:
        import time as _t

        self._search_history.append(
            {
                "seq": self._search_count,
                "query": query[:200],
                "results": count,
                "time": _t.strftime("%m-%d %H:%M:%S"),
            }
        )
        self._search_history = self._search_history[-self._history_limit :]

    @API("webui_status", description="登录状态页数据", version="1", public=True)
    async def webui_status(self) -> dict[str, Any]:
        if not self._webui_status:
            return {"enabled": False, "status": "页面未启用"}
        if self._store is None:
            return {"enabled": True, "status": "插件未初始化", "logged_in": False}
        rec = self._store.load() or {}
        import time as _t

        exp_ms = rec.get("expires_at") or 0
        return {
            "enabled": True,
            "logged_in": self._store.available(),
            "status": self._store.status_text(),
            "expires_at": _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(exp_ms / 1000))
            if exp_ms
            else "—",
            "expiry_source": rec.get("expiry_source") or "—",
            "revoked": bool(rec.get("revoked")),
            "pending_login": bool(self._login_verifier),
            "search_count": self._search_count,
            "fetch_count": self._fetch_count,
        }

    @API("webui_history", description="搜索历史页数据", version="1", public=True)
    async def webui_history(self) -> dict[str, Any]:
        if not self._webui_history:
            return {"enabled": False, "history": [], "total": 0}
        return {
            "enabled": True,
            "history": list(reversed(self._search_history)),
            "total": self._search_count,
        }

    @API("webui_logout", description="WebUI 登出按钮", version="1", public=True)
    async def webui_logout(self) -> dict[str, Any]:
        if self._store is None:
            return {"ok": False, "message": "插件未初始化"}
        self._store.clear()
        self._login_verifier = None
        return {"ok": True, "message": "已清除 Devin 会话"}

    @API("webui_clear_history", description="清空搜索历史", version="1", public=True)
    async def webui_clear_history(self) -> dict[str, Any]:
        n = len(self._search_history)
        self._search_history.clear()
        return {"ok": True, "message": f"已清空 {n} 条历史"}




_JINA_PREFIX = "https://r.jina.ai/"

_PRIVATE_NETS = (
    "127.", "0.", "10.", "192.168.", "169.254.",
    "localhost", "::1", "0.0.0.0",
)


def _is_private_host(hostname: str) -> bool:
    """阻断内网/回环地址，防止 devin_fetch 被当 SSRF 跳板。"""
    import ipaddress

    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return True
    if any(host.startswith(p) for p in _PRIVATE_NETS) or host.endswith((".local", ".internal", ".lan")):
        return True
    try:
        ip = ipaddress.ip_address(host)
        return not ip.is_global
    except ValueError:
        pass
    # 172.16-31 与 100.64-127 段（CGNAT）要走 ip_network
    for net in ("172.16.0.0/12", "100.64.0.0/10", "198.18.0.0/15", "192.0.0.0/24", "198.51.100.0/24", "203.0.113.0/24"):
        try:
            if ipaddress.ip_address(host) in ipaddress.ip_network(net):
                return True
        except ValueError:
            pass
    return False


def _fetch_page_text(url: str, timeout: int = 20, max_chars: int = 8000) -> str:
    """r.jina.ai 抓正文（能过多数反爬），直连 trafilatura 兜底。"""
    import urllib.request

    def _get(u: str, accept: str = "text/plain") -> str:
        req = urllib.request.Request(
            u,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
                "Accept": accept,
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(4 * 1024 * 1024).decode("utf-8", errors="replace")

    # 首选 jina reader：自带渲染+反爬，直接吐干净 markdown
    try:
        text = _get(_JINA_PREFIX + url)
        if text.strip():
            return text[:max_chars]
    except Exception:  # noqa: BLE001 — 回退直连
        pass

    html = _get(url, accept="text/html,application/xhtml+xml")

    text = ""
    try:
        import trafilatura

        text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    except Exception:  # noqa: BLE001
        pass
    if not text:
        try:
            from readability import Document
            from bs4 import BeautifulSoup

            text = BeautifulSoup(Document(html).summary(), "html.parser").get_text("\n")
        except Exception:  # noqa: BLE001
            pass
    text = "\n".join(line for line in (l.strip() for l in text.splitlines()) if line)
    return text[:max_chars]


def create_plugin() -> DevinSearchPlugin:
    return DevinSearchPlugin()
