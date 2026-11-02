"""Devin 会话与联网搜索客户端。

移植自 dsh-devin-search（MIT, https://github.com/mimimaster/dsh-devin-search）：

- OAuth：POST https://api.devin.ai/auth/cli/token 用一次性授权码 + PKCE
  code_verifier 换 session token；授权 URL 由 plugin.py 生成。
- 搜索：POST {host}/exa.api_server_pb.ApiServerService/GetWebSearchResults，
  双 host 容灾（server.codeium.com → server.self-serve.windsurf.com）。
- token 规范化为 `devin-session-token$<raw>` 后才被后端接受。
- 凭据落盘 plugin 目录下 devin_session.json（0600），不进任何对话上下文。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import stat
import time
import urllib.error
import urllib.request
from typing import Any, Optional
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

WEB_BASE = "https://app.devin.ai"
API_BASE = "https://api.devin.ai"
WEB_PATH = "/exa.api_server_pb.ApiServerService/GetWebSearchResults"
HOSTS = ("https://server.codeium.com", "https://server.self-serve.windsurf.com")

DEVIN_SESSION_TOKEN_PREFIX = "devin-session-token$"
# 无 exp 的 opaque session 与上游一致按 365 天记
FALLBACK_EXPIRES_MS = 365 * 24 * 60 * 60 * 1000

MAX_QUERY_LEN = 8192
MAX_RESULTS_BODY = 1024 * 1024


class DevinError(Exception):
    """code: login | network | protocol | bounds"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def to_devin_session_token(token: str) -> str:
    raw = (token or "").strip()
    if not raw:
        return raw
    if raw.startswith(DEVIN_SESSION_TOKEN_PREFIX) or raw.startswith("sk-ws-"):
        return raw
    return f"{DEVIN_SESSION_TOKEN_PREFIX}{raw}"


def token_expiry(token: str, now_ms: Optional[int] = None) -> tuple[int, str]:
    """返回 (expires_at_ms, 'jwt'|'fallback')。"""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    try:
        seg = token.split(".")[1]
        pad = "=" * (-len(seg) % 4)
        body = json.loads(base64.urlsafe_b64decode(seg + pad).decode("utf-8"))
        exp = body.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            return int(exp * 1000) - 60_000, "jwt"
    except Exception:  # noqa: BLE001 — opaque token 走 fallback
        pass
    return now + FALLBACK_EXPIRES_MS, "fallback"


def make_pkce() -> tuple[str, str]:
    """生成 (code_verifier, code_challenge)。"""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def auth_url(challenge: str, state: Optional[str] = None) -> str:
    """授权页 URL（无 redirect_uri 的 code 回填模式）。"""
    params = {
        "state": state or base64.urlsafe_b64encode(secrets.token_bytes(32))
        .rstrip(b"=")
        .decode(),
        "prompt": "select_account",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{WEB_BASE}/auth/cli/continue?{urlencode(params)}"


def validate_code(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 8192:
        raise DevinError("bounds", "授权码格式无效")
    code = value.strip()
    if not code or any(ord(c) <= 0x20 or ord(c) == 0x7F for c in code):
        raise DevinError("bounds", "授权码格式无效")
    return code


def _post_json(url: str, payload: dict, headers: dict, timeout: int) -> tuple[int, bytes]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"content-type": "application/json", "accept": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(MAX_RESULTS_BODY + 1)
            if len(data) > MAX_RESULTS_BODY:
                raise DevinError("bounds", "Devin 响应超过大小上限")
            return resp.status, data
    except urllib.error.HTTPError as exc:
        try:
            exc.read()
        except Exception:  # noqa: BLE001
            pass
        return exc.code, b""
    except Exception as exc:  # noqa: BLE001
        raise DevinError("network", f"Devin 网络请求失败: {exc}") from exc


def exchange_code(code: str, verifier: str, timeout: int = 30) -> str:
    """授权码 + verifier → session token（raw JWT 或 opaque）。"""
    status, data = _post_json(
        f"{API_BASE}/auth/cli/token",
        {"code": code, "code_verifier": verifier},
        {},
        timeout,
    )
    if status != 200:
        raise DevinError("network", f"授权码兑换失败（HTTP {status}），请重新走 /devin_login")
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise DevinError("protocol", "授权码兑换返回非 JSON") from exc
    token = payload.get("token")
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise DevinError("protocol", "授权码兑换未返回有效会话")
    return token


class SessionStore:
    """plugin 目录下的 devin_session.json，0600。落盘即 windsurf 形态 token。"""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> Optional[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DevinSearch] 会话文件损坏: %s", exc)
            return None
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("token"), str)
            or not data["token"]
            or not isinstance(data.get("expires_at"), (int, float))
        ):
            return None
        return data

    def save(self, token: str) -> dict:
        expires_at, source = token_expiry(token)
        record = {
            "version": 1,
            "token": token,
            "expires_at": expires_at,
            "expiry_source": source,
            "revoked": False,
        }
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, self.path)
        return record

    def mark_revoked(self) -> None:
        rec = self.load()
        if rec:
            rec["revoked"] = True
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(rec, fh)
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp, self.path)

    def clear(self) -> None:
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def available(self) -> bool:
        rec = self.load()
        return bool(
            rec
            and not rec.get("revoked")
            and float(rec.get("expires_at", 0)) > time.time() * 1000
        )

    def status_text(self) -> str:
        rec = self.load()
        if not rec:
            return "未登录（无会话文件）"
        if rec.get("revoked"):
            return "会话被服务端拒绝，请重新 /devin_login"
        ok = float(rec.get("expires_at", 0)) > time.time() * 1000
        exp = time.strftime(
            "%Y-%m-%d %H:%M:%S", time.localtime(float(rec["expires_at"]) / 1000)
        )
        head = "已登录" if ok else "会话已过期，请重新 /devin_login"
        return f"{head}。到期 {exp}（{rec.get('expiry_source', '?')}；不自动续期）"


class DevinSearchClient:
    """web_search 执行体：一次 Connect-RPC JSON POST，双 host 容灾。"""

    def __init__(self, store: SessionStore, timeout: int = 20) -> None:
        self.store = store
        self.timeout = timeout

    def _token(self) -> str:
        rec = self.store.load()
        if not rec or rec.get("revoked") or float(rec.get("expires_at", 0)) <= time.time() * 1000:
            raise DevinError("login", "未登录或会话已失效；请管理员执行 /devin_login")
        return to_devin_session_token(rec["token"])

    def search(self, query: str, max_results: int = 5) -> dict:
        query = (query or "").strip()
        if not query or len(query) > MAX_QUERY_LEN:
            raise DevinError("bounds", "查询为空或过长")
        limit = max(1, min(10, int(max_results or 5)))
        token = self._token()

        tried = rejected = 0
        for host in HOSTS:
            tried += 1
            try:
                status, data = _post_json(
                    f"{host}{WEB_PATH}",
                    {
                        "metadata": {
                            "apiKey": token,
                            "ideName": "windsurf",
                            "ideVersion": "1.9600.41",
                            "extensionName": "windsurf",
                            "extensionVersion": "1.9600.41",
                            "locale": "en",
                        },
                        "query": query,
                        "limit": limit,
                    },
                    {
                        "connect-protocol-version": "1",
                        "user-agent": "windsurf/1.9600.41",
                    },
                    self.timeout,
                )
            except DevinError:
                continue
            if status in (401, 403):
                rejected += 1
                continue
            if status != 200:
                continue
            try:
                payload = json.loads(data.decode("utf-8"))
            except Exception as exc:  # noqa: BLE001
                raise DevinError("protocol", "Devin 搜索返回非 JSON") from exc
            results = payload.get("results")
            if not isinstance(results, list):
                raise DevinError("protocol", "Devin 搜索返回结构异常")

            sources: list[dict] = []
            valid = 0
            for row in results:
                if not isinstance(row, dict):
                    continue
                url = ""
                for key in ("url", "sourceUrl", "webUrl", "link"):
                    v = row.get(key)
                    if isinstance(v, str) and v.strip():
                        url = v.strip()[:4096]
                        break
                if not url.startswith(("https://", "http://")):
                    continue
                valid += 1
                if len(sources) >= limit:
                    continue
                title = snippet = ""
                for key in ("title", "name", "webTitle"):
                    v = row.get(key)
                    if isinstance(v, str) and v.strip():
                        title = v.strip()[:512]
                        break
                for key in ("snippet", "summary", "text", "content"):
                    v = row.get(key)
                    if isinstance(v, str) and v.strip():
                        snippet = v.strip()[:4096]
                        break
                sources.append({"url": url, "title": title, "snippet": snippet})
            return {"sources": sources, "truncated": valid > limit}

        if tried and rejected == tried:
            self.store.mark_revoked()
            raise DevinError("login", "Devin 会话被服务端拒绝；请重新 /devin_login")
        raise DevinError("network", "Devin 搜索在所有节点上均失败")
