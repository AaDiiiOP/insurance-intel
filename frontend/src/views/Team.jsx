import { useEffect, useState } from 'react'

import {
  changePassword,
  inviteMember,
  issuePasswordReset,
  listAuditEvents,
  listInvitations,
  listMembers,
  revokeInvitation,
  updateMember,
} from '../api'

const ROLES = ['OWNER', 'ADMIN', 'BROKER', 'REVIEWER']
const ROLE_HELP = {
  OWNER: 'everything, including other Owners',
  ADMIN: 'everything except the Owner role',
  BROKER: 'clients, documents, analysis, drafts, export',
  REVIEWER: 'verify/correct facts, review findings, approve drafts, export',
}

/** Phase 2A: organization members, invitations, password resets, audit history,
 * and your own password. Server-side permissions decide what actually works. */
export default function Team({ me }) {
  const can = (p) => me.permissions.includes(p)
  const [members, setMembers] = useState([])
  const [invites, setInvites] = useState([])
  const [events, setEvents] = useState([])
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('BROKER')
  const [oneTime, setOneTime] = useState(null)
  const [error, setError] = useState(null)
  const [pw, setPw] = useState({ current: '', next: '' })
  const [pwNote, setPwNote] = useState(null)

  function load() {
    setError(null)
    if (can('users.manage')) {
      listMembers().then((d) => setMembers(d.members)).catch((e) => setError(e.message))
      listInvitations().then((d) => setInvites(d.invitations)).catch(() => {})
    }
    if (can('audit.view')) listAuditEvents({ limit: 50 }).then((d) => setEvents(d.events)).catch(() => {})
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(load, [me.organization.organization_id])

  async function run(fn) {
    setError(null)
    try {
      await fn()
      load()
    } catch (e) {
      setError(e.message)
    }
  }

  return (
    <div>
      <header className="page-head">
        <p className="eyebrow">{me.organization.name}</p>
        <h1>Team &amp; security</h1>
        <p className="subtitle">Signed in as {me.user.display_name} ({me.user.email}) · role {me.role}</p>
      </header>
      {error && <div className="banner bad">{error}</div>}
      {oneTime && (
        <div className="banner ok">
          <b>{oneTime.label}</b> — shown once, give it to the person directly:{' '}
          <code style={{ wordBreak: 'break-all' }}>{oneTime.token}</code>
          <div className="kx-meta">Expires {oneTime.expires_at}. It works once.</div>
          <button className="btn-ghost kx-small" onClick={() => setOneTime(null)}>Done</button>
        </div>
      )}

      {can('users.manage') && (
        <div className="section">
          <div className="section-head"><h2>Members</h2></div>
          <table className="team-table">
            <thead><tr><th>Name</th><th>E-mail</th><th>Role</th><th>Status</th><th /></tr></thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.user_id}>
                  <td>{m.display_name}</td>
                  <td>{m.email}</td>
                  <td>
                    <select value={m.role} onChange={(e) => run(() => updateMember(m.user_id, { role: e.target.value }))}>
                      {ROLES.map((r) => <option key={r} value={r}>{r}</option>)}
                    </select>
                  </td>
                  <td>
                    <button className="btn-ghost kx-small"
                      onClick={() => run(() => updateMember(m.user_id, { status: m.status === 'active' ? 'suspended' : 'active' }))}>
                      {m.status === 'active' ? 'Suspend' : 'Reactivate'}
                    </button>
                  </td>
                  <td>
                    <button className="btn-ghost kx-small" onClick={() => run(async () => {
                      const r = await issuePasswordReset(m.user_id)
                      setOneTime({ label: `Reset code for ${m.email}`, token: r.token, expires_at: r.expires_at })
                    })}>Reset code</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 style={{ marginTop: 16 }}>Invite someone</h3>
          <div className="actions" style={{ marginTop: 6 }}>
            <input className="approve-note" style={{ maxWidth: 260 }} type="email" placeholder="name@firm.in"
              value={email} onChange={(e) => setEmail(e.target.value)} />
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              {ROLES.map((r) => <option key={r} value={r}>{r} — {ROLE_HELP[r]}</option>)}
            </select>
            <button className="btn-primary" onClick={() => run(async () => {
              const r = await inviteMember(email.trim(), role)
              setOneTime({ label: `Invitation code for ${r.email} (${r.role})`, token: r.token, expires_at: r.expires_at })
              setEmail('')
            })}>Create invitation</button>
          </div>
          {invites.length > 0 && (
            <div className="kx-meta" style={{ marginTop: 8 }}>
              Open invitations:{' '}
              {invites.map((i) => (
                <span key={i.invitation_id} style={{ marginRight: 10 }}>
                  {i.email} ({i.role}){' '}
                  <button className="link" onClick={() => run(() => revokeInvitation(i.invitation_id))}>revoke</button>
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="section">
        <div className="section-head"><h2>Your password</h2></div>
        <div className="actions" style={{ marginTop: 0 }}>
          <input type="password" className="approve-note" style={{ maxWidth: 220 }} placeholder="Current password"
            value={pw.current} onChange={(e) => setPw({ ...pw, current: e.target.value })} />
          <input type="password" className="approve-note" style={{ maxWidth: 220 }} placeholder="New password (10+ characters)"
            value={pw.next} onChange={(e) => setPw({ ...pw, next: e.target.value })} />
          <button className="btn-ghost" onClick={() => run(async () => {
            await changePassword(pw.current, pw.next)
            setPw({ current: '', next: '' })
            setPwNote('Password changed. Your other sessions were signed out.')
          })}>Change password</button>
        </div>
        {pwNote && <div className="kx-meta">{pwNote}</div>}
      </div>

      {can('audit.view') && (
        <div className="section">
          <div className="section-head"><h2>Audit history (latest 50)</h2></div>
          {events.length === 0 && <p className="subtitle">No events yet.</p>}
          <ul className="audit-list">
            {events.map((e) => (
              <li key={e.id}>
                <span className="kx-meta">{new Date(e.at).toLocaleString()}</span> · <b>{e.action}</b>
                {e.actor ? ` · ${e.actor}` : ''}
                {e.resource_type ? ` · ${e.resource_type}` : ''}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}
