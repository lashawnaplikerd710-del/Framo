// Keep the owned native player producing frames; no display or audio recording.
import net from 'node:net';
import fs from 'node:fs';
const [endpoint,session,ready,width,height] = process.argv.slice(2);
const client=net.connect(endpoint);let buffer='';
client.on('connect',()=>client.write(JSON.stringify({id:'frame-subscription',protocolVersion:1,
 domain:'player',operation:'subscribeFrames',targetId:session,
 params:{width:Number(width),height:Number(height),fps:10,preferredTransport:'shared-memory-ring-v1'}})+'\n'));
client.on('data',chunk=>{
 buffer+=chunk.toString('utf8');
 if(buffer.length>8*1024*1024){client.destroy();process.exit(2);}
 for(;;){
  const i=buffer.indexOf('\n');if(i<0)break;
  const line=buffer.slice(0,i).trim();buffer=buffer.slice(i+1);if(!line)continue;
  let msg;try{msg=JSON.parse(line);}catch{process.exit(2);}
  if(msg.id==='frame-subscription'){
   if(!msg.ok)process.exit(2);
   fs.writeFileSync(ready,JSON.stringify(msg),{flag:'wx'});
  }
  if(msg.type==='sharedMemoryFrame')client.write(JSON.stringify({type:'releaseSharedMemorySlot',
   sessionId:msg.sessionId,name:msg.name,mappingGeneration:msg.mappingGeneration,
   slotIndex:msg.slotIndex,publishToken:msg.publishToken})+'\n');
 }
});
client.on('error',()=>process.exit(2));
client.on('close',()=>process.exit(0));
setTimeout(()=>client.destroy(),180000);
