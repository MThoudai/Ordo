"""Agent 工具定义与实现。

所有 Agent 工具集中在这里，编排器只负责：
  1. 根据 Agent 类型暴露工具白名单
  2. 执行 LLM 返回的 tool_use
  3. 将工具结果回传给 LLM

当前工具仅提供字段整理、API排查建议、知识检索和沟通草稿。
不连接真实业务系统，不执行查询、沙箱、申请或工单操作，不伪造业务状态。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple, Any, Awaitable, Callable, Dict, List, Optional, TYPE_CHECKING, Union

if TYPE_CHECKING:
    from agents.agent_orchestrator import Request


AgentToolHandler = Callable[["Request", Dict[str, Any]], Union[Any, Awaitable[Any]]]


@dataclass(frozen=True)
class AgentToolSpec:
    """Agent 可见工具的定义和执行函数。"""

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: AgentToolHandler
    allowed_agents: Optional[Tuple[str, ...]] = None
    retry_safe: bool = True


def make_tool(
    name: str,
    description: str,
    properties: Dict[str, Any],
    handler: AgentToolHandler,
    required: Optional[List[str]] = None,
) -> AgentToolSpec:
    """创建带 JSON Schema 的 Agent 工具。"""
    return AgentToolSpec(
        name=name,
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": required or [],
            "additionalProperties": False,
        },
        handler=handler,
    )


def inspect_request_context(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """通用客服工具：返回脱敏后的当前请求快照。"""
    return {
        "intent": req.intent.value if req.intent else None,
        "intent_group": req.intent_group,
        "urgency": req.urgency.name if req.urgency else None,
        "intent_confidence": round(req.intent_confidence, 4),
        "entities": req.entities or {},
        "context_available": bool(req.context),
        "requested_focus": str(args.get("focus", "general"))[:40],
    }


def suggest_required_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """通用客服工具：按业务类型计算下一轮只需询问的字段。"""
    intent = req.intent.value if req.intent else "other"
    fields: List[str] = []
    if intent == "ticket_query":
        # No ticket lookup exists: asking for an ID would imply an executable next step.
        fields = []
    elif intent in {"enterprise_mgmt", "permission_request"}:
        fields = []
    elif intent in {"api_error", "api_quota", "api_integration"}:
        fields = [label for key, label in (("api_path", "固定接口路径"), ("error_code", "脱敏报错或响应（如需排错）")) if not req.entities.get(key)]
    elif intent == "other":
        fields = ["希望解决的具体问题"]
    return {
        "intent": intent,
        "required_fields": fields,
        "known_entities": req.entities or {},
    }


def lookup_error_code(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """技术工具：解释常见错误码的排查方向，不声称读取了服务端日志。"""
    code = str(args.get("error_code", "")).upper().strip()
    mapping = {
        "400": ("请求参数不符合规范", ["根据固定接口规范检查路径、查询参数和请求结构"]),
        "401": ("凭据认证失败", ["检查凭据是否有效、过期或撤销", "不发送完整密钥"]),
        "403": ("授权条件未满足", ["分别检查凭据 project:read scope 和调用用户项目访问权限", "两者可能同时缺失，不能仅凭403断定原因"]),
        "429": ("短时限流或累计额度不足", ["查看脱敏业务错误码，区分 RATE_LIMITED 和 QUOTA_EXCEEDED", "按固定窗口或重置周期处理，不无限重试"]),
        "404": ("资源或路径不存在", ["确认接口路径和环境", "确认资源标识是否正确"]),
        "500": ("服务端处理异常", ["记录 request_id 和发生时间", "检查依赖服务、参数格式和服务端日志"]),
    }
    meaning, steps = mapping.get(
        code,
        ("暂未识别的错误码", ["补充完整错误信息、发生时间和运行环境"]),
    )
    request_kind = args.get("request_kind") or ("api" if any(word in req.message.lower() for word in ("api", "接口", "/sandbox/")) else "unknown")
    if code == "401" and request_kind != "api":
        meaning = "认证条件未满足，需先区分失败请求来源"
        steps = (["结合网页登录提示核对会话状态，必要时由用户重新登录验证"] if request_kind == "web_session" else
                 ["先确认失败请求是网页会话认证、固定产品API还是其他资源", "网页会话结合登录提示分析；API核对凭据与认证头，不能仅凭401断定网页登录过期"])
    elif code == "403" and request_kind != "api":
        meaning, steps = "资源访问条件可能未满足", ["结合页面提示核对当前账号、空间和项目访问条件，不将网页403直接解释为API scope缺失"]
    return {
        "error_code": code,
        "request_kind": request_kind,
        "meaning": meaning,
        "next_steps": steps,
        "server_log_checked": False,
        "status": "success",
        "evidence": [],
        "verified_cause": False,
    }


def build_diagnostic_plan(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """技术工具：生成低风险排障顺序。"""
    environment = str(args.get("environment", "unknown"))[:80]
    reproduced = bool(args.get("reproduced", False))
    from core.support_boundaries import platform_fault_topic
    issue_type = args.get("issue_type") or platform_fault_topic(req.message) or "api"
    steps = [
        "复现并记录完整错误信息",
        "根据固定产品接口规范核对参数",
        "分别检查凭据有效性、scope、调用用户项目权限和额度",
    ]
    platform_steps = {
        "document_sync": ["先备份未保存内容并核对完整性，必要时用脱敏截图保留格式", "核对同一空间、项目和实时文档，按用户提供的权限及保存提示分析", "版本历史仅作辅助；如需刷新，先比对当前正文及他人修改，仅补缺失内容，不整篇覆盖"],
        "page_loading": ["区分明确权限提示与白屏/持续加载，先保护未保存内容", "在同账号、同空间、同页面下对比无痕窗口，逐项检查环境差异", "记录脱敏失败请求，在组织允许的网络下对比，不关闭受管代理或安全检查"],
        "file_transfer": ["查看实际提示与项目权限，记录文件类型及体积，不编造固定限额", "同页面用普通小文件对比，结合提示分析差异，不断定单一根因", "仅对可信平台域名按浏览器实际拦截原因处理当次下载，不长期无条件放行"],
        "notifications": ["先核对工作台收到的批注提醒，桌面弹窗不是本站通知依据", "核对已发布批注及选中的@对象，再核对接收账号、空间和项目访问条件", "均符合仍未收到时保留脱敏证据，不反复发送相同批注，不推荐桌面/短信/邮件推送"],
    }
    if issue_type in platform_steps:
        steps = platform_steps[issue_type]
    elif reproduced:
        steps.append("按建议调整后，由用户用原调用身份和原凭据自行复测；助手不执行实际请求")
    return {
        "environment": environment,
        "issue_type": issue_type,
        "reproduced": reproduced,
        "diagnostic_steps": steps,
        "sandbox_executed": False,
        "backend_checked": False,
        "plan_only": True,
    }


def create_handoff_summary(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """升级工具：生成可交给人工客服的结构化摘要。"""
    return {
        "request_id": req.request_id,
        "reason": str(args.get("reason", "需要整理后续沟通材料"))[:120],
        "intent": req.intent.value if req.intent else "unknown",
        "urgency": req.urgency.name if req.urgency else "UNKNOWN",
        "entities": req.entities or {},
        "sensitive_data_required": False,
        "status": "draft",
        "ticket_submitted": False,
        "ticket_id": None,
        "missing_fields": ["影响范围", "脱敏证据", "已尝试步骤", "未解决事项"],
    }


def build_shared_rag_tools(tool_manager: Any) -> Dict[str, AgentToolSpec]:
    """构建四种问答角色可共享的 RAG 工具。"""

    async def search_knowledge_base(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query") or req.message or "").strip()
        top_k = int(args.get("top_k", 5) or 5)
        if not query:
            return {"success": False, "error": "query 不能为空", "results": []}
        if tool_manager is None:
            return {"success": False, "error": "RAG 工具未初始化", "results": []}

        result = await tool_manager.search_with_rewrite(
            "knowledge_search",
            query,
            top_k=top_k,
        )
        if not getattr(result, "success", False):
            return {
                "success": False,
                "query": query,
                "error": getattr(result, "error", "知识库检索失败"),
                "results": [],
                "reranked": False,
                "diagnostics": getattr(result, "diagnostics", {}),
            }

        return {
            "success": True,
            "query": query,
            "top_k": top_k,
            "results": result.data,
            "reranked": bool(getattr(result, "reranked", False)),
            "diagnostics": getattr(result, "diagnostics", {}),
        }

    return {
        "search_knowledge_base": make_tool(
            "search_knowledge_base",
            "检索产品资料并返回文档片段；不查询当前成员权限、调用记录或工单状态。",
            {
                "query": {"type": "string", "description": "用户问题或检索关键词"},
                "top_k": {"type": "integer", "description": "返回结果条数"},
            },
            search_knowledge_base,
            required=["query"],
        )
    }


def general_tools() -> Dict[str, AgentToolSpec]:
    return {
        "inspect_request_context": make_tool(
            "inspect_request_context",
            "查看当前请求的意图、紧急度、实体和上下文可用性；不查询外部业务系统。",
            {"focus": {"type": "string", "description": "希望关注的业务方向"}},
            inspect_request_context,
        ),
        "suggest_required_fields": make_tool(
            "suggest_required_fields",
            "根据当前意图建议下一轮只需向用户补充的字段。",
            {},
            suggest_required_fields,
        ),
    }


def inspect_collaboration_fields(req: Request, args: Dict[str, Any]) -> Dict[str, Any]:
    """整理同一协作流程的功能及权限线索，不执行查询、邀请或申请。"""
    return {
        "workspace_id": req.entities.get("workspace_id", []),
        "project_id": req.entities.get("project_id", []),
        "state_verified": False,
        "requested_permission": "read",
        "entity_source": "user_reported_unverified",
        "role_verified": False,
        "access_verified": False,
        "request_submitted": False,
        "missing_fields": [],
        "next_step": "依据功能及权限资料连续说明操作、适用条件和用户自查方法；read申请审批方为本空间admin或本项目负责人",
    }


def collaboration_tools() -> Dict[str, AgentToolSpec]:
    return {"inspect_collaboration_fields": make_tool(
        "inspect_collaboration_fields", "整理用户提供的功能、项目及权限线索，仅用于解释指导，不查询配置或执行申请。", {}, inspect_collaboration_fields,
    )}


def technical_tools() -> Dict[str, AgentToolSpec]:
    return {
        "lookup_error_code": make_tool(
            "lookup_error_code",
            "解释常见 HTTP 错误码的可能含义和低风险排查方向；不会读取服务端日志。",
            {"error_code": {"type": "string", "description": "例如 400、401、403、429"},
             "request_kind": {"type": "string", "enum": ["api", "web_session", "unknown"], "description": "失败请求属于固定API、网页会话还是尚未确认"}},
            lookup_error_code,
            required=["error_code"],
        ),
        "build_diagnostic_plan": make_tool(
            "build_diagnostic_plan",
            "根据运行环境和是否可复现生成排障顺序，不执行修改配置等操作。",
            {
                "environment": {"type": "string", "description": "页面、浏览器或固定产品接口环境"},
                "reproduced": {"type": "boolean", "description": "问题是否可以稳定复现"},
                "issue_type": {"type": "string", "enum": ["api", "page_loading", "file_transfer", "document_sync", "notifications"], "description": "当前诊断场景，未提供时按请求线索选择"},
            },
            build_diagnostic_plan,
            required=["environment", "reproduced"],
        ),
    }


def escalation_tools() -> Dict[str, AgentToolSpec]:
    return {
        "create_handoff_summary": make_tool(
            "create_handoff_summary",
            "生成供用户后续沟通的草稿，不联系人工、不提交申请或创建工单。",
            {"reason": {"type": "string", "description": "需要升级的原因"}},
            create_handoff_summary,
        ),
    }
