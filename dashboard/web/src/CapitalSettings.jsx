// CapitalSettings — the numbers the operator sets, and the guarded way to change them.
//
// RL-040, RL-041, RL-051 and RL-053 are the operator's to decide: the paper
// balance, what one trade may commit at least and at most, and the leverage
// ceiling the bot chooses under. RL-055 says the board shows them and when they
// last changed, and `docs/settings-schema.md` names the only legal source for the
// second half — capital-settings-change-recorder's journal, never the file's mtime
// and never git log.
//
// **The password is held in memory and nowhere else.** Not localStorage, not a
// cookie: this page is served from a public URL, and a password kept on the
// viewer's machine outlives the person who typed it. Reloading the page asks
// again, which is the correct amount of inconvenience for a form that can move
// capital limits.
//
// **A refusal is shown exactly as the server phrased it**, with which guard
// stopped it. An operator told only "no" cannot tell a typo from a lockout from a
// contradiction — and the operator is the person most likely to be refused.
//
// **Nothing here decides what may be edited.** The server marks each setting, and
// this renders the mark. Two lists of what is editable would drift, and the one
// that drifted would be this one.
import { useEffect, useRef, useState } from 'react'

const POLL_MS = 10000

export function useSettings() {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [reloadKey, setReloadKey] = useState(0)
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
  }, [reloadKey])

  return { settings: data, settingsError: error, reloadSettings: () => setReloadKey((k) => k + 1) }
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

function ValueCell({ setting, password, onChanged }) {
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)

  if (!setting.is_editable) {
    return (
      <div className="edit-cell">
        <b className="mono">{String(setting.value)}</b>
        <span className="locked" title={setting.not_editable_reason}>locked</span>
      </div>
    )
  }

  const submit = () => {
    if (!draft.trim() || busy) return
    setBusy(true)
    setResult(null)
    fetch('/api/settings/change', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        password,
        scope: setting.scope,
        setting: setting.name,
        value: draft.trim(),
      }),
    })
      .then((r) => r.json())
      .then((answer) => {
        setResult(answer)
        setBusy(false)
        if (answer.accepted) { setDraft(''); onChanged() }
      })
      .catch((e) => { setResult({ accepted: false, guard: 'network', reason: String(e) }); setBusy(false) })
  }

  return (
    <div className="edit-cell">
      <b className="mono">{String(setting.value)}</b>
      <input
        className="edit-input mono"
        value={draft}
        placeholder="new"
        disabled={!password || busy}
        title={password ? '' : 'enter the board password above first'}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => { if (e.key === 'Enter') submit() }}
      />
      <button className="edit-save" disabled={!password || !draft.trim() || busy} onClick={submit}>
        {busy ? '…' : 'set'}
      </button>
      {result && (
        <div className={`edit-result${result.accepted ? ' edit-ok' : ''}`}>
          {/* The server's own words, and which guard stopped it. A caller told
              only "no" cannot tell a typo from a lockout from a contradiction. */}
          <span className="edit-guard">{result.guard}</span> {result.reason}
          {(result.faults || []).map((fault) => (
            <div className="edit-fault" key={fault}>{fault}</div>
          ))}
        </div>
      )}
    </div>
  )
}

export default function CapitalSettings({ settings, settingsError, reloadSettings }) {
  const [password, setPassword] = useState('')

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
        <b>These are yours to set.</b> Every value below is what the parts are actually
        reading right now, and “last changed” comes from{' '}
        <code>capital-settings-change-recorder</code>’s journal — never the file’s
        timestamp, which says when it was <em>saved</em> and not when anything began
        acting on it. A change is refused unless it passes the same checks{' '}
        <code>capital-settings-validator</code> applies, because a settings file that
        contradicts itself zeroes the risk limit rather than trading on a guess.
      </div>

      <div className="password-bar">
        <label htmlFor="board-password">board password</label>
        <input
          id="board-password"
          className="password-input mono"
          type="password"
          value={password}
          placeholder="required to change anything"
          onChange={(e) => setPassword(e.target.value)}
        />
        <span className="password-note">
          {/* Not localStorage, not a cookie. This page is public, and a password
              kept on the viewer's machine outlives the person who typed it. */}
          held in this tab only — reloading asks again
        </span>
      </div>

      {settings.scopes.map((scope) => (
        <div className="trade-panel" key={scope.scope}>
          <div className="trade-panel-head">
            <h3>{scope.scope === 'main-account' ? 'Main account' : `Segment — ${scope.scope}`}</h3>
            <div className="trade-panel-figures mono">
              <span>{scope.settings.length} setting(s)</span>
              <span className="sep">·</span>
              <span>{scope.settings.filter((s) => s.is_editable).length} changeable here</span>
            </div>
          </div>

          {!scope.ok ? (
            <div className="trade-empty mono">{scope.proof}</div>
          ) : (
            <div className="trade-scroll">
              <table className="trade-table">
                <thead>
                  <tr>
                    <th>setting</th><th>value</th><th>unit</th>
                    <th>last changed</th><th>why it is set that way</th>
                  </tr>
                </thead>
                <tbody>
                  {scope.settings.map((setting) => (
                    <tr key={setting.name} className={setting.moves_real_money ? 'row-real-money' : undefined}>
                      <td className="sym">
                        {READABLE(setting.name)}
                        {setting.moves_real_money && (
                          <span className="chip-real-money" title={setting.not_editable_reason}>
                            decides real money
                          </span>
                        )}
                      </td>
                      <td>
                        <ValueCell setting={setting} password={password} onChanged={reloadSettings} />
                      </td>
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
