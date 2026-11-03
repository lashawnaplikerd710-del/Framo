// Real workerd + SQLite Durable Object; outbound model calls are isolated stubs.
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {sha256} from '../src/policy.mjs';
const token='jyg_'+'c'.repeat(43),id=crypto.randomUUID();let upstreamCalls=0;
const reply='data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n';
const mf=new Miniflare(convertV4MiniflareOptions({name:'recovery-test',modules:true,script:await readFile('D:/JYVerify/deepseek-20261005/gateway-dry-run/index.js','utf8'),
  compatibilityDate:'2026-09-28',durableObjects:{QUOTA:{className:'QuotaLedger',useSQLite:true}},
  bindings:{GLOBAL_DAILY_UNITS:'30',GLOBAL_CONCURRENT:'3',DEEPSEEK_API_KEY:'test-provider-secret',
    AUTH_CLIENTS:JSON.stringify([{id:'test',token_hash:await sha256(token),enabled:true,expires_at:'2099-01-01',daily_units:20,max_concurrent:1,models:['deepseek-flash']}])},
  outboundService:()=>{upstreamCalls++;return new Response(reply,{headers:{'content-type':'text/event-stream'}});}}));
try {
 const headers={authorization:'Bearer '+token,'content-type':'application/json','x-request-id':id,'x-recovery-protocol':'1'};
 const body=JSON.stringify({model:'deepseek-flash',messages:[{role:'user',content:'test'}],stream:true,max_tokens:64,thinking:{type:'disabled'}});
 const first=await mf.dispatchFetch('https://test/v1/chat/completions',{method:'POST',headers,body});
 const firstText=await first.text();assert.equal(first.status,200,firstText);assert.equal(firstText,reply);
 const state=await mf.dispatchFetch('https://test/v1/requests/'+id,{headers});
 assert.equal((await state.json()).state,'COMPLETED');
 const again=await mf.dispatchFetch('https://test/v1/chat/completions',{method:'POST',headers,body});assert.equal(again.status,409);
 const recovered=await mf.dispatchFetch('https://test/v1/requests/'+id+'/result',{headers});assert.equal(await recovered.text(),reply);
 assert.equal(upstreamCalls,1);console.log('PASS: real SQLite Durable Object stores and replays result; one upstream request.');
} finally {await mf.dispose();}
