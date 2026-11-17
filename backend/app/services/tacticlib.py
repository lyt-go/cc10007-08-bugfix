"""策略库业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services import recall_chain
from app.store import store

MODULE = "tacticlib"
REQUIRED_FIELDS = ["策略编号", "策略名称", "策略类型"]
STATUS_ORDER = ["草稿", "策略评审中", "已发布", "已退役"]
ACTION_RULES = {"提交策略评审": "策略评审中", "发布策略": "已发布", "退役策略": "已退役"}
NEGATIVE_ACTIONS = []


def enrich_entry(row: dict[str, Any]) -> dict[str, Any]:
    """清单与详情共用同一份关联视图：智能推荐结果反映到运营策略库清单。"""
    data = dict(row)
    linked = recall_chain.linked_recommends(row.get("策略编号"))
    if linked:
        data["关联推荐编号"] = "、".join(str(item.get("策略编号")) for item in linked)
        data["关联推荐状态"] = "、".join(str(item.get("status")) for item in linked)
    else:
        data["关联推荐编号"] = ""
        data["关联推荐状态"] = "未关联"
    data["版本"] = recall_chain.version_of(row)
    data.setdefault("待处理明细", [])
    return data


class TacticlibService:
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

    def list_repairs(
        self,
        *,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """推荐召回修复明细数据集：按关联策略编号检索，与待处理明细同一份留档。"""
        records = list(store.repairs())
        if keyword:
            records = [
                record
                for record in records
                if keyword in str(record.get("关联策略编号", ""))
                or keyword in str(record.get("关联推荐编号", ""))
                or keyword in str(record.get("修复编号", ""))
            ]
        total = len(records)
        start = max(page - 1, 0) * size
        return records[start:start + size], total

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

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"运营策略 {entry_id} 不存在或已归档"
            if action not in ACTION_RULES:
                return None, f"动作「{action}」不属于策略库可执行范围"
            target = ACTION_RULES[action]
            if target not in STATUS_ORDER:
                return None, f"目标状态「{target}」不在允许的状态序列里"
            if entry.get("status") == target:
                # 同请求重试：直接回当前状态，不重复传播、不重复生成修复明细。
                return enrich_entry(entry), f"运营策略已处于{target}，重复请求已忽略"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            message = f"运营策略已{action}"
            if target == "已退役":
                # 退役传播：按故障链路重算推荐召回，生成推荐召回修复明细。
                repairs = recall_chain.propagate_retirement(entry)
                if repairs:
                    message += f"，退役传播已校正 {len(repairs)} 条推荐召回"
                else:
                    message += "，故障链路上没有待校正的推荐召回"
            else:
                recall_chain.bump_boundary(entry)
            return enrich_entry(entry), message
