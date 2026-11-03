"""Fixed adapter for an evidence-verified native local review draft."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from agent_core.native_review_registration import register


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('install-dir','packaging','draft-root','user-data','name','out'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--isolated-host',action='store_true')
    a=p.parse_args()
    try:
        r=register(a.install_dir,a.packaging,a.draft_root,a.user_data,a.name,a.out,allow_isolated_host=a.isolated_host)
        print(json.dumps({'status':r['status'],'destination':r['destination'],'local_review_only':True},ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'status':'failed','error_type':type(exc).__name__}),flush=True)
        return 2


if __name__=='__main__':raise SystemExit(main())
