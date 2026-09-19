import { useEffect, useState } from 'react'

import { getHealth, getInsights, listCarriers, listClients } from '../api'

/**
 * The landing page.
 *
 * Every other view answers a question you already knew you had. This one is
 * for opening the tool with no particular question — so it leads with the
 * things that are actually waiting for you (a carrier with no briefing, a
 * client with no match) rather than a wall of totals nobody acts on.
 */
export default function Home({ onNavigate, onAddCarrier }) {
  const [stats, setStats] = useState(null)
  const [carriers, setCarriers] = useState([])
  const [clients, setClients] = useState([])
  const [health, setHealth] = useState(null)

  useEffect(() => {
    getInsights().then(setStats).catch(() => setStats(null))
    listCarriers().then((d) => setCarriers(d.carriers || [])).catch(() => {})
    listClients().then((d) => setClients(d.clients || [])).catch(() => {})
    getHealth().then(setHealth).catch(() => setHealth(null))
  }, [])

  const unbriefed = carriers.filter((c) => !c.has_summary)
  const unmatched = clients.filter((c) => !c.last_matched_at)
  const down =
    health && Object.entries(health.services || {}).filter(
      ([, s]) => s.status !== 'connected',
    )

  // Shown only when something is genuinely waiting. An empty "nothing to do"
  // panel is noise; its absence says the same thing more quietly.
  const todo = [
    ...(down?.length
      ? [{
          key: 'services',
          urgent: true,
          text: `${down.length} service${down.length === 1 ? '' : 's'} not running — briefing and matching will fail`,
          action: 'Open settings',
          go: 'health',
        }]
      : []),
    ...(carriers.length === 0
      ? [{
          key: 'nocarriers',
          text: 'No carriers yet. Add an insurer\u2019s policy wordings to begin.',
          action: 'Add a carrier',
          go: '__add',
        }]
      : []),
    ...(unbriefed.length
      ? [{
          key: 'unbriefed',
          text: `${unbriefed.length} carrier${unbriefed.length === 1 ? '' : 's'} uploaded but not briefed — ${unbriefed[0].name}${unbriefed.length > 1 ? ' and others' : ''}`,
          action: 'Go to carriers',
          go: 'carriers',
        }]
      : []),
    ...(unmatched.length
      ? [{
          key: 'unmatched',
          text: `${unmatched.length} client${unmatched.length === 1 ? '' : 's'} with no cover matched yet`,
          action: 'Go to clients',
          go: 'clients',
        }]
      : []),
  ]

  const cards = [
    {
      id: 'carriers',
      eyebrow: 'Step one',
      title: 'Carriers',
      body: 'Upload an insurer\u2019s wordings and let the tool read out what they sell, require and exclude.',
      count: stats?.carriers_briefed,
      unit: 'briefed',
    },
    {
      id: 'clients',
      eyebrow: 'Step two',
      title: 'Clients',
      body: 'Take a client\u2019s situation, match it against every carrier at once, then check their paperwork.',
      count: stats?.clients,
      unit: 'on file',
    },
    {
      id: 'ask',
      eyebrow: 'Anytime',
      title: 'Ask the library',
      body: 'Questions answered from your carrier wordings only, with the section and page each answer came from.',
      count: null,
    },
    {
      id: 'review',
      eyebrow: 'Over time',
      title: 'Review',
      body: 'What the tool is getting right and wrong, and the writing habits it has picked up from your edits.',
      count: stats?.drafts,
      unit: 'drafted',
    },
  ]

  return (
    <>
      <div className="home-hero">
        <p className="eyebrow">Insurance Intelligence</p>
        <h1 className="home-title">
          Read the wordings. Match the risk.
          <br />
          Draft the paperwork.
        </h1>
        <p className="home-sub">
          Everything runs on this machine. Nothing is sent to an insurer
          without you reading it first.
        </p>
      </div>

      {todo.length > 0 && (
        <div className="todo">
          <div className="todo-head">Waiting on you</div>
          {todo.map((item) => (
            <div className={`todo-row ${item.urgent ? 'is-urgent' : ''}`} key={item.key}>
              <span className="todo-dot" />
              <span className="todo-text">{item.text}</span>
              <button
                className="todo-go"
                onClick={() =>
                  item.go === '__add' ? onAddCarrier() : onNavigate(item.go)
                }
              >
                {item.action} →
              </button>
            </div>
          ))}
        </div>
      )}

      <div className="home-grid">
        {cards.map((card) => (
          <button
            key={card.id}
            className="home-card"
            onClick={() => onNavigate(card.id)}
          >
            <span className="home-card-eyebrow">{card.eyebrow}</span>
            <span className="home-card-title">{card.title}</span>
            <span className="home-card-body">{card.body}</span>
            <span className="home-card-foot">
              {card.count != null ? (
                <>
                  <strong>{card.count}</strong> {card.unit}
                </>
              ) : (
                <span />
              )}
              <span className="home-card-arrow">→</span>
            </span>
          </button>
        ))}
      </div>

      {stats && (
        <div className="home-strip">
          {[
            [stats.matches_run, 'matches run'],
            [stats.gap_reports, 'paperwork checks'],
            [stats.drafts_approved, 'documents signed off'],
            [
              (stats.verdicts?.mismatch || 0) +
                (stats.verdicts?.wrong_document || 0),
              'bad documents caught',
            ],
          ].map(([value, label]) => (
            <div className="home-stat" key={label}>
              <span className="home-stat-value">{value ?? 0}</span>
              <span className="home-stat-label">{label}</span>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
