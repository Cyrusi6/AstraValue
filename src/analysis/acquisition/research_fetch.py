"""所选研究原件的有界获取；原始 bytes 与请求账本分开保存。"""
from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx


class ResearchFetch:
    def __init__(self, root: Path, *, refresh=False):
        self.root=root; self.refresh=refresh; self.last=0.0
        root.mkdir(parents=True,exist_ok=True)
        self.requests=[]

    def fetch(self, url, *, data=None, max_bytes=100*1024*1024):
        host=urlsplit(url).hostname or ''
        if urlsplit(url).scheme!='https' or not (host.endswith('.cninfo.com.cn') or host in {'www.cninfo.com.cn','data.stats.gov.cn','www.stats.gov.cn'}):
            raise ValueError('unregistered_research_document_host')
        identity=json.dumps({'url':url,'data':data},sort_keys=True,ensure_ascii=False,separators=(',',':'))
        key=hashlib.sha256(identity.encode()).hexdigest(); index=self.root/'requests'/f'{key}.json'
        if index.exists() and not self.refresh:
            saved=json.loads(index.read_text(encoding='utf8')); path=(self.root/saved['relative_path']).resolve()
            if not path.is_relative_to(self.root.resolve()):raise ValueError('cache_path_outside_root')
            if path.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==saved['sha256'] and saved['http_status']==200:
                reused=saved|{'cache_reused':True}
                self.requests.append(reused); return path,reused
        time.sleep(max(0,1-(time.monotonic()-self.last)))
        attempts=[]
        for proxy in (None,'http://127.0.0.1:7897'):
            self.last=time.monotonic()
            try:
                with httpx.Client(timeout=40,trust_env=False,proxy=proxy,follow_redirects=False,headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.cninfo.com.cn/'}) as client:
                    with client.stream('POST' if data else 'GET',url,data=data) as response:
                        chunks=[]; size=0
                        for chunk in response.iter_bytes():
                            size+=len(chunk)
                            if size>max_bytes: raise ValueError('response_size_exceeded')
                            chunks.append(chunk)
                        body=b''.join(chunks)
                        sha=hashlib.sha256(body).hexdigest(); relative=f'raw/blobs/sha256/{sha[:2]}/{sha}'
                        path=self.root/relative; path.parent.mkdir(parents=True,exist_ok=True)
                        if not path.resolve().is_relative_to(self.root.resolve()):raise ValueError('original_path_outside_root')
                        if path.exists() and (actual:=hashlib.sha256(path.read_bytes()).hexdigest())!=sha:
                            quarantine=self.root/'raw'/'quarantine'/sha/actual
                            if not quarantine.resolve().is_relative_to(self.root.resolve()):raise ValueError('quarantine_path_outside_root')
                            quarantine.parent.mkdir(parents=True,exist_ok=True)
                            path.replace(quarantine)
                        if not path.exists():
                            temp=path.with_suffix('.tmp');temp.write_bytes(body);temp.replace(path)
                        saved={'request':json.loads(identity),'request_key':key,'http_status':response.status_code,
                            'mime_type':response.headers.get('content-type','application/octet-stream'),'sha256':sha,'relative_path':relative,
                            'observed_at':datetime.now(timezone.utc).isoformat(),'proxy_used':proxy is not None,'attempts':attempts,'cache_reused':False}
                        index.parent.mkdir(parents=True,exist_ok=True)
                        # Preserve each acquisition version before moving the cache pointer.
                        history=index.parent/'history'/key
                        history.mkdir(parents=True,exist_ok=True)
                        if index.exists():
                            old=index.read_bytes(); old_key=hashlib.sha256(old).hexdigest()
                            (history/(old_key+'.json')).write_bytes(old)
                        temp=index.with_suffix('.tmp')
                        temp.write_text(json.dumps(saved,ensure_ascii=False,sort_keys=True)+'\n',encoding='utf8')
                        temp.replace(index)
                        self.requests.append(saved)
                        if response.status_code!=200: raise ValueError(f'http_status:{response.status_code}')
                        return path,saved
            except httpx.TransportError as exc:
                attempts.append({'proxy':proxy,'error':f'{type(exc).__name__}:{exc}'})
        failure={'request':json.loads(identity),'attempts':attempts,'state':'transport_failed'}
        self.requests.append(failure)
        with (self.root/'failures.jsonl').open('a',encoding='utf8') as stream:
            stream.write(json.dumps(failure,ensure_ascii=False)+'\n')
        raise RuntimeError('all_source_transport_paths_failed')
