import { useCallback, useRef, useState } from 'react'

import { createCarrier } from '../api'

const ACCEPTED = ['.pdf', '.docx', '.xlsx', '.pptx']

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function extensionOf(filename) {
  const dot = filename.lastIndexOf('.')
  return dot === -1 ? '' : filename.slice(dot).toLowerCase()
}

export default function Upload({ onUploaded, onOpen }) {
  const [staged, setStaged] = useState([])
  const [carrierName, setCarrierName] = useState('')
  const [rejected, setRejected] = useState([])
  const [uploading, setUploading] = useState(false)
  const [isOver, setIsOver] = useState(false)
  const [error, setError] = useState(null)
  const [result, setResult] = useState(null)
  const inputRef = useRef(null)

  const addFiles = useCallback((fileList) => {
    setError(null)
    const incoming = Array.from(fileList)
    const bad = []

    setStaged((current) => {
      const next = [...current]
      for (const file of incoming) {
        if (!ACCEPTED.includes(extensionOf(file.name))) {
          bad.push(file.name)
          continue
        }
        // Silently skip an exact re-drop of the same file.
        if (next.some((s) => s.file.name === file.name && s.file.size === file.size)) {
          continue
        }
        next.push({ key: `${file.name}-${file.size}-${next.length}`, file })
      }
      return next
    })

    setRejected(bad)
  }, [])

  function removeFile(key) {
    setStaged((current) => current.filter((s) => s.key !== key))
  }

  function reset() {
    setStaged([])
    setCarrierName('')
    setRejected([])
    setResult(null)
    setError(null)
  }

  async function handleUpload() {
    setUploading(true)
    setError(null)
    try {
      const response = await createCarrier(staged, carrierName.trim() || undefined)
      setResult(response)
      setStaged([])
      setCarrierName('')
      onUploaded?.(response)
    } catch (err) {
      setError(err.message)
    } finally {
      setUploading(false)
    }
  }

  // ---- Result view, after a successful upload ----
  if (result) {
    const failed = result.files.filter((f) => f.status === 'failed')
    const parsed = result.files.length - failed.length

    return (
      <>
        <header className="page-head">
          <div>
            <p className="eyebrow">Upload</p>
            <h1>
              {result.name}
              <span className="stop">.</span>
            </h1>
            <p className="subtitle">
              {failed.length === 0
                ? 'Every file parsed. The text is stored — generate the briefing next.'
                : `${parsed} of ${result.files.length} files parsed. Re-upload the ones that failed, or carry on without them.`}
            </p>
          </div>
        </header>

        <div className={`banner ${failed.length ? 'bad' : 'good'}`}>
          {failed.length
            ? `${failed.length} file${failed.length > 1 ? 's' : ''} could not be read.`
            : 'Carrier created and all files parsed.'}
        </div>

        <div className="file-list">
          {result.files.map((file) => (
            <div
              key={file.document_id}
              className={`file-row ${file.status === 'failed' ? 'is-failed' : ''}`}
            >
              <span
                className="swatch"
                style={{
                  background: file.status === 'parsed' ? 'var(--ok)' : 'var(--bad)',
                }}
              />
              <div className="file-main">
                <div className="file-name">{file.name}</div>
                <div className="file-meta">
                  {file.status === 'parsed' && (
                    <>
                      {file.chars.toLocaleString()} characters
                      {file.pages > 0 && ` · ${file.pages} pages`}
                    </>
                  )}
                </div>
                {file.error && <div className="file-error">{file.error}</div>}
              </div>
              <span
                className={`file-status ${file.status === 'parsed' ? 'ok' : 'bad'}`}
              >
                {file.status}
              </span>
            </div>
          ))}
        </div>

        <div className="actions">
          <span className="hint" />
          <button className="btn-ghost" onClick={reset}>
            Add another carrier
          </button>
          <button
            className="btn-primary"
            onClick={() => onOpen?.(result.carrier_id)}
            disabled={parsed === 0}
          >
            Open briefing →
          </button>
        </div>
      </>
    )
  }

  // ---- Upload view ----
  return (
    <>
      <header className="page-head">
        <div>
          <p className="eyebrow">Upload</p>
          <h1>
            Add a carrier<span className="stop">.</span>
          </h1>
          <p className="subtitle">
            Drop in an insurer's own paperwork — policy wordings, prospectuses,
            rate cards, brochures. Several files for the same carrier can go in
            together; they are read as one set.
          </p>
        </div>
      </header>

      {error && <div className="banner bad">{error}</div>}

      {rejected.length > 0 && (
        <div className="banner bad">
          Skipped {rejected.join(', ')} — only {ACCEPTED.join(', ')} can be read.
        </div>
      )}

      <div className="drop-slot">
        <p className="drop-slot-label">
          Carrier documents
          {staged.length > 0 && (
            <span className="drop-slot-count">
              {' '}
              · {staged.length} file{staged.length === 1 ? '' : 's'}
            </span>
          )}
        </p>

        <button
          type="button"
          className={`dropzone ${isOver ? 'is-over' : ''}`}
          onClick={() => inputRef.current?.click()}
          onDragOver={(e) => {
            e.preventDefault()
            setIsOver(true)
          }}
          onDragLeave={() => setIsOver(false)}
          onDrop={(e) => {
            e.preventDefault()
            setIsOver(false)
            if (e.dataTransfer.files?.length) addFiles(e.dataTransfer.files)
          }}
        >
          <div className="dropzone-icon" aria-hidden="true">
            <svg width="22" height="22" viewBox="0 0 20 20" fill="none">
              <path
                d="M10 14V4m0 0L6 8m4-4l4 4M3 15v1a2 2 0 002 2h10a2 2 0 002-2v-1"
                stroke="currentColor"
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </div>
          <p className="dropzone-hint">
            Drop files here — or click to browse ({ACCEPTED.join(', ')})
          </p>
        </button>

        <input
          ref={inputRef}
          type="file"
          multiple
          accept={ACCEPTED.join(',')}
          style={{ display: 'none' }}
          onChange={(e) => {
            addFiles(e.target.files)
            e.target.value = ''
          }}
        />

        {staged.length > 0 && (
          <div className="file-list">
            {staged.map((s) => (
              <div key={s.key} className="file-row">
                <span className="swatch" style={{ background: 'var(--line-firm)' }} />
                <div className="file-main">
                  <div className="file-name">{s.file.name}</div>
                  <div className="file-meta">{formatSize(s.file.size)}</div>
                </div>
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => removeFile(s.key)}
                  aria-label={`Remove ${s.file.name}`}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
      </div>

      {staged.length > 0 && (
        <>
          <div className="name-field">
            <label htmlFor="carrier-name">Carrier name</label>
            <input
              id="carrier-name"
              type="text"
              value={carrierName}
              placeholder={staged[0].file.name.replace(/\.[^.]+$/, '')}
              onChange={(e) => setCarrierName(e.target.value)}
            />
          </div>

          <div className="actions">
            <span className="hint">
              {staged.length} file{staged.length === 1 ? '' : 's'} ready. Parsing
              is quick; the AI briefing is a separate step.
            </span>
            <button className="btn-ghost" onClick={reset} disabled={uploading}>
              Clear
            </button>
            <button
              className="btn-primary"
              onClick={handleUpload}
              disabled={uploading}
            >
              {uploading ? 'Parsing…' : 'Upload & parse'}
            </button>
          </div>
        </>
      )}

      <div className="setup-note">
        Phase 1 reads carrier paperwork only. Client details belong to Phase 2 and
        will be handled separately, so nothing personal should be uploaded here.
      </div>
    </>
  )
}
