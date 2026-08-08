import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}


class ApiRequestError extends Error {
  status: number
  detail: unknown

  constructor(status: number, detail: unknown) {
    const msg = typeof detail === 'string' ? detail : typeof detail === 'object' && detail !== null && 'message' in detail ? String((detail as { message?: unknown }).message) : `API 请求失败，状态码 ${status}`
    super(msg)
    this.name = 'ApiRequestError'
    this.status = status
    this.detail = detail
  }
}

export { ApiRequestError }

export async function apiFetch<T>(
  path: string,
  options?: RequestInit,
): Promise<T> {
  const url = `/api${path}`
  const res = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
    ...options,
  })

  if (!res.ok) {
    const text = await res.text()
    let detail: unknown = text
    try {
      detail = text ? JSON.parse(text) : ''
    } catch {
      detail = text
    }
    throw new ApiRequestError(res.status, detail)
  }

  if (res.status === 204) {
    return undefined as T
  }

  return res.json()
}


export function formatDate(value?: string): string {
  if (!value) return '—'
  return value.replace('T', ' ').slice(0, 19)
}

export function formatRelative(value?: string): string {
  if (!value) return '—'
  const then = new Date(value).getTime()
  const now = Date.now()
  const diff = now - then
  const seconds = Math.floor(diff / 1000)
  if (seconds < 60) return `${seconds} 秒前`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  const days = Math.floor(hours / 24)
  return `${days} 天前`
}
