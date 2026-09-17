"""精简 EROLABS V2 登录。

流程与 Android SDK 的 V2 分支一致：验证码预检取得 hashCode，再用账号密码换取
userId/accessToken。密码只存在于本次调用内存中，不写日志、不持久化。
"""
from __future__ import annotations

import base64
import json
import time

import httpx

from . import config
from .logutil import log


class PortalError(Exception):
    pass


_RETRY_STATUS = {502, 503, 504}


def _headers(device_id: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": config.PORTAL_V2_SITE,
        "Referer": f"{config.PORTAL_V2_SITE}/login/loginPage",
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
            "Chrome/125.0 Mobile Safari/537.36"
        ),
        "deviceid": device_id,
        "Authorization": "Bearer",
    }


def _response_json(response: httpx.Response, stage: str) -> dict:
    try:
        body = response.json()
    except Exception as error:
        raise PortalError(f"{stage}返回非 JSON（HTTP {response.status_code}）") from error
    if not isinstance(body, dict):
        raise PortalError(f"{stage}响应格式无效")
    if response.is_error:
        message = body.get("message") or body.get("msg") or f"HTTP {response.status_code}"
        raise PortalError(f"{stage}失败：{message}")
    return body


def _post_json(
    client: httpx.Client,
    url: str,
    payload: dict | None,
    headers: dict[str, str],
    timeout: float,
    stage: str,
    retry: bool = True,
) -> dict:
    last_error: Exception | None = None
    attempts = 2 if retry else 1
    for attempt in range(attempts):
        try:
            log.info("[portal->] %s request body suppressed (login)", stage)
            kwargs = {"headers": headers, "timeout": timeout}
            if payload is not None:
                kwargs["json"] = payload
            response = client.post(url, **kwargs)
            if response.status_code in _RETRY_STATUS and attempt + 1 < attempts:
                log.info("[portal<-] %s HTTP %s response body suppressed (login), retrying",
                         stage, response.status_code)
                continue
            log.info("[portal<-] %s HTTP %s response body suppressed (login), %d bytes",
                     stage, response.status_code, len(getattr(response, "content", b"")))
            return _response_json(response, stage)
        except (httpx.ConnectError, httpx.TimeoutException) as error:
            last_error = error
            if attempt + 1 < attempts:
                continue
    raise PortalError(f"{stage}网络请求失败：{last_error}") from last_error


def access_token_expires_at(token: str) -> float | None:
    """只读取 JWT 的公开 exp 元数据，不验证或记录 Token 内容。"""
    try:
        payload = str(token or "").split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload))
        return float(value["exp"])
    except (IndexError, KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None


def access_token_needs_refresh(token: str, *, leeway: float = 5 * 60,
                               now: float | None = None) -> bool:
    """JWT 将在 leeway 秒内到期时续期；未知格式交给游戏登录失败兜底。"""
    expires_at = access_token_expires_at(token)
    return expires_at is not None and expires_at <= (time.time() if now is None else now) + leeway


def portal_login_v2(account: str, password: str, device_id: str) -> dict:
    """账号密码换取 V2 Token，返回后端内部使用的规范化认证记录。"""
    account = (account or "").strip()
    if not account or not password:
        raise PortalError("账号和密码不能为空")
    if not device_id:
        raise PortalError("设备 ID 不能为空")

    proxy = config.get_proxy() or None
    with httpx.Client(proxy=proxy) as client:
        headers = _headers(device_id)
        captcha = _post_json(
            client,
            config.PORTAL_V2_CAPTCHA,
            {"captcha": [0, 0, 0, 1, 0]},
            headers,
            config.PORTAL_CAPTCHA_TIMEOUT,
            "验证码预检",
        )
        try:
            hash_code = int((captcha.get("data") or {})["hashCode"])
        except (KeyError, TypeError, ValueError) as error:
            raise PortalError("验证码预检响应缺少 hashCode") from error

        encoded_password = base64.b64encode(password.encode("utf-8")).decode("ascii")
        login = _post_json(
            client,
            config.PORTAL_V2_LOGIN,
            {
                "account": account,
                "password": encoded_password,
                "gameId": config.EROLABS_GAME_ID,
                "hashCode": hash_code,
            },
            headers,
            config.PORTAL_LOGIN_TIMEOUT,
            "门户登录",
        )

    data = login.get("data") or {}
    access_token = str(data.get("accessToken") or "")
    user_id = str(data.get("userId") or "")
    if not access_token or not user_id:
        message = login.get("message") or login.get("msg")
        raise PortalError(str(message or "门户登录响应缺少 accessToken/userId"))
    return {
        "kind": "erolabs-v2",
        "user_id": user_id,
        "access_token": access_token,
        "refresh_token": str(data.get("refreshToken") or ""),
        "device_id": device_id,
        "saved_at": int(time.time()),
    }


def portal_refresh_v2(auth: dict) -> dict:
    """使用 Refresh Token 无感换取一组新 Token；请求与响应正文均不落日志。"""
    refresh_token = str(auth.get("refresh_token") or "")
    device_id = str(auth.get("device_id") or "")
    if not refresh_token:
        raise PortalError("本地凭证没有 Refresh Token")
    if not device_id:
        raise PortalError("本地凭证没有设备 ID")

    headers = _headers(device_id)
    headers["Authorization"] = f"Bearer {refresh_token}"
    proxy = config.get_proxy() or None
    with httpx.Client(proxy=proxy) as client:
        result = _post_json(
            client,
            config.PORTAL_V2_REFRESH,
            None,
            headers,
            config.PORTAL_REFRESH_TIMEOUT,
            "登录令牌续期",
            retry=False,
        )

    data = result.get("data") or {}
    access_token = str(data.get("accessToken") or "")
    new_refresh_token = str(data.get("refreshToken") or refresh_token)
    if not access_token:
        message = result.get("message") or result.get("msg")
        raise PortalError(str(message or "登录令牌续期响应缺少 accessToken"))
    return {
        **auth,
        "kind": "erolabs-v2",
        "access_token": access_token,
        "refresh_token": new_refresh_token,
        "device_id": device_id,
        "saved_at": int(time.time()),
    }
