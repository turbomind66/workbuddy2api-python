"""projpath.py — 路径锚定。

把所有 CLI 入口用到的相对路径统一解析到**项目根**，而不是当前工作目录（cwd）。

背景：Go 版用 `./auths`、`./data/state.json` 这类相对路径，Go 程序通常从项目根启动所以没问题；
Python 版用户经常 `cd cli` 后直接 `server.py` / `credit.py`，相对路径就变成了 `cli/auths`，
结果是「找不到任何账号」却没有任何报错（pool 加载 0 账号 / credit 输出空列表），极难排查。

统一策略：
  - 绝对路径：原样使用。
  - 相对路径：先按 cwd 找，找到就用；找不到就回退到项目根。
    这样既尊重显式传入的路径，又保证从任意目录启动都能找到项目自带的 auths/config/data。

等价于 Go 版「约定从项目根启动」的显式化。
"""
from __future__ import annotations

import os
import sys


def project_root() -> str:
    """项目根 = 本文件（wb2api/projpath.py）的上两级目录。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_ROOT = project_root()


def resolve(p: str, root: str = "") -> str:
    """把相对路径锚定到项目根（不做 cwd 探测）。"""
    if not p:
        return p
    if os.path.isabs(p):
        return p
    return os.path.join(root or PROJECT_ROOT, p)


def resolve_path(p: str, root: str = "", is_dir: bool = False) -> str:
    """相对路径：优先 cwd，不存在则回退项目根。"""
    if not p:
        return resolve(p, root)
    if os.path.isabs(p):
        return p
    cwd_path = os.path.abspath(p)
    ok = os.path.isdir if is_dir else os.path.exists
    if ok(cwd_path):
        return cwd_path
    return resolve(p, root)


def ensure_importable(root: str = "") -> str:
    """把项目根加入 sys.path，保证 `py cli/credit.py` 直接运行时能 import wb2api。"""
    r = root or PROJECT_ROOT
    if r not in sys.path:
        sys.path.insert(0, r)
    return r
