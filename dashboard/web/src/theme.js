// One palette, used by every panel. Instrument-panel dark: the board is watched in a
// glance, so state has to read as colour before it reads as text.
export const T = {
  ground: '#0B0E13',
  panel: '#121721',
  sunk: '#080A0F',
  text: '#DCE3EC',
  muted: '#8B96A6',
  faint: '#5C6674',
  border: '#1D2530',
  accent: '#4DA3FF',
}

// A rung is a measured state, never an assertion. Order is the ladder.
export const RUNGS = {
  DECLARED: { color: '#39424F', label: 'DECLARED', meaning: 'in the blueprint, contract intact, no code' },
  IMPLEMENTED: { color: '#3D8FC4', label: 'IMPLEMENTED', meaning: 'a source file named for this part exists' },
  TESTED: { color: '#2FA37A', label: 'TESTED', meaning: 'a test file naming this part exists' },
  RUNNING: { color: '#57D9A3', label: 'RUNNING', meaning: 'the part reports a live heartbeat' },
  FAILING: { color: '#D45B54', label: 'FAILING', meaning: 'a probe ran and the part is broken' },
  PARTIAL: { color: '#C08A3E', label: 'PARTIAL', meaning: 'some parts built, some not' },
  'NOT MEASURED': { color: '#6E5B2E', label: 'NOT MEASURED', meaning: 'no probe could run' },
}

export const rung = (name) => RUNGS[name] || RUNGS['NOT MEASURED']
