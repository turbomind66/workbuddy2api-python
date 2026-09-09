# -*- coding: utf-8 -*-
"""check_closures.py — 静态扫描「闭包内赋值导致 UnboundLocalError」的隐患。

背景（本项目真实踩过）：
    def outer():
        sticky_uid = ""
        def fail(uid):
            if sticky_uid:        # 读
                ...
            sticky_uid = ""       # 写 → sticky_uid 被判为 fail 的局部变量，
                                  #      上面的读抛 UnboundLocalError

Python 规则：函数内只要有对某名字的赋值，该名字在整个函数内都算局部变量。
Go 没有这条规则（:= 与 = 作用域不同），所以 Go→Python 移植极易踩。

检测口径：
  内层函数中「既有 Load 又有 Store」，且该名字在外层函数（或模块）作用域被绑定过，
  且内层没有 nonlocal/global 声明 → 报告。

静态上无法判断 Load 是否一定早于 Store（循环/分支），故一律视为风险，宁可多看一眼。

用法：python scripts/check_closures.py [目录...]
退出码：0 无风险；1 发现风险。
"""
from __future__ import annotations

import ast
import os
import sys

# 不进入的嵌套作用域（内层 def/lambda/class 有自己的作用域，单独递归处理）
_SKIP = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
         ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _iter_local(node):
    """遍历 node 的直接作用域，跳过嵌套作用域。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SKIP):
            continue
        yield child


def _walk_local(node):
    """遍历 node 的直接作用域：嵌套 def/class/lambda 本身会被 yield（供上层递归处理），
    但不深入其内部的语句。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, _SKIP):
            yield child  # 只给出作用域节点本身，不 descend
            continue
        yield child
        for sub in _walk_local(child):
            yield sub


def _bound_names(fn) -> set:
    """函数自身作用域内绑定的名字（赋值/参数/for target/with as/except as/import/嵌套 def 名）。"""
    names = set()
    a = fn.args
    for arg in list(getattr(a, "posonlyargs", [])) + list(a.args) + list(a.kwonlyargs):
        names.add(arg.arg)
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)

    for n in _walk_local(fn):
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            names.add(n.name)
        elif isinstance(n, ast.Import):
            names.update((al.asname or al.name).split(".")[0] for al in n.names)
        elif isinstance(n, ast.ImportFrom):
            names.update(al.asname or al.name for al in n.names)
        elif isinstance(n, ast.Global) or isinstance(n, ast.Nonlocal):
            names.update(n.names)
    return names


def _name_uses(fn):
    """返回 (loads, stores)：名字 -> 最早出现行号（仅统计函数自身作用域）。

    保留行号用于判断「读取是否早于赋值」——只有读取早于（或同于）赋值时才会
    真正触发 UnboundLocalError；`x = 1; print(x)` 这类合法遮蔽不该报警。
    """
    loads: dict = {}
    stores: dict = {}
    for n in _walk_local(fn):
        if not isinstance(n, ast.Name):
            continue
        lineno = getattr(n, "lineno", 0)
        if isinstance(n.ctx, ast.Load):
            loads[n.id] = min(loads.get(n.id, 10 ** 9), lineno)
        elif isinstance(n.ctx, (ast.Store, ast.Del)):
            stores[n.id] = min(stores.get(n.id, 10 ** 9), lineno)
    return loads, stores


def _declared(fn) -> set:
    """函数内显式 global/nonlocal 声明的名字（只看自身作用域）。"""
    names = set()
    for n in _walk_local(fn):
        if isinstance(n, (ast.Global, ast.Nonlocal)):
            names.update(n.names)
    return names


def _module_bound(tree) -> set:
    """模块顶层绑定的名字（def/class/import/赋值）。"""
    names = set()
    for n in _walk_local(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            names.add(n.id)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(n.name)
        elif isinstance(n, ast.Import):
            names.update((al.asname or al.name).split(".")[0] for al in n.names)
        elif isinstance(n, ast.ImportFrom):
            names.update(al.asname or al.name for al in n.names)
    return names


def check_file(path: str):
    """返回 [(lineno, name)] 风险列表。"""
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    try:
        tree = ast.parse(src, filename=path)
    except SyntaxError as e:
        print(f"  ! 语法错误，跳过 {path}: {e}", file=sys.stderr)
        return []

    risks = []
    mod_bound = _module_bound(tree)

    def visit(fn, outer_bound: set):
        for n in _walk_local(fn):
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            loads, stores = _name_uses(n)
            declared = _declared(n)
            # 「先读后写」的嫌疑名字：读取行号 <= 赋值行号 才是真的会炸
            risky = {nm for nm, ln in loads.items()
                     if nm in stores and ln <= stores[nm]
                     and nm not in declared
                     and (nm in outer_bound or nm in mod_bound)}
            if risky:
                for nm in sorted(risky):
                    risks.append((n.lineno, n.name, nm))
            # 递归：内层的内层，其外层作用域 = 外层 + 本层绑定
            visit(n, outer_bound | _bound_names(fn))

    for n in _walk_local(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            # 顶层函数的外层作用域 = 模块作用域 + 自身绑定
            visit(n, mod_bound | _bound_names(n))
    return risks


def main() -> int:
    roots = sys.argv[1:] or ["wb2api", "cli"]
    found = 0
    for root in roots:
        if os.path.isfile(root):
            files = [root]
        else:
            files = []
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames
                               if d not in ("__pycache__", ".venv", "venv", ".git")]
                for fn in filenames:
                    if fn.endswith(".py"):
                        files.append(os.path.join(dirpath, fn))
        for path in sorted(files):
            risks = check_file(path)
            for lineno, fname, nm in risks:
                found += 1
                print(f"RISK {path}:{lineno}  {fname}() 中对 '{nm}' 既有读取又有赋值，"
                      f"却未声明 nonlocal/global —— 可能 UnboundLocalError")
    if found:
        print(f"\n发现 {found} 处闭包作用域风险（如确认安全，可加 nonlocal 或重命名局部变量）")
        return 1
    print("CLOSURE_CHECK_OK: 未发现闭包作用域风险")
    return 0


if __name__ == "__main__":
    sys.exit(main())
