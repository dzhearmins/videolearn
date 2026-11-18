"""IAAA 统一身份认证 → 教学网 SSO 会话。

流程（与网页端一致，已用真实页面/JS 核对）：
1. GET  /iaaa/getPublicKey.do                 取 RSA 公钥与 keyVersion
2. POST /iaaa/oauthlogin.do                   密码用 RSA PKCS#1 v1.5 加密后提交
3. GET  .../campusLogin?_rand=..&token=..     换取 course.pku.edu.cn 的会话 Cookie
4. GET  tabAction?tab_tab_group_id=_1_1       验证确实已登录（不再跳转登录页）

只用标准库 + cryptography；不打印密码、token 或 Cookie。
"""

from __future__ import annotations

import base64
import random

import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from .session import AuthError, BBClient

IAAA_PUBKEY = "https://iaaa.pku.edu.cn/iaaa/getPublicKey.do"
IAAA_LOGIN = "https://iaaa.pku.edu.cn/iaaa/oauthlogin.do"
SSO_LOGIN = "https://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
OAUTH_REDIR = "http://course.pku.edu.cn/webapps/bb-sso-BBLEARN/execute/authValidate/campusLogin"
BB_HOME = "https://course.pku.edu.cn/webapps/portal/execute/tabs/tabAction"
BB_LOGIN = "https://course.pku.edu.cn/webapps/login/"


def _get_public_key(client: BBClient) -> tuple[str, str]:
    r = client.get(IAAA_PUBKEY)
    r.raise_for_status()
    data = r.json()
    if not data.get("success"):
        raise AuthError("IAAA 公钥获取失败")
    return data["key"], str(data.get("version", ""))


def rsa_encrypt_password(password: str, pem: str) -> str:
    """等价于网页端 JSEncrypt.encrypt：RSA PKCS#1 v1.5 + Base64。"""
    pub = serialization.load_pem_public_key(pem.encode())
    ciphertext = pub.encrypt(password.encode("utf-8"), padding.PKCS1v15())
    return base64.b64encode(ciphertext).decode("ascii")


def is_authenticated(client: BBClient) -> bool:
    """访问教学网主页，未登录会被 302 到 /webapps/login/。"""
    try:
        r = client.get(
            BB_HOME,
            params={"tab_tab_group_id": "_1_1"},
            allow_redirects=False,
        )
    except requests.RequestException:
        return False
    if r.status_code in (301, 302, 303, 307, 308):
        return "login" not in (r.headers.get("Location") or "").lower()
    if r.status_code != 200:
        return False
    body = r.text
    return "loginPageContainer" not in body and 'id="loginForm"' not in body and "登录教学网" not in body


def login(client: BBClient, username: str, password: str) -> None:
    if not username or not password:
        raise AuthError("学号或密码为空")

    pem, key_version = _get_public_key(client)
    encrypted = rsa_encrypt_password(password, pem)

    # 注意：任何日志都不应包含 encrypted / password / token
    r = client.post(
        IAAA_LOGIN,
        data={
            "appid": "blackboard",
            "userName": username,
            "password": encrypted,
            "keyVersion": key_version,
            "randCode": "",
            "smsCode": "",
            "otpCode": "",
            "redirUrl": OAUTH_REDIR,
        },
        headers={"Origin": "https://iaaa.pku.edu.cn", "Referer": "https://iaaa.pku.edu.cn/iaaa/oauth.jsp"},
    )
    try:
        data = r.json()
    except ValueError as e:
        raise AuthError(f"IAAA 返回了非 JSON 响应（HTTP {r.status_code}）") from e

    if not data.get("success"):
        err = data.get("errors") or {}
        code = err.get("code", "?")
        msg = err.get("msg", data.get("msg", "未知错误"))
        hints = {
            "E01": "密码错误或密码加密方式不被接受",
            "E02": "账号不存在或被锁定",
            "E05": "手机令牌验证码错误",
            "E21": "尝试次数过多，请半小时后再试",
        }
        hint = hints.get(code, "")
        raise AuthError(f"IAAA 登录失败 [{code}] {msg}" + (f"（{hint}）" if hint else ""))

    token = data.get("token")
    if not token:
        raise AuthError("IAAA 登录成功但没有返回 token")

    _rand = f"{random.random():.20f}"
    r = client.get(SSO_LOGIN, params={"_rand": _rand, "token": token})
    if r.status_code >= 400:
        raise AuthError(f"SSO 回调失败（HTTP {r.status_code}）")

    if not is_authenticated(client):
        raise AuthError(
            "SSO 回调完成但教学网会话未建立。可能是 IAAA 登录成功但账号无教学网权限，"
            "或回调需要额外的二步验证。"
        )

    client.save_cookies()


def ensure_login(client: BBClient, username: str, password: str, force: bool = False) -> None:
    """优先复用 .state/cookies.json，失效则重新登录。"""
    if not force and client.load_cookies() and is_authenticated(client):
        return
    login(client, username, password)
