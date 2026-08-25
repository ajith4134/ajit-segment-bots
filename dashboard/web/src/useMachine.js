// useMachine / useTrades — the two heavier polls, each on its own clock.
//
// Separate from useBoard and useActivity for the reason those two are separate from
// each other: they cost different amounts and answer different questions, and one
// request for all four would make the cheapest wait for the dearest.
//
// The machine poll is cheap (three files in /proc) but only yields a CPU figure on
// its second call, because a percentage is a difference between two readings.
// The trades poll is the dear one -- it marks each held symbol against the tape --
// so it runs slowly and says so.
import { useEffect, useRef, useState } from 'react'

const MACHINE_POLL_MS = 3000
const TRADES_POLL_MS = 15000

function usePolledJson(url, intervalMs) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    const load = () => {
      if (document.hidden) return
      fetch(url)
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`)
          return r.json()
        })
        .then((payload) => {
          if (!alive.current) return
          setData(payload)
          setError(null)
        })
        .catch((e) => {
          // Keep the last good payload and mark it, rather than blanking the view.
          if (alive.current) setError(String(e))
        })
    }
    load()
    const timer = setInterval(load, intervalMs)
    const onVisible = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive.current = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [url, intervalMs])

  return { data, error }
}

export function useMachine() {
  const { data, error } = usePolledJson('/api/machine', MACHINE_POLL_MS)
  return { machine: data, machineError: error }
}

export function useTrades() {
  const { data, error } = usePolledJson('/api/trades', TRADES_POLL_MS)
  return { trades: data, tradesError: error }
}

const GIB = 1024 ** 3
const MIB = 1024 ** 2

export function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) return null
  if (Math.abs(bytes) >= GIB) return `${(bytes / GIB).toFixed(bytes / GIB >= 100 ? 0 : 1)} GiB`
  if (Math.abs(bytes) >= MIB) return `${(bytes / MIB).toFixed(0)} MiB`
  return `${(bytes / 1024).toFixed(0)} KiB`
}

export function formatPercent(fraction, digits = 1) {
  if (fraction === null || fraction === undefined) return null
  return `${(fraction * 100).toFixed(digits)}%`
}

// One scale for every load bar on the page, so green and red mean the same thing
// whether they are drawn against CPU, memory or disk. Thresholds are about
// headroom, not about beauty: comfortable, working hard, and out of room.
export function loadColour(fraction) {
  if (fraction === null || fraction === undefined) return '#6E5B2E'
  if (fraction >= 0.9) return '#D45B54'
  if (fraction >= 0.7) return '#C08A3E'
  return '#57D9A3'
}

export function formatMoney(value, digits = 2) {
  if (value === null || value === undefined) return null
  const sign = value > 0 ? '+' : ''
  return `${sign}${value.toFixed(digits)}`
}

export function formatDuration(seconds) {
  if (seconds === null || seconds === undefined) return null
  if (seconds < 60) return `${seconds.toFixed(1)}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
  return `${Math.floor(seconds / 3600)}h ${Math.round((seconds % 3600) / 60)}m`
}
