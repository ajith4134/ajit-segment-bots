// useBoard — the only thing in the app that touches the network.
//
// Modelled on the polling hook in the user's own dashboard, including the lesson its
// comments record: refresh each endpoint INDEPENDENTLY and merge by key, because one
// batched request makes the whole board flicker empty whenever any single endpoint is
// slow. There is one endpoint here today; the shape is kept so adding the second costs
// nothing.
//
// Two modes, and the board is told which one it is in:
//   live      served by part_health_api.py, polls /api/board
//   snapshot  published as a page, reads the payload frozen into the build
//
// A snapshot presented as live is a lie with a timestamp available, so the mode is
// carried in the payload and rendered, never inferred.
import { useEffect, useRef, useState } from 'react'

const POLL_MS = 5000

export function useBoard() {
  const frozen = typeof window !== 'undefined' ? window.__BOARD_SNAPSHOT__ : null
  const [data, setData] = useState(frozen || null)
  const [error, setError] = useState(null)
  const [fetchedAt, setFetchedAt] = useState(frozen ? frozen.generated_at : null)
  const alive = useRef(true)

  useEffect(() => {
    // A frozen build never polls: there is no API behind it, and retrying forever
    // would only fill the console with failures that mean nothing.
    if (frozen) return undefined

    alive.current = true
    const load = () => {
      if (document.hidden) return
      fetch('/api/board')
        .then((r) => {
          if (!r.ok) throw new Error(`HTTP ${r.status}`)
          return r.json()
        })
        .then((payload) => {
          if (!alive.current) return
          setData(payload)
          setFetchedAt(payload.generated_at)
          setError(null)
        })
        .catch((e) => {
          // Keep the last good payload on screen and mark it stale, rather than
          // blanking the board. A board that empties on one failed poll teaches
          // people to distrust it.
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

  return { data, error, fetchedAt, mode: data?.mode || (frozen ? 'snapshot' : 'live') }
}
