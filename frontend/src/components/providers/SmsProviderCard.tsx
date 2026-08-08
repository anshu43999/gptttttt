import { Smartphone } from "lucide-react"
import {
  ProviderCardBase,
  type ProviderCardBaseProps,
} from "@/components/providers/ProviderCardBase"
import { providerFieldLabel } from "@/components/providers/fieldLabels"

export interface SmsProviderCardProps
  extends Omit<ProviderCardBaseProps, "icon"> {}


function safeValue(key: string, value: unknown): string {
  const text = Array.isArray(value) ? `${value.length} 条` : String(value ?? '')
  if (/key|token|secret|pass/i.test(key)) return text ? '已保存' : ''
  if (text.length > 28) return `${text.slice(0, 28)}…`
  return text
}
export function SmsProviderCard(props: SmsProviderCardProps) {
  const { provider } = props
  const settings = provider.settings || {}
  if (provider.provider_name === 'user_phone_url' || provider.provider_name === 'bind_user_phone_url') {
    return (
      <ProviderCardBase {...props} icon={<Smartphone size={18} />}>
        <div className="rounded-lg border border-white/10 bg-zinc-950/70 p-3">
          <p className="text-xs font-medium text-zinc-300">SQLite 资源池管理</p>
          <p className="mt-1 text-xs text-zinc-500">点击配置可批量导入/导出手机号和取码 URL。</p>
        </div>
      </ProviderCardBase>
    )
  }

  return (
    <ProviderCardBase {...props} icon={<Smartphone size={18} />}>
      {Object.keys(settings).length > 0 && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
          {Object.entries(settings).map(([key, value]) => (
            <div key={key} className="overflow-hidden">
              <dt className="truncate text-[10px] font-medium uppercase text-zinc-500">
                {providerFieldLabel(key)}
              </dt>
              <dd className="truncate text-xs text-zinc-300">
                {safeValue(key, value)}
              </dd>
            </div>
          ))}
        </dl>
      )}
      {Object.keys(settings).length === 0 && (
        <p className="text-xs text-zinc-600">未配置参数</p>
      )}
    </ProviderCardBase>
  )
}
