import { useCallback, useEffect, useState } from 'react'

import { getHealth } from './api'
import Ask from './views/Ask'
import Briefing from './views/Briefing'
import Carriers from './views/Carriers'
import ClientDetail from './views/ClientDetail'
import Clients from './views/Clients'
import Health from './views/Health'
import Review from './views/Review'
import Upload from './views/Upload'

// Only screens that exist. Dead links for unbuilt phases would be worse than
// a short nav — Phase 3 adds gap analysis, Phase 4 the drafting step.
const NAV = [
  { id: 'carriers', label: 'Carriers' },
  { id: 'clients', label: 'Clients' },
  { id: 'ask', label: 'Ask' },
  { id: 'review', label: 'Review' },
  { id: 'health', label: 'Settings' },
]

export default function App() {
  const [view, setView] = useState('carriers')
  // Bumped after an upload or delete so the list refetches instead of
  // showing stale data.
  const [refreshKey, setRefreshKey] = useState(0)
  const [openCarrierId, setOpenCarrierId] = useState(null)
  const [openClientId, setOpenClientId] = useState(null)
  const [health, setHealth] = useState(null)

  // A quiet status light in the sidebar, so a dead backend or a missing
  // Ollama is obvious from any screen rather than only on the settings page.
  const pollHealth = useCallback(async () => {
    try {
      const data = await getHealth()
      setHealth(data.status)
    } catch {
      setHealth('unreachable')
    }
  }, [])

  useEffect(() => {
    pollHealth()
    const timer = setInterval(pollHealth, 30000)
    return () => clearInterval(timer)
  }, [pollHealth])

  const healthColour =
    health === 'ok'
      ? 'var(--ok)'
      : health === 'degraded'
        ? 'var(--warn)'
        : health === 'unreachable'
          ? 'var(--bad)'
          : 'var(--ink-faint)'

  const healthLabel =
    health === 'ok'
      ? 'All services running'
      : health === 'degraded'
        ? 'Some services down'
        : health === 'unreachable'
          ? 'Backend unreachable'
          : 'Checking…'

  function openCarrier(carrierId) {
    setOpenCarrierId(carrierId)
    setView('briefing')
  }

  function openClient(clientId) {
    setOpenClientId(clientId)
    setView('client')
  }

  return (
    <div className="shell">
      <aside className="sidebar">
        <div>
          <h2 className="wordmark">
            Insurance<span> Intelligence</span>
          </h2>
          <p className="wordmark-sub">Carriers · Clients · Matching</p>
        </div>

        <button
          className="new-report"
          onClick={() => setView('upload')}
          aria-current={view === 'upload' ? 'page' : undefined}
        >
          + Add carrier
        </button>

        <nav className="nav">
          {NAV.map((item) => (
            <button
              key={item.id}
              onClick={() => setView(item.id)}
              aria-current={
                view === item.id ||
                (item.id === 'carriers' && view === 'briefing') ||
                (item.id === 'clients' && view === 'client')
                  ? 'page'
                  : undefined
              }
            >
              {item.label}
            </button>
          ))}
        </nav>

        <div className="sidebar-foot">
          <span className="dot" style={{ background: healthColour }} />
          {healthLabel}
        </div>
      </aside>

      <main className="main">
        {view === 'upload' && (
          <Upload
            onUploaded={() => {
              setRefreshKey((k) => k + 1)
              pollHealth()
            }}
            onOpen={openCarrier}
          />
        )}
        {view === 'carriers' && (
          <Carriers
            refreshKey={refreshKey}
            onAddCarrier={() => setView('upload')}
            onOpen={openCarrier}
          />
        )}
        {view === 'briefing' && openCarrierId && (
          <Briefing
            carrierId={openCarrierId}
            onBack={() => setView('carriers')}
            onDeleted={() => {
              setRefreshKey((k) => k + 1)
              setOpenCarrierId(null)
              setView('carriers')
            }}
          />
        )}
        {view === 'clients' && (
          <Clients
            refreshKey={refreshKey}
            onOpen={openClient}
            onCreated={() => setRefreshKey((k) => k + 1)}
          />
        )}
        {view === 'client' && openClientId && (
          <ClientDetail
            clientId={openClientId}
            onBack={() => setView('clients')}
            onDeleted={() => {
              setRefreshKey((k) => k + 1)
              setOpenClientId(null)
              setView('clients')
            }}
          />
        )}
        {view === 'ask' && <Ask />}
        {view === 'review' && <Review />}
        {view === 'health' && <Health />}
      </main>
    </div>
  )
}
