"""Ordo Agent编排：意图路由、主辅协作、工具调用与结果合成。"""
import asyncio
import inspect
import json
import logging
import os
import re
import time
import uuid
from collections import deque
from datetime import datetime
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from anthropic import AsyncAnthropic

from agents.tools import (
    AgentToolSpec,
    create_handoff_summary,
    build_shared_rag_tools,
    escalation_tools,
    general_tools,
    collaboration_tools,
    technical_tools,
)
from core.intent_recognizer import IntentCategory, IntentRecognizer, UrgencyLevel
from core.support_boundaries import is_general_overview, platform_fault_topic
from core.llm_utils import completed_text, response_metadata, short_task_options

logger = logging.getLogger(__name__)


# ── 数据结构 ──────────────────────────────────────────────────────────────────

class AgentType(Enum):
    GENERAL   = "general"    # 通用客服
    COLLABORATION = "collaboration" # 功能、项目协作与成员权限
    TECHNICAL = "technical"              # 固定产品接口支持
    ESCALATION = "escalation" # 人工升级与交接


@dataclass(frozen=True)
class AgentProfile:

    role: str
    mission: str
    workflow: Tuple[str, ...]
    input_contract: Tuple[str, ...]
    output_contract: Tuple[str, ...]
    handoff_conditions: Tuple[str, ...] = ()
    tool_scope: Tuple[str, ...] = ()
    model: Optional[str] = None
    temperature: float = 0.2
    max_tokens: int = 1024


def _env_float(name: str, default: float) -> float:
    """读取可选浮点配置；错误配置不应阻塞服务启动。"""
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("忽略非法浮点配置 %s=%r", name, os.getenv(name))
        return default


def _env_int(name: str, default: int) -> int:
    """读取可选整数配置；错误配置不应阻塞服务启动。"""
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        logger.warning("忽略非法整数配置 %s=%r", name, os.getenv(name))
        return default


@dataclass
class AgentStats:
    """Agent 运行时统计，供 Monitor 和路由决策使用。"""
    total:     int   = 0
    success:   int   = 0
    total_ms:  float = 0.0
    monitor_penalty: float = 0.0

    inflight: int = 0

    @property
    def completed(self):
        return max(0, self.total - self.inflight)

    samples: Any = field(default_factory=lambda: deque(maxlen=50))
    retry_at: float = 0.0
    probe_running: bool = False
    probe_owner: Optional[str] = None
    observer: Any = field(default=None, repr=False)

    def recent(self):
        cutoff = time.monotonic() - 300
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        n = len(self.samples)
        return n, sum(x[1] for x in self.samples) / n if n else 1.0, sum(x[2] for x in self.samples) / n if n else 0.0

    def available(self):
        if not self.retry_at and self.monitor_penalty:
            self.set_penalty(self.monitor_penalty)
        return not self.probe_running and (not self.retry_at or time.monotonic() >= self.retry_at)

    def set_penalty(self, penalty):
        self.monitor_penalty = penalty
        threshold = min(max(_env_float("ORDO_MONITOR_FALLBACK_PENALTY", 0.5), 0.01), 0.9)
        if penalty >= threshold and not self.retry_at:
            self.retry_at = time.monotonic() + max(1, _env_float("ORDO_MONITOR_COOLDOWN_SECONDS", 60))

    def record(self, success, ms, request_id=None):
        if self.probe_running and request_id == self.probe_owner:
            self.probe_running = False
            self.probe_owner = None
            if success:
                self.samples.clear()
                self.retry_at = 0.0
                self.monitor_penalty = 0.0
            else:
                self.retry_at = time.monotonic() + max(1, _env_float("ORDO_MONITOR_COOLDOWN_SECONDS", 60))
        self.samples.append((time.monotonic(), success, ms))
        if self.observer:
            self.observer(ms)

    @property
    def success_rate(self) -> float:
        return self.success / self.completed if self.completed else 1.0

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.completed if self.completed else 0.0

    def routing_score(self) -> float:
        """路由评分：成功率高、延迟低的 Agent 得分高。"""
        latency_score = 1.0 / (1.0 + self.avg_ms / 1000)
        base_score = self.success_rate * 0.7 + latency_score * 0.3
        return base_score * max(0.0, 1.0 - self.monitor_penalty)


@dataclass
class AgentResponse:
    agent_type:  AgentType
    content:     str
    success:     bool
    confidence:  float = 1.0
    latency_ms:  float = 0.0
    escalate:    bool  = False   # 是否需要升级
    tools_used:  List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    model_calls: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "success"
    error_code: Optional[str] = None



@dataclass
class AgentCallResult:
    """一次 LLM 调用的请求级结果，不在共享 Agent 实例上保存状态。"""
    content: str
    tools_used: List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    model_calls: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "success"



class AgentCallError(RuntimeError):
    """保留失败前已经发生的工具调用，供请求 trace 记录。"""

    def __init__(self, message: str, tools_used: List[str], tool_traces: List[Dict[str, Any]], model_calls=None):
        super().__init__(message)
        self.tools_used = list(tools_used)
        self.tool_traces = list(tool_traces)
        self.model_calls = list(model_calls or [])


@dataclass
class Request:
    message:     str
    user_id:     str
    conv_id:     str
    context:     str = ""        # 来自 MemoryManager 的格式化上下文
    history:     Optional[List[Dict[str, str]]] = None  # 对话历史，传给意图识别
    entities:    Dict[str, List[str]] = field(default_factory=dict)
    intent:      Optional[IntentCategory] = None
    intent_group: Optional[str] = None
    urgency:     Optional[UrgencyLevel]   = None
    intent_confidence: float = 1.0
    request_id:  str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    conversation_state: Dict[str, Any] = field(default_factory=dict)
    tool_attempts: List[Dict[str, Any]] = field(default_factory=list)
    unsafe_tool_calls: set = field(default_factory=set)
    before_unsafe_tool: Optional[Any] = None



@dataclass
class OrchestratorResult:
    request_id:  str
    response:    str
    agent_type:  AgentType
    intent:      Optional[IntentCategory]
    escalated:   bool  = False
    latency_ms:  float = 0.0
    agent_types: List[AgentType] = field(default_factory=list)
    primary_agent: Optional[AgentType] = None
    supporting_agents: List[AgentType] = field(default_factory=list)
    tools_used: List[str] = field(default_factory=list)
    tool_traces: List[Dict[str, Any]] = field(default_factory=list)
    routing_reason: str = ""
    routing_confidence: float = 0.0
    model_calls: List[Dict[str, Any]] = field(default_factory=list)
    status: str = "success"
    retryable: bool = True
    error_code: Optional[str] = None



@dataclass
class RoutingDecision:
    """一次请求的结构化路由决策。"""
    primary_agent: AgentType
    supporting_agents: List[AgentType] = field(default_factory=list)
    reason: str = ""
    confidence: float = 0.0

    @property
    def agent_types(self) -> List[AgentType]:
        return [self.primary_agent] + self.supporting_agents

    @property
    def multi_agent(self) -> bool:
        return bool(self.supporting_agents)


# ── 基础 Agent ────────────────────────────────────────────────────────────────

class BaseAgent:
    """所有 Agent 的基类，封装 LLM 调用、角色契约和统计。"""

    agent_type: AgentType
    system_prompt: str
    profile: AgentProfile

    def __init__(
        self,
        client: AsyncAnthropic,
        model: str,
        skill_manager: Optional[Any] = None,
        profile: Optional[AgentProfile] = None,
    ):
        self._client = client
        self.profile = profile or self.profile
        self._model  = self.profile.model or model
        self._skill_manager = skill_manager
        self.stats   = AgentStats()
        self._shared_tools: Dict[str, AgentToolSpec] = {}

    def get_tools(self):
        return {name: spec for name, spec in self._shared_tools.items()
                if (name in self.profile.tool_scope or (spec.allowed_agents is not None and self.agent_type.value in spec.allowed_agents))
                and (spec.allowed_agents is None or self.agent_type.value in spec.allowed_agents)}

    def set_shared_tools(self, tools: Optional[Dict[str, AgentToolSpec]]) -> None:
        self._shared_tools = dict(tools or {})

    async def handle(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        if self.agent_type not in (AgentType.GENERAL, AgentType.ESCALATION):
            if not self.stats.available():
                return AgentResponse(self.agent_type, "专业支持正在恢复，请稍后重试。", False, status="failed", error_code="agent_busy")
            if self.stats.retry_at:
                self.stats.probe_running, self.stats.probe_owner = True, req.request_id
        self.stats.total += 1
        self.stats.inflight += 1
        try:
            call_result = await self._call_llm(req)
            ms = (time.monotonic() - t0) * 1000
            self.stats.record(call_result.status == "success", ms, req.request_id)
            self.stats.success += int(call_result.status == "success")
            escalate = self._needs_escalation(call_result.content)
            return AgentResponse(
                agent_type=self.agent_type,
                content=call_result.content,
                success=True,
                latency_ms=ms,
                escalate=escalate,
                tools_used=call_result.tools_used,
                tool_traces=call_result.tool_traces,
                model_calls=call_result.model_calls,
            )
        except asyncio.CancelledError:
            self.stats.record(False, (time.monotonic() - t0) * 1000, req.request_id)
            raise
        except Exception as ex:
            ms = (time.monotonic() - t0) * 1000
            self.stats.record(False, ms, req.request_id)
            logger.error(f"{self.agent_type.value} 处理失败: {ex}")
            tools_used = ex.tools_used if isinstance(ex, AgentCallError) else []
            tool_traces = ex.tool_traces if isinstance(ex, AgentCallError) else []
            return AgentResponse(
                agent_type=self.agent_type,
                content="抱歉，处理您的请求时出现问题，请稍后重试。",
                success=False,
                status="failed", error_code="agent_failed",
                latency_ms=ms,
                tools_used=list(tools_used),
                tool_traces=list(tool_traces),
                model_calls=list(ex.model_calls) if isinstance(ex, AgentCallError) else [],
            )
        finally:
            # No await between these updates: in-flight requests never count as failures.
            self.stats.total_ms += (time.monotonic() - t0) * 1000
            self.stats.inflight -= 1

    async def _call_llm(self, req: Request) -> AgentCallResult:
        def _clean(s: str) -> str:
            return s.encode("utf-8", errors="ignore").decode("utf-8")

        messages = []
        if req.context:
            messages.append({"role": "user", "content": f"[背景信息]\n{_clean(req.context)}"})
            messages.append({"role": "assistant", "content": "好的，我已了解背景信息。"})
        if req.entities:
            entities_text = json.dumps(req.entities, ensure_ascii=False)
            messages.append({"role": "user", "content": f"[结构化实体]\n{_clean(entities_text)}"})
            messages.append({"role": "assistant", "content": "好的，我会结合这些结构化实体处理。"})
        role_packet = self._build_role_packet(req)
        if role_packet:
            messages.append({"role": "user", "content": f"[角色输入契约]\n{_clean(role_packet)}"})
            messages.append({"role": "assistant", "content": "好的，我会按照该角色的输入和输出契约处理。"})
        messages.append({"role": "user", "content": _clean(req.message)})

        tools = self.get_tools()
        tools_used: List[str] = []
        tool_traces: List[Dict[str, Any]] = []
        model_calls: List[Dict[str, Any]] = []
        tool_cache: Dict[str, Any] = {}
        seen_chunks = set()
        force_final = False
        try:
            for round_index in range(3):
                final_round = force_final or round_index == 2
                system = self._build_system_prompt(req)
                if final_round:
                    system += (
                        "\n[本轮必须答复] 不再调用工具。根据已有有效资料直接回答，保留来源；"
                        "资料不足时给出有依据的部分答案并说明缺失项，不补造菜单或业务结果。"
                        "优先核心结论和不超过6条步骤，简短收尾。"
                    )
                request_kwargs: Dict[str, Any] = {
                    "model": self._model,
                    "max_tokens": self.profile.max_tokens,
                    "temperature": self.profile.temperature,
                    "system": system,
                    "messages": messages,
                }
                if final_round:
                    request_kwargs.update(short_task_options(self._client, self._model))
                if tools and not final_round:
                    request_kwargs["tools"] = [
                        {
                            "name": spec.name,
                            "description": spec.description,
                            "input_schema": spec.input_schema,
                        }
                        for spec in tools.values()
                    ]
                resp = await asyncio.wait_for(self._client.messages.create(**request_kwargs), timeout=max(1, _env_float("ORDO_LLM_TIMEOUT_S", 15)))
                model_calls.append({"agent_type": self.agent_type.value, "round": round_index + 1,
                                    "final_round": final_round, **response_metadata(resp)})
                tool_uses = [block for block in (resp.content or []) if self._block_type(block) == "tool_use"]
                if not tool_uses:
                    if getattr(resp, "stop_reason", None) == "max_tokens" and not final_round:
                        force_final = True
                        continue
                    from core.response_guidance import clean_guidance_reply
                    return AgentCallResult(
                        content=clean_guidance_reply(completed_text(resp), req.message, req.intent.value if req.intent else ''),
                        tools_used=list(tools_used),
                        tool_traces=list(tool_traces),
                        model_calls=list(model_calls),
                    )

                if final_round:
                    raise AgentCallError("最终答复仍请求工具，已阻止执行", tools_used, tool_traces, model_calls)

                messages.append({"role": "assistant", "content": resp.content})
                tool_results = []
                for block in tool_uses:
                    name = self._block_value(block, "name")
                    tool_use_id = self._block_value(block, "id")
                    args = self._block_value(block, "input") or {}
                    spec = tools.get(name)
                    tool_t0 = time.monotonic()
                    call_success = True
                    result_success: Optional[bool] = None
                    error_text = ""
                    reused = False
                    if spec is None:
                        call_success = False
                        result: Any = {"success": False, "error": f"工具不在 {self.agent_type.value} Agent 白名单中"}
                        error_text = result["error"]
                    else:
                        try:
                            self._validate_tool_input(spec, args)
                            cache_key = name + ":" + json.dumps(args, sort_keys=True, ensure_ascii=False)
                            if cache_key in tool_cache:
                                result = tool_cache[cache_key]
                                reused = True
                                force_final = True
                            else:
                                if not spec.retry_safe:
                                    operation = json.dumps([name, args], sort_keys=True, ensure_ascii=True)
                                    if operation in req.unsafe_tool_calls:
                                        raise ValueError("不能重复执行相同的副作用操作")
                                    req.unsafe_tool_calls.add(operation)
                                req.tool_attempts.append({"tool_name": name, "retry_safe": spec.retry_safe})
                                if not spec.retry_safe and req.before_unsafe_tool is not None:
                                    await req.before_unsafe_tool(name)
                                result = spec.handler(req, args)
                                if inspect.isawaitable(result):
                                    result = await result
                                if not isinstance(result, dict) or result.get("success", True):
                                    tool_cache[cache_key] = result
                            tools_used.append(name)
                            if isinstance(result, dict) and "success" in result:
                                result_success = bool(result.get("success"))
                        except Exception as ex:
                            call_success = False
                            logger.warning("Agent 工具 %s 执行失败: %s", name, ex)
                            error_text = str(ex)
                            result = {"success": False, "error": error_text}
                    tool_latency_ms = (time.monotonic() - tool_t0) * 1000
                    if not error_text and isinstance(result, dict):
                        error_text = str(result.get("error", "") or "")
                    evidence = []
                    if name == "search_knowledge_base" and isinstance(result, dict):
                        evidence = [{key: item.get(key) for key in ("document_id", "chunk_id", "version", "source")}
                                    for item in result.get("results", []) if isinstance(item, dict)]
                        ids = {item["chunk_id"] for item in evidence if item.get("chunk_id")}
                        if ids and not (ids - seen_chunks):
                            force_final = True
                        seen_chunks.update(ids)
                    tool_traces.append(
                        {
                            "source": result.get("source", "local") if isinstance(result, dict) else "local",
                            "mcp_server": result.get("mcp_server") if isinstance(result, dict) else None,
                            "mcp_tool": result.get("mcp_tool") if isinstance(result, dict) else None,
                            "retry_safe": spec.retry_safe if spec else True,
                            "agent_type": self.agent_type.value,
                            "tool_name": name,
                            "tool_use_id": tool_use_id,
                            "input": dict(args),
                            "success": call_success,
                            "result_success": result_success,
                            "latency_ms": round(tool_latency_ms, 1),
                            "cached": bool(result.get("cached")) if isinstance(result, dict) else False,
                            "request_cache_hit": reused,
                            "reranked": bool(result.get("reranked")) if isinstance(result, dict) else False,
                            "error": error_text,
                            "evidence": evidence,
                            "diagnostics": result.get("diagnostics", {}) if isinstance(result, dict) else {},
                        }
                    )
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": json.dumps(result, ensure_ascii=False),
                        "is_error": not call_success or result_success is False,
                    })
                messages.append({"role": "user", "content": tool_results})

            raise AgentCallError(
                f"{self.agent_type.value} 工具调用超过最大轮数",
                tools_used,
                tool_traces,
                model_calls,
            )
        except AgentCallError:
            raise
        except Exception as ex:
            raise AgentCallError(str(ex), tools_used, tool_traces, model_calls) from ex

    @staticmethod
    def _block_type(block: Any) -> Optional[str]:
        if isinstance(block, dict):
            return block.get("type")
        return getattr(block, "type", None)

    @staticmethod
    def _block_value(block: Any, key: str) -> Any:
        if isinstance(block, dict):
            return block.get(key)
        return getattr(block, key, None)

    @staticmethod
    def _validate_tool_input(spec: AgentToolSpec, args: Any) -> None:
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是 JSON 对象")
        from jsonschema import validators, ValidationError
        try:
            validators.validator_for(spec.input_schema)(spec.input_schema).validate(args)
        except ValidationError as ex:
            if ex.validator == "additionalProperties":
                raise ValueError("不允许的工具参数（additionalProperties）") from None
            raise ValueError(f"工具参数未通过 JSON Schema 校验（{ex.validator}）") from None

    def _build_system_prompt(self, req: Request) -> str:
        """把角色契约和动态 Skills 拼入 system prompt。"""
        profile_prompt = (
            f"\n\n[角色契约]\n"
            f"角色：{self.profile.role}\n"
            f"职责：{self.profile.mission}\n"
            f"处理流程：{' -> '.join(self.profile.workflow)}\n"
            f"可用输入：{'；'.join(self.profile.input_contract)}\n"
            f"输出要求：{'；'.join(self.profile.output_contract)}\n"
            f"升级条件：{'；'.join(self.profile.handoff_conditions) or '无，按通用客服规则处理'}\n"
            f"允许的数据/工具范围：{'、'.join(self.profile.tool_scope) or '仅使用当前请求上下文'}\n"
            "按资料和用户描述分析，保留条件与不确定性；不把假设当成事实，不编造操作结果。"
            "你是团队在线协作平台内的Ordo智能支持中心，平台具备操作页面。"
            "当前工具用于知识检索和信息分析，业务办理由用户在平台页面完成；不承诺提供ID即可代办。"
            "本次检索未找到某条款不等于整库不存在；资料冲突时说明分歧，不改写用户的假设条件为既成事实。"
            "内部字段、工具JSON和角色名称转成自然语言，不重复整套待核实清单；优先直接、简短地解决用户疑问。"
            "本次角色规则以最新设计为准：admin可管理本工作空间全部公开与私有项目；"
            "member默认访问公开项目，私有项目按授权；guest只访问明确授权项目。"
            "guest最高comment，不可授予edit或manage；read不自动包含编辑、批注或删除权限；所有角色都不能跨租户。"
            "项目read申请可由本空间admin或该项目负责人审批，负责人仅审批本项目。资料冲突时按当前角色规则解释。"
            "参数、限额和等待时长需要具体资料或配置依据；不能把示例时长泛化为恢复保证。"
        )
        base_prompt = f"{self.system_prompt}{profile_prompt}"
        from core.response_guidance import RESPONSE_GUIDANCE
        if self._skill_manager is None:
            return base_prompt + RESPONSE_GUIDANCE
        skill_prompt = self._skill_manager.prompt_for(req.message, self.agent_type.value)
        if not skill_prompt:
            return base_prompt + RESPONSE_GUIDANCE
        return f"{base_prompt}\n\n[动态 Skills]\n{skill_prompt}{RESPONSE_GUIDANCE}"

    def _build_role_packet(self, req: Request) -> str:
        """给子 Agent 的确定性输入包；子类可补充领域字段。"""
        packet = {
            "agent_type": self.agent_type.value,
            "intent": req.intent.value if req.intent else None,
            "intent_group": req.intent_group,
            "urgency": req.urgency.name if req.urgency else None,
            "intent_confidence": round(req.intent_confidence, 4),
            "available_entities": req.entities or {},
            "entity_source": "user_reported_unverified",
        }
        return json.dumps(packet, ensure_ascii=False)

    def _needs_escalation(self, content: str) -> bool:
        """检测 Agent 是否建议升级（简单关键词检测）。"""
        keywords = ["转人工", "人工客服", "escalate", "specialist", "无法处理"]
        return any(kw in content for kw in keywords)


class GeneralAgent(BaseAgent):
    agent_type    = AgentType.GENERAL
    profile = AgentProfile(
        role="SaaS 产品概述与需求澄清",
        mission="介绍平台整体用途与用户能获得的帮助，理解尚未明确的目标，提供简短全局指引并衔接对话；具体功能与权限由协作支持处理。",
        workflow=("回应诉求", "核对已知资料", "澄清缺失信息", "给出下一步"),
        input_contract=("对话历史", "用户画像", "意图与紧急度", "知识库上下文"),
        output_contract=("先回应核心问题", "信息不足时只询问必要字段", "给出有用的下一步"),
        handoff_conditions=("专业诊断暂不可用", "用户明确要求人工"),
        tool_scope=("search_knowledge_base", "inspect_request_context", "suggest_required_fields"),
        temperature=0.3,
        max_tokens=900,
    )
    system_prompt = (
        "你是团队在线协作平台内的 Ordo 通用支持角色，负责整体产品介绍、可提供的帮助、问候反馈和诉求澄清。"
        "快速判断用户想了解什么、完成什么操作或解决什么阻碍，用清晰、礼貌、可执行的方式推动问题解决。"
        "尚无具体目标的新用户先给简短全局指引，必要时追问；不展开具体功能的完整教程，不猜测权限或API故障结论。"
        "具体功能和权限规则属于协作支持，平台使用故障及固定接口问题属于技术支持。用户已经说清目标时不让其重复选择问题类别。"
        "若专业领域暂不可用，说明分析限制，仍提供有依据的解释和建议。"
        "可解释用户贴出的状态或整理沟通材料，介绍支持范围时优先说明能提供的帮助。"
        "支持范围限于项目协作、成员权限、平台使用故障及固定API；其他领域先澄清具体需求。"
    )

    def _build_role_packet(self, req: Request) -> str:
        packet = json.loads(super()._build_role_packet(req))
        packet["support_domains"] = ["collaboration", "technical"]
        packet["response_mode"] = "answer_or_clarify"
        return json.dumps(packet, ensure_ascii=False)

    def get_tools(self) -> Dict[str, AgentToolSpec]:
        tools = super().get_tools()
        tools.update(general_tools())
        return tools


class CollaborationAgent(BaseAgent):
    agent_type = AgentType.COLLABORATION
    profile = AgentProfile(
        role="项目协作与成员权限支持",
        mission="连续解释工作空间、项目与文档操作、邀请分享、角色权限及申请审批规则，按用户描述分析协作受阻。",
        workflow=("理解具体目标", "检索功能及权限资料", "按描述分析条件", "给出步骤与用户自查方法"),
        input_contract=("目标功能", "用户描述的页面、角色和提示", "已尝试步骤", "操作及权限资料"),
        output_contract=("功能或规则及适用条件", "针对性的步骤或原因分析", "用户可观察的结果与下一步"),
        handoff_conditions=("独立接口规范或错误需API支持", "实际审批由本空间admin或本项目负责人处理"),
        tool_scope=("search_knowledge_base", "inspect_collaboration_fields"),
        temperature=0.1,
        max_tokens=1400,
    )
    system_prompt = (
        "你是 Ordo 的协作支持角色，负责具体功能与协作规则：工作空间、项目创建、成员邀请、文档编辑批注与分享、角色权限及访问申请。"
        "创建项目、邀请成员和确认guest权限属于同一协作流程，应连续回答，不拆成两个角色。平台整体介绍或尚无具体目标的入门问题由通用支持处理。"
        "页面、按钮、步骤须有本产品资料依据；先给能回答的部分，不要求通用教程先提供业务ID。"
        "admin可管理本空间全部公开及私有项目，无需逐个申请read；member默认访问公开项目，私有项目按授权。"
        "guest仅访问明确授权项目，最高comment，不能获得edit或manage；read不自动包含批注、编辑或删除。"
        "所有角色不能跨租户，也不能绕过API scope；read申请由本空间admin或该项目负责人审批，负责人只处理本项目。"
        "按用户描述的角色和状态给适用步骤，不编造创建、邀请、审批结果或单号。"
        "不将scope或403单独视为网页权限已确认的问题；有独立API或平台故障诉求时与技术支持协作。"
    )

    def _build_role_packet(self, req: Request) -> str:
        packet = json.loads(super()._build_role_packet(req))
        packet["collaboration_fields"] = {
            "workspace_id": req.entities.get("workspace_id", []),
            "project_id": req.entities.get("project_id", []),
            "requested_permission": "read",
        }
        packet["state_source"] = "user_reported_unverified"
        return json.dumps(packet, ensure_ascii=False)

    def get_tools(self) -> Dict[str, AgentToolSpec]:
        return {**super().get_tools(), **collaboration_tools()}

class TechnicalAgent(BaseAgent):
    agent_type    = AgentType.TECHNICAL
    profile = AgentProfile(
        role="平台技术故障与API支持",
        mission="依据资料分析页面加载、文件传输、保存协同、站内提醒及固定API问题，提供低风险自查与复测方法。",
        workflow=("区分故障场景与规则限制", "检索操作与技术资料", "分析用户脱敏现象", "先保护内容再给针对性检查"),
        input_contract=("页面或固定接口", "用户提供的脱敏提示和响应", "技术及API资料", "已尝试步骤"),
        output_contract=("规范与可判断内容", "可能原因", "针对性排查建议", "自行复测方法"),
        handoff_conditions=("独立权限规则或申请问题由协作支持分析", "持续故障可整理沟通材料", "重大故障线索需管理方核实"),
        tool_scope=("search_knowledge_base", "lookup_error_code", "build_diagnostic_plan"),
        temperature=0.1,
        max_tokens=1400,
    )
    system_prompt = (
        "你是 Ordo 的技术支持角色，依据资料排查页面白屏/加载、文件上传下载、文档保存协同和站内提醒问题，同时解释固定产品接口 GET /sandbox/product/v1/me、"
        "GET /sandbox/product/v1/projects、GET /sandbox/product/v1/projects/{id}。"
        "功能怎么用和单纯权限规则归协作支持；按用户描述的权限分析当前现象。"
        "保存或同步异常先备份并核对完整性，再检查权限与连接；如需刷新，先比对当前正文和他人修改，仅补缺失部分，不指导整篇覆盖。"
        "版本历史只是辅助，不凭记录缺失断定未保存；无痕、切网和单个状态码不直接证明根因。"
        "通知以工作台收到的批注提醒为准，不推荐未定义的桌面/邮件/短信推送，不让用户反复重发相同批注。"
        "网络和浏览器检查按资料低风险顺序，不关闭企业受管代理/VPN或安全扫描，不编造文件限制。"
        "400检查请求规范；401检查凭据有效性；403分别检查 scope 和调用用户的项目访问授权，两者都要满足。"
        "429区分 RATE_LIMITED 短时限流与 QUOTA_EXCEEDED 累计额度，不推荐付费套餐。"
        "可给接口示例和自行复测方法，用户实际复测应保持原凭据和原身份，不换管理员绕过错误。"
        "不执行任意代码、外部URL或生产密钥操作。不索取完整Token、密钥或Cookie。"
    )

    def _build_role_packet(self, req: Request) -> str:
        packet = json.loads(super()._build_role_packet(req))
        packet["diagnostic_fields"] = {
            "fault_topic": platform_fault_topic(req.message) or "api",
            "error_codes": req.entities.get("error_code", []),
            "business_error_codes": req.entities.get("business_error_code", []),
            "request_ids": req.entities.get("request_id", []),
            "project_ids": req.entities.get("project_id", []),
            "scopes": req.entities.get("scope", []),
            "api_paths": req.entities.get("api_path", []),
            "environment_hint": "按实际场景确认页面或固定接口、脱敏提示、是否有未保存内容和已尝试步骤",
            "risk_boundary": "不得要求密码、验证码、完整密钥；不得建议破坏性操作",
        }
        return json.dumps(packet, ensure_ascii=False)

    def get_tools(self) -> Dict[str, AgentToolSpec]:
        tools = super().get_tools()
        tools.update(technical_tools())
        return tools


class EscalationAgent(BaseAgent):
    """人工升级节点。

    升级不是一个普通问答 Prompt：它应该生成标准化的交接信息并停止普通
    Agent 继续编造答案。生产环境可在这里接工单系统、人工队列或 Webhook。
    """

    agent_type = AgentType.ESCALATION
    profile = AgentProfile(
        role="人工升级与交接",
        mission="确认升级原因，整理已知上下文，告知用户下一步，不执行未经授权的业务操作。",
        workflow=("确认升级原因", "整理已知信息", "标记优先级", "生成交接摘要"),
        input_contract=("用户消息", "意图", "紧急度", "结构化实体", "对话背景"),
        output_contract=("升级原因", "已知信息摘要", "还需补充的信息", "保守的后续说明"),
        handoff_conditions=("用户明确要求人工", "紧急或高风险场景"),
        tool_scope=("create_handoff_summary",),
        temperature=0.0,
        max_tokens=500,
    )
    system_prompt = "你负责客服人工升级交接，不要继续模拟已完成的后台操作。"

    def get_tools(self) -> Dict[str, AgentToolSpec]:
        tools = super().get_tools()
        tools.update(escalation_tools())
        return tools

    async def handle(self, req: Request) -> AgentResponse:
        t0 = time.monotonic()
        self.stats.total += 1
        summary = create_handoff_summary(req, {"reason": "用户请求人工或持续受阻"})
        content = (
            "当前暂不支持接通人工、创建工单或查询处理进度；本次尚未提交任何申请或工单。\n\n"
            "可以先保留以下沟通材料：\n"
            "1. 问题现象及受影响的功能。\n"
            "2. 页面提示或脱敏错误信息。\n"
            "3. 已尝试的步骤、结果及仍未解决的事项。\n\n"
            "如果希望继续分析，可以说明当前受阻的环节，我会结合产品资料提供指导。"
            "请不要发送密码、验证码或完整密钥。"
        )
        ms = (time.monotonic() - t0) * 1000
        self.stats.success += 1
        self.stats.total_ms += ms
        return AgentResponse(
            agent_type=self.agent_type,
            content=content,
            success=True,
            latency_ms=ms,
            escalate=True,
            tools_used=["create_handoff_summary"],
            tool_traces=[{"tool_name": "create_handoff_summary", "input": {}, "output": summary, "success": True, "agent_type": self.agent_type.value}],
        )


class ResponseComposer:
    """多 Agent 汇总节点，统一主次、去重和输出边界。"""

    def __init__(self, client: AsyncAnthropic, model: str, skill_manager: Optional[Any] = None):
        self._client = client
        self._model = model
        self._skill_manager = skill_manager

    async def compose(self, req: Request, responses: List[AgentResponse]) -> str:
        successful = [response for response in responses if response.success and response.content.strip()]
        failures = "\n\n".join(response.content for response in responses if not response.success and response.content.strip())

        def preserve_failures(content: str) -> str:
            return f"{content}\n\n{failures}" if failures else content

        if not successful:
            return failures or "专业诊断暂不可用，请保留问题和脱敏证据以供人工核验。"
        if len(successful) == 1:
            return preserve_failures(successful[0].content)

        evidence = "\n\n".join(
            f"[{response.agent_type.value} Agent 输出]\n{response.content}"
            for response in successful
        )
        prompt = (
            "你是客服 Response Composer，负责把多个专业 Agent 的结果合并成一条最终回复。\n"
            "要求：以主 Agent 的结论为主，按用户问题优先级组织内容；去掉重复和冲突表述；"
            "不能补造配置、授权、调用记录或工单回执；如果结论冲突，明确说明需要核验；"
            "删除候选回答中与当前诉求无关的助手能力声明，保留实际代办请求的未执行说明。"
            "保留有依据的解释、自助步骤与必要限制，不重复未核实字段，不输出工具JSON；"
            "只输出给用户看的中文回复，不要提及 Agent。\n\n"
            f"主 Agent：{successful[0].agent_type.value}\n"
            f"用户问题：{req.message}\n"
            f"候选结果：\n{evidence}"
        )
        if self._skill_manager is not None:
            skill = self._skill_manager.prompt_for(req.message, "general")
            if skill:
                prompt += f"\n\n[通用客服输出边界]\n{skill}"
        from core.response_guidance import RESPONSE_GUIDANCE
        prompt += RESPONSE_GUIDANCE
        try:
            response = await asyncio.wait_for(self._client.messages.create(
                model=self._model,
                max_tokens=_env_int("ORDO_COMPOSER_MAX_TOKENS", 1000),
                temperature=_env_float("ORDO_COMPOSER_TEMPERATURE", 0.1),
                messages=[{"role": "user", "content": prompt}],
                **short_task_options(self._client, self._model),
            ), timeout=max(1, _env_float("ORDO_COMPOSER_TIMEOUT_S", 8)))
            from core.response_guidance import clean_guidance_reply
            content = clean_guidance_reply(completed_text(response), req.message, req.intent.value if req.intent else '')
            if content:
                return preserve_failures(content)
        except Exception as ex:
            logger.warning("Response Composer 失败，使用确定性合并: %s", ex)

        # 汇总节点不可用时保留主次标签，避免丢失某个专业 Agent 的结论。
        return preserve_failures("\n\n".join(
            f"{response.content}" if index == 0 else f"补充说明：\n{response.content}"
            for index, response in enumerate(successful)
        ))

    async def compose_result(self, req, responses):
        content = await self.compose(req, responses)
        successful = [r for r in responses if r.success]
        status = "failed" if not successful else "success" if len(successful) == len(responses) and all(r.status == "success" for r in successful) else "partial"
        return AgentCallResult(content, status=status)



# ── 编排器 ────────────────────────────────────────────────────────────────────

class AgentOrchestrator:
    """
    多 Agent 编排器。

    路由逻辑（三层）：
      1. 意图 → Agent 类型映射
      2. 同类多实例时按 routing_score() 选最优
      3. 专属 Agent 失败时降级到 GeneralAgent
    """

    # 意图 → Agent 类型的静态映射（路由表）
    _INTENT_ROUTING: Dict[IntentCategory, AgentType] = {
        IntentCategory.TECHNICAL_ISSUE: AgentType.TECHNICAL,
        IntentCategory.PRODUCT_OVERVIEW: AgentType.GENERAL,
        IntentCategory.FEATURE_INQUIRY: AgentType.COLLABORATION,
        IntentCategory.ONBOARDING: AgentType.COLLABORATION,
        IntentCategory.ENTERPRISE_MGMT: AgentType.COLLABORATION,
        IntentCategory.PERMISSION_REQUEST: AgentType.COLLABORATION,
        IntentCategory.API_INTEGRATION: AgentType.TECHNICAL,
        IntentCategory.API_ERROR: AgentType.TECHNICAL,
        IntentCategory.API_QUOTA: AgentType.TECHNICAL,
        IntentCategory.TICKET_QUERY: AgentType.GENERAL,
        IntentCategory.HUMAN_HANDOFF: AgentType.ESCALATION,
        # 其余意图 → GENERAL（默认）
    }

    # Monitor 的降权达到该值时，专业 Agent 暂停接收新请求并回退到 GeneralAgent。
    # 使用环境变量可按线上数据调整，默认值与 Monitor 的严重成功率惩罚上限一致。
    MONITOR_FALLBACK_PENALTY = 0.5

    def __init__(
        self,
        api_key:  str,
        base_url: Optional[str] = None,
        model:    str = "claude-3-5-sonnet-20241022",
        skill_manager: Optional[Any] = None,
        rag_tool_manager: Optional[Any] = None,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncAnthropic(**kwargs)

        self._intent_recognizer = IntentRecognizer(api_key=api_key, base_url=base_url, model=model)
        self._skill_manager = skill_manager
        self._composer = ResponseComposer(client, model, skill_manager)
        self._shared_tools: Dict[str, AgentToolSpec] = {}
        self._recent_tool_traces = deque(maxlen=_env_int("ORDO_TOOL_TRACE_MAX", 200))

        # Agent 池：每种类型可有多个实例（水平扩展）
        self._pool: Dict[AgentType, List[BaseAgent]] = {
            AgentType.GENERAL: [self._make_agent(GeneralAgent, client, model, skill_manager)],
            AgentType.COLLABORATION: [self._make_agent(CollaborationAgent, client, model, skill_manager)],
            AgentType.TECHNICAL: [self._make_agent(TechnicalAgent, client, model, skill_manager)],
            AgentType.ESCALATION: [self._make_agent(EscalationAgent, client, model, skill_manager)],
        }
        self.set_shared_tools(build_shared_rag_tools(rag_tool_manager))

    @staticmethod
    def _make_agent(
        agent_cls: type[BaseAgent],
        client: AsyncAnthropic,
        default_model: str,
        skill_manager: Optional[Any],
    ) -> BaseAgent:
        """按角色创建 Agent，并允许用环境变量覆盖该角色的模型。

        可使用更强模型，通用接待可使用更快模型，升级节点本身不需要调用 LLM。
        """
        profile = agent_cls.profile
        env_name = f"ORDO_{agent_cls.agent_type.value.upper()}_MODEL"
        model = os.getenv(env_name, "").strip() or profile.model
        configured_profile = replace(profile, model=model) if model else profile
        return agent_cls(client, default_model, skill_manager, profile=configured_profile)

    def set_skill_manager(self, skill_manager: Optional[Any]) -> None:
        """更新 SkillManager 引用，供运行时重载或测试替换使用。"""
        self._skill_manager = skill_manager
        self._composer._skill_manager = skill_manager
        for agents in self._pool.values():
            for agent in agents:
                agent._skill_manager = skill_manager

    def set_shared_tools(self, tools: Optional[Dict[str, AgentToolSpec]]) -> None:
        """更新所有 Agent 共享的工具白名单。"""
        self._shared_tools = dict(tools or {})
        for agents in self._pool.values():
            for agent in agents:
                agent.set_shared_tools(self._shared_tools)

    async def recognize_intent(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ):
        """对外暴露意图识别，供 API 层先判断是否需要 RAG 等前置能力。"""
        return await self._intent_recognizer.recognize(message, history=history)

    def _record_tool_trace(self, result: OrchestratorResult) -> None:
        trace = {
            "request_id": result.request_id,
            "timestamp": datetime.now().isoformat(),
            "intent": result.intent.value if result.intent else None,
            "primary_agent": result.primary_agent.value if result.primary_agent else None,
            "supporting_agents": [agent.value for agent in result.supporting_agents],
            "tools_used": list(result.tools_used),
            "tool_calls": list(result.tool_traces),
            "model_calls": list(result.model_calls),
            "escalated": result.escalated,
            "latency_ms": round(result.latency_ms, 1),
        }
        from core.redaction import redact
        self._recent_tool_traces.append(redact(trace))

    def get_tool_trace(self, request_id: str) -> Optional[Dict[str, Any]]:
        for trace in reversed(self._recent_tool_traces):
            if trace.get("request_id") == request_id:
                return trace
        return None

    def get_recent_tool_traces(self, limit: int = 20) -> List[Dict[str, Any]]:
        if not self._recent_tool_traces:
            return []
        limit = max(1, min(int(limit or 20), len(self._recent_tool_traces)))
        return list(reversed(list(self._recent_tool_traces)[-limit:]))

    # ── 主入口 ────────────────────────────────────────────────────────────────

    async def run(self, req: Request) -> OrchestratorResult:
        """
        处理一次请求的完整流程：
          意图识别 → 路由选 Agent → 执行 → 检查升级 → 返回结果
        """
        t0 = time.monotonic()

        # 1. 意图识别（如果调用方已识别则跳过）
        if req.intent is None:
            intent_result = await self._intent_recognizer.recognize(req.message, history=req.history)
            req.intent  = intent_result.intent
            req.intent_group = intent_result.intent_group
            req.urgency = intent_result.urgency
            req.intent_confidence = intent_result.confidence
            req.entities = {**intent_result.entities, **req.entities}

        from core.conversation_state import merge_state
        req.conversation_state = merge_state(req.conversation_state, req.entities, req.intent_group, req.message, req.request_id)
        req.entities = req.conversation_state["entities"]
        if req.conversation_state:
            req.context += "\n[用户陈述的会话状态，未经后台核验]\n" + json.dumps(req.conversation_state, ensure_ascii=False)
        if self._needs_clarification(req):
            result = OrchestratorResult(
                request_id=req.request_id,
                response="请补充您希望解决的具体问题：功能与上手、成员与项目权限、API 接入与排错，或已有工单的处理进度。",
                agent_type=AgentType.GENERAL,
                intent=req.intent,
                escalated=False,
                latency_ms=(time.monotonic() - t0) * 1000,
                agent_types=[AgentType.GENERAL],
                primary_agent=AgentType.GENERAL,
                routing_reason="低置信度 OTHER 意图，先澄清用户需求",
                routing_confidence=req.intent_confidence,
            )
            self._record_tool_trace(result)
            return result

        # 只有需要不同业务证据的复合诉求才协作，例如 API403 与网页项目不可见。
        decision = self._route_decision(req)
        if decision.multi_agent:
            return await self.run_parallel(req, decision)

        # 2. 执行主 Agent（含降级）
        response = await self._execute(req, decision.primary_agent)

        # 4. 升级检查
        escalated = False
        if response.escalate or req.urgency == UrgencyLevel.CRITICAL or req.intent in (
            IntentCategory.HUMAN_HANDOFF,
        ):
            escalated = True
            logger.warning(f"请求 {req.request_id} 触发升级: urgency={req.urgency}")
            # 此标志仅表示建议进一步核验，不代表提交或办理。

        result = OrchestratorResult(
            request_id=req.request_id,
            response=response.content,
            status=response.status if response.success else "failed",
            retryable=not response.success and all(t.get("retry_safe", False) for t in req.tool_attempts),
            error_code=response.error_code,
            agent_type=response.agent_type,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[response.agent_type],
            primary_agent=decision.primary_agent,
            supporting_agents=[],
            tools_used=list(response.tools_used),
            tool_traces=list(response.tool_traces),
            model_calls=list(response.model_calls),
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
        )
        self._record_tool_trace(result)
        return result

    async def run_parallel(self, req: Request, decision: RoutingDecision) -> OrchestratorResult:
        """
        并行派发给多个 Agent，合并结果。
        适用于复合问题（如 API 诊断与网页项目权限）。
        """
        t0 = time.monotonic()
        agent_types = decision.agent_types
        tasks = [self._execute(req, at) for at in agent_types]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        valid_responses = [r for r in responses if isinstance(r, AgentResponse)]
        composed = await self._composer.compose_result(req, valid_responses)
        combined = composed.content
        escalated = any(isinstance(r, AgentResponse) and r.escalate for r in responses)
        tools_used = list(dict.fromkeys(
            tool_name
            for response in valid_responses
            for tool_name in response.tools_used
        ))
        tool_traces = [
            trace
            for response in valid_responses
            for trace in response.tool_traces
        ]
        result = OrchestratorResult(
            request_id=req.request_id,
            response=combined,
            status=composed.status if len(valid_responses) == len(responses) else "partial",
            retryable=composed.status == "failed" and all(t.get("retry_safe", False) for t in req.tool_attempts),
            agent_type=decision.primary_agent,
            intent=req.intent,
            escalated=escalated,
            latency_ms=(time.monotonic() - t0) * 1000,
            agent_types=[
                r.agent_type for r in responses
                if isinstance(r, AgentResponse) and r.success
            ] or agent_types,
            primary_agent=decision.primary_agent,
            supporting_agents=decision.supporting_agents,
            tools_used=tools_used,
            tool_traces=tool_traces,
            model_calls=[call for response in valid_responses for call in response.model_calls],
            routing_reason=decision.reason,
            routing_confidence=decision.confidence,
        )
        self._record_tool_trace(result)
        return result

    # ── 路由逻辑 ──────────────────────────────────────────────────────────────

    def _route(self, intent: Optional[IntentCategory], urgency: Optional[UrgencyLevel]) -> AgentType:
        """
        三层路由决策：
          1. 意图映射
          2. 紧急度覆盖（CRITICAL 直接升级）
          3. 默认 GENERAL
        """
        if urgency == UrgencyLevel.CRITICAL:
            return AgentType.ESCALATION

        if intent and intent in self._INTENT_ROUTING:
            target = self._INTENT_ROUTING[intent]
            # 如果目标类型有可用实例则使用，否则降级
            if target in self._pool and self._pool[target]:
                return target

        return AgentType.GENERAL

    def _route_decision(self, req: Request) -> RoutingDecision:
        """
        结构化路由决策。

        先处理紧急/转人工，再用领域分数决定主 Agent 和辅助 Agent。
        这样可以表达“主处理 + 辅助诊断”，避免关键词命中后无主次地拼接。
        """
        if req.urgency == UrgencyLevel.CRITICAL:
            return RoutingDecision(
                primary_agent=AgentType.ESCALATION,
                reason="紧急度为 CRITICAL，触发升级路由",
                confidence=1.0,
            )

        if req.intent == IntentCategory.HUMAN_HANDOFF:
            return RoutingDecision(
                primary_agent=AgentType.ESCALATION,
                reason=f"意图为 {req.intent.value if req.intent else 'unknown'}，触发升级路由",
                confidence=max(req.intent_confidence, 0.8),
            )

        scores = self._domain_scores(req)
        target = self._INTENT_ROUTING.get(req.intent, AgentType.GENERAL)
        if is_general_overview(req.message):
            target = AgentType.GENERAL
        elif target == AgentType.GENERAL and req.intent == IntentCategory.OTHER:
            candidates = self._domain_targets(req)
            # 明确组合沿用既有主辅优先级，不靠字典插入顺序打破同分。
            target = candidates[0] if candidates else max(scores, key=scores.get)
        degraded = self._monitor_degraded(target)
        available = bool(self._pool.get(target)) and not degraded
        primary = target if available else AgentType.GENERAL
        # 主领域不可用时只澄清和收集信息，不将其他领域视为具备主领域能力。
        supporting = [
            agent_type for agent_type in self._collaboration_targets(req)
            if available and agent_type != primary
            and self._pool.get(agent_type) and not self._monitor_degraded(agent_type)
        ][:1]
        reason = self._routing_reason(req, scores, primary, supporting)
        if degraded:
            reason += f", monitor_fallback=[{target.value}]"
        elif not available:
            reason += f", unavailable=[{target.value}]"
        return RoutingDecision(
            primary_agent=primary,
            supporting_agents=supporting,
            reason=reason,
            confidence=round(min(scores.get(target, 0.1), 1.0), 3),
        )

    def _domain_scores(self, req: Request) -> Dict[AgentType, float]:
        """领域分用于责任角色选择，不作为授权或答案正确率。"""
        scores = {
            AgentType.GENERAL: 0.1,
            AgentType.COLLABORATION: 0.0,
            AgentType.TECHNICAL: 0.0,
        }
        target = self._INTENT_ROUTING.get(req.intent, AgentType.GENERAL)
        if is_general_overview(req.message):
            target = AgentType.GENERAL
        collaboration = self._domain_targets(req)
        # OTHER 是分类不确定，并非支持 General 的领域证据。
        # 已有明确跨域诉求时，不能让兜底角色压过实际业务角色。
        if target in scores and not (req.intent == IntentCategory.OTHER and collaboration):
            scores[target] += 0.75
        for agent_type in collaboration:
            scores[agent_type] += 0.2
        return {agent_type: round(score, 3) for agent_type, score in scores.items()}

    @staticmethod
    def _routing_reason(
        req: Request,
        scores: Dict[AgentType, float],
        primary_agent: AgentType,
        supporting_agents: List[AgentType],
    ) -> str:
        score_text = ", ".join(
            f"{agent_type.value}={score:.2f}"
            for agent_type, score in sorted(scores.items(), key=lambda item: item[1], reverse=True)
        )
        support_text = ", ".join(agent.value for agent in supporting_agents) or "none"
        intent = req.intent.value if req.intent else "unknown"
        return (
            f"intent={intent}, group={req.intent_group or 'unknown'}, "
            f"primary={primary_agent.value}, supporting={support_text}, scores=[{score_text}]"
        )

    def _domain_targets(self, req: Request) -> List[AgentType]:
        """具体协作线索属于同一角色；只有独立API诉求才形成跨角色协作。"""
        if is_general_overview(req.message):
            return []
        msg = req.message.lower()
        api = req.intent in (IntentCategory.API_INTEGRATION, IntentCategory.API_ERROR, IntentCategory.API_QUOTA) or any(
            word in msg for word in ("api", "接口", "/sandbox/")
        )
        fault = bool(platform_fault_topic(msg)) or req.intent == IntentCategory.TECHNICAL_ISSUE
        # API 自身的 scope/403 并不单独触发协作支持；网页资源问题才是额外诉求。
        web_access = any(word in msg for word in ("网页", "页面", "项目链接", "文档链接", "团队里", "工作空间里")) and any(
            word in msg for word in ("看不到", "不可见", "无法访问", "打不开")
        )
        permission = web_access or any(word in msg for word in ("访问申请", "申请项目权限", "我的角色", "成员角色"))
        permission = permission or any(
            any(word in clause for word in ("guest", "member", "admin", "权限"))
            and any(word in clause for word in ("角色", "权限", "编辑", "批注", "访问", "创建"))
            and not any(marker in clause for marker in ("api", "接口", "/sandbox/"))
            for clause in re.split(r"[。！？!?；;\n]", msg)
        )
        if fault:
            # “我有编辑权限但保存失败”是故障前提，不是另一项权限咨询。
            permission = web_access or any(word in msg for word in (
                "访问申请", "申请项目权限", "谁审批", "谁能审批", "怎么申请", "如何申请", "如何调整权限", "怎么调整权限",
                "guest能否编辑", "guest可以编辑吗", "guest能不能编辑", "角色有什么区别",
            ))
        onboarding = any(word in msg for word in ("如何创建", "怎么创建", "邀请步骤", "分享步骤", "上手步骤"))
        # 自然表达的页面操作诉求；接口自身的创建/邀请参数不算额外上手诉求。
        onboarding = onboarding or any(
            any(goal in clause for goal in ("创建项目", "创建一个项目", "新建项目", "邀请成员", "邀请同事", "分享项目"))
            and any(cue in clause for cue in ("如何", "怎么", "步骤", "想", "需要"))
            and not any(marker in clause for marker in ("api", "接口", "/sandbox/"))
            for clause in re.split(r"[。！？!?；;\n]", msg)
        )
        if (api or fault) and (permission or onboarding):
            return [AgentType.TECHNICAL, AgentType.COLLABORATION]
        if fault:
            return [AgentType.TECHNICAL]
        if permission or onboarding:
            return [AgentType.COLLABORATION]
        return []

    def _collaboration_targets(self, req: Request) -> List[AgentType]:
        targets = self._domain_targets(req)
        return targets if len(targets) > 1 else []

    def _needs_clarification(self, req: Request) -> bool:
        """低置信度且无明确意图时，先追问，避免误路由。"""
        if req.urgency == UrgencyLevel.CRITICAL:
            return False
        if req.intent != IntentCategory.OTHER:
            return False
        if is_general_overview(req.message) or self._domain_targets(req):
            return False
        text = (req.message or "").strip()
        if len(text) <= 2:
            return False
        return req.intent_confidence < 0.5

    def _best_agent(self, agent_type: AgentType) -> Optional[BaseAgent]:
        """
        性能路由：从同类 Agent 中选 routing_score() 最高的。
        这是"基于在线表现动态调整路由"的核心。
        """
        agents = self._pool.get(agent_type, [])
        if not agents:
            return None
        eligible = [a for a in agents if agent_type not in (AgentType.COLLABORATION, AgentType.TECHNICAL) or a.stats.available()]
        return max(eligible, key=lambda a: a.stats.routing_score()) if eligible else None

    def _monitor_degraded(self, agent_type: AgentType) -> bool:
        """专业 Agent 降权过高时由路由层回退，确保单实例部署也能生效。"""
        if agent_type in (AgentType.GENERAL, AgentType.ESCALATION):
            return False
        agents = self._pool.get(agent_type, [])
        if not agents:
            return False
        return all(not agent.stats.available() for agent in agents)

    async def _execute(self, req: Request, agent_type: AgentType) -> AgentResponse:
        """执行同领域 Agent；失败时明确能力受限，保留失败前的 trace。"""
        agent = self._best_agent(agent_type)
        if agent is None:
            agent = self._best_agent(AgentType.GENERAL)
        if agent is None:
            return AgentResponse(
                agent_type=AgentType.GENERAL,
                content="服务暂时不可用，请稍后重试。",
                success=False,
            )

        response = await agent.handle(req)

        # 不通过其他领域模型重新猜测失败诊断。
        if not response.success and agent_type not in (AgentType.GENERAL, AgentType.ESCALATION):
            logger.warning(f"{agent_type.value} 失败，返回确定性能力限制说明")
            response.content = f"{agent_type.value} 专业诊断暂不可用。请保留问题、脱敏证据和已尝试步骤以供人工核验；当前尚未提交工单。"
            response.escalate = True

        return response

    # ── 统计（供 Monitor 读取）────────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        result = {}
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                recent_n, recent_sr, recent_ms = agent.stats.recent()
                result[key] = {
                    "recent_total": recent_n, "recent_success_rate": recent_sr, "recent_avg_ms": recent_ms,
                    "recovery_state": "probing" if agent.stats.probe_running else "cooldown" if agent.stats.retry_at else "healthy",
                    "inflight": agent.stats.inflight, "completed": agent.stats.completed,
                    "total":        agent.stats.total,
                    "success_rate": round(agent.stats.success_rate, 3),
                    "avg_ms":       round(agent.stats.avg_ms, 1),
                    "monitor_penalty": round(agent.stats.monitor_penalty, 3),
                    "routing_score": round(agent.stats.routing_score(), 3),
                    "role": agent.profile.role,
                    "workflow": list(agent.profile.workflow),
                    "tool_scope": list(agent.get_tools()),
                    "available_tools": list(agent.get_tools()),
                    "model": agent._model,
                }
        return result

    def update_routing_penalties(self, penalties: Dict[str, float]) -> None:
        """
        接收 Monitor 的在线表现反馈，动态调整路由惩罚项。

        penalties 的 key 使用 get_stats() 中的 agent key，例如 api_0。
        """
        for agent_type, agents in self._pool.items():
            for i, agent in enumerate(agents):
                key = f"{agent_type.value}_{i}"
                penalty = penalties.get(key, 0.0)
                agent.stats.set_penalty(min(max(penalty, 0.0), 0.9))
