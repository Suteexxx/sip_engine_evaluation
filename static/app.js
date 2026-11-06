const $=s=>document.querySelector(s),E=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const SRC={play_store:'Google Play',app_store:'App Store',youtube:'YouTube comments',web:'Open web'};
const SITE={'apps.apple.com':'App Store','play.google.com':'Google Play','youtube.com':'YouTube'};
const CLS={sat:'Satisfied',neu:'Neutral',dis:'Not satisfied'};
const site=h=>SITE[h]||h, safeUrl=u=>/^https?:\/\//i.test(u||'')?u:'';
let R=null,ITEMS={};
const nb=c=>c&&c.n?`<button type="button" class="nb" data-c="${E(c.c)}">${c.k} of ${c.n} <b>${c.p}%</b></button>`:'<span class="mute">–</span>';
const rangeTxt=c=>c&&c.n?`likely range ${c.lo}–${c.hi}%`:'';
const sitesTxt=ids=>{const m={};ids.forEach(i=>{const s=ITEMS[i];if(s)m[site(s.site)]=(m[site(s.site)]||0)+1});return Object.entries(m).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`${E(k)} ×${v}`).join(' · ')};
const para=t=>String(t||'').split(/\n+/).filter(x=>x.trim()).map(x=>`<p>${E(x)}</p>`).join('');

$('#f').onsubmit=async e=>{e.preventDefault();$('#go').disabled=true;$('#out').hidden=true;$('#log').innerHTML='';
 const say=(m,c='')=>$('#log').insertAdjacentHTML('beforeend',`<div class="${c}">${E(m)}</div>`);
 let id;try{const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:$('#q').value,reuse:$('#reuse').checked,code:$('#code').value})});
  const b=await r.json().catch(()=>({}));if(!r.ok||!b.id){say('ERROR: '+(b.error||('server answered '+r.status+'. If this is the first visit in a while the server may be waking up; wait a minute and try again.')),'err');$('#go').disabled=false;return}id=b.id}
 catch(x){say('ERROR: could not reach the server ('+x.message+')','err');$('#go').disabled=false;return}
 let seen=0;const t=setInterval(async()=>{let j;try{j=await (await fetch('/api/job/'+id)).json()}catch(x){return}
  j.log.slice(seen).forEach(l=>$('#log').insertAdjacentHTML('beforeend',`<div class="${l.startsWith('ERROR')?'err':''}">${E(l)}</div>`));seen=j.log.length;$('#log').scrollTop=1e9;
  if(j.status!=='running'){clearInterval(t);$('#go').disabled=false;if(j.status==='done'){show(j.result,id);$('#out').scrollIntoView({behavior:'smooth'})}}},1500)};

/* ---- evidence drawer: shows the original posts behind any number */
function itemHtml(i){const u=safeUrl(i.url),ev=i.ev&&i.text.toLowerCase().indexOf(i.ev.toLowerCase());
 const txt=i.ev&&ev>=0?E(i.text.slice(0,ev))+'<mark>'+E(i.text.slice(ev,ev+i.ev.length))+'</mark>'+E(i.text.slice(ev+i.ev.length)):E(i.text);
 const tags=[i.cls?CLS[i.cls]:(i.dup?'repeat of another post, not counted':'dropped (not about SIPs)'),i.nt&&('neutral: '+i.nt.replace('_',' ')),i.neg.length&&('complains: '+i.neg.join(', ')),i.pos.length&&('praises: '+i.pos.join(', ')),i.out!=='na'&&i.rel&&('says: '+i.out),i.age!=='unknown'&&('age '+i.age+(i.ab==='cue'?' (inferred)':'')),i.via&&('found via '+i.via+(i.how?', '+i.how:''))].filter(Boolean).map(E).join(' · ');
 return `<article class="ev"><header>${u?`<a href="${E(u)}" target="_blank" rel="noopener noreferrer">${E(site(i.site))}</a>`:E(site(i.site))} · ${E(i.date)}${i.rating?` · ${i.rating}★`:''}${i.cls?`<span class="pill ${i.cls}">${CLS[i.cls]}</span>`:''} · ${E(i.id)}</header><p>${txt}</p><small>${tags}</small></article>`};
function openList(title,sub,ids){const items=ids.map(i=>ITEMS[i]).filter(Boolean);
 $('#dt').textContent=title;$('#ds').innerHTML=sub;$('#db').innerHTML=items.length?items.slice(0,120).map(itemHtml).join('')+(items.length>120?`<p class="mute">Showing 120 of ${items.length}.</p>`:''):'<p class="mute">No posts.</p>';
 const d=$('#dlg');d.showModal?d.showModal():d.setAttribute('open','')}
function openClaim(cid){const c=R.stats.claims[cid];if(!c)return;
 openList(c.label,`<b>${c.k} of ${c.n}</b> (${c.p}%${c.n?`, ${rangeTxt(c)}`:''}). Counted out of: ${E(c.den)}.<br>The ${c.k} counted posts come from: ${sitesTxt(c.ids)||'–'}.<br>Highlighted words are the phrase the labeller used as its reason.`,c.ids)}
document.addEventListener('click',e=>{const b=e.target.closest('[data-c],[data-ids]');if(b&&R){b.dataset.c?openClaim(b.dataset.c):openList(b.dataset.title||'Posts','',b.dataset.ids.split(',').filter(Boolean))}});
$('#dx').onclick=()=>{const d=$('#dlg');d.close?d.close():d.removeAttribute('open')};

let GID='';function show(r,id){R=r;GID=id;const s=r.stats,o=s.overall,p=r.report;ITEMS=Object.fromEntries(s.items.map(i=>[i.id,i]));$('#out').hidden=false;$('#pdf').href='/api/pdf/'+id;
 /* 1. question + plain-language answer */
 $('#head').innerHTML=`<p class="tag">Your question</p><p class="q">${E(r.query)}</p><h2 style="font-size:32px">${E(p.headline)}</h2>
  <div class="card answer"><h3>The short answer</h3>${para(p.answer)}${p.key_points.length?`<ul>${p.key_points.map(x=>`<li>${E(x)}</li>`).join('')}</ul>`:''}</div>${s.warnings.map(w=>`<div class="warn">${E(w)}</div>`).join('')}`;
 /* 2. headline numbers: the three groups add up to 100% */
 const k3=['sat','neu','dis'].map(k=>`<div class="card"><b class="big">${o[k].p}%</b><span>${CLS[k].toLowerCase()}</span><small><button type="button" class="nb" data-c="${E(o[k].c)}">${o[k].k} of ${o[k].n} posts</button></small></div>`).join('');
 $('#kpi').className='grid5';$('#kpi').innerHTML=`<div class="card"><b class="big">${s.n_collected}</b><span>posts collected</span></div><div class="card"><b class="big">${s.n_relevant}</b><span>about SIPs, analysed</span><small>${s.n_analyzed-s.n_relevant} dropped (off-topic or repeated)</small></div>`+k3;
 /* 3. how posts are sorted, with the neutral box opened up */
 const drop=s.items.filter(i=>!i.rel).map(i=>i.id);
 $('#cls').innerHTML=`<div class="defs"><div class="card"><h3>Satisfied <span class="pill sat">${o.sat.k}</span></h3><p>A 4 or 5 star review. If there are no stars: the writer is clearly happy with the product.</p></div>
  <div class="card"><h3>Neutral <span class="pill neu">${o.neu.k}</span></h3><p>A 3 star review. If there are no stars: the writer does not clearly lean either way. The five kinds are listed below.</p></div>
  <div class="card"><h3>Not satisfied <span class="pill dis">${o.dis.k}</span></h3><p>A 1 or 2 star review. If there are no stars: the writer is clearly unhappy.</p></div></div>
  <p class="sub">The star rating is used whenever there is one. Posts that are not about SIPs or investing, and repeats of another post, are dropped first${drop.length?` (<button type="button" class="nb" data-ids="${drop.join(',')}" data-title="Dropped posts">see the ${drop.length} dropped</button>)`:''}.</p>
  <h3 class="h3">What is inside "Neutral" (${s.neutral.n} posts)</h3>${s.neutral.types.map(t=>`<div class="ntrow"><div>${E(t.label)} ${nb(t.claim)}</div><div class="track"><i data-w="${t.claim.p}"></i></div><div class="what">${E(t.what)}${t.example?` <button type="button" class="nb" data-ids="${t.example}" data-title="Example: ${E(t.label)}">see an example</button>`:''}</div></div>`).join('')}`;
 /* 4. by source + sites */
 $('#src').innerHTML=`<div class="legend"><span><i style="background:var(--lime)"></i>satisfied</span><span><i style="background:var(--slate)"></i>neutral</span><span><i style="background:var(--cobalt)"></i>not satisfied</span></div>`+Object.entries(s.by_source).map(([k,v])=>`<div class="srow"><div>${E(SRC[k]||k)} <span class="tag">${v.n} posts</span></div><div class="stack"><i class="s" data-w="${v.sat.p}"></i><i class="n" data-w="${v.neu.p}"></i><i class="d" data-w="${v.dis.p}"></i></div><small>satisfied ${nb(v.sat)} · neutral ${nb(v.neu)} · not satisfied ${nb(v.dis)}</small></div>`).join('');
 $('#sites').innerHTML=`<table><tr><th>Site</th><th>Posts read</th><th>About SIPs</th><th>Satisfied</th><th>Neutral</th><th>Not satisfied</th><th>Found via</th></tr>${s.sites.map(x=>`<tr><td>${E(site(x.site))}</td><td class="m">${x.analysed}</td><td class="m">${x.relevant?`<button type="button" class="nb" data-c="${E(x.claim.c)}">${x.relevant}</button>`:0}</td><td class="m">${x.sat}</td><td class="m">${x.neu}</td><td class="m">${x.dis}</td><td class="m">${E(x.via.join(', '))}</td></tr>`).join('')}</table>`;
 /* 5. complaints and praise */
 const neg=s.aspects.filter(a=>a.neg.k);$('#asp').innerHTML=neg.map(a=>`<div class="arow"><span>${E(a.label)}</span><div class="track"><i data-w="${a.neg.p}"></i></div><span class="num">${nb(a.neg)}</span></div>`).join('')||'<p class="sub">No complaint topics detected.</p>';
 const pos=s.aspects.filter(a=>a.pos.k).sort((a,b)=>b.pos.k-a.pos.k);$('#praise').innerHTML=pos.map(a=>`<div class="arow"><span>${E(a.label)}</span><div class="track"><i data-w="${a.pos.p}"></i></div><span class="num">${nb(a.pos)}</span></div>`).join('')||'<p class="sub">No praise topics detected.</p>';
 /* 6. success vs failure */
 const sf=s.success_failure,c=sf.counts;
 $('#sf').innerHTML=`<p class="sub">Only a few people say what they actually did with their SIP. We compare the ones who say they <b>left</b> (paused or stopped) with the ones who say they <b>stay</b> (continue).</p>
  <div class="grid5"><div class="card"><b class="big">${sf.stayed}</b><span>say they continue</span></div><div class="card"><b class="big">${sf.left}</b><span>say they paused or stopped</span><small>paused ${c.paused||0} · stopped ${c.stopped||0}</small></div><div class="card"><b class="big">${sf.unsaid}</b><span>do not say</span></div></div>
  ${sf.reliable?'':`<div class="note">Fewer than 5 people on at least one side, so read the table as hints, not as results.</div>`}
  <p class="sub" style="margin-top:14px">People who left, among everyone who said what they did: ${nb(sf.share_left)}</p>
  <div class="scroll"><table><tr><th>Topic</th><th>Complained about by people who <b>left</b> (${sf.left})</th><th>Complained about by people who <b>stayed</b> (${sf.stayed})</th><th>What it suggests</th></tr>${sf.rows.filter(x=>x.left.k||x.stay.k).map(x=>`<tr><td>${E(x.label)}</td><td class="m">${nb(x.left)}</td><td class="m">${nb(x.stay)}</td><td>${E(x.verdict)}</td></tr>`).join('')||'<tr><td colspan="4">Nobody who left or stayed complained about a topic.</td></tr>'}</table></div>
  <h3 class="h3">What people who stay praise</h3><div class="scroll"><table><tr><th>Topic</th><th>Praised by people who stayed (${sf.stayed})</th></tr>${sf.rows.filter(x=>x.praise.k).sort((a,b)=>b.praise.k-a.praise.k).map(x=>`<tr><td>${E(x.label)}</td><td class="m">${nb(x.praise)}</td></tr>`).join('')||'<tr><td colspan="2">No praise topics among people who stay.</td></tr>'}</table></div>`;
 /* 7. segments */
 const sg=s.segments,GN={age:'Age (band edges: 27 is in 18-27, 40 is in 27-40, 55 is in 40-55)',exp:'Experience',inc:'Income'};let last='';
 const rows=sg.rows.map(x=>{let h='';if(x.dim!==last){last=x.dim;h=`<tr class="grp"><td colspan="6">${E(GN[x.dim])}</td></tr>`}
  return h+`<tr><td>${E(x.label)}${x.tiny?'<small>fewer than 5 posts: ignore the percentages</small>':x.indicative?'<small>fewer than 30 posts: only a hint</small>':''}</td><td class="m">${x.n}</td><td class="m">${nb(x.sat)}</td><td class="m">${nb(x.neu)}</td><td class="m">${nb(x.dis)}</td><td class="m">${nb(x.left)}</td></tr>`}).join('');
 const cv=sg.coverage,rel=s.n_relevant;
 $('#seg').innerHTML=`<p class="sub">Age placed for ${cv.age.placed} of ${rel} posts (${cv.age.stated} stated by the writer, ${cv.age.cue} inferred from life-stage cues such as "student", "first job", "my kids"). Experience placed for ${cv.exp.placed}, income for ${cv.inc.placed}. People who say nothing are left out of these rows.</p>
  <table><tr><th>Group</th><th>Posts</th><th>Satisfied</th><th>Neutral</th><th>Not satisfied</th><th>Left (of those who said)</th></tr>${rows||'<tr><td colspan="6">Nobody gave enough detail to place them in a group.</td></tr>'}</table>`;
 /* 8. findings in plain words, each with where it comes from */
 const based=f=>{const ids=[...new Set(f.claims.flatMap(c=>(s.claims[c]||{ids:[]}).ids))];
  return `<div class="based">${ids.length?`Based on ${ids.length} posts: ${sitesTxt(ids)}<br>`:''}${f.claims.map(c=>`<button type="button" class="chipbtn" data-c="${E(c)}">${E(s.claims[c].label)}: ${s.claims[c].k} of ${s.claims[c].n}</button>`).join('')}${f.quotes.length?`<button type="button" class="chipbtn" data-ids="${f.quotes.join(',')}" data-title="Example posts">example posts</button>`:''}</div>`};
 $('#nar').innerHTML=`<div class="grid">${p.findings.map(f=>`<div class="card"><h3>${E(f.title)}</h3><p>${E(f.detail)}</p>${based(f)}</div>`).join('')}</div>
  <div class="grid" style="margin-top:12px"><div class="card"><h3>What keeps people investing</h3><ul>${p.keeps_people.map(x=>`<li>${E(x)}</li>`).join('')}</ul></div><div class="card"><h3>Why people leave</h3><ul>${p.why_people_leave.map(x=>`<li>${E(x)}</li>`).join('')}</ul></div></div>`;
 /* 9. method, checks, number trace, evidence explorer */
 const a=r.audit,cr=r.credits||{},tc=a.number_trace||[];
 $('#meth').innerHTML=`<p class="sub">Tools called: ${r.trace.map(t=>`${E(t.tool)} (${t.items})`).join(', ')||'saved data reused'}. ${cr.reused?'Saved data was reused, so no new Firecrawl credits were spent.':cr.budget!=null?`Firecrawl credits used: <b>${cr.credits}</b> of a ${cr.budget} budget (${cr.searches||0} searches, ${cr.scrapes||0} pages read, ${cr.cache_hits||0} answered free from cache, profile "${E(cr.profile)}").`:''}</p>
  <h3 class="h3">Checks the agent ran on itself</h3>${s.checks.map(c=>`<div class="chk"><span class="${c.ok?'ok':'no'}">${c.ok?'✓':'!'}</span> ${E(c.name)}: <span class="mute">${E(c.detail)}</span></div>`).join('')}
  <div class="chk"><span class="${a.unverified_numbers.length?'no':'ok'}">${a.unverified_numbers.length?'!':'✓'}</span> Every number in the written summary matches a computed figure${a.unverified_numbers.length?`. <b>Not matched:</b> ${E(a.unverified_numbers.join(', '))}`:''}${a.guardrail_violation?'. Guardrail words detected':''}</div>
  <h3 class="h3">Where each number in the summary comes from</h3>
  ${tc.length?`<div class="scroll"><table><tr><th>Number in text</th><th>Matches</th></tr>${tc.map(t=>`<tr><td class="m">${E(t.text)}</td><td>${t.claims.map(c=>`<button type="button" class="chipbtn" data-c="${E(c)}">${E(R.stats.claims[c].label)}: ${R.stats.claims[c].k} of ${R.stats.claims[c].n}</button><small>${sitesTxt(R.stats.claims[c].ids)}</small>`).join('')}</td></tr>`).join('')}</table></div>`:'<p class="sub">The summary quotes no percentages.</p>'}
  <details><summary>What the report writer was allowed to use (${r.facts.length} facts, ${r.quotes.length} quotes)</summary><ul>${r.facts.map(f=>`<li><b>${E(f.id)}</b> ${E(f.text)}</li>`).join('')}</ul></details>
  <details><summary>Limitations</summary><ul>${p.limitations.map(x=>`<li>${E(x)}</li>`).join('')}</ul><p class="sub">Public reviews are a self-selected sample, so results describe people who write reviews, not all users.</p></details>
  <h3 class="h3">Golden set for the evidence log</h3><p class="sub">Download the blank sheet first and label the 20 posts by hand <b>before</b> you open the engine's answers. Then download the engine file and run <code>python tools/golden_agreement.py golden_sheet.csv golden_engine.csv</code>. Download both right away: the free server forgets this run when it restarts.</p><p><a class="btn" href="/api/golden/${GID}/sheet.csv">1. Blank sheet</a> <a class="btn" href="/api/golden/${GID}/engine.csv">2. Engine answers</a></p><h3 class="h3">Evidence explorer</h3><div class="filters"><select id="fc"><option value="">All posts</option><option value="sat">Satisfied</option><option value="neu">Neutral</option><option value="dis">Not satisfied</option><option value="drop">Dropped (off-topic or repeated)</option></select>
  <select id="fs"><option value="">All sites</option>${[...new Set(s.items.map(i=>i.site))].map(x=>`<option value="${E(x)}">${E(site(x))}</option>`).join('')}</select></div><div id="evl"></div>`;
 const draw=()=>{const c=$('#fc').value,t=$('#fs').value,l=s.items.filter(i=>(!c||(c==='drop'?!i.rel:i.cls===c))&&(!t||i.site===t));$('#evl').innerHTML=`<p class="mute">${l.length} posts</p>`+l.slice(0,60).map(itemHtml).join('')+(l.length>60?`<p class="mute">Showing the first 60.</p>`:'')};
 $('#fc').onchange=$('#fs').onchange=draw;draw();
 requestAnimationFrame(()=>setTimeout(()=>document.querySelectorAll('[data-w]').forEach(i=>i.style.width=i.dataset.w+'%'),50))}
(function(){const c=$('#bg'),x=c.getContext('2d');if(matchMedia('(prefers-reduced-motion:reduce)').matches)return;let w,h,cs=[],p=0,off=0;
 const mk=()=>{const o=p,cl=o+(Math.random()-.52)*26;p=Math.min(h*.85,Math.max(h*.25,cl));return{o,c:cl,hi:Math.max(o,cl)+Math.random()*12,lo:Math.min(o,cl)-Math.random()*12}};
 const fit=()=>{w=c.width=innerWidth;h=c.height=innerHeight;cs=[];p=h*.6;for(let i=0;i<w/16+2;i++)cs.push(mk())};
 (function f(){x.clearRect(0,0,w,h);off+=.35;if(off>=16){off=0;cs.shift();cs.push(mk())}cs.forEach((k,i)=>{const px=i*16-off;x.strokeStyle=x.fillStyle=k.c<=k.o?'#c6ff00':'#2a5bff';x.beginPath();x.moveTo(px+5,k.hi);x.lineTo(px+5,k.lo);x.stroke();x.fillRect(px+1,Math.min(k.o,k.c),8,Math.max(2,Math.abs(k.c-k.o)))});requestAnimationFrame(f)})();
 addEventListener('resize',fit);fit()})();

/* wake a sleeping server and learn whether fresh runs need an access code */
fetch('/api/config').then(r=>r.json()).then(c=>{$('#codebox').hidden=!c.needs_code}).catch(()=>{});
