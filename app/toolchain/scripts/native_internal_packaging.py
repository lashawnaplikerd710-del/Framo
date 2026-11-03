"""Fixed subprocess entry for the Agent's official-resource packaging adapter."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from agent_core.native_packaging import package


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('install-dir','draft','request','out'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--isolated-host',action='store_true',required=True)
    args=parser.parse_args()
    try:
        result=package(args.install_dir,args.draft,
            json.loads(Path(args.request).read_text(encoding='utf-8-sig')),args.out,
            allow_isolated_host=args.isolated_host,resume=True)
        print(json.dumps(result,ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({'status':'native_packaging_failed','error_type':type(exc).__name__}))
        return 2


if __name__=='__main__':raise SystemExit(main())
