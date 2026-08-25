// useActivity — the live half of the board: what each part is doing right now.
//
// A second, independent poll rather than more fields on /api/board, because the two
// answer different questions at different costs. The board measures the filesystem
// for every part's rung and is expensive; activity reads one small file the running
// system already writes. Merging them into one request would force the cheap fact to
// wait for the expensive one, and the live numbers would crawl.
//
// Independent also means independently broken: when this fails the shape of the
// board stays on screen and only the live column goes dark, which is the honest
// picture -- the blueprint is still known, the behaviour is not.
//
// A frozen snapshot never polls. It carries whatever activity was measured at the
// moment it was built, and says so, because a snapshot rendered as live is exactly
// the failure the board exists to prevent.
import { useEffect, useRef, useState } from 'react'

const POLL_MS = 3000

export function useActivity() {
  const frozen = typeof window !== 'undefined' ? window.__ACTIVITY_SNAPSHOT__ : null
  const [data, setData] = useState(frozen || null)
  const [error, setError] = useState(null)
  const alive = useRef(true)

  useEffect(() => {
    if (frozen) return undefined

    alive.current = true
    const load = () => {
      if (document.hidden) return
      fetch('/api/activity')
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
          if (alive.current) setError(String(e))
        })
    }
    load()
    const timer = setInterval(load, POLL_MS)
    const onVisible = () => { if (!document.hidden) load() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      alive.current = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [frozen])

  return { activity: data, activityError: error, isFrozen: Boolean(frozen) }
}

// A rate only exists once two tables have been observed. Until then the reading is
// NOT MEASURED and must render as its own state -- never as zero, which is a
// different fact, and never as blank, which tells the reader nothing.
export function formatRate(perSecond) {
  if (perSecond === null || perSecond === undefined) return null
  const magnitude = Math.abs(perSecond)
  if (magnitude === 0) return '0/s'
  if (magnitude >= 100) return `${Math.round(perSecond).toLocaleString()}/s`
  if (magnitude >= 1) return `${perSecond.toFixed(1)}/s`
  if (magnitude >= 0.01) return `${perSecond.toFixed(2)}/s`
  return `${perSecond.toExponential(1)}/s`
}

export function formatCount(value) {
  if (value === null || value === undefined) return '—'
  if (Number.isInteger(value)) return value.toLocaleString()
  if (Math.abs(value) >= 1000) return Math.round(value).toLocaleString()
  return value.toFixed(4).replace(/0+$/, '').replace(/\.$/, '')
}

// A counter name is written for the code that keeps it. On a board it is read at a
// glance, so the underscores become spaces -- and nothing else changes, because a
// prettier paraphrase would stop matching the name the part actually publishes.
export function readableCounter(name) {
  return name.replace(/_/g, ' ')
}
