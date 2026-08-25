import { useMemo, useState } from 'react'
import { useBoard } from './useBoard.js'
import { useActivity } from './useActivity.js'
import { RUNGS, rung } from './theme.js'
import BlockPanel from './BlockPanel.jsx'
import LiveBoard from './LiveBoard.jsx'
import MachineLoad from './MachineLoad.jsx'
import TradingView from './TradingView.jsx'
import CapitalSettings, { useSettings } from './CapitalSettings.jsx'
import { useMachine, useTrades } from './useMachine.js'

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

// The headline sentence is derived from the counts, never written beside them. A
// hand-written line goes stale silently while the numbers under it move -- this one
// said "this project is a blueprint, described not written" while 59 parts were
// running and a million records were on the tape.
function describeProgress(counts, totals) {
  const running = counts.RUNNING || 0
  const declared = counts.DECLARED || 0
  if (running) {
    return (
      <>
        <b>{running} of {totals.parts} parts are RUNNING</b> — reporting a live heartbeat right
        now. {declared > 0 && <>{declared} are still DECLARED: in the blueprint, no code. </>}
        Nothing is ever inferred upward — a part is only past DECLARED when a file naming it exists.
      </>
    )
  }
  return (
    <>
      <b>{declared} of {totals.parts} parts are DECLARED.</b> Nothing is reporting a heartbeat, so
      no part on this board is running. Cells climb as code lands, and nothing is ever inferred upward.
    </>
  )
}

export default function App() {
  const { data, error, mode } = useBoard()
  const { activity, activityError } = useActivity()
  const { machine, machineError } = useMachine()
  const { trades, tradesError } = useTrades()
  const { settings, settingsError } = useSettings()
  const [view, setView] = useState('live')
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
          <h1>Segment Bots</h1>
          <small>
            {totals.blocks} foundation blocks · {totals.parts} parts · every number traces to a probe that ran
          </small>
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

      <nav className="views">
        <button className={`view${view === 'live' ? ' active' : ''}`} onClick={() => setView('live')}>
          What it is doing
        </button>
        <button className={`view${view === 'trading' ? ' active' : ''}`} onClick={() => setView('trading')}>
          Trading
        </button>
        <button className={`view${view === 'machine' ? ' active' : ''}`} onClick={() => setView('machine')}>
          Server load
        </button>
        <button className={`view${view === 'settings' ? ' active' : ''}`} onClick={() => setView('settings')}>
          Capital settings
        </button>
        <button className={`view${view === 'build' ? ' active' : ''}`} onClick={() => setView('build')}>
          How far built
        </button>
      </nav>

      {!contract.ok && (
        <div className="banner warn">
          <b>{contract.violations.length} contract violation(s).</b>
          <ul>{contract.violations.slice(0, 8).map((v) => <li key={v}>{v}</li>)}</ul>
        </div>
      )}

      {view === 'live' ? (
        <LiveBoard data={data} activity={activity} activityError={activityError} />
      ) : view === 'trading' ? (
        <TradingView trades={trades} tradesError={tradesError} />
      ) : view === 'machine' ? (
        <MachineLoad machine={machine} machineError={machineError} />
      ) : view === 'settings' ? (
        <CapitalSettings settings={settings} settingsError={settingsError} />
      ) : (
        <>
          <div className="banner">{describeProgress(counts, totals)}</div>

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
        </>
      )}

      <footer>
        <div>served by dashboard/part_health_api.py · blueprint source docs/features.json</div>
        <div>
          live behaviour read from the heartbeat table heartbeat-collector writes — the same file
          the part monitor and the trade board read
        </div>
      </footer>
    </div>
  )
}
