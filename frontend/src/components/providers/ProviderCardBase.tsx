import type { ReactNode } from "react"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Play, Settings2 } from "lucide-react"
import type { ProviderInfo } from "@/lib/types"

export interface ProviderCardBaseProps {
  provider: ProviderInfo
  icon: ReactNode
  onTest?: (name: string) => void
  onConfigure?: (provider: ProviderInfo) => void
  children?: ReactNode
}

export function ProviderCardBase({
  provider,
  icon,
  onTest,
  onConfigure,
  children,
}: ProviderCardBaseProps) {
  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
        <div className="flex items-center gap-2">
          <span className="text-zinc-400">{icon}</span>
          <CardTitle className="text-sm">{provider.definition?.label ?? provider.provider_name}</CardTitle>
        </div>
        <Badge variant={provider.enabled ? "success" : "secondary"}>
          {provider.enabled ? "已启用" : "已停用"}
        </Badge>
      </CardHeader>
      <CardContent>
        <p className="mb-1 text-xs text-zinc-500">
          {provider.provider_type}/{provider.provider_name}
        </p>
        {provider.definition?.help && (
          <p className="mb-3 text-xs text-zinc-500">{provider.definition.help}</p>
        )}
        {children}
        <div className="mt-2 grid grid-cols-2 gap-2">
          <Button
            variant="ghost"
            size="sm"
            className="w-full"
            onClick={() => onConfigure?.(provider)}
          >
            <Settings2 size={14} />
            配置
          </Button>
          {onTest && (
            <Button
              variant="ghost"
              size="sm"
              className="w-full"
              onClick={() => onTest(provider.provider_name)}
            >
              <Play size={14} />
              测试
            </Button>
          )}
        </div>
      </CardContent>
    </Card>
  )
}
