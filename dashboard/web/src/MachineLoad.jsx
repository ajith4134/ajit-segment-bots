// MachineLoad — what this server is actually spending, live.
//
// Every number here is read from /proc on the call that produced it. Nothing is a
// configured capacity or a remembered figure, and the one number that cannot exist
// on a first poll — CPU, which is a rate — renders as NOT MEASURED rather than as
// zero (Rule 8).
//
// Shown on the same page as the parts because the two questions are one question:
// a part that is busy and a machine that is out of CPU are the same finding seen
// from opposite ends, and a board that separated them would make the operator join
// them by hand.
import { formatBytes, formatPercent, loadColour } from './useMachine.js'

function Gauge({ label, fraction, headline, detail, proof }) {
  const colour = loadColour(fraction)
  const measured = fraction !== null && fraction !== undefined
  return (
    <div className="gauge">
      <div className="gauge-top">
        <span className="gauge-label">{label}</span>
        <span className="gauge-headline mono" style={{ color: colour }}>
          {headline ?? <em className="unmeasured">not measured</em>}
        </span>
      </div>
      <div className="gauge-track">
        <div
          className="gauge-fill"
          style={{ width: measured ? `${Math.min(100, fraction * 100)}%` : 0, background: colour }}
        />
      </div>
      {detail && <div className="gauge-detail mono">{detail}</div>}
      {proof && <div className="gauge-proof mono">{proof}</div>}
    </div>
  )
}

export default function MachineLoad({ machine, machineError }) {
  if (machineError && !machine) {
    return (
      <div className="banner warn">
        <b>Server load is not being read.</b> <code>/api/machine</code> is not answering.
        <div className="banner-sub mono">{machineError}</div>
      </div>
    )
  }
  if (!machine) return <div className="empty">reading /proc…</div>

  const { cpu, memory, load, disk, parts } = machine
  const busiest = parts.filter((p) => p.is_measured)

  return (
    <section className="machine">
      <div className="machine-head">
        <h2>This server, right now</h2>
        {machineError && <span className="stale">stale — last poll failed</span>}
      </div>

      <div className="gauges">
        <Gauge
          label="CPU"
          fraction={cpu.busy_fraction}
          headline={cpu.is_measured ? formatPercent(cpu.busy_fraction) : null}
          detail={`${cpu.logical_cpus} logical cpus`}
          // Said out loud: counting iowait as busy is how a board reports 100%
          // on a machine that is asleep waiting for a disk.
          proof={cpu.is_measured
            ? `busy = everything but idle and iowait, over ${cpu.over_seconds?.toFixed(1)}s`
            : 'a percentage needs two readings of /proc/stat'}
        />
        <Gauge
          label="Memory"
          fraction={memory.used_fraction}
          headline={memory.is_measured ? formatPercent(memory.used_fraction) : null}
          detail={`${formatBytes(memory.used_bytes)} of ${formatBytes(memory.total_bytes)} · ${formatBytes(memory.available_bytes)} available`}
          // MemAvailable, not MemFree: free memory on a busy Linux box is near
          // zero by design because the kernel spends the rest on page cache.
          proof="used = total − MemAvailable, the kernel's own estimate"
        />
        <Gauge
          label="Load"
          fraction={load.per_cpu === null ? null : Math.min(1, load.per_cpu)}
          headline={load.one_minute?.toFixed(2)}
          detail={`${load.five_minutes?.toFixed(2)} over 5m · ${load.fifteen_minutes?.toFixed(2)} over 15m`}
          proof={load.per_cpu === null
            ? '/proc/loadavg'
            : `${load.per_cpu.toFixed(2)} per cpu — above 1.00 means work is queueing`}
        />
        <Gauge
          label="Disk"
          fraction={disk.used_fraction}
          headline={disk.is_measured ? formatPercent(disk.used_fraction) : null}
          detail={disk.is_measured
            ? `${formatBytes(disk.used_bytes)} of ${formatBytes(disk.total_bytes)} · ${formatBytes(disk.free_bytes)} free`
            : null}
          proof={disk.is_measured ? `where the tape and journals live: ${disk.path}` : disk.proof}
        />
      </div>

      {cpu.per_cpu?.length > 0 && (
        <div className="cores">
          <div className="cores-label mono">per core</div>
          <div className="cores-row">
            {cpu.per_cpu.map((core) => (
              <div className="core" key={core.cpu} title={`${core.cpu}: ${formatPercent(core.busy_fraction) ?? 'not measured'}`}>
                <div className="core-track">
                  <div
                    className="core-fill"
                    style={{
                      height: core.busy_fraction === null ? 0 : `${core.busy_fraction * 100}%`,
                      background: loadColour(core.busy_fraction),
                    }}
                  />
                </div>
                <div className="core-name mono">{core.cpu.replace('cpu', '')}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      <div className="part-cost">
        <div className="part-cost-head">
          <h3>What each part costs</h3>
          <span className="mono faint">
            {parts.length} process(es) the spine started and /proc still has
          </span>
        </div>
        {busiest.length === 0 ? (
          <div className="empty">
            no per-part CPU yet — that needs two readings, one poll apart
          </div>
        ) : (
          <div className="cost-table">
            <div className="cost-row cost-head mono">
              <span>part</span><span>cpu</span><span>memory</span><span>pid</span>
            </div>
            {parts.map((part) => (
              <div className="cost-row mono" key={part.part_id}>
                <span className="cost-name">{part.part_id}</span>
                <span className="cost-cpu" style={{ color: loadColour(part.cpu_fraction) }}>
                  {part.is_measured
                    ? formatPercent(part.cpu_fraction, 2)
                    : <em className="unmeasured">—</em>}
                </span>
                <span className="cost-mem">{formatBytes(part.resident_bytes)}</span>
                <span className="cost-pid faint">{part.pid}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  )
}
