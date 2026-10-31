"""Disclosure cadence and collector health are independent of portfolio basis dates."""
from datetime import date,datetime,timezone
from zoneinfo import ZoneInfo

STATUS_LABELS={'success':'최신','no_new_data':'신규 데이터 없음','delayed':'공개자료 지연',
 'needs_configuration':'설정 필요','needs_review':'공식 자료 검토 필요','error':'수집 실패',
 'partial':'부분수집','running':'수집 중','unverified':'수집상태 미확인'}


def collector_for(row):
    identifier=row.get('institution_id',row.get('id'))
    if row.get('kind')=='ETF' or identifier=='kr-etf': return 'krx-etf'
    if row.get('kind')=='13F' or row.get('kind')=='US Guru': return 'sec-13f'
    if identifier=='korea-gurus' or row.get('kind')=='Korea Guru': return 'dart'
    return identifier


def decorate_entities(repo,rows,entity_type):
    today=datetime.now(ZoneInfo('Asia/Seoul')).date()
    with repo.connect() as connection:
        for row in rows:
            identifier=row['id']
            collector=collector_for(row)
            field='fund_id' if entity_type=='fund' else 'institution_id'
            snapshot=connection.execute('SELECT * FROM effective_snapshots WHERE '+field+'=? ORDER BY as_of_date DESC,published_at DESC,id DESC LIMIT 1',(identifier,)).fetchone()
            allocation=None
            if entity_type=='institution':
                allocation=connection.execute('SELECT * FROM asset_allocation_snapshots WHERE institution_id=? ORDER BY as_of_date DESC,published_at DESC,id DESC LIMIT 1',(identifier,)).fetchone()
            latest=snapshot or allocation
            components=[dict(r) for r in connection.execute('SELECT * FROM source_status WHERE entity_type=? AND entity_id=?',(entity_type,identifier))]
            if entity_type=='institution' and collector=='sec-13f':
                components += [dict(r) for r in connection.execute("SELECT * FROM source_status WHERE entity_type='fund' AND entity_id=?",(identifier,))]
            run=connection.execute('SELECT * FROM data_fetch_runs WHERE collector IN (?,?) ORDER BY id DESC LIMIT 1',(collector,'backfill:'+str(collector))).fetchone()
            last_success=connection.execute("SELECT max(finished_at) FROM data_fetch_runs WHERE collector=? AND status IN ('success','no_new_data')",(collector,)).fetchone()[0]
            specific=[c for c in components if c['component']!='collector']
            status=(specific[0]['status'] if len(specific)==1 else None) or (run['status'] if run else 'unverified')
            if any(c['status']=='error' for c in specific): status='error'
            elif any(c['status'] in ('needs_configuration','needs_review','partial') for c in specific):
                status='partial' if latest or any(c['last_success'] for c in specific) else (
                    'needs_review' if any(c['status']=='needs_review' for c in specific) else 'needs_configuration')
            # Entity-specific ETF statuses take precedence over the aggregate collector:
            # TIGER configuration must not mark successful KODEX funds as incomplete.
            if (entity_type=='fund' or collector=='sec-13f') and len(specific)==1:
                status=specific[0]['status']
            age=(today-date.fromisoformat(latest['as_of_date'])).days if latest else None
            limit=7 if row.get('kind')=='ETF' else 150 if collector=='sec-13f' else 230 if collector=='nbim' else 550
            if status in ('success','no_new_data','unverified') and age is not None and age>limit:
                status='delayed'
            if not latest and status=='unverified': status='needs_configuration'
            hints={'nps':'상세 종목 공개 시차 존재','nbim':'공식 공개 기준일 기준',
                   'sec-13f':'분기말 기준 자료이며 현재 보유가 아닐 수 있음',
                   'krx-etf':'영업일 PDF 기준','gic':'공개 범위 제한 · 전체 포트폴리오 미공개',
                   'temasek':'Temasek Review · 공식 공개 범위 기준' if latest else '공식 데이터 자동수집 불가 / 수동 공식자료 import 필요'}
            row.update(latest_as_of=latest['as_of_date'] if latest else None,
              latest_as_of_date=latest['as_of_date'] if latest else None,
              latest_published_at=latest['published_at'] if latest else None,
              latest_published=latest['published_at'] if latest else None,
              latest_fetched_at=latest['fetched_at'] if latest else None,
              latest_allocation=allocation['as_of_date'] if allocation else None,
              last_collector_success=max((c['last_success'] for c in components if c['last_success']),default=last_success),
              last_collector_check=max((c['last_check'] for c in components),default=run['started_at'] if run else None),
              next_scheduled=next((c['next_scheduled'] for c in components if c['next_scheduled']),run['next_scheduled'] if run else None),
              data_source=latest['source_url'] if latest else row.get('source_url'),
              status=status,status_label=STATUS_LABELS.get(status,status),components=components,
              freshness_note=hints.get(collector,'공식 공개자료 기준'),basis_age_days=age)
    return rows
