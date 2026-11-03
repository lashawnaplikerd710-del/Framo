// Native card choreography, with output-clock continuity at scene seams.
(()=>{
if(window.PACK_LAYOUT?.name!=='portrait')throw Error('This style requires portrait');
const original=window.renderAt,from=[];let initialized=false;
const neutral=()=>document.getElementById('world').style.transform='';
const body=i=>SCENES[i].el.querySelector('.tkc');
window.renderAt=async t=>{ADU_PRESENTER.resetSeams();await original(t);await imgWait();neutral();if(!initialized)return;const f=window.MACRO_OUTPUT_T*MACRO_PLAN.fps,i=MACRO_PLAN.scenes.findIndex(s=>f>=s.output_start_frame&&f<s.output_end_frame);if(i<0)return;const e=body(i);ADU_PRESENTER.seam(e,e,from[i],(f-MACRO_PLAN.scenes[i].output_start_frame)/MACRO_PLAN.fps,faceAt(window.MACRO_OUTPUT_T));};
window.READY=Promise.resolve(window.READY).then(async()=>{
 for(let i=0;i<SCENES.length;i++)ADU_PRESENTER.wrapper(body(i));
 for(let i=1;i<SCENES.length;i++){ADU_PRESENTER.resetSeams();await original((MACRO_PLAN.scenes[i-1].output_end_frame-1)/MACRO_PLAN.fps);await imgWait();neutral();const e=body(i-1);if(e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))from[i]=ADU_PRESENTER.rect(e);}
 initialized=true;await window.renderAt(0);
});
})();
