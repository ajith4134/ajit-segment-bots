// PartCell — one part. The whole point of the spine: this renders ANY part from its
// declared contract, so a part written tomorrow appears on the board with no panel
// written for it. Bespoke panels come later, only where a generic cell is not enough.
import { useState } from 'react'
import { T, rung } from './theme.js'
import CompletionDot from './CompletionDot.jsx'

export default function PartCell({ part }) {
  const [open, setOpen] = useState(false)
  const r = rung(part.rung)

  return (
    <div
      className="cell"
      style={{ borderLeftColor: r.color, background: part.rung === 'FAILING' ? '#D45B5418' : T.sunk }}
      onClick={() => setOpen((v) => !v)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setOpen((v) => !v) } }}
      aria-expanded={open}
    >
      <div className="cell-top">
        <CompletionDot isComplete={part.is_complete} />
        <div className="cell-name">{part.name}</div>
      </div>
      <div className="cell-rung" style={{ color: r.color }}>{part.rung}</div>

      {open && (
        <div className="cell-detail">
          <div className="detail-role">{part.role}</div>
          {/* The proof is the point. A status with no provenance is not a status.
              RL-070's dot proof is folded into the same line rather than a second
              reveal mechanism: for a part the dot and the rung are the same fact. */}
          <div className="detail-proof">
            <CompletionDot isComplete={part.is_complete} />{' '}
            {part.is_complete ? 'complete' : 'unfinished'} — {part.dot_proof}
          </div>
          <div className="detail-io">
            <span className="io-label">reads</span>
            {part.consumes.length ? part.consumes.join(' · ') : 'nothing'}
          </div>
          <div className="detail-io">
            <span className="io-label">writes</span>
            {part.produces.length ? part.produces.join(' · ') : 'nothing'}
          </div>
        </div>
      )}
    </div>
  )
}
