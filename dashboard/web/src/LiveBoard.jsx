// LiveBoard — every foundation block at once, ordered by what is actually happening.
//
// All 27 blocks are always on the page. A dark block is the correct rendering of a
// block that is not running, and hiding it would make the system look more finished
// than it is -- most of this one is not built, and the board has to say so.
//
// The order is by activity rather than by name, because the question this page
// answers is "what is the bot doing", and the answer belongs at the top. Within a
// state the blueprint's own order is kept, so a block does not wander between polls.
import { useMemo } from 'react'
import LiveBlockCard from './LiveBlockCard.jsx'
import { formatRate, readableCounter } from './useActivity.js'

const ORDER = { WORKING: 0, IDLE: 1, 'NOT MEASURED': 2, OFF: 3 }

function LiveStat({ label, value, sub, color }) {
  return (
    <div className="live-stat">
      <div className="live-stat-val" style={color ? { color } : undefined}>{value}</div>
      <div className="live-stat-lab">{label}</div>
      {sub && <div className="live-stat-sub mono">{sub}</div>}
    </div>
  )
}

export default function LiveBoard({ data, activity, activityError }) {
  const partsByBlock = useMemo(() => {
    const map = {}
    for (const p of data.parts) (map[p.block] ||= []).push(p)
    return map
  }, [data])

  const ordered = useMemo(() => {
    const blocks = data.blocks.map((b, index) => {
      const summary = activity?.blocks?.[b.id]
      const state = !summary || summary.reporting === 0 ? 'OFF' : summary.state
      return { block: b, summary, state, index }
    })
    return blocks.sort((a, z) => (ORDER[a.state] - ORDER[z.state]) || (a.index - z.index))
  }, [data, activity])

  const provenance = activity?.provenance
  const reporting = Object.keys(activity?.parts || {}).length
  const working = ordered.reduce((n, o) => n + (o.summary?.working || 0), 0)
  const blocksWorking = ordered.filter((o) => o.state === 'WORKING').length

  // The single busiest thing in the whole system, whatever it happens to be. It is
  // the one number that says at a glance that the bot is alive rather than merely up.
  const busiest = useMemo(() => {
    let best = null
    for (const o of ordered) {
      const b = o.summary?.busiest
      if (b && (best === null || b.per_second > best.per_second)) best = b
    }
    return best
  }, [ordered])

  return (
    <>
      {activityError && (
        // The shape stays on screen and only the live column goes dark. That is the
        // honest picture: the blueprint is still known, the behaviour is not.
        <div className="banner warn">
          <b>Live column is dark.</b> <code>/api/activity</code> is not answering, so nothing
          below is current. The build state beside it still comes from the last good board poll.
          <div className="banner-sub mono">{activityError}</div>
        </div>
      )}

      {provenance && !provenance.ok && (
        <div className="banner warn">
          <b>Nothing is reporting.</b> {provenance.proof}
        </div>
      )}

      <div className="live-stats">
        <LiveStat
          label="parts reporting"
          value={reporting}
          sub={`of ${data.totals.parts} in the blueprint`}
          color={reporting ? '#57D9A3' : '#6E5B2E'}
        />
        <LiveStat
          label="parts working"
          value={provenance?.has_a_rate ? working : '—'}
          sub={provenance?.has_a_rate
            ? `a counter moved in the last ${provenance.rate_over_seconds?.toFixed(1)}s`
            : 'no rate taken yet'}
          color={provenance?.has_a_rate ? '#57D9A3' : '#6E5B2E'}
        />
        <LiveStat
          label="blocks working"
          value={provenance?.has_a_rate ? blocksWorking : '—'}
          sub={`of ${data.totals.blocks} foundation blocks`}
          color={provenance?.has_a_rate ? '#57D9A3' : '#6E5B2E'}
        />
        <LiveStat
          label="busiest counter"
          value={busiest ? formatRate(busiest.per_second) : '—'}
          sub={busiest ? `${busiest.part_id} · ${readableCounter(busiest.counter)}` : 'no rate taken yet'}
          color={busiest ? '#4DA3FF' : '#6E5B2E'}
        />
        <LiveStat
          label="heartbeat table"
          value={provenance?.table_age_seconds === null || provenance?.table_age_seconds === undefined
            ? '—'
            : `${provenance.table_age_seconds.toFixed(1)}s`}
          sub="age when it was read"
          color={provenance?.ok ? '#57D9A3' : '#D45B54'}
        />
        <LiveStat
          label="contract"
          value={data.contract.ok ? 'HOLDS' : `${data.contract.violations.length} BROKEN`}
          sub="what is built equals the blueprint"
          color={data.contract.ok ? '#57D9A3' : '#D45B54'}
        />
      </div>

      {provenance?.proof && <p className="live-proof mono">{provenance.proof}</p>}

      <div className="live-grid">
        {ordered.map(({ block, summary }) => (
          <LiveBlockCard
            key={block.id}
            block={block}
            parts={partsByBlock[block.id] || []}
            blockActivity={summary}
            partActivity={activity?.parts}
          />
        ))}
      </div>
    </>
  )
}
