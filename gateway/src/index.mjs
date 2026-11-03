import {MODELS, MAX_BODY, MAX_DAILY_UNITS, Rejection, reject, authenticate, validatePayload, reserveState, sha256} from './policy.mjs';
import {recoveryAction, expireResults, base64} from './recovery.mjs';

const json = (value, status = 200) => Response.json(value, {status, headers: {'cache-control': 'no-store', 'x-content-type-options': 'nosniff'}});
const error = (code, status) => json({error: {code, message: code, type: 'gateway_error'}}, status);
function limits(env) {
  const daily = Number(env.GLOBAL_DAILY_UNITS), concurrent = Number(env.GLOBAL_CONCURRENT);
  if (!Number.isInteger(daily) || daily < 1 || daily > MAX_DAILY_UNITS || !Number.isInteger(concurrent) || concurrent < 1 || concurrent > 8) reject('gateway_not_configured', 503);
  return {daily, concurrent};
}
async function boundedBody(request) {
  if (!request.headers.get('content-type')?.toLowerCase().startsWith('application/json') || request.headers.has('content-encoding')) reject('json_required', 415);
  if (Number(request.headers.get('content-length')) > MAX_BODY) reject('request_too_large', 413);
  const reader = request.body?.getReader();
  if (!reader) reject('missing_body');
  let size = 0, text = ''; const decoder = new TextDecoder('utf-8', {fatal: true});
  try {
    while (true) {
      const {done, value} = await reader.read(); if (done) break;
      size += value.byteLength;
      if (size > MAX_BODY) { await reader.cancel(); reject('request_too_large', 413); }
      text += decoder.decode(value, {stream: true});
    }
    text += decoder.decode();
    return JSON.parse(text);
  } catch (e) { if (e instanceof Rejection) throw e; reject('invalid_json'); }
}

// Only the bound Worker can reach this object. Quotas are atomic and durable,
// not counters that each edge location or a restarted process can reset.
export class QuotaLedger {
  constructor(ctx, env) { this.ctx = ctx; this.env = env; }
  async alarm() { await expireResults(this.ctx.storage); }
  async fetch(request) {
    const data = await request.json();
    try {
      const response=await this.ctx.storage.transaction(async tx => {
        const recovered=await recoveryAction(tx,data,limits(this.env));
        if(recovered) return recovered;
        let state = await tx.get('ledger');
        if (data.action === 'reserve') {
          state = reserveState(state, data.client, data.requestId, data.weight, limits(this.env));
          await tx.put('ledger', state);
          return json({reserved: true});
        }
        if (data.action === 'release') {
          if (state?.leases[data.requestId]) { delete state.leases[data.requestId]; await tx.put('ledger', state); }
          return json({released: true});
        }
        if (data.action === 'status') {
          const day = new Date().toISOString().slice(0, 10), same = state?.day === day;
          return json({recovery_protocol:1,client_id: data.client.id, day, used_units: same ? (state.users[data.client.id] || 0) : 0,
            daily_units: data.client.daily_units, global_used_units: same ? state.total : 0, global_daily_units: limits(this.env).daily,
            quota_definition: 'conservative_request_units_not_currency', max_weight: 15,
            expires_at: data.client.expires_at,
            used_requests: state?.request_counts?.[data.client.id] || 0,
            max_requests: data.client.max_requests ?? null,
            remaining_requests: data.client.max_requests === undefined ? null :
              Math.max(0,data.client.max_requests-(state?.request_counts?.[data.client.id] || 0))});
        }
        return error('not_found', 404);
      });
      if(data.action==='reserve_recovery' && response.ok && this.ctx.storage.setAlarm && !(await this.ctx.storage.getAlarm())) {
        await this.ctx.storage.setAlarm(Date.now()+86400000);
      }
      return response;
    } catch (e) { return error(e instanceof Rejection ? e.code : 'quota_unavailable', e instanceof Rejection ? e.status : 503); }
  }
}
function ledger(env, data) {
  return env.QUOTA.get(env.QUOTA.idFromName('gateway-owner-v1')).fetch('https://quota.internal', {
    method: 'POST', headers: {'content-type': 'application/json'}, body: JSON.stringify(data)});
}
export async function handle(request, env, ctx, upstreamFetch = fetch) {
  const requestStarted=Date.now();
  const url = new URL(request.url);
  if (url.pathname === '/health' && request.method === 'GET') return json({service: 'jianying-agent-model-gateway', version: 1});
  // Native clients have no Origin. Reject browser origins rather than enable a public CORS proxy.
  if (request.headers.has('origin') || url.search) return error('request_not_allowed', 403);
  const match=url.pathname.match(/^\/v1\/requests\/([a-f0-9-]{36})(\/result)?$/);
  if (!['/v1/chat/completions', '/v1/status'].includes(url.pathname) && !match) return error('not_found', 404);
  let release, timer, receipt, diagnosticId;
  try {
    const client = await authenticate(request, env);
    if(match && request.method==='GET') {
      const result=await ledger(env,{action:match[2]?'request_result':'request_status',client,requestId:match[1]});
      if(!result.ok||!match[2]) return result;
      const cached=await result.json();
      return new Response(Uint8Array.from(atob(cached.body),x=>x.charCodeAt(0)),{headers:{'content-type':cached.content_type,'cache-control':'no-store','x-request-id':match[1]}});
    }
    if (url.pathname === '/v1/status' && request.method === 'GET') return await ledger(env, {action: 'status', client});
    if (url.pathname !== '/v1/chat/completions' || request.method !== 'POST') return error('method_not_allowed', 405);
    const payload = await boundedBody(request), model = validatePayload(payload, client);
    const secret = env[model.secret];
    if (typeof secret !== 'string' || secret.length < 16 || /\s/.test(secret)) reject('provider_not_configured', 503);
    const requestId = request.headers.get('x-request-id');
    if (!/^[a-f0-9-]{36}$/.test(requestId || '')) reject('request_id_required');
    diagnosticId=requestId;
    const recoverable=request.headers.get('x-recovery-protocol')==='1';
    const reservation = await ledger(env, {action: recoverable?'reserve_recovery':'reserve', client, requestId, weight: model.weight,
      model:payload.model,hash:recoverable?await sha256(JSON.stringify(payload)):undefined});
    if (!reservation.ok) return reservation;
    const reserved=await reservation.json();
    if(reserved.duplicate) return error('request_in_progress_or_recorded',409);
    if(recoverable) receipt=async data=>{await ledger(env,{action:'request_finish',client,requestId,...data});};
    release = async () => { await ledger(env, {action: 'release', requestId}); };
    const controller = new AbortController();
    timer = setTimeout(() => controller.abort(), 180000);
    const upstream = await upstreamFetch(model.endpoint, {method: 'POST', redirect: 'manual', signal: controller.signal,
      headers: {'authorization': `Bearer ${secret}`, 'content-type': 'application/json', 'accept': payload.stream ? 'text/event-stream' : 'application/json'},
      body: JSON.stringify(payload)});
    if (!upstream.ok || !upstream.body) {
      if(receipt) await receipt({state:'FAILED',status_code:upstream.status,error_code:'upstream_http_'+upstream.status});
      await upstream.body?.cancel(); clearTimeout(timer); await release(); release = null;
      // Never forward upstream error bodies/headers, which can contain credential or request data.
      const status = [400, 401, 402, 403, 422, 429, 503].includes(upstream.status) ? upstream.status : 502;
      return error('upstream_request_failed_' + upstream.status, status);
    }
    if (payload.stream && !upstream.headers.get('content-type')?.includes('text/event-stream')) {
      await upstream.body.cancel(); reject('unexpected_upstream_response', 502);
    }
    let output,disconnected=false;
    const readable=new ReadableStream({start(c){output=c;},cancel(){disconnected=true;}});
    const reader = upstream.body.getReader();
    const finish = release; release = null;
    const transfer = (async () => {
      let total = 0;const chunks=[];const started=requestStarted;let firstByte=null;
      try {
        while (true) {
          const {done, value} = await reader.read(); if (done) break;
          total += value.byteLength;
          if(firstByte===null) firstByte=Date.now()-started;
          if (total > 8 * 1024 * 1024) throw new Error('response_limit');
          if(receipt&&total<=1024*1024) chunks.push(value);
          if(!disconnected) output.enqueue(value);
        }
        if(receipt) {
          const bytes=new Uint8Array(total<=1024*1024?total:0);let offset=0;
          for(const c of total<=1024*1024?chunks:[]){bytes.set(c,offset);offset+=c.length;}
          await receipt({state:total<=1024*1024?'COMPLETED':'UNCERTAIN',body:base64(bytes),bytes:total,
            content_type:payload.stream?'text/event-stream; charset=utf-8':'application/json'});
        }
        if(!disconnected) output.close();
        console.log(JSON.stringify({event:'model_response',request_id:requestId,model:payload.model,state:'COMPLETED',bytes:total,first_byte_ms:firstByte,elapsed_ms:Date.now()-started}));
      } catch {
        controller.abort(); await reader.cancel().catch(() => {});
        if(receipt) await receipt({state:'UNCERTAIN',bytes:total,error_code:'gateway_stream_interrupted'}).catch(()=>{});
        if(!disconnected) output.error(new Error('gateway_stream_interrupted'));
        console.log(JSON.stringify({event:'model_response',request_id:requestId,model:payload.model,state:'UNCERTAIN',bytes:total,elapsed_ms:Date.now()-started}));
      }
      finally { clearTimeout(timer); await finish(); }
    })();
    ctx.waitUntil(transfer);
    return new Response(readable, {headers: {'content-type': payload.stream ? 'text/event-stream; charset=utf-8' : 'application/json',
      'cache-control': 'no-store', 'x-content-type-options': 'nosniff', 'x-request-id': requestId}});
  } catch (e) {
    clearTimeout(timer);
    if(receipt) await receipt({state:'UNCERTAIN',error_code:'upstream_connect_or_response_failed'}).catch(()=>{});
    if (release) await release().catch(() => {});
    if(diagnosticId) console.log(JSON.stringify({event:'model_request_failed',request_id:diagnosticId,
      code:e instanceof Rejection?e.code:'upstream_connect_or_response_failed',elapsed_ms:Date.now()-requestStarted}));
    return error(e instanceof Rejection ? e.code : 'gateway_unavailable_outcome_uncertain', e instanceof Rejection ? e.status : 502);
  }
}
export default {fetch: handle};
