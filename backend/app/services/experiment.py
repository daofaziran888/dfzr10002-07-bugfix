"""分析实验管理业务规则：状态流转、字段校验、异常恢复与审计台账都收在这里。

生命周期只允许单向流转、逐级推进，不允许回退或跳级：

    草稿 --启动实验--> 运行中 --结束实验--> 已完成 --关闭实验--> 已关闭

运行中的实验可被标记为异常（原因必填），异常实验可从运行中重试恢复，也可
直接结束。实验每次动作都会向分析操作日志（审计台账）追加一条不可变记录，
待复核任务随实验状态同步生成，历史台账保留动作发生时的状态结论。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

MODULE = "experiment"
AUDIT_MODULE = "log"
OPERATOR = "值班管理员"
REQUIRED_FIELDS = ["实验编号", "实验名称", "分析数据集"]
STATUS_ORDER = ["草稿", "运行中", "已完成", "已关闭"]
# 动作 -> (唯一允许的前置状态, 目标状态)：只允许单向逐级流转。
ACTION_RULES: dict[str, tuple[str, str]] = {
    "启动实验": ("草稿", "运行中"),
    "结束实验": ("运行中", "已完成"),
    "关闭实验": ("已完成", "已关闭"),
}
# 异常处置动作：仅在运行中可用，不改变生命周期位置。
ABNORMAL_ACTIONS = {"标记异常", "恢复实验"}
# 动作 -> 状态流转完成后写入的时间字段；只补空值，绝不覆盖历史结论时间。
ACTION_TIME_FIELDS = {
    "启动实验": "启动时间",
    "结束实验": "结束时间",
    "关闭实验": "关闭时间",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _reject(action: str, status: str) -> str:
    """按当前状态给出明确的拒绝原因，已关闭实验重启必须明确拒绝。"""
    if action == "启动实验":
        if status == "已关闭":
            return "实验已关闭，生命周期已结束，不能重新启动"
        if status == "运行中":
            return "实验已在运行中，请勿重复启动"
        if status == "已完成":
            return "实验已完成，不能重新启动；如需归档请执行关闭实验"
    if action == "结束实验":
        if status == "草稿":
            return "实验尚未启动，请先执行启动实验"
        if status == "已完成":
            return "实验已结束，请勿重复操作"
        if status == "已关闭":
            return "实验已关闭，不能再执行结束实验"
    if action == "关闭实验":
        if status == "草稿":
            return "实验仍在草稿状态，请先启动并结束实验后再关闭"
        if status == "运行中":
            return "实验仍在运行中，请先结束实验再关闭"
        if status == "已关闭":
            return "实验已关闭，请勿重复操作"
    if action == "标记异常":
        if status != "运行中":
            return "只有运行中的实验才能标记异常"
    if action == "恢复实验":
        if status != "运行中":
            return "只有运行中的异常实验才能重试恢复"
    return f"当前状态为「{status}」，不允许执行「{action}」"


class ExperimentService:
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
            rows = [row for row in rows if keyword in str(row.get("实验编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def status_stats(self) -> dict[str, int]:
        """看板统计与列表读同一份数据，保证运行中/已完成数量不再错位。"""
        rows = store.rows(MODULE)
        return {
            "total": len(rows),
            **{status: sum(1 for row in rows if row.get("status") == status) for status in STATUS_ORDER},
        }

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
            entry["异常原因"] = ""
            entry["启动时间"] = ""
            entry["结束时间"] = ""
            entry["关闭时间"] = ""
            rows.append(entry)
            self._write_audit(entry, "登记实验", "草稿", "草稿", ok=True, reason="")
        return entry, []

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        reason: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        if action not in ACTION_RULES and action not in ABNORMAL_ACTIONS:
            return None, f"动作「{action}」不属于分析实验管理可执行范围"
        reason = (reason or "").strip()
        with store.lock:
            # 锁内重新读取：并发/重复点击只有第一个请求能通过前置状态校验。
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None, f"分析实验 {entry_id} 不存在或已归档"
            current = str(entry.get("status") or "")

            if action in ABNORMAL_ACTIONS:
                message = self._apply_abnormal(entry, action, reason)
                if message:
                    self._write_audit(entry, action, current, current, ok=False, reason=reason)
                    return None, message
                self._write_audit(entry, action, current, current, ok=True, reason=reason)
                return entry, f"分析实验已{action}"

            required_status, target = ACTION_RULES[action]
            if current != required_status:
                message = _reject(action, current)
                # 拒绝也是审计事件：台账保留原结论，状态不被篡改。
                self._write_audit(entry, action, current, current, ok=False, reason=reason)
                return None, message

            entry["status"] = target
            # 展示字段与内部状态同源同步，避免详情页与列表读到两个结论。
            entry["实验状态"] = target
            time_field = ACTION_TIME_FIELDS[action]
            # 补记只填空缺：历史已完成实验的结论时间不能被后来的补录覆盖。
            if not entry.get(time_field):
                entry[time_field] = _now()
            entry["pending"] = target != STATUS_ORDER[-1]
            if action == "结束实验":
                # 异常实验结束后结论落定，异常原因作为结论说明保留。
                entry["abnormal"] = False
            self._write_audit(entry, action, required_status, target, ok=True, reason=reason)
            return entry, f"分析实验已{action}"

    def _apply_abnormal(self, entry: dict[str, Any], action: str, reason: str) -> str:
        """异常标记与重试恢复；返回非空字符串表示拒绝原因。"""
        current = str(entry.get("status") or "")
        if current != "运行中":
            return _reject(action, current)
        if action == "标记异常":
            if not reason:
                return "标记异常必须填写异常原因（失败/中断说明）"
            entry["abnormal"] = True
            entry["异常原因"] = reason
            entry["pending"] = True
            entry["实验状态"] = current
            return ""
        # 恢复实验：从可重试状态（运行中且异常）回到正常运行。
        if not entry.get("abnormal"):
            return "实验当前不是异常状态，无需恢复"
        entry["abnormal"] = False
        entry["异常原因"] = ""
        entry["pending"] = True
        entry["实验状态"] = current
        return ""

    def _write_audit(
        self,
        entry: dict[str, Any],
        action: str,
        from_status: str,
        to_status: str,
        *,
        ok: bool,
        reason: str,
    ) -> None:
        """向相邻模块（分析操作日志）追加不可变审计台账。

        台账只追加、不回改：动作发生时的状态结论被快照保留，后续补录不会覆盖。
        新台账默认「已记录、待复核」，待复核任务随实验状态同步生成。
        """
        rows = store.rows(AUDIT_MODULE)
        log_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        code = f"LOG-{log_id:04d}"
        experiment_code = str(entry.get("实验编号") or entry.get("id"))
        if ok:
            remark = f"实验 {experiment_code} 执行「{action}」：{from_status} → {to_status}"
            if reason:
                remark = f"{remark}；原因：{reason}"
        else:
            remark = f"实验 {experiment_code} 执行「{action}」被拒绝（当前状态：{from_status}）；原因：{reason or '状态不满足流转条件'}"
        rows.append({
            "id": log_id,
            "status": "已记录",
            "pending": True,
            "abnormal": not ok,
            "日志编号": code,
            "操作类型": f"分析实验-{action}",
            "操作对象": experiment_code,
            "操作人": OPERATOR,
            "操作时间": _now(),
            "查询耗时": "—",
            "返回行数": "—",
            "日志备注": remark,
            # 历史台账快照字段：保留动作当时的结论，不随后续状态变化而变化。
            "来源模块": MODULE,
            "来源记录ID": entry.get("id"),
            "动作时状态": from_status,
            "结论状态": to_status if ok else from_status,
            "动作结论": "成功" if ok else "已拒绝",
            "异常原因": reason,
        })
