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
from .temasek import TemasekCollector
from ..source_state import record_status

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


def run_collectors(repo,only=None,transport_factory=Transport,config_path=None,*,origin='cli',job_id=None,requested_at=None):
    if origin not in ('scheduler','web','cli','backfill'): raise ValueError('Invalid execution origin')
    if only is not None and (not only or any(n not in COLLECTORS for n in only)): raise ValueError('Unknown collector')
    config=json.loads((config_path or ROOT/'config/sources.json').read_text())
    results=[]
    for name in only or COLLECTORS:
        if name not in COLLECTORS: raise ValueError('Unknown collector')
        run_id=repo.execute('INSERT INTO data_fetch_runs(collector,started_at,status,next_scheduled,origin,job_id,requested_at) VALUES(?,?,?,?,?,?,?)',
          (name,now(),'running',next_scheduled(name),origin,job_id,requested_at or now()))
        try:
            warnings=[]
            collector=None
            transport=transport_factory()
            if name=='sec-13f':
                collector=SEC13FCollector(repo,transport)
                count=collector.run()
            elif name=='dart': count=DARTCollector(repo,transport).run()
            else:
                source=config.get(name,{})
                count=0
                configured=False
                if name=='nps':
                    configured=True
                    collector=NPSCollector(repo,transport,source)
                    count+=collector.run()
                if name=='nbim':
                    configured=True
                    collector=NBIMCollector(repo,transport,source)
                    count+=collector.run()
                if name=='krx-etf':
                    configured=True
                    collector=KRXETFCollector(repo,transport,source)
                    count+=collector.run()
                if name=='gic':
                    configured=True
                    count+=GICCollector(repo,transport).run()
                if name=='temasek':
                    configured=True
                    count+=TemasekCollector(repo,transport,source).run()
                if source.get('datasets') and name not in ('nps','temasek'):
                    configured=True
                    extra=DatasetCollector(repo,transport,{'id':name,**source}).run()
                    count+=extra
                for feed in source.get('feeds',[]) if name!='temasek' else []:
                    configured=True
                    count+=NewsFeedCollector(repo,transport,name,feed).run()
                if not configured: raise NeedsConfiguration('공식 다운로드 또는 RSS 피드 설정 필요')
            warnings.extend(getattr(collector,'warnings',[]))
            status='success' if count else 'no_new_data'
            message='새 데이터 '+str(count)+'건' if count else '신규 데이터 없음'
            if warnings:
                status='needs_review' if name=='nbim' and not count else 'partial'
                message+=' · '+'; '.join(warnings)
        except NeedsConfiguration as e:
            status,message,count='needs_configuration',str(e),0
        except Exception as e:
            # Never persist raw exception text: upstream URLs may contain credentials.
            status,message,count='error','수집 실패 · '+type(e).__name__+' · 공식 소스/설정 검토 필요',0
        details=getattr(collector,'details',{}) if collector else {}
        repo.execute('UPDATE data_fetch_runs SET finished_at=?,status=?,message=?,new_items=?,details_json=? WHERE id=?',
          (now(),status,message,count,json.dumps(details),run_id))
        institution={'krx-etf':'kr-etf','sec-13f':None,'dart':'korea-gurus'}.get(name,name)
        if institution:
            source=repo.rows('SELECT source_url FROM institutions WHERE id=?',(institution,))
            record_status(repo,'institution',institution,'collector',name,status,message,
                          source[0]['source_url'] if source else None,next_scheduled(name))
        results.append(dict(collector=name,status=status,message=message,new_items=count,**details))
    return results


def run_backfill(repo,name,*,guru=None,from_year=None,transport=None,config_path=None):
    if name not in ('nbim','sec-13f'):
        raise ValueError('History backfill supports NBIM and SEC 13F')
    if from_year is not None and not 1994<=from_year<=datetime.now().year:
        raise ValueError('Invalid history year')
    config=json.loads((config_path or ROOT/'config/sources.json').read_text())
    run_id=repo.execute('INSERT INTO data_fetch_runs(collector,started_at,status,origin,requested_at) VALUES(?,?,?,?,?)',
                       ('backfill:'+name,now(),'running','backfill',now()))
    try:
        client=transport or Transport()
        if name=='nbim':
            collector=NBIMCollector(repo,client,config.get(name,{}))
            count=collector.run(history=True,from_year=from_year)
        else:
            collector=SEC13FCollector(repo,client)
            count=collector.run(history=True,guru_id=guru,from_year=from_year)
        warnings=getattr(collector,'warnings',[])
        status=('partial' if count else 'needs_review') if warnings else 'success' if count else 'no_new_data'
        message='역사 backfill '+str(count)+'건'+(' · '+'; '.join(warnings) if warnings else '')
    except NeedsConfiguration as error:
        status,count,message='needs_configuration',0,str(error)
    except Exception as error:
        status,count,message='error',0,'역사 backfill 실패 · '+type(error).__name__
    repo.execute('UPDATE data_fetch_runs SET finished_at=?,status=?,message=?,new_items=? WHERE id=?',
                 (now(),status,message,count,run_id))
    return dict(collector=name,status=status,new_items=count,message=message)
