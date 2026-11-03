/* Presenter geometry only. No playback-history filter and no clock retiming. */
(function (global) {
  'use strict';
  const clamp = x => Math.max(0, Math.min(1, x));
  const fields = ['x', 'y', 'w', 'h', 'r', 'o', 's', 'z', 'rx', 'ry', 'rz'];
  function compile(keys, options = {}) {
    if (!Array.isArray(keys) || !keys.length) throw Error('Presenter motion needs keys');
    const segments = [], minimum = options.minimum ?? .75;
    const speed = options.speed ?? 1100, scaleSpeed = options.scaleSpeed ?? 1.8;
    const pose = value => Object.fromEntries(fields.map(k => [k, value[k] ?? (['o','s','z'].includes(k) ? 1 : 0)]));
    const sorted = keys.map(k => ({t:k.t, d:k.d, L:pose(k.L)}));
    if (sorted.some((k,i) => !Number.isFinite(k.t) || !Number.isFinite(k.d) || k.d < 0 ||
        (i && k.t <= sorted[i-1].t) || Object.values(k.L).some(v => !Number.isFinite(v))))
      throw Error('Presenter keys must be finite, ordered and unique');
    function sample(segment, t, derivative = 0) {
      const u = clamp((t-segment.t)/segment.d), out = {};
      for (const k of fields) {
        const c = segment.coefficients[k];
        out[k] = derivative === 0 ? c.reduce((v,a,i)=>v+a*u**i,0) :
          derivative === 1 ? c.reduce((v,a,i)=>v+(i ? i*a*u**(i-1) : 0),0)/segment.d :
          c.reduce((v,a,i)=>v+(i>1 ? i*(i-1)*a*u**(i-2) : 0),0)/segment.d**2;
        if (derivative && t >= segment.t+segment.d) out[k]=0;
      }
      return out;
    }
    let previous = sorted[0].L;
    for (let i=1; i<sorted.length; i++) {
      const key=sorted[i], last=segments.at(-1);
      const from=last ? sample(last,key.t) : previous;
      const velocity=last ? sample(last,key.t,1) : pose({});
      const acceleration=last ? sample(last,key.t,2) : pose({});
      if (!last) for(const k of fields) velocity[k]=acceleration[k]=0;
      const distance=Math.hypot(key.L.x-from.x,key.L.y-from.y);
      const scale=Math.max(...['w','h'].map(k=>Math.abs(Math.log(Math.max(1,key.L[k])/Math.max(1,from[k])))));
      const d=Math.max(key.d,minimum,1.875*distance/speed,1.875*scale/scaleSpeed), coefficients={};
      for(const k of fields) {
        const a=from[k], b=velocity[k]*d, c=acceleration[k]*d*d/2, delta=key.L[k]-a;
        coefficients[k]=[a,b,c,10*delta-6*b-3*c,-15*delta+8*b+3*c,6*delta-3*b-c];
      }
      segments.push({t:key.t,d,coefficients}); previous=key.L;
    }
    return {at(t) {
      if (!Number.isFinite(t)) throw Error('Presenter time must be finite');
      let segment=null; for(const s of segments) if(t>=s.t) segment=s; else break;
      const out=segment ? sample(segment,t) : {...sorted[0].L};
      out.w=Math.max(2,out.w);out.h=Math.max(2,out.h);out.o=clamp(out.o);
      return out;
    }, segments:segments.map(({t,d})=>({t,d}))};
  }
  const cache=new WeakMap();
  function at(keys,t,options) {
    let value=cache.get(keys); if(!value || value.length!==keys.length) {
      value={length:keys.length,curve:compile(keys,options)};cache.set(keys,value);
    }
    return value.curve.at(t);
  }
  function small(root,body,x,y,size,face,carrier=body) {
    const stage=document.getElementById('stage'); if(!stage || !body) return;
    // The source decides visibility and frame identity. The portrait card has
    // one resting anchor and crop, even when source cards differ in proportion.
    carrier.style.transform=carrier.style.transform.replace(/(?:perspective|rotateX|rotateY|rotateZ|rotate)\([^)]*\)/g,'');
    body.style.width='300px';body.style.height='300px';body.style.borderRadius='50%';
    body.style.overflow='hidden';
    const im=body.querySelector('img'),f=face || {cx:.5,cy:.42,h:.28};
    if(im) {
      const sw=im.naturalWidth||720,sh=im.naturalHeight||1280;
      const cover=Math.max(300/sw,300/sh),zoom=Math.max(1,Math.min(2.6,132/(Math.max(.01,f.h)*sh*cover)));
      const iw=sw*cover*zoom,ih=sh*cover*zoom;
      Object.assign(im.style,{position:'absolute',width:iw+'px',height:ih+'px',left:Math.max(300-iw,Math.min(0,150-f.cx*iw))+'px',top:Math.max(300-ih,Math.min(0,168-f.cy*ih))+'px',objectFit:'fill',maxWidth:'none'});
    }
    const label=body.querySelector('.lab,.tlab');if(label)label.style.opacity='0';
    fit(root,body,{x,y,w:size,h:size});
  }
  function fit(root,body,pose){
    const stage=document.getElementById('stage'),sr=stage.getBoundingClientRect(),targetW=pose.w*sr.width/1080,targetH=pose.h*sr.height/1920;
    let rect=body.getBoundingClientRect();if(rect.width<.001||rect.height<.001)return;
    let base=root.style.transform;
    root.style.transformOrigin='0 0';
    const center=()=>{const r=body.getBoundingClientRect();return [r.x+r.width/2,r.y+r.height/2];};
    // Measure the actual projected plane, including private 3D ancestors.
    // This bounded solve is repeated from the authored pose every frame; it
    // neither integrates a prior frame nor delays the narrator's image.
    for(let pass=0;pass<3;pass++) {
      rect=body.getBoundingClientRect();if(rect.width<.001||rect.height<.001)break;
      base=root.style.transform+' scale('+targetW/rect.width+','+targetH/rect.height+')';
      root.style.transform=base;
      for(let correction=0;correction<3;correction++) {
        const a=center(),dx=sr.left+pose.x*sr.width/1080-a[0],dy=sr.top+pose.y*sr.height/1920-a[1];
        if(Math.abs(dx)+Math.abs(dy)<.01)break;
        root.style.transform='translate(1px,0) '+base;const px=center();
        root.style.transform='translate(0,1px) '+base;const py=center();
        const ax=px[0]-a[0],ay=px[1]-a[1],bx=py[0]-a[0],by=py[1]-a[1],det=ax*by-ay*bx;
        if(Math.abs(det)<1e-9){root.style.transform=base;break;}
        base='translate('+((by*dx-bx*dy)/det)+'px,'+((-ay*dx+ax*dy)/det)+'px) '+base;
        root.style.transform=base;
      }
    }
  }
  const seamWrappers=new Map();
  function wrapper(carrier){let root=seamWrappers.get(carrier);if(!root){root=document.createElement('div');root.style.cssText='position:absolute;inset:0;transform-origin:0 0';root.className='presenter-seam-wrapper';carrier.parentNode.insertBefore(root,carrier);root.appendChild(carrier);seamWrappers.set(carrier,root);}return root;}
  function resetSeams(){for(const root of seamWrappers.values())root.style.transform='';}
  function rect(body){const stage=document.getElementById('stage').getBoundingClientRect(),r=body.getBoundingClientRect();return{x:(r.x+r.width/2-stage.x)*1080/stage.width,y:(r.y+r.height/2-stage.y)*1920/stage.height,w:r.width*1080/stage.width,h:r.height*1920/stage.height,r:parseFloat(getComputedStyle(body).borderRadius)||0};}
  function seam(carrier,body,from,elapsed,face){
    const root=wrapper(carrier);if(!from||elapsed>=1.2||elapsed<0||!body.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))return;
    const current=rect(body),u=clamp(elapsed/1.2),e=u*u*u*(u*(u*6-15)+10),pose={};
    for(const k of ['x','y','w','h','r'])pose[k]=from[k]+(current[k]-from[k])*e;
    body.style.width=pose.w+'px';body.style.height=pose.h+'px';body.style.borderRadius=Math.max(0,Math.min(pose.r,pose.w/2))+'px';
    const im=body.querySelector('img'),f=face||{cx:.5,cy:.42,h:.28};
    if(im){const sw=im.naturalWidth||720,sh=im.naturalHeight||1280,cover=Math.max(pose.w/sw,pose.h/sh),k=clamp((1.5-pose.h/pose.w)/.55),zoom=1+(Math.max(1,Math.min(2.6,pose.h*.44/(Math.max(.01,f.h)*sh*cover)))-1)*k,iw=sw*cover*zoom,ih=sh*cover*zoom;Object.assign(im.style,{position:'absolute',width:iw+'px',height:ih+'px',left:Math.max(pose.w-iw,Math.min(0,pose.w/2-f.cx*iw))+'px',top:Math.max(pose.h-ih,Math.min(0,pose.h*.54-f.cy*ih))+'px',objectFit:'fill',maxWidth:'none'});}
    fit(root,body,pose);
  }

  global.ADU_PRESENTER={version:'1.0.0',compile,at,small,wrapper,resetSeams,rect,seam};
})(typeof window === 'undefined' ? globalThis : window);
