(function(){
const STAGES=[
  {id:'idea',name:'Идея',c:'--s-idea'},
  {id:'research',name:'Проработка',c:'--s-research'},
  {id:'pilot',name:'Пилот',c:'--s-pilot'},
  {id:'launched',name:'Внедрено',c:'--s-launched'},
  {id:'stopped',name:'Остановлено',c:'--s-stopped'}
];
const DIRS=['GenAI','Компьютерное зрение','OCR/VLM','Антифрод','Аналитика и прогнозы','Автоматизация процессов'];
const stageOf=id=>STAGES.find(s=>s.id===id)||STAGES[0];
const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

let cases=[]; let dbReady=false; let CFG={llm:false};
async function api(path,opts={}){
  const r=await fetch(path,{headers:{'Content-Type':'application/json'},...opts,body:opts.body?JSON.stringify(opts.body):undefined});
  let data=null;try{data=await r.json()}catch(e){}
  if(!r.ok)throw Object.assign(new Error((data&&data.message)||('HTTP '+r.status)),{code:data&&data.error,status:r.status});
  return data;
}
async function reload(){
  try{const d=await api('api/cases');cases=d.cases||[];dbReady=true;$('#notice').innerHTML='';render();if(typeof renderKB==='function')renderKB();}
  catch(e){dbReady=true;render();$('#notice').innerHTML='<div class="notice">Сервер реестра недоступен. Проверьте, что приложение запущено, и обновите страницу.</div>';}
}
const filt={stage:null,dir:'',type:'all',q:''};

/* ---------- similarity ---------- */
const STOP=new Set('и в во на для по с со из от что как это при или не к до за под над без об о у же ли то так все его их она они мы вы есть был была были будет также через чтобы который которые где когда если уже нет лишь только более менее очень можно нужно надо'.split(' '));
function stems(text){
  const out=[];
  String(text||'').toLowerCase().replace(/ё/g,'е').split(/[^a-zа-я0-9]+/).forEach(w=>{
    if(w.length<3||STOP.has(w))return;
    out.push({s:w.length>5?w.slice(0,5):w,w});
  });
  return out;
}
function vec(c){
  const m=new Map(), words=new Map();
  const add=(t,wt)=>stems(t).forEach(({s,w})=>{m.set(s,(m.get(s)||0)+wt);if(!words.has(s))words.set(s,w)});
  add(c.title,3);add((c.tags||[]).join(' '),2.5);add(c.problem,1);add(c.solution,1.2);add(c.direction,1.5);
  return {m,words};
}
function similarity(a,b){
  const A=vec(a),B=vec(b);let dot=0,na=0,nb=0;const common=[];
  A.m.forEach((v,k)=>{na+=v*v;if(B.m.has(k)){dot+=v*B.m.get(k);common.push([k,v*B.m.get(k)])}});
  B.m.forEach(v=>nb+=v*v);
  if(!na||!nb)return {score:0,common:[]};
  let s=dot/Math.sqrt(na*nb);
  if(a.direction&&a.direction===b.direction)s+=0.08;
  common.sort((x,y)=>y[1]-x[1]);
  return {score:Math.min(s,1),common:common.slice(0,4).map(([k])=>B.words.get(k))};
}
function similarTo(c,excludeId,n=3){
  return cases.filter(x=>x.id!==excludeId).map(x=>({x,...similarity(c,x)}))
    .filter(r=>r.score>=0.12).sort((a,b)=>b.score-a.score).slice(0,n);
}

/* ---------- render ---------- */
function renderFunnel(){
  const counts=Object.fromEntries(STAGES.map(s=>[s.id,0]));
  cases.forEach(c=>{if(counts[c.stage]!=null)counts[c.stage]++});
  $('#funnel').innerHTML=STAGES.map((s,i)=>`<button class="stage" data-s="${s.id}" style="--c:var(${s.c})" aria-pressed="${filt.stage===s.id}">
    <span class="lbl"><span class="dot"></span>${s.name}</span><span class="num">${dbReady?counts[s.id]:'–'}</span>${i<3?'<span class="arrow">→</span>':''}</button>`).join('');
}
function matches(c){
  if(filt.stage&&c.stage!==filt.stage)return false;
  if(filt.dir&&c.direction!==filt.dir)return false;
  if(filt.type!=='all'&&c.type!==filt.type)return false;
  if(filt.q){
    const hay=[c.title,c.problem,c.solution,c.effect,c.owner,c.unit,(c.tags||[]).join(' '),c.code].join(' ').toLowerCase();
    if(!filt.q.toLowerCase().split(/\s+/).every(t=>hay.includes(t)))return false;
  }
  return true;
}
function cardHTML(c){
  const s=stageOf(c.stage);
  return `<button class="card" data-id="${esc(c.id)}">
    <div class="card-top"><span class="pill" style="--c:var(${s.c})"><span class="dot"></span>${s.name}</span><span class="mono" style="color:var(--muted)">${esc(c.code||'')}</span></div>
    <h3>${esc(c.title)}</h3>
    <p>${esc(c.problem||c.solution)}</p>
    ${c.effect?`<div><span class="kind">${c.effectKind==='fact'?'Эффект · факт':'Эффект · план'}</span><div class="effect">${esc(c.effect)}</div></div>`:''}
    <div class="card-foot"><span>${esc(c.direction||'')}</span><span>${c.type==='idea'?'<span class="idea-mark">идея</span> ':''}${esc(c.owner||'Владелец не указан')}</span></div>
  </button>`;
}
function render(){
  renderFunnel();
  if(!dbReady)return;
  const list=cases.filter(matches).sort((a,b)=>(b.updatedAt||'').localeCompare(a.updatedAt||''));
  $('#count').textContent=`${list.length} из ${cases.length}`;
  $('#grid').innerHTML=list.length?list.map(cardHTML).join(''):
    `<div class="empty">${cases.length?'Ничего не нашлось. Попробуйте убрать часть фильтров.':'В реестре пока нет кейсов. Добавьте первый.'}</div>`;
}
function fillDirFilter(){
  $('#dirFilter').innerHTML='<option value="">Все направления</option>'+DIRS.map(d=>`<option>${esc(d)}</option>`).join('');
}

/* ---------- detail ---------- */
function openDetail(id){
  const c=cases.find(x=>x.id===id);if(!c)return;
  const s=stageOf(c.stage);
  const sims=similarTo(c,c.id,4);
  $('#drawer').innerHTML=`
    <div class="dhead"><span class="mono" style="color:var(--muted)">${esc(c.code||'')} · ${c.type==='idea'?'Идея':'Проект'}</span><button class="x" data-close aria-label="Закрыть">×</button></div>
    <div><span class="pill" style="--c:var(${s.c})"><span class="dot"></span>${s.name}</span><h2 id="dTitle" style="margin-top:10px">${esc(c.title)}</h2></div>
    <dl class="facts">
      <dt>Направление</dt><dd>${esc(c.direction||'—')}</dd>
      <dt>Владелец</dt><dd>${esc(c.owner||'—')}</dd>
      <dt>Подразделение</dt><dd>${esc(c.unit||'—')}</dd>
      <dt>Вики</dt><dd>${c.wiki?`<a href="${esc(c.wiki)}" target="_blank" rel="noopener">${esc(c.wiki.replace(/^https?:\/\//,'').slice(0,48))}</a>`:'—'}</dd>
    </dl>
    <div class="sec"><h4>Проблема</h4><p>${esc(c.problem||'—')}</p></div>
    <div class="sec"><h4>Решение</h4><p>${esc(c.solution||'—')}</p></div>
    <div class="sec"><h4>Эффект · ${c.effectKind==='fact'?'факт':'план'}</h4><p>${esc(c.effect||'Не указан')}</p></div>
    ${(c.tags||[]).length?`<div class="tags">${c.tags.map(t=>`<span class="tag">${esc(t)}</span>`).join('')}</div>`:''}
    <div class="sec"><h4>Стадия</h4><div class="inline-field">
      <select class="t" id="stageSel" style="width:auto">${STAGES.map(x=>`<option value="${x.id}" ${x.id===c.stage?'selected':''}>${x.name}</option>`).join('')}</select>
      <span class="hint" id="stageMsg"></span></div></div>
    <div class="sec"><h4>Похожие проекты</h4>
      <div class="sim-list">${sims.length?sims.map(simHTML).join(''):'<p class="hint">Похожих кейсов в реестре нет.</p>'}</div></div>`;
  $('#dOverlay').hidden=false;
  $('#stageSel').onchange=async e=>{
    const v=e.target.value;
    try{await api('api/cases/'+encodeURIComponent(c.id),{method:'PATCH',body:{stage:v}});$('#stageMsg').textContent='Сохранено';reload();}
    catch(err){$('#stageMsg').textContent='Не удалось сохранить: '+err.message;}
  };
  $('#drawer').querySelector('[data-close]').focus();
}
function simHTML(r){
  const s=stageOf(r.x.stage);
  return `<button class="sim" data-id="${esc(r.x.id)}"><span class="score">${Math.round(r.score*100)}%<small>сходство</small></span>
    <span><b>${esc(r.x.title)}</b><span><span style="color:var(${s.c})">●</span> ${s.name} · ${esc(r.x.direction||'')}${r.common.length?' · общее: '+esc(r.common.join(', ')):''}</span></span></button>`;
}
$('#dOverlay').addEventListener('click',e=>{
  if(e.target.id==='dOverlay'||e.target.closest('[data-close]')){$('#dOverlay').hidden=true;return}
  const sim=e.target.closest('.sim');if(sim)openDetail(sim.dataset.id);
});

/* ---------- wizard ---------- */
const QUESTIONS=[
  {k:'what',q:'Что сделали или что хотите сделать?',hint:'Своими словами, как рассказали бы коллеге. Например: «Сделали цифрового аватара, который отвечает новым сотрудникам на вопросы по регламентам».',ph:'Опишите решение или идею'},
  {k:'problem',q:'Какую проблему это решает и для кого?',hint:'Чья это боль и в каком подразделении: магазины, логистика, закупки, поддержка…',ph:'Проблема и заказчик'},
  {k:'stage',q:'На какой стадии сейчас?',hint:'Выберите стадию и, если есть, уточните масштаб: сколько магазинов, сроки пилота.',ph:'Например: пилот в 12 магазинах Москвы до конца ноября',chips:true},
  {k:'effect',q:'Какой эффект: ожидаемый или уже полученный?',hint:'Лучше в цифрах: часы, рубли, проценты, доля ошибок. Если цифр пока нет, опишите качественно.',ph:'Например: −30% времени на онбординг (план)'}
];
const MISSING_Q={problem:QUESTIONS[1],stage:QUESTIONS[2],effect:QUESTIONS[3]};
let W=null;
function openWizard(){
  W={mode:null,step:0,answers:{},stagePick:null,history:[],queue:[],wikiText:'',wikiUrl:'',followupAsked:false,draft:null,busy:false,error:''};
  $('#wOverlay').hidden=false;drawWizard();
}
function closeWizard(){$('#wOverlay').hidden=true;W=null}
function progress(){
  const total=4;let done=0;
  if(W.mode==='scratch')done=Math.min(W.step,4);
  if(W.mode==='wiki')done=W.wikiText?4-W.queue.length:0;
  if(W.draft)done=5;
  $('#wProg').innerHTML=Array.from({length:total+1},(_,i)=>`<i class="${i<done?'on':''}"></i>`).join('');
}
function chatHTML(){
  return `<div class="chat">${W.history.map(h=>`<div class="bubble q"><span class="mono">${esc(h.label)}</span>${esc(h.q)}</div><div class="bubble a">${esc(h.a)}</div>`).join('')}</div>`;
}
function currentQ(){
  if(W.mode==='scratch'&&W.step<4)return QUESTIONS[W.step];
  if(W.pendingFollowup)return {k:'followup',q:W.pendingFollowup,hint:'Уточнение от ассистента, чтобы карточка получилась полной.',ph:'Ответ'};
  if(W.mode==='wiki'&&W.wikiText&&W.queue.length)return MISSING_Q[W.queue[0]];
  return null;
}
function drawWizard(){
  if(!W)return;progress();
  const body=$('#wBody'),foot=$('#wFoot');
  if(!W.mode){
    body.innerHTML=`<p class="hint" style="margin:0">Как удобнее завести кейс?</p>
      <div class="modes">
        <button class="mode" data-mode="scratch"><span class="mono">4 вопроса · ~2 минуты</span><b>Ответить на вопросы</b><span>Для новой идеи или проекта, по которому ещё нет страницы в вики.</span></button>
        <button class="mode" data-mode="wiki"><span class="mono">вставить текст</span><b>Из страницы вики</b><span>Вставьте текст страницы, ассистент заполнит карточку и спросит только то, чего не хватает.</span></button>
      </div>`;
    foot.innerHTML=`<span class="hint">Ассистент разложит ответы по полям, вы проверите карточку перед сохранением.</span>`;
    return;
  }
  if(W.busy){body.innerHTML=chatHTML()+`<div class="working"><span class="spin"></span>Ассистент собирает карточку…</div>`;foot.innerHTML='';return}
  if(W.draft){drawPreview();return}
  if(W.mode==='wiki'&&!W.wikiText){
    body.innerHTML=`<div class="current">
      <label for="wikiUrl">Ссылка на страницу (необязательно)</label>
      <input class="t" id="wikiUrl" type="url" placeholder="https://wiki.x5.ru/…" value="${esc(W.wikiUrl)}">
      <label for="wikiTxt">Текст страницы</label>
      <p class="hint">В прототипе текст нужно вставить вручную. В боевой версии система будет читать вики сама.</p>
      <textarea id="wikiTxt" rows="9" placeholder="Скопируйте сюда содержимое страницы проекта"></textarea>
      ${W.error?`<p class="err">${esc(W.error)}</p>`:''}</div>`;
    foot.innerHTML=`<button class="btn ghost" data-act="back">Назад</button><button class="btn primary" data-act="wikiGo">Разобрать страницу</button>`;
    $('#wikiTxt').focus();return;
  }
  const q=currentQ();
  if(!q)return;
  const idx=W.mode==='scratch'?`Вопрос ${W.step+1} из 4`:(q.k==='followup'?'Уточнение':'Не нашлось на странице');
  body.innerHTML=chatHTML()+`<div class="current">
    <span class="mono" style="color:var(--muted)">${idx}</span>
    <label for="ans">${esc(q.q)}</label><p class="hint">${esc(q.hint)}</p>
    ${q.chips?`<div class="chips" id="stChips">${STAGES.slice(0,4).map(s=>`<button class="chip" data-st="${s.id}" style="--c:var(${s.c})" aria-pressed="${W.stagePick===s.id}"><span class="dot"></span>${s.name}</button>`).join('')}</div>`:''}
    <textarea id="ans" rows="3" placeholder="${esc(q.ph)}"></textarea>
    ${W.error?`<p class="err">${esc(W.error)}</p>`:''}</div>`;
  foot.innerHTML=`<button class="btn ghost" data-act="back">Назад</button><span class="hint">Ctrl + Enter — дальше</span><button class="btn primary" data-act="next">Дальше</button>`;
  body.scrollTop=body.scrollHeight;$('#ans').focus();
}
function answerCurrent(){
  const q=currentQ();const ta=$('#ans');const txt=(ta?ta.value:'').trim();
  if(q.chips){
    if(!W.stagePick){W.error='Выберите стадию.';drawWizard();return}
    const sn=stageOf(W.stagePick).name;
    W.answers.stage=sn+(txt?'. '+txt:'');
  }else{
    if(!txt){W.error='Напишите хотя бы пару слов.';drawWizard();return}
    W.answers[q.k==='followup'?'followup':q.k]=(W.answers[q.k]&&q.k==='followup'?W.answers[q.k]+'\n':'')+txt;
  }
  W.error='';
  W.history.push({label:q.k==='followup'?'Уточнение':(W.mode==='scratch'?`Вопрос ${W.step+1}`:'Вопрос'),q:q.q,a:q.chips?W.answers.stage:txt});
  if(W.pendingFollowup){W.pendingFollowup=null;return runParse()}
  if(W.mode==='scratch'){W.step++;if(W.step>=4)return runParse();}
  else{W.queue.shift();if(!W.queue.length)return runParse();}
  drawWizard();
}
function buildPayload(){
  const a=W.answers;
  return {wikiText:W.wikiText||'',what:a.what||'',problem:a.problem||'',stage:a.stage||'',effect:a.effect||'',followup:a.followup||'',
    allowFollowup:W.mode==='scratch'&&!W.followupAsked,chat:W.chatText||''};
}
function heuristicDraft(){
  const a=W.answers,t=W.wikiText||'';
  const first=(a.what||t).split(/[.\n]/).map(s=>s.trim()).find(Boolean)||'Новый кейс';
  const text=(a.what||'')+' '+(a.problem||'')+' '+t;
  const guess=[['Компьютерное зрение',/видео|камер|фото|распозна|зрени/i],['OCR/VLM',/документ|ocr|скан|накладн/i],['Антифрод',/мошен|фрод|хищ|краж/i],['Аналитика и прогнозы',/прогноз|аналит|спрос/i],['GenAI',/бот|ассистент|llm|gpt|аватар|генер/i]].find(([,r])=>r.test(text));
  return {title:first.slice(0,60),type:W.stagePick==='idea'?'idea':'project',stage:W.stagePick||'idea',direction:guess?guess[0]:'Автоматизация процессов',
    problem:a.problem||'',solution:a.what||t.slice(0,300),effect:a.effect||'',effectKind:W.stagePick==='launched'?'fact':'plan',unit:'',owner:'',tags:[],missing:[],followup:null};
}
async function runParse(){
  W.busy=true;drawWizard();
  let r=null;
  if(CFG.llm){
    try{r=await api('api/parse',{method:'POST',body:buildPayload()});}
    catch(e){W.aiNote='Ассистент не ответил ('+e.message+') — карточка заполнена по ответам как есть. Проверьте поля.';}
  }else W.aiNote='Ассистент (LLM) не подключён на сервере — карточка заполнена по ответам как есть. Проверьте поля.';
  if(!r||typeof r!=='object')r=heuristicDraft();
  if(W.stagePick)r.stage=W.stagePick;
  W.busy=false;
  if(W.mode==='wiki'&&!W.parsedOnce){
    W.parsedOnce=true;
    const miss=(Array.isArray(r.missing)?r.missing:[]).filter(k=>MISSING_Q[k]);
    if(miss.length){W.queue=miss;W.firstDraft=r;drawWizard();return}
  }
  if(W.mode==='scratch'&&!W.followupAsked&&r.followup&&typeof r.followup==='string'){
    W.followupAsked=true;W.pendingFollowup=r.followup;drawWizard();return;
  }
  W.draft=normalize(r);drawWizard();
}
function normalize(r){
  return {title:String(r.title||'Новый кейс').slice(0,90),type:r.type==='idea'?'idea':'project',
    stage:STAGES.some(s=>s.id===r.stage)?r.stage:'idea',direction:DIRS.includes(r.direction)?r.direction:DIRS[5],
    problem:String(r.problem||''),solution:String(r.solution||''),effect:String(r.effect||''),effectKind:r.effectKind==='fact'?'fact':'plan',
    unit:String(r.unit||''),owner:String(r.owner||''),tags:Array.isArray(r.tags)?r.tags.map(String).slice(0,6):[],wiki:W.wikiUrl||''};
}
function drawPreview(){
  const d=W.draft;
  $('#wBody').innerHTML=`${W.aiNote?`<div class="warn">${esc(W.aiNote)}</div>`:''}
  <div class="preview">
    <div class="form">
      <label>Название<input class="t" id="p_title" value="${esc(d.title)}"></label>
      <div class="row">
        <label>Тип<select class="t" id="p_type"><option value="project" ${d.type==='project'?'selected':''}>Проект</option><option value="idea" ${d.type==='idea'?'selected':''}>Идея</option></select></label>
        <label>Стадия<select class="t" id="p_stage">${STAGES.map(s=>`<option value="${s.id}" ${s.id===d.stage?'selected':''}>${s.name}</option>`).join('')}</select></label>
      </div>
      <div class="row">
        <label>Направление<select class="t" id="p_dir">${DIRS.map(x=>`<option ${x===d.direction?'selected':''}>${esc(x)}</option>`).join('')}</select></label>
        <label>Владелец<input class="t" id="p_owner" value="${esc(d.owner)}" placeholder="Кто ведёт"></label>
      </div>
      <label>Подразделение-заказчик<input class="t" id="p_unit" value="${esc(d.unit)}"></label>
      <label>Проблема<textarea class="t" id="p_problem" rows="2">${esc(d.problem)}</textarea></label>
      <label>Решение<textarea class="t" id="p_solution" rows="3">${esc(d.solution)}</textarea></label>
      <div class="row">
        <label>Эффект<input class="t" id="p_effect" value="${esc(d.effect)}"></label>
        <label>План или факт<select class="t" id="p_ek"><option value="plan" ${d.effectKind==='plan'?'selected':''}>План</option><option value="fact" ${d.effectKind==='fact'?'selected':''}>Факт</option></select></label>
      </div>
      <label>Теги через запятую<input class="t" id="p_tags" value="${esc(d.tags.join(', '))}"></label>
      <label>Ссылка на вики<input class="t" id="p_wiki" value="${esc(d.wiki)}" placeholder="https://…"></label>
    </div>
    <div class="side"><h4>Похожие проекты в реестре</h4><div class="sim-list" id="pSims"></div>
      <p class="hint">Проверьте, не дублирует ли кейс уже существующий. Похожие можно открыть и взять за основу.</p></div>
  </div>`;
  $('#wFoot').innerHTML=`<button class="btn ghost" data-act="restart">Начать заново</button><span class="err" id="saveErr"></span><button class="btn primary" data-act="save">Сохранить в реестр</button>`;
  refreshSims();
  $('#wBody').querySelectorAll('input,textarea,select').forEach(el=>el.addEventListener('input',debounce(refreshSims,300)));
}
function readDraft(){
  const v=id=>$('#'+id).value.trim();
  return {title:v('p_title')||'Без названия',type:v('p_type'),stage:v('p_stage'),direction:v('p_dir'),owner:v('p_owner'),unit:v('p_unit'),
    problem:v('p_problem'),solution:v('p_solution'),effect:v('p_effect'),effectKind:v('p_ek'),tags:v('p_tags').split(',').map(s=>s.trim()).filter(Boolean).slice(0,8),wiki:v('p_wiki')};
}
function refreshSims(){
  if(!W||!W.draft)return;
  const sims=similarTo(readDraft(),null,4);
  $('#pSims').innerHTML=sims.length?sims.map(r=>(r.score>=0.45?`<div class="warn">Похоже на дубль: сходство ${Math.round(r.score*100)}%</div>`:'')+simHTML(r)).join(''):'<p class="hint">Совпадений не нашлось — похоже, это новое направление.</p>';
}
function debounce(fn,ms){let t;return(...a)=>{clearTimeout(t);t=setTimeout(()=>fn(...a),ms)}}
async function saveDraft(btn){
  const d=readDraft();
  btn.disabled=true;
  try{const saved=await api('api/cases',{method:'POST',body:d});closeWizard();toast('Кейс '+saved.code+' добавлен в реестр');reload();}
  catch(e){btn.disabled=false;$('#saveErr').textContent='Не удалось сохранить: '+e.message;}
}
$('#wOverlay').addEventListener('click',e=>{
  if(!W)return;
  if(e.target.id==='wOverlay'||e.target.id==='wClose'){closeWizard();return}
  const m=e.target.closest('[data-mode]');if(m){W.mode=m.dataset.mode;drawWizard();return}
  const ch=e.target.closest('[data-st]');if(ch){W.stagePick=ch.dataset.st;const keep=$('#ans').value;W.error='';drawWizard();$('#ans').value=keep;return}
  const sim=e.target.closest('.sim');if(sim){openDetail(sim.dataset.id);return}
  const a=e.target.closest('[data-act]');if(!a)return;
  const act=a.dataset.act;
  if(act==='next')answerCurrent();
  else if(act==='save')saveDraft(a);
  else if(act==='restart')openWizard();
  else if(act==='wikiGo'){
    const t=$('#wikiTxt').value.trim();if(t.length<40){W.error='Вставьте текст страницы — хотя бы несколько предложений.';drawWizard();return}
    W.wikiText=t;W.wikiUrl=$('#wikiUrl').value.trim();W.error='';
    W.history.push({label:'Страница вики',q:'Текст страницы',a:t.length>220?t.slice(0,220)+'…':t});
    runParse();
  }
  else if(act==='back'){
    W.error='';
    if(W.mode==='scratch'&&W.step>0&&!W.pendingFollowup){W.step--;W.history.pop();}
    else if(W.mode==='wiki'&&W.wikiText){W.wikiText='';W.history=[];W.queue=[];W.parsedOnce=false;W.answers={};}
    else {W.mode=null;W.history=[];W.answers={};W.step=0;}
    drawWizard();
  }
});
$('#wOverlay').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)&&$('#ans')){e.preventDefault();answerCurrent()}});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(!$('#dOverlay').hidden)$('#dOverlay').hidden=true;else if(W)closeWizard();}});

function toast(msg){const t=document.createElement('div');t.className='toast';t.textContent=msg;document.body.append(t);setTimeout(()=>t.remove(),2600)}

/* ---------- consultant chat ---------- */
const CHAT_KEY='innolib.chat.v1';
let chat={messages:[]};
try{const s=JSON.parse(localStorage.getItem(CHAT_KEY)||'null');if(s&&Array.isArray(s.messages))chat={messages:s.messages.filter(m=>!m.pending)}}catch(e){}
function saveChat(){try{localStorage.setItem(CHAT_KEY,JSON.stringify({messages:chat.messages.slice(-40)}))}catch(e){}}
let streaming=null, docs=[];
const STARTERS=[
  {k:'Что уже есть',q:'Что у нас уже есть по компьютерному зрению и на каких стадиях?'},
  {k:'Проверить идею',q:'Хочу сделать голосового помощника для директоров магазинов. Такое уже было? С чего начать?'},
  {k:'Новые идеи',q:'Предложи 3 идеи по снижению потерь в магазинах, опираясь на наши кейсы.'},
  {k:'Масштабирование',q:'Какие пилоты стоит масштабировать в первую очередь и почему?'}
];

function inlineMd(t){
  return t.replace(/`([^`]+)`/g,'<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>')
    .replace(/\[((?:INN|DOC)-\d+(?:\s*[,;]\s*(?:INN|DOC)-\d+)*)\]/g,(_,g)=>g.split(/\s*[,;]\s*/).map(r=>`<button type="button" class="ref" data-ref="${r}">${r}</button>`).join(' '));
}
function mdRender(src){
  const lines=esc(src).split('\n');let html='',list=null,para=[],inCode=false,code=[];
  const flushP=()=>{if(para.length){html+='<p>'+inlineMd(para.join('<br>'))+'</p>';para=[]}};
  const flushL=()=>{if(list){html+=`<${list.t}>`+list.items.map(i=>'<li>'+inlineMd(i)+'</li>').join('')+`</${list.t}>`;list=null}};
  for(const raw of lines){
    const l=raw.replace(/\s+$/,'');let m;
    if(/^```/.test(l.trim())){if(inCode){html+='<pre><code>'+code.join('\n')+'</code></pre>';code=[];inCode=false}else{flushP();flushL();inCode=true}continue}
    if(inCode){code.push(raw);continue}
    if(!l.trim()){flushP();flushL();continue}
    if((m=l.match(/^#{1,6}\s+(.*)/))){flushP();flushL();html+='<h4>'+inlineMd(m[1])+'</h4>';continue}
    if((m=l.match(/^\s*[-*•]\s+(.*)/))){flushP();if(!list||list.t!=='ul'){flushL();list={t:'ul',items:[]}}list.items.push(m[1]);continue}
    if((m=l.match(/^\s*\d+[.)]\s+(.*)/))){flushP();if(!list||list.t!=='ol'){flushL();list={t:'ol',items:[]}}list.items.push(m[1]);continue}
    if(list&&/^\s{2,}/.test(raw)){list.items[list.items.length-1]+=' '+l.trim();continue}
    flushL();para.push(l);
  }
  if(inCode)html+='<pre><code>'+code.join('\n')+'</code></pre>';
  flushP();flushL();return html;
}
function withCaret(html){
  const caret='<span class="caret"></span>';
  const at=Math.max(html.lastIndexOf('</p>'),html.lastIndexOf('</li>'),html.lastIndexOf('</h4>'));
  return at<0?html+caret:html.slice(0,at)+caret+html.slice(at);
}
function uniqSources(list){const seen=new Set();return (list||[]).filter(s=>!seen.has(s.ref)&&seen.add(s.ref))}
function sourcesHTML(m){
  if(m.pending||!m.sources||!m.sources.length)return '';
  const cited=new Set(m.content.match(/(?:INN|DOC)-\d+/g)||[]);
  const list=uniqSources(m.sources).sort((a,b)=>cited.has(b.ref)-cited.has(a.ref));
  return `<div class="sources"><span class="sources-label">Источники</span>${list.map(s=>`<button type="button" class="src${cited.has(s.ref)?' cited':''}" data-ref="${esc(s.ref)}" title="${esc(s.title)}"><b>${esc(s.ref)}</b><span>${esc(s.title)}</span></button>`).join('')}</div>`;
}
function msgHTML(m,i){
  if(m.role==='user')return `<div class="msg-user">${esc(m.content)}</div>`;
  let body='';
  if(m.content)body=`<div class="md">${m.pending?withCaret(mdRender(m.content)):mdRender(m.content)}</div>`;
  else if(m.pending)body=`<div class="thinking"><span class="spin"></span>Ищу в реестре и думаю…</div>`;
  const err=m.error?`<div class="msg-error">${esc(m.error)}</div>`:'';
  return `<div class="msg-bot" data-i="${i}"><div class="avatar" aria-hidden="true">ИИ</div><div class="bot-body">${body}${err}${sourcesHTML(m)}</div></div>`;
}
function emptyHTML(){
  return `<div class="chat-empty">
    <h2>Консультант по инновациям</h2>
    <p>Знает все кейсы реестра и документы базы знаний. Расскажет, что уже делали, найдёт похожие проекты, поможет проверить и развить идею. На кейсы ссылается кодами — по ним можно открыть карточку.</p>
    ${CFG.llm?'':'<div class="warn">Модель не подключена: консультант покажет только найденные в реестре кейсы. Администратору — задать LLM_URL и LLM_MODEL в .env.</div>'}
    <div class="starters">${STARTERS.map(s=>`<button type="button" class="starter" data-q="${esc(s.q)}"><small>${esc(s.k)}</small>${esc(s.q)}</button>`).join('')}</div>
  </div>`;
}
function renderChat(){
  $('#chatLog').innerHTML=chat.messages.length?chat.messages.map(msgHTML).join(''):emptyHTML();
  $('#toCaseBtn').disabled=!!streaming||!chat.messages.some(m=>m.role==='user');
}
const nearBottom=()=>window.innerHeight+window.scrollY>=document.documentElement.scrollHeight-160;
function scrollBottom(){window.scrollTo({top:document.documentElement.scrollHeight})}
let raf=0;
function scheduleUpdate(){
  if(raf)return;
  raf=requestAnimationFrame(()=>{
    raf=0;const i=chat.messages.length-1;const el=document.querySelector(`.msg-bot[data-i="${i}"]`);
    const stick=nearBottom();
    if(el)el.outerHTML=msgHTML(chat.messages[i],i);
    if(stick)scrollBottom();
  });
}
function setSending(on){
  const b=$('#sendBtn');b.textContent=on?'Остановить':'Отправить';b.classList.toggle('stop',on);
  $('#toCaseBtn').disabled=on||!chat.messages.some(m=>m.role==='user');
}
function errText(ev){
  return ({llm_auth:'Ключ API не принят. Проверьте LLM_API_KEY на сервере.',
    llm_unreachable:'Модель недоступна. Проверьте LLM_URL и сетевой доступ сервера к API.',
    llm_rate_limited:'Слишком много запросов к модели. Повторите через минуту.',
    llm_not_found:'Модель или адрес API не найдены. Проверьте LLM_MODEL и LLM_URL.'})[ev.code]||('Ошибка модели: '+(ev.message||'неизвестная'));
}
async function sendChat(text){
  text=(text||'').trim();if(!text||streaming)return;
  chat.messages.push({role:'user',content:text});
  const history=chat.messages.filter(m=>m.content&&!m.error).map(m=>({role:m.role,content:m.content}));
  const bot={role:'assistant',content:'',pending:true,sources:[]};
  chat.messages.push(bot);renderChat();scrollBottom();
  streaming=new AbortController();setSending(true);
  try{
    const r=await fetch('api/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({messages:history}),signal:streaming.signal});
    if(!r.ok||!r.body){let d=null;try{d=await r.json()}catch(e){}throw new Error((d&&d.message)||('HTTP '+r.status))}
    const reader=r.body.getReader(),dec=new TextDecoder();let buf='';
    for(;;){
      const {value,done}=await reader.read();if(done)break;
      buf+=dec.decode(value,{stream:true});let k;
      while((k=buf.indexOf('\n\n'))>=0){
        const line=buf.slice(0,k).split('\n').find(l=>l.startsWith('data:'));buf=buf.slice(k+2);
        if(!line)continue;let ev;try{ev=JSON.parse(line.slice(5))}catch(e){continue}
        if(ev.type==='sources')bot.sources=ev.sources||[];
        else if(ev.type==='delta'){bot.content+=ev.text;scheduleUpdate()}
        else if(ev.type==='error')bot.error=errText(ev);
      }
    }
  }catch(e){if(e.name==='AbortError')bot.stopped=true;else bot.error='Консультант не ответил: '+e.message}
  bot.pending=false;bot.content=bot.content.replace(/^\s+/,'');streaming=null;setSending(false);
  if(!bot.content&&!bot.error)bot.error=bot.stopped?'Ответ остановлен.':'Модель вернула пустой ответ. Попробуйте переформулировать вопрос.';
  if(bot.stopped&&bot.content)bot.content+='\n\n_(ответ остановлен)_';
  saveChat();const stick=nearBottom();renderChat();if(stick)scrollBottom();
}
function openDocDrawer(ref,snips){
  const d=docs.find(x=>x.code===ref);
  $('#drawer').innerHTML=`<div class="dhead"><span class="mono" style="color:var(--muted)">${esc(ref)} · документ базы знаний</span><button class="x" data-close aria-label="Закрыть">×</button></div>
    <h2 id="dTitle">${esc(d?d.title:ref)}</h2>
    ${d&&d.filename?`<p class="hint">Файл: ${esc(d.filename)}</p>`:''}
    <div class="sec"><h4>Фрагменты, на которые опирался ответ</h4><div class="sim-list">${snips.length?snips.map(t=>`<div class="doc-snippet">${esc(t.replace(/^\[DOC-\d+\][^\n]*\n/,''))}</div>`).join(''):'<p class="hint">В этом ответе фрагменты документа не передавались.</p>'}</div></div>`;
  $('#dOverlay').hidden=false;$('#drawer').querySelector('[data-close]').focus();
}
function wizardFromChat(){
  const users=chat.messages.filter(m=>m.role==='user').map(m=>m.content);
  const transcript=chat.messages.filter(m=>m.content&&!m.error).slice(-10).map(m=>(m.role==='user'?'Сотрудник: ':'Консультант: ')+m.content.slice(0,1500)).join('\n\n');
  openWizard();
  Object.assign(W,{mode:'scratch',step:4,followupAsked:true,chatText:transcript,answers:{what:users.join('\n').slice(0,3000)},
    history:[{label:'Из диалога с консультантом',q:'Ваша идея',a:users.slice(-3).join('\n').slice(0,400)}]});
  runParse();
}
async function loadDocs(){try{const d=await api('api/docs');docs=d.docs||[]}catch(e){docs=[]}renderKB()}
function renderKB(){
  $('#kbCases').textContent=dbReady?cases.length:'–';$('#kbDocs').textContent=docs.length;
  $('#kbModel').textContent=CFG.llm?('Модель: '+CFG.chatModel+' · поиск '+(CFG.embeddings?'гибридный':'по ключевым словам')):'Модель не подключена — работает только поиск по реестру.';
  $('#docList').innerHTML=docs.length?docs.map(d=>`<li><b>${esc(d.code)}</b><span title="${esc(d.filename||d.title)}">${esc(d.title)}</span></li>`).join(''):'<li class="doc-empty">Документов пока нет</li>';
}
function fileToB64(f){return new Promise((res,rej)=>{const r=new FileReader();r.onload=()=>res(String(r.result).split(',')[1]||'');r.onerror=()=>rej(new Error('не удалось прочитать файл'));r.readAsDataURL(f)})}
$('#docFile').addEventListener('change',async e=>{
  const files=[...e.target.files];e.target.value='';$('#docErr').textContent='';
  for(const f of files){
    if(f.size>18e6){$('#docErr').textContent=f.name+': файл больше 18 МБ';continue}
    try{await api('api/docs',{method:'POST',body:{filename:f.name,contentBase64:await fileToB64(f)}});toast('Документ «'+f.name+'» добавлен в базу знаний')}
    catch(err){$('#docErr').textContent=f.name+': '+err.message}
  }
  loadDocs();
});
$('#chatLog').addEventListener('click',e=>{
  const st=e.target.closest('.starter');if(st){sendChat(st.dataset.q);return}
  const r=e.target.closest('[data-ref]');if(!r)return;
  const ref=r.dataset.ref,bot=e.target.closest('.msg-bot'),m=bot?chat.messages[+bot.dataset.i]:null;
  if(ref.startsWith('INN-')){const c=cases.find(x=>x.code===ref);if(c)openDetail(c.id);else toast('Кейса '+ref+' нет в реестре')}
  else openDocDrawer(ref,((m&&m.sources)||[]).filter(s=>s.ref===ref).map(s=>s.snippet));
});
$('#composer').addEventListener('submit',e=>{
  e.preventDefault();
  if(streaming){streaming.abort();return}
  const ta=$('#chatIn');const v=ta.value;ta.value='';ta.style.height='';sendChat(v);
});
$('#chatIn').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('#composer').requestSubmit()}});
$('#chatIn').addEventListener('input',e=>{const t=e.target;t.style.height='auto';t.style.height=Math.min(t.scrollHeight,200)+'px'});
$('#newChatBtn').onclick=()=>{if(streaming)streaming.abort();chat.messages=[];saveChat();renderChat();$('#chatIn').focus()};
$('#toCaseBtn').onclick=wizardFromChat;
function setView(v){
  const on=v==='chat';
  $('#registryView').hidden=on;$('#chatView').hidden=!on;
  $('#tabRegistry').setAttribute('aria-selected',String(!on));$('#tabChat').setAttribute('aria-selected',String(on));
  try{history.replaceState(null,'',on?'#chat':location.pathname+location.search)}catch(e){}
  if(on){renderChat();renderKB();setTimeout(()=>$('#chatIn').focus(),0)}
}
document.querySelector('.tabs').addEventListener('click',e=>{const b=e.target.closest('[data-view]');if(b)setView(b.dataset.view)});

/* ---------- events ---------- */
$('#addBtn').onclick=openWizard;
$('#funnel').addEventListener('click',e=>{const b=e.target.closest('.stage');if(!b)return;filt.stage=filt.stage===b.dataset.s?null:b.dataset.s;render()});
$('#grid').addEventListener('click',e=>{const c=e.target.closest('.card');if(c)openDetail(c.dataset.id)});
$('#q').addEventListener('input',debounce(e=>{filt.q=e.target.value.trim();render()},150));
$('#dirFilter').onchange=e=>{filt.dir=e.target.value;render()};
$('#typeSeg').addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;filt.type=b.dataset.v;$('#typeSeg').querySelectorAll('button').forEach(x=>x.setAttribute('aria-pressed',x===b));render()});

/* ---------- boot ---------- */
fillDirFilter();renderFunnel();
(async()=>{
  try{CFG=await api('api/config');}catch(e){}
  if(CFG.version)$('#foot').textContent='innolib v'+CFG.version+(CFG.llm?' · модель '+CFG.chatModel:' · модель не подключена');
  if(location.hash==='#chat')setView('chat');
  await reload();loadDocs();
  // подтягиваем изменения коллег раз в 30 секунд, пока вкладка открыта
  setInterval(()=>{if(!document.hidden&&$('#wOverlay').hidden)reload()},30000);
  document.addEventListener('visibilitychange',()=>{if(!document.hidden)reload()});
})();
})();
