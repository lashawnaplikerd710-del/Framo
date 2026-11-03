"""Owner-only grant management. Real model keys are never read by this tool."""
import argparse
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

HERE=Path(__file__).resolve().parents[1]
MAX_DAILY_UNITS=10000
MAX_ACCESS_DAYS=184  # Covers any six-calendar-month grant.
sys.path.insert(0,str(HERE.parent/'app'))
from agent_core.task_store import atomic_json
from agent_core.local_executor import draft_lock

def show_code(client_id,token):
    import tkinter as tk
    from tkinter import ttk
    root=tk.Tk();root.title('工作台访问码 · '+client_id);root.geometry('610x200');root.resizable(False,False)
    ttk.Label(root,text='已开通 '+client_id+'。将下面的工作台访问码单独发给对应朋友。',padding=18).pack(anchor='w')
    field=ttk.Entry(root,width=70);field.insert(0,token);field.configure(state='readonly');field.pack(padx=18,pady=10)
    ttk.Label(root,text='此窗口关闭后不再显示原码。遗失时使用 rotate 重新发放。').pack(pady=4)
    def copy():root.clipboard_clear();root.clipboard_append(token);button.configure(text='已复制')
    button=ttk.Button(root,text='复制访问码',command=copy);button.pack(pady=10)
    root.mainloop()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['list','issue','rotate','revoke','renew','quota'])
    parser.add_argument('--id');parser.add_argument('--days',type=int,default=30);parser.add_argument('--units',type=int,default=80)
    parser.add_argument('--max-requests',type=int,help='Lifetime model-request cap; does not reset at midnight')
    parser.add_argument('--node',default='node');args=parser.parse_args()
    registry=HERE/'private/grants.json'
    if not registry.exists():raise ValueError('Provision the gateway first')
    with draft_lock(HERE/'private/.grants.lock'):
        clients=json.loads(registry.read_text(encoding='utf-8'))
        if args.action=='list':
            print(json.dumps([{k:v for k,v in c.items() if k!='token_hash'} for c in clients],ensure_ascii=False,indent=2));return 0
        if not args.id or not re.fullmatch(r'[a-z0-9_-]{1,48}',args.id) or args.id in ('__proto__','constructor','prototype') or (args.id=='owner-test' and args.action not in ('renew','quota')):
            raise ValueError('Use a friend id such as friend-01; owner-test only supports renew/quota')
        if not 1<=args.days<=MAX_ACCESS_DAYS or not 1<=args.units<=MAX_DAILY_UNITS:raise ValueError(f'Days must be 1..{MAX_ACCESS_DAYS}; units must be 1..{MAX_DAILY_UNITS}')
        if args.max_requests is not None and not 1<=args.max_requests<=1000000:
            raise ValueError('max-requests must be 1..1000000')
        existing=next((c for c in clients if c['id']==args.id),None)
        if args.action=='issue' and existing:raise ValueError('Already issued. Use rotate to replace its code')
        if args.action in ('rotate','revoke','renew','quota') and not existing:raise ValueError('Unknown client id')
        token=None
        if args.action=='revoke':existing['enabled']=False
        elif args.action=='quota':existing['daily_units']=args.units
        elif args.action=='renew':existing.update(enabled=True,expires_at=(datetime.now(timezone.utc)+timedelta(days=args.days)).isoformat(),daily_units=args.units)
        else:
            token='jyg_'+secrets.token_urlsafe(32)
            replacement={'id':args.id,'token_hash':hashlib.sha256(token.encode()).hexdigest(),'enabled':True,
                'expires_at':(datetime.now(timezone.utc)+timedelta(days=args.days)).isoformat(),
                'daily_units':args.units,'max_concurrent':1,'models':['deepseek-flash','qwen3.8-omni-flash','qwen3.8-max']}
            if existing and 'max_requests' in existing:replacement['max_requests']=existing['max_requests']
            if existing:clients[clients.index(existing)]=replacement
            else:clients.append(replacement)
        if args.max_requests is not None and args.action!='revoke':
            next(c for c in clients if c['id']==args.id)['max_requests']=args.max_requests
        if len(clients)>50:raise ValueError('Friend preview supports at most 50 grants')
        pending=HERE/'private/grants.pending.json';atomic_json(pending,clients)
        environment={k:v for k,v in os.environ.items() if not k.endswith(('_API_KEY','_TOKEN'))}
        environment['WRANGLER_SEND_METRICS']='false'
        proc=subprocess.run([args.node,str(HERE/'node_modules/wrangler/bin/wrangler.js'),'secret','put','AUTH_CLIENTS'],
            input=json.dumps(clients,separators=(',',':')),encoding='utf-8',text=True,capture_output=True,cwd=HERE,env=environment,timeout=120,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=='win32' else 0)
        if proc.returncode:raise RuntimeError('Access update not confirmed. No model key was read. Re-run the intended update after checking Cloudflare connectivity; the pending hash registry is preserved.')
        atomic_json(registry,clients)
        print(json.dumps({'status':'updated','client_id':args.id,'action':args.action,'token_in_console':False}))
    if token:show_code(args.id,token)
    return 0

if __name__=='__main__':
    try:raise SystemExit(main())
    except (ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
        print(json.dumps({'status':'error','message':str(exc)}));raise SystemExit(2)
