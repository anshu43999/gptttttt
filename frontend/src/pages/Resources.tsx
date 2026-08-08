import { useEffect, useMemo, useState } from 'react'
import { RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { checkProxyHealth, deleteResourcesBulk, getResourceCategories, getResources, importResources, recoverStaleResources, setResourceStatus, setResourceStatusBulk } from '@/lib/api'
import { formatDate } from '@/lib/utils'
import type { ResourceCategoryOption, ResourceImportType, ResourceItem, ProxyHealthCheckResult } from '@/lib/types'

const TYPE_LABELS: Record<string, string> = {
  phone: '手机号池',
  proxy: '代理 Seed 池',
  email: '邮箱池',
  sms_activation: 'SMS 激活池',
}

const STATUS_LABELS: Record<string, string> = {
  available: '可用',
  leased: '租用中',
  used: '已使用',
  cooldown: '冷却中',
  disabled: '已禁用',
}

const STATUS_BADGE: Record<string, 'default' | 'success' | 'danger' | 'warning' | 'secondary'> = {
  available: 'success',
  leased: 'warning',
  used: 'secondary',
  cooldown: 'danger',
  disabled: 'secondary',
}

const IMPORT_PROVIDERS: Record<ResourceImportType, string> = {
  phone: 'user_phone_url',
  bind_phone: 'bind_user_phone_url',
  proxy: 'proxy_seed',
  email: 'outlook_token',
  icloud_email: 'icloud_api',
}

const IMPORT_HELP: Record<ResourceImportType, { title: string; placeholder: string; help: string }> = {
  phone: {
    title: '导入自备手机号 API',
    placeholder: '15555550100|https://sms.example.invalid/messages/placeholder',
    help: '每行一个：手机号|取码URL。',
  },
  bind_phone: {
    title: '导入绑定手机号 API',
    placeholder: '15555550101|https://sms.example.invalid/messages/placeholder',
    help: '仅用于 Plus 后 CPA/OAuth 绑定；不会被注册任务租用。每行一个：手机号|取码URL。',
  },
  proxy: {
    title: '导入代理 Seed',
    placeholder: 'account:password@us.proxy001.com:7878',
    help: '每行一条 seed（base 账号）。注册时按你选的地区自动拼 zone/region + 新 SID；网络错误会自动换 SID。不必再导入成百上千条 sticky 会话。',
  },
  email: {
    title: '导入 Outlook Token 邮箱',
    placeholder: 'mailbox@example.invalid----password----client_id----refresh_token',
    help: '每行一个邮箱账号，注册任务会从资源池租用。',
  },
  icloud_email: {
    title: '导入 iCloud API 邮箱',
    placeholder: 'mailbox@example.invalid----https://mail.example.invalid/inbox/placeholder',
    help: '每行一个 iCloud 邮箱订单：email----收信URL。',
  },
}

function displayResourceKey(item: ResourceItem) {
  if (item.resource_type === 'proxy') return item.resource_key.replace(/([^:@]{3})[^:@]*(:[^@]+@)/, '$1***$2')
  return item.resource_key
}

function exportResourceLine(item: ResourceItem) {
  if (item.resource_type === 'phone') {
    const smsUrl = String(item.payload?.sms_url ?? item.payload?.url ?? '').trim()
    return smsUrl ? `${item.resource_key}|${smsUrl}` : item.resource_key
  }
  if (item.provider === 'outlook_token') return `${String(item.payload?.email ?? item.resource_key)}----${String(item.payload?.password ?? '')}----${String(item.payload?.client_id ?? '')}----${String(item.payload?.refresh_token ?? '')}`
  if (item.provider === 'icloud_api') return `${String(item.payload?.email ?? item.resource_key)}----${String(item.payload?.inbox_url ?? '')}${item.payload?.code_url ? `----code:${String(item.payload.code_url)}` : ''}${item.payload?.mail_url ? `----mail:${String(item.payload.mail_url)}` : ''}`
  if (item.provider === 'icloud_privacy') return String(item.payload?.email ?? item.resource_key)
  return String(item.payload?.url ?? item.resource_key)
}

function selectedCountText(count: number) {
  return count > 0 ? `已选择 ${count} 条` : '未选择资源'
}

export default function Resources() {
  const [items, setItems] = useState<ResourceItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [type, setType] = useState('')
  const [status, setStatus] = useState('')
  const [provider, setProvider] = useState('')
  const [categories, setCategories] = useState<ResourceCategoryOption[]>([])
  const [busyId, setBusyId] = useState<number | null>(null)
  const [selectedIds, setSelectedIds] = useState<number[]>([])
  const [importType, setImportType] = useState<ResourceImportType>('phone')
  const [importText, setImportText] = useState('')
  const [importFileName, setImportFileName] = useState('')
  const [importRegion, setImportRegion] = useState('JP')
  const [importProtocol, setImportProtocol] = useState('socks5h')
  const [proxyCheck, setProxyCheck] = useState<ProxyHealthCheckResult | null>(null)
  const [bulkStatus, setBulkStatus] = useState('available')
  const [bulkMode, setBulkMode] = useState<'selected' | 'filter'>('selected')

  const load = async () => {
    setLoading(true)
    try {
      const nextItems = await getResources({ resource_type: type || undefined, provider: provider || undefined, status: status || undefined })
      setItems(nextItems)
      setSelectedIds((prev) => prev.filter((id) => nextItems.some((item) => item.id === id)))
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '加载失败')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    getResourceCategories().then(setCategories).catch(() => setCategories([]))
  }, [])

  useEffect(() => {
    load()
  }, [type, provider, status])

  const stats = useMemo(() => {
    const base = { total: items.length, available: 0, leased: 0, cooldown: 0, used: 0, disabled: 0 }
    for (const item of items) {
      if (item.status in base) base[item.status as keyof typeof base] += 1
    }
    return base
  }, [items])

  const allVisibleSelected = items.length > 0 && items.every((item) => selectedIds.includes(item.id))
  const selectedSet = useMemo(() => new Set(selectedIds), [selectedIds])

  const selectedCategory = categories.find((item) => item.resource_type === type && item.provider === provider)

  const selectCategory = (key: string) => {
    if (!key) {
      setType('')
      setProvider('')
      return
    }
    const category = categories.find((item) => item.key === key)
    setType(category?.resource_type ?? '')
    setProvider(category?.provider ?? '')
  }

  const update = async (item: ResourceItem, next: string) => {
    setBusyId(item.id)
    try {
      await setResourceStatus(item.id, next)
      setNotice(`已更新 1 条资源为${STATUS_LABELS[next] ?? next}`)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setBusyId(null)
    }
  }

  const recover = async () => {
    setBusyId(-1)
    try {
      const result = await recoverStaleResources(1800)
      setNotice(`已回收 ${result.recovered} 条过期租约`)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '回收失败')
    } finally {
      setBusyId(null)
    }
  }

  const toggleAll = (checked: boolean) => {
    setSelectedIds(checked ? items.map((item) => item.id) : [])
  }

  const toggleOne = (id: number, checked: boolean) => {
    setSelectedIds((prev) => checked ? Array.from(new Set([...prev, id])) : prev.filter((itemId) => itemId !== id))
  }

  const handleImportFile = async (file: File | null) => {
    if (!file) return
    setError(null)
    setImportFileName(file.name)
    // Browser FileReader path — no paste into controlled textarea for multi-MB files.
    const text = await file.text()
    setImportText(text)
  }

  const submitImport = async () => {
    if (!importText.trim()) {
      setError('请先填写要导入的资源，或选择文件')
      return
    }
    setBusyId(-2)
    setProxyCheck(null)
    try {
      const metadata = importType === 'proxy' ? { region: importRegion, protocol: importProtocol } : undefined
      const result = await importResources({
        resource_type: importType === 'bind_phone' ? 'phone' : (importType === 'icloud_email' ? 'email' : importType),
        provider: IMPORT_PROVIDERS[importType],
        text: importText,
        metadata,
      })
      setNotice(`已导入 ${result.count} 条${IMPORT_HELP[importType].title.replace('导入', '')}`)
      setImportText('')
      setImportFileName('')
      setError(null)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '导入失败')
    } finally {
      setBusyId(null)
    }
  }

  const validateProxy = async () => {
    if (!importText.trim()) {
      setError('请先填写代理')
      return
    }
    setBusyId(-3)
    try {
      const result = await checkProxyHealth(importText, false)
      setProxyCheck(result)
      setNotice(`已校验 ${result.checked} 条代理，${result.valid} 条格式有效`)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : '代理校验失败')
    } finally {
      setBusyId(null)
    }
  }

  const applyBulkStatus = async (next = bulkStatus, mode = bulkMode) => {
    if (mode === 'selected' && selectedIds.length === 0) {
      setError('请先选择资源，或切换为按当前筛选批量操作')
      return
    }
    setBusyId(-4)
    try {
      const result = await setResourceStatusBulk({
        status: next,
        resource_ids: mode === 'selected' ? selectedIds : undefined,
        resource_type: mode === 'filter' ? type : undefined,
        current_status: mode === 'filter' ? status : undefined,
        error: mode === 'filter' ? '按筛选批量操作' : '按选择批量操作',
      })
      setNotice(`已更新 ${result.count} 条资源为${STATUS_LABELS[next] ?? next}`)
      setSelectedIds([])
      setError(null)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '批量操作失败')
    } finally {
      setBusyId(null)
    }
  }

  const deleteBulk = async (mode = bulkMode) => {
    if (mode === 'selected' && selectedIds.length === 0) {
      setError('请先选择资源，或切换为按当前筛选批量删除')
      return
    }
    const targetText = mode === 'selected' ? `${selectedIds.length} 条选中资源` : '当前筛选结果'
    if (!window.confirm(`确认从数据库永久删除${targetText}？此操作不可撤销。`)) return
    setBusyId(-5)
    try {
      const result = await deleteResourcesBulk({
        resource_ids: mode === 'selected' ? selectedIds : undefined,
        resource_type: mode === 'filter' ? type : undefined,
        provider: mode === 'filter' ? provider : undefined,
        current_status: mode === 'filter' ? status : undefined,
      })
      setNotice(`已从数据库删除 ${result.count} 条资源`)
      setSelectedIds([])
      setError(null)
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : '批量删除失败')
    } finally {
      setBusyId(null)
    }
  }

  const exportTxt = () => {
    const targetItems = selectedIds.length > 0 ? items.filter((item) => selectedSet.has(item.id)) : items
    if (targetItems.length === 0) {
      setError('当前没有可导出的资源')
      return
    }
    const text = `${targetItems.map(exportResourceLine).join('\n')}\n`
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    const scope = selectedIds.length > 0 ? 'selected' : (selectedCategory?.label ?? 'all_resources')
    link.href = url
    link.download = `${scope.replace(/[\\/:*?"<>|\s]+/g, '_').replace(/^_+|_+$/g, '') || 'resources'}_${status || 'all'}.txt`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    setError(null)
    setNotice(`已导出 ${targetItems.length} 条资源到 TXT`)
  }

  const importConfig = IMPORT_HELP[importType]

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold text-zinc-100">资源池</h2>
          <p className="mt-1 text-sm text-zinc-400">统一管理手机号、代理和邮箱资源的导入、租用、冷却和禁用状态。</p>
        </div>
        <Button variant="outline" onClick={recover} disabled={busyId === -1}>
          <RefreshCw className="h-4 w-4" />
          回收过期租约
        </Button>
      </div>

      {error && <div className="rounded-md border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-400">{error}</div>}
      {notice && <div className="rounded-md border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-400">{notice}</div>}

      <div className="grid gap-3 sm:grid-cols-6">
        <div className="rounded-xl border border-white/5 bg-white/[0.03] p-4"><p className="text-xs text-zinc-500">总数</p><b className="text-2xl text-zinc-100">{stats.total}</b></div>
        <div className="rounded-xl border border-white/5 bg-white/[0.03] p-4"><p className="text-xs text-zinc-500">可用</p><b className="text-2xl text-emerald-400">{stats.available}</b></div>
        <div className="rounded-xl border border-white/5 bg-white/[0.03] p-4"><p className="text-xs text-zinc-500">租用中</p><b className="text-2xl text-amber-400">{stats.leased}</b></div>
        <div className="rounded-xl border border-white/5 bg-white/[0.03] p-4"><p className="text-xs text-zinc-500">冷却中</p><b className="text-2xl text-red-400">{stats.cooldown}</b></div>
        <div className="rounded-xl border border-white/5 bg-white/[0.03] p-4"><p className="text-xs text-zinc-500">已使用</p><b className="text-2xl text-zinc-400">{stats.used}</b></div>
        <div className="rounded-xl border border-white/5 bg-white/[0.03] p-4"><p className="text-xs text-zinc-500">已禁用</p><b className="text-2xl text-zinc-400">{stats.disabled}</b></div>
      </div>

      <div className="rounded-xl border border-white/5 bg-white/[0.03] p-4">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="font-medium text-zinc-100">直接导入资源</h3>
            <p className="text-xs text-zinc-500">{importConfig.help}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <select className="rounded-md border border-white/10 bg-zinc-900 px-3 py-2 text-sm" value={importType} onChange={(e) => { setImportType(e.target.value as ResourceImportType); setProxyCheck(null) }}>
              <option value="phone">注册手机号 API</option>
              <option value="bind_phone">绑定手机号 API</option>
              <option value="proxy">账密代理</option>
              <option value="email">Outlook Token 邮箱</option>
              <option value="icloud_email">iCloud API 邮箱</option>
            </select>
            {importType === 'proxy' && (
              <>
                <input className="w-20 rounded-md border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-200" value={importRegion} onChange={(e) => setImportRegion(e.target.value)} placeholder="地区" />
                <input className="w-28 rounded-md border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-200" value={importProtocol} onChange={(e) => setImportProtocol(e.target.value)} placeholder="协议" />
              </>
            )}
          </div>
        </div>
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <label className="inline-flex cursor-pointer items-center gap-2 rounded-md border border-white/10 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 hover:bg-zinc-800">
            选择文件
            <input
              type="file"
              accept=".txt,.csv,text/plain"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0] ?? null
                void handleImportFile(file)
                event.target.value = ''
              }}
            />
          </label>
          {importFileName && (
            <span className="text-xs text-zinc-400">
              已选：{importFileName}（{importText.split(/\r?\n/).filter(Boolean).length} 行）
            </span>
          )}
          <span className="text-xs text-zinc-500">大批量请选文件，勿粘贴</span>
        </div>
        <textarea
          value={importText}
          onChange={(e) => setImportText(e.target.value)}
          placeholder={importConfig.placeholder}
          rows={6}
          className="w-full rounded-md border border-white/10 bg-zinc-900 px-3 py-2 font-mono text-sm text-zinc-200 placeholder:text-zinc-600 focus:outline-none focus:ring-1 focus:ring-blue-600"
          aria-label={importConfig.title}
        />
        <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
          <div className="text-xs text-zinc-500">
            导入到：{TYPE_LABELS[importType === 'bind_phone' ? 'phone' : (importType === 'icloud_email' ? 'email' : importType)]} / {IMPORT_PROVIDERS[importType]}
          </div>
          <div className="flex gap-2">
            {importType === 'proxy' && (
              <Button variant="outline" onClick={validateProxy} disabled={busyId === -3}>
                只校验格式
              </Button>
            )}
            <Button onClick={submitImport} disabled={busyId === -2 || !importText.trim()}>
              {busyId === -2 ? '导入中…' : '批量导入'}
            </Button>
          </div>
        </div>
        {proxyCheck && (
          <div className="mt-3 rounded-md border border-white/5 bg-black/20 p-3 text-xs text-zinc-400">
            <p className="mb-2 text-zinc-300">代理校验：{proxyCheck.valid}/{proxyCheck.checked} 条格式有效，未连接外部代理。</p>
            <div className="space-y-1">
              {proxyCheck.items.slice(0, 5).map((item) => (
                <p key={item.proxy} className={item.ok ? 'text-emerald-400' : 'text-red-400'}>
                  {item.ok ? '✓' : '✗'} {item.message}
                </p>
              ))}
            </div>
          </div>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <select className="rounded-md border border-white/10 bg-zinc-900 px-3 py-2 text-sm" value={selectedCategory?.key ?? ''} onChange={(e) => selectCategory(e.target.value)}>
          <option value="">全部资源池</option>
          {categories.map((category) => (
            <option key={category.key} value={category.key}>{category.label}（可用 {category.available} / 总 {category.total}）</option>
          ))}
        </select>
        <select className="rounded-md border border-white/10 bg-zinc-900 px-3 py-2 text-sm" value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">全部状态</option>
          <option value="available">可用</option>
          <option value="leased">租用中</option>
          <option value="used">已使用</option>
          <option value="cooldown">冷却中</option>
          <option value="disabled">已禁用</option>
        </select>
        <Button variant="outline" onClick={exportTxt} disabled={items.length === 0}>导出为 TXT</Button>
        <Button variant="ghost" onClick={load}>刷新</Button>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <span className="text-xs text-zinc-500">{selectedCountText(selectedIds.length)}</span>
          <select className="rounded-md border border-white/10 bg-zinc-900 px-3 py-2 text-sm" value={bulkMode} onChange={(e) => setBulkMode(e.target.value as 'selected' | 'filter')}>
            <option value="selected">按选择</option>
            <option value="filter">按当前筛选</option>
          </select>
          <select className="rounded-md border border-white/10 bg-zinc-900 px-3 py-2 text-sm" value={bulkStatus} onChange={(e) => setBulkStatus(e.target.value)}>
            <option value="available">释放为可用</option>
            <option value="cooldown">置为冷却</option>
            <option value="disabled">禁用</option>
          </select>
          <Button variant="outline" onClick={() => applyBulkStatus()} disabled={busyId === -4}>批量执行</Button>
          <Button variant="destructive" onClick={() => deleteBulk()} disabled={busyId === -5}>批量删除</Button>
        </div>
      </div>

      <div className="overflow-x-auto rounded-xl border border-white/5 bg-white/[0.03]">
        {loading ? (
          <p className="py-12 text-center text-sm text-zinc-500">正在加载资源…</p>
        ) : items.length === 0 ? (
          <p className="py-12 text-center text-sm text-zinc-500">暂无资源。可以在本页直接导入手机号、代理或邮箱。</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead className="border-b border-white/5 text-xs uppercase text-zinc-500">
              <tr>
                <th className="px-4 py-3 font-medium">
                  <input type="checkbox" checked={allVisibleSelected} onChange={(e) => toggleAll(e.target.checked)} className="rounded border-white/10 bg-zinc-900 accent-blue-600" aria-label="选择全部可见资源" />
                </th>
                <th className="px-4 py-3 font-medium">类型</th>
                <th className="px-4 py-3 font-medium">服务商</th>
                <th className="px-4 py-3 font-medium">资源</th>
                <th className="px-4 py-3 font-medium">状态</th>
                <th className="px-4 py-3 font-medium">租约</th>
                <th className="px-4 py-3 font-medium">成功/失败</th>
                <th className="px-4 py-3 font-medium">冷却到</th>
                <th className="px-4 py-3 font-medium text-right">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-white/5">
              {items.map((item) => (
                <tr key={item.id} className="hover:bg-white/[0.02]">
                  <td className="px-4 py-3">
                    <input type="checkbox" checked={selectedSet.has(item.id)} onChange={(e) => toggleOne(item.id, e.target.checked)} className="rounded border-white/10 bg-zinc-900 accent-blue-600" aria-label={`选择资源 ${item.id}`} />
                  </td>
                  <td className="px-4 py-3">{TYPE_LABELS[item.resource_type] ?? item.resource_type}</td>
                  <td className="px-4 py-3 text-zinc-400">{item.provider}</td>
                  <td className="whitespace-nowrap px-4 py-3 font-mono text-xs text-zinc-300" title={item.resource_key}>{displayResourceKey(item)}</td>
                  <td className="px-4 py-3"><Badge variant={STATUS_BADGE[item.status] ?? 'default'}>{STATUS_LABELS[item.status] ?? item.status}</Badge></td>
                  <td className="px-4 py-3 text-xs text-zinc-500">{item.lease_id || '—'}</td>
                  <td className="px-4 py-3 text-xs text-zinc-400">{item.success_count}/{item.fail_count}</td>
                  <td className="px-4 py-3 text-xs text-zinc-500">{formatDate(item.cooldown_until)}</td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-2">
                      <Button variant="ghost" size="sm" disabled={busyId === item.id} onClick={() => update(item, 'available')}>释放</Button>
                      <Button variant="ghost" size="sm" disabled={busyId === item.id} onClick={() => update(item, 'cooldown')}>冷却</Button>
                      <Button variant="ghost" size="sm" disabled={busyId === item.id} onClick={() => update(item, 'disabled')}>禁用</Button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}
