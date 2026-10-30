"""Local, service-user access to a supervised OpenCode gateway; no laser API."""
import argparse
import base64
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import urllib.parse
import urllib.request


def access_root(create=False):
    root=Path.home()/'.local/state/ldcon/gateway-access'
    if any(p.is_symlink() for p in (root,*root.parents)):
        raise ValueError('Gateway access storage must not use symlinks')
    if create:root.mkdir(parents=True,exist_ok=True,mode=0o700);root.chmod(0o700)
    return root


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None


def request(url,password,method,path,body=None,timeout=10):
    parsed=urllib.parse.urlsplit(url)
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.path or parsed.username or parsed.query or parsed.fragment:
        raise ValueError('Gateway server must be authenticated loopback HTTP')
    if not path.startswith('/api/') or path.startswith('//'):raise ValueError('Invalid gateway API route')
    headers={'Authorization':'Basic '+base64.b64encode(('opencode:'+password).encode()).decode(),'Content-Type':'application/json'}
    req=urllib.request.Request(url+path,data=None if body is None else json.dumps(body).encode(),method=method,headers=headers)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    try:
        with opener.open(req,timeout=timeout) as response:
            raw=response.read(16*1024*1024+1)
            if len(raw)>16*1024*1024:raise RuntimeError('OpenCode response exceeded the bounded read budget')
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        # Provider/server bodies may contain paths, prompts or credentials.
        raise RuntimeError(f'OpenCode HTTP request rejected ({error.code}); inspect the native session. No request was retried.') from None


def record(value):
    from ldcon.core.records import atomic_json
    root=access_root(True)
    if any(not re.fullmatch('[a-zA-Z0-9_-]+',value[k]) for k in ('laser_id','job_id')):raise ValueError('Invalid gateway identity')
    path=root/(value['laser_id']+'-'+value['job_id']+'.json')
    atomic_json(path,value);path.chmod(0o600)
    return path


def find(laser):
    from ldcon.jobs.job_service import identity
    if not re.fullmatch('[a-zA-Z0-9_-]+',laser):raise ValueError('Invalid laser identity')
    matches=[]
    for p in access_root().glob(laser+'-*.json'):
        info=p.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>65536:continue
        value=json.loads(p.read_text())
        if value.get('laser_id')==laser and identity(value['worker']['pid'])==value['worker']:
            matches.append(value)
    if len(matches)!=1:raise RuntimeError('Expected one live OpenCode gateway for this laser')
    value=matches[0]
    current=request(value['url'],value['password'],'GET','/api/session/'+value['session_id'])['data']
    if current.get('location',{}).get('directory')!=value['workspace']:
        raise RuntimeError('Gateway session workspace differs; reconnect through LDCon')
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('laser')
    parser.add_argument('--web',action='store_true',help='Print a one-use browser pairing link and SSH forwarding command')
    parser.add_argument('--status',action='store_true',help='Print only public connection information')
    args=parser.parse_args();v=find(args.laser)
    if args.status:print(json.dumps({k:v[k] for k in ('laser_id','url','session_id','workspace')},indent=2));return
    if args.web:
        pairing=request(v['url'],v['password'],'POST','/api/pair',{})
        code=pairing.get('code')
        if not isinstance(code,str) or not re.fullmatch('[A-Za-z0-9_-]+',code):raise RuntimeError('Unexpected pairing response; no credential printed')
        port=urllib.parse.urlsplit(v['url']).port
        print(f'From your computer: ssh -N -L {port}:127.0.0.1:{port} <your-login>@<laser-host>')
        print('Then open this one-use link in that computer\'s browser:')
        print(v['url']+'/auth/connect/'+code)
        print('Select session '+v['session_id']+'. The terminal and Web use this same session.')
        return
    # Keep the credential out of argv and shell history. Native CLI performs auth.
    env={**os.environ,'OPENCODE_PASSWORD':v['password']}
    raise SystemExit(subprocess.call([v['binary'],'--server',v['url'],'--session',v['session_id']],cwd=v['workspace'],env=env))


if __name__=='__main__':main()
