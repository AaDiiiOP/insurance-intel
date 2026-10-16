import { useState } from 'react'

import { acceptInvitation, completePasswordReset, login } from '../api'

/**
 * Phase 2A sign-in screen. Three small forms on one page:
 *   - sign in
 *   - join with an invitation code (from an Owner/Admin)
 *   - set a new password with a reset code (from an Owner/Admin)
 * No e-mail is sent by Aurevia; codes are handed over by a colleague.
 */
export default function Login({ onSignedIn }) {
  const [mode, setMode] = useState('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [name, setName] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)

  async function submit(e) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    setNotice(null)
    try {
      if (mode === 'signin') {
        onSignedIn(await login(email.trim(), password))
        return
      }
      if (mode === 'invite') {
        const r = await acceptInvitation(code.trim(), name.trim(), password)
        setNotice(`Welcome — your ${r.role.toLowerCase()} account for ${r.email} is ready. Sign in below.`)
        setEmail(r.email)
      } else {
        await completePasswordReset(code.trim(), password)
        setNotice('Password changed. Sign in with the new password.')
      }
      setMode('signin')
      setPassword('')
      setCode('')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="login-wrap">
      <form className="login-card" onSubmit={submit}>
        <h1 className="wordmark">
          Aurevia<span> BrokerOS</span>
        </h1>
        <p className="subtitle">
          {mode === 'signin' && 'Sign in to your organization.'}
          {mode === 'invite' && 'Join with the invitation code you were given.'}
          {mode === 'reset' && 'Set a new password with the reset code you were given.'}
        </p>
        {error && <div className="banner bad" role="alert">{error}</div>}
        {notice && <div className="banner ok">{notice}</div>}

        {mode === 'signin' && (
          <label>
            E-mail
            <input type="email" autoComplete="username" value={email} required
              onChange={(e) => setEmail(e.target.value)} />
          </label>
        )}
        {mode !== 'signin' && (
          <label>
            {mode === 'invite' ? 'Invitation code' : 'Reset code'}
            <input value={code} required autoComplete="one-time-code" onChange={(e) => setCode(e.target.value)} />
          </label>
        )}
        {mode === 'invite' && (
          <label>
            Your name
            <input value={name} autoComplete="name" onChange={(e) => setName(e.target.value)} />
          </label>
        )}
        <label>
          {mode === 'signin' ? 'Password' : 'New password (at least 10 characters)'}
          <input type="password" value={password} required minLength={mode === 'signin' ? 1 : 10}
            autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
            onChange={(e) => setPassword(e.target.value)} />
        </label>
        <button className="btn-primary" type="submit" disabled={busy}>
          {busy ? 'Please wait…' : mode === 'signin' ? 'Sign in' : mode === 'invite' ? 'Create account' : 'Set password'}
        </button>
        <div className="login-links">
          {mode !== 'signin' && <button type="button" className="link" onClick={() => setMode('signin')}>Back to sign in</button>}
          {mode !== 'invite' && <button type="button" className="link" onClick={() => setMode('invite')}>I have an invitation code</button>}
          {mode !== 'reset' && <button type="button" className="link" onClick={() => setMode('reset')}>I have a reset code</button>}
        </div>
      </form>
    </div>
  )
}
