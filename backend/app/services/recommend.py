"""智能推荐业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services import recall_chain
from app.store import store

MODULE = "recommend"
REQUIRED_FIELDS = ["策略编号", "推荐场景", "召回算法"]
STATUS_ORDER = ["草稿", "策略评审中", "已上线", "已下线"]
ACTION_RULES = {"提交策略评审": "策略评审中", "上线策略": "已上线", "下线策略": "已下线"}
NEGATIVE_ACTIONS = ["下线策略"]


def enrich_entry(row: dict[str, Any]) -> dict[str, Any]:
    """列表与详情共用同一份关联视图，刷新后业务关联项一致。"""
    data = dict(row)
    tactic = recall_chain.linked_tactic(row.get("策略编号"))
    if tactic is not None:
        data["关联策略编号"] = tactic.get("策略编号")
        data["关联策略状态"] = tactic.get("status")
    else:
        data["关联策略编号"] = ""
        data["关联策略状态"] = "未关联"
    data["版本"] = recall_chain.version_of(row)
    return data


class RecommendService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [enrich_entry(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        row = store.find(MODULE, entry_id)
        if row is None:
            return None
        return enrich_entry(row)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def import_repairs(
        self, items: list[dict[str, Any]]
    ) -> dict[str, list[dict[str, Any]]]:
        """接收推荐召回修复明细批次：只接纳有效记录，并发上传按故障链路去重。"""
        return recall_chain.import_repairs(items)

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"推荐策略 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于智能推荐可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            if entry.get("status") == target:
                # 同请求重试：直接回当前状态，复用次数不重复增长。
                return enrich_entry(entry), f"推荐策略已处于{target}，重复请求已忽略"
            if target == "已上线":
                tactic = recall_chain.linked_tactic(entry.get("策略编号"))
                if tactic is not None and tactic.get("status") == recall_chain.RETIRED_STATUS:
                    return None, (
                        f"关联运营策略 {tactic.get('策略编号')} 已退役，"
                        "退役策略不能进入新的推荐召回"
                    )
                # 替代策略切换：同一推荐场景只保留一个可用召回，
                # 公开推荐结果不能同时引用两个策略版本。
                for other in recall_chain.online_conflicts(entry):
                    other_tactic = recall_chain.linked_tactic(other.get("策略编号"))
                    if other_tactic is not None:
                        recall_chain.freeze_recall(
                            other, other_tactic, action_label=recall_chain.ACTION_REPLACE
                        )
                    else:
                        other["status"] = "已下线"
                        other["pending"] = False
                        other["abnormal"] = True
                        recall_chain.bump_boundary(other)
                entry["status"] = target
                entry["pending"] = target != STATUS_ORDER[-1]
                entry["abnormal"] = action in NEGATIVE_ACTIONS
                if tactic is not None:
                    # 推荐结果与复用次数在同一版本边界内更新。
                    tactic["使用次数"] = recall_chain.usage_count(tactic) + 1
                    recall_chain.bump_boundary(entry, tactic)
                else:
                    recall_chain.bump_boundary(entry)
                return enrich_entry(entry), f"推荐策略已{action}"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            recall_chain.bump_boundary(entry)
            return enrich_entry(entry), f"推荐策略已{action}"
