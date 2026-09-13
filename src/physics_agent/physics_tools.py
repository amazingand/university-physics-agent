"""受限的 SymPy、NumPy 与 Pint 确定性工具。"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
import multiprocessing
from multiprocessing.connection import Connection
import re
import sys
from typing import Any


MAX_PAYLOAD_BYTES = 64 * 1024
MAX_AST_NODES = 128
MAX_AST_DEPTH = 20
MAX_NUMBER_DIGITS = 64
MAX_SYMBOLS = 32
MAX_ARRAY_ELEMENTS = 100
MAX_MATRIX_ROWS = 10
MAX_MATRIX_COLUMNS = 10
MAX_UNIT_CHARS = 64

_OPERATIONS = {
    "symbolic_equivalence",
    "numeric_compare",
    "order_of_magnitude",
    "unit_convert",
    "unit_compatibility",
    "dimensionality",
    "newton_acceleration",
}
_SYMBOL_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,31}$")
_UNIT_TEXT = re.compile(r"^[A-Za-z0-9_*/^(). +\-]+$")
_UNIT_TOKEN = re.compile(r"[A-Za-z_]+")
_ALLOWED_UNITS = {
    "dimensionless",
    "meter",
    "metre",
    "m",
    "kilometer",
    "kilometre",
    "km",
    "centimeter",
    "centimetre",
    "cm",
    "millimeter",
    "millimetre",
    "mm",
    "second",
    "s",
    "minute",
    "min",
    "hour",
    "h",
    "kilogram",
    "kg",
    "gram",
    "g",
    "newton",
    "N",
    "kilonewton",
    "kN",
    "joule",
    "J",
    "watt",
    "W",
    "pascal",
    "Pa",
    "hertz",
    "Hz",
    "radian",
    "rad",
    "degree",
    "deg",
    "kelvin",
    "K",
    "coulomb",
    "C",
    "volt",
    "V",
    "ampere",
    "A",
    "tesla",
    "T",
    "weber",
    "Wb",
}


class DeterministicPhysicsTool:
    """在独立进程中执行结构化、白名单化的确定性检查。"""

    name = "physics_deterministic"

    def __init__(
        self,
        *,
        timeout_seconds: float = 2.0,
        memory_limit_mb: int = 1024,
    ) -> None:
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not 0.001 <= float(timeout_seconds) <= 30
        ):
            raise ValueError("timeout_seconds 必须在 0.001 到 30 秒之间")
        if (
            isinstance(memory_limit_mb, bool)
            or not isinstance(memory_limit_mb, int)
            or not 128 <= memory_limit_mb <= 4096
        ):
            raise ValueError("memory_limit_mb 必须在 128 到 4096 之间")
        self._timeout_seconds = float(timeout_seconds)
        self._memory_limit_mb = memory_limit_mb

    def execute(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        """执行一次检查；不确定与错误作为结构化结果返回。"""

        if not isinstance(arguments, Mapping):
            return _error("unknown", "invalid_arguments", "工具参数必须是对象")
        try:
            encoded = json.dumps(
                dict(arguments),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            return _error("unknown", "invalid_arguments", "工具参数必须是有限 JSON 数据")
        if len(encoded) > MAX_PAYLOAD_BYTES:
            return _error("unknown", "input_too_large", "工具参数超过大小上限")
        sanitized = json.loads(encoded.decode("utf-8"))
        operation = sanitized.get("operation")
        if not isinstance(operation, str) or operation not in _OPERATIONS:
            return _error("unknown", "unsupported_operation", "工具操作不在白名单中")

        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        process = context.Process(
            target=_worker_entry,
            args=(sanitized, sender, self._memory_limit_mb, self._timeout_seconds),
            daemon=True,
        )
        started = False
        try:
            process.start()
            started = True
            sender.close()
            process.join(self._timeout_seconds)
            if process.is_alive():
                process.terminate()
                process.join(0.5)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(0.5)
                return {
                    "status": "indeterminate",
                    "operation": operation,
                    "message": "工具达到硬超时，无法确定结论",
                    "data": {"conclusion": "indeterminate", "reason": "timeout"},
                }
            if receiver.poll(0.2):
                result = receiver.recv()
                if isinstance(result, dict):
                    return result
            return {
                "status": "indeterminate",
                "operation": operation,
                "message": "工具进程未返回可用结果",
                "data": {
                    "conclusion": "indeterminate",
                    "reason": "worker_terminated",
                },
            }
        except (OSError, EOFError):
            return _error(operation, "worker_failure", "无法安全执行工具进程")
        finally:
            sender.close()
            receiver.close()
            if started and process.is_alive():
                process.terminate()
                process.join(0.5)
            if started:
                process.close()


def execute_physics_tool(
    arguments: Mapping[str, object],
    *,
    timeout_seconds: float = 2.0,
    memory_limit_mb: int = 1024,
) -> Mapping[str, object]:
    """一次性便捷入口。"""

    return DeterministicPhysicsTool(
        timeout_seconds=timeout_seconds,
        memory_limit_mb=memory_limit_mb,
    ).execute(arguments)


def _worker_entry(
    arguments: dict[str, Any],
    sender: Connection,
    memory_limit_mb: int,
    timeout_seconds: float,
) -> None:
    operation = str(arguments.get("operation", "unknown"))
    try:
        _apply_worker_limits(memory_limit_mb, timeout_seconds)
        result = _dispatch(arguments)
    except MemoryError:
        result = _error(operation, "resource_limit", "工具超过内存边界")
    except _InputError as exc:
        result = _error(operation, exc.code, exc.message)
    except BaseException:
        result = _error(operation, "execution_failure", "工具执行失败，未产生结论")
    try:
        sender.send(result)
    except (BrokenPipeError, EOFError, OSError):
        pass
    finally:
        sender.close()


def _apply_worker_limits(memory_limit_mb: int, timeout_seconds: float) -> None:
    # Windows 没有 ``resource``，但仍由 spawn worker、父进程硬超时和完整输入
    # 白名单提供隔离。其他 Unix 若缺少对应能力也安全降级为同一组边界。
    if sys.platform == "win32":
        return
    try:
        import resource
    except ImportError:
        return

    if not hasattr(resource, "RLIMIT_AS") or not hasattr(resource, "RLIMIT_CPU"):
        return

    memory_bytes = memory_limit_mb * 1024 * 1024
    cpu_seconds = max(1, math.ceil(timeout_seconds))
    try:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    except (OSError, ValueError):
        # 某些 Unix/容器不允许收紧 rlimit；不能因此退回同进程执行。
        return


def _dispatch(arguments: dict[str, Any]) -> dict[str, object]:
    operation = arguments["operation"]
    allowed: dict[str, set[str]] = {
        "symbolic_equivalence": {"operation", "left", "right"},
        "numeric_compare": {"operation", "actual", "expected", "rtol", "atol"},
        "order_of_magnitude": {"operation", "actual", "expected"},
        "unit_convert": {"operation", "value", "from_unit", "to_unit"},
        "unit_compatibility": {"operation", "left_unit", "right_unit"},
        "dimensionality": {"operation", "unit"},
        "newton_acceleration": {
            "operation",
            "net_force",
            "force_unit",
            "mass",
            "mass_unit",
            "to_unit",
        },
    }
    unknown = sorted(set(arguments) - allowed[operation])
    if unknown:
        raise _InputError("unknown_fields", "工具参数包含未知字段")

    if operation == "symbolic_equivalence":
        return _symbolic_equivalence(arguments)
    if operation == "numeric_compare":
        return _numeric_compare(arguments)
    if operation == "order_of_magnitude":
        return _order_of_magnitude(arguments)
    if operation == "unit_convert":
        return _unit_convert(arguments)
    if operation == "unit_compatibility":
        return _unit_compatibility(arguments)
    if operation == "dimensionality":
        return _dimensionality(arguments)
    return _newton_acceleration(arguments)


def _symbolic_equivalence(arguments: dict[str, Any]) -> dict[str, object]:
    if "left" not in arguments or "right" not in arguments:
        raise _InputError("missing_fields", "符号等价检查缺少 left 或 right")
    import sympy as sp

    state = _AstState()
    left = _build_expression(arguments["left"], state, depth=0, sp=sp)
    right = _build_expression(arguments["right"], state, depth=0, sp=sp)
    if isinstance(left, sp.MatrixBase) or isinstance(right, sp.MatrixBase):
        if not isinstance(left, sp.MatrixBase) or not isinstance(right, sp.MatrixBase):
            conclusion = "not_equivalent"
        elif left.shape != right.shape:
            conclusion = "not_equivalent"
        else:
            decisions = [
                _expression_equivalence_decision(left_value, right_value, sp)
                for left_value, right_value in zip(left, right, strict=True)
            ]
            conclusion = _combine_zero_decisions(decisions)
    else:
        conclusion = _expression_equivalence_decision(left, right, sp)
    if conclusion == "indeterminate":
        return {
            "status": "indeterminate",
            "operation": "symbolic_equivalence",
            "message": "符号工具无法确定两个表达式是否等价",
            "data": {"conclusion": conclusion},
        }
    return _verified(
        "symbolic_equivalence",
        conclusion,
        "符号等价性检查完成",
    )


def _zero_decision(value: Any, sp: Any) -> str:
    if value == sp.S.Zero:
        return "equivalent"
    decision = value.equals(0)
    if decision is True:
        return "equivalent"
    if decision is False:
        return "not_equivalent"
    return "indeterminate"


def _expression_equivalence_decision(left: Any, right: Any, sp: Any) -> str:
    """同时比较表达式值和实数定义域，避免约分掩盖奇点。"""

    value_decision = _zero_decision(sp.simplify(left - right), sp)
    if value_decision != "equivalent":
        return value_decision
    symbols = sorted(left.free_symbols | right.free_symbols, key=lambda item: item.name)
    try:
        for symbol in symbols:
            left_domain = sp.calculus.util.continuous_domain(left, symbol, sp.S.Reals)
            right_domain = sp.calculus.util.continuous_domain(right, symbol, sp.S.Reals)
            if left_domain == right_domain:
                continue
            difference = sp.SymmetricDifference(left_domain, right_domain)
            if difference is sp.S.EmptySet or difference.is_empty is True:
                continue
            if difference.is_empty is False:
                return "not_equivalent"
            return "indeterminate"
    except (NotImplementedError, TypeError, ValueError):
        return "indeterminate"
    return "equivalent"


def _combine_zero_decisions(decisions: list[str]) -> str:
    if any(value == "not_equivalent" for value in decisions):
        return "not_equivalent"
    if any(value == "indeterminate" for value in decisions):
        return "indeterminate"
    return "equivalent"


class _AstState:
    def __init__(self) -> None:
        self.nodes = 0
        self.symbols: set[str] = set()


def _build_expression(value: Any, state: _AstState, *, depth: int, sp: Any) -> Any:
    if depth > MAX_AST_DEPTH:
        raise _InputError("ast_too_deep", "符号 AST 超过深度上限")
    if not isinstance(value, dict):
        raise _InputError("invalid_ast", "符号表达式必须使用结构化 AST")
    state.nodes += 1
    if state.nodes > MAX_AST_NODES:
        raise _InputError("ast_too_large", "符号 AST 超过节点上限")
    node_type = value.get("type")
    if not isinstance(node_type, str):
        raise _InputError("invalid_ast", "符号 AST 节点缺少 type")

    if node_type == "integer":
        _require_exact_keys(value, {"type", "value"})
        integer = _bounded_integer(value.get("value"))
        return sp.Integer(integer)
    if node_type == "rational":
        _require_exact_keys(value, {"type", "numerator", "denominator"})
        numerator = _bounded_integer(value.get("numerator"))
        denominator = _bounded_integer(value.get("denominator"))
        if denominator == 0:
            raise _InputError("invalid_number", "有理数分母不能为零")
        return sp.Rational(numerator, denominator)
    if node_type == "float":
        _require_exact_keys(value, {"type", "value"})
        number = _finite_number(value.get("value"))
        return sp.Float(number, 15)
    if node_type == "symbol":
        _require_exact_keys(value, {"type", "name"})
        name = value.get("name")
        if not isinstance(name, str) or not _SYMBOL_NAME.fullmatch(name) or "__" in name:
            raise _InputError("invalid_symbol", "符号名称不在白名单格式内")
        state.symbols.add(name)
        if len(state.symbols) > MAX_SYMBOLS:
            raise _InputError("too_many_symbols", "符号数量超过上限")
        return sp.Symbol(name, real=True, finite=True)
    if node_type in {"add", "mul"}:
        _require_exact_keys(value, {"type", "args"})
        raw_args = value.get("args")
        if not isinstance(raw_args, list) or not 2 <= len(raw_args) <= 16:
            raise _InputError("invalid_ast", "add/mul 必须含 2 到 16 个参数")
        children = [
            _build_expression(child, state, depth=depth + 1, sp=sp)
            for child in raw_args
        ]
        if any(isinstance(child, sp.MatrixBase) for child in children):
            raise _InputError("invalid_ast", "矩阵不能嵌入 add/mul 节点")
        constructor = sp.Add if node_type == "add" else sp.Mul
        return constructor(*children, evaluate=False)
    if node_type == "neg":
        _require_exact_keys(value, {"type", "arg"})
        child = _build_expression(value.get("arg"), state, depth=depth + 1, sp=sp)
        if isinstance(child, sp.MatrixBase):
            return -child
        return sp.Mul(sp.Integer(-1), child, evaluate=False)
    if node_type == "pow":
        _require_exact_keys(value, {"type", "base", "exponent"})
        base = _build_expression(value.get("base"), state, depth=depth + 1, sp=sp)
        exponent = _build_expression(
            value.get("exponent"), state, depth=depth + 1, sp=sp
        )
        if isinstance(base, sp.MatrixBase) or isinstance(exponent, sp.MatrixBase):
            raise _InputError("invalid_ast", "pow 不接受矩阵参数")
        if exponent.is_number and abs(float(exponent)) > 100:
            raise _InputError("exponent_too_large", "幂指数超过上限")
        return sp.Pow(base, exponent, evaluate=False)
    if node_type == "function":
        _require_exact_keys(value, {"type", "name", "arg"})
        name = value.get("name")
        functions = {
            "sin": sp.sin,
            "cos": sp.cos,
            "tan": sp.tan,
            "exp": sp.exp,
            "log": sp.log,
            "sqrt": sp.sqrt,
            "abs": sp.Abs,
        }
        if name not in functions:
            raise _InputError("unsupported_function", "符号函数不在白名单中")
        argument = _build_expression(value.get("arg"), state, depth=depth + 1, sp=sp)
        if isinstance(argument, sp.MatrixBase):
            raise _InputError("invalid_ast", "白名单函数不接受矩阵参数")
        return functions[name](argument, evaluate=False)
    if node_type == "matrix":
        _require_exact_keys(value, {"type", "rows"})
        rows = value.get("rows")
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MATRIX_ROWS:
            raise _InputError("matrix_too_large", "矩阵行数超出边界")
        if not all(isinstance(row, list) for row in rows):
            raise _InputError("invalid_matrix", "矩阵 rows 必须是二维数组")
        widths = {len(row) for row in rows}
        if len(widths) != 1 or not widths or not 1 <= next(iter(widths)) <= MAX_MATRIX_COLUMNS:
            raise _InputError("matrix_too_large", "矩阵列数或形状无效")
        return sp.ImmutableMatrix(
            [
                [
                    _build_expression(cell, state, depth=depth + 1, sp=sp)
                    for cell in row
                ]
                for row in rows
            ]
        )
    raise _InputError("unsupported_ast_node", "符号 AST 节点不在白名单中")


def _numeric_compare(arguments: dict[str, Any]) -> dict[str, object]:
    if "actual" not in arguments or "expected" not in arguments:
        raise _InputError("missing_fields", "数值比较缺少 actual 或 expected")
    import numpy as np

    actual = _numeric_array(arguments["actual"], np)
    expected = _numeric_array(arguments["expected"], np)
    if actual.shape != expected.shape:
        return _verified(
            "numeric_compare",
            "not_equal",
            "数值形状不同",
            actual_shape=list(actual.shape),
            expected_shape=list(expected.shape),
        )
    rtol = _bounded_tolerance(arguments.get("rtol", 1e-9), "rtol")
    atol = _bounded_tolerance(arguments.get("atol", 1e-12), "atol")
    equal = bool(np.allclose(actual, expected, rtol=rtol, atol=atol))
    max_error = float(np.max(np.abs(actual - expected))) if actual.size else 0.0
    return _verified(
        "numeric_compare",
        "equal" if equal else "not_equal",
        "数值容差检查完成",
        max_absolute_error=max_error,
        rtol=rtol,
        atol=atol,
    )


def _order_of_magnitude(arguments: dict[str, Any]) -> dict[str, object]:
    if "actual" not in arguments or "expected" not in arguments:
        raise _InputError("missing_fields", "数量级检查缺少 actual 或 expected")
    actual = _finite_number(arguments["actual"])
    expected = _finite_number(arguments["expected"])
    if actual == 0 or expected == 0:
        if actual == expected:
            return _verified(
                "order_of_magnitude", "same_order", "两个数均为零"
            )
        return {
            "status": "indeterminate",
            "operation": "order_of_magnitude",
            "message": "零与非零值之间没有有限的十进制数量级比较",
            "data": {"conclusion": "indeterminate", "reason": "zero_value"},
        }
    actual_order = math.floor(math.log10(abs(actual)))
    expected_order = math.floor(math.log10(abs(expected)))
    return _verified(
        "order_of_magnitude",
        "same_order" if actual_order == expected_order else "different_order",
        "数量级检查完成",
        actual_order=actual_order,
        expected_order=expected_order,
    )


def _unit_convert(arguments: dict[str, Any]) -> dict[str, object]:
    for key in ("value", "from_unit", "to_unit"):
        if key not in arguments:
            raise _InputError("missing_fields", "单位换算参数不完整")
    value = _finite_number(arguments["value"])
    from_unit = _validated_unit(arguments["from_unit"])
    to_unit = _validated_unit(arguments["to_unit"])
    import pint

    registry = pint.UnitRegistry(autoconvert_offset_to_baseunit=False)
    try:
        converted = (value * registry.Unit(from_unit)).to(registry.Unit(to_unit))
    except pint.DimensionalityError:
        return _verified(
            "unit_convert",
            "incompatible",
            "源单位与目标单位量纲不兼容",
        )
    magnitude = float(converted.magnitude)
    if not math.isfinite(magnitude):
        raise _InputError("non_finite_result", "单位换算得到非有限结果")
    return _verified(
        "unit_convert",
        "converted",
        "单位换算完成",
        value=magnitude,
        unit=str(converted.units),
    )


def _unit_compatibility(arguments: dict[str, Any]) -> dict[str, object]:
    if "left_unit" not in arguments or "right_unit" not in arguments:
        raise _InputError("missing_fields", "量纲兼容检查缺少单位")
    left = _validated_unit(arguments["left_unit"])
    right = _validated_unit(arguments["right_unit"])
    import pint

    registry = pint.UnitRegistry(autoconvert_offset_to_baseunit=False)
    left_unit = registry.Unit(left)
    right_unit = registry.Unit(right)
    compatible = left_unit.dimensionality == right_unit.dimensionality
    return _verified(
        "unit_compatibility",
        "compatible" if compatible else "incompatible",
        "量纲兼容性检查完成",
        left_dimensionality=str(left_unit.dimensionality),
        right_dimensionality=str(right_unit.dimensionality),
    )


def _dimensionality(arguments: dict[str, Any]) -> dict[str, object]:
    if "unit" not in arguments:
        raise _InputError("missing_fields", "量纲检查缺少 unit")
    unit = _validated_unit(arguments["unit"])
    import pint

    registry = pint.UnitRegistry(autoconvert_offset_to_baseunit=False)
    dimensionality = str(registry.Unit(unit).dimensionality)
    return _verified(
        "dimensionality",
        "determined",
        "量纲检查完成",
        dimensionality=dimensionality,
    )


def _newton_acceleration(arguments: dict[str, Any]) -> dict[str, object]:
    """按 a=F_net/m 计算加速度，并由 Pint 同时核验量纲。"""

    for key in ("net_force", "force_unit", "mass", "mass_unit", "to_unit"):
        if key not in arguments:
            raise _InputError("missing_fields", "牛顿第二定律计算参数不完整")
    net_force = _finite_number(arguments["net_force"])
    mass = _finite_number(arguments["mass"])
    if mass <= 0:
        raise _InputError("invalid_mass", "质量必须为正数")
    force_unit = _validated_unit(arguments["force_unit"])
    mass_unit = _validated_unit(arguments["mass_unit"])
    to_unit = _validated_unit(arguments["to_unit"])

    import pint

    registry = pint.UnitRegistry(autoconvert_offset_to_baseunit=False)
    try:
        acceleration = (
            net_force * registry.Unit(force_unit)
            / (mass * registry.Unit(mass_unit))
        ).to(registry.Unit(to_unit))
    except pint.DimensionalityError:
        return _verified(
            "newton_acceleration",
            "incompatible",
            "合外力、质量或目标加速度单位的量纲不兼容",
        )
    value = float(acceleration.magnitude)
    if not math.isfinite(value):
        raise _InputError("non_finite_result", "牛顿第二定律计算得到非有限结果")
    return _verified(
        "newton_acceleration",
        "calculated",
        "按牛顿第二定律核验加速度完成",
        value=value,
        unit=str(acceleration.units),
    )


def _numeric_array(value: Any, np: Any) -> Any:
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError, OverflowError) as exc:
        raise _InputError("invalid_number", "数值数组格式无效") from exc
    if array.ndim > 2 or array.size > MAX_ARRAY_ELEMENTS:
        raise _InputError("array_too_large", "数值数组超过维度或元素上限")
    if array.ndim == 2 and (
        array.shape[0] > MAX_MATRIX_ROWS or array.shape[1] > MAX_MATRIX_COLUMNS
    ):
        raise _InputError("array_too_large", "数值矩阵超过 10x10 上限")
    if not bool(np.all(np.isfinite(array))):
        raise _InputError("invalid_number", "数值必须全部有限")
    return array


def _validated_unit(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_UNIT_CHARS
        or not _UNIT_TEXT.fullmatch(value)
        or "__" in value
    ):
        raise _InputError("invalid_unit", "单位表达式不在受限语法内")
    tokens = _UNIT_TOKEN.findall(value)
    if not tokens or any(token not in _ALLOWED_UNITS for token in tokens):
        raise _InputError("unsupported_unit", "单位名称不在白名单中")
    return value


def _bounded_integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _InputError("invalid_number", "整数 AST 节点必须含整数值")
    if len(str(abs(value))) > MAX_NUMBER_DIGITS:
        raise _InputError("number_too_large", "整数位数超过上限")
    return value


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InputError("invalid_number", "数值必须是整数或浮点数")
    try:
        number = float(value)
    except OverflowError as exc:
        raise _InputError("invalid_number", "数值超出浮点范围") from exc
    if not math.isfinite(number):
        raise _InputError("invalid_number", "数值必须有限")
    return number


def _bounded_tolerance(value: Any, name: str) -> float:
    number = _finite_number(value)
    if not 0 <= number <= 1:
        raise _InputError("invalid_tolerance", f"{name} 必须在 0 到 1 之间")
    return number


def _require_exact_keys(value: dict[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise _InputError("invalid_ast", "符号 AST 节点字段不符合白名单")


def _verified(
    operation: str,
    conclusion: str,
    message: str,
    **data: object,
) -> dict[str, object]:
    return {
        "status": "verified",
        "operation": operation,
        "message": message,
        "data": {"conclusion": conclusion, **data},
    }


def _error(operation: str, code: str, message: str) -> dict[str, object]:
    return {
        "status": "error",
        "operation": operation,
        "message": message,
        "data": {"conclusion": "error", "code": code},
    }


class _InputError(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)
