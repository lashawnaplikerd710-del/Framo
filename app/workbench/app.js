'use strict';
const $ = id => document.getElementById(id);
const state = {csrf: '', assets: [], busy: false, loadedSettings: false, last: null, view: 'create', taskSignature: '', closed: false};
const labels = {CREATED:'排队中', ANALYZING:'分析素材', ANALYZING_SOURCE:'分析素材', TRANSCRIBING:'理解口播', PLANNING:'制定剪辑方案', EXECUTING:'执行剪辑', BUILDING_DRAFT:'构建草稿', REVIEWING:'抽样复核', PACKAGING:'编排元素', VALIDATING:'检查素材与环境', REVISING:'调整剪辑', READY_FOR_REVIEW:'草稿待检查', COMPLETED:'任务结束', WAITING_FOR_INPUT:'需要处理', BLOCKED_RESOURCES:'资源待处理', FAILED:'执行失败', CANCELLED:'已取消', PUBLISHING:'登记草稿'};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function eventText(ev) {
  if(!ev) return ''; const p=ev.payload||{};
  return p.message||p.error||(p.stage==='sentence_synthesized'?`剪映已生成第 ${Number(p.slot)} 句全新口播`:({new_script_written:'全新口播文案已生成',native_narration_quality_check:'检查全新口播、字幕与音轨'}[p.stage]))||labels[ev.stage]||ev.stage;
}
function notice(text, error=false) { $('notice').textContent=text; $('notice').className=error?'error':''; $('notice').hidden=!text; }
async function api(path, data) {
  const response=await fetch(path, {credentials:'same-origin', headers:data===undefined?{}:{'Content-Type':'application/json','X-Workbench-CSRF':state.csrf}, ...(data===undefined?{}:{method:'POST', body:JSON.stringify(data)})});
  const result=await response.json(); if(!response.ok) throw new Error(typeof result.error==='string'?result.error:'操作未完成'); return result;
}
function show(view) {
  state.view=view;
  document.querySelectorAll('.view').forEach(el=>el.hidden=el.id!=='view-'+view);
  document.querySelectorAll('.nav').forEach(el=>el.classList.toggle('active',el.dataset.view===view));
  $('breadcrumb').textContent='工作空间 / '+({create:'新建剪辑',tasks:'任务与草稿',settings:'环境与设置'}[view]);
}
document.querySelectorAll('[data-view]').forEach(el=>el.addEventListener('click',()=>show(el.dataset.view)));
function drawAssets() {
  $('asset-count').textContent=state.assets.length+' / 8';
  $('assets').innerHTML=state.assets.map((a,i)=>`<div class="asset"><span class="asset-mark">VIDEO</span><div class="asset-body"><strong title="${esc(a.name)}">${esc(a.name)}</strong><small>${(a.duration_us/1e6).toFixed(1)} 秒 · ${a.width} × ${a.height}</small></div><button data-remove="${i}" aria-label="移除 ${esc(a.name)}">×</button></div>`).join('');
  $('assets').querySelectorAll('[data-remove]').forEach(el=>el.onclick=()=>{state.assets.splice(Number(el.dataset.remove),1);drawAssets();});
}
function addAssets(assets) {
  for(const asset of assets) if(!state.assets.some(a=>a.asset_id===asset.asset_id)&&state.assets.length<8) state.assets.push(asset);
  drawAssets();
}
async function busy(action) {
  if(state.busy) return;
  state.busy=true; document.querySelectorAll('#create,#pick,#settings-form button').forEach(b=>b.disabled=true);
  try { await action(); } catch(e) { notice(e.message,true); }
  finally { state.busy=false; document.querySelectorAll('#create,#pick,#settings-form button').forEach(b=>b.disabled=false); }
}
async function upload(files) {
  if(!files.length) return;
  await busy(async()=>{
    if(files.length+state.assets.length>8) throw new Error('每个任务最多使用 8 条素材。');
    for(let i=0;i<files.length;i++) {
      const file=files[i]; notice(`正在将素材保存到本机工作目录（${i+1}/${files.length}）：${file.name}`);
      const response=await fetch('/api/upload',{method:'POST',headers:{'X-Workbench-CSRF':state.csrf,'X-File-Name':encodeURIComponent(file.name),'Content-Type':'application/octet-stream'},body:file});
      const result=await response.json(); if(!response.ok) throw new Error(result.error); addAssets(result);
    }
    notice('素材已添加，可以填写要求并开始剪辑。'); $('files').value='';
  });
}
$('files').addEventListener('change',e=>upload([...e.target.files]));
$('dropzone').addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();$('files').click();}});
$('dropzone').addEventListener('dragover',e=>{e.preventDefault();$('dropzone').classList.add('dragging');});
$('dropzone').addEventListener('dragleave',()=>$('dropzone').classList.remove('dragging'));
$('dropzone').addEventListener('drop',e=>{e.preventDefault();$('dropzone').classList.remove('dragging');upload([...e.dataTransfer.files]);});
$('pick').onclick=()=>busy(async()=>{notice('请选择素材文件。');addAssets(await api('/api/pick',{}));notice('素材已添加。');});
$('instruction').oninput=()=>$('text-count').textContent=$('instruction').value.length+' / 10000';
$('workflow').onchange=()=>{$('workflow-hint').textContent={product_ad:'选择 2–8 条同画幅素材，包含可识别的口播内容。',product_montage:'选择 2–8 条同画幅素材，保留素材本身的声音。',talking_head:'选择 1 条中文口播素材，保留完整表达。',interview:'选择 1 条中文访谈素材，保留语义与回答上下文。'}[$('workflow').value];};
$('create').onclick=()=>busy(async()=>{
  const workflow=$('workflow').value;
  const instruction=$('instruction').value.trim()||(workflow==='product_ad'?'制作 28 秒带货混剪草稿，使用全新口播、字幕文案、个性字体、音乐、音效、贴纸、转场、特效与滤镜，附加元素使用剪映免费内部资源。':'根据素材自主剪辑，保留有价值的信息，生成可编辑剪映草稿。');
  const outputCount=Math.max(1,Math.min(15,Number($('output-count').value||1)));
  await api('/api/jobs',{instruction,workflow,asset_ids:state.assets.map(a=>a.asset_id),output_count:outputCount});
  state.assets=[]; drawAssets(); notice('任务已加入队列，Agent 将自动开始处理。'); show('tasks'); await refresh();
});
$('settings-form').onsubmit=e=>{e.preventDefault();busy(async()=>{
  const data=Object.fromEntries(['gateway_url','access_token','install_dir','draft_root','user_data_dir'].map(k=>[k,$(k).value]));
  await api('/api/settings',data); $('access_token').value=''; notice('设置已保存，访问码保存在本机 Windows 凭据管理器。'); await refresh();
});};
$('check-gateway').onclick=()=>busy(async()=>{
  $('gateway-result').textContent='正在检测连接…';
  try {const r=await api('/api/gateway-status',{});$('gateway-result').textContent=`网关已连接 · 今日已用 ${r.used_units} / ${r.daily_units} 请求单位（UTC 日期 ${r.day}）。Max 请求计 15 单位，其余模型计 1 单位；这不是人民币或精确 token 费用。`;}
  catch(e){$('gateway-result').textContent='';throw e;}
});
function drawTasks(jobs) {
  const signature=JSON.stringify(jobs); if(signature===state.taskSignature) return; state.taskSignature=signature;
  const opened=new Set([...document.querySelectorAll('.task-log[open]')].map(x=>x.dataset.job));
  $('task-count').textContent=jobs.length;
  $('tasks').innerHTML=jobs.length?jobs.map(j=>{
    const ready=['READY_FOR_REVIEW','COMPLETED'].includes(j.state), waiting=['WAITING_FOR_INPUT','BLOCKED_RESOURCES','FAILED','CANCELLED'].includes(j.state);
    const registered=j.publication?.registered_draft_path;
    const registration=j.registration||{};
    const registrationLabel={queued:'检查通过 · 等待自动登记',waiting_editor:'待登记 · 请退出剪映',waiting_workers:'待登记 · 等待剪辑结束',registering:'正在自动登记',failed:'草稿已保留 · 登记失败'}[registration.status];
    const latest=eventText(j.events.at(-1));
    return `<article class="card"><div class="task-header"><div><h2>${esc(j.instruction)}</h2><p>${esc(new Date(j.created_at).toLocaleString('zh-CN'))} · ${esc(j.job_id.slice(0,16))}</p></div><span class="badge ${registered?'done':ready||waiting?'wait':''}">${esc(registered?'已登记 · 待视听检查':registrationLabel||labels[j.state]||j.state)}${j.worker_running?' · 执行中':''}</span></div>
    ${!registered&&registration.message?`<p class="${registration.status==='failed'?'task-error':'muted'}">${esc(registration.message)}</p>`:''}
    ${latest?`<p class="muted">${esc(latest)}</p>`:''}${j.native_progress?`<p class="muted">原生包装检查点：已写入 ${Number(j.native_progress.completed)} / ${Number(j.native_progress.total)} 个元素。下载、保存和读回期间计数可能暂时不变。</p>`:''}${j.error?`<p class="task-error">${esc(j.error)}</p>`:''}
    ${registered?`<p class="task-path">草稿已放入剪映草稿目录：<br>${esc(registered)}<br>打开剪映检查完整音画与轨道，再手动导出。</p>`:''}
    ${j.review?.issues?.length?`<details class="task-log"><summary>此前抽样复核有 ${j.review.issues.length} 项提醒，最终草稿仍需检查</summary>${j.review.issues.slice(0,8).map(item=>`<p>${esc(String(item.problem||'').slice(0,800))}</p>`).join('')}</details>`:''}
    <div class="task-actions">${registered?`<button class="secondary" data-action="open_folder" data-job="${j.job_id}">打开草稿文件夹 ↗</button>`:''}${ready&&!registered&&!j.worker_running&&registration.status!=='registering'?`<button class="primary" data-action="publish" data-job="${j.job_id}">${registration.status==='failed'?'重试登记':'立即登记'}</button>`:''}${!ready&&!j.worker_running&&j.state!=='CREATED'?`<button class="secondary" data-action="resume" data-job="${j.job_id}">从检查点继续</button>`:''}${!ready&&j.state!=='CANCELLED'?`<button class="secondary" data-action="cancel" data-job="${j.job_id}" ${j.cancel_requested?'disabled':''}>${j.cancel_requested?'等待安全停止':'取消任务'}</button>`:''}</div>
    <details class="task-log" data-job="${j.job_id}" ${opened.has(j.job_id)?'open':''}><summary>查看最近 ${j.events.length} 条进度记录</summary>${j.events.map(ev=>`<div class="event"><time>${esc(new Date(ev.timestamp).toLocaleTimeString('zh-CN'))}</time><p>${esc(eventText(ev))}</p></div>`).join('')}</details></article>`;
  }).join(''):'<div class="empty"><strong>还没有开始的故事</strong>添加素材，创建你的第一条自动剪辑任务。</div>';
  $('tasks').querySelectorAll('[data-action]').forEach(el=>el.onclick=()=>busy(async()=>{await api('/api/action',{action:el.dataset.action,job_id:el.dataset.job});notice('操作已提交。');await refresh();}));
}
function drawBatches(batches) {
  if(!batches?.length) return;
  const text=batches.map(b=>`批次 ${esc(b.batch_id?.slice(0,16))} · ${esc(b.state)} · 已完成 ${Number(b.counts?.success||0)} / ${b.children?.length||0}`).join('；');
  $('notice').textContent=text; $('notice').hidden=false;
}
async function refresh() {
  const data=await api('/api/status'); state.last=data; state.csrf=data.csrf;
  if(!state.loadedSettings){for(const k of ['gateway_url','install_dir','draft_root','user_data_dir'])$(k).value=data.settings[k]||'';state.loadedSettings=true;}
  $('checks').innerHTML=data.checks.map(c=>`<div class="check ${c.ok?'':'bad'}"><span class="check-icon">${c.ok?'✓':'!'}</span><div><strong>${esc(c.name)}</strong><p>${esc(c.detail)}</p></div></div>`).join('');
  $('state-dir').textContent=data.state_dir; $('setup-banner').hidden=data.checks.every(c=>c.ok);
  $('limitations').innerHTML=data.limitations.map(t=>`<p>${esc(t)}</p>`).join(''); drawTasks(data.jobs); drawBatches(data.batches);
}
async function start() {
  const token=location.hash.slice(1); if(token){history.replaceState(null,'','/');const r=await api('/api/unlock',{token});state.csrf=r.csrf;}
  await refresh();
  setInterval(()=>{if(!state.closed)refresh().catch(e=>notice('本地工作台连接已断开，请重新双击启动器。'+e.message,true));},4000);
}
$('shutdown').onclick=()=>busy(async()=>{await api('/api/shutdown',{});state.closed=true;notice('工作台已退出，可以关闭此页面。下次双击启动器即可继续使用。');});
start().catch(e=>notice(e.message,true));
