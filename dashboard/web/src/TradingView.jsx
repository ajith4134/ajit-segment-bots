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
          {/* Notional and what it commits are two numbers, and only the second
              is what `maximum_capital_per_trade` bounds. Showing the first alone
              under the word "capital" made a 100 USDT ceiling read as breached
              4.5x while the gate was holding every trade to 99.99999996. */}
          <span>{open.capital_in?.toFixed(2)} USDT notional</span>
          <span className="sep">·</span>
          {open.capital_committed == null ? (
            <span className="unmeasured">committed NOT MEASURED</span>
          ) : (
            <>
              <span>{open.capital_committed.toFixed(2)} committed</span>
              {open.positions_with_a_known_leverage !== positions.length && (
                <>
                  <span className="sep">·</span>
                  <span className="unmeasured">
                    over {open.positions_with_a_known_leverage} of {positions.length} rows
                  </span>
                </>
              )}
            </>
          )}
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
                <th className="n">leverage</th>
                <th className="n">notional</th><th className="n">committed</th>
                <th className="n">unrealised</th>
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
                  {/* The leverage this position was opened at, weighted across
                      its entering fills. NOT MEASURED, never 1x, for a position
                      checkpointed before position-close-detector recorded it:
                      unlevered and unknown are different claims and only one of
                      them may render as a number (Rule 8). */}
                  <td className="n mono">
                    {p.leverage === null || p.leverage === undefined
                      ? <em className="unmeasured" title={p.leverage_proof}>not measured</em>
                      : <span title={p.leverage_proof}>{p.leverage.toFixed(2)}×</span>}
                  </td>
                  {/* Notional is what is in the market; committed is what it ties
                      up, which is notional over leverage and the number the
                      operator's per-trade ceiling actually bounds. Both, because
                      either one alone misleads about the other. */}
                  <td className="n mono" title={
                    p.entered_capital && p.entered_capital > p.capital_in
                      ? `the lots still held. ${p.entered_capital.toFixed(2)} has been entered `
                        + `across this round trip; the difference has already been sold back`
                      : 'the lots still held'
                  }>
                    {p.capital_in?.toFixed(2)}
                  </td>
                  <td className="n mono">
                    {p.capital_committed === null || p.capital_committed === undefined
                      ? <em className="unmeasured" title={p.leverage_proof}>not measured</em>
                      : <span title="notional over leverage -- what maximum_capital_per_trade bounds">
                          {p.capital_committed.toFixed(2)}
                        </span>}
                  </td>
                  <td className="n mono"><Pnl value={p.unrealised_pnl} digits={3} /></td>
                  {/* Peak and worst in one cell, best/worst, because they are one
                      fact -- the range this position has travelled -- and reading
                      them apart makes the reader do the subtraction. Never zero for
                      "we did not watch": the excursion's absence is what says
                      nothing was recorded, and that renders as not measured.

                      Read from peak-excursion-tracker's own checkpoint since
                      2026-08-29 (excursion_proof carries its age) rather than from
                      position-close-detector's relay, which only reaches disk on a
                      fill and could be stale by however long since the last one
                      anywhere -- measured live, 573 seconds -- while price_now and
                      unrealised are always fresh off the tape. Still widened by the
                      live unrealised, for the residual gap between this checkpoint's
                      write and the current instant: a stored worst of -4.91 on a
                      position sitting at -27.27 is an older reading, not a smaller
                      loss, and the live mark is evidence of the excursion in its own
                      right. */}
                  <td className="n mono">
                    <Excursion
                      best={p.best_unrealised}
                      worst={p.worst_unrealised}
                      live={p.unrealised_pnl}
                      proof={p.excursion_proof}
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
                          {/* How much of the position is actually behind it. A
                              stop is not a yes-or-no fact: AKEUSDT held 231,812
                              with a stop resting for 198.634 on 2026-08-28 and
                              this cell painted it protected. Shown only when it
                              covers less than the whole, and loud when it does. */}
                          {p.stop_covers_fraction != null && p.stop_covers_fraction < 1 && (
                            <span className="unprotected">
                              {' '}covers {(p.stop_covers_fraction * 100).toFixed(2)}%
                            </span>
                          )}
                        </span>}
                  </td>
                  <td className="n mono">
                    {p.target_price === null || p.target_price === undefined
                      ? <span className="faint" title={p.exit_proof}>—</span>
                      : <span title={p.exit_proof}><Num value={p.target_price} /></span>}
                  </td>
                  {/* Read from tail-trailing-exit-planner's own checkpoint, the file
                      that part restores from -- since 2026-08-29. Before it this trail
                      lived in that part's memory alone, the same defect stop-order-manager
                      had until 2026-08-26, and the column could only ever say `not built`
                      because nothing anywhere held the answer. `not tailgated` is a fact
                      about which bot opened this position, not a gap in this reading. */}
                  <td className="n mono">
                    {p.tail_stop_price === null || p.tail_stop_price === undefined
                      ? <em className="unmeasured" title={p.tailgating_proof}>
                          {p.tailgating_proof?.startsWith('NOT MEASURED')
                            ? 'not measured' : 'not tailgated'}
                        </em>
                      : <span title={p.tailgating_proof}>
                          <Num value={p.tail_stop_price} />
                          {p.tail_stop_distance_fraction != null && (
                            <span className="faint">
                              {' '}({(p.tail_stop_distance_fraction * 100).toFixed(2)}%)
                            </span>
                          )}
                        </span>}
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

function Excursion({ best, worst, live, proof }) {
  // The widest each side has been, counting the live mark as evidence. The
  // stored figure can only ever be as fresh as peak-excursion-tracker's own
  // checkpoint (see `proof`), and the instant between that write and now is a
  // real gap -- so the live unrealised widens the range it falls outside of,
  // rather than being treated as proof the stored figure was wrong. Shown as
  // best/worst in one cell.
  const hasStored = best !== null && best !== undefined
    && worst !== null && worst !== undefined
  const hasLive = live !== null && live !== undefined
  if (!hasStored && !hasLive) {
    return (
      <em className="unmeasured"
          title={proof || 'peak-excursion-tracker has recorded no excursion for this position, and the tape cannot mark it either'}>
        not measured
      </em>
    )
  }
  const peak = Math.max(hasStored ? best : -Infinity, hasLive ? live : -Infinity)
  const trough = Math.min(hasStored ? worst : Infinity, hasLive ? live : Infinity)
  const widened = hasStored && hasLive && (live > best || live < worst)
  const title = !hasStored
    ? 'no excursion recorded; this range is the live mark alone'
    : widened
      ? `the recorded excursion was ${best.toFixed(3)}/${worst.toFixed(3)} as of the last checkpoint write `
        + `(${proof || 'age not reported'}); widened here by the live mark, which is newer`
      : (proof || 'from the excursion peak-excursion-tracker recorded, alongside the lots')
  return (
    <span title={title} className={widened ? 'widened' : undefined}>
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
              {/* What was put in across these round trips, and what came back on
                  it. Only over the rows whose capital the journal recorded, and
                  it says so when that is not all of them. */}
              {summary.capital_in == null ? (
                <span className="unmeasured">capital in NOT MEASURED</span>
              ) : (
                <>
                  <span>{summary.capital_in.toFixed(2)} USDT in</span>
                  <span className="sep">·</span>
                  <span>
                    <Pnl value={summary.return_on_capital * 100} digits={2} />% on capital
                  </span>
                  {summary.trades_with_a_known_capital !== summary.count && (
                    <>
                      <span className="sep">·</span>
                      <span className="unmeasured">
                        over {summary.trades_with_a_known_capital} of {summary.count} rows
                      </span>
                    </>
                  )}
                </>
              )}
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
                <th className="n">capital in</th>
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
                  {/* What went in, at the price it went in at. Missing rather
                      than zero when the journal never recorded a price or a
                      quantity: a trade that cost nothing is a different claim
                      from a trade whose cost is unknown. */}
                  <td className="n mono">
                    {t.capital_in == null
                      ? <span className="unmeasured">—</span>
                      : t.capital_in.toFixed(2)}
                  </td>
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
