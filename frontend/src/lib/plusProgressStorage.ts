import type { PlusVerificationProgress } from './types'

export const PLUS_PROGRESS_STORAGE_KEY = 'gpt-register.plusVerificationTasks.v1'

export interface StoredPlusVerificationTask {
  task_id: string
  label: string
  created_at: string
  updated_at: string
  total: number
  completed: number
  paid: number
  failed: number
  running: boolean
  cancelled: boolean
  message?: string
}

function storage(): Storage | null {
  if (typeof window === 'undefined') return null
  return window.localStorage
}

function sanitizeTask(item: Partial<StoredPlusVerificationTask>): StoredPlusVerificationTask | null {
  const taskId = String(item.task_id || '').trim()
  if (!taskId) return null
  const now = new Date().toISOString()
  return {
    task_id: taskId,
    label: String(item.label || taskId),
    created_at: String(item.created_at || now),
    updated_at: String(item.updated_at || now),
    total: Math.max(0, Number(item.total || 0)),
    completed: Math.max(0, Number(item.completed || 0)),
    paid: Math.max(0, Number(item.paid || 0)),
    failed: Math.max(0, Number(item.failed || 0)),
    running: Boolean(item.running),
    cancelled: Boolean(item.cancelled),
    message: item.message ? String(item.message) : undefined,
  }
}

export function readStoredPlusVerificationTasks(): StoredPlusVerificationTask[] {
  const store = storage()
  if (!store) return []
  try {
    const parsed = JSON.parse(store.getItem(PLUS_PROGRESS_STORAGE_KEY) || '[]')
    if (!Array.isArray(parsed)) return []
    return parsed
      .map((item) => sanitizeTask(item as Partial<StoredPlusVerificationTask>))
      .filter((item): item is StoredPlusVerificationTask => Boolean(item))
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
  } catch {
    return []
  }
}

function writeStoredPlusVerificationTasks(tasks: StoredPlusVerificationTask[]) {
  const store = storage()
  if (!store) return
  store.setItem(PLUS_PROGRESS_STORAGE_KEY, JSON.stringify(tasks.slice(0, 20)))
}

export function rememberPlusVerificationTask(progress: PlusVerificationProgress, label?: string): StoredPlusVerificationTask {
  const existing = readStoredPlusVerificationTasks()
  const previous = existing.find((item) => item.task_id === progress.task_id)
  const now = new Date().toISOString()
  const next: StoredPlusVerificationTask = {
    task_id: progress.task_id,
    label: label || previous?.label || `Plus 校验 ${progress.task_id}`,
    created_at: previous?.created_at || now,
    updated_at: now,
    total: Math.max(0, Number(progress.total || 0)),
    completed: Math.max(0, Number(progress.completed || 0)),
    paid: Math.max(0, Number(progress.paid || 0)),
    failed: Math.max(0, Number(progress.failed || 0)),
    running: Boolean(progress.running),
    cancelled: Boolean(progress.cancelled),
    message: progress.message,
  }
  writeStoredPlusVerificationTasks([next, ...existing.filter((item) => item.task_id !== progress.task_id)])
  return next
}

export function rememberPlusVerificationTaskId(taskId: string, label?: string): StoredPlusVerificationTask | null {
  const sanitized = sanitizeTask({
    task_id: taskId,
    label: label || `Plus 校验 ${taskId}`,
    running: true,
  })
  if (!sanitized) return null
  const existing = readStoredPlusVerificationTasks()
  writeStoredPlusVerificationTasks([sanitized, ...existing.filter((item) => item.task_id !== sanitized.task_id)])
  return sanitized
}

export function forgetPlusVerificationTask(taskId: string) {
  writeStoredPlusVerificationTasks(readStoredPlusVerificationTasks().filter((item) => item.task_id !== taskId))
}
