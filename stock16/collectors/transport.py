"""Allowlisted HTTPS, bounded responses, redirects checked and robots respected."""
import ipaddress
import socket
import time
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
import requests

OFFICIAL_HOSTS={'fund.nps.or.kr','www.nps.or.kr','www.nbim.no','nbim.no',
 'www.gic.com.sg','gic.com.sg','www.temasek.com.sg','www.temasekreview.com.sg',
 'data.krx.co.kr','openapi.krx.co.kr','www.samsungfund.com','m.samsungfund.com',
 'www.tigeretf.com','www.sec.gov','data.sec.gov','opendart.fss.or.kr'}
OFFICIAL_HOSTS.update({'tigeretf.com','www.riseetf.co.kr','riseetf.co.kr',
 'www.aceetf.co.kr','aceetf.co.kr','www.soletf.co.kr','soletf.co.kr',
 'www.plusetf.co.kr','plusetf.co.kr'})


class FetchError(RuntimeError):
    def __init__(self,message,status_code=None,retry_after=None):
        super().__init__(message)
        self.status_code=status_code
        self.retry_after=retry_after


class Transport:
    def __init__(self,user_agent='stock16/1.0',max_bytes=25*1024*1024):
        self.user_agent=user_agent
        self.max_bytes=max_bytes
        self.last_request=0
        self.http=requests.Session()
        self.http.trust_env=False
        self.robots={}

    def validate_url(self,url):
        p=urlsplit(url)
        if p.scheme!='https' or p.username or p.password or p.port not in (None,443) or p.hostname not in OFFICIAL_HOSTS:
            raise FetchError('URL is not an approved official HTTPS source')
        for result in socket.getaddrinfo(p.hostname,443,type=socket.SOCK_STREAM):
            if not ipaddress.ip_address(result[4][0]).is_global:
                raise FetchError('Non-public upstream address rejected')

    def fetch(self,url,types,robots=True):
        """Do not log URLs: DART query strings may contain an API key."""
        try:
            return self._fetch(url,types,robots)
        except requests.HTTPError as error:
            response=error.response
            code=response.status_code if response is not None else None
            raise FetchError('Official upstream HTTP '+str(code),status_code=code,
                             retry_after=response.headers.get('Retry-After') if response is not None else None) from None
        except (requests.RequestException,OSError) as e:
            raise FetchError('Upstream unavailable or timeout ('+type(e).__name__+')') from None

    def _fetch(self,url,types,robots):
        for _ in range(4):
            self.validate_url(url)
            p=urlsplit(url)
            origin=p.scheme+'://'+p.netloc
            if robots and origin not in self.robots:
                content,_,_=self.fetch(origin+'/robots.txt',{'text/plain'},robots=False)
                rp=RobotFileParser()
                rp.parse(content.decode('utf-8',errors='replace').splitlines())
                self.robots[origin]=rp
            if robots and not self.robots[origin].can_fetch(self.user_agent,url):
                raise FetchError('Official robots policy disallows automated access')
            time.sleep(max(0,0.25-(time.monotonic()-self.last_request)))
            self.last_request=time.monotonic()
            with self.http.get(url,headers={'User-Agent':self.user_agent,'Accept-Encoding':'identity'},timeout=(5,20),stream=True,allow_redirects=False) as r:
                # A missing robots.txt permits standard API access. Other failures fail closed.
                if not robots and r.status_code==404:
                    return b'', 'text/plain',url
                if r.status_code in (301,302,303,307,308):
                    url=urljoin(url,r.headers.get('Location',''))
                    continue
                r.raise_for_status()
                media=r.headers.get('Content-Type','').split(';')[0].strip().lower()
                if media not in types:
                    raise FetchError('Unexpected upstream content type')
                if int(r.headers.get('Content-Length','0'))>self.max_bytes:
                    raise FetchError('Upstream file too large')
                chunks=[]
                size=0
                started=time.monotonic()
                for chunk in r.iter_content(65536):
                    size+=len(chunk)
                    if size>self.max_bytes or time.monotonic()-started>60:
                        raise FetchError('Upstream size/time limit exceeded')
                    chunks.append(chunk)
                return b''.join(chunks),media,url
        raise FetchError('Too many upstream redirects')
