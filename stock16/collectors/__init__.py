import json
from datetime import datetime,timedelta,timezone
from ..settings import ROOT
from ..model import now
from .base import DatasetCollector, NewsFeedCollector, NeedsConfiguration
from .transport import Transport
from .sec_13f import SEC13FCollector
from .dart import DARTCollector
from .nps import NPSCollector
from .nbim import NBIMCollector
from .krx_etf import KRXETFCollector
from .gic import GICCollector

COLLECTORS=('nps','nbim','krx-etf','sec-13f','gic','temasek','dart')


def next_scheduled(name):
    from zoneinfo import ZoneInfo
    current=datetime.now(ZoneInfo('Asia/Seoul'))
    hours=((17,30),(19,30)) if name=='krx-etf' else ((8,30),(18,30))
    for offset in range(8):
        day=current+timedelta(days=offset)
        if name=='krx-etf' and day.weekday()>=5: continue
        for hour,minute in hours:
            candidate=day.replace(hour=hour,minute=minute,second=0,microsecond=0)
            if candidate>current: return candidate.isoformat()


def run_collectors(repo,only=None,transport_factory=Transport,config_path=None):
    config=json.loads((config_path or ROOT/'config/sources.json').read_text())
    results=[]
    for name in only or COLLECTORS:
        if name not in COLLECTORS: raise ValueError('Unknown collector')
        run_id=repo.execute('INSERT INTO data_fetch_runs(collector,started_at,status,next_scheduled) VALUES(?,?,?,?)',
          (name,now(),'running',next_scheduled(name)))
        try:
            warnings=[]
            transport=transport_factory()
            if name=='sec-13f': count=SEC13FCollector(repo,transport).run()
            elif name=='dart': count=DARTCollector(repo,transport).run()
            else:
                source=config.get(name,{})
                count=0
                configured=False
                if name=='nps':
                    configured=True
                    count+=NPSCollector(repo,transport).run()
                if name=='nbim':
                    configured=True
                    count+=NBIMCollector(repo,transport,source).run()
                if name=='krx-etf':
                    configured=True
                    collector=KRXETFCollector(repo,transport,source)
                    count+=collector.run()
                    warnings.extend(collector.warnings)
                if name=='gic':
                    configured=True
                    count+=GICCollector(repo,transport).run()
                if source.get('datasets'):
                    configured=True
                    count+=DatasetCollector(repo,transport,{'id':name,**source}).run()
                for feed in source.get('feeds',[]):
                    configured=True
                    count+=NewsFeedCollector(repo,transport,name,feed).run()
                if not configured: raise NeedsConfiguration('공식 다운로드 또는 RSS 피드 설정 필요')
            status='success' if count else 'no_new_data'
            message='새 데이터 '+str(count)+'건' if count else '신규 데이터 없음'
            if warnings:
                status='partial'
                message+=' · '+'; '.join(warnings)
        except NeedsConfiguration as e:
            status,message,count='needs_configuration',str(e),0
        except Exception as e:
            # Never persist raw exception text: upstream URLs may contain credentials.
            status,message,count='error','수집 실패 · '+type(e).__name__+' · 공식 소스/설정 검토 필요',0
        repo.execute('UPDATE data_fetch_runs SET finished_at=?,status=?,message=?,new_items=? WHERE id=?',
          (now(),status,message,count,run_id))
        results.append(dict(collector=name,status=status,message=message,new_items=count))
    return results
