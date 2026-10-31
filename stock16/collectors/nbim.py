"""Official NBIM datasets. Source-local IDs remain reviewable; no fuzzy merging."""
import json
from decimal import Decimal
from ..model import import_snapshot,import_allocation,number,now
from ..storage import store_document
from .base import NeedsConfiguration
from ..source_state import queue_review,record_status

BASE='https://www.nbim.no/api/investments/v2/'


def normalize_holdings(records,total_usd):
    rows=[]
    for r in records:
        if r['at'] not in (0,1): continue
        asset='eq' if r['at']==0 else 'fi'
        usd=number(r['a']['e'])
        item=dict(name=r['n'],source_security_id=r['id']+':'+asset,
          position_key=asset,market_value=usd,shares=None,weight=number(Decimal(usd)/Decimal(total_usd)*100),
          currency='USD',country=r['cc'],sector=r[asset].get('s'),
          ownership_pct=number(r.get('o')),instrument='equity' if asset=='eq' else 'bond_issuer')
        # These are issuer-level aggregates, not individual bond issues.
        rows.append(item)
    if not rows: raise ValueError('Empty NBIM dataset')
    return rows


class NBIMCollector:
    def __init__(self,repo,transport,config): self.repo,self.transport,self.config=repo,transport,config

    def run(self,history=False,from_year=None):
        body,_,_=self.transport.fetch(BASE+'init.json',{'application/json'})
        init=json.loads(body)['data']
        count=0
        self.warnings=[]
        details=sorted(init['details'],key=lambda d:d['date'],reverse=True)
        if not history: details=details[:2]
        if from_year: details=[d for d in details if d['date'][:4]>=str(from_year)]
        for detail in reversed(details):
            asof=detail['date'][:10]
            pub=self.config.get('publication_dates',{}).get(asof)
            if not pub or not pub.get('published_at') or not pub.get('source_url'):
                url=BASE+detail['fileName']
                queue_review(self.repo,'nbim','nbim',asof,url,'공식 publication date 검증 필요')
                self.warnings.append('NBIM '+asof+' 공식 발표일 검증 필요')
                continue
            if not pub['source_url'].startswith('https://www.nbim.no/'):
                raise ValueError('Publication evidence must be an official NBIM source')
            if history and self.repo.rows('SELECT id FROM portfolio_snapshots WHERE fund_id=? AND as_of_date=? AND published_at=?',('nbim',asof,pub['published_at'])):
                continue
            publication=pub['published_at']
            url=BASE+detail['fileName']
            dataset,media,url=self.transport.fetch(url,{'application/json'})
            nav=[n for n in init['nav'] if n['d'][:10]==asof]
            total=sum(Decimal(str(n['v']['e'])) for n in nav)
            if total<=0: raise ValueError('NBIM NAV unavailable')
            rows=normalize_holdings(json.loads(dataset)['data'],total)
            meta=dict(fund_id='nbim',as_of_date=asof,published_at=publication,source_url=url,
                 source_type='nbim',currency='USD',disclosure_scope='full',data_quality='Official-derived')
            sha=store_document(self.repo,dataset,media,meta)
            count+=int(import_snapshot(self.repo,meta,rows,sha)[1])
            names={'eq':('Equities','equity'),'fi':('Fixed income','fixed_income'),
                   're':('Real estate','real_estate'),'ri':('Renewable energy infrastructure','infrastructure')}
            allocations=[dict(original_category=names[n['a']][0],standard_category=names[n['a']][1],
                              weight=number(Decimal(str(n['v']['e']))/total*100),market_value=number(n['v']['e'])) for n in nav]
            allocations.append(dict(original_category='Total NAV (official investments website)',standard_category='total_fund',dimension='total',weight='100',market_value=number(total)))
            # Only the relevant NAV slice is archived, avoiding repeated full init history.
            allocation_body=json.dumps(nav,sort_keys=True).encode()
            navmeta={**meta,'source_url':BASE+'init.json'}
            navsha=store_document(self.repo,allocation_body,'application/json',navmeta)
            count+=import_allocation(self.repo,navmeta,allocations,navsha)
            for asset in ('eq','fi'):
                csv_url=BASE+'report/?assetType='+asset+'&date='+asof+'&fileType=csv'
                raw,kind,final=self.transport.fetch(csv_url,{'text/csv'})
                store_document(self.repo,raw,kind,{**meta,'source_url':final})
            self.repo.execute('INSERT OR IGNORE INTO disclosures(institution_id,external_id,title,published_at,as_of_date,fetched_at,kind,importance,source_url) VALUES(?,?,?,?,?,?,?,?,?)',
               ('nbim','holdings-'+asof,'NBIM 공개 보유내역 · '+asof,publication,asof,now(),'holdings release',1,pub['source_url']))
            self.repo.execute('DELETE FROM history_review_queue WHERE collector=? AND fund_id=? AND as_of_date=?',('nbim','nbim',asof))
        from . import next_scheduled
        status=('partial' if count else 'needs_review') if self.warnings else 'success' if count else 'no_new_data'
        record_status(self.repo,'institution','nbim','holdings','nbim',status,
                      '; '.join(self.warnings) or '공식 공개 기준일 기준',
                      'https://www.nbim.no/en/investments/all-investments/',next_scheduled('nbim'))
        return count
