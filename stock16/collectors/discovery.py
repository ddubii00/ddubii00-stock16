"""Reviewed link discovery: transient HTML, strict mappings and explicit dates."""
from html.parser import HTMLParser
import json
import re
from urllib.parse import urljoin
from .base import DatasetCollector,NeedsConfiguration


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links=[]
        self.current=None

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='a':
            self.current=[attrs.get('href',''),[]]

    def handle_data(self,data):
        if self.current:
            self.current[1].append(data)

    def handle_endtag(self,tag):
        if tag=='a' and self.current:
            self.links.append((self.current[0],' '.join(self.current[1])))
            self.current=None


class ReviewedDiscoveryCollector(DatasetCollector):
    def discover(self):
        rules=self.config.get('discovery_rules',[])
        if not rules:
            raise NeedsConfiguration('공식 파일 수동 등록 필요 · 검증한 다운로드 discovery mapping 없음')
        items=[]
        for rule in rules:
            if not rule.get('reviewed') or not rule.get('mapping'):
                raise NeedsConfiguration('파일 구조·단위·공개범위 검증 후 mapping 등록 필요')
            body,_,final=self.transport.fetch(rule['index_url'],{'text/html'})
            parser=Links();parser.feed(body.decode(rule.get('page_encoding','utf-8')))
            for href,label in parser.links:
                url=urljoin(final,href)
                match=re.search(rule['link_pattern'],url)
                if not match or not re.search(rule.get('label_pattern','.*'),label):
                    continue
                self.transport.validate_url(url)
                metadata=dict(rule['metadata'])
                metadata.update({k:v for k,v in match.groupdict().items() if v is not None and k in ('as_of_date','published_at')})
                if not metadata.get('published_at'):
                    raise NeedsConfiguration('공식 발표일 검증 필요')
                items.append({**rule,'metadata':metadata,'url':url})
        if not items:
            raise NeedsConfiguration('검증한 구조의 공식 다운로드 미발견 / 수동 공식자료 import 필요')
        # One workbook may expose domestic/foreign sheets for different funds.
        # Deduplicate rules, never discard a distinct extraction of the same blob.
        unique={json.dumps(item,sort_keys=True,ensure_ascii=False):item for item in items}
        return list(unique.values())
