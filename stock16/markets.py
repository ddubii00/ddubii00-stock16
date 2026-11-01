"""Point-in-time whole-market ranks. Top-100 is a view, never the universe."""
from datetime import date,datetime,timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo
from .model import now,number,resolve_security
from .storage import store_document

MARKETS=('KOSPI','KOSDAQ','NASDAQ')
FIRST_WEEK='2020-01-03'
EXCLUDED={'ETF','ETN','FUND','MUTUAL_FUND','CLOSED_END_FUND','INDEX','WARRANT','RIGHT','BOND'}
STOCK_TYPES={'STOCK','COMMON_STOCK','PREFERRED_STOCK','DR','REIT','SPAC'}


def week_end(day):
    day=date.fromisoformat(day) if isinstance(day,str) else day
    return day+timedelta(days=4-day.weekday())


def closed_week(market='KOSPI',instant=None):
    current=instant or datetime.now(ZoneInfo('Asia/Seoul'))
    zone=ZoneInfo('America/New_York' if market=='NASDAQ' else 'Asia/Seoul')
    local=current.astimezone(zone)
    end=week_end(local.date())
    if local.date()<end or (local.date()==end and local.hour<(16 if market=='NASDAQ' else 16)):
        end-=timedelta(days=7)
    return end


def weeks_between(start,end):
    start,end=date.fromisoformat(start),date.fromisoformat(end)
    if start.weekday()!=4 or end.weekday()!=4 or start>end: raise ValueError('Friday week labels required')
    if start<date(2020,1,3): raise ValueError('History begins 2020-W01')
    return [(start+timedelta(days=7*i)).isoformat() for i in range((end-start).days//7+1)]


def import_market(repo,meta,rows,body,media='application/json'):
    market=meta['market']
    if market not in MARKETS: raise ValueError('Unknown market')
    day=date.fromisoformat(meta['as_of_date'])
    if day.weekday()>4 or day>datetime.now(ZoneInfo('America/New_York' if market=='NASDAQ' else 'Asia/Seoul')).date():
        raise ValueError('Invalid market basis date')
    if meta.get('universe_complete') is not True or len(rows)!=meta.get('expected_count') or not rows:
        raise ValueError('Complete point-in-time universe with exact source count required')
    if not meta.get('universe_definition') or not meta.get('source_type') or not meta['source_url'].startswith('https://'):
        raise ValueError('Explicit universe and provenance required')
    expected_currency='USD' if market=='NASDAQ' else 'KRW'
    if meta['currency']!=expected_currency: raise ValueError('Mixed market currencies')
    accepted=[];excluded=0
    for row in rows:
        kind=row.get('instrument')
        if kind in EXCLUDED:
            excluded+=1;continue
        if kind not in STOCK_TYPES: raise ValueError('Unverified instrument type')
        if row.get('exchange')!=market: raise ValueError('Point-in-time exchange mismatch')
        cap=number(row.get('market_cap'))
        if cap is None or Decimal(cap)<0: raise ValueError('Invalid market capitalization')
        if not row.get('name') or not row.get('ticker'): raise ValueError('Missing listing labels')
        if not any(row.get(k) for k in ('kr_code','isin','cusip','source_security_id')):
            raise ValueError('Permanent security identifier required; ticker alone is insufficient')
        accepted.append((row,cap))
    if not accepted: raise ValueError('No eligible stocks')
    provenance=dict(fund_id='market-'+market.lower(),as_of_date=day.isoformat(),published_at=meta.get('published_at'),source_url=meta['source_url'],source_type=meta['source_type'])
    sha=store_document(repo,body,media,provenance)
    with repo.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        existing=c.execute('SELECT id FROM market_snapshots WHERE market=? AND as_of_date=? AND checksum=?',(market,day.isoformat(),sha)).fetchone()
        if existing:return existing[0],False
        observation=c.execute('SELECT id FROM source_observations WHERE checksum=? AND fund_id=? AND as_of_date=? AND source_url=? ORDER BY id DESC LIMIT 1',(sha,provenance['fund_id'],day.isoformat(),meta['source_url'])).fetchone()[0]
        prepared=[];seen=set()
        for row,cap in accepted:
            sid=resolve_security(c,row,meta['source_type'])
            if sid in seen: raise ValueError('Duplicate market security; share classes need distinct identifiers')
            seen.add(sid)
            c.execute('INSERT OR IGNORE INTO market_listings(security_id,market,ticker,name,instrument) VALUES(?,?,?,?,?)',(sid,market,row['ticker'],row['name'],row['instrument']))
            listing=c.execute('SELECT id FROM market_listings WHERE security_id=? AND market=? AND ticker=? AND name=? AND instrument=?',(sid,market,row['ticker'],row['name'],row['instrument'])).fetchone()[0]
            identity=c.execute('SELECT identity_key FROM securities WHERE id=?',(sid,)).fetchone()[0]
            prepared.append((sid,listing,cap,identity))
        # Exact Decimal descending; equal caps have deterministic permanent-ID order.
        prepared.sort(key=lambda x:(-Decimal(x[2]),x[3]))
        previous=c.execute('SELECT * FROM market_snapshots WHERE market=? AND as_of_date=? ORDER BY id DESC LIMIT 1',(market,day.isoformat())).fetchone()
        if previous and previous['source_type']==meta['source_type'] and previous['universe_definition']==meta['universe_definition']:
            saved=[tuple(r) for r in c.execute('SELECT security_id,listing_id,market_cap FROM market_ranks WHERE snapshot_id=? ORDER BY rank',(previous['id'],))]
            if saved==[(r[0],r[1],r[2]) for r in prepared]:return previous['id'],False
        identifier=c.execute('''INSERT INTO market_snapshots(market,week_end,as_of_date,currency,universe_count,excluded_count,universe_definition,source_type,observation_id,checksum,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
          (market,week_end(day).isoformat(),day.isoformat(),expected_currency,len(prepared),excluded,meta['universe_definition'],meta['source_type'],observation,sha,now())).lastrowid
        c.executemany('INSERT INTO market_ranks VALUES(?,?,?,?,?)',[(identifier,sid,listing,rank,cap) for rank,(sid,listing,cap,_) in enumerate(prepared,1)])
    return identifier,True


class MarketAnalytics:
    def __init__(self,repo): self.repo=repo

    def snapshot(self,market,week='latest'):
        if market not in MARKETS:raise ValueError('Unknown market')
        clause='' if week=='latest' else ' AND week_end=?'
        args=(market,) if week=='latest' else (market,week)
        rows=self.repo.rows('''SELECT p.*,o.source_url,o.published_at,o.fetched_at FROM market_snapshots p
          JOIN source_observations o ON o.id=p.observation_id WHERE p.market=?'''+clause+' ORDER BY p.as_of_date DESC,p.id DESC LIMIT 1',args)
        if rows:
            rows[0]['data_quality']='Secondary' if rows[0]['source_type']=='marcap-archive' else 'Official-derived' if rows[0]['source_type']=='krx-market-ranks' else 'Reviewed import'
        return rows[0] if rows else None

    def entries(self,snapshot,limit=None):
        if not snapshot:return []
        query='''SELECT r.*,l.ticker,l.name,l.instrument FROM market_ranks r JOIN market_listings l ON l.id=r.listing_id WHERE r.snapshot_id=? ORDER BY r.rank'''
        return self.repo.rows(query+(' LIMIT ?' if limit else ''),(snapshot['id'],limit) if limit else (snapshot['id'],))

    def current_positions(self):
        results={}
        for market in MARKETS:
            snapshot=self.snapshot(market)
            if not snapshot:continue
            for row in self.entries(snapshot):
                results.setdefault(row['security_id'],[]).append(dict(current_market=market,current_rank=row['rank'],current_as_of_date=snapshot['as_of_date'],current_week_end=snapshot['week_end'],current_name=row['name'],current_data_complete=snapshot['week_end']==closed_week(market).isoformat()))
        for sid,positions in results.items():
            newest=max(p['current_week_end'] for p in positions)
            results[sid]=[p for p in positions if p['current_week_end']==newest]
        return results

    def ranking(self,market,week='latest'):
        snap=self.snapshot(market,week)
        if not snap:return dict(snapshot=None,previous=None,rows=[],entered=[],exited=[],status='not_collected',week=week)
        previous_week=(date.fromisoformat(snap['week_end'])-timedelta(days=7)).isoformat()
        previous=self.snapshot(market,previous_week)
        old={r['security_id']:r for r in self.entries(previous)}
        all_rows=self.entries(snap);current={r['security_id']:r for r in all_rows}
        latest_positions=self.current_positions()
        rows=[]
        for row in all_rows[:100]:
            before=old.get(row['security_id'])
            status=('ENTERED_TOP100' if before and before['rank']>100 else 'NEW_TO_UNIVERSE' if not before else 'STAYED') if previous else 'BASELINE'
            candidates=latest_positions.get(row['security_id'],[])
            latest=max(candidates,key=lambda r:(r['current_week_end'],r['current_as_of_date'],r['current_market']==market)) if candidates else {}
            rows.append({**row,**latest,'previous_rank':before['rank'] if before else None,'rank_change':before['rank']-row['rank'] if before else None,'status':status})
        exited=[]
        if previous:
            for sid,row in old.items():
                if row['rank']<=100 and (sid not in current or current[sid]['rank']>100):
                    after=current.get(sid)
                    exited.append(dict(name=row['name'],ticker=row['ticker'],security_id=sid,previous_rank=row['rank'],rank=after['rank'] if after else None,status='LEFT_TOP100' if after else 'NOT_IN_MARKET_UNIVERSE'))
        return dict(snapshot=snap,previous=previous,expected_previous_week=previous_week,comparison_available=bool(previous),rows=rows,
          entered=[r for r in rows if r['status'] in ('ENTERED_TOP100','NEW_TO_UNIVERSE')],exited=exited,status='available',week=snap['week_end'])

    def trace(self,market,week,rank):
        snap=self.snapshot(market,week)
        if not snap:return dict(status='baseline_not_collected',baseline=None,current=[],history=[])
        baseline=next((r for r in self.entries(snap) if r['rank']==rank),None)
        if baseline is None:return dict(status='rank_not_available',baseline=None,current=[],history=[])
        sid=baseline['security_id']
        history=self.repo.rows('''SELECT p.market,p.week_end,p.as_of_date,r.rank,r.market_cap,l.name,l.ticker,p.currency,o.source_url FROM market_snapshots p
          JOIN market_ranks r ON r.snapshot_id=p.id JOIN market_listings l ON l.id=r.listing_id
          JOIN source_observations o ON o.id=p.observation_id WHERE r.security_id=? AND p.id=(
          SELECT s.id FROM market_snapshots s WHERE s.market=p.market AND s.week_end=p.week_end ORDER BY s.as_of_date DESC,s.id DESC LIMIT 1)
          ORDER BY p.week_end,p.market''',(sid,))
        current=self.current_positions().get(sid,[])
        current.sort(key=lambda r:(r['current_week_end'],r['current_as_of_date']),reverse=True)
        return dict(status=('available' if any(p['current_data_complete'] for p in current) else 'latest_data_missing') if current else 'not_in_latest_universe',baseline={**baseline,'market':market,'week_end':week,'as_of_date':snap['as_of_date'],'source_url':snap['source_url']},current=current,history=history,
          limitation='최신 확보 자료 기준. 목록 부재만으로 상장폐지·합병을 확정하지 않습니다. 동일 종목의 시장 이전은 명시적 식별자로만 연결합니다.')

    def status(self):
        rows=[]
        for market in MARKETS:
            latest=self.snapshot(market)
            weeks=[r['week_end'] for r in self.repo.rows('SELECT DISTINCT week_end FROM market_snapshots WHERE market=? ORDER BY week_end',(market,))]
            target=closed_week(market).isoformat();expected=weeks_between(FIRST_WEEK,target)
            source=self.repo.rows("SELECT * FROM source_status WHERE entity_type='fund' AND entity_id=?",('market-'+market.lower(),))
            rows.append(dict(market=market,latest=latest,weeks=weeks,collected_weeks=len(weeks),expected_weeks=len(expected),missing_weeks=[w for w in expected if w not in weeks],source_status=source,first_week=FIRST_WEEK,target_week=target))
        return rows
