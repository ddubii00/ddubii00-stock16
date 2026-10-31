"""Temasek Review is independent from GIC and never expanded by inference."""
from .base import DatasetCollector,NewsFeedCollector,NeedsConfiguration
from .discovery import ReviewedDiscoveryCollector


class TemasekCollector:
    def __init__(self,repo,transport,config):
        self.repo,self.transport,self.config=repo,transport,config

    def run(self):
        config={'id':'temasek',**self.config}
        if not any(config.get(k) for k in ('datasets','feeds','discovery_rules')):
            raise NeedsConfiguration('공식 데이터 자동수집 불가 / 수동 공식자료 import 필요 · Temasek Review')
        count=0
        if config.get('datasets'):
            count+=DatasetCollector(self.repo,self.transport,config).run()
        if config.get('discovery_rules'):
            count+=ReviewedDiscoveryCollector(self.repo,self.transport,config).run()
        for feed in config.get('feeds',[]):
            count+=NewsFeedCollector(self.repo,self.transport,'temasek',feed).run()
        return count
