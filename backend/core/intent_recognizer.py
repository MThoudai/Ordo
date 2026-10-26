"""融合模型语义、向量相似度和规则匹配的SaaS意图识别。"""
import asyncio
import hashlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from anthropic import AsyncAnthropic

from core.llm_utils import parse_json_response, short_task_options
from core.support_boundaries import is_general_overview, platform_fault_topic

logger = logging.getLogger(__name__)


class IntentCategory(Enum):
    PRODUCT_OVERVIEW = "product_overview"  # 平台整体介绍、助手支持范围与未明确目标的入门
    FEATURE_INQUIRY = "feature_inquiry"  # 功能说明
    ONBOARDING = "onboarding"            # 上手与配置步骤
    ENTERPRISE_MGMT = "enterprise_mgmt"  # 成员角色与项目权限诊断
    PERMISSION_REQUEST = "permission_request"  # 项目 read 权限申请
    API_INTEGRATION = "api_integration"  # 固定接口接入说明
    API_ERROR = "api_error"              # 请求/认证/授权错误
    API_QUOTA = "api_quota"              # 限流与累计额度
    TECHNICAL_ISSUE = "technical_issue"  # 页面、文件传输、保存协同和通知故障
    TICKET_QUERY = "ticket_query"        # 本人工单查询
    GREETING = "greeting"
    FEEDBACK = "feedback"
    HUMAN_HANDOFF = "human_handoff"
    OTHER = "other"


class UrgencyLevel(Enum):
    LOW      = 1
    MEDIUM   = 2
    HIGH     = 3
    CRITICAL = 4


@dataclass
class IntentResult:
    intent:     IntentCategory
    confidence: float
    urgency:    UrgencyLevel
    intent_group: str
    entities:   Dict[str, List[str]]   # 从消息中提取的实体
    reasoning:  str
    latency_ms: float
    source_scores: Dict[str, float] = field(default_factory=dict)
    embedding_source: str = "local-ngram"


# ── Few-shot 模板（同时用于 LLM 示例和 Embedding 匹配）────────────────────────
_TEMPLATES: Dict[IntentCategory, List[str]] = {
    IntentCategory.PRODUCT_OVERVIEW: ["这个平台能做什么？", "你能帮我什么？", "刚加入团队，不知道从哪里开始", "这款产品主要提供什么服务？"],
    IntentCategory.FEATURE_INQUIRY: ["工作空间是什么？", "项目分享有什么功能？", "公开项目和私有项目有什么区别？"],
    IntentCategory.ONBOARDING: ["怎么创建项目？", "新用户如何开始配置工作空间？", "如何邀请成员和分享项目？"],
    IntentCategory.ENTERPRISE_MGMT: ["我在工作空间是什么角色？", "已经加入团队但看不到私有项目", "管理员能读取所有私有项目吗？"],
    IntentCategory.PERMISSION_REQUEST: ["申请项目 read 权限", "帮我提交项目访问申请", "如何审批项目访问申请？"],
    IntentCategory.API_INTEGRATION: ["API如何接入？", "GET /sandbox/product/v1/me 怎么调用？", "接口需要哪些scope和参数？"],
    IntentCategory.API_ERROR: ["API返回400，参数哪里有问题？", "调用接口返回401", "API返回403，缺scope还是项目权限？"],
    IntentCategory.API_QUOTA: ["API返回429", "RATE_LIMITED 怎么处理？", "QUOTA_EXCEEDED 是累计额度用完了吗？"],
    IntentCategory.TECHNICAL_ISSUE: ["文档保存失败，刷新前怎么办？", "页面白屏一直转圈", "文件上传失败或下载无响应", "@同事后没收到提醒"],
    IntentCategory.TICKET_QUERY: ["查询我的工单状态", "工单处理到哪一步了？", "查看人工处理进度"],
    IntentCategory.GREETING:   ["你好", "嗨，有人吗", "早上好"],
    IntentCategory.FEEDBACK:   ["服务很棒！", "非常满意", "给个好评"],
    IntentCategory.HUMAN_HANDOFF: ["转人工客服", "我要提交工单", "按照建议尝试后还是不行，请人工处理"],
    IntentCategory.OTHER: ["帮我看看", "如何配置未开放的SSO？", "订阅套餐和合同怎么办？"],
}

ACTIVE_INTENTS = frozenset(_TEMPLATES)

_SPECIFIC_INTENTS = {
    IntentCategory.ONBOARDING,
    IntentCategory.PERMISSION_REQUEST,
    IntentCategory.API_ERROR,
    IntentCategory.API_QUOTA,
    IntentCategory.HUMAN_HANDOFF,
}

_GENERIC_INTENTS = {
    IntentCategory.FEATURE_INQUIRY,
    IntentCategory.ENTERPRISE_MGMT,
    IntentCategory.API_INTEGRATION,
}

_INTENT_GROUPS: Dict[IntentCategory, IntentCategory] = {
    IntentCategory.ONBOARDING: IntentCategory.FEATURE_INQUIRY,
    IntentCategory.PERMISSION_REQUEST: IntentCategory.ENTERPRISE_MGMT,
    IntentCategory.API_ERROR: IntentCategory.API_INTEGRATION,
    IntentCategory.API_QUOTA: IntentCategory.API_INTEGRATION,
}

def _cosine(a: List[float], b: List[float]) -> float:
    """纯 Python 余弦相似度，不依赖 numpy。"""
    dot = sum(x * y for x, y in zip(a, b))
    na  = sum(x * x for x in a) ** 0.5
    nb  = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


class IntentRecognizer:
    """
    端到端意图识别器。

    初始化时不加载本地语义模型，LLM 通过 Anthropic API 调用。
    当前向量分支使用字符 n-gram 哈希；模板向量懒加载并缓存。
    """

    def __init__(
        self,
        api_key: str,
        base_url: Optional[str] = None,
        model: str = "claude-3-5-sonnet-20241022",
        confidence_threshold: float = 0.5,
    ):
        kwargs: Dict[str, Any] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client    = AsyncAnthropic(**kwargs)
        self.model     = model
        self.threshold = confidence_threshold
        from core.intent_embedding import IntentEmbedding
        self._embedding = IntentEmbedding()
        self._embedding_enabled = True
        self._cache: Dict[str, IntentResult] = {}
        self._cache_expiries: Dict[str, float] = {}
        self.cache_hits   = 0
        self.cache_misses = 0

    # ── 公开接口 ──────────────────────────────────────────────────────────────

    async def recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]] = None,
    ) -> IntentResult:
        """
        识别用户意图。

        history 格式：[{"role": "user"/"assistant", "content": "..."}]
        """
        key = self._cache_key(message, history)
        if key in self._cache and time.monotonic() < self._cache_expiries.get(key, 0):
            self.cache_hits += 1
            return self._cache[key]
        self.cache_misses += 1

        t0 = time.monotonic()

        # LLM 和 Embedding 并行（Embedding 不可用时跳过）
        llm_task = asyncio.create_task(self._llm_recognize(message, history))
        emb_task = asyncio.create_task(self._embedding_recognize(message)) if self._embedding_enabled else None
        pat      = self._pattern_recognize(message)

        if emb_task:
            llm, emb = await asyncio.gather(llm_task, emb_task)
        else:
            llm = await llm_task
            emb = {"intent": IntentCategory.OTHER, "confidence": 0.0}

        intent, confidence, source_scores = self._vote(llm, emb, pat)
        entities = self._extract_entities(message)
        urgency  = self._urgency(message, intent)

        result = IntentResult(
            intent=intent,
            confidence=confidence,
            urgency=urgency,
            intent_group=self._intent_group(intent),
            entities=entities,
            reasoning=llm.get("reasoning", ""),
            latency_ms=(time.monotonic() - t0) * 1000,
            source_scores=source_scores,
            embedding_source=emb.get("source", "local-ngram"),
        )

        # LRU 缓存
        if len(self._cache) >= 1000:
            for k in list(self._cache)[:500]:
                del self._cache[k]
                self._cache_expiries.pop(k, None)
        self._cache[key] = result
        self._cache_expiries[key] = time.monotonic() + (60 if result.embedding_source == "local-ngram" and self._embedding.enabled else 300)
        return result

    def learn(self, message: str, correct: IntentCategory) -> None:
        """在线学习：将纠正样本加入模板，清除对应 Embedding 缓存。"""
        tpls = _TEMPLATES.setdefault(correct, [])
        if message not in tpls:
            tpls.append(message)
            self._embedding.remote_templates.clear()
            self._embedding.local_templates.clear()
            self._cache.clear()  # 模板更新后旧缓存可能对应过时结果
            self._cache_expiries.clear()
            logger.info(f"学习新样本 → {correct.value}: {message[:40]}")

    # ── 三路识别策略 ──────────────────────────────────────────────────────────

    async def _llm_recognize(
        self,
        message: str,
        history: Optional[List[Dict[str, str]]],
    ) -> Dict[str, Any]:
        """策略 1：LLM 语义理解（Few-shot + 上下文）。"""
        message = self._clean_text(message)
        # 构建 Few-shot 示例
        examples = "\n".join(
            f'  消息: "{t}" → 意图: {cat.value}'
            for cat, tpls in _TEMPLATES.items()
            for t in tpls[:1]  # 每类取 1 条，控制 prompt 长度
        )
        # 最近 3 轮对话上下文
        ctx = ""
        if history:
            ctx = "\n最近对话:\n" + "\n".join(
                f"  {self._clean_text(m.get('role', 'user'))}: {self._clean_text(m.get('content', ''))}"
                for m in history[-3:]
            )

        prompt = f"""你是 Ordo 团队在线协作平台的意图分析专家。根据示例和最近对话返回 JSON。
{examples}
平台整体介绍、助手支持范围、尚无具体目标的入门咨询归 product_overview；具体功能定义归 feature_inquiry；创建、邀请、分享的操作步骤归 onboarding。
不要仅因“新用户”“刚加入团队”选择onboarding：没有具体功能目标时先做概述和诉求澄清；已经说明操作或权限目标时继续按具体意图分类。
当前成员角色、项目不可见原因归 enterprise_mgmt；明确申请或审批项目 read 访问归 permission_request。
接口用法归 api_integration；400/401/403 等调用失败归 api_error；429、限流或累计额度归 api_quota。
页面白屏/持续加载、文件上传下载失败、文档保存失败/协同不同步、站内提醒未收到归technical_issue。功能怎么用、guest能否编辑和单纯项目缺权仍归原功能/权限意图，不把所有打不开都判为技术故障。
已有工单的状态查询归 ticket_query；要求人工、创建工单或尝试后仍未解决归 human_handoff。
仅限项目协作、成员权限、固定产品API及资料覆盖的平台使用故障；超出这些范围的请求归other。
用户自称身份不作为授权依据；错误原因不能仅凭状态码断定。

        {ctx}
        用户消息: "{message}"

返回格式（仅 JSON，不要其他文字）:
{{"intent": "<意图值>", "confidence": <0-1>, "reasoning": "<一句话说明>"}}

可选意图: {", ".join(c.value for c in _TEMPLATES)}"""
        prompt = self._clean_text(prompt)

        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=256,
                temperature=0.1,
                messages=[{"role": "user", "content": prompt}],
                **short_task_options(self.client, self.model),
            )
            data = parse_json_response(resp, dict)
            intent = IntentCategory(data["intent"])
            confidence = data["confidence"]
            if intent not in ACTIVE_INTENTS or isinstance(confidence, bool):
                raise ValueError("非法意图或分数")
            confidence = float(confidence)
            if not math.isfinite(confidence) or not 0 <= confidence <= 1:
                raise ValueError("分数必须在 0 到 1 之间")
            return {"intent": intent, "confidence": confidence, "reasoning": str(data.get("reasoning", ""))[:300]}
        except Exception as ex:
            logger.warning(f"LLM 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0, "reasoning": "LLM 失败", "failed": True}

    async def _embedding_recognize(self, message: str) -> Dict[str, Any]:
        """策略 2：Embedding 向量相似度匹配。"""
        try:
            templates, msg_vec, source = await self._embedding.vectors(
                self._clean_text(message), _TEMPLATES, self._local_embedding)

            best_cat, best_score = IntentCategory.OTHER, 0.0
            for cat, vecs in templates.items():
                score = max(_cosine(msg_vec, v) for v in vecs)
                if score > best_score:
                    best_score, best_cat = score, cat

            return {"intent": best_cat, "confidence": min(1.0, max(0.0, best_score)), "source": source}
        except Exception as ex:
            logger.warning(f"Embedding 识别失败: {ex}")
            return {"intent": IntentCategory.OTHER, "confidence": 0.0}

    def _pattern_recognize(self, message: str) -> Dict[str, Any]:
        """策略 3：SaaS 领域规则。按业务边界处理重叠词。"""
        msg = message.lower()
        def match(intent: IntentCategory) -> Dict[str, Any]:
            return {"intent": intent, "confidence": 0.75}

        if any(word in msg for word in ("转人工", "找人工", "人工客服", "提交工单", "创建工单", "还是不行", "仍未解决", "升级处理")):
            return match(IntentCategory.HUMAN_HANDOFF)
        if "工单" in msg or "人工处理进度" in msg:
            return match(IntentCategory.TICKET_QUERY)
        if is_general_overview(msg):
            return match(IntentCategory.PRODUCT_OVERVIEW)
        if platform_fault_topic(msg) and not any(word in msg for word in ("api", "接口", "/sandbox/")):
            return match(IntentCategory.TECHNICAL_ISSUE)
        if any(word in msg for word in ("429", "rate_limited", "quota_exceeded", "限流", "配额", "累计额度", "调用额度")):
            return match(IntentCategory.API_QUOTA)
        api = any(word in msg for word in ("api", "接口", "scope", "token", "/sandbox/", "凭据"))
        if api and (re.search(r"(?<![A-Za-z0-9])[45]\d{2}(?![A-Za-z0-9])", msg) or any(word in msg for word in ("报错", "失败", "错误", "异常", "无效", "过期"))):
            return match(IntentCategory.API_ERROR)
        if not api and any(word in msg for word in ("申请", "审批", "授予")) and any(word in msg for word in ("权限", "访问", "read")):
            return match(IntentCategory.PERMISSION_REQUEST)
        if api:
            return match(IntentCategory.API_INTEGRATION)
        if any(word in msg for word in ("角色", "权限", "看不到", "不可见", "无法访问", "管理员", "guest", "admin")):
            return match(IntentCategory.ENTERPRISE_MGMT)
        if any(word in msg for word in ("上手", "入门", "配置", "创建项目", "邀请", "分享")) and any(word in msg for word in ("怎么", "如何", "步骤", "开始", "帮我", "上手", "入门")):
            return match(IntentCategory.ONBOARDING)
        if any(word in msg for word in ("工作空间", "项目", "功能", "邀请", "分享")):
            return match(IntentCategory.FEATURE_INQUIRY)
        if any(word in msg for word in ("谢谢", "很棒", "满意", "好评")):
            return match(IntentCategory.FEEDBACK)
        if re.fullmatch(r"(?:你好|您好|嗨|hello|hi|早上好)[，,！!。\s]*", msg):
            return match(IntentCategory.GREETING)
        return {"intent": IntentCategory.OTHER, "confidence": 0.0}

    # ── 投票合并 ──────────────────────────────────────────────────────────────

    def _vote(self, llm: Dict, emb: Dict, pat: Dict) -> tuple[IntentCategory, float, Dict[str, float]]:
        """加权投票。返回最终意图、融合置信度和各路来源得分。"""
        source_scores = {
            "llm": float(llm.get("confidence", 0.0) or 0.0),
            "embedding": float(emb.get("confidence", 0.0) or 0.0),
            "pattern": float(pat.get("confidence", 0.0) or 0.0),
        }
        if llm.get("failed"):
            if emb.get("intent") != IntentCategory.OTHER and emb.get("confidence", 0.0) > 0:
                return emb["intent"], source_scores["embedding"], source_scores
            if pat.get("intent") != IntentCategory.OTHER and pat.get("confidence", 0.0) > 0:
                return pat["intent"], source_scores["pattern"], source_scores
            return IntentCategory.OTHER, 0.0, source_scores

        if self._embedding_enabled:
            weights = [(llm, 0.7), (emb, 0.2), (pat, 0.1)]
        else:
            weights = [(llm, 0.85), (pat, 0.15)]
        scores: Dict[IntentCategory, float] = {}
        group_scores: Dict[IntentCategory, float] = {}
        for result, w in weights:
            cat  = result.get("intent", IntentCategory.OTHER)
            conf = result.get("confidence", 0.0)
            weighted_score = w * conf
            scores[cat] = scores.get(cat, 0.0) + weighted_score
            group = _INTENT_GROUPS.get(cat, cat)
            group_scores[group] = group_scores.get(group, 0.0) + weighted_score

        best_group = max(group_scores, key=group_scores.get)
        best_score = group_scores[best_group]
        best = max(
            (cat for cat in scores if _INTENT_GROUPS.get(cat, cat) == best_group),
            key=scores.get,
        )
        pat_intent = pat.get("intent", IntentCategory.OTHER)
        pat_conf = float(pat.get("confidence", 0.0) or 0.0)
        if (best in _GENERIC_INTENTS and pat_intent in _SPECIFIC_INTENTS
                and _INTENT_GROUPS.get(pat_intent, pat_intent) == best_group
                and pat_conf >= 0.5 and best_score < 0.8):
            source_scores["refined_by_pattern"] = pat_conf
            return pat_intent, max(best_score, pat_conf), source_scores
        if best_score < self.threshold:
            return IntentCategory.OTHER, best_score, source_scores
        return best, best_score, source_scores

    # ── 实体提取 ──────────────────────────────────────────────────────────────

    def _extract_entities(self, message: str) -> Dict[str, List[str]]:
        """用规则提取高价值实体，避免每次识别都额外调用 LLM。"""
        message = self._clean_text(message)
        def object_ids(labels: str, prefix: str) -> List[str]:
            # Candidate IDs from user text are not verified identities or grants.
            explicit = re.findall(
                rf"(?:{labels})\s*[:：=]?\s*([A-Za-z0-9_-]{{1,64}})", message, re.I,
            )
            natural = re.findall(
                rf"(?<![A-Za-z0-9_])({prefix}_[A-Za-z0-9_-]{{1,59}})(?![A-Za-z0-9_-])", message, re.I,
            )
            return self._unique(explicit + [item for item in natural if item.lower() != prefix + "_id"])
        return {
            "workspace_id": object_ids(r"workspace_id|工作空间ID|工作空间标识", "ws"),
            "project_id": object_ids(r"project_id|项目ID|项目标识", "proj"),
            "request_id": self._unique(re.findall(r"(?:request_id|请求ID)\s*[:：=]?\s*([A-Za-z0-9_-]{1,64})", message, re.I)),
            "ticket_id": object_ids(r"ticket_id|工单号|工单ID|工单标识", "ticket"),
            "scope": self._unique(re.findall(r"\b(?:project|projects|workspace|user):(?:read|write)\b", message, re.I)),
            "method": self._unique(re.findall(r"\b(GET|POST|PUT|PATCH|DELETE)\b", message, re.I)),
            "api_path": self._unique(re.findall(r"/sandbox/product/v1/[A-Za-z0-9_/{}/-]+", message)),
            "business_error_code": self._unique(code.upper() for code in re.findall(
                r"(?<![A-Za-z0-9_])(?:RATE_LIMITED|QUOTA_EXCEEDED)(?![A-Za-z0-9_])", message, re.I,
            )),
            "date": self._unique(re.findall(r"(今天|明天|昨天|本周|这周|下周|\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}日?)", message)),
            "error_code": self._unique(
                re.findall(r"(?:error(?:_code)?|错误码|状态码|http)\s*[:：#]?\s*([45]\d{2})\b", message, re.I)
                + re.findall(r"(?<![A-Za-z0-9])([45]\d{2})(?![A-Za-z0-9])", message)
            ),
        }

    # ── 辅助 ──────────────────────────────────────────────────────────────────

    async def _load_template_embeddings(self) -> None:
        """懒加载所有模板的 Embedding（只在首次调用时执行）。"""
        missing = [cat for cat in _TEMPLATES if cat not in self._tpl_embeddings]
        if not missing:
            return

        all_texts = [t for cat in missing for t in _TEMPLATES[cat]]
        vecs = [await self._embed_text(text) for text in all_texts]
        idx = 0
        for cat in missing:
            n = len(_TEMPLATES[cat])
            self._tpl_embeddings[cat] = vecs[idx: idx + n]
            idx += n

    async def _embed_text(self, text: str) -> List[float]:
        """
        生成文本向量。

        如果未来接入的官方/兼容客户端提供 embeddings.create，会优先使用远端向量；
        当前 Anthropic SDK 没有该资源时，退化为字符 n-gram 哈希向量。这样不会因为
        Embedding 服务缺失导致三路融合中断。
        """
        embeddings = getattr(self.client, "embeddings", None)
        if embeddings is not None:
            try:
                resp = await embeddings.create(model="voyage-3-lite", input=[text])
                return list(resp.data[0].embedding)
            except Exception as ex:
                logger.warning(f"远端 Embedding 失败，使用本地向量兜底: {ex}")

        return self._local_embedding(text)

    @staticmethod
    def _local_embedding(text: str, dims: int = 256) -> List[float]:
        """稳定的字符 n-gram 哈希向量，仅用于词面相似匹配，并非语义模型。"""
        normalized = text.lower().strip()
        vec = [0.0] * dims
        tokens = set()
        for n in (1, 2, 3):
            if len(normalized) >= n:
                tokens.update(normalized[i:i + n] for i in range(len(normalized) - n + 1))
        if not tokens:
            tokens.add(normalized)

        for token in tokens:
            digest = hashlib.md5(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % dims
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        return vec

    def _urgency(self, message: str, intent: IntentCategory) -> UrgencyLevel:
        msg = message.lower()
        msg = re.sub(r"(?:不(?:是|用|需要)?|无需|无须|非|并不|没那么|暂不)\s*(?:紧急|立刻|马上|尽快|快点)", "", msg)
        msg = re.sub(r"\b(?:not|isn't|is not|no)\s+(?:an?\s+)?(?:emergency|urgent|asap|now)\b", "", msg)
        msg = re.sub(r'[“"「].*?[”"」]', '', msg)
        clauses = re.split(r"[，,。；;！!\n]", msg)
        current = " ".join(c for c in clauses if not re.search(r"如果|假设|例如|比如|是什么意思|文档.*写着|没有.*(?:丢失|泄露|不可用)|未发生", c))
        if any(word in current for word in ("全站不可用", "全员无法访问", "所有用户无法访问", "数据丢失", "数据泄露")):
            return UrgencyLevel.CRITICAL
        if any(word in current for word in ("紧急", "尽快", "马上")) or re.search(r"\b(?:urgent|asap|emergency)\b", current):
            return UrgencyLevel.HIGH
        if any(word in current for word in ("一直失败", "持续失败", "业务受阻", "还是不行", "仍未解决")):
            return UrgencyLevel.MEDIUM
        if intent == IntentCategory.HUMAN_HANDOFF:
            return UrgencyLevel.HIGH
        return UrgencyLevel.LOW

    def _cache_key(self, message: str, history: Optional[List[Dict[str, str]]] = None) -> str:
        payload = {"message": self._clean_text(message)}
        if history:
            payload["history"] = [
                {
                    "role": self._clean_text(item.get("role", ""))[:20],
                    "content": self._clean_text(item.get("content", "")),
                }
                for item in history[-3:]
            ]
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.md5(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _unique(values: List[str]) -> List[str]:
        return list(dict.fromkeys(value.strip() for value in values if value and value.strip()))

    @staticmethod
    def _intent_group(intent: IntentCategory) -> str:
        return _INTENT_GROUPS.get(intent, intent).value

    @staticmethod
    def _clean_text(value: Any) -> str:
        """移除 Unicode 代理字符，避免 HTTP 客户端编码 prompt 时崩溃。"""
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value.encode("utf-8", errors="ignore").decode("utf-8")

    @property
    def cache_stats(self) -> Dict[str, Any]:
        total = self.cache_hits + self.cache_misses
        return {
            "size": len(self._cache),
            "hits": self.cache_hits,
            "misses": self.cache_misses,
            "hit_rate": self.cache_hits / total if total else 0.0,
        }
