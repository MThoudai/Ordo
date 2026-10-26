"""带来源的会话陈述；用于指代，不是身份、授权或执行回执。"""
import re


def merge_state(previous, entities, intent_group, message, request_id):
    previous = previous if isinstance(previous, dict) else {}
    old = previous.get("entities", {})
    old = old if isinstance(old, dict) else {}
    sources = previous.get("sources", {})
    new_task = bool(re.search(r"(?:换个|新的|另一个)(?:问题|任务|项目)|重新开始", message))
    changed_project = bool(entities.get("project_id") and old.get("project_id")
                           and entities["project_id"] != old["project_id"])
    changed_workspace = bool(entities.get("workspace_id") and old.get("workspace_id")
                             and entities["workspace_id"] != old["workspace_id"])
    keep = not new_task and not changed_workspace
    merged = {k: list(v) for k, v in old.items() if isinstance(v, list)} if keep else {}
    origins = dict(sources) if keep else {}
    if changed_project:
        for key in ("project_id", "api_path", "method", "error_code", "business_error_code", "request_id", "ticket_id"):
            merged.pop(key, None)
            origins.pop(key, None)
    # Endpoint-specific constraints must not move from a list call into a detail or /me call.
    if entities.get("api_path") and entities["api_path"] != old.get("api_path"):
        for key in ("scope", "error_code", "business_error_code", "request_id"):
            merged.pop(key, None)
            origins.pop(key, None)
    for key, values in entities.items():
        if values:
            merged[key] = list(values)
            origins[key] = request_id
        else:
            merged.setdefault(key, [])
    claims = dict(previous.get("user_claims", {})) if keep else {}
    hypothetical = bool(re.search(r"如果|假设|设想|倘若|比如.*(?:变成|成为)", message))
    if not hypothetical:
        role = re.search(r"(?:我是|我其实是|我的角色是|角色(?:更正为|改为|是))\s*(admin|member|guest)\b", message, re.I)
        if role:
            claims["workspace_role"] = {"value": role.group(1).lower(), "source_request_id": request_id,
                                        "source": "user_statement", "verified": False}
    return {"domain": intent_group or previous.get("domain", "other"), "entities": merged,
            "sources": origins, "user_claims": claims, "verified": False}
