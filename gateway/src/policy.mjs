// This module contains no network access or credentials and can be tested in Node.
export const MODELS = Object.freeze({
  'deepseek-flash': {endpoint: 'https://api.deepseek.com/chat/completions', secret: 'DEEPSEEK_API_KEY', weight: 1, vision: true, family: 'deepseek', tools: true},
  'qwen3.8-omni-flash': {endpoint: 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions', secret: 'DASHSCOPE_API_KEY', weight: 1, vision: true, family: 'qwen', tools: false},
  'qwen3.8-max': {endpoint: 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions', secret: 'DASHSCOPE_API_KEY', weight: 15, vision: true, family: 'qwen', tools: false}
});
export const MAX_BODY = 12 * 1024 * 1024;
export const MAX_DAILY_UNITS = 10000;
export class Rejection extends Error {
  constructor(code, status = 400) { super(code); this.code = code; this.status = status; }
}
export const reject = (code, status = 400) => { throw new Rejection(code, status); };
export async function sha256(text) {
  return [...new Uint8Array(await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text)))].map(n => n.toString(16).padStart(2, '0')).join('');
}
export function constantEqual(a, b) {
  if (typeof a !== 'string' || typeof b !== 'string' || a.length !== b.length) return false;
  let difference = 0;
  for (let i = 0; i < a.length; i++) difference |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return difference === 0;
}
export function parseClients(raw) {
  let clients;
  try { clients = JSON.parse(raw); } catch { reject('gateway_not_configured', 503); }
  if (!Array.isArray(clients) || !clients.length || clients.length > 50) reject('gateway_not_configured', 503);
  const ids = new Set(), hashes = new Set();
  for (const c of clients) {
    if (!c || !/^[a-z0-9_-]{1,48}$/.test(c.id) || ['__proto__', 'constructor', 'prototype'].includes(c.id) || ids.has(c.id) ||
        !/^[a-f0-9]{64}$/.test(c.token_hash) || hashes.has(c.token_hash) ||
        typeof c.enabled !== 'boolean' || !Number.isInteger(c.daily_units) || c.daily_units < 1 || c.daily_units > MAX_DAILY_UNITS ||
        (c.max_requests !== undefined && (!Number.isInteger(c.max_requests) || c.max_requests < 1 || c.max_requests > 1000000)) ||
        !Number.isInteger(c.max_concurrent) || c.max_concurrent < 1 || c.max_concurrent > 3 ||
        !Number.isFinite(Date.parse(c.expires_at)) || !Array.isArray(c.models) || !c.models.length ||
        c.models.some(m => !Object.hasOwn(MODELS, m))) reject('gateway_not_configured', 503);
    ids.add(c.id); hashes.add(c.token_hash);
  }
  return clients;
}
export async function authenticate(request, env, now = Date.now()) {
  const auth = request.headers.get('authorization') || '';
  if (!/^Bearer jyg_[A-Za-z0-9_-]{43}$/.test(auth)) reject('access_denied', 401);
  const hash = await sha256(auth.slice(7));
  const client = parseClients(env.AUTH_CLIENTS).find(c => constantEqual(c.token_hash, hash));
  if (!client || !client.enabled || Date.parse(client.expires_at) <= now) reject('access_denied', 401);
  return client;
}
function keys(value, allowed, code = 'unsupported_field') {
  if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(k => !allowed.includes(k))) reject(code);
}
export function validatePayload(p, client) {
  keys(p, ['model', 'messages', 'stream', 'stream_options', 'max_tokens', 'thinking', 'reasoning_effort', 'modalities', 'response_format', 'tools']);
  if (!Object.hasOwn(MODELS, p.model) || !client.models.includes(p.model)) reject('model_not_allowed', 403);
  const model = MODELS[p.model];
  if (typeof p.stream !== 'boolean' || !Number.isInteger(p.max_tokens) || p.max_tokens < 1 || p.max_tokens > 8192) reject('invalid_output_limit');
  if (p.stream_options !== undefined && (!p.stream || JSON.stringify(p.stream_options) !== '{"include_usage":true}')) reject('invalid_stream_options');
  if (p.response_format !== undefined && JSON.stringify(p.response_format) !== '{"type":"json_object"}') reject('invalid_response_format');
  if (!Array.isArray(p.messages) || !p.messages.length || p.messages.length > 100) reject('invalid_messages');
  let chars = 0, images = 0;
  for (const m of p.messages) {
    keys(m, ['role', 'content', 'tool_call_id', 'tool_calls', 'reasoning_content']);
    if (!['system', 'user', 'assistant', 'tool'].includes(m.role)) reject('invalid_role');
    if (typeof m.content === 'string') chars += m.content.length;
    else if (Array.isArray(m.content) && m.content.length <= 40) {
      for (const part of m.content) {
        if (part?.type === 'text') {
          keys(part, ['type', 'text']);
          if (typeof part.text !== 'string') reject('invalid_text');
          chars += part.text.length;
        } else if (part?.type === 'image_url' && model.vision) {
          if (m.role !== 'user') reject('images_require_user_role');
          keys(part, ['type', 'image_url']); keys(part.image_url, ['url']);
          const url = part.image_url.url;
          if (typeof url !== 'string' || url.length > 1398126 || !/^data:image\/png;base64,iVBORw0KGgo[A-Za-z0-9+/]*={0,2}$/.test(url)) reject('only_bounded_inline_png_supported');
          if (++images > 8) reject('too_many_images');
        } else reject('unsupported_content');
      }
    } else if (m.content !== null || !m.tool_calls) reject('invalid_content');
    if (m.reasoning_content !== undefined) {
      if (typeof m.reasoning_content !== 'string') reject('invalid_reasoning');
      chars += m.reasoning_content.length;
    }
    if (m.tool_calls !== undefined) {
      if (!model.tools || m.role !== 'assistant' || !Array.isArray(m.tool_calls)) reject('invalid_tool_history');
      chars += JSON.stringify(m.tool_calls).length;
    }
    if (m.tool_call_id !== undefined && (m.role !== 'tool' || typeof m.tool_call_id !== 'string' || m.tool_call_id.length > 128)) reject('invalid_tool_history');
  }
  if (p.tools !== undefined) {
    if (!model.tools || p.stream || !Array.isArray(p.tools) || p.tools.length > 24) reject('tools_not_supported');
    for (const t of p.tools) {
      keys(t, ['type', 'function']); keys(t.function, ['name', 'description', 'parameters', 'strict']);
      if (t.type !== 'function' || !/^[A-Za-z0-9_-]{1,64}$/.test(t.function.name)) reject('invalid_tool');
    }
    chars += JSON.stringify(p.tools).length;
  }
  if (chars > 200000) reject('text_limit_exceeded', 413);
  if (model.family === 'qwen') {
    if (p.thinking !== undefined || !['none', 'low'].includes(p.reasoning_effort)) reject('unsupported_reasoning');
    if (p.modalities !== undefined && JSON.stringify(p.modalities) !== '["text"]') reject('only_text_output_supported');
    if (!p.stream) reject('qwen_requires_streaming');
  } else if (JSON.stringify(p.thinking) !== '{"type":"disabled"}' || p.reasoning_effort !== undefined || p.modalities !== undefined) reject('unsupported_reasoning');
  return model;
}
export function reserveState(state, client, requestId, weight, limits, now = Date.now()) {
  const day = new Date(now).toISOString().slice(0, 10);
  state ??= {day, total: 0, users: {}, leases: {}, requests: {}, request_counts: {}};
  // Daily counters reset at UTC midnight; live leases remain across the boundary.
  if (state.day !== day) state = {day, total: 0, users: {}, leases: state.leases, requests: state.requests,
                                request_counts: state.request_counts || {}};
  state.request_counts ??= {};
  state.leases = Object.fromEntries(Object.entries(state.leases).filter(([, x]) => x.until > now));
  state.requests = Object.fromEntries(Object.entries(state.requests).filter(([, until]) => until > now));
  if (Object.hasOwn(state.requests, requestId)) reject('request_already_submitted', 409);
  const own = state.users[client.id] || 0;
  const calls = state.request_counts[client.id] || 0;
  if (client.max_requests !== undefined && calls >= client.max_requests) reject('request_allowance_exhausted', 402);
  if (state.total + weight > limits.daily || own + weight > client.daily_units) reject('daily_allowance_exhausted', 402);
  if (Object.keys(state.leases).length >= limits.concurrent || Object.values(state.leases).filter(x => x.id === client.id).length >= client.max_concurrent) reject('gateway_busy', 429);
  state.total += weight; state.users[client.id] = own + weight;
  if (client.max_requests !== undefined) state.request_counts[client.id] = calls + 1;
  state.leases[requestId] = {id: client.id, until: now + 210000};
  state.requests[requestId] = now + 86400000;
  return state;
}
