"""分析操作日志业务规则：状态流转、字段校验与筛选口径都收在这里。

台账生命周期：已记录 --标记复核--> 已复核 --归档记录--> 已归档，单向不可回退。
实验模块追加进来的审计台账默认落在「已记录」，随实验动作同步生成待复核任务。
"""
from __future__ import annotations

from typing import Any

from app.store import store

MODULE = "log"
REQUIRED_FIELDS = ["日志编号", "操作类型", "操作对象"]
STATUS_ORDER = ["已记录", "已复核", "已归档"]
# 动作 -> (唯一允许的前置状态, 目标状态)。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "标记复核": ("已记录", "已复核"),
    "归档记录": ("已复核", "已归档"),
}
NEGATIVE_ACTIONS: list[str] = []


class LogService:
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
            rows = [row for row in rows if keyword in str(row.get("日志编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        with store.lock:
            rows = store.rows(MODULE)
            entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
            entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
            entry["status"] = STATUS_ORDER[0]
            entry["pending"] = True
            entry["abnormal"] = False
            rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于分析操作日志可执行范围"
        required_status, target = ACTION_RULES[action]
        with store.lock:
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"操作日志 {entry_id} 不存在或已归档"
            current = str(entry.get("status") or "")
            if current != required_status:
                return None, f"当前状态为「{current}」，不允许执行「{action}」；仅「{required_status}」状态可执行该动作"
            entry["status"] = target
            entry["pending"] = target != STATUS_ORDER[-1]
            entry["abnormal"] = action in NEGATIVE_ACTIONS
            return entry, f"操作日志已{action}"
