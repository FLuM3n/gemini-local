"""Build clean Windows and macOS assets from explicit program-file lists."""
from pathlib import Path
import argparse
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = ['gemini_terminal.py','gemini_api.py','gemini_auth.py','gemini_browser_link.py',
              'bootstrap.py','Gemini.bat','Gemini.command','requirements.txt','VERSION',
              'README.md','LICENSE','THIRD_PARTY_NOTICES.md','.env.example']


def program_files():
    files = {name: (ROOT/name).read_bytes() for name in ROOT_FILES}
    for directory in ['browser-extension','vendor','tests','tools']:
        for file in (ROOT/directory).rglob('*'):
            if not file.is_file() or '__pycache__' in file.parts or file.suffix in {'.pyc','.pyo'}:
                continue
            files[file.relative_to(ROOT).as_posix()] = file.read_bytes()
    return files


def build(files, target):
    hashes = {name: hashlib.sha256(data).hexdigest() for name,data in sorted(files.items())}
    files = {**files, 'SHA256SUMS.json': (json.dumps(hashes,indent=2)+'\n').encode()}
    with zipfile.ZipFile(target,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as archive:
        for name,data in sorted(files.items()):
            assert not name.startswith('/') and '..' not in Path(name).parts
            entry = zipfile.ZipInfo('Gemini-Local/'+name,date_time=(2026,11,18,0,0,0))
            entry.create_system = 3
            mode = 0o100755 if name == 'Gemini.command' else 0o100644
            entry.external_attr = mode << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry,data,compresslevel=9)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        saved=json.loads(archive.read('Gemini-Local/SHA256SUMS.json'))
        for name,digest in saved.items(): assert hashlib.sha256(archive.read('Gemini-Local/'+name)).hexdigest()==digest
        assert archive.getinfo('Gemini-Local/Gemini.command').external_attr >> 16 & 0o111
    return {'name':target.name,'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--windows-runtime',type=Path,help='Existing portable package runtime directory')
    args=parser.parse_args()
    version=(ROOT/'VERSION').read_text().strip()
    output=ROOT/'dist';output.mkdir(exist_ok=True)
    base=program_files()
    reports=[build(base,output/f'Gemini-Local-macOS-v{version}.zip')]
    if args.windows_runtime:
        runtime=args.windows_runtime.resolve()
        manifest=json.loads((runtime.parent/'SHA256SUMS.json').read_text(encoding='utf-8'))
        files=dict(base)
        for name in manifest:
            if not name.startswith(('runtime/','licenses/')) or '__pycache__' in Path(name).parts or name.endswith(('.pyc','.pyo')):
                continue
            file=runtime.parent/name
            assert file.is_file(),name
            files[name]=file.read_bytes()
        files['components.json']=(runtime.parent/'components.json').read_bytes()
        reports.append(build(files,output/f'Gemini-Local-Windows-x64-v{version}.zip'))
    (output/'SHA256SUMS.txt').write_text(''.join(item['sha256']+'  '+item['name']+'\n' for item in reports),encoding='ascii',newline='\n')
    (output/'release-assets.json').write_text(json.dumps(reports,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(reports))


if __name__=='__main__':main()
