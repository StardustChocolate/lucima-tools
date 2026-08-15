"""EROLABS 门户登录 —— 用账号密码(+Turnstile 验证码 token)换取 erolabsjwt。

erolabsjwt 即游戏客户端的 ?token=，也是 GameClient 需要的 game_token。
"""
from __future__ import annotations

import base64

import httpx

from . import config


class PortalError(Exception):
    pass


def portal_login(account: str, password: str, turnstile_token: str | None = None) -> dict:
    """门户登录。返回 {jwt, nickname, userId, coins, ...}。

    password 会按门户要求 base64 编码。turnstile_token 为前端 Turnstile 组件产出。
    """
    data = {
        "account": account,
        "password": base64.b64encode(password.encode("utf-8")).decode("ascii"),
    }
    if turnstile_token:
        # 门户把验证码 token 作为 turnstileToken 一并提交
        data["turnstileToken"] = turnstile_token

    # 代理跟着门户站点走：默认入口(game.ero-labs.tech)可直连，不套代理。
    proxy = (config.get_proxy() or None) if config.PORTAL_NEEDS_PROXY else None
    with httpx.Client(proxy=proxy, timeout=config.HTTP_TIMEOUT) as client:
        resp = client.post(
            config.PORTAL_LOGIN,
            data=data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Origin": config.PORTAL_SITE,
                "Referer": config.PORTAL_LOGIN_PAGE,
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0 Safari/537.36",
            },
        )
        try:
            body = resp.json()
        except Exception:
            raise PortalError(f"门户返回非 JSON (HTTP {resp.status_code}): {resp.text[:200]}")

    if body.get("status") != "SUCCESS":
        raise PortalError(body.get("message") or f"登录失败: {body}")
    d = body.get("data", {})
    if not d.get("jwt"):
        raise PortalError("门户未返回 jwt")
    return d
