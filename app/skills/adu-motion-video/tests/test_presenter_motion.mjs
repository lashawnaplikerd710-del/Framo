import assert from 'node:assert/strict';
import '../scripts/presenter_motion.js';
const keys=[{t:0,d:0,L:{x:540,y:1000,w:840,h:840}},{t:1,d:.2,L:{x:885,y:255,w:246,h:246}},{t:1.45,d:.3,L:{x:540,y:1060,w:600,h:760}}];
const curve=ADU_PRESENTER.compile(keys),h=1e-5;
// Interrupted transitions must start at the actual pose, not the previous
// destination. Position and first/second derivatives are continuous.
for(const t of [1,1.45]) {
 const a=curve.at(t-h),b=curve.at(t),c=curve.at(t+h);
 for(const k of ['x','y','w','h']) {
  assert.ok(Math.abs(a[k]-b[k])<.02);
  assert.ok(Math.abs((b[k]-a[k])/h-(c[k]-b[k])/h)<.2);
 }
}
const first=curve.at(1.8);curve.at(5);curve.at(.2);assert.deepEqual(curve.at(1.8),first);
const hero=ADU_PRESENTER.compile(keys.slice(0,2));
for(let f=0;f<240;f++) {
 const p=hero.at(f/60);assert.ok(p.x>=540-1e-7&&p.x<=885+1e-7);
 assert.ok(p.w>=246-1e-7&&p.w<=840+1e-7);
}
assert.throws(()=>ADU_PRESENTER.compile([...keys,keys.at(-1)]),/ordered/);
assert.throws(()=>curve.at(NaN),/finite/);
console.log('Presenter curves: interrupted continuity, no settled overshoot, finite keys and seek determinism passed');
