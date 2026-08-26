// TradingView — what the bot is holding, and what it has closed.
//
// Open positions come from the checkpoint `position-close-detector` restores from,
// which is the same lots the bot is acting on rather than a second copy kept for a
// board. Closed trades come from the tail of the position journal, and the view
// says so: it never claims the rows are all of them when they are the last few.
//
// Two states this must be able to reach, because they are the true ones and a
// board that cannot render them is a board that lies on the day they happen:
// NOTHING YET, when the bot holds nothing or has closed nothing, and NOT MEASURED,
// when the checkpoint has never been written — which is a different fact from
// holding nothing.
import { formatDuration, formatMoney, formatPercent } from './useMachine.js'

const pnlColour = (value) => {
  if (value === null || value === undefined) return 'var(--faint)'
  if (value > 0) return '#57D9A3'
  if (value < 0) return '#D45B54'
  return 'var(--muted)'
}

function Num({ value, digits = 6 }) {
  if (value === null || value === undefined) return <em className="unmeasured">—</em>
  const magnitude = Math.abs(value)
  const text = magnitude >= 1000
    ? value.toFixed(2)
    : magnitude >= 1
      ? value.toFixed(Math.min(digits, 4))
      : value.toPrecision(Math.min(digits, 6))
  return <>{text}</>
}

function Pnl({ value, digits = 3 }) {
  if (value === null || value === undefined) return <em className="unmeasured">—</em>
  return <span style={{ color: pnlColour(value) }}>{formatMoney(value, digits)}</span>
}

function OpenPositions({ open }) {
  const { positions, provenance } = open

  if (!provenance.ok) {
    return (
      <div className="trade-panel">
        <div className="trade-panel-head">
          <h3>Open positions</h3>
          <span className="badge" style={{ color: '#6E5B2E', borderColor: '#6E5B2E' }}>NOT MEASURED</span>
        </div>
        <div className="trade-empty mono">{provenance.proof}</div>
      </div>
    )
  }

  const marked = positions.filter((p) => p.unrealised_pnl !== null && p.unrealised_pnl !== undefined)
  const unrealised = marked.length
    ? marked.reduce((sum, p) => sum + p.unrealised_pnl, 0)
    : null

  return (
    <div className="trade-panel">
      <div className="trade-panel-head">
        <h3>Open positions</h3>
        <div className="trade-panel-figures mono">
          <span>{positions.length} held</span>
          <span className="sep">·</span>
          <span>{open.capital_in?.toFixed(2)} USDT in</span>
          {unrealised !== null && (
            <>
              <span className="sep">·</span>
              <span>unrealised <Pnl value={unrealised} digits={2} /></span>
            </>
          )}
        </div>
      </div>

      {positions.length === 0 ? (
        <div className="trade-empty">
          <b>NOTHING YET.</b> The bot holds no position right now. The checkpoint was
          read and says so — this is not an absence of evidence.
        </div>
      ) : (
        <div className="trade-scroll">
          <table className="trade-table">
            <thead>
              <tr>
                <th>symbol</th><th>venue</th><th>side</th>
                <th className="n">quantity</th><th className="n">entry</th>
                <th className="n">price now</th><th className="n">age</th>
                <th className="n">capital in</th><th className="n">unrealised</th>
                <th className="n">peak / worst</th>
                <th className="n">realised</th>
                <th className="n">predicted up</th><th className="n">predicted down</th>
                <th className="n">stop</th><th className="n">target</th>
                <th className="n">tailgating</th>
                <th className="n">fees</th><th className="n">held for</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => (
                <tr key={`${p.venue_id}|${p.symbol}`}>
                  <td className="sym">{p.symbol}</td>
                  <td className="faint">{p.venue_id}</td>
                  <td style={{ color: p.direction === 'short' ? '#D48A54' : '#4DA3FF' }}>
                    {p.direction || '—'}
                  </td>
                  <td className="n mono"><Num value={p.quantity} /></td>
                  <td className="n mono"><Num value={p.entry_price} /></td>
                  <td className="n mono">
                    {p.price_now === null || p.price_now === undefined
                      ? <em className="unmeasured" title={p.price_proof}>not measured</em>
                      : <Num value={p.price_now} />}
                  </td>
                  {/* A price's whole value is its age, so the age travels with it. */}
                  <td className="n mono faint">
                    {p.price_age_seconds === null || p.price_age_seconds === undefined
                      ? '—' : `${p.price_age_seconds.toFixed(0)}s`}
                  </td>
                  <td className="n mono">{p.capital_in?.toFixed(2)}</td>
                  <td className="n mono"><Pnl value={p.unrealised_pnl} digits={3} /></td>
                  {/* Peak and worst in one cell, best/worst, because they are one
                      fact -- the range this position has travelled -- and reading
                      them apart makes the reader do the subtraction. Never zero for
                      "we did not watch": the excursion's absence is what says
                      nothing was recorded, and that renders as not measured.

                      Both are widened by the live unrealised before display. The
                      checkpoint records what peak-excursion-tracker saw, and it
                      cannot have seen a move it was not running for -- so a stored
                      worst of -4.91 on a position sitting at -27.27 is not a
                      smaller loss, it is an older reading. The live mark is
                      evidence of the excursion in its own right, and taking the
                      wider of the two is the only reading that cannot understate
                      it. */}
                  <td className="n mono">
                    <Excursion
                      best={p.best_unrealised}
                      worst={p.worst_unrealised}
                      live={p.unrealised_pnl}
                    />
                  </td>
                  {/* Realised on a position still open: a lot sold back before the
                      rest. Absent means this position was never scaled out of, which
                      is not the same fact as having realised zero on one that was. */}
                  <td className="n mono">
                    {p.realised_so_far === null || p.realised_so_far === undefined
                      ? <span className="faint">—</span>
                      : <Pnl value={p.realised_so_far} digits={3} />}
                  </td>
                  {/* What this symbol has historically done to a call of this side,
                      from signal-excursion-profiler's own checkpoint -- the file that
                      part restores from, not a second count kept for the board. Up is
                      the favourable move at the first exit-target quantile; down is
                      the adverse move a correct call survived, which is the only
                      honest basis for a stop. Below the profiler's own claim minimum
                      it says so rather than showing a quantile nobody should size on. */}
                  <td className="n mono">
                    {p.expected_favourable_quote === null || p.expected_favourable_quote === undefined
                      ? <em className="unmeasured" title={p.prediction_proof}>not measured</em>
                      : <span title={p.prediction_proof}>
                          +{p.expected_favourable_quote.toFixed(2)}
                          <span className="faint"> ({(p.expected_favourable_fraction * 100).toFixed(2)}%)</span>
                        </span>}
                  </td>
                  <td className="n mono">
                    {p.expected_adverse_quote === null || p.expected_adverse_quote === undefined
                      ? <em className="unmeasured" title={p.prediction_proof}>not measured</em>
                      : <span title={p.prediction_proof}>
                          −{p.expected_adverse_quote.toFixed(2)}
                          <span className="faint"> ({(p.expected_adverse_fraction * 100).toFixed(2)}%)</span>
                        </span>}
                  </td>
                  {/* The stop and target actually resting, from stop-order-manager's
                      own checkpoint -- the file that part restores from. `unprotected`
                      is the state worth seeing and is deliberately loud: it means this
                      position is open with no stop resting for it, which is a fact
                      about the trade rather than a gap in the board. It is distinct
                      from `not measured`, which means the part has never checkpointed
                      at all. */}
                  <td className="n mono">
                    {p.stop_price === null || p.stop_price === undefined
                      ? <em className={p.is_protected === false ? 'unprotected' : 'unmeasured'}
                            title={p.exit_proof}>
                          {p.is_protected === false ? 'unprotected' : 'not measured'}
                        </em>
                      : <span title={p.exit_proof}>
                          <Num value={p.stop_price} />
                          {p.stop_distance_fraction != null && (
                            <span className="faint"> ({(p.stop_distance_fraction * 100).toFixed(2)}%)</span>
                          )}
                        </span>}
                  </td>
                  <td className="n mono">
                    {p.target_price === null || p.target_price === undefined
                      ? <span className="faint" title={p.exit_proof}>—</span>
                      : <span title={p.exit_proof}><Num value={p.target_price} /></span>}
                  </td>
                  {/* Tailgating is the one per-position plan still held on the bus
                      alone. tail-trailing-exit-planner keeps its trailing level in
                      memory the way stop-order-manager kept its resting stops until
                      2026-08-26; until it checkpoints too, nothing anywhere holds the
                      answer and a column that guessed would be exactly the failure
                      Rule 8 exists to prevent. NOT BUILT, not NOT MEASURED: this is a
                      fact about the system, not about this reading. */}
                  <td className="n mono">
                    <em className="unmeasured" title="tail-trailing-exit-planner holds its trailing level in memory and publishes tail-exit-plan on the bus; nothing writes a per-position tailgating state to disk, so no board process can read one">not built</em>
                  </td>
                  <td className="n mono faint">{p.fees_paid?.toFixed(3)}</td>
                  <td className="n mono faint">
                    {p.opened_at_ns
                      ? formatDuration((Date.now() * 1e6 - p.opened_at_ns) / 1e9)
                      : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="trade-proof mono">{provenance.proof}</div>
    </div>
  )
}

function Excursion({ best, worst, live }) {
  // The widest each side has been, counting the live mark as evidence. A tracker
  // that was not running for a move did not record it, and the stored figure is
  // then older rather than smaller -- so the live unrealised widens the range it
  // falls outside of. Shown as best/worst in one cell.
  const hasStored = best !== null && best !== undefined
    && worst !== null && worst !== undefined
  const hasLive = live !== null && live !== undefined
  if (!hasStored && !hasLive) {
    return (
      <em className="unmeasured"
          title="peak-excursion-tracker has recorded no excursion for this position, and the tape cannot mark it either">
        not measured
      </em>
    )
  }
  const peak = Math.max(hasStored ? best : -Infinity, hasLive ? live : -Infinity)
  const trough = Math.min(hasStored ? worst : Infinity, hasLive ? live : Infinity)
  const widened = hasStored && hasLive && (live > best || live < worst)
  const proof = !hasStored
    ? 'no excursion recorded; this range is the live mark alone'
    : widened
      ? `the recorded excursion was ${best.toFixed(3)}/${worst.toFixed(3)}, `
        + 'widened by the live mark -- the tracker was not running for part of this trade'
      : 'from the excursion peak-excursion-tracker recorded, alongside the lots'
  return (
    <span title={proof} className={widened ? 'widened' : undefined}>
      <Pnl value={peak} digits={2} />
      <span className="faint"> / </span>
      <Pnl value={trough} digits={2} />
    </span>
  )
}

// Where the money actually came from. The same +40 is produced by a move that went
// straight to target and by one twice as large that gave half of itself to fees --
// opposite lessons, and realised PnL alone cannot tell them apart.
//
// The residual is always shown, never folded into the components. pnl-attributor's
// own docstring calls a large residual the most useful thing it produces: it says
// the model of where PnL comes from is missing something, and a board showing the
// pieces without it would present an incomplete reconciliation as a complete one.
// The key pnl-attributor files the residual under, from runtime/trade_decoding_types.py
// (FROM_UNEXPLAINED). Named once so the two places that treat it specially agree.
const RESIDUAL_COMPONENT = 'unexplained'

function Attribution({ attribution }) {
  if (!attribution) {
    return <em className="unmeasured" title="learning-recorder has not journalled an attribution for this trade">not attributed</em>
  }
  const { components, residual, reconciles } = attribution
  const pieces = Object.entries(components || {})
    // The residual is one of the components AND is reported on its own, so listing
    // it here too printed "unexplained" twice on the first live attribution. It is
    // dropped from the list rather than from the cell: it gets its own rendering
    // below, with its own colour and the warning when the pieces do not add up.
    .filter(([name, value]) => value && name !== RESIDUAL_COMPONENT)
    .sort((a, z) => Math.abs(z[1]) - Math.abs(a[1]))

  return (
    <div className="attribution-cell mono">
      {pieces.length === 0
        ? <em className="unmeasured">no component carried any of it</em>
        : pieces.map(([name, value]) => (
          <span className="piece" key={name}>
            <span className="piece-name">{name.replace(/^from[-_]/, '').replace(/[-_]/g, ' ')}</span>
            <b style={{ color: value > 0 ? '#57D9A3' : '#D45B54' }}>{formatMoney(value, 2)}</b>
          </span>
        ))}
      {residual !== null && residual !== undefined && (
        <span
          className={`piece piece-residual${reconciles === false ? ' piece-broken' : ''}`}
          title={reconciles === false
            ? 'the components do not add back to the realised total'
            : 'what the model of where PnL comes from could not explain'}
        >
          <span className="piece-name">unexplained</span>
          <b>{formatMoney(residual, 2)}</b>
        </span>
      )}
    </div>
  )
}

function ClosedTrades({ closed }) {
  const { trades, summary, provenance } = closed

  return (
    <div className="trade-panel">
      <div className="trade-panel-head">
        <h3>Closed trades</h3>
        <div className="trade-panel-figures mono">
          {summary.count === 0 ? (
            <span className="unmeasured">nothing closed in the stretch read</span>
          ) : (
            <>
              <span>{summary.count} shown</span>
              <span className="sep">·</span>
              {/* Net, not gross. A board showing gross calls a fee-eaten loser a winner. */}
              <span>net <Pnl value={summary.net_pnl} digits={2} /></span>
              <span className="sep">·</span>
              <span>{formatPercent(summary.win_rate, 0)} won</span>
              <span className="sep">·</span>
              <span className="faint">{summary.fees_paid?.toFixed(2)} in fees</span>
            </>
          )}
          {summary.untrusted_count > 0 && (
            <>
              <span className="sep">·</span>
              <span className="unmeasured">
                {summary.untrusted_count} not counted — recorded before the entry-price fix
              </span>
            </>
          )}
        </div>
      </div>

      {trades.length === 0 ? (
        <div className="trade-empty">
          <b>NOTHING YET.</b> No round trip has closed in the stretch of journal read.
        </div>
      ) : (
        <div className="trade-scroll trade-scroll-tall">
          <table className="trade-table">
            <thead>
              <tr>
                <th>symbol</th><th>side</th>
                <th className="n">quantity</th><th className="n">entry</th><th className="n">exit</th>
                <th className="n">held</th><th className="n">best</th><th className="n">worst</th>
                <th className="n">fees</th><th className="n">net</th>
                <th>where the money came from</th>
              </tr>
            </thead>
            <tbody>
              {trades.map((t, index) => (
                <tr
                  key={`${t.closed_at_ns}-${t.symbol}-${index}`}
                  className={t.is_trusted === false ? 'row-untrusted' : undefined}
                  title={t.is_trusted === false
                    ? `not counted: ${t.trust_reason}. This row's entry price and quantity were computed by code that has since been fixed.`
                    : undefined}
                >
                  <td className="sym">
                    {t.symbol}
                    {/* Shown and marked, never hidden. Dropping the row would be
                        quietly editing the record; showing it unmarked would be
                        presenting fiction as measurement. */}
                    {t.is_trusted === false && <span className="chip-untrusted">before fix</span>}
                  </td>
                  <td style={{ color: t.direction === 'short' ? '#D48A54' : '#4DA3FF' }}>
                    {t.direction || '—'}
                  </td>
                  <td className="n mono"><Num value={t.quantity} /></td>
                  <td className="n mono"><Num value={t.entry_price} /></td>
                  <td className="n mono"><Num value={t.exit_price} /></td>
                  <td className="n mono faint">{formatDuration(t.holding_seconds) || '—'}</td>
                  <td className="n mono"><Pnl value={t.best_unrealised} /></td>
                  <td className="n mono"><Pnl value={t.worst_unrealised} /></td>
                  <td className="n mono faint">{t.fees_paid?.toFixed(3)}</td>
                  <td className="n mono"><Pnl value={t.net_pnl} /></td>
                  <td className="attribution"><Attribution attribution={t.attribution} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div className="trade-proof mono">
        {provenance.proof}
        {provenance.attribution_proof && <><br />{provenance.attribution_proof}</>}
      </div>
    </div>
  )
}

export default function TradingView({ trades, tradesError }) {
  if (tradesError && !trades) {
    return (
      <div className="banner warn">
        <b>Trades are not being read.</b> <code>/api/trades</code> is not answering, so
        nothing about what the bot holds can be shown here.
        <div className="banner-sub mono">{tradesError}</div>
      </div>
    )
  }
  if (!trades) return <div className="empty">reading the checkpoint and the journal…</div>

  return (
    <>
      {tradesError && (
        <div className="banner warn">
          <b>Stale.</b> The last poll failed; the rows below are the previous reading.
          <div className="banner-sub mono">{tradesError}</div>
        </div>
      )}
      <OpenPositions open={trades.open} />
      <ClosedTrades closed={trades.closed} />
    </>
  )
}
