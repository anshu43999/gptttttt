import { Wifi } from "lucide-react"
import {
  ProviderCardBase,
  type ProviderCardBaseProps,
} from "@/components/providers/ProviderCardBase"
import { providerFieldLabel } from "@/components/providers/fieldLabels"

export interface ProxyProviderCardProps
  extends Omit<ProviderCardBaseProps, "icon"> {}

export function ProxyProviderCard(props: ProxyProviderCardProps) {
  const { provider } = props
  const settings = provider.settings || {}

  return (
    <ProviderCardBase {...props} icon={<Wifi size={18} />}>
      {Object.keys(settings).length > 0 && (
        <dl className="grid grid-cols-2 gap-x-4 gap-y-1">
          {Object.entries(settings).map(([key, value]) => (
            <div key={key} className="overflow-hidden">
              <dt className="truncate text-[10px] font-medium uppercase text-zinc-500">
                {providerFieldLabel(key)}
              </dt>
              <dd className="truncate text-xs text-zinc-300">
                {String(value)}
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
