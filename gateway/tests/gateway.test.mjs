import test from 'node:test';
import assert from 'node:assert/strict';
import {authenticate, sha256, validatePayload, reserveState, MAX_BODY, parseClients, MAX_DAILY_UNITS} from '../src/policy.mjs';
import {handle, QuotaLedger} from '../src/index.mjs';

const token = 'jyg_' + 'a'.repeat(43);
const grant = {id: 'friend', token_hash: await sha256(token), enabled: true, expires_at: '2099-01-01T00:00:00Z', daily_units: 20, max_concurrent: 1,
  models: ['deepseek-flash', 'qwen3.8-omni-flash', 'qwen3.8-max']};
const payload = () => ({model: 'deepseek-flash', messages: [{role: 'user', content: '返回 JSON'}], stream: false, max_tokens: 64, thinking: {type: 'disabled'}});
const req = (p = payload(), extra = {}) => new Request('https://example.com/v1/chat/completions', {method: 'POST', headers: {
  authorization: 'Bearer ' + token, 'content-type': 'application/json', 'x-request-id': crypto.randomUUID(), ...extra}, body: JSON.stringify(p)});
function environment() {
  const saved=new Map(); let queue = Promise.resolve();
  const tx = {get: async key => structuredClone(saved.get(key)), put: async (key, value) => { saved.set(key,structuredClone(value)); }};
  const storage = {transaction: fn => { const result = queue.then(() => fn(tx)); queue = result.catch(() => {}); return result; }};
  const env = {AUTH_CLIENTS: JSON.stringify([grant]), DEEPSEEK_API_KEY: 'sk-PRIVATE_DEEPSEEK_KEY', DASHSCOPE_API_KEY: 'sk-PRIVATE_QWEN_KEY',
    GLOBAL_DAILY_UNITS: '30', GLOBAL_CONCURRENT: '3'};
  const ledger = new QuotaLedger({storage}, env);
  env.QUOTA = {idFromName: x => x, get: () => ({fetch: (url, init) => ledger.fetch(new Request(url, init))})};
  return env;
}
const context = () => ({pending: [], waitUntil(p) { this.pending.push(p); }});
test('access token is hashed, expired/revoked tokens rejected', async () => {
  const env = environment(); assert.equal((await authenticate(req(), env)).id, 'friend');
  env.AUTH_CLIENTS = JSON.stringify([{...grant, enabled: false}]);
  await assert.rejects(authenticate(req(), env), /access_denied/);
  env.AUTH_CLIENTS = JSON.stringify([{...grant, expires_at: '2000-01-01'}]);
  await assert.rejects(authenticate(req(), env), /access_denied/);
  await assert.rejects(authenticate(req(payload(), {authorization: 'Bearer sk-real-provider-key'}), env), /access_denied/);
});

test('10000 daily units are valid for the grant and global gateway with strict bounds', async () => {
  const env = environment(), ctx = context();
  env.AUTH_CLIENTS = JSON.stringify([{...grant, daily_units: 10000}]);
  env.GLOBAL_DAILY_UNITS = '10000';
  assert.equal(MAX_DAILY_UNITS, 10000);
  assert.equal((await authenticate(req(), env)).daily_units, 10000);
  const status = () => handle(new Request('https://example.com/v1/status', {
    headers: {authorization: 'Bearer ' + token}}), env, ctx);
  const result = await status();
  assert.equal(result.status, 200);
  const data = await result.json();
  assert.equal(data.daily_units, 10000);
  assert.equal(data.global_daily_units, 10000);
  for (const daily_units of [0, 10001, 1.5, '10000']) {
    assert.throws(() => parseClients(JSON.stringify([{...grant, daily_units}])), /gateway_not_configured/);
  }
  env.GLOBAL_DAILY_UNITS = '10001';
  assert.equal((await status()).status, 503);
});

test('raising a spent quota preserves daily counters and immediately allows more requests', () => {
  const now = Date.parse('2026-10-13T12:00:00Z');
  const state = {day:'2026-10-13', total:200, users:{friend:200}, leases:{}, requests:{}};
  assert.throws(() => reserveState(structuredClone(state), {...grant, daily_units:200},
    'new-request', 1, {daily:400, concurrent:3}, now), /daily_allowance_exhausted/);
  const next = reserveState(structuredClone(state), {...grant, daily_units:10000},
    'new-request', 1, {daily:10000, concurrent:3}, now);
  assert.equal(next.total, 201);
  assert.equal(next.users.friend, 201);
  assert.deepEqual(state.users, {friend:200});
  const exhausted = {...state, total:10000, users:{friend:10000}};
  assert.throws(() => reserveState(exhausted, {...grant, daily_units:10000},
    'blocked', 1, {daily:10000, concurrent:3}, now), /daily_allowance_exhausted/);
});
test('allowlist prevents alternate providers, URLs and paid tools', () => {
  for (const change of [{model: 'other'}, {base_url: 'https://evil.example'}, {enable_search: true}, {max_tokens: 8193}, {n: 100}])
    assert.throws(() => validatePayload({...payload(), ...change}, grant));
});

test('request cap is exactly fifty calls independent of model weight and midnight resets', () => {
  const client={...grant,daily_units:750,max_requests:50};
  const limits={daily:10000,concurrent:3};let state;
  for(let i=0;i<50;i++) {
    const now=Date.parse('2026-10-19T23:59:40Z')+i*1000;
    state=reserveState(state,client,'call-'+i,i%2?15:1,limits,now);
    delete state.leases['call-'+i];
  }
  assert.equal(state.request_counts.friend,50);
  assert.equal(state.day,'2026-10-20');
  assert.throws(()=>reserveState(state,client,'call-51',1,limits,Date.parse('2026-10-20T00:01:00Z')),
    /request_allowance_exhausted/);
  for(const max_requests of [0,-1,1.5,'50',1000001,null])
    assert.throws(()=>parseClients(JSON.stringify([{...client,max_requests}])),/gateway_not_configured/);
});

test('last lifetime request is atomic and free status queries preserve the remaining count',async()=>{
  const env=environment(),ctx=context();
  const client={...grant,daily_units:750,max_concurrent:3,max_requests:1};
  env.AUTH_CLIENTS=JSON.stringify([client]);
  const reserve=id=>env.QUOTA.get().fetch('https://quota.internal',{method:'POST',
    body:JSON.stringify({action:'reserve',client,requestId:id,weight:1})});
  const result=await Promise.all([reserve('last-a'),reserve('last-b')]);
  assert.deepEqual(result.map(r=>r.status).sort(),[200,402]);
  for(let i=0;i<2;i++) {
    const status=await handle(new Request('https://example.com/v1/status',
      {headers:{authorization:'Bearer '+token}}),env,ctx);
    const data=await status.json();
    assert.equal(data.used_requests,1);assert.equal(data.max_requests,1);assert.equal(data.remaining_requests,0);
  }
});

test('recovery replay does not consume another lifetime request',async()=>{
  const env=environment(),ctx=context();env.AUTH_CLIENTS=JSON.stringify([{...grant,max_requests:1}]);
  const id=crypto.randomUUID();let calls=0;
  const headers={'x-request-id':id,'x-recovery-protocol':'1'};
  const upstream=async()=>{calls++;return new Response('{"ok":true}');};
  assert.equal((await handle(req(payload(),headers),env,ctx,upstream)).status,200);
  await Promise.all(ctx.pending);
  assert.equal((await handle(req(payload(),headers),env,ctx,upstream)).status,409);
  assert.equal((await handle(req(),env,ctx,upstream)).status,402);
  const replay=await handle(new Request('https://example.com/v1/requests/'+id+'/result',
    {headers:{authorization:'Bearer '+token}}),env,ctx);
  assert.equal(await replay.text(),'{"ok":true}');assert.equal(calls,1);
});
test('both providers accept bounded user PNG with separate parameter rules', () => {
  const p = {...payload(), model: 'qwen3.8-omni-flash', stream: true, reasoning_effort: 'none'}; delete p.thinking;
  p.messages = [{role: 'user', content: [{type: 'image_url', image_url: {url: 'data:image/png;base64,iVBORw0KGgoAAA=='}}]}];
  assert.equal(validatePayload(p, grant).vision, true);
  const deepseek = {...payload(), messages: structuredClone(p.messages)};
  assert.equal(validatePayload(deepseek, grant).family, 'deepseek');
  assert.equal(validatePayload({...deepseek,stream:true,stream_options:{include_usage:true}}, grant).vision,true);
  assert.throws(() => validatePayload({...p, model: 'deepseek-flash'}, grant), /unsupported_reasoning/);
  assert.throws(() => validatePayload({...deepseek,model:'qwen3.8-omni-flash'}, grant), /unsupported_reasoning/);
  assert.throws(() => validatePayload({...p,stream:false}, grant), /qwen_requires_streaming/);
  for (const role of ['system','assistant','tool']) {
    assert.throws(() => validatePayload({...deepseek,messages:[{...deepseek.messages[0],role}]},grant), /images_require_user_role/);
  }
  assert.throws(() => validatePayload({...deepseek,messages:[{role:'user',content:Array(9).fill(deepseek.messages[0].content[0])}]},grant), /too_many_images/);
  p.messages[0].content[0].image_url.url = 'https://127.0.0.1/private';
  assert.throws(() => validatePayload(p, grant));
  deepseek.messages[0].content[0].image_url.url='https://example.com/photo.png';
  assert.throws(() => validatePayload(deepseek,grant), /only_bounded_inline_png_supported/);
});
test('durable reservation enforces daily/global allowance, max model weighting, duplicates and midnight concurrency', () => {
  const limits = {daily: 25, concurrent: 3}, now = Date.parse('2026-09-28T23:59:59Z');
  const state = reserveState(null, grant, 'req1', 15, limits, now);
  assert.throws(() => reserveState(structuredClone(state), grant, 'req1', 1, limits, now), /already_submitted/);
  assert.throws(() => reserveState(structuredClone(state), grant, 'req2', 1, limits, now + 2000), /gateway_busy/);
  delete state.leases.req1;
  assert.throws(() => reserveState(structuredClone(state), grant, 'req2', 15, limits, now), /allowance/);
  assert.throws(() => reserveState(structuredClone(state), {...grant, id: 'other'}, 'req2', 15, limits, now), /allowance/);
});
test('bad auth and bad payload never call upstream or reserve quota', async () => {
  const env = environment(), ctx = context(); let calls = 0;
  const remote = async () => { calls++; throw new Error('must not call'); };
  assert.equal((await handle(req(payload(), {authorization: 'bad'}), env, ctx, remote)).status, 401);
  assert.equal((await handle(req({...payload(), model: 'other'}), env, ctx, remote)).status, 403);
  assert.equal((await handle(req(payload(), {origin: 'https://evil.example'}), env, ctx, remote)).status, 403);
  assert.equal((await handle(req(payload(), {'content-length': String(MAX_BODY + 1)}), env, ctx, remote)).status, 413);
  assert.equal(calls, 0);
});
test('upstream gets only server key, client sees no provider headers or errors', async () => {
  const env = environment(), ctx = context();
  let captured;
  const response = await handle(req(), env, ctx, async (url, options) => {
    captured = {url, options}; return new Response(JSON.stringify({ok: true}), {headers: {'x-secret': env.DEEPSEEK_API_KEY}});
  });
  assert.equal(captured.url, 'https://api.deepseek.com/chat/completions');
  assert.equal(captured.options.headers.authorization, 'Bearer ' + env.DEEPSEEK_API_KEY);
  assert.equal(captured.options.redirect, 'manual');
  assert.equal(response.headers.get('x-secret'), null);
  assert.deepEqual(await response.json(), {ok: true}); await Promise.all(ctx.pending);
  const failed = await handle(req(), env, ctx, async () => new Response(env.DEEPSEEK_API_KEY + token, {status: 401}));
  const text = await failed.text(); assert(!text.includes('sk-')); assert(!text.includes(token));
});
test('streaming preserves SSE and releases concurrency only after completion', async () => {
  const env = environment(), ctx = context();
  const p = {...payload(), stream: true};
  const sse = 'data: {"choices":[]}\n\ndata: [DONE]\n\n';
  const response = await handle(req(p), env, ctx, async () => new Response(sse, {headers: {'content-type': 'text/event-stream'}}));
  assert.equal(await response.text(), sse); await Promise.all(ctx.pending);
  const status = await handle(new Request('https://example.com/v1/status', {headers: {authorization: 'Bearer ' + token}}), env, ctx);
  assert.equal((await status.json()).used_units, 1);
});
test('concurrent reservations cannot both spend the last allowance', async () => {
  const env = environment();
  env.GLOBAL_DAILY_UNITS = '15';
  const reserve = id => env.QUOTA.get().fetch('https://quota.internal', {method: 'POST', body: JSON.stringify({action: 'reserve', client: grant, requestId: id, weight: 15})});
  const results = await Promise.all([reserve('a'), reserve('b')]);
  assert.deepEqual(results.map(x => x.status).sort(), [200, 402]);
});

test('recoverable completion is replayed without another upstream call or quota charge',async()=>{
  const env=environment(),ctx=context(),id=crypto.randomUUID();let calls=0;
  const result='{"choices":[{"message":{"content":"ok"},"finish_reason":"stop"}]}';
  const headers={'x-request-id':id,'x-recovery-protocol':'1'};
  const upstream=async()=>{calls++;return new Response(result);};
  assert.equal(await (await handle(req(payload(),headers),env,ctx,upstream)).text(),result);
  await Promise.all(ctx.pending);
  const get=path=>handle(new Request('https://example.com/v1/requests/'+id+path,{headers:{authorization:'Bearer '+token}}),env,ctx);
  assert.equal((await (await get('')).json()).state,'COMPLETED');
  assert.equal(await (await get('/result')).text(),result);
  assert.equal((await handle(req(payload(),headers),env,ctx,upstream)).status,409);
  assert.equal(calls,1);
  assert.equal((await handle(req({...payload(),max_tokens:65},headers),env,ctx,upstream)).status,409);
  const another='jyg_'+'b'.repeat(43);
  env.AUTH_CLIENTS=JSON.stringify([grant,{...grant,id:'another',token_hash:await sha256(another)}]);
  const denied=await handle(new Request('https://example.com/v1/requests/'+id+'/result',{headers:{authorization:'Bearer '+another}}),env,ctx);
  assert.equal(denied.status,404);
});

test('disconnected client can recover completed stream with same identity',async()=>{
  const env=environment(),ctx=context(),id=crypto.randomUUID();let source;
  const response=await handle(req({...payload(),stream:true},{'x-request-id':id,'x-recovery-protocol':'1'}),env,ctx,
    async()=>new Response(new ReadableStream({start(c){source=c;}}),{headers:{'content-type':'text/event-stream'}}));
  await response.body.cancel();
  const text='data: {"choices":[]}\n\ndata: [DONE]\n\n';
  source.enqueue(new TextEncoder().encode(text));source.close();await Promise.all(ctx.pending);
  const replay=await handle(new Request('https://example.com/v1/requests/'+id+'/result',{headers:{authorization:'Bearer '+token}}),env,ctx);
  assert.equal(await replay.text(),text);
});

test('broken upstream stays uncertain and never pretends to be complete',async()=>{
  const env=environment(),ctx=context(),id=crypto.randomUUID();
  const response=await handle(req(payload(),{'x-request-id':id,'x-recovery-protocol':'1'}),env,ctx,
    async()=>new Response(new ReadableStream({start(c){c.error(new Error('PRIVATE'));}})));
  await assert.rejects(response.text());await Promise.all(ctx.pending);
  const state=await handle(new Request('https://example.com/v1/requests/'+id,{headers:{authorization:'Bearer '+token}}),env,ctx);
  const text=await state.text();assert.equal(JSON.parse(text).state,'UNCERTAIN');assert(!text.includes('PRIVATE'));
});
