"""Shared reply instructions for individual agents and combined answers."""

import re


def clean_guidance_reply(text: str, message: str, intent: str = "") -> str:
    """Drop explicit assistant disclaimers only in unambiguous ordinary guidance.

    Execution/capability questions and uncertain requests keep their original
    answer. Business restrictions and operational warnings are not matched.
    Called only for successful generated answers, never failure messages.
    """
    execution = (
        r"(?:替我|替我们|代我|代为|代办|帮我|帮我们).{0,12}(?:查询|查一下|查查|恢复|保存|提交|修改|发送|邀请|执行|运行|接通)|"
        r"(?:你能|你可以|你是否|能不能|可不可以|能否).{0,12}(?:查询|查到|读取|恢复|保存|提交|修改|发送|邀请|执行|运行|接通)|"
        r"直接.{0,8}(?:查|恢复|保存|提交|修改|发送|邀请|执行|运行|操作)|"
        r"(?:能力限制|有什么限制|不能做|不支持|是否接入|有没有接入|真实业务|后台日志)"
    )
    if re.search(execution, message, re.I):
        return text
    ordinary = intent == 'product_overview' or re.search(
        r"如何|怎么操作|怎么处理|怎么办|应该.{0,6}(?:处理|操作)|操作步骤|"
        r"起什么作用|主要能帮团队做什么|平台有什么功能", message)
    if not ordinary:
        return text
    aside = re.compile(
        r"(?:我|我们|本助手|该助手|这个助手|Ordo|它).{0,12}"
        r"(?:未接入|不连接|没有接入|无法(?:代|替|连接|访问|读取|查询)|"
        r"不能(?:代|替|直接(?:读取|查询|操作|执行))|查不到(?:你的|您|真实))", re.I)
    overview = intent == 'product_overview' or bool(re.search(r'起什么作用|主要能帮团队做什么|平台有什么功能', message))
    generic_redirect = re.compile(r'(?:实际查询状态|查询状态、提交申请).{0,60}(?:需要|须|要)你.{0,20}页面')
    parts = re.findall(r"[^。！？\n]+[。！？]?|\n+", text)
    kept = ''.join(part for part in parts if not aside.search(part)
                   and not (overview and generic_redirect.search(part))).strip()
    # If the whole answer was a disclaimer, do not silently replace it with an
    # empty success. Let the caller retain the original result for diagnosis.
    return kept if kept else text

RESPONSE_GUIDANCE = (
    "\n\n[对用户的表达方式]\n"
    "默认直接回答问题：概念给解释，方法给步骤，故障给检查顺序。"
    "平台介绍侧重能做什么，排错侧重用户接下来怎么做。"
    "依据用户背景分析时，必要时用‘按你描述’引入即可，不附加身份未经核验的段落。"
    "知识资料和候选回答中的助手能力声明属于背景，不随教程、FAQ或故障步骤一并复述。"
    "例如用户问‘保存失败怎么办’，先讲备份与排查；不要追加‘我不能替你保存或恢复’。"
    "只有用户这次明确要求你实际代查、提交、修改、恢复、执行接口、接通人工，或询问能否代办时，"
    "才简短回答这项执行要求暂不支持，再给可行的自助步骤或材料。"
    "例如‘直接帮我恢复文档’需要说明不能代恢复；后续问‘那我怎么操作’则直接讲方法。"
    "‘Ordo起什么作用/能帮我什么/支持哪些问题’是用途介绍，不是代办能力询问，"
    "只介绍能获得的帮助和入口，不追加无法查询、无法提交、未接业务系统等段落；"
    "只有‘你能替我查/改/提交吗’这类具体执行要求才触发执行限制说明。"
    "保留与问题相关的实际条件和风险，如guest权限上限、保存前备份；不要删除这些业务规则。"
    "任何时候都不编造已核验、已提交、已执行或已联系的结果。"
)
