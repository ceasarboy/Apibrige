"""BI 计算字段表达式引擎（add-bi-calc-fields）

设计（2026-09-27 拍板）：
- 引用语法 ``[中文显示名]``：优先按中文显示名解析为字段键，回落行键（path）本身；
  两者都不命中 → 解析期报错（不静默产出空列）
- 结构：引用 + 四则运算 + 括号 + 数值/字符串字面量 + 白名单函数
- 求值：AST 白名单遍历（**禁止 eval**）；单行缺值/不可转数 → 该行结果为 None
  （与聚合器「跳过不可转数值行」语义一致，不编造 0）
- 循环防护：派生列之间按引用关系建图，``detect_cycle`` 拓扑检测报环路径

函数白名单（14）：
    数值  round(x[, d]) abs(x) ceil(x) floor(x) min(..) max(..) coalesce(..)
    字符串 substring(s, start[, len])（SQL 风格 1-based） upper(s) lower(s) trim(s) concat(..)
    日期  date_part(s, 'year'|'month'|'day')（ISO 日期字符串取分量）
"""
from __future__ import annotations

import ast
import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

_REF_RE = re.compile(r"\[(.*?)]")


class ExprError(ValueError):
    """表达式解析/校验错误（校验层转 422 文案）"""


@dataclass
class CompiledExpr:
    """编译后的表达式：safe 名 → 字段键的引用表 + 可求值 AST"""
    raw: str
    node: ast.Expression
    refs: dict[str, str] = field(default_factory=dict)   # safe 名(__r0) → 字段键
    names: dict[str, str] = field(default_factory=dict)  # safe 名 → 原始引用名（报错用）


# ---------------------------------------------------------------------------
# 函数表（白名单；全部容错：入参类型不合法返回 None，由行级容错接住）
# ---------------------------------------------------------------------------

def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (ValueError, TypeError):
        return None


def _text(v: Any) -> str | None:
    return None if v is None else str(v)


def _fn_round(x: Any, d: Any = 0) -> float | None:
    n = _num(x)
    digits = _num(d)
    if n is None or digits is None:
        return None
    return round(n, int(digits))


def _fn_abs(x: Any) -> float | None:
    n = _num(x)
    return None if n is None else abs(n)


def _fn_ceil(x: Any) -> float | None:
    n = _num(x)
    return None if n is None else float(math.ceil(n))


def _fn_floor(x: Any) -> float | None:
    n = _num(x)
    return None if n is None else float(math.floor(n))


def _fn_min(*args: Any) -> float | None:
    nums = [_num(a) for a in args]
    nums = [n for n in nums if n is not None]
    return min(nums) if nums else None


def _fn_max(*args: Any) -> float | None:
    nums = [_num(a) for a in args]
    nums = [n for n in nums if n is not None]
    return max(nums) if nums else None


def _fn_coalesce(*args: Any) -> Any:
    for a in args:
        if a is None:
            continue
        if isinstance(a, str) and not a.strip():
            continue
        return a
    return None


def _fn_substring(s: Any, start: Any, length: Any = None) -> str | None:
    t = _text(s)
    st = _num(start)
    if t is None or st is None:
        return None
    begin = max(0, int(st) - 1)  # SQL 风格 1-based
    if length is None:
        return t[begin:]
    ln = _num(length)
    if ln is None:
        return None
    return t[begin:begin + max(0, int(ln))]


def _fn_upper(s: Any) -> str | None:
    t = _text(s)
    return None if t is None else t.upper()


def _fn_lower(s: Any) -> str | None:
    t = _text(s)
    return None if t is None else t.lower()


def _fn_trim(s: Any) -> str | None:
    t = _text(s)
    return None if t is None else t.strip()


def _fn_concat(*args: Any) -> str | None:
    if any(a is None for a in args):
        return None
    return "".join(str(a) for a in args)


def _fn_date_part(s: Any, part: Any) -> float | None:
    """ISO 日期字符串取分量（year/month/day）；非日期文本 → None"""
    t = _text(s)
    p = (_text(part) or "").strip().lower()
    if t is None or p not in ("year", "month", "day"):
        return None
    m = re.match(r"^\s*(\d{4})-(\d{1,2})(?:-(\d{1,2}))?", t)
    if not m:
        return None
    if p == "year":
        return float(m.group(1))
    if p == "month":
        return float(m.group(2))
    return float(m.group(3)) if m.group(3) else None


_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "round": _fn_round, "abs": _fn_abs, "ceil": _fn_ceil, "floor": _fn_floor,
    "min": _fn_min, "max": _fn_max, "coalesce": _fn_coalesce,
    "substring": _fn_substring, "substr": _fn_substring,
    "upper": _fn_upper, "lower": _fn_lower, "trim": _fn_trim, "concat": _fn_concat,
    "date_part": _fn_date_part,
}

FUNCTION_NAMES: tuple[str, ...] = tuple(sorted(_FUNCTIONS))

_ALLOWED_NODES = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Call, ast.Name,
                  ast.Load, ast.Constant, ast.Add, ast.Sub, ast.Mult,
                  ast.Div, ast.Mod, ast.USub, ast.UAdd)


# ---------------------------------------------------------------------------
# 解析与校验
# ---------------------------------------------------------------------------

def _split_literals(expr: str) -> list[tuple[str, bool]]:
    """切分为 (片段, 是否字符串字面量)：单双引号段内的 [xxx] 不是字段引用

    表达式引擎本身不支持多行字符串，无需处理三引号/转义引号的边界。
    """
    parts: list[tuple[str, bool]] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in expr or "":
        if quote is None:
            if ch in ("'", '"'):
                if buf:
                    parts.append(("".join(buf), False))
                    buf = []
                quote = ch
                buf.append(ch)
            else:
                buf.append(ch)
        else:
            buf.append(ch)
            if ch == quote:
                parts.append(("".join(buf), True))
                buf = []
                quote = None
    if buf:
        parts.append(("".join(buf), quote is not None))
    return parts


def extract_refs(expr: str) -> list[str]:
    """按出现顺序去重提取 [引用名] 列表（字符串字面量内的 [xxx] 不算引用）"""
    seen: list[str] = []
    for seg, is_lit in _split_literals(expr or ""):
        if is_lit:
            continue
        for m in _REF_RE.findall(seg):
            name = m.strip()
            if name and name not in seen:
                seen.append(name)
    return seen


def compile_expr(expr: str, resolve: Callable[[str], str | None]) -> CompiledExpr:
    """解析并静态校验表达式

    resolve(引用名) -> 字段键；返回 None 表示未知引用（报错）。
    出错抛 ExprError（消息含具体引用名/函数名/节点类型，便于 422 文案）。
    """
    raw = (expr or "").strip()
    if not raw:
        raise ExprError("表达式不能为空")

    refs: dict[str, str] = {}
    names: dict[str, str] = {}
    ordered = extract_refs(raw)
    for i, name in enumerate(ordered):
        key = resolve(name)
        if not key:
            raise ExprError(f"表达式引用了不存在的字段：[{name}]")
        safe = f"__r{i}"
        refs[safe] = key
        names[safe] = name
    # 只在代码段替换引用（含两侧空白变体）；字符串字面量原样保留——
    # 原实现 re.sub 全局替换会把 concat([产线], "[A线]") 的字面量也改写（审查 P3）
    rewritten = ""
    code_only = ""
    for seg, is_lit in _split_literals(raw):
        if is_lit:
            rewritten += seg
            continue
        for i, name in enumerate(ordered):
            seg = re.sub(r"\[\s*" + re.escape(name) + r"\s*]", f"__r{i}", seg)
        rewritten += seg
        code_only += seg          # 只收集替换后的代码段（字面量不进检查）

    # 未解析引用检查同样只看代码段：字面量里的 [xxx] 不是引用
    if _REF_RE.search(code_only):
        raise ExprError("表达式含未解析的引用（引用名含方括号或为空）")

    try:
        node = ast.parse(rewritten, mode="eval")
    except SyntaxError as e:
        raise ExprError(f"表达式语法错误：{e.msg}") from e

    for sub in ast.walk(node):
        if not isinstance(sub, _ALLOWED_NODES):
            raise ExprError(f"表达式含不允许的结构：{type(sub).__name__}")
        if isinstance(sub, ast.Call):
            if not isinstance(sub.func, ast.Name):
                raise ExprError("仅支持直接调用白名单函数（不允许属性/方法调用）")
            if sub.func.id.lower() not in _FUNCTIONS:
                raise ExprError(
                    f"不支持的函数：{sub.func.id}（可用：{', '.join(FUNCTION_NAMES)}）")
            if sub.keywords:
                raise ExprError("函数不支持关键字参数")
        if isinstance(sub, ast.Name) and sub.id not in refs \
                and sub.id.lower() not in _FUNCTIONS:
            raise ExprError(f"表达式含未知标识符：{sub.id}（字段引用请用 [字段名]）")
        if isinstance(sub, ast.Constant) and not isinstance(sub.value, (int, float, str)):
            raise ExprError("仅支持数值与字符串字面量")

    return CompiledExpr(raw=raw, node=node, refs=refs, names=names)


def eval_compiled(compiled: CompiledExpr, row: dict[str, Any]) -> Any:
    """对单行求值；缺值/类型不合法 → None（行级容错，不抛出）"""
    return _eval(compiled.node.body, compiled, row)


def _eval(node: ast.AST, compiled: CompiledExpr, row: dict[str, Any]) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in compiled.refs:
            return row.get(compiled.refs[node.id])
        # 函数名作为值无意义（已在校验期排除）；防御性返回 None
        return None
    if isinstance(node, ast.BinOp):
        left = _eval(node.left, compiled, row)
        right = _eval(node.right, compiled, row)
        op = node.op
        # 注意：AST 运算符节点不保证单例（Python 3.13 实测 `op is ast.Mod` 为 False），
        # 一律 isinstance 判断
        if isinstance(op, ast.Add) and (isinstance(left, str) or isinstance(right, str)):
            return _fn_concat(left, right)
        ln, rn = _num(left), _num(right)
        if ln is None or rn is None:
            return None
        if isinstance(op, ast.Add):
            return ln + rn
        if isinstance(op, ast.Sub):
            return ln - rn
        if isinstance(op, ast.Mult):
            return ln * rn
        if isinstance(op, ast.Div):
            return None if rn == 0 else ln / rn
        if isinstance(op, ast.Mod):
            return None if rn == 0 else ln % rn
        return None
    if isinstance(node, ast.UnaryOp):
        val = _eval(node.operand, compiled, row)
        n = _num(val)
        if n is None:
            return None
        return -n if isinstance(node.op, ast.USub) else n
    if isinstance(node, ast.Call):
        fn = _FUNCTIONS[node.func.id.lower()]
        args = [_eval(a, compiled, row) for a in node.args]
        try:
            return fn(*args)
        except (TypeError, ValueError, OverflowError):
            return None
    return None


# ---------------------------------------------------------------------------
# 派生列与循环检测
# ---------------------------------------------------------------------------

def detect_cycle(deps: dict[str, set[str]]) -> list[str] | None:
    """依赖图拓扑检测；有环返回环路径（[A, B, A]），无环返回 None"""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {k: WHITE for k in deps}
    stack: list[str] = []

    def visit(key: str) -> list[str] | None:
        color[key] = GREY
        stack.append(key)
        for nxt in deps.get(key, ()):  # 仅跟踪同批次派生列之间的依赖
            if nxt not in color:
                continue
            if color[nxt] == GREY:
                return stack[stack.index(nxt):] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[key] = BLACK
        return None

    for key in deps:
        if color[key] == WHITE:
            found = visit(key)
            if found:
                return found
    return None
