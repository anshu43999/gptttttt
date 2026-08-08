import { lazy, Suspense } from 'react'
import { BrowserRouter, Routes, Route, NavLink } from 'react-router-dom'
import {
  LayoutDashboard,
  Rocket,
  Users,
  Link2,
  ListTodo,
  Settings2,
  Server,
  Database,
  Activity,
  Archive,
} from 'lucide-react'
const Dashboard = lazy(() => import('@/pages/Dashboard'))
const Register = lazy(() => import('@/pages/Register'))
const Binding = lazy(() => import('@/pages/Binding'))
const Accounts = lazy(() => import('@/pages/Accounts'))
const Tasks = lazy(() => import('@/pages/Tasks'))
const Providers = lazy(() => import('@/pages/Providers'))
const Settings = lazy(() => import('@/pages/Settings'))
const Resources = lazy(() => import('@/pages/Resources'))
const PlusProgress = lazy(() => import('@/pages/PlusProgress'))
const ArchiveBatches = lazy(() => import('@/pages/ArchiveBatches'))

const navItems = [
  { to: '/', icon: LayoutDashboard, label: '概览' },
  { to: '/register', icon: Rocket, label: '注册' },
  { to: '/binding', icon: Link2, label: '绑定' },
  { to: '/accounts', icon: Users, label: '账号' },
  { to: '/archive-batches', icon: Archive, label: '归档批次' },
  { to: '/tasks', icon: ListTodo, label: '任务' },
  { to: '/plus-progress', icon: Activity, label: 'Plus 进度' },
  { to: '/providers', icon: Server, label: '服务商' },
  { to: '/resources', icon: Database, label: '资源池' },
  { to: '/settings', icon: Settings2, label: '设置' },
] as const

function Sidebar() {
  return (
    <aside className="flex h-screen w-60 shrink-0 flex-col border-r border-white/5 bg-zinc-950/95">
      <div className="flex items-center gap-3 px-5 py-5">
        <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-blue-600 text-sm font-bold text-white shadow-lg shadow-blue-600/20">GR</div>
        <div className="min-w-0">
          <h1 className="truncate text-sm font-semibold text-zinc-100">GPT Register</h1>
          <p className="truncate text-[11px] text-zinc-500">账号自动化面板</p>
        </div>
      </div>
      <nav className="flex-1 space-y-1 px-3">
        {navItems.map(({ to, icon: Icon, label }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/'}
            className={({ isActive }) =>
              `flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${isActive ? 'bg-blue-600/10 text-blue-400' : 'text-zinc-400 hover:bg-white/5 hover:text-zinc-200'}`
            }
          >
            <Icon size={18} />
            <span>{label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="m-4 rounded-xl border border-white/5 bg-white/[0.03] p-3">
        <b className="text-xs text-zinc-300">API 状态</b>
        <p className="mt-1 text-xs text-zinc-500">后端: {typeof window !== 'undefined' ? window.location.host : 'localhost'}</p>
      </div>
    </aside>
  )
}

function PageLoader() {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="text-zinc-500">加载中…</div>
    </div>
  )
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="flex h-screen bg-zinc-950 text-zinc-200">
        <Sidebar />
        <main className="min-w-0 flex-1 overflow-auto">
          <Suspense fallback={<PageLoader />}>
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/register" element={<Register />} />
              <Route path="/binding" element={<Binding />} />
              <Route path="/accounts" element={<Accounts />} />
              <Route path="/archive-batches" element={<ArchiveBatches />} />
              <Route path="/tasks" element={<Tasks />} />
              <Route path="/plus-progress" element={<PlusProgress />} />
              <Route path="/providers" element={<Providers />} />
              <Route path="/settings" element={<Settings />} />
              <Route path="/resources" element={<Resources />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </BrowserRouter>
  )
}
