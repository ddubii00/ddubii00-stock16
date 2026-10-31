"""Synthetic operations/source contracts. No live API calls."""
from contextlib import contextmanager
from datetime import date
import json
from pathlib import Path
from collections import namedtuple
import pytest
from stock16.analytics import Analytics
from stock16.collectors.base import DatasetCollector,NeedsConfiguration
from stock16.collectors.discovery import ReviewedDiscoveryCollector
from stock16.collectors.nps import NPSCollector
from stock16.collectors.temasek import TemasekCollector
from stock16.collectors.etf_adapters import SolAdapter
from stock16.freshness import decorate_entities
from stock16.source_state import record_status
from stock16.storage import backup,storage_status,maintain_database


def test_default_backup_generations_3_2_3_and_hardlinks(repo,monkeypatch):
    for period in ('DAILY','WEEKLY','MONTHLY'):
        monkeypatch.delenv('STOCK16_BACKUP_'+period,raising=False)
    for month in range(1,9):
        for day in (1,15): backup(repo,date(2026,month,day))
    folder=repo.root/'backups'
    assert [len(list(folder.glob(p+'-*'))) for p in ('daily','weekly','monthly')]==[3,2,3]
    latest=folder/'daily-2026-08-15.sqlite3.gz'
    assert latest.stat().st_ino==(folder/'monthly-2026-08.sqlite3.gz').stat().st_ino


def test_data_and_whole_filesystem_sizes_are_separate(repo,ingest,stock,monkeypatch):
    ingest('2026-10-30',[stock]);logs=repo.root/'logs';logs.mkdir();(logs/'app.log').write_bytes(b'x'*100)
    usage=namedtuple('Usage','total used free')(10**9,8*10**8,2*10**8)
    monkeypatch.setattr('stock16.storage.shutil.disk_usage',lambda path:usage)
    with repo.connect() as c:
        c.execute("INSERT INTO collector_state VALUES('TEST','wal')");c.commit()
        result=storage_status(repo)
        assert result['wal_bytes']>0
    assert result['db_bytes']==repo.path.stat().st_size
    assert result['disk_used_bytes']==8*10**8 and result['total_bytes']<result['disk_used_bytes']
    assert result['logs_bytes']==100 and result['warning_level']==80
    assert result['human_readable']['db_bytes'] and result['counts']['etf_snapshots']==1
    assert result['counts']['holding_sets']==1 and result['counts']['source_documents']==1


def test_light_maintenance_has_no_vacuum(repo,monkeypatch):
    queries=[];original=repo.connect
    @contextmanager
    def traced():
        with original() as c:
            c.set_trace_callback(queries.append);yield c
    monkeypatch.setattr(repo,'connect',traced)
    assert maintain_database(repo)['vacuum'] is False
    assert not any('VACUUM' in query for query in queries)
    assert 'PRAGMA optimize' in queries and 'PRAGMA wal_checkpoint(TRUNCATE)' in queries


def test_manual_vacuum_checks_free_space_before_work(repo,monkeypatch):
    usage=namedtuple('Usage','total used free')(100,100,0)
    monkeypatch.setattr('stock16.storage.shutil.disk_usage',lambda path:usage)
    with pytest.raises(RuntimeError,match='free space'):maintain_database(repo,vacuum=True)
    assert not (repo.root/'backups').exists()


def test_per_fund_freshness_does_not_inherit_other_etf_failure(repo,ingest,stock):
    today=date.today().isoformat()
    ingest(today,[stock])
    record_status(repo,'fund','etf-069500','holdings','krx-etf','success','TEST verified')
    repo.execute("INSERT INTO data_fetch_runs(collector,started_at,status) VALUES('krx-etf','2026-10-31','partial')")
    record_status(repo,'fund','etf-102110','holdings','krx-etf','needs_configuration','TEST mapping')
    rows=decorate_entities(repo,repo.rows("SELECT * FROM funds WHERE id IN ('etf-069500','etf-102110')"),'fund')
    first=next(r for r in rows if r['id']=='etf-069500')
    assert first['status']=='success' and first['latest_as_of_date']==today
    assert all(key in first for key in ('latest_published_at','latest_fetched_at','last_collector_success','last_collector_check','next_scheduled','data_source'))


def test_sec_institution_shows_its_own_collector_failure(repo):
    record_status(repo,'fund','berkshire','holdings','sec-13f','error','TEST failed')
    repo.execute("INSERT INTO data_fetch_runs(collector,started_at,status) VALUES('sec-13f','2026-10-31','success')")
    row=next(r for r in Analytics(repo).institutions() if r['id']=='berkshire')
    assert row['status']=='error'
    assert '분기말' in row['freshness_note']


def test_old_basis_is_delayed_without_relabelling_fetched_date(repo,ingest,stock):
    ingest('2001-01-01',[stock],fund='nps')
    record_status(repo,'institution','nps','holdings','nps','no_new_data','TEST checked')
    row=next(r for r in Analytics(repo).institutions() if r['id']=='nps')
    assert row['status']=='delayed' and row['latest_as_of_date']=='2001-01-01'


def test_today_information_uses_kst_published_and_fetched_not_basis(repo,ingest,stock):
    ingest('2024-01-01',[stock])
    repo.execute("UPDATE portfolio_snapshots SET fetched_at='2026-10-30T15:00:00+00:00'")
    for external,pub,fetched in [('before','2026-10-30T14:59:59+00:00','2026-10-29'),
                                 ('after','2026-10-30T15:00:00+00:00','2026-10-29'),
                                 ('history','2020-01-01','2026-10-31T14:59:59+00:00')]:
        repo.execute('INSERT INTO disclosures(institution_id,external_id,title,published_at,fetched_at,kind,source_url) VALUES(?,?,?,?,?,?,?)',
          ('berkshire',external,'TEST',pub,fetched,'13F-HR','https://www.sec.gov/TEST'))
    today=Analytics(repo).today_information('2026-10-31')
    assert today['counts']['published_disclosures']==1 and today['counts']['fetched_disclosures']==1
    assert today['counts']['sec_filings']==1 and today['counts']['etf_snapshots']==1
    assert {r['external_id'] for r in today['disclosures']}=={'after','history'}
    assert today['snapshots'][0]['as_of_date']=='2024-01-01'


def discovery_config(fund='nps-domestic-equity'):
    return dict(id='nps',discovery_rules=[dict(reviewed=True,index_url='https://fund.nps.or.kr/TEST',
      link_pattern=r'/TEST/(?P<as_of_date>\d{4}-\d{2}-\d{2})\.csv$',label_pattern='공식 종목별',
      format='csv',mapping={'kr_code':'code','name':'name','market_value':'value','weight':'weight'},
      multipliers={'market_value':'100000000'},
      metadata=dict(fund_id=fund,published_at='2026-06-30',currency='KRW',disclosure_scope='partial'))])


class NPSFiles:
    def validate_url(self,url):
        assert url.startswith('https://fund.nps.or.kr/')
    def fetch(self,url,types):
        if url.endswith('.csv'):
            return b'code,name,value,weight\n005930,TEST issuer,12,1.5\n','text/csv',url
        return '<a href="/TEST/2025-12-31.csv">공식 종목별 보유내역</a>'.encode(),'text/html',url


def test_reviewed_nps_file_discovery_units_and_replay(repo):
    collector=ReviewedDiscoveryCollector(repo,NPSFiles(),discovery_config())
    assert collector.run()==1 and collector.run()==0
    result=Analytics(repo).holdings('nps-domestic-equity')
    assert result['snapshot']['as_of_date']=='2025-12-31' and result['snapshot']['published_at']=='2026-06-30'
    assert result['rows'][0]['market_value']=='1200000000' and result['rows'][0]['shares'] is None
    assert not repo.rows('SELECT * FROM holding_changes')
    assert not repo.rows("SELECT * FROM source_documents WHERE media_type='text/html'")


def test_unreviewed_discovery_does_not_guess_format(repo):
    config=discovery_config();config['discovery_rules'][0]['reviewed']=False
    with pytest.raises(NeedsConfiguration): ReviewedDiscoveryCollector(repo,NPSFiles(),config).run()


@pytest.mark.parametrize('scope,expected',[('partial','LEFT_DISCLOSED_LIST'),('full','EXITED')])
def test_nps_full_partial_exit_is_explicit(repo,ingest,stock,scope,expected):
    ingest('2025-12-31',[stock],fund='nps-domestic-equity',scope=scope,source_type='nps')
    ingest('2026-12-31',[dict(name='TEST B',kr_code='000660',shares='10')],fund='nps-domestic-equity',scope=scope,source_type='nps')
    rows=Analytics(repo).changes('nps-domestic-equity')['rows']
    assert next(r for r in rows if r['name']==stock['name'])['status']==expected


def nps_home():
    cats=['국내주식','해외주식','국내채권','해외채권','대체투자','단기자금']
    html='<div class="main-portfolio">기금 포트폴리오 <span>2026년 8월 말 기준</span><table><caption>기금 포트폴리오 구성별 규모</caption>'
    html+=''.join('<tr><th>'+n+'</th><td>10 조 원</td><td>10 %</td></tr>' for n in cats)
    return (html+'''</table><a onclick="fnc_goBbsDetail('TEST','offi')"><p>포트폴리오 2026.8월</p><span class="notice-date">2026/10/30</span></a>'''.replace("'TEST'","'1'")).encode()


def test_nps_detailed_failure_preserves_monthly_allocation(repo):
    class Files(NPSFiles):
        def fetch(self,url,types):
            if url.endswith('main.do'):return nps_home(),'text/html',url
            raise TimeoutError('TEST secret must not be logged')
    collector=NPSCollector(repo,Files(),discovery_config())
    assert collector.run()>0
    assert Analytics(repo).allocation('nps') and collector.warnings
    assert 'secret' not in json.dumps(repo.rows('SELECT * FROM source_status'))
    assert repo.rows("SELECT status FROM source_status WHERE component='detailed_holdings'")[0]['status']=='error'


def test_temasek_no_config_is_manual_official_import(repo):
    with pytest.raises(NeedsConfiguration,match='공식 데이터 자동수집 불가 / 수동 공식자료 import 필요'):
        TemasekCollector(repo,None,{}).run()


def test_sol_native_official_dates_product_crosscheck_and_cash(repo):
    class Files:
        day='20261030'
        def fetch(self,url,types):
            if 'viewType' in url:
                return b'var etfResultData = [{"ETF_CD6":"069500","FUND_CD":"211028","Name":"TEST SOL"}];','text/html',url
            payload=[dict(ETF_CD6='069500',FUND_CD='211028',WORK_DT=self.day,STOCK_CODE=code,
              SEC_NM='TEST',QTY=12,WT_DISP='1.2%',PRICE=1000) for code in ('005930','KRD010010001')]
            return json.dumps(payload).encode(),'application/json',url
    files=Files();adapter=SolAdapter(repo,files,{})
    fund=repo.rows("SELECT * FROM funds WHERE id='etf-069500'")[0]
    product=adapter.prepare(fund)
    result=adapter.fetch_day(fund,date(2026,10,30),product)
    assert result['rows'][0]['shares']=='12' and result['rows'][1]['shares'] is None
    assert result['quantity_type']=='pdf_per_cu_quantity' and result['disclosure_scope']=='full'
    assert adapter.fetch_day(fund,date(2026,11,2),product) is None
    files.day='20261103'
    with pytest.raises(ValueError,match='mismatch'):adapter.fetch_day(fund,date(2026,11,2),product)


def test_plus_native_checks_product_and_all_pages_before_import(repo):
    from stock16.collectors.etf_adapters import PlusAdapter
    class Files:
        stale=False
        short=False
        def fetch(self,url,types):
            if '/product/detail' in url:
                return b'<div class="summary__product-code">069500</div>','text/html',url
            page=int(url.split('page=')[-1])
            rows=[dict(wkdate='20261029' if self.stale else '20261030',jmCd='005930' if page==0 else None,
                       krJmCd='KR7005930003' if page==0 else 'KRD010010001',jmNm='TEST',amount=12,ratio=1.2)]
            payload=dict(number=page,content=rows,last=page==1 or self.short,totalElements=2)
            return json.dumps(payload).encode(),'application/json',url
    fund=repo.rows("SELECT * FROM funds WHERE id='etf-069500'")[0]
    fund['source_url']='https://www.plusetf.co.kr/product/detail?n=006304'
    files=Files();adapter=PlusAdapter(repo,files,{})
    assert adapter.prepare(fund)=='006304'
    result=adapter.fetch_day(fund,date(2026,10,30),'006304')
    assert len(result['rows'])==2 and result['rows'][1]['shares'] is None
    assert result['rows'][0]['market_value'] is None
    assert len(json.loads(result['body'])['pages'])==2
    files.stale=True
    assert adapter.fetch_day(fund,date(2026,10,30),'006304') is None
    files.stale=False;files.short=True
    with pytest.raises(ValueError,match='Incomplete'): adapter.fetch_day(fund,date(2026,10,30),'006304')
    fund['code']='000660'
    with pytest.raises(NeedsConfiguration,match='일치'):adapter.prepare(fund)


def test_discovery_preserves_multiple_extractions_of_shared_source(repo):
    import copy
    config=discovery_config()
    second=copy.deepcopy(config['discovery_rules'][0])
    second['metadata']['fund_id']='nps-foreign-equity'
    config['discovery_rules'].append(second)
    assert ReviewedDiscoveryCollector(repo,NPSFiles(),config).run()==2
    assert len(repo.rows('SELECT * FROM source_documents'))==1
    assert {r['fund_id'] for r in repo.rows('SELECT * FROM portfolio_snapshots')}=={'nps-domestic-equity','nps-foreign-equity'}


def test_reviewed_etf_wrapper_checks_real_basis_and_skips_empty(repo):
    from stock16.collectors.etf_adapters import ReviewedManagerAdapter
    class Files:
        rows=[dict(day='20261030',code='005930',name='TEST',quantity=12)]
        def fetch(self,url,types):return json.dumps(dict(content=self.rows)).encode(),'application/json',url
    fund=repo.rows("SELECT * FROM funds WHERE id='etf-069500'")[0]
    entry=dict(reviewed=True,url_template='https://www.aceetf.co.kr/TEST?d={date:%Y%m%d}',
      format='json',rows_path='content',as_of_path='content.0.day',date_format='%Y%m%d',
      mapping=dict(kr_code='code',name='name',shares='quantity'))
    files=Files();adapter=ReviewedManagerAdapter(repo,files,{'providers':{'ace':{'069500':entry}}},'ace')
    context=adapter.prepare(fund)
    assert adapter.fetch_day(fund,date(2026,10,30),context)['rows'][0]['shares']==12
    assert adapter.fetch_day(fund,date(2026,11,2),context) is None
    files.rows=[]
    assert adapter.fetch_day(fund,date(2026,10,30),context) is None
