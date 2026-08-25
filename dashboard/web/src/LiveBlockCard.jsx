// LiveBlockCard — one foundation block, and what it is actually doing.
//
// The part board answers "how far built". This answers "what is it doing", which is
// a different fact with a different proof, so the two states are shown side by side
// and never merged into one badge. A block can be fully built and doing nothing, and
// a board that averaged those into a single colour would hide exactly that.
//
// Three activity states, not two. WORKING means a counter this page watched actually
// move; IDLE means every counter held still between two observed tables -- a finding,
// not a fault; NOT MEASURED means no rate has been taken yet, and calling that idle
// would be asserting a measurement nobody made (Rule 8).
import { useState } from 'react'
import { rung } from './theme.js'
import { formatCount, formatRate, readableCounter } from './useActivity.js'

const ACTIVITY = {
  WORKING: { color: '#57D9A3', glyph: '▲', meaning: 'a counter moved between two observed tables' },
  IDLE: { color: '#8B96A6', glyph: '■', meaning: 'every counter held still between two observed tables' },
  'NOT MEASURED': { color: '#6E5B2E', glyph: '◇', meaning: 'no rate has been taken yet' },
  OFF: { color: '#39424F', glyph: '·', meaning: 'no part of this block reported a heartbeat' },
}

const activityLook = (state) => ACTIVITY[state] || ACTIVITY['NOT MEASURED']

function CounterRow({ counter }) {
  const rate = formatRate(counter.per_second)
  const moving = counter.per_second !== null && counter.per_second !== 0
  return (
    <div className={`counter${moving ? ' counter-moving' : ''}`}>
      <span className="counter-name mono">{readableCounter(counter.name)}</span>
      <span className="counter-value mono">{formatCount(counter.value)}</span>
      <span className={`counter-rate mono${moving ? ' rate-live' : ''}`}>
        {rate === null ? <em className="unmeasured">not measured</em> : rate}
      </span>
    </div>
  )
}

function PartActivityRow({ part, activity }) {
  const [open, setOpen] = useState(false)
  const r = rung(part.rung)

  if (!activity) {
    // The part is in the blueprint and is not reporting. That is a state, and it
    // renders as one -- silently dropping it would make a dark block look smaller
    // than it is.
    return (
      <div className="part-row part-row-silent">
        <span className="part-dot" style={{ background: r.color }} />
        <span className="part-name">{part.name}</span>
        <span className="part-rung mono" style={{ color: r.color }}>{part.rung}</span>
        <span className="part-doing mono unmeasured">not reporting</span>
      </div>
    )
  }

  const moving = activity.counters.filter((c) => c.per_second)
  const busiest = moving.reduce(
    (best, c) => (best === null || Math.abs(c.per_second) > Math.abs(best.per_second) ? c : best),
    null,
  )
  const state = activity.is_working === true ? 'WORKING'
    : activity.is_working === false ? 'IDLE'
      : 'NOT MEASURED'
  const look = activityLook(state)

  return (
    <div className="part-row-wrap">
      <button className="part-row part-row-open" onClick={() => setOpen(!open)} type="button">
        <span className="part-dot" style={{ background: r.color }} />
        <span className="part-name">{part.name}</span>
        <span className="part-act mono" style={{ color: look.color }}>{look.glyph} {state}</span>
        <span className="part-doing mono">
          {busiest
            ? <>{readableCounter(busiest.name)} <b className="rate-live">{formatRate(busiest.per_second)}</b></>
            : <span className="unmeasured">no counter moved</span>}
        </span>
        <span className="part-chev">{open ? '−' : '+'}</span>
      </button>
      {open && (
        <div className="part-detail">
          <div className="part-detail-meta mono">
            <span>heard {activity.age_seconds === null ? '—' : `${activity.age_seconds.toFixed(1)}s`} ago</span>
            <span>staleness {activity.staleness_seconds === null ? '—' : `${activity.staleness_seconds.toFixed(2)}s`}</span>
            <span>rate ratio {activity.rate_ratio ?? '—'}</span>
            <span>
              input loss {activity.input_loss.length ? activity.input_loss.join(', ') : 'none'}
            </span>
          </div>
          {activity.refused_control_frame && (
            <div className="part-refusal mono">refused a control frame: {String(activity.refused_control_frame)}</div>
          )}
          {activity.counters.length === 0
            ? <div className="empty">this part publishes no counters</div>
            : <div className="counters">{activity.counters.map((c) => <CounterRow key={c.name} counter={c} />)}</div>}
          <p className="part-proof">{part.proof}</p>
        </div>
      )}
    </div>
  )
}

export default function LiveBlockCard({ block, parts, blockActivity, partActivity }) {
  const [expanded, setExpanded] = useState(false)
  const summary = blockActivity || { reporting: 0, working: 0, idle: 0, unknown: 0, busiest: null, state: 'NOT MEASURED' }
  const state = summary.reporting === 0 ? 'OFF' : summary.state
  const look = activityLook(state)
  const r = rung(block.state)

  return (
    <section className={`live-card live-card-${state.toLowerCase().replace(' ', '-')}`}>
      <button className="live-head" onClick={() => setExpanded(!expanded)} type="button">
        <div className="live-head-top">
          <h3>{block.name}</h3>
          <span className="badge" style={{ color: look.color, borderColor: look.color }}>
            {look.glyph} {state}
          </span>
        </div>
        <div className="live-head-meta mono">
          {/* Two states, never merged: how far built, and whether it is doing anything. */}
          <span style={{ color: r.color }}>{block.state}</span>
          <span className="sep">·</span>
          <span>{summary.reporting}/{block.n_parts} reporting</span>
          {summary.reporting > 0 && (
            <>
              <span className="sep">·</span>
              <span style={{ color: ACTIVITY.WORKING.color }}>{summary.working} working</span>
              {summary.idle > 0 && <><span className="sep">·</span><span>{summary.idle} idle</span></>}
            </>
          )}
        </div>
        <div className="live-busiest mono">
          {summary.busiest
            ? <>
              <span className="busy-part">{summary.busiest.part_id}</span>
              <span className="busy-counter">{readableCounter(summary.busiest.counter)}</span>
              <b className="rate-live">{formatRate(summary.busiest.per_second)}</b>
            </>
            : <span className="unmeasured">{look.meaning}</span>}
        </div>
      </button>

      {expanded && (
        <div className="live-parts">
          {parts.length === 0
            ? <div className="empty">no parts declared in this block yet</div>
            : parts.map((p) => (
              <PartActivityRow key={p.id} part={p} activity={partActivity?.[p.id]} />
            ))}
        </div>
      )}
    </section>
  )
}
