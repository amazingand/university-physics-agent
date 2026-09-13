"""厂商无关的提示优先教学应用服务。"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Mapping, Sequence
import unicodedata

from physics_agent.provider_diagnostics import provider_error_details, provider_error_text

from physics_agent.core import (
    ChatProvider,
    CompletionRequest,
    DeterministicTool,
    KnowledgeHit,
    KnowledgePackageRef,
    KnowledgeRepository,
    Message,
    SourceReference,
)


ERROR_TYPES = frozenset(
    {
        "concept",
        "model",
        "force",
        "sign",
        "unit",
        "algebra",
        "order_of_magnitude",
    }
)
MAX_TEACHING_HITS = 3
MAX_KNOWLEDGE_TEXT_BYTES = 4 * 1024
MAX_TOOL_EVIDENCE_BYTES = 4 * 1024
MAX_MESSAGES_BYTES = 32 * 1024

_DIAGNOSTIC_SUGGESTIONS = {
    "concept": "先说明所用物理概念及其适用条件。",
    "model": "重新检查研究对象、参考系和模型假设。",
    "force": "隔离研究对象并逐项核对所有外力。",
    "sign": "先固定正方向，再检查各矢量分量的符号。",
    "unit": "统一单位并逐步检查量纲。",
    "algebra": "保留物理关系式，逐步复核代数变形。",
    "order_of_magnitude": "估算合理数量级，再与计算结果比较。",
}

_CITATION_RE = re.compile(r"\[source:([A-Za-z0-9][A-Za-z0-9._:-]{0,127})\]")
_CITATION_TOKEN_RE = re.compile(r"\[source:[^\]\r\n]*\]")
_REVIEWED_MECHANICS_PACKAGE = ("mechanics.zh.reviewed", "0.2.0")
_NET_FORCE_HINT_SOURCE_ID = "mechanics.net-force"
_NET_FORCE_HINT_QUERY = "合力决定加速度而不是速度"
_DECIMAL = r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))"
_MASS_PATTERN = re.compile(
    rf"(?:质量\s*(?:为|是|=)?|(?<![A-Za-z])m\s*=)\s*{_DECIMAL}"
    r"\s*(kg|kilograms?|g|grams?|千克|克)",
    re.IGNORECASE,
)
_NET_FORCE_PATTERNS = (
    re.compile(
        rf"(?:水平方向(?:的)?\s*)?合外力\s*(?:大小)?\s*(?:为|是|=)\s*{_DECIMAL}"
        r"\s*(kN|N|kilonewtons?|newtons?|千牛|牛顿)",
        re.IGNORECASE,
    ),
    re.compile(
        rf"(?<![A-Za-z])F_?net(?:,?x)?\s*=\s*{_DECIMAL}\s*"
        r"\s*(kN|N|kilonewtons?|newtons?|千牛|牛顿)",
        re.IGNORECASE,
    ),
)
_SINGLE_FORCE_PATTERNS = (
    re.compile(
        rf"(?:只受到?|仅受到?|单一)\s*(?:沿[正负]方向(?:的)?\s*)?"
        rf"(?:大小为)?\s*{_DECIMAL}\s*"
        r"(kN|N|kilonewtons?|newtons?|千牛|牛顿)\s*(?:的)?\s*"
        r"(?:沿[正负]方向(?:的)?\s*)?水平(?:外)?力",
        re.IGNORECASE,
    ),
    re.compile(
        rf"单一水平(?:外)?力\s*(?:沿[正负]方向)?\s*(?:大小)?\s*"
        rf"(?:为|是|=)\s*{_DECIMAL}\s*"
        r"(kN|N|kilonewtons?|newtons?|千牛|牛顿)",
        re.IGNORECASE,
    ),
)
_SAFE_ACCELERATION_UNITS = {
    "meter / second ** 2": r"\mathrm{m/s^2}",
    "meter/second**2": r"\mathrm{m/s^2}",
    "m / s ** 2": r"\mathrm{m/s^2}",
    "m/s**2": r"\mathrm{m/s^2}",
    "m/s^2": r"\mathrm{m/s^2}",
    "m/s²": r"\mathrm{m/s^2}",
}


@dataclass(frozen=True)
class ErrorSignal:
    """由上游规则或确定性检查给出的结构化错误信号。"""

    error_type: str
    evidence: str


@dataclass(frozen=True)
class Diagnostic:
    """面向学生的错误诊断，不包含模型内部推理。"""

    error_type: str
    evidence: str
    suggestion: str


@dataclass(frozen=True)
class ToolCall:
    """调用已注册确定性工具的结构化请求。"""

    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True)
class ToolEvidence:
    """与模型文字、教材来源分离的确定性工具证据。"""

    name: str
    status: str
    details: Mapping[str, object]


@dataclass(frozen=True)
class ModelOutput:
    """模型输出及其完成状态；它本身不是真实性证据。"""

    text: str
    completed: bool
    provider: str
    model: str


@dataclass(frozen=True)
class _NewtonAccelerationCase:
    net_force: float
    force_unit: str
    force_label: str
    mass: float
    mass_unit: str
    mass_label: str
    axis_description: str
    negative_direction_description: str
    to_unit: str = "meter / second ** 2"


@dataclass(frozen=True)
class TeachingRequest:
    """教学请求；提示状态和完整解答授权由应用层显式提供。"""

    problem: str
    goal: str
    known_conditions: tuple[str, ...]
    missing_conditions: tuple[str, ...]
    student_answer: str | None
    error_signals: tuple[ErrorSignal, ...]
    hint_level: int
    full_solution: bool
    tool_calls: tuple[ToolCall, ...] = ()
    max_tokens: int | None = None


@dataclass(frozen=True)
class TeachingResponse:
    """分层教学响应。只有 ``readable_text`` 可直接展示给学生。"""

    status: str
    completed: bool
    publishable_final: bool
    hint_level: int
    diagnostics: tuple[Diagnostic, ...]
    citations: tuple[str, ...]
    rejected_citations: tuple[str, ...]
    source_evidence: tuple[KnowledgeHit, ...]
    model_output: ModelOutput | None
    tool_results: tuple[ToolEvidence, ...]
    readable_text: str
    provider_error: dict[str, object] | None = None


class TeachingService:
    """协调固定知识包、聊天模型与确定性工具的教学应用层。"""

    def __init__(
        self,
        *,
        provider: ChatProvider,
        repository: KnowledgeRepository,
        package: KnowledgePackageRef,
        tools: Sequence[DeterministicTool] = (),
        timeout_seconds: int = 60,
        search_limit: int = MAX_TEACHING_HITS,
    ) -> None:
        if not package.package_id.strip() or not package.version.strip():
            raise ValueError("知识包 ID 和版本不能为空")
        if timeout_seconds <= 0:
            raise ValueError("模型超时必须为正数")
        if not 1 <= search_limit <= MAX_TEACHING_HITS:
            raise ValueError(f"正式教学检索数量上限必须在 1 到 {MAX_TEACHING_HITS} 之间")

        tools_by_name: dict[str, DeterministicTool] = {}
        for tool in tools:
            if not tool.name or tool.name in tools_by_name:
                raise ValueError("工具名称必须非空且不能重复")
            tools_by_name[tool.name] = tool

        self._provider = provider
        self._repository = repository
        self._package = package
        self._tools = tools_by_name
        self._timeout_seconds = timeout_seconds
        self._search_limit = search_limit
        self._require_published_repository()

    def respond(self, request: TeachingRequest) -> TeachingResponse:
        """生成一次教学响应，并在应用层执行提示和发布门禁。"""

        self._require_published_repository()
        preflight_response = preflight_teaching_request(request)
        if preflight_response is not None:
            return preflight_response
        diagnostics = self._diagnose(request.error_signals)

        acceleration_case: _NewtonAccelerationCase | None = None
        if request.full_solution:
            acceleration_case = self._parse_newton_acceleration_case(request)
            if acceleration_case is None:
                return TeachingResponse(
                    status="unverified",
                    completed=False,
                    publishable_final=False,
                    hint_level=request.hint_level,
                    diagnostics=diagnostics,
                    citations=(),
                    rejected_citations=(),
                    source_evidence=(),
                    model_output=None,
                    tool_results=(),
                    readable_text=(
                        "当前完整解答只开放给可机械核验的案例：光滑水平面上质量和单一"
                        "水平力均明确、目标为求加速度。其他问题只能继续使用分级提示。"
                    ),
                )

        raw_hits = tuple(
            self._repository.search(
                self._search_query(request),
                self._package,
                limit=self._search_limit,
            )
        )
        hits = self._normalize_and_validate_hits(raw_hits)
        evidence_error = self._evidence_error(hits)
        if evidence_error is not None:
            return TeachingResponse(
                status="unverified",
                completed=False,
                publishable_final=False,
                hint_level=request.hint_level,
                diagnostics=diagnostics,
                citations=(),
                rejected_citations=(),
                source_evidence=(),
                model_output=None,
                tool_results=(),
                readable_text=evidence_error,
            )

        if request.full_solution:
            source_hit = self._select_application_source(hits)
        else:
            hits, source_hit = self._select_hint_evidence(request, hits)
        if source_hit is None:
            return TeachingResponse(
                status="unverified",
                completed=False,
                publishable_final=False,
                hint_level=request.hint_level,
                diagnostics=diagnostics,
                citations=(),
                rejected_citations=(),
                source_evidence=(),
                model_output=None,
                tool_results=(),
                readable_text="检索结果没有可机械定位的牛顿第二定律证据，不能发布教学结论。",
            )

        calls = list(request.tool_calls)
        if acceleration_case is not None:
            calls.append(
                ToolCall(
                    name="physics_deterministic",
                    arguments={
                        "operation": "newton_acceleration",
                        "net_force": acceleration_case.net_force,
                        "force_unit": acceleration_case.force_unit,
                        "mass": acceleration_case.mass,
                        "mass_unit": acceleration_case.mass_unit,
                        "to_unit": acceleration_case.to_unit,
                    },
                )
            )
        tool_results = self._execute_tools(calls)
        self._validate_tool_evidence_sizes(tool_results)
        messages = self._messages(request, hits, tool_results)
        self._validate_messages_size(messages)
        completion_request = CompletionRequest(
            messages=messages,
            timeout_seconds=self._timeout_seconds,
            max_tokens=request.max_tokens,
        )
        try:
            completion = self._provider.complete(completion_request)
        except Exception as exc:
            details = provider_error_details(exc)
            return TeachingResponse(
                status="provider_incomplete",
                completed=False,
                publishable_final=False,
                hint_level=request.hint_level,
                diagnostics=diagnostics,
                citations=(),
                rejected_citations=(),
                source_evidence=(),
                model_output=None,
                tool_results=tool_results,
                readable_text=provider_error_text(details),
                provider_error=details,
            )

        if not completion.completed:
            return TeachingResponse(
                status="provider_incomplete",
                completed=False,
                publishable_final=False,
                hint_level=request.hint_level,
                diagnostics=diagnostics,
                citations=(),
                rejected_citations=(),
                source_evidence=(),
                model_output=None,
                tool_results=tool_results,
                readable_text=provider_error_text(provider_error_details(None)),
                provider_error=provider_error_details(None),
            )

        model_output = ModelOutput(
            text=completion.text,
            completed=completion.completed,
            provider=completion.provider,
            model=completion.model,
        )
        accepted_ids, rejected_ids = self._check_citations(completion.text, hits)
        cited_evidence = tuple(hit for hit in hits if hit.item_id in accepted_ids)

        if not request.full_solution:
            if rejected_ids:
                return TeachingResponse(
                    status="unverified",
                    completed=False,
                    publishable_final=False,
                    hint_level=request.hint_level,
                    diagnostics=diagnostics,
                    citations=(),
                    rejected_citations=rejected_ids,
                    source_evidence=(),
                    model_output=model_output,
                    tool_results=tool_results,
                    readable_text=(
                        "隐藏的模型候选包含畸形或未命中的来源引用，本轮不能发布教学提示。"
                    ),
                )
            return TeachingResponse(
                status="hint",
                completed=True,
                publishable_final=False,
                hint_level=request.hint_level,
                diagnostics=diagnostics,
                citations=(source_hit.item_id,),
                rejected_citations=(),
                source_evidence=(source_hit,),
                model_output=model_output,
                tool_results=tool_results,
                readable_text=self._render_hint(request.hint_level, source_hit.item_id),
            )

        if not accepted_ids or rejected_ids:
            reason = "缺少可核验来源引用" if not accepted_ids else "包含未命中的来源引用"
            return TeachingResponse(
                status="unverified",
                completed=False,
                publishable_final=False,
                hint_level=request.hint_level,
                diagnostics=diagnostics,
                citations=accepted_ids,
                rejected_citations=rejected_ids,
                source_evidence=cited_evidence,
                model_output=model_output,
                tool_results=tool_results,
                readable_text=f"模型内容{reason}，本轮不能作为教学结论发布。",
            )

        assert acceleration_case is not None
        acceleration_result = tool_results[-1]
        verified_acceleration = self._verified_acceleration(acceleration_result)
        requested_results = tool_results[: len(request.tool_calls)]
        if any(result.status != "verified" for result in requested_results):
            verification_error = "至少一项调用者请求的工具检查未通过核验。"
        elif verified_acceleration is None:
            verification_error = "牛顿第二定律加速度计算没有得到可核验的确定性工具结果。"
        else:
            verification_error = None

        if verification_error is not None:
            return TeachingResponse(
                status="unverified",
                completed=False,
                publishable_final=False,
                hint_level=request.hint_level,
                diagnostics=diagnostics,
                citations=accepted_ids,
                rejected_citations=(),
                source_evidence=cited_evidence,
                model_output=model_output,
                tool_results=tool_results,
                readable_text=verification_error + "模型候选文字不会展示或充当最终结论。",
            )

        value, unit = verified_acceleration
        return TeachingResponse(
            status="full_solution",
            completed=True,
            publishable_final=True,
            hint_level=request.hint_level,
            diagnostics=diagnostics,
            citations=(source_hit.item_id,),
            rejected_citations=(),
            source_evidence=(source_hit,),
            model_output=model_output,
            tool_results=tool_results,
            readable_text=self._render_newton_acceleration(
                acceleration_case,
                value=value,
                unit=unit,
                source_id=source_hit.item_id,
            ),
        )

    @staticmethod
    def _validate_request(request: TeachingRequest) -> None:
        if not request.problem.strip() or not request.goal.strip():
            raise ValueError("题目和目标不能为空")
        if request.hint_level not in (1, 2, 3):
            raise ValueError("提示级别只能为 1、2 或 3")
        if request.max_tokens is not None and (
            isinstance(request.max_tokens, bool)
            or not isinstance(request.max_tokens, int)
            or not 1 <= request.max_tokens <= 8192
        ):
            raise ValueError("max_tokens 必须是 1 到 8192 的整数或 None")
        if any(not condition.strip() for condition in request.known_conditions):
            raise ValueError("已知条件不能包含空项")
        if any(not condition.strip() for condition in request.missing_conditions):
            raise ValueError("缺失条件不能包含空项")
        unsupported = {
            signal.error_type
            for signal in request.error_signals
            if signal.error_type not in ERROR_TYPES
        }
        if unsupported:
            raise ValueError(f"不支持的错误类型：{', '.join(sorted(unsupported))}")

    def _require_published_repository(self) -> None:
        """教学服务自身拒绝未通过人工审核门禁的知识库。"""

        if getattr(self._repository, "status", None) != "published":
            raise ValueError("TeachingService 只接受 status=published 的知识库")

    @staticmethod
    def _diagnose(signals: Sequence[ErrorSignal]) -> tuple[Diagnostic, ...]:
        return tuple(
            Diagnostic(
                error_type=signal.error_type,
                evidence=signal.evidence,
                suggestion=_DIAGNOSTIC_SUGGESTIONS[signal.error_type],
            )
            for signal in signals
        )

    @staticmethod
    def _search_query(request: TeachingRequest) -> str:
        parts = [request.problem, request.goal, *request.known_conditions]
        return " ".join(part.strip() for part in parts if part.strip())

    @classmethod
    def _collect_missing_conditions(
        cls, request: TeachingRequest
    ) -> tuple[str, ...]:
        """合并显式缺失项和首批力学题的保守条件检查。"""

        missing = list(request.missing_conditions)
        statement = " ".join((request.problem, *request.known_conditions))
        goal = request.goal

        explicitly_frictionless = any(
            marker in statement for marker in ("光滑", "无摩擦", "忽略摩擦")
        )
        involves_friction = "粗糙" in statement or (
            "摩擦" in statement and not explicitly_frictionless
        )
        asks_for_motion_or_friction = any(
            marker in goal
            for marker in (
                "摩擦",
                "运动",
                "加速度",
                "是否滑动",
                "能否滑动",
                "状态",
            )
        )
        if not involves_friction or not asks_for_motion_or_friction:
            return cls._deduplicate(missing)

        state_markers = (
            "保持静止",
            "处于静止",
            "静止不动",
            "平衡",
            "匀速运动",
            "匀速滑动",
            "正在滑动",
            "已经滑动",
            "滑动中",
            "将要滑动",
            "即将滑动",
            "刚要滑动",
            "临界",
        )
        has_motion_state = any(marker in statement for marker in state_markers)
        if not has_motion_state:
            missing.append("物体是静止、临界将滑还是已经滑动的运动状态")

        has_friction_parameter = any(
            marker in statement
            for marker in (
                "摩擦系数",
                "摩擦因数",
                "μ",
                "mu=",
                "mu =",
                "最大静摩擦力",
                "滑动摩擦力为",
                "摩擦力为",
            )
        )
        # 已明确保持静止/平衡且其余受力已知时，可由平衡关系确定静摩擦力；
        # 其他状态若没有摩擦参数，不能据接触面“粗糙”补造唯一结论。
        explicitly_static = any(
            marker in statement
            for marker in ("保持静止", "处于静止", "静止不动", "平衡")
        )
        if not has_friction_parameter and not explicitly_static:
            missing.append("与运动状态相应的静摩擦上限或滑动摩擦参数")

        return cls._deduplicate(missing)

    @staticmethod
    def _deduplicate(items: Sequence[str]) -> tuple[str, ...]:
        unique: list[str] = []
        for item in items:
            if item not in unique:
                unique.append(item)
        return tuple(unique)

    @staticmethod
    def _parse_newton_acceleration_case(
        request: TeachingRequest,
    ) -> _NewtonAccelerationCase | None:
        statement = " ".join((request.problem, *request.known_conditions))
        if "加速度" not in request.goal:
            return None
        if "水平面" not in statement or not any(
            marker in statement for marker in ("光滑", "无摩擦", "忽略摩擦")
        ):
            return None
        mass_matches = [
            TeachingService._normalize_measurement(match.group(1), match.group(2), "mass")
            for match in _MASS_PATTERN.finditer(statement)
        ]
        force_matches: list[tuple[float, str, str]] = []
        coordinate_force_matches: list[tuple[float, str, str]] = []
        ambiguous_signed_matches: list[tuple[float, str, str]] = []
        for pattern in (*_NET_FORCE_PATTERNS, *_SINGLE_FORCE_PATTERNS):
            for match in pattern.finditer(statement):
                measurement = TeachingService._normalize_directed_force(match)
                if measurement is None:
                    phrase = match.group(0)
                    has_explicit_axis = re.search(
                        r"(?:规定|取|以)\s*\+x\s*(?:轴)?\s*(?:向右|为正)",
                        statement,
                    ) is not None
                    is_coordinate_worded_net_force = (
                        "水平方向" in phrase
                        and "合外力" in phrase
                        and "大小" not in phrase
                        and has_explicit_axis
                    )
                    if not is_coordinate_worded_net_force:
                        return None
                    ambiguous_signed_matches.append(
                        TeachingService._normalize_measurement(
                            match.group(1), match.group(2), "force"
                        )
                    )
                    continue
                force_matches.append(measurement)
                if re.search(
                    r"(?<![A-Za-z])F_?net,?x\s*=", match.group(0), re.IGNORECASE
                ):
                    coordinate_force_matches.append(measurement)
        masses = TeachingService._unique_measurements(mass_matches)
        forces = TeachingService._unique_measurements(force_matches)
        coordinate_forces = TeachingService._unique_measurements(
            coordinate_force_matches
        )
        ambiguous_forces = TeachingService._unique_measurements(
            ambiguous_signed_matches
        )
        if ambiguous_forces and (
            len(coordinate_forces) != 1
            or any(force != coordinate_forces[0] for force in ambiguous_forces)
        ):
            return None
        if len(masses) != 1 or len(forces) != 1:
            return None

        mass, mass_unit, mass_label = masses[0]
        force, force_unit, force_label = forces[0]
        if not math.isfinite(mass) or mass <= 0 or not math.isfinite(force):
            return None
        if re.search(r"(?:规定|取|以)\s*\+x\s*(?:轴)?", statement):
            axis_description = "题面规定 $+x$ 方向，所有分量符号均按该约定解释。"
            negative_direction_description = "负号表示加速度方向与 $+x$ 相反。"
        elif re.search(
            r"(?<![A-Za-z])F_?net,?x\s*=", statement, re.IGNORECASE
        ):
            axis_description = "题面以 $x$ 轴有向分量给出合外力，符号按该坐标约定解释。"
            negative_direction_description = "负号表示加速度沿 $x$ 轴负方向。"
        elif any(marker in statement for marker in ("正方向", "负方向")):
            axis_description = "题面已给出正负方向约定，合外力符号按该约定解释。"
            negative_direction_description = "负号表示加速度沿题设负方向。"
        elif force == 0:
            axis_description = "题面未给坐标轴，可任选水平 $+x$；零合外力本身没有方向。"
            negative_direction_description = ""
        else:
            axis_description = "题面未给坐标轴，因此取该合外力方向为 $+x$ 方向。"
            negative_direction_description = ""
        return _NewtonAccelerationCase(
            net_force=force,
            force_unit=force_unit,
            force_label=force_label,
            mass=mass,
            mass_unit=mass_unit,
            mass_label=mass_label,
            axis_description=axis_description,
            negative_direction_description=negative_direction_description,
        )

    @staticmethod
    def _normalize_directed_force(
        match: re.Match[str],
    ) -> tuple[float, str, str] | None:
        raw_value = match.group(1)
        value, unit, label = TeachingService._normalize_measurement(
            raw_value, match.group(2), "force"
        )
        phrase = match.group(0)
        explicit_sign = raw_value.startswith(("+", "-"))
        has_coordinate_component = re.search(
            r"(?<![A-Za-z])F_?net,?x\s*=", phrase, re.IGNORECASE
        ) is not None
        if explicit_sign and not has_coordinate_component:
            # “大小”或普通力名称后的正负号没有坐标约定，不能把它补造成有向分量。
            return None
        if "负方向" in phrase:
            if explicit_sign:
                return None
            value = -abs(value)
        elif "正方向" in phrase:
            if explicit_sign and value < 0:
                return None
            value = abs(value)
        return value, unit, label

    @staticmethod
    def _normalize_measurement(
        raw_value: str, raw_unit: str, kind: str
    ) -> tuple[float, str, str]:
        value = float(raw_value)
        unit_key = raw_unit.lower()
        if kind == "mass":
            units = {
                "kg": ("kilogram", "kg"),
                "kilogram": ("kilogram", "kg"),
                "kilograms": ("kilogram", "kg"),
                "千克": ("kilogram", "kg"),
                "g": ("gram", "g"),
                "gram": ("gram", "g"),
                "grams": ("gram", "g"),
                "克": ("gram", "g"),
            }
        else:
            units = {
                "n": ("newton", "N"),
                "newton": ("newton", "N"),
                "newtons": ("newton", "N"),
                "牛顿": ("newton", "N"),
                "kn": ("kilonewton", "kN"),
                "kilonewton": ("kilonewton", "kN"),
                "kilonewtons": ("kilonewton", "kN"),
                "千牛": ("kilonewton", "kN"),
            }
        unit, label = units[unit_key]
        return value, unit, label

    @staticmethod
    def _unique_measurements(
        measurements: Sequence[tuple[float, str, str]],
    ) -> tuple[tuple[float, str, str], ...]:
        unique: list[tuple[float, str, str]] = []
        for measurement in measurements:
            if measurement not in unique:
                unique.append(measurement)
        return tuple(unique)

    @staticmethod
    def _select_application_source(
        hits: Sequence[KnowledgeHit],
    ) -> KnowledgeHit | None:
        for hit in hits:
            if "加速度" in hit.text and ("合外力" in hit.text or "合力" in hit.text):
                return hit
        return None

    def _select_hint_evidence(
        self,
        request: TeachingRequest,
        hits: Sequence[KnowledgeHit],
    ) -> tuple[tuple[KnowledgeHit, ...], KnowledgeHit | None]:
        """按应用层将要提示的物理主张选择来源，不使用检索排名兜底。"""

        claim = self._hint_claim(request)
        if claim != "net_force_acceleration":
            return tuple(hits), None

        package_identity = (self._package.package_id, self._package.version)
        if package_identity == _REVIEWED_MECHANICS_PACKAGE:
            targeted = self._normalize_and_validate_hits(
                tuple(
                    self._repository.search(
                        _NET_FORCE_HINT_QUERY,
                        self._package,
                        limit=self._search_limit,
                    )
                )
            )
            source_hit = next(
                (
                    hit
                    for hit in targeted
                    if hit.item_id == _NET_FORCE_HINT_SOURCE_ID
                    and self._supports_hint_claim(hit, claim)
                ),
                None,
            )
            if source_hit is None or self._evidence_error((source_hit,)) is not None:
                return tuple(hits), None
            remaining = sorted(
                (hit for hit in hits if hit.item_id != source_hit.item_id),
                key=lambda hit: hit.item_id,
            )
            selected_hits = (source_hit, *remaining[: self._search_limit - 1])
            return selected_hits, source_hit

        relevant = sorted(
            (hit for hit in hits if self._supports_hint_claim(hit, claim)),
            key=lambda hit: hit.item_id,
        )
        return tuple(hits), (relevant[0] if relevant else None)

    @staticmethod
    def _hint_claim(request: TeachingRequest) -> str | None:
        statement = unicodedata.normalize(
            "NFC", " ".join((request.problem, request.goal, *request.known_conditions))
        )
        asks_acceleration = "加速度" in request.goal or "加速度" in request.problem
        identifies_net_force = any(
            marker in statement
            for marker in ("合外力", "合力", "F_net", "Fnet", "只受到", "只受")
        )
        if asks_acceleration and identifies_net_force:
            return "net_force_acceleration"
        return None

    @staticmethod
    def _supports_hint_claim(hit: KnowledgeHit, claim: str) -> bool:
        if claim != "net_force_acceleration":
            return False
        text = unicodedata.normalize("NFC", hit.text)
        return "加速度" in text and ("合外力" in text or "合力" in text)

    @staticmethod
    def _source_is_complete(source: SourceReference) -> bool:
        return bool(
            source.source_id.strip()
            and source.source_version.strip()
            and source.license_id.strip()
            and source.locator
        )

    @staticmethod
    def _normalize_and_validate_hits(
        hits: Sequence[KnowledgeHit],
    ) -> tuple[KnowledgeHit, ...]:
        if len(hits) > MAX_TEACHING_HITS:
            raise ValueError(f"正式教学请求最多允许 {MAX_TEACHING_HITS} 条检索证据")
        normalized_hits: list[KnowledgeHit] = []
        for hit in hits:
            normalized_text = unicodedata.normalize("NFC", hit.text).strip()
            if len(normalized_text.encode("utf-8")) > MAX_KNOWLEDGE_TEXT_BYTES:
                raise ValueError(
                    f"知识条目 {hit.item_id!r} 的规范化文本超过 4 KiB 上限"
                )
            normalized_hits.append(
                KnowledgeHit(
                    item_id=hit.item_id,
                    text=normalized_text,
                    sources=hit.sources,
                )
            )
        return tuple(normalized_hits)

    def _evidence_error(self, hits: Sequence[KnowledgeHit]) -> str | None:
        if not hits:
            return "固定知识包没有命中可核验资料，本轮不能生成教学结论。"
        item_ids = [hit.item_id for hit in hits]
        if any(not item_id.strip() for item_id in item_ids) or len(set(item_ids)) != len(
            item_ids
        ):
            return "检索结果的稳定条目 ID 无效，本轮不能生成教学结论。"
        if any(
            not hit.sources
            or any(not self._source_is_complete(source) for source in hit.sources)
            for hit in hits
        ):
            return "检索结果缺少完整来源信息，本轮不能生成教学结论。"
        return None

    def _execute_tools(self, calls: Sequence[ToolCall]) -> tuple[ToolEvidence, ...]:
        results: list[ToolEvidence] = []
        for call in calls:
            tool = self._tools.get(call.name)
            if tool is None:
                results.append(
                    ToolEvidence(
                        name=call.name,
                        status="uncertain",
                        details={"reason": "工具未注册"},
                    )
                )
                continue
            try:
                details = dict(tool.execute(call.arguments))
            except Exception:
                results.append(
                    ToolEvidence(
                        name=call.name,
                        status="uncertain",
                        details={"reason": "工具执行失败"},
                    )
                )
                continue
            status = details.get("status", "ok")
            if not isinstance(status, str) or not status:
                status = "uncertain"
            results.append(
                ToolEvidence(name=call.name, status=status, details=details)
            )
        return tuple(results)

    @classmethod
    def _validate_tool_evidence_sizes(
        cls, results: Sequence[ToolEvidence]
    ) -> None:
        for result in results:
            payload = {
                "name": result.name,
                "status": result.status,
                "details": result.details,
            }
            try:
                encoded = cls._canonical_json(payload).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise ValueError("确定性工具证据必须是有限 JSON 数据") from exc
            if len(encoded) > MAX_TOOL_EVIDENCE_BYTES:
                raise ValueError(
                    f"工具 {result.name!r} 的证据 JSON 超过 4 KiB 上限"
                )

    @classmethod
    def _validate_messages_size(cls, messages: Sequence[Message]) -> None:
        payload = [
            {"role": message.role, "content": message.content} for message in messages
        ]
        encoded = cls._canonical_json(payload).encode("utf-8")
        if len(encoded) > MAX_MESSAGES_BYTES:
            raise ValueError("正式教学请求的 messages JSON 超过 32 KiB 上限")

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _verified_acceleration(result: ToolEvidence) -> tuple[float, str] | None:
        if result.name != "physics_deterministic" or result.status != "verified":
            return None
        if result.details.get("operation") != "newton_acceleration":
            return None
        data = result.details.get("data")
        if not isinstance(data, Mapping) or data.get("conclusion") != "calculated":
            return None
        value = data.get("value")
        unit = data.get("unit")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not isinstance(unit, str)
            or unit not in _SAFE_ACCELERATION_UNITS
        ):
            return None
        return float(value), _SAFE_ACCELERATION_UNITS[unit]

    @staticmethod
    def _render_hint(level: int, source_id: str) -> str:
        templates = {
            1: (
                "第 1 级提示（概念模型）：先逐项列出题目明确给出的条件与目标，标出仍需"
                "确认的适用条件；暂时不要代入数值。"
            ),
            2: (
                "第 2 级提示（关系式）：从已核验条目中选择与目标直接相关的定义或关系式，"
                "并逐项核对其适用条件；暂时不要求最终数值。"
            ),
            3: (
                "第 3 级提示（关键步骤）：把题设量与已选关系式逐项对应，先完成下一步"
                "变形；请自行完成最终代入、单位和数量级检查。"
            ),
        }
        return f"{templates[level]} 可查阅已核验条目：[source:{source_id}]"

    @staticmethod
    def _render_newton_acceleration(
        case: _NewtonAccelerationCase,
        *,
        value: float,
        unit: str,
        source_id: str,
    ) -> str:
        value_text = format(value, ".12g")
        if value == 0:
            magnitude = "零"
        else:
            magnitude = f"$10^{{{math.floor(math.log10(abs(value)))}}}$"
        direction_text = (
            case.negative_direction_description + "\n"
            if value < 0 and case.negative_direction_description
            else ""
        )
        return (
            f"条件：质量 $m={format(case.mass, '.12g')}\\,{case.mass_label}$，水平方向合外力 "
            f"$F_{{\\mathrm{{net}},x}}={format(case.net_force, '.12g')}\\,{case.force_label}$，"
            "水平面光滑。\n"
            "目标：求物体的水平加速度。\n"
            "参考系与模型：取地面为惯性参考系；计算使用题设明确的水平方向合外力，"
            f"{case.axis_description}"
            f"[source:{source_id}]\n"
            "关系式：$\\sum F_x=ma_x$。\n"
            f"确定性工具代入并换算得到：$a_x={value_text}\\,{unit}$。\n"
            f"{direction_text}"
            f"单位检查已由确定性工具通过；结果的数量级约为 {magnitude}\\,{unit}。"
        )

    def _messages(
        self,
        request: TeachingRequest,
        hits: Sequence[KnowledgeHit],
        tool_results: Sequence[ToolEvidence],
    ) -> tuple[Message, ...]:
        if request.full_solution:
            mode = (
                "学生已明确请求完整解答。给出条件、目标、假设、参考系、模型及适用理由、"
                "学生可读的分步推导、单位与数量级检查和结论。"
            )
        else:
            hint_instruction = {
                1: "只给概念模型提示，不给关系式或关键步骤。",
                2: "给概念模型和关系式提示，不给关键代数步骤或最终答案。",
                3: "给出下一关键步骤，但不要给完整推导或最终答案。",
            }[request.hint_level]
            mode = f"这是应用层指定的第 {request.hint_level} 级提示。{hint_instruction}"

        evidence_payload = [
            {
                "item_id": hit.item_id,
                "text": hit.text,
                "sources": [
                    {
                        "source_id": source.source_id,
                        "source_version": source.source_version,
                        "license_id": source.license_id,
                        "locator": dict(source.locator),
                    }
                    for source in hit.sources
                ],
            }
            for hit in hits
        ]
        tool_payload = [
            {"name": result.name, "status": result.status, "details": result.details}
            for result in tool_results
        ]
        request_payload = {
            "problem": request.problem,
            "goal": request.goal,
            "known_conditions": request.known_conditions,
            "student_answer": request.student_answer,
            "diagnostics": [
                {"error_type": signal.error_type, "evidence": signal.evidence}
                for signal in request.error_signals
            ],
        }
        allowed_ids = ", ".join(hit.item_id for hit in hits)
        return (
            Message(
                role="system",
                content=(
                    "你是大学物理提示式教学助手。只输出给学生看的简洁教学文本，不输出或索要"
                    "内部推理。模型文字不是事实证据。每项事实必须使用且只能使用本次允许的"
                    f"稳定引用 [source:<item_id>]；允许的 item_id：{allowed_ids}。{mode}"
                ),
            ),
            Message(
                role="user",
                content=(
                    "结构化教学请求：\n"
                    + self._canonical_json(request_payload)
                    + "\n固定知识包证据：\n"
                    + self._canonical_json(evidence_payload)
                    + "\n确定性工具结果（不能代替物理模型判断）：\n"
                    + self._canonical_json(tool_payload)
                ),
            ),
        )

    @staticmethod
    def _check_citations(
        text: str, hits: Sequence[KnowledgeHit]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        allowed = {hit.item_id for hit in hits}
        accepted: list[str] = []
        rejected: list[str] = []
        tokens = _CITATION_TOKEN_RE.findall(text)
        for token in tokens:
            match = _CITATION_RE.fullmatch(token)
            if match is None:
                malformed = token.removeprefix("[source:").removesuffix("]")
                rejected.append(malformed or "<malformed>")
                continue
            item_id = match.group(1)
            target = accepted if item_id in allowed else rejected
            if item_id not in target:
                target.append(item_id)
        if "[source:" in _CITATION_TOKEN_RE.sub("", text):
            rejected.append("<malformed>")
        return tuple(accepted), tuple(rejected)


def preflight_teaching_request(
    request: TeachingRequest,
) -> TeachingResponse | None:
    """纯本地预检案例 A；条件齐全时返回 ``None`` 交由正式教学服务处理。"""

    TeachingService._validate_request(request)
    missing_conditions = TeachingService._collect_missing_conditions(request)
    if not missing_conditions:
        return None
    missing = "、".join(missing_conditions)
    return TeachingResponse(
        status="needs_information",
        completed=False,
        publishable_final=False,
        hint_level=request.hint_level,
        diagnostics=TeachingService._diagnose(request.error_signals),
        citations=(),
        rejected_citations=(),
        source_evidence=(),
        model_output=None,
        tool_results=(),
        readable_text=(
            f"现有条件不足以得到唯一结论。请补充：{missing}。"
            "如果暂时无法补充，应按这些条件的不同取值分情况讨论。"
        ),
    )
