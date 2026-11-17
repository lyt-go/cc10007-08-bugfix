"""退役传播故障链路：策略库退役记录与智能推荐召回状态之间的联动校正。

链路约定：推荐策略编号与运营策略编号按数字尾段对应（RECO-0003 ↔ TACT-0003）。
所有跨模块写操作都在 store.lock 内完成，保证推荐结果与复用次数在同一版本
边界内更新；同请求重试与并发上传只会留下一份有效修复明细。
"""
from __future__ import annotations

from typing import Any

from app.store import store

TACTIC_MODULE = "tacticlib"
RECOMMEND_MODULE = "recommend"

REVIEW_BASELINE = "TACT-0001"  # 复核基准：遗留调用链数据核对以 TACT-0001 为基准
ONLINE_STATUS = "已上线"
OFFLINE_STATUS = "已下线"
RETIRED_STATUS = "已退役"

ACTION_RETIRE = "停止引用退役策略"
ACTION_REPLACE = "替代策略切换"


def code_suffix(code: Any) -> str:
    """取编号尾段作为故障链路键：RECO-0003 与 TACT-0003 落在同一链路。"""
    text = str(code or "").strip()
    if "-" in text:
        return text.rsplit("-", 1)[-1]
    return text


def find_tactic_by_code(tactic_code: Any) -> dict[str, Any] | None:
    suffix = code_suffix(tactic_code)
    if not suffix:
        return None
    for row in store.rows(TACTIC_MODULE):
        if code_suffix(row.get("策略编号")) == suffix:
            return row
    return None


def find_recommend_by_code(recommend_code: Any) -> dict[str, Any] | None:
    suffix = code_suffix(recommend_code)
    if not suffix:
        return None
    for row in store.rows(RECOMMEND_MODULE):
        if code_suffix(row.get("策略编号")) == suffix:
            return row
    return None


def linked_recommends(tactic_code: Any) -> list[dict[str, Any]]:
    """按故障链路读取引用该运营策略的推荐召回记录。"""
    suffix = code_suffix(tactic_code)
    if not suffix:
        return []
    rows = [
        row
        for row in store.rows(RECOMMEND_MODULE)
        if code_suffix(row.get("策略编号")) == suffix
    ]
    return sorted(rows, key=lambda row: int(row.get("id", 0)))


def linked_tactic(recommend_code: Any) -> dict[str, Any] | None:
    """按故障链路读取推荐召回对应的策略库记录。"""
    return find_tactic_by_code(recommend_code)


def usage_count(tactic_row: dict[str, Any]) -> int:
    """复用次数容错读取：历史样例数据可能是文本，统一按 0 起步。"""
    value = tactic_row.get("使用次数")
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value or "").strip()
    return int(text) if text.isdigit() else 0


def version_of(row: dict[str, Any]) -> int:
    value = row.get("版本")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 1
    return int(value)


def bump_boundary(*rows: dict[str, Any]) -> int:
    """把同一批记录推进到同一个版本边界，返回边界号。"""
    boundary = max((version_of(row) for row in rows), default=1) + 1
    for row in rows:
        row["版本"] = boundary
    return boundary


def last_hit_snapshot(
    recommend_row: dict[str, Any],
    tactic_row: dict[str, Any] | None,
    boundary: int,
) -> dict[str, Any]:
    """最后一次可用场景命中结果：留作历史，策略评分保持原样不改写。"""
    return {
        "推荐场景": recommend_row.get("推荐场景"),
        "召回算法": recommend_row.get("召回算法"),
        "策略评分": tactic_row.get("策略评分") if tactic_row else None,
        "命中边界": boundary,
    }


def _unique_repair_code(preferred: Any = None) -> str:
    existing = {str(record.get("修复编号")) for record in store.repairs()}
    candidate = str(preferred or "").strip()
    if candidate and candidate not in existing:
        return candidate
    code = store.next_repair_code()
    while code in existing:
        code = store.next_repair_code()
    return code


def _register_repair(
    tactic_row: dict[str, Any],
    recommend_row: dict[str, Any],
    *,
    action_label: str,
    boundary: int,
    history: dict[str, Any],
    preferred_code: Any = None,
) -> dict[str, Any]:
    """生成一条推荐召回修复明细，并交由运营策略待处理明细保存。"""
    code = _unique_repair_code(preferred_code)
    record = {
        "id": len(store.repairs()) + 1,
        "修复编号": code,
        "关联策略编号": tactic_row.get("策略编号"),
        "关联推荐编号": recommend_row.get("策略编号"),
        "推荐场景": recommend_row.get("推荐场景"),
        "原召回算法": recommend_row.get("召回算法"),
        "校正动作": action_label,
        "校正后状态": OFFLINE_STATUS,
        "复核基准": REVIEW_BASELINE,
        "版本边界": boundary,
        "历史命中": history,
        "状态": "待处理",
    }
    store.add_repair(record)
    pending_details = tactic_row.setdefault("待处理明细", [])
    pending_details.append(dict(record))
    return record


def freeze_recall(
    recommend_row: dict[str, Any],
    tactic_row: dict[str, Any],
    *,
    action_label: str,
) -> dict[str, Any]:
    """停止当前召回对退役/被替换策略的引用，留历史命中并生成修复明细。"""
    boundary = bump_boundary(tactic_row, recommend_row)
    history = last_hit_snapshot(recommend_row, tactic_row, boundary)
    recommend_row["status"] = OFFLINE_STATUS
    recommend_row["pending"] = False
    recommend_row["abnormal"] = True
    recommend_row["历史命中"] = history
    return _register_repair(
        tactic_row,
        recommend_row,
        action_label=action_label,
        boundary=boundary,
        history=history,
    )


def propagate_retirement(tactic_row: dict[str, Any]) -> list[dict[str, Any]]:
    """退役传播：故障链路上仍在引用该策略的召回全部停下并留档。"""
    targets = [
        row
        for row in linked_recommends(tactic_row.get("策略编号"))
        if row.get("status") == ONLINE_STATUS
    ]
    if not targets:
        bump_boundary(tactic_row)
        return []
    return [freeze_recall(row, tactic_row, action_label=ACTION_RETIRE) for row in targets]


def online_conflicts(recommend_row: dict[str, Any]) -> list[dict[str, Any]]:
    """同一推荐场景下仍在在线的其他召回：公开推荐不能同时引用两个策略版本。"""
    scene = recommend_row.get("推荐场景")
    return [
        row
        for row in store.rows(RECOMMEND_MODULE)
        if row is not recommend_row
        and row.get("推荐场景") == scene
        and row.get("status") == ONLINE_STATUS
    ]


def repair_exists(tactic_code: Any, recommend_code: Any) -> dict[str, Any] | None:
    for record in store.repairs():
        if (
            record.get("关联策略编号") == tactic_code
            and record.get("关联推荐编号") == recommend_code
        ):
            return record
    return None


def import_repairs(items: list[dict[str, Any]]) -> dict[str, Any]:
    """批次导入推荐召回修复明细：只接纳有效记录，重复上传按故障链路去重。

    有效口径：关联策略存在且已退役、关联推荐与策略在同一故障链路上。
    仍在线的召回在导入时立即重算校正；已离线的补登明细，兼容历史数据集口径。
    """
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    duplicated: list[dict[str, Any]] = []
    with store.lock:
        for item in items:
            if not isinstance(item, dict):
                rejected.append({"item": item, "reason": "明细记录必须是对象"})
                continue
            tactic_code = str(item.get("关联策略编号") or "").strip()
            recommend_code = str(item.get("关联推荐编号") or "").strip()
            if not tactic_code or not recommend_code:
                rejected.append({"item": item, "reason": "缺少关联策略编号或关联推荐编号"})
                continue
            tactic = find_tactic_by_code(tactic_code)
            if tactic is None:
                rejected.append({"item": item, "reason": f"运营策略 {tactic_code} 不存在"})
                continue
            if tactic.get("status") != RETIRED_STATUS:
                rejected.append({"item": item, "reason": f"运营策略 {tactic_code} 未退役，不在故障链路"})
                continue
            recommend = find_recommend_by_code(recommend_code)
            if recommend is None or code_suffix(recommend.get("策略编号")) != code_suffix(tactic_code):
                rejected.append({"item": item, "reason": f"推荐 {recommend_code} 与策略 {tactic_code} 不在同一故障链路"})
                continue
            existing = repair_exists(tactic.get("策略编号"), recommend.get("策略编号"))
            if existing is not None:
                duplicated.append(existing)
                continue
            if recommend.get("status") == ONLINE_STATUS:
                boundary = bump_boundary(tactic, recommend)
                history = last_hit_snapshot(recommend, tactic, boundary)
                recommend["status"] = OFFLINE_STATUS
                recommend["pending"] = False
                recommend["abnormal"] = True
                recommend["历史命中"] = history
                record = _register_repair(
                    tactic,
                    recommend,
                    action_label=ACTION_RETIRE,
                    boundary=boundary,
                    history=history,
                    preferred_code=item.get("修复编号"),
                )
            else:
                history = recommend.get("历史命中") or last_hit_snapshot(
                    recommend, tactic, version_of(recommend)
                )
                record = _register_repair(
                    tactic,
                    recommend,
                    action_label=ACTION_RETIRE,
                    boundary=version_of(tactic),
                    history=history,
                    preferred_code=item.get("修复编号"),
                )
            accepted.append(record)
    return {"accepted": accepted, "rejected": rejected, "duplicated": duplicated}
