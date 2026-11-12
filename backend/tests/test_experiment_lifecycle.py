"""分析实验生命周期与审计台账回归测试（标准库实现，不依赖 pytest）。

运行：cd backend && .venv/bin/python -I tests/test_experiment_lifecycle.py
"""
from __future__ import annotations

import concurrent.futures
import pathlib
import sys
import threading
import traceback

# 隔离模式（python -I）不带 cwd，先把 backend 根目录放进导入路径。
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.services.experiment import ExperimentService
from app.services.log import LogService
from app.store import store

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  PASS  {name}")
    else:
        FAILURES.append(f"{name} {detail}")
        print(f"  FAIL  {name} {detail}")


def find_experiment(code: str) -> dict:
    for row in store.rows("experiment"):
        if row.get("实验编号") == code:
            return row
    raise AssertionError(f"种子数据缺少 {code}")


def main() -> int:
    svc = ExperimentService()
    log_svc = LogService()
    audit_before = len(store.rows("log"))

    print("== 1. EXPE-0002 草稿启动后直接进入运行中 ==")
    expe2 = find_experiment("EXPE-0002")
    check("EXPE-0002 初始为草稿", expe2["status"] == "草稿", expe2["status"])
    entry, msg = svc.run_action(expe2["id"], "启动实验")
    check("启动成功", entry is not None and entry["status"] == "运行中", msg)
    check("启动时间已记录", bool(entry and entry.get("启动时间")))

    print("== 2. 重复启动/跳级关闭必须被拒绝，状态不变 ==")
    before = expe2["status"]
    entry, msg = svc.run_action(expe2["id"], "启动实验")
    check("重复启动被拒绝", entry is None and "重复启动" in msg, msg)
    check("拒绝后状态保持运行中", expe2["status"] == before == "运行中")
    entry, msg = svc.run_action(expe2["id"], "关闭实验")
    check("运行中直接关闭被拒绝并提示先结束", entry is None and "先结束" in msg, msg)
    check("拒绝后仍是运行中", expe2["status"] == "运行中")

    print("== 3. 结束只允许运行中 -> 已完成 ==")
    entry, msg = svc.run_action(expe2["id"], "结束实验")
    check("结束成功进入已完成", entry is not None and entry["status"] == "已完成", msg)
    check("结束时间已记录", bool(entry and entry.get("结束时间")))
    entry, msg = svc.run_action(expe2["id"], "结束实验")
    check("已完成再结束被拒绝", entry is None and "已结束" in msg, msg)

    print("== 4. 关闭只允许已完成 -> 已关闭，关闭后不能重启 ==")
    entry, msg = svc.run_action(expe2["id"], "关闭实验")
    check("关闭成功进入已关闭", entry is not None and entry["status"] == "已关闭", msg)
    check("关闭后 pending=False", entry is not None and entry.get("pending") is False)
    check("关闭时间已记录", bool(entry and entry.get("关闭时间")))
    entry, msg = svc.run_action(expe2["id"], "启动实验")
    check("已关闭再启动得到明确拒绝", entry is None and "不能重新启动" in msg, msg)
    check("审计结论保留原状态", expe2["status"] == "已关闭")

    print("== 5. EXPE-0003 运行中：关闭被拦截，结束后才可关闭 ==")
    expe3 = find_experiment("EXPE-0003")
    check("EXPE-0003 初始运行中", expe3["status"] == "运行中", expe3["status"])
    entry, msg = svc.run_action(expe3["id"], "关闭实验")
    check("运行中关闭被拒绝", entry is None and "先结束" in msg, msg)
    entry, msg = svc.run_action(expe3["id"], "结束实验")
    check("结束进入已完成", entry is not None and entry["status"] == "已完成", msg)
    entry, msg = svc.run_action(expe3["id"], "关闭实验")
    check("已完成关闭进入已关闭", entry is not None and entry["status"] == "已关闭", msg)

    print("== 6. 历史已完成实验的结论时间不被补录覆盖 ==")
    expe4 = find_experiment("EXPE-0004")
    original_end = expe4["结束时间"]
    check("EXPE-0004 初始已完成且带历史结束时间", expe4["status"] == "已完成" and bool(original_end))
    entry, msg = svc.run_action(expe4["id"], "结束实验")
    check("已完成不允许重复结束", entry is None)
    check("历史结束时间未被覆盖", expe4["结束时间"] == original_end, expe4["结束时间"])

    print("== 7. 异常标记（原因必填）与重试恢复 ==")
    created, missing = svc.create_entry({
        "实验编号": "EXPE-TEST-1",
        "实验名称": "异常恢复实验",
        "分析数据集": "DS-TEST",
    })
    check("新建实验为草稿且无缺失字段", created is not None and not missing)
    assert created is not None
    entry, msg = svc.run_action(created["id"], "标记异常")
    check("草稿标记异常被拒绝", entry is None and "运行中" in msg, msg)
    svc.run_action(created["id"], "启动实验")
    entry, msg = svc.run_action(created["id"], "标记异常")
    check("无原因标记异常被拒绝", entry is None and "异常原因" in msg, msg)
    entry, msg = svc.run_action(created["id"], "标记异常", reason="样本抽取任务中断")
    check("带原因标记异常成功且保持运行中", entry is not None and entry["abnormal"] is True and entry["status"] == "运行中", msg)
    check("异常原因已保存", entry and entry.get("异常原因") == "样本抽取任务中断")
    entry, msg = svc.run_action(created["id"], "恢复实验")
    check("恢复后仍在运行中且清除异常", entry is not None and entry["abnormal"] is False and entry["status"] == "运行中", msg)
    entry, msg = svc.run_action(created["id"], "恢复实验")
    check("非异常状态恢复被拒绝", entry is None and "无需恢复" in msg, msg)
    # 异常实验也允许直接结束，结论保留原因说明
    svc.run_action(created["id"], "标记异常", reason="数据源延迟")
    entry, msg = svc.run_action(created["id"], "结束实验")
    check("异常实验可直接结束", entry is not None and entry["status"] == "已完成", msg)
    check("结束后异常标记清除、原因留档", entry is not None and entry["abnormal"] is False and entry.get("异常原因") == "数据源延迟")

    print("== 8. 并发/重复点击：同一动作只接受一个请求 ==")
    parallel, _ = svc.create_entry({
        "实验编号": "EXPE-TEST-2",
        "实验名称": "并发实验",
        "分析数据集": "DS-TEST",
    })
    assert parallel is not None
    barrier = threading.Barrier(8)

    def concurrent_start() -> tuple[bool, str]:
        barrier.wait()
        entry, msg = svc.run_action(parallel["id"], "启动实验")
        return entry is not None, msg

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: concurrent_start(), range(8)))
    accepted = [item for item in results if item[0]]
    rejected = [item for item in results if not item[0]]
    check("8 个并发启动仅 1 个成功", len(accepted) == 1, f"accepted={len(accepted)}")
    check("其余 7 个请求被明确拒绝", len(rejected) == 7, f"rejected={len(rejected)}")
    check("并发后状态为运行中", parallel["status"] == "运行中", parallel["status"])

    print("== 9. 未知动作与不存在记录 ==")
    entry, msg = svc.run_action(parallel["id"], "删除实验")
    check("未知动作被拒绝", entry is None and "可执行范围" in msg, msg)
    entry, msg = svc.run_action(99999, "启动实验")
    check("不存在记录被拒绝", entry is None and "不存在" in msg, msg)

    print("== 10. 审计台账联动：待复核随动作生成、历史快照保留结论 ==")
    logs = store.rows("log")
    check("每个动作都追加了台账记录", len(logs) > audit_before, f"{audit_before} -> {len(logs)}")
    experiment_logs = [row for row in logs if row.get("来源模块") == "experiment"]
    check("实验台账包含动作时状态快照", all("动作时状态" in row and "结论状态" in row for row in experiment_logs))
    new_logs = [row for row in logs if row.get("来源模块") == "experiment" and row["id"] > audit_before]
    check("新台账默认待复核（已记录/pending=True）", all(row["status"] == "已记录" and row["pending"] is True for row in new_logs))
    rejected_logs = [row for row in new_logs if row["动作结论"] == "已拒绝"]
    check("拒绝动作也留痕且结论状态保留原状态", len(rejected_logs) >= 3 and all(row["结论状态"] == row["动作时状态"] for row in rejected_logs))
    # EXPE-0002 的关闭后重启被拒：台账结论仍是已关闭（按动作时状态过滤，排除运行中的重复启动拒绝）
    restart_reject = [
        row for row in new_logs
        if row["操作对象"] == "EXPE-0002"
        and "启动实验" in row["操作类型"]
        and row["动作结论"] == "已拒绝"
        and row["动作时状态"] == "已关闭"
    ]
    check("已关闭重启被拒的台账保留已关闭结论", bool(restart_reject) and all(row["结论状态"] == "已关闭" for row in restart_reject))

    print("== 11. 台账复核流转（修正后单向） ==")
    target = new_logs[0]
    entry, msg = log_svc.run_action(target["id"], "归档记录")
    check("未复核直接归档被拒绝", entry is None and "已记录" in msg, msg)
    entry, msg = log_svc.run_action(target["id"], "标记复核")
    check("标记复核成功", entry is not None and entry["status"] == "已复核", msg)
    entry, msg = log_svc.run_action(target["id"], "标记复核")
    check("重复复核被拒绝", entry is None)
    entry, msg = log_svc.run_action(target["id"], "归档记录")
    check("复核后归档成功", entry is not None and entry["status"] == "已归档", msg)
    check("归档后不再待复核", entry is not None and entry["pending"] is False)

    print("== 12. 看板统计与列表同源、数量正确 ==")
    stats = svc.status_stats()
    rows = store.rows("experiment")
    check("统计总数与列表一致", stats["total"] == len(rows), str(stats))
    check("各状态计数之和等于总数", sum(stats[s] for s in ["草稿", "运行中", "已完成", "已关闭"]) == stats["total"], str(stats))
    check("运行中计数与实际一致", stats["运行中"] == sum(1 for r in rows if r["status"] == "运行中"))
    check("已完成计数与实际一致", stats["已完成"] == sum(1 for r in rows if r["status"] == "已完成"))
    check("已关闭计数与实际一致", stats["已关闭"] == sum(1 for r in rows if r["status"] == "已关闭"))

    print("== 13. 空态：按不存在编号过滤返回空页不报错 ==")
    items, total = svc.list_entries(keyword="NO-SUCH-EXPE")
    check("空结果返回空列表", items == [] and total == 0)

    print()
    if FAILURES:
        print(f"共 {len(FAILURES)} 项失败：")
        for item in FAILURES:
            print(" -", item)
        return 1
    print("全部断言通过。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        sys.exit(2)
