import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/experiment'
const columns = [
  "实验编号",
  "实验名称",
  "分析数据集",
  "分组字段",
  "目标度量",
  "基准区间",
  "显著阈值",
  "实验状态",
  "异常原因"
]
const statuses = ["草稿", "运行中", "已完成", "已关闭"] as const
type Status = (typeof statuses)[number]

type Row = Record<string, string | number | null> & {
  id: number
  status?: Status
  abnormal?: boolean
}

type Stats = {
  total: number
  草稿: number
  运行中: number
  已完成: number
  已关闭: number
}

type ActionResult = { ok: boolean; message: string; entry: Row | null }

// 动作可用性：与后端 ACTION_RULES 的前置状态保持一致。
const ACTION_GUARD: Record<string, (row: Row) => { enabled: boolean; hint: string }> = {
  启动实验: (row) =>
    row.status === '草稿'
      ? { enabled: true, hint: '' }
      : { enabled: false, hint: row.status === '已关闭' ? '已关闭实验不能重新启动' : `当前为${row.status ?? '未知'}状态，仅草稿可启动` },
  结束实验: (row) =>
    row.status === '运行中'
      ? { enabled: true, hint: '' }
      : { enabled: false, hint: row.status === '已关闭' ? '实验已关闭，不能结束' : `当前为${row.status ?? '未知'}状态，仅运行中可结束` },
  关闭实验: (row) =>
    row.status === '已完成'
      ? { enabled: true, hint: '' }
      : { enabled: false, hint: row.status === '运行中' ? '实验仍在运行中，请先结束实验' : `当前为${row.status ?? '未知'}状态，仅已完成可关闭` },
}

const EMPTY_STATS: Stats = { total: 0, 草稿: 0, 运行中: 0, 已完成: 0, 已关闭: 0 }

export default function ExperimentView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [stats, setStats] = useState<Stats>(EMPTY_STATS)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [submittingId, setSubmittingId] = useState<number | null>(null)
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [listResp, statsResp] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/stats`),
      ])
      if (!listResp.ok || !statsResp.ok) {
        throw new Error('分析实验列表读取失败')
      }
      const payload = await listResp.json()
      const statsPayload = (await statsResp.json()) as Stats
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
      setStats({ ...EMPTY_STATS, ...statsPayload })
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析实验管理列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    const guard = ACTION_GUARD[action]?.(row)
    if (guard && !guard.enabled) {
      setErrorMessage(guard.hint)
      return
    }
    // 重复点击/并发：同一实验有动作在途时直接拒绝后续点击。
    if (submittingId !== null) {
      setErrorMessage('上一个操作仍在处理中，请勿重复点击')
      return
    }
    let reason: string | undefined
    if (action === '标记异常') {
      const input = window.prompt('请输入异常原因（失败/中断说明）')
      if (input === null) return
      reason = input.trim()
      if (!reason) {
        setErrorMessage('标记异常必须填写异常原因')
        return
      }
    }
    setErrorMessage('')
    setSubmittingId(row.id)
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify(reason ? { action, reason } : { action }),
      })
      const payload = (await response.json()) as ActionResult
      if (!response.ok || !payload.ok) {
        // 后端对非法流转给出明确拒绝原因，直接展示给运营人员。
        throw new Error(payload.message || '分析实验管理动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析实验管理操作失败')
    } finally {
      setSubmittingId(null)
    }
  }

  async function createExperiment() {
    const code = window.prompt('实验编号（如 EXPE-0010）')?.trim()
    if (!code) return
    const name = window.prompt('实验名称')?.trim()
    if (!name) return
    const dataset = window.prompt('分析数据集')?.trim()
    if (!dataset) return
    setErrorMessage('')
    try {
      const response = await request(ENDPOINT, {
        method: 'POST',
        body: JSON.stringify({ values: { 实验编号: code, 实验名称: name, 分析数据集: dataset } }),
      })
      const payload = (await response.json()) as ActionResult
      if (!response.ok || !payload.ok) {
        throw new Error(payload.message || '分析实验登记失败')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '分析实验登记失败')
    }
  }

  const statCards = [
    { label: '分析实验总数', value: stats.total },
    { label: '运行中实验', value: stats.运行中 },
    { label: '已完成实验', value: stats.已完成 },
    { label: '已关闭实验', value: stats.已关闭 },
  ]

  return (
    <section className="page" data-module="experiment">
      <header className="page-head">
        <div>
          <h2>分析实验管理</h2>
          <p className="page-desc">维护分析实验的设计参数、分组方案与统计显著性校验，支持A/B实验分析。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => void createExperiment()}>
            登记分析实验
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出分析实验管理清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {statCards.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <label className="filter-item">
          <span>实验状态</span>
          <select
            value={filters.status ?? ''}
            onChange={(event) => setFilters((prev) => ({ ...prev, status: event.target.value }))}
          >
            <option value="">全部状态</option>
            {statuses.map((status) => <option key={status} value={status}>{status}</option>)}
          </select>
        </label>
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const status = (row.status ?? '') as Status
            const abnormal = Boolean(row.abnormal)
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => (
                  <td key={column}>
                    {column === '实验状态' ? (
                      <span className={`status-badge status-${status}`}>{status || '—'}</span>
                    ) : column === '异常原因' ? (
                      abnormal
                        ? <span className="abnormal-tag" title={String(row[column] ?? '')}>{String(row[column] || '实验异常')}</span>
                        : '—'
                    ) : (
                      (row[column] ?? '—')
                    )}
                  </td>
                ))}
                <td className="row-actions">
                  {(['启动实验', '结束实验', '关闭实验'] as const).map((action) => {
                    const guard = ACTION_GUARD[action](row)
                    const busy = submittingId === row.id
                    return (
                      <button
                        key={action}
                        className="link"
                        type="button"
                        disabled={!guard.enabled || busy}
                        title={guard.enabled ? (busy ? '操作处理中，请勿重复点击' : action) : guard.hint}
                        onClick={() => void runAction(action, row)}
                      >
                        {action}
                      </button>
                    )
                  })}
                  {status === '运行中' && (
                    abnormal ? (
                      <button
                        className="link"
                        type="button"
                        disabled={submittingId !== null}
                        title="从可重试状态恢复，清除异常标记并继续运行"
                        onClick={() => void runAction('恢复实验', row)}
                      >
                        恢复实验
                      </button>
                    ) : (
                      <button
                        className="link"
                        type="button"
                        disabled={submittingId !== null}
                        title="标记异常时必须填写异常原因"
                        onClick={() => void runAction('标记异常', row)}
                      >
                        标记异常
                      </button>
                    )
                  )}
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无分析实验数据，可先登记分析实验</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条分析实验记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
