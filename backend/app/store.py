"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
"""
from __future__ import annotations

import threading
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 跨模块写操作（退役传播、替代策略切换、批次导入）都走同一把锁，
        # 保证推荐结果与复用次数落在同一版本边界里，并发上传不会拿到中间态。
        self.lock = threading.RLock()
        # 推荐召回修复明细数据集：退役传播与批次导入生成的校正记录统一留档，
        # 并同步挂到运营策略的待处理明细上，供业务版本材料核对。
        self._repairs: list[dict[str, Any]] = []
        self._repair_seq = 0

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def repairs(self) -> list[dict[str, Any]]:
        """推荐召回修复明细数据集（全量留档）。"""
        return self._repairs

    def next_repair_code(self) -> str:
        self._repair_seq += 1
        return f"FIX-{self._repair_seq:04d}"

    def add_repair(self, record: dict[str, Any]) -> None:
        self._repairs.append(record)

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()
