"""Download optional fonts from frozen upstream URLs, verifying each SHA256."""
import argparse,hashlib,json
from pathlib import Path
from urllib.request import urlopen

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--all',action='store_true');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]/'app/desktop-assets/media-library/font-library'
    data=json.loads((root/'manifest.json').read_text('utf-8'))
    chosen=set(data['default_families'].values())
    for family in data['families']:
        if not args.all and family['id'] not in chosen:continue
        for item in family['files']:
            dest=(root/item['file']).resolve()
            if not dest.is_relative_to(root.resolve()):raise ValueError('Invalid font destination')
            if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest()==item['sha256']:continue
            if not item['source_url'].startswith('https://'):raise ValueError('HTTPS required')
            with urlopen(item['source_url'],timeout=120) as response:content=response.read(64*1024*1024)
            if hashlib.sha256(content).hexdigest()!=item['sha256']:raise ValueError('Font hash mismatch: '+family['id'])
            dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(content)
            print('Verified:',item['file'])
if __name__=='__main__':main()
