import { useMemo, useState } from 'react'
import { useBoard } from './useBoard.js'
import { RUNGS, rung } from './theme.js'
import BlockPanel from './BlockPanel.jsx'

const LADDER = ['DECLARED', 'IMPLEMENTED', 'TESTED', 'RUNNING']
const OFF_LADDER = ['FAILING', 'NOT MEASURED']

function Stat({ label, value, color }) {
  return (
    <div className="stat">
      <div className="stat-val" style={color ? { color } : undefined}>{value}</div>
      <div className="stat-lab">{label}</div>
    </div>
  )
}

export default function App() {
  const { data, error, mode } = useBoard()
  const [tab, setTab] = useState('overview')

  const partsByBlock = useMemo(() => {
    const map = {}
    for (const p of data?.parts || []) (map[p.block] ||= []).push(p)
    return map
  }, [data])

  if (!data) {
    return (
      <div className="app">
        <div className="banner warn">
          <b>No data.</b> The board is served by <code>part_health_api.py</code>. Start it with{' '}
          <code>python3 dashboard/part_health_api.py</code>, or open a published snapshot.
          {error && <div className="banner-sub">{error}</div>}
        </div>
      </div>
    )
  }

  const { totals, counts, contract, blocks } = data
  const built = totals.built
  const pct = totals.parts ? Math.round((100 * built) / totals.parts) : 0
  const shown = tab === 'overview' ? blocks : blocks.filter((b) => b.id === tab)

  return (
    <div className="app">
      <header className="top">
        <div className="brand">
          <h1>Segment Bots — Part Board</h1>
          <small>{totals.blocks} blocks · {totals.parts} parts · every cell traces to a probe that ran</small>
        </div>
        <div className="mode">
          {/* A snapshot presented as live is a lie with a timestamp available. */}
          <span className={`badge ${mode === 'live' ? 'live' : 'snap'}`}>
            {mode === 'live' ? 'LIVE · polling' : 'SNAPSHOT · frozen'}
          </span>
          <span className="stamp">{data.generated_at}</span>
          {error && <span className="stale">stale — last poll failed</span>}
        </div>
      </header>

      <div className="banner">
        <b>{counts.DECLARED} of {totals.parts} parts are DECLARED.</b> This project is a
        blueprint: described, not written. Cells climb as code lands, and nothing is ever
        inferred upward — a part is only past DECLARED when a file naming it exists.
      </div>

      {!contract.ok && (
        <div className="banner warn">
          <b>{contract.violations.length} contract violation(s).</b>
          <ul>{contract.violations.slice(0, 8).map((v) => <li key={v}>{v}</li>)}</ul>
        </div>
      )}

      <section className="stats">
        <Stat label="blocks" value={totals.blocks} />
        <Stat label="parts" value={totals.parts} />
        <Stat label="data types" value={totals.data_types} />
        <Stat label="built" value={`${pct}%`} color={rung(built ? 'IMPLEMENTED' : 'DECLARED').color} />
        <Stat label="failing" value={counts.FAILING} color={counts.FAILING ? RUNGS.FAILING.color : undefined} />
      </section>

      <section className="legend">
        {[...LADDER, ...OFF_LADDER].map((name) => (
          <div className="legend-row" key={name} style={{ opacity: counts[name] ? 1 : 0.55 }}>
            <span className="swatch" style={{ background: rung(name).color }} />
            <b>{name}</b>
            <span className="legend-meaning">{rung(name).meaning}</span>
            <span className="legend-count">{counts[name] ?? 0}</span>
          </div>
        ))}
      </section>

      <nav className="tabs">
        <button className={`tab${tab === 'overview' ? ' active' : ''}`} onClick={() => setTab('overview')}>
          All blocks
        </button>
        {blocks.map((b) => (
          <button
            key={b.id}
            className={`tab${tab === b.id ? ' active' : ''}`}
            style={{ borderColor: tab === b.id ? rung(b.state).color : undefined }}
            onClick={() => setTab(b.id)}
          >
            <span className="tab-dot" style={{ background: rung(b.state).color }} />
            {b.name}
          </button>
        ))}
      </nav>

      <div className="grid">
        {shown.map((b) => (
          <BlockPanel key={b.id} block={b} parts={partsByBlock[b.id] || []} />
        ))}
      </div>

      <footer>
        <div>served by dashboard/part_health_api.py · blueprint source docs/features.json</div>
        <div>click any cell for its role, its proof, and the data it reads and writes</div>
      </footer>
    </div>
  )
}
