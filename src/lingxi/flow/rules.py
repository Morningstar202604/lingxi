"""Phase 2 — 自动化规则引擎：事件触发条件 → 执行动作。

🔗 工程逻辑
    在 Agent 推理之前/之外，提供一套轻量规则引擎：
    - RuleCondition: 基于字段/操作符/值 匹配事件字段
    - RuleAction: escalate / reply_template / set_category / webhook / tag
    - RuleEngine: AND 条件聚合 → priority 降序评估 → 执行动作

    典型用法: 收到含"投诉"的消息 → 立刻升级，不浪费 Agent token;
    或者 low_confidence + 3 轮对话未解决 → 自动工单升级到人工。

🤖 AI 避坑
    - 规则评估是同步的 (evaluate), 只有 execute 才是 async
    - YAML 加载只做最小解析，不运行任意代码
"""
from __future__ import annotations

import contextlib
from typing import Any

import orjson
import yaml
from pydantic import BaseModel, Field


def orjson_dumps(obj: Any) -> bytes:
    """序列化为 JSON bytes (orjson 比 stdlib json 快 3-10x)。"""
    return orjson.dumps(obj, default=str)


class RuleCondition(BaseModel):
    """触发条件。"""

    field: str   # "message_contains", "confidence_below", "ticket_status", "channel", "time_exceeded"
    operator: str  # "equals", "contains", "gt", "lt", "in"
    value: Any


class RuleAction(BaseModel):
    """触发动作。"""

    action: str  # "escalate", "reply_template", "set_category", "webhook", "tag"
    params: dict[str, Any] = Field(default_factory=dict)


class Rule(BaseModel):
    """一条自动化规则。"""

    name: str
    enabled: bool = True
    conditions: list[RuleCondition]
    actions: list[RuleAction]
    priority: int = 0


def _match_condition(condition: RuleCondition, event: dict[str, Any]) -> bool:
    """评估单个条件是否满足。"""
    field = condition.field
    op = condition.operator
    cond_val = condition.value

    # 特殊字段映射
    if field == "message_contains":
        text = event.get("message", "") or ""
        if op == "contains":
            return cond_val in text
        return False

    if field == "confidence_below":
        conf = event.get("confidence")
        if conf is None:
            return False
        try:
            return float(conf) < float(cond_val)
        except (TypeError, ValueError):
            return False

    # 通用字段匹配
    actual = event.get(field)
    if actual is None:
        return False

    if op == "equals":
        return bool(actual == cond_val)
    if op == "contains":
        if isinstance(actual, str) and isinstance(cond_val, str):
            return cond_val in actual
        return False
    if op == "gt":
        try:
            return float(actual) > float(cond_val)
        except (TypeError, ValueError):
            return False
    if op == "lt":
        try:
            return float(actual) < float(cond_val)
        except (TypeError, ValueError):
            return False
    if op == "in":
        if isinstance(cond_val, list):
            return actual in cond_val
        return actual in [cond_val]

    return False


async def _execute_action(
    action: RuleAction,
    event: dict[str, Any],
    *,
    ticket: Any = None,
    escalate_to_queue: Any = None,
    send_func: Any = None,
) -> tuple[str, str | None]:
    """执行单个动作。返回 (action_name, error_or_None)。"""
    name = action.action

    if name == "escalate":
        from lingxi.flow.ticket import TicketStatus
        if ticket is not None:
            with contextlib.suppress(ValueError):
                ticket.transition(TicketStatus.ESCALATED)
        if escalate_to_queue is not None and ticket is not None:
            escalate_to_queue.enqueue(ticket, reason=action.params.get("reason", "rule_engine"))
        return name, None

    if name == "reply_template":
        return name, None

    if name == "set_category":
        if ticket is not None:
            cat_val = action.params.get("category", "")
            from lingxi.flow.ticket import TicketCategory
            try:
                ticket.category = TicketCategory(cat_val)
            except ValueError:
                return name, f"invalid category: {cat_val}"
        return name, None

    if name == "webhook":
        url = action.params.get("url", "")
        if not url:
            return name, "missing url in webhook params"
        try:
            import httpx

            from lingxi.config import get_settings
            from lingxi.security import sign_webhook_payload

            settings = get_settings()
            body_bytes = orjson_dumps(event)

            # HMAC-SHA256 签名（如配置了 hmac_secret）
            extra_headers: dict[str, str] = {"Content-Type": "application/json"}
            if settings.webhook.hmac_secret:
                secret = settings.webhook.hmac_secret.get_secret_value()
                sig_headers = sign_webhook_payload(body_bytes, secret)
                extra_headers.update(sig_headers)

            timeout = settings.webhook.timeout
            max_retries = settings.webhook.max_retries

            async with httpx.AsyncClient(timeout=timeout) as client:
                for attempt in range(max_retries + 1):
                    try:
                        resp = await client.post(url, content=body_bytes, headers=extra_headers)
                        if resp.status_code < 500:
                            break
                    except httpx.HTTPError:
                        if attempt == max_retries:
                            raise
        except Exception as e:
            return name, str(e)
        return name, None

    if name == "tag":
        if ticket is not None:
            tag = action.params.get("tag", "")
            if tag:
                tags = getattr(ticket, "_tags", None)
                if tags is None:
                    ticket._tags = []
                    tags = ticket._tags
                if tag not in tags:
                    tags.append(tag)
        return name, None

    return name, f"unknown action: {name}"


class RuleEngine:
    """评估规则 → 执行动作。"""

    def __init__(self, rules: list[Rule] | None = None) -> None:
        self._rules: list[Rule] = list(rules) if rules else []

    def add_rule(self, rule: Rule) -> None:
        """添加规则。同名则覆盖。"""
        self._rules = [r for r in self._rules if r.name != rule.name]
        self._rules.append(rule)
        self._rules.sort(key=lambda r: -r.priority)

    def remove_rule(self, name: str) -> bool:
        """按名称删除规则。返回是否找到并删除。"""
        orig_len = len(self._rules)
        self._rules = [r for r in self._rules if r.name != name]
        return len(self._rules) < orig_len

    def list_rules(self) -> list[dict[str, Any]]:
        """列出所有规则详情。"""
        return [r.model_dump() for r in self._rules]

    def evaluate(self, event: dict[str, Any]) -> list[RuleAction]:
        """评估所有规则，返回所有匹配规则的动作列表 (priority DESC)。"""
        matched_actions: list[RuleAction] = []
        for rule in self._rules:
            if not rule.enabled:
                continue
            # AND: 所有 condition 必须 match
            if all(_match_condition(c, event) for c in rule.conditions):
                matched_actions.extend(rule.actions)
        return matched_actions

    async def execute(
        self,
        event: dict[str, Any],
        *,
        ticket: Any = None,
        escalate_to_queue: Any = None,
        send_func: Any = None,
    ) -> dict[str, list[str]]:
        """evaluate + 执行动作。返回 {"executed": [action_names], "errors": []}。"""
        actions = self.evaluate(event)
        executed: list[str] = []
        errors: list[str] = []
        for action in actions:
            name, err = await _execute_action(
                action, event,
                ticket=ticket,
                escalate_to_queue=escalate_to_queue,
                send_func=send_func,
            )
            executed.append(name)
            if err:
                errors.append(f"{name}: {err}")
        return {"executed": executed, "errors": errors}

    def load_yaml(self, path: str) -> int:
        """加载 YAML 规则文件, 返回加载条数。"""
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not isinstance(data, list):
            data = [data] if data else []
        count = 0
        for item in data:
            rule = Rule(**item)
            self.add_rule(rule)
            count += 1
        return count

    def export_yaml(self) -> str:
        """导出规则为 YAML 字符串。"""
        data = [r.model_dump() for r in self._rules]
        return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


__all__ = [
    "Rule",
    "RuleAction",
    "RuleCondition",
    "RuleEngine",
]
