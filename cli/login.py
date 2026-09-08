"""cli/login.py — WorkBuddy CN OAuth 登录（落盘 auth 文件）。

两个子命令（由 login.sh 顺序驱动）：
    login url   → POST /v2/plugin/auth/state?platform=CLI 拿 state+authUrl，打印授权 URL
    login poll  → 读 state，GET /v2/plugin/auth/token?state=，成功再 GET /v2/plugin/login/account，
                  打印完整 token+account JSON

等价于 Go 版 cmd/login/main.go（CN realm only）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time
from typing import Any, Optional, Tuple

import requests

UPSTREAM_BASE_CN = "https://copilot.tencent.com"
CLIENT_UA = "CLI/2.63.2 CodeBuddy/2.63.2"
ORIGIN = "https://www.codebuddy.cn"
ENDPOINT_AUTH_STATE = UPSTREAM_BASE_CN + "/v2/plugin/auth/state?platform=CLI"
ENDPOINT_LOGIN_ACCT = UPSTREAM_BASE_CN + "/v2/plugin/login/account?state="
ENDPOINT_AUTH_TOKEN = UPSTREAM_BASE_CN + "/v2/plugin/auth/token?state="
STATE_FILE = os.path.join(tempfile.gettempdir(), "wb2api-login-state.json")


def common_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": CLIENT_UA,
    }


def do_json(client: requests.Session, method: str, full_url: str, body: Any = None
            ) -> Tuple[Optional[Any], int, Optional[str]]:
    headers = common_headers()
    try:
        resp = client.request(method, full_url, headers=headers, json=body, timeout=30)
    except requests.RequestException as e:
        return None, 0, f"transport: {e}"
    raw = resp.text
    if resp.status_code >= 400:
        return None, resp.status_code, f"http_error: upstream {resp.status_code}"
    if resp.status_code >= 300:
        return None, resp.status_code, f"http_error: upstream redirect {resp.status_code}"
    try:
        env = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None, resp.status_code, "parse failed"
    code = env.get("code", 0)
    if code != 0:
        return None, resp.status_code, f"code={code} msg={env.get('msg','')}"
    return env.get("data"), resp.status_code, None


def fatal(msg: str) -> None:
    print(f"login: {msg}", file=sys.stderr)
    sys.exit(1)


def jwt_payload(access_token: str) -> dict:
    """从 JWT access_token 提取 payload（不校验签名，仅做 uid/nickname 兜底）。

    新 realm 下 /v2/plugin/login/account 可能不返回 uid，但 access_token 的
    payload 里通常有 sub（用户 id）与 nickname，作为 fallback 来源。
    """
    if not access_token:
        return {}
    parts = access_token.split(".")
    if len(parts) != 3:
        return {}
    try:
        pad = "=" * (-len(parts[1]) % 4)
        raw = base64.urlsafe_b64decode(parts[1] + pad)
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa
        return {}


def save_nested(out: dict, auth_dir: str) -> Optional[str]:
    """把 poll 的扁平下划线结果转成 auth.py 可读的嵌套驼峰格式，落盘 auth_dir/workbuddy-<uid>.json。

    login.py poll 输出的字段（access_token/refresh_token/expires_in/enterprise_id 下划线命名）
    与 auth.py 期望的嵌套驼峰格式（auth.accessToken / account.uid）不一致，这里统一做转换。
    """
    uid = out.get("uid") or ""
    if not uid:
        return None
    expires_at = int(time.time()) + int(out.get("expires_in") or 0)
    doc = {
        "account": {
            "uid": uid,
            "enterpriseId": out.get("enterprise_id") or "",
            "nickname": out.get("nickname") or "",
        },
        "auth": {
            "accessToken": out.get("access_token") or "",
            "refreshToken": out.get("refresh_token") or "",
            "expiresAt": expires_at,
            "domain": out.get("domain") or "",
        },
    }
    os.makedirs(auth_dir, exist_ok=True)
    fp = os.path.join(auth_dir, f"workbuddy-{uid}.json")
    with open(fp, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)
    return fp


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sub", nargs="?", choices=["url", "poll"], help="url | poll")
    ap.add_argument("--save", metavar="DIR", default="",
                    help="poll 成功后把凭证转成嵌套驼峰格式落盘到 DIR/workbuddy-<uid>.json")
    args = ap.parse_args()
    if not args.sub:
        fatal("usage: login <url|poll> [--save DIR]")

    client = requests.Session()

    if args.sub == "url":
        data, _, err = do_json(client, "POST", ENDPOINT_AUTH_STATE, body={})
        if err:
            fatal(f"auth state failed: {err}")
        if not isinstance(data, dict) or not data.get("state") or not data.get("authUrl"):
            fatal("auth state: missing state or authUrl")
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            f.write(json.dumps({"state": data["state"]}))
        print(data["authUrl"])
        return 0

    # poll
    if not os.path.exists(STATE_FILE):
        fatal(f"read state: no such file (先跑 login url)")
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        ls = json.load(f)
    state = ls.get("state", "")
    tok_raw, status, err = do_json(client, "GET", ENDPOINT_AUTH_TOKEN + state)
    if err:
        if status == 0 or status >= 500:
            fatal(f"token endpoint error: {err}")
        fatal("登录未完成（waiting for login）。请确认已在浏览器完成登录再按 y")
    if not isinstance(tok_raw, dict) or not tok_raw.get("accessToken"):
        fatal("登录未完成（waiting for login）。请确认已在浏览器完成登录再按 y")

    tok = tok_raw
    acct: dict = {}
    acct_headers = dict(common_headers())
    acct_headers["Authorization"] = "Bearer " + tok["accessToken"]
    acct_raw, _, acct_err = do_json(client, "GET", ENDPOINT_LOGIN_ACCT + state, None)
    if acct_err is None and isinstance(acct_raw, dict):
        acct = acct_raw

    # fallback：account 端点拿不到 uid/nickname 时，从 access_token 的 JWT payload 兜底。
    if not (acct.get("uid") or "").strip():
        claims = jwt_payload(tok.get("accessToken") or "")
        if claims.get("sub"):
            acct["uid"] = claims["sub"]
        if not (acct.get("nickname") or "").strip() and claims.get("nickname"):
            acct["nickname"] = claims["nickname"]

    out = {
        "access_token": tok.get("accessToken"),
        "refresh_token": tok.get("refreshToken"),
        "expires_in": tok.get("expiresIn"),
        "domain": tok.get("domain"),
        "uid": acct.get("uid"),
        "enterprise_id": acct.get("enterpriseId"),
        "nickname": acct.get("nickname"),
    }
    print(json.dumps(out, ensure_ascii=False))
    if args.save:
        fp = save_nested(out, args.save)
        if fp:
            print(f"login: 已保存凭证 -> {fp}", file=sys.stderr)
        else:
            print("login: 未获取到 uid，无法落盘（token 可能无效）", file=sys.stderr)
    try:
        os.remove(STATE_FILE)
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
