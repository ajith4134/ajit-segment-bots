// PartCell — one part. The whole point of the spine: this renders ANY part from its
// declared contract, so a part written tomorrow appears on the board with no panel
// written for it. Bespoke panels come later, only where a generic cell is not enough.
import { useState } from 'react'
import { T, rung } from './theme.js'

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
      <div className="cell-name">{part.name}</div>
      <div className="cell-rung" style={{ color: r.color }}>{part.rung}</div>

      {open && (
        <div className="cell-detail">
          <div className="detail-role">{part.role}</div>
          {/* The proof is the point. A status with no provenance is not a status. */}
          <div className="detail-proof">{part.proof}</div>
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
