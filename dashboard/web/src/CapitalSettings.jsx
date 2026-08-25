// CapitalSettings — the numbers the operator sets, and when each last moved.
//
// RL-040, RL-041, RL-051 and RL-053 are the operator's to decide: the paper
// balance, what one trade may commit at least and at most, and the leverage
// ceiling the bot chooses under. RL-055 says the board shows them and when they
// last changed, and `docs/settings-schema.md` names the only legal source for the
// second half — capital-settings-change-recorder's journal, never the file's
// mtime and never git log.
//
// Read-only today, and it says so rather than showing inputs that do nothing. The
// write path is password-gated, rate-limited, validated by
// capital-settings-validator and journalled, and a board that offered a text box
// before any of that existed would be teaching the operator a gesture that is not
// yet safe.
//
// A setting whose change has never been journalled reads NOT MEASURED, not
// "never changed". Those are different facts and the journal is what separates
// them.
import { useEffect, useRef, useState } from 'react'

const POLL_MS = 10000

export function useSettings() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    const load = () => {
      if (document.hidden) return
      fetch('/api/settings')
        .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
        .then((payload) => { if (alive.current) { setData(payload); setError(null) } })
        .catch((e) => { if (alive.current) setError(String(e)) })
    }
    load()
    const timer = setInterval(load, POLL_MS)
    return () => { alive.current = false; clearInterval(timer) }
  }, [])

  return { settings: data, settingsError: error }
}

function whenChanged(setting) {
  if (!setting.last_changed_is_measured) {
    return (
      <em className="unmeasured" title="capital-settings-change-recorder has journalled no change for this setting">
        no change journalled
      </em>
    )
  }
  const seconds = setting.last_changed_age_seconds
  const ago = seconds < 3600
    ? `${Math.round(seconds / 60)}m ago`
    : seconds < 86400
      ? `${(seconds / 3600).toFixed(1)}h ago`
      : `${(seconds / 86400).toFixed(1)}d ago`
  return (
    <span title={`was ${setting.previous_value}, changed ${setting.change_count} time(s)`}>
      {ago} <span className="faint">from {String(setting.previous_value)}</span>
    </span>
  )
}

const READABLE = (name) => name.replace(/_/g, ' ')

export default function CapitalSettings({ settings, settingsError }) {
  if (settingsError && !settings) {
    return (
      <div className="banner warn">
        <b>Settings are not being read.</b> <code>/api/settings</code> is not answering.
        <div className="banner-sub mono">{settingsError}</div>
      </div>
    )
  }
  if (!settings) return <div className="empty">reading the settings and the change journal…</div>

  return (
    <>
      <div className="banner">
        <b>These are yours to set, and this board only shows them.</b> They are edited
        in the settings file on the server. Every value below is what the parts are
        actually reading right now, and “last changed” comes from{' '}
        <code>capital-settings-change-recorder</code>’s journal — never the file’s
        timestamp, which says when it was <em>saved</em> and not when anything began
        acting on it.
      </div>

      {settings.scopes.map((scope) => (
        <div className="trade-panel" key={scope.scope}>
          <div className="trade-panel-head">
            <h3>{scope.scope === 'main-account' ? 'Main account' : `Segment — ${scope.scope}`}</h3>
            <div className="trade-panel-figures mono">
              <span>{scope.settings.length} setting(s)</span>
            </div>
          </div>

          {!scope.ok ? (
            <div className="trade-empty mono">{scope.proof}</div>
          ) : (
            <div className="trade-scroll">
              <table className="trade-table">
                <thead>
                  <tr>
                    <th>setting</th><th className="n">value</th><th>unit</th>
                    <th>last changed</th><th>why it is set that way</th>
                  </tr>
                </thead>
                <tbody>
                  {scope.settings.map((setting) => (
                    <tr key={setting.name} className={setting.moves_real_money ? 'row-real-money' : undefined}>
                      <td className="sym">
                        {READABLE(setting.name)}
                        {/* RL-005: paper first. A setting that can put real money on
                            the market is not the same kind of thing as one that
                            bounds a paper trade, and the board says so. */}
                        {setting.moves_real_money && (
                          <span className="chip-real-money">decides real money</span>
                        )}
                      </td>
                      <td className="n mono"><b>{String(setting.value)}</b></td>
                      <td className="faint mono">{setting.unit}</td>
                      <td className="mono">{whenChanged(setting)}</td>
                      <td className="setting-note">{setting.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      ))}

      <p className="live-proof mono">{settings.journal.proof}</p>
      <p className="live-proof mono">{settings.editable_reason}</p>
    </>
  )
}
