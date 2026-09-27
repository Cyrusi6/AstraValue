"""Bounded, cached public-document transport for automatic research supplements."""
from datetime import datetime, timezone
from pathlib import Path
import json
import time
import warnings
from urllib.parse import urlparse
import requests
from .workspace import digest,sha,read_json,ResearchError


class Checkpoint(Exception):pass


def dump(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf8');tmp.replace(path)


class Transport:
    def __init__(self,root,seconds=70):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.deadline=time.monotonic()+seconds
        self.network_requests=0;self.cache_hits=0

    def invalidate(self,url,params=None):
        """Retain the response for diagnosis, but allow the next request to retry."""
        (self.root/(digest([url,params or {}])+'.json')).unlink(missing_ok=True)

    def fetch(self,url,params=None):
        ident=digest([url,params or {}]);meta=self.root/(ident+'.json')
        if meta.exists():
            old=read_json(meta)
            if sha(Path(old['path']))!=old['sha256']:raise ResearchError('supplement_cache_changed')
            self.cache_hits+=1;return old
        if time.monotonic()>self.deadline-5:raise Checkpoint()
        attempts=[(None,True),('http://127.0.0.1:7897',True)]
        # Only this public host has a documented certificate issue; record the fallback.
        if urlparse(url).hostname=='www.swsresearch.com':attempts.append((None,False))
        failures=[]
        for proxy,verify in attempts:
            remaining=self.deadline-time.monotonic()
            if remaining<5:raise Checkpoint()
            try:
                with requests.Session() as session:
                    session.trust_env=False
                    if proxy:session.proxies={'http':proxy,'https':proxy}
                    self.network_requests+=1
                    with warnings.catch_warnings():
                        warnings.simplefilter('ignore',requests.packages.urllib3.exceptions.InsecureRequestWarning)
                        with session.get(url,params=params,verify=verify,stream=True,
                            timeout=(min(5,remaining/3),min(12,remaining/3)),headers={'User-Agent':'Mozilla/5.0',
                            'Referer':'https://legulegu.com/stockdata/sw-industry-overview' if 'legulegu.com' in url else 'https://data.eastmoney.com/'}) as r:
                            parts=[];size=0
                            for part in r.iter_content(65536):
                                size+=len(part)
                                if size>8*1024*1024:raise ResearchError('supplement_response_too_large')
                                if time.monotonic()>self.deadline:raise Checkpoint()
                                parts.append(part)
                            content=b''.join(parts)
                            path=self.root/(ident+('-tls' if verify else '-fallback')+'.body');path.write_bytes(content)
                            info={'url':r.url,'path':str(path.resolve()),'sha256':sha(path),'tls_verified':verify,
                                'http_status':r.status_code,'content_type':r.headers.get('Content-Type'),
                                'observed_at':datetime.now(timezone.utc).isoformat()}
                            dump(path.with_suffix('.request.json'),info)
                            if r.status_code==200:
                                dump(meta,info);return info
                            failures.append('HTTP '+str(r.status_code))
            except requests.RequestException as exc:failures.append(type(exc).__name__)
        raise ResearchError('supplement_transport_failed:'+','.join(failures))
