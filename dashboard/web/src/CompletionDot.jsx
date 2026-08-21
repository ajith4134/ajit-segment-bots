// CompletionDot — RL-070's second channel, alongside colour, for whether a part
// or block is measured complete (dashboard/completion.py's part_is_measured_complete
// / block_completion, the one place that predicate is decided).
//
// A bare red/green pair is the worst choice for colour-blind readers -- roughly 8%
// of men cannot reliably separate those two hues, and this board exists to tell
// someone at a glance what is left to build. So shape carries the state too: a
// filled disc for complete, a hollow ring for unfinished, which still reads with
// the colour stripped out entirely. One component so a part's dot and a block's
// dot are drawn identically and can never drift apart visually.
import { completionDot } from './theme.js'

export default function CompletionDot({ isComplete }) {
  const d = completionDot(isComplete)
  return (
    <span
      className={`completion-dot ${isComplete ? 'complete' : 'unfinished'}`}
      style={{ color: d.color }}
      title={d.label}
      aria-label={d.label}
    >
      {d.glyph}
    </span>
  )
}
