"""Isolated local speech entry point; never executes user text as code."""
import json
from pathlib import Path
import sys

if __name__ == '__main__':
    request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    root = Path(request['root']).resolve()
    sys.path.insert(0, str(root/'python'))
    from local_voice import synthesize_request
    try:
        response = synthesize_request(request)
    except Exception as exc:
        Path(request['result']).write_text(json.dumps({'error':str(exc)}, ensure_ascii=False), encoding='utf-8')
        raise SystemExit(1)
    Path(request['result']).write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding='utf-8')
