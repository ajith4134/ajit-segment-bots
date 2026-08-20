// Two builds from one source.
//
//   npm run build          -> dist/, served by part_health_api.py, polls /api/board  (LIVE)
//   npm run build:single   -> dist-single/index.html, everything inlined            (SNAPSHOT)
//
// The single-file build exists because the user cannot open a port on this server --
// only 22 is listening -- so the board reaches them as a published page instead. Same
// code either way; the only difference is where the data comes from, and the board
// says which mode it is in rather than letting a frozen page pass as live.
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { viteSingleFile } from 'vite-plugin-singlefile'

const single = process.env.SINGLEFILE === '1'

export default defineConfig({
  base: './',
  plugins: [react(), ...(single ? [viteSingleFile()] : [])],
  build: { target: 'es2020', assetsInlineLimit: single ? 100000000 : 4096 },
})
