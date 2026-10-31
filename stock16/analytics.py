from datetime import date,timedelta
from decimal import Decimal
from .model import compare, difference

PERIODS={'1d':1,'1w':7,'1m':31,'3m':93,'quarter':93,'half':183,'1y':366}


class Analytics:
    def __init__(self,repo): self.repo=repo

    def institutions(self):
        return self.repo.rows('''SELECT i.*,
          (SELECT max(as_of_date) FROM effective_snapshots p WHERE p.institution_id=i.id) latest_as_of,
          (SELECT max(published_at) FROM effective_snapshots p WHERE p.institution_id=i.id) latest_published,
          (SELECT max(as_of_date) FROM asset_allocation_snapshots p WHERE p.institution_id=i.id) latest_allocation,
          (SELECT count(*) FROM portfolio_snapshots p WHERE p.institution_id=i.id) snapshot_count
          FROM institutions i ORDER BY i.id''')

    def snapshot(self,fund,asof=None):
        rows=self.repo.rows('SELECT * FROM effective_snapshots WHERE fund_id=? AND as_of_date<=? ORDER BY as_of_date DESC LIMIT 1',
                            (fund,asof or '9999-12-31'))
        return rows[0] if rows else None

    def holdings(self,fund,asof=None):
        snap=self.snapshot(fund,asof)
        if not snap: return dict(snapshot=None,rows=[])
        rows=self.repo.rows('''SELECT s.*,v.position_key,v.shares,v.market_value,v.weight,v.ownership_pct,v.currency
         FROM holdings h JOIN holding_values v ON v.hash=h.value_hash JOIN securities s ON s.id=v.security_id
         WHERE h.set_hash=?''',(snap['set_hash'],))
        rows.sort(key=lambda r:Decimal(r['weight'] or '-1'),reverse=True)
        return dict(snapshot=snap,rows=rows)

    def changes(self,fund,period='previous',asof=None,from_date=None):
        after=self.snapshot(fund,asof)
        if not after: return dict(snapshot=None,previous=None,rows=[])
        before=None
        if from_date:
            before=self.snapshot(fund,from_date)
        elif period in ('5b','20b'):
            # Observed business snapshots; explicit dates are always returned.
            r=self.repo.rows('SELECT * FROM effective_snapshots WHERE fund_id=? AND as_of_date<? ORDER BY as_of_date DESC LIMIT 1 OFFSET ?',
                            (fund,after['as_of_date'],int(period[:-1])-1))
            before=r[0] if r else None
        elif period in PERIODS:
            day=date.fromisoformat(after['as_of_date'])
            if period in ('1m','3m','quarter','half','1y'):
                import calendar
                from .storage import months_before
                target=months_before(day,{'1m':1,'3m':3,'quarter':3,'half':6,'1y':12}[period])
                if day.day==calendar.monthrange(day.year,day.month)[1]:
                    target=target.replace(day=calendar.monthrange(target.year,target.month)[1])
                cutoff=target.isoformat()
            else: cutoff=(day-timedelta(days=PERIODS[period])).isoformat()
            before=self.snapshot(fund,cutoff)
        else:
            r=self.repo.rows('SELECT * FROM effective_snapshots WHERE fund_id=? AND as_of_date<? ORDER BY as_of_date DESC LIMIT 1',
                            (fund,after['as_of_date']))
            before=r[0] if r else None
        with self.repo.connect() as c:
            rows=compare(c,before,after) if before and before['id']!=after['id'] else []
            for row in rows:
                row.update(dict(c.execute('SELECT name,ticker,sector,country FROM securities WHERE id=?',(row['security_id'],)).fetchone()))
                row.update(source_url=after['source_url'],as_of_date=after['as_of_date'],published_at=after['published_at'])
        return dict(snapshot=after,previous=before,rows=rows,period=period,
                    limitation='5b/20b는 저장된 영업일 관측 기준. 수량 변화는 거래량과 다를 수 있습니다.')

    def allocation(self,institution):
        rows=self.repo.rows('''SELECT * FROM (SELECT a.*,row_number() OVER(PARTITION BY institution_id,as_of_date,dimension,original_category ORDER BY published_at DESC,id DESC) rn
          FROM asset_allocation_snapshots a WHERE institution_id=?) WHERE rn=1 ORDER BY as_of_date,dimension,original_category''',(institution,))
        previous={}
        for row in rows:
            key=(row['dimension'],row['original_category'])
            row['weight_delta_pp']=difference(row['weight'],previous.get(key))
            row['target_gap_pp']=difference(row['weight'],row['target_weight'])
            previous[key]=row['weight']
        return rows

    def consensus(self,period='1m',minimum=2,mode='increased',institutions=None):
        cutoff=(date.today()-timedelta(days=PERIODS.get(period,31))).isoformat()
        where='p.as_of_date>=?' if mode!='held' else '1=1'
        params=(cutoff,) if mode!='held' else ()
        if mode=='held':
            sql='''SELECT v.security_id,p.fund_id,p.institution_id,p.as_of_date,p.published_at,p.source_url,
              s.name,s.sector,s.country,f.kind,v.shares,v.weight,NULL shares_delta,NULL portfolio_weight_delta_pp
              FROM effective_snapshots p JOIN holdings h ON h.set_hash=p.set_hash
              JOIN holding_values v ON v.hash=h.value_hash JOIN securities s ON s.id=v.security_id
              JOIN funds f ON f.id=p.fund_id'''
        else:
            sql='''SELECT ch.*,p.fund_id,p.institution_id,p.as_of_date,p.published_at,p.source_url,s.name,s.sector,s.country,f.kind
              FROM holding_changes ch JOIN effective_snapshots p ON p.id=ch.snapshot_id
              JOIN securities s ON s.id=ch.security_id JOIN funds f ON f.id=p.fund_id'''
        sql+=' WHERE '+where+''' AND p.id=(SELECT e.id FROM effective_snapshots e WHERE e.fund_id=p.fund_id ORDER BY e.as_of_date DESC LIMIT 1)'''
        groups={}
        for r in self.repo.rows(sql,params):
            if institutions and r['institution_id'] not in institutions: continue
            if mode=='increased' and not (r['shares_delta'] is not None and Decimal(r['shares_delta'])>0): continue
            if mode=='weight' and not (r['portfolio_weight_delta_pp'] is not None and Decimal(r['portfolio_weight_delta_pp'])>0): continue
            if mode=='new' and r.get('status')!='NEW': continue
            groups.setdefault(r['security_id'],[]).append(r)
        return [dict(security_id=sid,name=rs[0]['name'],sector=rs[0]['sector'],country=rs[0]['country'],
                     institution_count=len({r['institution_id'] for r in rs if r['kind']!='ETF'}),
                     etf_count=len({r['fund_id'] for r in rs if r['kind']=='ETF'}),fund_count=len({r['fund_id'] for r in rs}),observations=rs)
                for sid,rs in groups.items() if len({r['fund_id'] for r in rs})>=minimum]

    def security_holdings(self,sid):
        return self.repo.rows('''SELECT f.name fund_name,f.kind,p.*,v.shares,v.weight,v.market_value,v.currency holding_currency,
          (SELECT min(q.as_of_date) FROM portfolio_snapshots q JOIN holdings hh ON hh.set_hash=q.set_hash JOIN holding_values vv ON vv.hash=hh.value_hash WHERE vv.security_id=v.security_id AND q.fund_id=p.fund_id) first_confirmed,
          ch.shares_delta,ch.portfolio_weight_delta_pp,ch.status
          FROM effective_snapshots p JOIN holdings h ON h.set_hash=p.set_hash
          JOIN holding_values v ON v.hash=h.value_hash JOIN funds f ON f.id=p.fund_id
          LEFT JOIN holding_changes ch ON ch.snapshot_id=p.id AND ch.security_id=v.security_id AND ch.position_key=v.position_key
          WHERE v.security_id=? AND p.id=(SELECT e.id FROM effective_snapshots e WHERE e.fund_id=p.fund_id ORDER BY e.as_of_date DESC LIMIT 1)''',(sid,))

    def disclosures(self,since='0000-01-01',institution=None):
        return self.repo.rows('SELECT d.*,i.name institution_name FROM disclosures d JOIN institutions i ON i.id=d.institution_id WHERE published_at>=? AND (? IS NULL OR institution_id=?) ORDER BY published_at DESC LIMIT 500',
                              (since,institution,institution))

    def dashboard(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today=datetime.now(ZoneInfo('Asia/Seoul')).date().isoformat()
        rows=self.repo.rows('''SELECT ch.*,s.name,s.ticker,p.fund_id,f.name fund_name,f.kind,
          p.as_of_date,p.published_at,p.fetched_at,p.source_url FROM holding_changes ch
          JOIN effective_snapshots p ON p.id=ch.snapshot_id JOIN securities s ON s.id=ch.security_id
          JOIN funds f ON f.id=p.fund_id
          WHERE p.id=(SELECT e.id FROM effective_snapshots e WHERE e.fund_id=p.fund_id ORDER BY e.as_of_date DESC LIMIT 1)
          ORDER BY coalesce(p.published_at,p.fetched_at) DESC''')
        counts={'today_disclosures':len(self.disclosures(today))}
        for key,status,etf in [('etf_new','NEW',True),('institution_new','NEW',False),('institution_increased','INCREASED',False),('institution_decreased','DECREASED',False),('institution_exited','EXITED',False)]:
            counts[key]=sum(r['status']==status and (r['kind']=='ETF')==etf for r in rows)
        counts['etf_weight_increased']=sum(r['kind']=='ETF' and r['portfolio_weight_delta_pp'] is not None and Decimal(r['portfolio_weight_delta_pp'])>0 for r in rows)
        counts['consensus']=len(self.consensus())
        important=sorted(rows,key=lambda r:(r['published_at'] or r['fetched_at'],r['status'] in ('NEW','EXITED'),abs(Decimal(r['portfolio_weight_delta_pp'] or '0'))),reverse=True)[:20]
        return dict(counts=counts,changes=important,reference_date=today,
                    comparison_window='기관·ETF별 최근 두 공개시점 비교; 각 행 기준일 확인',
                    institutions=self.institutions())
