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
