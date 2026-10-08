"""Publish tested installers with the repository's existing GitHub credentials.

Credentials are read in memory from Git Credential Manager, never logged or saved.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import urllib.error
import urllib.parse
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
REPOSITORY='De-guy-with-1-brain-c3ll/A.T.H.E.N.A'

def credentials():
    env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'never'}
    result=subprocess.run(['git','credential','fill'],input='protocol=https\nhost=github.com\n\n',capture_output=True,text=True,env=env,timeout=45)
    values=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    token=values.get('password')
    if not token:raise RuntimeError('Sign in to GitHub using Git Credential Manager before publishing.')
    return token

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--tag',required=True);parser.add_argument('--notes',type=Path,required=True);parser.add_argument('--check-auth',action='store_true');args=parser.parse_args()
    token=credentials()
    def api(path,data=None,method=None,content_type='application/json'):
        address=path if path.startswith('https://uploads.github.com/') else 'https://api.github.com'+path
        if not address.startswith(('https://api.github.com/','https://uploads.github.com/')):raise ValueError('Invalid release endpoint.')
        request=urllib.request.Request(address,data=data,method=method,headers={'Authorization':'Bearer '+token,'Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28','Content-Type':content_type,'User-Agent':'ATHENA-release-builder'})
        with urllib.request.urlopen(request,timeout=300) as response:return json.loads(response.read())
    user=api('/user')
    print('Authenticated GitHub account:',user['login'])
    if args.check_auth:return
    base=f'/repos/{REPOSITORY}/releases'
    # Finish uploading into a draft; users only see the complete package set.
    try:release=api(base+'/tags/'+urllib.parse.quote(args.tag,safe=''))
    except urllib.error.HTTPError as error:
        if error.code!=404:raise
        revision=subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()
        release=api(base,json.dumps({'tag_name':args.tag,'target_commitish':revision,'name':'ATHENA '+args.tag.removeprefix('v')+' — Windows and Linux installers','body':args.notes.read_text(encoding='utf-8'),'draft':True}).encode(),method='POST')
    files=[ROOT/'dist/installers'/name for name in ('ATHENA-Standalone-Windows-Setup.exe','ATHENA-Companion-Windows-Setup.exe','ATHENA-Linux-Setup.run','SHA256SUMS.txt')]
    assets={item['name']:item for item in release['assets']}
    for path in files:
        payload=path.read_bytes();digest='sha256:'+hashlib.sha256(payload).hexdigest()
        if path.name in assets:
            if assets[path.name].get('digest')!=digest:raise RuntimeError('An existing release asset has different contents: '+path.name)
            continue
        result=api(release['upload_url'].split('{')[0]+'?name='+urllib.parse.quote(path.name),payload,'POST','application/octet-stream')
        if result.get('size')!=len(payload) or result.get('digest')!=digest:raise RuntimeError('Uploaded asset verification failed: '+path.name)
        print('Uploaded and verified:',path.name)
    release=api(base+'/'+str(release['id']),json.dumps({'draft':False}).encode(),method='PATCH')
    print('Published:',release['html_url'])

if __name__=='__main__':main()
