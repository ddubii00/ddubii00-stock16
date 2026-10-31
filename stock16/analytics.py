from datetime import date,timedelta
from decimal import Decimal
from .model import compare, difference

from .periods import PERIODS, select_pair, SUPPORTED_PERIODS


class Analytics:
    def __init__(self,repo): self.repo=repo

    def institutions(self):
        from .freshness import decorate_entities
        rows=self.repo.rows("SELECT i.*,(SELECT count(*) FROM portfolio_snapshots p WHERE p.institution_id=i.id) snapshot_count FROM institutions i ORDER BY i.id")
        return decorate_entities(self.repo,rows,'institution')

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

        for row in rows:
            row.update(quantity=row['shares'],quantity_type=snap['quantity_type'])
        return dict(snapshot=snap,rows=rows)

    def changes(self,fund,period='previous',asof=None,from_date=None):
        with self.repo.connect() as connection:
            before,after=select_pair(connection,fund,period,asof,from_date)
        if not after:
            return dict(snapshot=None,previous=None,rows=[],period=period)
        with self.repo.connect() as c:
            rows=compare(c,before,after) if before and before['id']!=after['id'] else []
            for row in rows:
                row.update(dict(c.execute('SELECT name,ticker,sector,country FROM securities WHERE id=?',(row['security_id'],)).fetchone()))
                row.update(source_url=after['source_url'],as_of_date=after['as_of_date'],published_at=after['published_at'],before_as_of_date=before['as_of_date'],after_as_of_date=after['as_of_date'])
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

    # Internal adjacent-disclosure default; HTTP/UI explicitly default to 1m.
    def consensus(self,period='previous',minimum=2,mode='increased',institutions=None):
        if period not in SUPPORTED_PERIODS:
            raise ValueError('Unsupported comparison period')
        groups={}
        with self.repo.connect() as c:
            funds=c.execute('SELECT * FROM funds').fetchall()
            for fund in funds:
                if institutions and fund['institution_id'] not in institutions:
                    continue
                before,after=select_pair(c,fund['id'],period)
                if not after:
                    continue
                differences=compare(c,before,after) if before else []
                if mode=='held':
                    from .model import holding_map
                    changed={(r['security_id'],r['position_key']):r for r in differences}
                    observations=[]
                    for key,value in holding_map(c,after['set_hash']).items():
                        observations.append({**changed.get(key,{}),
                            'security_id':key[0],'position_key':key[1],
                            'shares':value['shares'],'weight':value['weight'],
                            'status':changed.get(key,{}).get('status','UNCHANGED' if before else 'BASELINE'),
                            'shares_delta':changed.get(key,{}).get('shares_delta'),
                            'shares_delta_pct':changed.get(key,{}).get('shares_delta_pct'),
                            'portfolio_weight_delta_pp':changed.get(key,{}).get('portfolio_weight_delta_pp')})
                else:
                    observations=differences
                for row in observations:
                    if mode=='increased' and not (row['shares_delta'] is not None and Decimal(row['shares_delta'])>0):
                        continue
                    if mode=='weight' and not (row['portfolio_weight_delta_pp'] is not None and Decimal(row['portfolio_weight_delta_pp'])>0):
                        continue
                    if mode=='new' and row['status']!='NEW':
                        continue
                    row.update(dict(c.execute('SELECT name,sector,country FROM securities WHERE id=?',(row['security_id'],)).fetchone()))
                    row.update(fund_id=fund['id'],institution_id=fund['institution_id'],kind=fund['kind'],
                        before_as_of_date=before['as_of_date'] if before else None,
                        after_as_of_date=after['as_of_date'],as_of_date=after['as_of_date'],
                        published_at=after['published_at'],fetched_at=after['fetched_at'],source_url=after['source_url'],
                        quantity_type=after['quantity_type'],quantity_delta=row.get('shares_delta'),
                        quantity_delta_pct=row.get('shares_delta_pct'))
                    groups.setdefault(row['security_id'],[]).append(row)
        return [dict(security_id=sid,name=rs[0]['name'],sector=rs[0]['sector'],country=rs[0]['country'],
                     institution_count=len({r['institution_id'] for r in rs if r['kind']!='ETF'}),
                     etf_count=len({r['fund_id'] for r in rs if r['kind']=='ETF'}),
                     fund_count=len({r['fund_id'] for r in rs}),period=period,observations=rs)
                for sid,rs in sorted(groups.items()) if len({r['fund_id'] for r in rs})>=minimum]

    def security_holdings(self,sid):
        return self.repo.rows('''SELECT f.name fund_name,f.kind,p.*,v.shares,v.weight,v.market_value,v.currency holding_currency,
          (SELECT min(q.as_of_date) FROM portfolio_snapshots q JOIN holdings hh ON hh.set_hash=q.set_hash JOIN holding_values vv ON vv.hash=hh.value_hash WHERE vv.security_id=v.security_id AND q.fund_id=p.fund_id) first_confirmed,
          ch.shares_delta,ch.shares_delta_pct,ch.portfolio_weight_delta_pp,ch.status,v.position_key,v.shares quantity
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
          p.as_of_date,p.published_at,p.fetched_at,p.source_url,p.quantity_type,
          (SELECT as_of_date FROM portfolio_snapshots WHERE id=ch.previous_snapshot_id) before_as_of_date
          FROM holding_changes ch
          JOIN effective_snapshots p ON p.id=ch.snapshot_id JOIN securities s ON s.id=ch.security_id
          JOIN funds f ON f.id=p.fund_id
          WHERE p.id=(SELECT e.id FROM effective_snapshots e WHERE e.fund_id=p.fund_id ORDER BY e.as_of_date DESC LIMIT 1)
          ORDER BY coalesce(p.published_at,p.fetched_at) DESC''')
        today_information=self.today_information(today)
        counts={'today_disclosures':today_information['counts']['published_disclosures']}
        for key,status,etf in [('etf_new','NEW',True),('institution_new','NEW',False),('institution_increased','INCREASED',False),('institution_decreased','DECREASED',False),('institution_exited','EXITED',False)]:
            counts[key]=sum(r['status']==status and (r['kind']=='ETF')==etf for r in rows)
        counts['etf_weight_increased']=sum(r['kind']=='ETF' and r['portfolio_weight_delta_pp'] is not None and Decimal(r['portfolio_weight_delta_pp'])>0 for r in rows)
        counts['consensus']=len(self.consensus())
        for row in rows:
            row.update(quantity_delta=row['shares_delta'],quantity_delta_pct=row['shares_delta_pct'])
        important=sorted(rows,key=lambda r:(r['published_at'] or r['fetched_at'],r['status'] in ('NEW','EXITED'),abs(Decimal(r['portfolio_weight_delta_pp'] or '0'))),reverse=True)[:20]
        return dict(counts=counts,changes=important,latest_changes=important,today=today_information,reference_date=today,
                    comparison_window='기관·ETF별 최근 두 공개시점 비교; 각 행 기준일 확인',
                    institutions=self.institutions())

    def today_information(self,day=None):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        zone=ZoneInfo('Asia/Seoul')
        selected=date.fromisoformat(day) if day else datetime.now(zone).date()
        threshold=(selected-timedelta(days=1)).isoformat()
        def same_day(value):
            if not value: return False
            try:
                if len(value)==10: return date.fromisoformat(value)==selected
                instant=datetime.fromisoformat(value.replace('Z','+00:00'))
                if instant.tzinfo is None: instant=instant.replace(tzinfo=zone)
                return instant.astimezone(zone).date()==selected
            except ValueError:
                return False
        disclosures=self.repo.rows("SELECT d.*,i.name institution_name FROM disclosures d JOIN institutions i ON i.id=d.institution_id WHERE d.published_at>=? OR d.fetched_at>=?",(threshold,threshold))
        disclosures=[{**r,'published_today':same_day(r['published_at']),'fetched_today':same_day(r['fetched_at'])} for r in disclosures if same_day(r['published_at']) or same_day(r['fetched_at'])]
        snapshots=self.repo.rows("SELECT p.*,f.name fund_name,f.kind FROM portfolio_snapshots p JOIN funds f ON f.id=p.fund_id WHERE p.published_at>=? OR p.fetched_at>=?",(threshold,threshold))
        snapshots=[{**r,'published_today':same_day(r['published_at']),'fetched_today':same_day(r['fetched_at'])} for r in snapshots if same_day(r['published_at']) or same_day(r['fetched_at'])]
        allocations=self.repo.rows("SELECT institution_id,as_of_date,published_at,min(fetched_at) fetched_at,source_url,checksum FROM asset_allocation_snapshots WHERE published_at>=? OR fetched_at>=? GROUP BY institution_id,as_of_date,published_at,source_url,checksum",(threshold,threshold))
        allocations=[r for r in allocations if same_day(r['published_at']) or same_day(r['fetched_at'])]
        counts=dict(published_disclosures=sum(r['published_today'] for r in disclosures),
          fetched_disclosures=sum(r['fetched_today'] for r in disclosures),
          etf_snapshots=sum(r['kind']=='ETF' and r['fetched_today'] for r in snapshots),
          sec_filings=sum(r['kind'].startswith('13F') and r['fetched_today'] for r in disclosures),
          institution_updates=len({r['institution_id'] for r in snapshots if r['kind']!='ETF'}|{r['institution_id'] for r in allocations}))
        return dict(reference_date=selected.isoformat(),timezone='Asia/Seoul',counts=counts,
          disclosures=disclosures,snapshots=snapshots,allocations=allocations,
          explanation='오늘은 KST 발표일·최초 수집일 기준이며 포트폴리오 기준일과 다릅니다.')
