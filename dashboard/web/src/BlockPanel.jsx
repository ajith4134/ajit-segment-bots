// BlockPanel — one foundation block, with its own badge.
//
// The badge is per panel, never global. That is the thing the user's own board gets
// right and a single board-wide status gets wrong: when each panel declares its own
// state, a dead panel does not discredit a live one beside it.
import { T, rung } from './theme.js'
import PartCell from './PartCell.jsx'

export default function BlockPanel({ block, parts }) {
  const r = rung(block.state)
  const pct = block.n_parts ? Math.round((100 * block.n_built) / block.n_parts) : 0

  return (
    <section className="panel">
      <header className="panel-head">
        <div className="panel-title">
          <h3>{block.name}</h3>
          <span className="badge" style={{ color: r.color, borderColor: r.color }}>{block.state}</span>
        </div>
        <div className="panel-meta">
          <span className="scope">{block.scope || 'scope not set'}</span>
          <div className="meter"><div className="meter-fill" style={{ width: `${pct}%`, background: r.color }} /></div>
          <span className="meter-num">{block.n_built}/{block.n_parts}</span>
        </div>
      </header>

      {block.summary && <p className="panel-summary">{block.summary}</p>}

      {parts.length === 0 ? (
        // A block with no parts is a real state and renders as one. Silently omitting
        // it would read as "nothing to see" when it means "nothing described yet".
        <div className="empty">no parts declared in this block yet</div>
      ) : (
        <div className="cells">
          {parts.map((p) => <PartCell key={p.id} part={p} />)}
        </div>
      )}
    </section>
  )
}
