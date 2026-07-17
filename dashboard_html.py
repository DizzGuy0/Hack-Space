"""The Ops Board single-page dashboard. Served by GET /dashboard; all data
comes from GET /api/state, polled every 8s. No external assets, no login,
caller IDs never appear (the API never sends them)."""

DASH_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>OpsBrain — Ops Board</title>
<style>
:root{color-scheme:light dark;
 --bg:#f4f3f1;--surface:#fcfcfb;--border:#e5e4e0;--ink:#0b0b0b;--ink2:#52514e;
 --accent:#2a78d6;--accent-bg:#e8f0fb;--crit:#d03b3b;--warn:#8a5a00;--good:#0b7a0b;
 --crit-bg:#fbe9e9;--warn-bg:#fdf3d9;--good-bg:#e7f6e7;--chip:#eceae6}
@media (prefers-color-scheme:dark){:root{
 --bg:#121211;--surface:#1a1a19;--border:#2c2b29;--ink:#ffffff;--ink2:#c3c2b7;
 --accent:#3987e5;--accent-bg:#1d2c40;--crit:#e66767;--warn:#fab219;--good:#4dc44d;
 --crit-bg:#3a2020;--warn-bg:#37310f;--good-bg:#1e3320;--chip:#26251f}}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);
 font:14.5px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.app{display:flex;min-height:100vh}
aside{width:210px;flex-shrink:0;border-right:1px solid var(--border);
 padding:20px 12px;position:sticky;top:0;height:100vh}
.logo{font-weight:700;font-size:16px;padding:0 10px}
.livewrap{display:flex;align-items:center;gap:6px;padding:2px 10px 0;
 margin-bottom:18px;color:var(--ink2);font-size:12px}
.dot{width:8px;height:8px;border-radius:50%;background:var(--ink2);opacity:.4}
.dot.on{background:var(--good);opacity:1}
nav button{display:flex;justify-content:space-between;width:100%;text-align:left;
 padding:8px 10px;margin:2px 0;border:none;background:none;color:var(--ink);
 font:inherit;border-radius:8px;cursor:pointer}
nav button:hover{background:var(--chip)}
nav button.act{background:var(--accent-bg);color:var(--accent);font-weight:600}
nav .cnt{color:var(--ink2);font-size:12px}
nav button.act .cnt{color:var(--accent)}
main{flex:1;padding:22px 26px;max-width:1000px}
.head{display:flex;gap:12px;align-items:center;margin-bottom:14px;flex-wrap:wrap}
.head h1{font-size:18px;flex:1}
.head input{background:var(--surface);border:1px solid var(--border);
 border-radius:8px;padding:7px 12px;color:var(--ink);font:inherit;width:230px}
.upd{color:var(--ink2);font-size:12px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));
 gap:10px;margin-bottom:12px}
.tile{background:var(--surface);border:1px solid var(--border);
 border-radius:10px;padding:12px 14px}
.tile .n{font-size:26px;font-weight:700}
.tile .l{color:var(--ink2);font-size:12px;margin-top:2px}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.card{background:var(--surface);border:1px solid var(--border);
 border-radius:10px;padding:14px;margin-bottom:10px}
.card h2{font-size:12px;font-weight:650;text-transform:uppercase;
 letter-spacing:.05em;color:var(--ink2);margin-bottom:6px}
.row{padding:8px 6px;border-top:1px solid var(--border);cursor:pointer;
 border-radius:6px}
.row:hover{background:var(--chip)}
.row:first-of-type{border-top:none}
.row .m{color:var(--ink2);font-size:12px;margin-top:3px;display:flex;gap:6px;
 flex-wrap:wrap;align-items:center}
.row.new{animation:flash 2.5s ease-out}
@keyframes flash{0%{background:var(--accent-bg)}100%{background:transparent}}
.chip{display:inline-block;border-radius:20px;padding:1px 8px;font-size:11px;
 font-weight:600;background:var(--chip);color:var(--ink2)}
.chip.high{background:var(--crit-bg);color:var(--crit)}
.chip.medium{background:var(--warn-bg);color:var(--warn)}
.chip.resolved{background:var(--good-bg);color:var(--good)}
.chip.f{background:var(--accent-bg);color:var(--accent)}
.detail{background:var(--bg);border:1px solid var(--border);border-radius:8px;
 padding:10px;margin:6px 0 2px;font-size:13px;cursor:default}
.detail div{margin:3px 0}
.detail b{color:var(--ink2);font-weight:600;font-size:11.5px;
 text-transform:uppercase;letter-spacing:.04em;margin-right:6px}
.bar{display:grid;grid-template-columns:1fr auto;gap:8px;align-items:center;
 padding:4px 0;font-size:13px}
.track{height:7px;border-radius:4px;background:var(--chip);margin-top:3px}
.fill{height:7px;border-radius:4px;background:var(--accent)}
.filters{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap;align-items:center}
.fbtn{border:1px solid var(--border);background:var(--surface);color:var(--ink2);
 border-radius:20px;padding:3px 11px;font-size:12px;font-family:inherit;cursor:pointer}
.fbtn.act{background:var(--accent-bg);color:var(--accent);border-color:transparent}
.empty{color:var(--ink2);padding:8px 6px;font-size:13.5px}
@media(max-width:720px){
 .app{flex-direction:column}
 aside{width:100%;height:auto;position:static;display:flex;align-items:center;
  gap:8px;overflow-x:auto;padding:10px 12px;border-right:none;
  border-bottom:1px solid var(--border)}
 nav{display:flex;gap:2px}
 nav button{white-space:nowrap;width:auto;gap:5px}
 .logo{padding:0}
 .livewrap{margin:0;padding:0}
 .cols{grid-template-columns:1fr}}
</style></head><body>
<div class="app">
<aside>
 <div class="logo">OpsBrain</div>
 <div class="livewrap"><span class="dot" id="dot"></span><span id="livetxt">connecting…</span></div>
 <nav id="nav"></nav>
</aside>
<main>
 <div class="head"><h1 id="title">Overview</h1>
  <input id="q" placeholder="Search the log…"><span class="upd" id="upd"></span></div>
 <div id="view"></div>
</main>
</div>
<script>
const VIEWS=[["overview","Overview"],["cases","Cases"],["follow","Follow-ups"],
             ["know","Knowledge"],["team","Team"]];
let S=null,view="overview",q="",fUrg="",fSt="",open={},known=null,cnt={};
const $=id=>document.getElementById(id);
const esc=s=>(s||"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function ago(ts){if(!ts)return"";const d=(Date.now()-new Date(ts))/864e5|0;
 return d<=0?"today":d==1?"1d ago":d+"d ago"}
function isOpen(e){return e.status=="confirmed"&&e.type!="completion"&&e.type!="knowledge"}
function match(e){if(!q)return true;
 return (e.summary+" "+e.ask+" "+e.category+" "+e.raw).toLowerCase().includes(q)}
function nav(){$("nav").innerHTML=VIEWS.map(([k,l])=>{
 const c=k in cnt?cnt[k]:"";
 return `<button class="${k==view?"act":""}" onclick="go('${k}')">${l}<span class="cnt">${c}</span></button>`}).join("")}
function go(k){view=k;render()}
function tog(id){open[id]=!open[id];render()}
function setU(v){fUrg=v;render()}
function setS(v){fSt=v;render()}
function tile(n,l){return `<div class="tile"><div class="n">${n}</div><div class="l">${l}</div></div>`}
function emp(t){return `<div class="empty">${t}</div>`}
function row(e){
 const chips=`<span class="chip ${e.urgency}">${e.urgency||"—"}</span>`
  +(e.status=="resolved"?`<span class="chip resolved">resolved</span>`:"")
  +(e.follow_up&&e.status!="resolved"?`<span class="chip f">follow-up</span>`:"")
  +`<span>${esc(e.category)}</span><span>·</span><span>${esc(e.type)}</span><span>·</span><span>${ago(e.ts)}</span>`;
 const d=open[e.id]?`<div class="detail" onclick="event.stopPropagation()">
   ${e.ask?`<div><b>Ask</b>${esc(e.ask)}</div>`:""}
   ${e.impact?`<div><b>Stated impact</b>${esc(e.impact)}</div>`:""}
   ${e.resolution?`<div><b>Resolution</b>${esc(e.resolution)}</div>`:""}
   ${e.raw?`<div><b>Original message</b>${esc(e.raw)}</div>`:""}</div>`:"";
 const isNew=known&&!known.has(e.id);
 return `<div class="row ${isNew?"new":""}" onclick="tog('${e.id}')">${esc(e.summary)}<div class="m">${chips}</div>${d}</div>`}
function vOverview(){
 const es=S.entries,op=es.filter(isOpen),high=op.filter(e=>e.urgency=="high");
 const cats={};op.forEach(e=>{const c=e.category||"Other";cats[c]=(cats[c]||0)+1});
 const mx=Math.max(1,...Object.values(cats),1);
 return `<div class="tiles">${tile(op.length,"Open cases")}${tile(high.length,"High urgency")}
  ${tile(es.filter(e=>e.status=="resolved").length,"Resolved")}${tile(S.team.length,"Active members")}</div>
 <div class="cols">
 <div class="card"><h2>High urgency — needs action</h2>${high.filter(match).map(row).join("")||emp("Nothing urgent open.")}</div>
 <div class="card"><h2>Open cases by category</h2>${Object.entries(cats).sort((a,b)=>b[1]-a[1]).map(([c,n])=>
  `<div class="bar"><div>${esc(c)}<div class="track"><div class="fill" style="width:${Math.round(100*n/mx)}%"></div></div></div><div>${n}</div></div>`).join("")||emp("No open cases.")}</div>
 </div>
 <div class="card"><h2>Latest activity</h2>${es.filter(match).slice(0,6).map(row).join("")||emp("Nothing yet.")}</div>`}
function vCases(){
 let es=S.entries.filter(e=>e.type!="knowledge").filter(match);
 if(fUrg)es=es.filter(e=>e.urgency==fUrg);
 if(fSt)es=es.filter(e=>e.status==fSt);
 const fb=(lbl,val,cur,fn)=>`<button class="fbtn ${cur==val?"act":""}" onclick="${fn}('${cur==val?"":val}')">${lbl}</button>`;
 return `<div class="filters">
  ${fb("high","high",fUrg,"setU")}${fb("medium","medium",fUrg,"setU")}${fb("low","low",fUrg,"setU")}
  <span style="width:10px"></span>
  ${fb("open","confirmed",fSt,"setS")}${fb("resolved","resolved",fSt,"setS")}</div>
 <div class="card">${es.map(row).join("")||emp("No matching cases.")}</div>`}
function vFollow(){
 const es=S.entries.filter(e=>isOpen(e)&&e.follow_up).filter(match);
 return `<div class="card"><h2>To check</h2>${es.map(row).join("")||emp("No follow-ups flagged.")}</div>`}
function vKnow(){
 const es=S.entries.filter(e=>e.type=="knowledge").filter(match);
 return `<div class="card"><h2>Standing notes</h2>${es.map(row).join("")||emp("No standing notes yet — messages like “from next time, everyone check X” land here.")}</div>`}
function vTeam(){
 return `<div class="card"><h2>Active roster</h2>${S.team.map(t=>
  `<div class="row" style="cursor:default">${esc(t.name)}<div class="m"><span>${esc(t.role)}</span>${t.alerts?`<span>·</span><span>alerts: ${esc(t.alerts)}</span>`:""}</div></div>`).join("")}</div>
 <div class="card"><h2>Routing rules</h2><div class="empty">Each member's “alerts” lists the categories they get pinged for; high urgency always alerts leads. The data steward edits these in Airtable → Roster → alerts_for.</div></div>`}
function render(){
 if(!S)return;
 cnt={cases:S.entries.filter(isOpen).length,
      follow:S.entries.filter(e=>isOpen(e)&&e.follow_up).length,
      know:S.entries.filter(e=>e.type=="knowledge").length,
      team:S.team.length};
 nav();
 $("title").textContent=VIEWS.find(v=>v[0]==view)[1];
 $("view").innerHTML=({overview:vOverview,cases:vCases,follow:vFollow,know:vKnow,team:vTeam})[view]();}
async function tick(){
 try{
  const r=await fetch("/api/state");if(!r.ok)throw 0;
  const s=await r.json();
  known=S?new Set(S.entries.map(e=>e.id)):null;
  S=s;
  $("dot").classList.add("on");$("livetxt").textContent="live";
  $("upd").textContent="updated "+new Date().toLocaleTimeString();
  render();
 }catch(e){$("dot").classList.remove("on");$("livetxt").textContent="reconnecting…"}}
$("q").addEventListener("input",e=>{q=e.target.value.toLowerCase();render()});
tick();setInterval(tick,8000);
</script></body></html>"""
