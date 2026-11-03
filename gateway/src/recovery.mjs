// Results are bound to the authenticated grant and exact payload hash, kept for 24h.
// No prompts, media, provider keys, or access tokens are persisted.
import {reserveState, reject} from './policy.mjs';
const json = (x, status=200) => Response.json(x, {status});
export async function recoveryAction(tx, data, limits) {
  if (!['reserve_recovery','request_status','request_result','request_finish'].includes(data.action)) return null;
  const key = 'request:'+data.requestId;
  let row = await tx.get(key);
  if (data.action === 'reserve_recovery') {
    if (row) {
      if (row.owner !== data.client.id || row.hash !== data.hash) reject('request_identity_conflict',409);
      return json({state: row.state, duplicate: true});
    }
    const state = reserveState(await tx.get('ledger'), data.client, data.requestId, data.weight, limits);
    await tx.put('ledger',state);
    row = {owner:data.client.id, hash:data.hash, model:data.model, state:'RUNNING',
      started_at:Date.now(), expires:Date.now()+86400000, bytes:0};
    await tx.put(key,row);
    if (tx.setAlarm && !(await tx.getAlarm())) await tx.setAlarm(row.expires);
    return json({reserved:true});
  }
  if (!row || row.owner !== data.client.id || row.expires <= Date.now()) reject('request_not_found',404);
  if (data.action === 'request_finish') {
    if (row.state !== 'RUNNING') return json({stored:false});
    const body = data.body;
    // Bound retained generated text to 1MiB; larger replies remain deliverable but not replayable.
    const complete = data.state === 'COMPLETED' && typeof body === 'string' && body.length <= 1400000;
    const chunks = complete ? Math.ceil(body.length/60000) : 0;
    for(let i=0;i<chunks;i++) await tx.put(key+':'+i,body.slice(i*60000,(i+1)*60000));
    row={...row,state:complete?'COMPLETED':data.state==='FAILED'?'FAILED':'UNCERTAIN',
      ended_at:Date.now(),bytes:data.bytes||0,status_code:data.status_code||null,
      error_code:data.error_code||null,content_type:data.content_type,chunks};
    await tx.put(key,row);
    return json({stored:complete});
  }
  if (row.state === 'RUNNING' && Date.now()-row.started_at > 210000) {
    row={...row,state:'UNCERTAIN',error_code:'worker_ended_without_receipt'};await tx.put(key,row);
  }
  if (data.action === 'request_status') {
    const {owner,hash,...safe}=row;return json(safe);
  }
  if(row.state!=='COMPLETED') reject('request_not_complete',409);
  let body='';for(let i=0;i<row.chunks;i++) body+=await tx.get(key+':'+i);
  return json({body,content_type:row.content_type});
}
export async function expireResults(storage) {
  const rows=await storage.list({prefix:'request:'});let next=null;
  for(const [key,row] of rows) {
    if(typeof row !== 'object') continue;
    if(row.expires<=Date.now()) {
      await storage.delete([key,...Array.from({length:row.chunks||0},(_,i)=>key+':'+i)]);
    } else next=Math.min(next||row.expires,row.expires);
  }
  if(next) await storage.setAlarm(next);
}
export function base64(bytes) {
  let s='';for(let i=0;i<bytes.length;i+=32768) s+=String.fromCharCode(...bytes.subarray(i,i+32768));return btoa(s);
}
