"""Offline checks for this documentation-only repository. Python 3.10+."""
from __future__ import annotations
import ast,hashlib,json,re,sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote,urlsplit
from xml.etree import ElementTree

ROOT=Path(__file__).resolve().parents[1]
ALLOWED_TOP={'.github','assets','docs','evidence','examples','scripts',
    '.gitignore','README.md','CHANGELOG.md','CONTRIBUTING.md','SECURITY.md','LICENSE','THIRD_PARTY_NOTICES.md','SHA256SUMS.txt'}
BANNED_SUFFIXES={'.exe','.dll','.onnx','.safetensors','.sqlite','.sqlite3','.db','.mp4','.wav','.zip','.rar','.log'}
SCAN_SUFFIXES={'.md','.json','.yml','.yaml','.svg','.py','.txt'}

class AssetLinks(HTMLParser):
    def __init__(self):
        super().__init__();self.links=[]
    def handle_starttag(self,tag,attrs):
        for name,value in attrs:
            if not value:continue
            if name in {'src','href'}:self.links.append(value)
            elif name=='srcset':
                self.links.extend(item.strip().split()[0] for item in value.split(',') if item.strip())
def check():
    errors=[];files=[p for p in ROOT.rglob('*') if p.is_file() and not any(x in {'.git','__pycache__'} for x in p.relative_to(ROOT).parts)]
    for p in files:
        rel=p.relative_to(ROOT);label=rel.as_posix()
        if rel.parts[0] not in ALLOWED_TOP:errors.append(label+': unexpected top-level item')
        if p.suffix.lower() in BANNED_SUFFIXES:errors.append(label+': excluded binary/private artifact')
        if p.is_symlink():errors.append(label+': symlink is not allowed')
        if p.stat().st_size>20*1024*1024:errors.append(label+': exceeds documentation asset budget')
        if p.name.startswith('.env') or p.name in {'grants.json','grants.pending.json','runtime-config.json','settings.json'}:errors.append(label+': private filename')
        if p.suffix.lower() in SCAN_SUFFIXES or p.name=='LICENSE':
            text=p.read_text('utf-8-sig')
            secret_patterns=[r'jyg_'+'[A-Za-z0-9_-]{24,}',r'(?<![A-Za-z])sk-'+'[A-Za-z0-9_-]{24,}',r'-----BEGIN '+'(?:RSA |EC |OPENSSH )?PRIVATE KEY-----']
            if any(re.search(pattern,text) for pattern in secret_patterns):errors.append(label+': credential-like content')
            if re.search(r'[A-Z]:[/\\](?:Users[/\\][^\s/\\]+|JianyingAgentWork|JianyingRestored)',text,re.I):errors.append(label+': owner-specific local path')
            if re.search(r'\b(?:job|batch|asset)_[0-9a-f]{16,64}\b',text):errors.append(label+': unsanitized private identifier')
        if p.suffix=='.md':
            html_links=AssetLinks();html_links.feed(text)
            for raw in re.findall(r'!?\[[^\]\n]*\]\(([^)\n]+)\)',text)+html_links.links:
                target=raw.strip().split(' "',1)[0].strip('<>')
                url=urlsplit(target)
                if url.scheme or target.startswith(('#','//')):continue
                dest=(ROOT/unquote(url.path.lstrip('/'))) if target.startswith('/') else (p.parent/unquote(url.path))
                dest=dest.resolve()
                if not dest.is_relative_to(ROOT):errors.append(label+': link leaves public repository: '+target)
                elif not dest.exists():errors.append(label+': broken local link: '+target)
        if p.suffix=='.json':
            try:json.loads(text)
            except ValueError as e:errors.append(label+': invalid JSON: '+str(e))
        if p.suffix=='.svg':
            try:ElementTree.fromstring(text)
            except ElementTree.ParseError as e:errors.append(label+': invalid SVG: '+str(e))
        if p.suffix=='.py':
            try:ast.parse(text)
            except SyntaxError as e:errors.append(label+': invalid Python: '+str(e))
    manifest=ROOT/'SHA256SUMS.txt'
    if manifest.exists():
        rows=[line.split('  ',1) for line in manifest.read_text('utf-8').splitlines() if line.strip()]
        expected={p.relative_to(ROOT).as_posix() for p in files if p!=manifest}
        if {row[1] for row in rows}!=expected:errors.append('SHA256SUMS.txt: file list differs from repository')
        for wanted,name in rows:
            p=ROOT/name
            if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest()!=wanted:errors.append(name+': manifest hash mismatch')
    summary={'files':len(files),'markdown_documents':sum(p.suffix=='.md' for p in files),
        'local_links_and_formats_checked':True,'html_asset_references_checked':True,'common_sensitive_patterns_checked':True,
        'manifest_checked':manifest.exists(),'errors':errors,
        'scope':'Offline documentation checks; not a security audit, application test, or remote-link check'}
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 1 if errors else 0
if __name__=='__main__':raise SystemExit(check())
