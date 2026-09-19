import { useEffect, useRef, useState } from 'react'

import { askQuestion, listCarriers } from '../api'

const SUGGESTIONS = [
  'Which carriers cover basement storage, and what loading applies?',
  'What is the claim intimation deadline for a burglary claim?',
  'Compare the fire deductibles across our carriers.',
  'What does the buyer need to provide for a liability proposal?',
]

function sourceLine(source) {
  const parts = [source.carrier_name]
  if (source.filename) parts.push(source.filename)
  if (source.section_title) parts.push(source.section_title)
  if (source.page_start) parts.push(`p.${source.page_start}`)
  return parts.filter(Boolean).join(' · ')
}

export default function Ask() {
  const [carriers, setCarriers] = useState([])
  const [scope, setScope] = useState('')
  const [question, setQuestion] = useState('')
  const [turns, setTurns] = useState([])
  const [asking, setAsking] = useState(false)
  const [error, setError] = useState(null)
  const endRef = useRef(null)

  useEffect(() => {
    listCarriers()
      .then((d) => setCarriers(d.carriers.filter((c) => c.has_summary)))
      .catch(() => setCarriers([]))
  }, [])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns, asking])

  async function send(text) {
    const q = (text ?? question).trim()
    if (!q || asking) return

    setQuestion('')
    setError(null)
    setAsking(true)
    setTurns((t) => [...t, { role: 'you', text: q }])

    try {
      const answer = await askQuestion(q, scope || null)
      setTurns((t) => [...t, { role: 'ai', ...answer }])
    } catch (e) {
      setError(e.message)
      // Drop the unanswered question rather than leaving it hanging in the
      // thread with no reply under it.
      setTurns((t) => t.slice(0, -1))
    } finally {
      setAsking(false)
    }
  }

  return (
    <>
      <header className="page-head">
        <div>
          <p className="eyebrow">Ask</p>
          <h1>Ask the library</h1>
          <p className="subtitle">
            Questions answered only from the carrier documents you have
            uploaded. Every answer cites the wording it came from, and says so
            plainly when the documents do not cover it.
          </p>
        </div>
      </header>

      {error && <div className="banner bad">{error}</div>}

      <div className="scope-row">
        <label htmlFor="scope">Search</label>
        <select
          id="scope"
          value={scope}
          onChange={(e) => setScope(e.target.value)}
        >
          <option value="">every carrier</option>
          {carriers.map((c) => (
            <option key={c.carrier_id} value={c.carrier_id}>
              {c.name}
            </option>
          ))}
        </select>
      </div>

      {turns.length === 0 && !asking && (
        <div className="section">
          <div className="section-head">
            <h2>Try</h2>
          </div>
          <div className="listing">
            {SUGGESTIONS.map((s) => (
              <button
                key={s}
                className="listing-item suggestion"
                onClick={() => send(s)}
              >
                <div className="listing-text">{s}</div>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="thread">
        {turns.map((turn, i) =>
          turn.role === 'you' ? (
            <div className="turn turn-you" key={i}>
              {turn.text}
            </div>
          ) : (
            <div className="turn turn-ai" key={i}>
              <p className="prose" style={{ margin: 0 }}>
                {turn.answer}
              </p>

              {!turn.sufficient && (
                <div className="turn-flag">
                  Not answered from the documents — nothing relevant was found.
                </div>
              )}

              {turn.sources?.length > 0 && (
                <div className="turn-sources">
                  <div className="plan-exclusions-label">Sources</div>
                  {turn.sources.map((s, j) => (
                    <div className="listing-source" key={j}>
                      {sourceLine(s)}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ),
        )}
        {asking && <div className="turn turn-ai loading">Reading the wordings…</div>}
        <div ref={endRef} />
      </div>

      <div className="ask-bar">
        <input
          type="text"
          value={question}
          placeholder="Ask about cover, exclusions, premiums, requirements…"
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          disabled={asking}
        />
        <button
          className="btn-primary"
          onClick={() => send()}
          disabled={asking || !question.trim()}
        >
          Ask
        </button>
      </div>
    </>
  )
}
