#!/usr/bin/env python3
"""Generate dashboard/wiring-explorer.html: every connection between parts, explorable.

The mermaid planes on the status board are right for the block-level story, but
at 25 blocks x 425 labelled edges (1 363 between parts) a node-link drawing is a
hairball. This page is the other tool:

  Matrix   25 x 25 blocks, cell = how many data types flow producer -> consumer.
           Click a cell: the types and the exact part pairs behind it.
  Focus    one part in the centre, everything that feeds it on the left and
           everything it feeds on the right, grouped by block, wires labelled.
           Click any part to recentre.
  Types    one data type: who writes it, who reads it.

Nothing in it is drawn by hand. Edges come from render_blueprint.derive_edges,
so the page shows exactly the wiring the contract checker judges; the checker's
verdict and the generation time are stamped on the page (Rule 8).

    python3 dashboard/build_wiring_explorer.py
"""
from __future__ import annotations

import datetime as dt
import html
import json
from pathlib import Path

from render_blueprint import (
    HEALTH_TYPE,
    derive_edges,
    find_contract_violations,
    load_feature_registry,
)

OUT = Path(__file__).resolve().parent / "wiring-explorer.html"


def build_payload() -> dict:
    registry = load_feature_registry()
    edges = [e for e in derive_edges(registry) if e[2] != HEALTH_TYPE]
    violations = find_contract_violations(registry)
    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "check": {"violations": violations, "command": "python3 dashboard/check_contracts.py"},
        "categories": [
            {
                "id": c["id"],
                "name": c.get("name", c["id"]),
                "scope": c.get("scope", ""),
                "peer_group": c.get("peer_group", ""),
                "summary": c.get("summary", ""),
            }
            for c in registry.categories
        ],
        "types": [
            {"id": t["id"], "name": t.get("name", t["id"]), "description": t.get("description", "")}
            for t in registry.data_types
            if t["id"] != HEALTH_TYPE
        ],
        "parts": [
            {
                "id": f["id"],
                "name": f.get("name", f["id"]),
                "role": f.get("role", ""),
                "category": f.get("category", ""),
                "consumes": [t for t in f.get("consumes", []) if t != HEALTH_TYPE],
                "produces": [t for t in f.get("produces", []) if t != HEALTH_TYPE],
            }
            for f in registry.features
        ],
        "edges": edges,
        "health_edges": sum(1 for e in derive_edges(registry) if e[2] == HEALTH_TYPE),
    }


PAGE = r"""<title>Segment Bot Wiring</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root{
  --paper:#F3F1EC; --paper-2:#E9E6DF; --ink:#1E2128; --ink-2:#4B5060; --ink-3:#8A8F9C;
  --rule:#D6D2C8; --copper:#B0581F; --copper-soft:rgba(176,88,31,.14); --copper-ink:#7E3E13;
  --in:#2F6F9F; --out:#B0581F; --bad:#B42D2D; --good:#2E7D4F;
  --cell-0:transparent; --cell-max:#B0581F;
  --mono:"IBM Plex Mono",ui-monospace,SFMono-Regular,Menlo,monospace;
  --sans:"IBM Plex Sans",system-ui,-apple-system,"Segoe UI",sans-serif;
}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
  --paper:#171A20; --paper-2:#1F232B; --ink:#E8E5DE; --ink-2:#B3B0A8; --ink-3:#7C7F89;
  --rule:#2C313C; --copper:#D3823F; --copper-soft:rgba(211,130,63,.18); --copper-ink:#E9A56C;
  --in:#6FA8D6; --out:#D3823F; --bad:#E06060; --good:#5DB67F; --cell-max:#D3823F;
}}
:root[data-theme="dark"]{
  --paper:#171A20; --paper-2:#1F232B; --ink:#E8E5DE; --ink-2:#B3B0A8; --ink-3:#7C7F89;
  --rule:#2C313C; --copper:#D3823F; --copper-soft:rgba(211,130,63,.18); --copper-ink:#E9A56C;
  --in:#6FA8D6; --out:#D3823F; --bad:#E06060; --good:#5DB67F; --cell-max:#D3823F;
}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:14px/1.45 var(--sans);min-height:100vh}
a{color:var(--copper-ink)}
button{font:inherit;color:inherit;background:none;border:1px solid var(--rule);border-radius:3px;padding:4px 10px;cursor:pointer}
button:focus-visible,input:focus-visible,.part:focus-visible{outline:2px solid var(--copper);outline-offset:2px}
button.on{background:var(--copper);border-color:var(--copper);color:#fff}
header{display:flex;flex-wrap:wrap;gap:10px 24px;align-items:baseline;padding:16px 22px 12px;border-bottom:1px solid var(--rule)}
header h1{margin:0;font:600 19px/1.2 var(--sans);letter-spacing:-.01em}
.stamp{font:12px var(--mono);color:var(--ink-3)}
.verdict{font:500 12px var(--mono);padding:2px 8px;border-radius:3px;border:1px solid}
.verdict.ok{color:var(--good);border-color:var(--good)}
.verdict.bad{color:var(--bad);border-color:var(--bad)}
.layout{display:grid;grid-template-columns:270px minmax(0,1fr);min-height:calc(100vh - 58px)}
@media (max-width:820px){.layout{grid-template-columns:1fr}}
aside{border-right:1px solid var(--rule);padding:14px 16px;display:flex;flex-direction:column;gap:14px}
aside input[type="search"]{width:100%;padding:7px 9px;border:1px solid var(--rule);border-radius:3px;background:var(--paper-2);color:var(--ink);font:13px var(--mono)}
.eyebrow{font:500 11px var(--mono);letter-spacing:.08em;text-transform:uppercase;color:var(--ink-3);margin:0 0 6px}
.blocks{display:flex;flex-direction:column;gap:2px;max-height:52vh;overflow:auto}
.blocks label{display:flex;gap:8px;align-items:center;font-size:13px;padding:2px 4px;border-radius:3px;cursor:pointer;text-align:left}
.blocks label span:not(.n):not(.peer){flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.blocks label input{flex:none;margin:0}
.blocks label:hover{background:var(--paper-2)}
.blocks .n{margin-left:auto;font:12px var(--mono);color:var(--ink-3)}
.peer{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--copper)}
.results{display:flex;flex-direction:column;gap:1px;max-height:30vh;overflow:auto}
.part{display:block;width:100%;text-align:left;border:0;padding:4px 6px;border-radius:3px;font:13px var(--mono)}
.part:hover{background:var(--copper-soft)}
.part small{display:block;font:11px var(--sans);color:var(--ink-3)}
main{padding:16px 22px;min-width:0}
.tabs{display:flex;gap:6px;margin-bottom:14px}
.view{display:none}.view.on{display:block}
.note{color:var(--ink-2);max-width:68ch;margin:0 0 12px}
.scroll{overflow-x:auto}
table.matrix{border-collapse:collapse;font:11px var(--mono)}
table.matrix th{font-weight:500;color:var(--ink-2);text-align:left;padding:2px 6px;white-space:nowrap}
table.matrix thead th{height:130px;vertical-align:bottom;padding:0}
table.matrix thead th div{transform:rotate(-60deg) translate(8px,0);transform-origin:left bottom;width:24px;white-space:nowrap}
table.matrix td{width:26px;height:26px;border:1px solid var(--rule);text-align:center;cursor:pointer;color:var(--ink-2)}
table.matrix td.diag{background-image:repeating-linear-gradient(45deg,transparent 0 4px,var(--rule) 4px 5px)}
table.matrix td:hover{outline:2px solid var(--copper);outline-offset:-2px}
table.matrix td.sel{outline:2px solid var(--ink);outline-offset:-2px}
table.matrix tr:hover th{color:var(--copper-ink)}
.detail{margin-top:14px;border-top:1px solid var(--rule);padding-top:12px}
.detail h3{margin:0 0 8px;font:600 14px var(--sans)}
.wire{font:12px var(--mono);padding:3px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}
.wire .t{color:var(--copper-ink)}
.wire .arrow{color:var(--ink-3)}
.typebadge{display:inline-block;font:12px var(--mono);padding:1px 7px;border-radius:3px;background:var(--copper-soft);color:var(--copper-ink);cursor:pointer;border:0}
svg.ego{display:block;max-width:100%;font:12px var(--mono)}
svg.ego .node rect{fill:var(--paper-2);stroke:var(--rule)}
svg.ego .node.centre rect{fill:var(--copper-soft);stroke:var(--copper);stroke-width:1.5}
svg.ego .node text{fill:var(--ink)}
svg.ego .node .blk{fill:var(--ink-3);font-size:10px}
svg.ego .node{cursor:pointer}
svg.ego .node:hover rect{stroke:var(--copper)}
svg.ego path.w{fill:none;stroke-width:1.2;opacity:.75}
svg.ego path.w.in{stroke:var(--in)}
svg.ego path.w.out{stroke:var(--out)}
svg.ego text.lbl{font-size:10px;fill:var(--ink-2)}
svg.ego rect.lblbg{fill:var(--paper);opacity:.9}
.centre-card{border:1px solid var(--rule);border-left:3px solid var(--copper);padding:10px 14px;margin-bottom:14px;max-width:74ch}
.centre-card h2{margin:0 0 2px;font:600 16px var(--sans)}
.centre-card .id{font:12px var(--mono);color:var(--ink-3)}
.centre-card p{margin:6px 0 0;color:var(--ink-2)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:24px}
@media (max-width:820px){.cols{grid-template-columns:1fr}}
.cols h3{margin:0 0 6px;font:600 13px var(--sans)}
.cols h3 .k{font:12px var(--mono);color:var(--ink-3);font-weight:400}
.group{margin-bottom:10px}
.group .g{font:500 11px var(--mono);letter-spacing:.06em;text-transform:uppercase;color:var(--ink-3);margin:6px 0 2px}
.legend{display:flex;gap:16px;font:12px var(--mono);color:var(--ink-2);margin-bottom:10px}
.legend i{display:inline-block;width:18px;height:2px;vertical-align:middle;margin-right:6px}
.empty{color:var(--ink-3);font-style:italic}
</style>

<header>
  <h1>Segment Bot Wiring</h1>
  <span class="stamp" id="counts"></span>
  <span class="verdict" id="verdict"></span>
  <span class="stamp" id="stamp"></span>
</header>

<div class="layout">
<aside>
  <div>
    <p class="eyebrow">Find a part or data type</p>
    <input id="q" type="search" placeholder="type to search…" autocomplete="off">
    <div class="results" id="results"></div>
  </div>
  <div>
    <p class="eyebrow">Blocks <button id="allblocks" style="float:right;padding:0 6px;font-size:11px">all / none</button></p>
    <div class="blocks" id="blocks"></div>
  </div>
</aside>

<main>
  <div class="tabs">
    <button data-view="matrix" class="on">Matrix</button>
    <button data-view="focus">Part focus</button>
    <button data-view="type">Data type</button>
  </div>

  <section class="view on" id="v-matrix">
    <p class="note">Rows write, columns read. A cell counts the distinct data types that flow from the row block into the column block; hatched diagonal is wiring inside one block. Click a cell for the exact part pairs. Peer blocks (•) are separate at runtime and must stay empty between each other — R-03.</p>
    <div class="scroll" id="matrix"></div>
    <div class="detail" id="matrix-detail"><span class="empty">Click a cell.</span></div>
  </section>

  <section class="view" id="v-focus">
    <div class="legend"><span><i style="background:var(--in)"></i>feeds this part</span><span><i style="background:var(--out)"></i>this part feeds</span></div>
    <div id="focus"><span class="empty">Pick a part from the search box, the matrix, or a data type.</span></div>
  </section>

  <section class="view" id="v-type">
    <div id="type"><span class="empty">Pick a data type from the search box or any wire label.</span></div>
  </section>
</main>
</div>

<script id="data" type="application/json">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent);
const cat = Object.fromEntries(D.categories.map(c=>[c.id,c]));
const part = Object.fromEntries(D.parts.map(p=>[p.id,p]));
const type = Object.fromEntries(D.types.map(t=>[t.id,t]));
const E = D.edges; // [producer, consumer, type]
const enabled = new Set(D.categories.map(c=>c.id));
const esc = s => String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

// header
document.getElementById('counts').textContent =
  `${D.parts.length} parts · ${D.categories.length} blocks · ${D.types.length} data types · ${E.length} wires (+${D.health_edges} health, hidden)`;
const v = document.getElementById('verdict');
v.textContent = D.check.violations.length ? `${D.check.violations.length} contract violations` : 'all contracts hold';
v.className = 'verdict ' + (D.check.violations.length ? 'bad' : 'ok');
v.title = D.check.command;
document.getElementById('stamp').textContent = 'measured ' + D.generated_at;

// tabs
function show(view){
  document.querySelectorAll('.tabs button').forEach(b=>b.classList.toggle('on',b.dataset.view===view));
  document.querySelectorAll('.view').forEach(s=>s.classList.toggle('on',s.id==='v-'+view));
}
document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>show(b.dataset.view));

// blocks filter
const blocksEl = document.getElementById('blocks');
const partCount = {};
D.parts.forEach(p=>partCount[p.category]=(partCount[p.category]||0)+1);
D.categories.forEach(c=>{
  const l = document.createElement('label');
  l.innerHTML = `<input type="checkbox" checked data-id="${esc(c.id)}"> ${c.peer_group?'<span class="peer" title="peer group: '+esc(c.peer_group)+'"></span>':''}<span>${esc(c.name)}</span><span class="n">${partCount[c.id]||0}</span>`;
  l.querySelector('input').onchange = e => { e.target.checked ? enabled.add(c.id) : enabled.delete(c.id); renderMatrix(); if(focusId) renderFocus(focusId); };
  blocksEl.appendChild(l);
});
document.getElementById('allblocks').onclick = () => {
  const all = enabled.size === D.categories.length;
  D.categories.forEach(c=>{ all ? enabled.delete(c.id) : enabled.add(c.id); });
  blocksEl.querySelectorAll('input').forEach(i=>i.checked=!all);
  renderMatrix(); if(focusId) renderFocus(focusId);
};

// search
const q = document.getElementById('q'), results = document.getElementById('results');
q.oninput = () => {
  const s = q.value.trim().toLowerCase(); results.innerHTML='';
  if(!s) return;
  const hits = [];
  D.parts.forEach(p=>{ if((p.id+' '+p.name+' '+p.role).toLowerCase().includes(s)) hits.push({k:'part',o:p}); });
  D.types.forEach(t=>{ if((t.id+' '+t.name).toLowerCase().includes(s)) hits.push({k:'type',o:t}); });
  hits.slice(0,40).forEach(h=>{
    const b = document.createElement('button'); b.className='part';
    b.innerHTML = h.k==='part' ? `${esc(h.o.id)}<small>${esc(cat[h.o.category]?.name||'')}</small>` : `<span class="t">${esc(h.o.id)}</span><small>data type</small>`;
    b.onclick = () => h.k==='part' ? goPart(h.o.id) : goType(h.o.id);
    results.appendChild(b);
  });
  if(!hits.length) results.innerHTML='<span class="empty" style="padding:4px 6px">nothing matches</span>';
};

// ---------- matrix
const M = {}; // producerCat -> consumerCat -> {types:Set, pairs:[]}
E.forEach(([p,c,t])=>{
  const a = part[p].category, b = part[c].category;
  ((M[a] ||= {})[b] ||= {types:new Set(), pairs:[]});
  M[a][b].types.add(t); M[a][b].pairs.push([p,c,t]);
});
let selCell = null;
function renderMatrix(){
  const cats = D.categories.filter(c=>enabled.has(c.id));
  let max = 1; cats.forEach(a=>cats.forEach(b=>{ const m=M[a.id]?.[b.id]; if(m&&a.id!==b.id) max=Math.max(max,m.types.size); }));
  let h = '<table class="matrix"><thead><tr><th></th>' + cats.map(c=>`<th><div>${esc(c.name)}</div></th>`).join('') + '</tr></thead><tbody>';
  cats.forEach(a=>{
    h += `<tr><th>${a.peer_group?'• ':''}${esc(a.name)}</th>`;
    cats.forEach(b=>{
      const m = M[a.id]?.[b.id]; const n = m ? m.types.size : 0;
      const diag = a.id===b.id;
      const peerBreach = !diag && n && a.peer_group && a.peer_group===b.peer_group;
      const bg = n && !diag ? `background:color-mix(in srgb, var(--cell-max) ${Math.round(15+70*n/max)}%, transparent)` : '';
      const sel = selCell && selCell[0]===a.id && selCell[1]===b.id ? ' sel' : '';
      h += `<td class="${diag?'diag':''}${sel}" style="${peerBreach?'background:var(--bad);color:#fff':bg}" data-a="${esc(a.id)}" data-b="${esc(b.id)}" title="${esc(a.name)} → ${esc(b.name)}: ${n} type${n===1?'':'s'}${peerBreach?' — R-03 BREACH':''}">${n||''}</td>`;
    });
    h += '</tr>';
  });
  h += '</tbody></table>';
  const el = document.getElementById('matrix'); el.innerHTML = h;
  el.querySelectorAll('td').forEach(td=>td.onclick=()=>{ selCell=[td.dataset.a,td.dataset.b]; renderMatrix(); renderCell(td.dataset.a,td.dataset.b); });
}
function renderCell(a,b){
  const m = M[a]?.[b]; const el = document.getElementById('matrix-detail');
  if(!m){ el.innerHTML = `<h3>${esc(cat[a].name)} → ${esc(cat[b].name)}</h3><span class="empty">no wire${a!==b&&cat[a].peer_group&&cat[a].peer_group===cat[b].peer_group?' — correct: peer blocks never wire into each other (R-03)':''}</span>`; return; }
  const byType = {}; m.pairs.forEach(([p,c,t])=>(byType[t] ||= []).push([p,c]));
  let h = `<h3>${esc(cat[a].name)} → ${esc(cat[b].name)} <span class="k">· ${m.types.size} types, ${m.pairs.length} wires</span></h3>`;
  Object.keys(byType).sort().forEach(t=>{
    h += `<div class="group"><button class="typebadge" data-t="${esc(t)}">${esc(t)}</button>`;
    byType[t].forEach(([p,c])=> h += `<div class="wire"><button class="part" style="width:auto;padding:0 2px" data-p="${esc(p)}">${esc(p)}</button><span class="arrow">→</span><button class="part" style="width:auto;padding:0 2px" data-p="${esc(c)}">${esc(c)}</button></div>`);
    h += '</div>';
  });
  el.innerHTML = h; wireClicks(el);
}
function wireClicks(el){
  el.querySelectorAll('[data-p]').forEach(b=>b.onclick=()=>goPart(b.dataset.p));
  el.querySelectorAll('[data-t]').forEach(b=>b.onclick=()=>goType(b.dataset.t));
}

// ---------- focus (ego graph)
let focusId = null;
function goPart(id){ focusId=id; show('focus'); renderFocus(id); }
function renderFocus(id){
  const p = part[id]; const el = document.getElementById('focus');
  const ins = E.filter(e=>e[1]===id && enabled.has(part[e[0]].category));
  const outs = E.filter(e=>e[0]===id && enabled.has(part[e[1]].category));
  // group by block then part
  const grp = (edges, idx) => { const g={}; edges.forEach(e=>{ const pid=e[idx]; ((g[part[pid].category] ||= {})[pid] ||= []).push(e[2]); }); return g; };
  const gi = grp(ins,0), go = grp(outs,1);
  const rows = g => Object.keys(g).sort().flatMap(c=>Object.keys(g[c]).sort().map(pid=>({pid,cat:c,types:[...new Set(g[c][pid])]})));
  const L = rows(gi), R = rows(go);
  const rowH = 36, W = 1100, colW = 290, cx = W/2, n = Math.max(L.length, R.length, 1), H = Math.max(140, n*rowH+40);
  const y = (i,total) => H/2 + (i - (total-1)/2)*rowH;
  let s = `<svg class="ego" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="wiring around ${esc(id)}">`;
  const short = s => s.length>22 ? s.slice(0,21)+'…' : s;
  const node = (x,yy,pid,cls='') => `<g class="node ${cls}" data-p="${esc(pid)}" tabindex="0"><title>${esc(pid)} · ${esc(cat[part[pid].category]?.name||'')}</title><rect x="${x}" y="${yy-15}" width="${colW}" height="30" rx="3"/><text x="${x+8}" y="${yy-1}">${esc(pid)}</text><text class="blk" x="${x+8}" y="${yy+11}">${esc(short(cat[part[pid].category]?.name||''))}</text></g>`;
  const wire = (x1,y1,x2,y2,cls,label) => { const mx=(x1+x2)/2; const lx = cls==='in' ? x1+14 : x2-14, ly = cls==='in' ? y1 : y2, anchor = cls==='in' ? 'start' : 'end'; const bx = cls==='in' ? lx-3 : lx-label.length*6-3; return `<path class="w ${cls}" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/><rect class="lblbg" x="${bx}" y="${ly-16}" width="${label.length*6+6}" height="13" rx="2"/><text class="lbl" x="${lx}" y="${ly-6}" text-anchor="${anchor}" data-t="${esc(label)}">${esc(label)}</text>`; };
  L.forEach((r,i)=>{ const yy=y(i,L.length); s += wire(20+colW, yy, cx-colW/2, H/2, 'in', r.types.join(', ')); s += node(20,yy,r.pid); });
  R.forEach((r,i)=>{ const yy=y(i,R.length); s += wire(cx+colW/2, H/2, W-20-colW, yy, 'out', r.types.join(', ')); s += node(W-20-colW,yy,r.pid); });
  s += node(cx-colW/2, H/2, id, 'centre') + '</svg>';
  el.innerHTML = `<div class="centre-card"><h2>${esc(p.name)}</h2><div class="id">${esc(p.id)} · ${esc(cat[p.category]?.name||'')} · ${esc(cat[p.category]?.scope||'')}</div><p>${esc(p.role)}</p>
    <p><span class="k">reads</span> ${p.consumes.map(t=>`<button class="typebadge" data-t="${esc(t)}">${esc(t)}</button>`).join(' ')||'<span class="empty">nothing</span>'}<br><span class="k">writes</span> ${p.produces.map(t=>`<button class="typebadge" data-t="${esc(t)}">${esc(t)}</button>`).join(' ')||'<span class="empty">nothing</span>'}</p></div>
    <div class="scroll">${s}</div>
    <p class="note" style="margin-top:8px">${ins.length} wire${ins.length===1?'':'s'} in from ${L.length} part${L.length===1?'':'s'} · ${outs.length} out to ${R.length} part${R.length===1?'':'s'}${enabled.size<D.categories.length?' · filtered to the ticked blocks':''}</p>`;
  wireClicks(el);
  el.querySelectorAll('.node').forEach(g=>{ g.onclick=()=>goPart(g.dataset.p); g.onkeydown=e=>{ if(e.key==='Enter') goPart(g.dataset.p); }; });
  el.querySelectorAll('text.lbl').forEach(t=>{ t.style.cursor='pointer'; t.onclick=()=>goType(t.dataset.t.split(', ')[0]); });
}

// ---------- type view
function goType(id){ show('type'); renderType(id); }
function renderType(id){
  const t = type[id]; const el = document.getElementById('type');
  if(!t){ el.innerHTML=`<span class="empty">unknown type ${esc(id)}</span>`; return; }
  const prod = D.parts.filter(p=>p.produces.includes(id)), cons = D.parts.filter(p=>p.consumes.includes(id));
  const list = ps => { if(!ps.length) return '<span class="empty">none</span>'; const g={}; ps.forEach(p=>(g[p.category] ||= []).push(p)); return Object.keys(g).sort().map(c=>`<div class="group"><div class="g">${esc(cat[c]?.name||c)}</div>${g[c].map(p=>`<button class="part" data-p="${esc(p.id)}">${esc(p.id)}<small>${esc(p.role)}</small></button>`).join('')}</div>`).join(''); };
  el.innerHTML = `<div class="centre-card"><h2>${esc(t.name)}</h2><div class="id">${esc(t.id)}</div><p>${esc(t.description)}</p></div>
    <div class="cols"><div><h3>Written by <span class="k">${prod.length}</span></h3>${list(prod)}</div><div><h3>Read by <span class="k">${cons.length}</span></h3>${list(cons)}</div></div>`;
  wireClicks(el);
}

renderMatrix();
</script>
"""


def main() -> None:
    payload = build_payload()
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    OUT.write_text(PAGE.replace("__DATA__", data))
    print(
        f"wrote {OUT}  ({len(payload['parts'])} parts, {len(payload['edges'])} wires, "
        f"{len(payload['check']['violations'])} violations)"
    )


if __name__ == "__main__":
    main()
