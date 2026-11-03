"""Verify deployed auth boundaries without sending media or spending model quota."""
import json
from pathlib import Path
import sys
import urllib.error
import urllib.request
import uuid
HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE.parent/'app'))
from agent_core.credential_store import load_key
from agent_core.gateway_credentials import target
from agent_core.provider import NoRedirect
from agent_core.task_store import atomic_json

def main():
    config=json.loads((HERE.parent/'agent-runtime/config.gateway.local.json').read_text(encoding='utf-8'))
    base=config['provider']['base_url'];token=load_key(target=target(config['provider']))
    def call(path,auth=None,payload=None):
        headers={'User-Agent':'Jianying-Agent-Workbench/0.1'}
        if auth:headers['Authorization']='Bearer '+auth
        body=None
        if payload is not None:
            body=json.dumps(payload).encode();headers.update({'Content-Type':'application/json','X-Request-Id':str(uuid.uuid4())})
        req=urllib.request.Request(base+path,data=body,headers=headers)
        try:
            with urllib.request.build_opener(NoRedirect()).open(req,timeout=20) as response:return response.status,json.loads(response.read(20000))
        except urllib.error.HTTPError as e:return e.code,json.loads(e.read(20000))
    before=call('/status',token)
    assert before[0]==200
    assert call('/status')[0]==401
    assert call('/status','jyg_'+'x'*43)[0]==401
    assert call('/chat/completions',token,{'model':'unapproved-model'})[0]==403
    assert call('/proxy?url=https://example.com',token)[0] in (403,404)
    after=call('/status',token)
    assert before[1]['used_units']==after[1]['used_units']
    result={'status':'passed','auth_and_allowlist_checks':5,'no_provider_request':True,'quota_unchanged':True,'endpoint':base}
    atomic_json(HERE.parent/'agent-runtime/data/gateway-onboarding/live-boundaries.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
