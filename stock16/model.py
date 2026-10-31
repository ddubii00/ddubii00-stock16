"""Immutable snapshots, exact decimals and explicit identifier resolution."""
import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',', ':'),ensure_ascii=False).encode()).hexdigest()


def number(value):
    if value is None or value == '':
        return None
    try:
        d = Decimal(str(value).replace(',', '').strip())
    except InvalidOperation as e:
        raise ValueError('Invalid numeric field') from e
    if not d.is_finite() or abs(d) > Decimal('1e30'):
        raise ValueError('Nonfinite or oversized number')
    return format(d.normalize(), 'f') if d else '0'


def difference(a, b):
    return None if a is None or b is None else number(Decimal(a)-Decimal(b))


def validate_meta(meta):
    for key in ('fund_id','as_of_date','source_url','source_type','currency','disclosure_scope'):
        if not meta.get(key):
            raise ValueError('Missing metadata: '+key)
    date.fromisoformat(meta['as_of_date'])
    if meta.get('published_at'):
        date.fromisoformat(meta['published_at'][:10])
        if meta['as_of_date'] > meta['published_at'][:10]:
            raise ValueError('As-of date cannot follow publication date')
    if not meta['source_url'].startswith('https://'):
        raise ValueError('HTTPS source required')
    if meta.get('data_quality','Official') not in ('Official','Official-derived','Secondary','Unavailable'):
        raise ValueError('Unknown data quality')


def resolve_security(c, row, namespace):
    ids=[]
    for field,scheme in (('isin','ISIN'),('cusip','CUSIP'),('kr_code','KRX')):
        v=str(row.get(field) or '').strip().upper()
        if v:
            if (field=='isin' and len(v)!=12) or (field=='cusip' and len(v)!=9) or (field=='kr_code' and (len(v)!=6 or not v.isdigit())):
                raise ValueError('Invalid security identifier '+field)
            ids.append((scheme,v))
    if not ids:
        # Keep name-only securities source-local and in review; never fuzzy merge.
        local = row.get('source_security_id')
        if not local:
            raise ValueError('Stable identifier required; name-only matching forbidden')
        ids=[('SOURCE:'+namespace,str(local))]
    existing={r[0] for s,v in ids for r in c.execute(
        'SELECT security_id FROM security_identifiers WHERE scheme=? AND value=?',(s,v))}
    if len(existing)>1:
        raise ValueError('Identifier conflict: manual review required')
    if existing:
        sid=existing.pop()
    else:
        issuer_id=None
        if row.get('cik'):
            c.execute('INSERT OR IGNORE INTO issuers(name,cik) VALUES(?,?)', (row['name'],row['cik']))
            issuer_id=c.execute('SELECT id FROM issuers WHERE cik=?',(row['cik'],)).fetchone()[0]
        sid=c.execute('INSERT INTO securities(issuer_id,identity_key,name,isin,cusip,kr_code,ticker,exchange,country,sector,instrument,review_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
          (issuer_id,':'.join(ids[0]),row['name'],row.get('isin'),row.get('cusip'),row.get('kr_code'),row.get('ticker'),row.get('exchange'),row.get('country'),row.get('sector'),row.get('instrument','equity'),'review' if ids[0][0].startswith('SOURCE:') else 'verified')).lastrowid
    for scheme,value in ids:
        c.execute('INSERT OR IGNORE INTO security_identifiers VALUES(?,?,?)',(scheme,value,sid))
    return sid


def holding_map(c, set_hash):
    return {(r['security_id'],r['position_key']):dict(r) for r in c.execute(
      'SELECT v.* FROM holdings h JOIN holding_values v ON v.hash=h.value_hash WHERE h.set_hash=?',(set_hash,))}


def compare(c, before, after):
    old=holding_map(c,before['set_hash']) if before else {}
    new=holding_map(c,after['set_hash'])
    result=[]
    for key in old.keys() | new.keys():
        a,b=old.get(key),new.get(key)
        # An initial observation is a baseline, never evidence of a new purchase.
        if before is None:
            continue
        numeric=('shares','market_value','weight')
        delta={}
        for f in numeric:
            left=a.get(f) if a else ('0' if b.get(f) is not None and before['disclosure_scope']=='full' else None)
            right=b.get(f) if b else ('0' if a.get(f) is not None and after['disclosure_scope']=='full' else None)
            # Monetary changes across currencies are not comparable.
            delta[f]=None if f=='market_value' and a and b and a['currency']!=b['currency'] else difference(right,left)
        raw=delta['shares']
        ratio=Decimal(1)
        if a and b:
            for r in c.execute('SELECT ratio FROM corporate_actions WHERE security_id=? AND effective_date>? AND effective_date<=?',
                               (key[0],before['as_of_date'],after['as_of_date'])):
                ratio*=Decimal(r[0])
            if ratio!=1 and a['shares'] is not None and b['shares'] is not None:
                delta['shares']=number(Decimal(b['shares'])-Decimal(a['shares'])*ratio)
        pct=None
        if a and a['shares'] is not None and Decimal(a['shares']) and delta['shares'] is not None:
            pct=number(Decimal(delta['shares'])/(Decimal(a['shares'])*ratio)*100)
        if a is None:
            status='NEW' if before['disclosure_scope']=='full' else 'FIRST_DISCLOSED'
        elif b is None:
            status='EXITED' if after['disclosure_scope']=='full' else 'LEFT_DISCLOSED_LIST'
        elif delta['shares'] is None:
            status='UNCHANGED' if all(v in (None,'0') for v in delta.values()) else 'VALUE_OR_WEIGHT_CHANGED'
        else:
            status='INCREASED' if Decimal(delta['shares'])>0 else 'DECREASED' if Decimal(delta['shares'])<0 else 'UNCHANGED'
        if status=='UNCHANGED' and all(v in (None,'0') for v in delta.values()) and raw in (None,'0'):
            continue
        result.append(dict(security_id=key[0],position_key=key[1],status=status,
          shares_delta=delta['shares'],shares_delta_pct=pct,market_value_delta=delta['market_value'],
          portfolio_weight_delta_pp=delta['weight'],raw_shares_delta=raw,corporate_action=number(ratio) if ratio!=1 else None))
    return result


def rebuild_changes(c, fund):
    """Rebuild adjacent comparisons on backfills/revisions; raw snapshots stay immutable."""
    c.execute('DELETE FROM holding_changes WHERE snapshot_id IN (SELECT id FROM portfolio_snapshots WHERE fund_id=?)',(fund,))
    previous=None
    for row in c.execute('SELECT * FROM effective_snapshots WHERE fund_id=? ORDER BY as_of_date',(fund,)).fetchall():
        for d in compare(c,previous,row):
            c.execute('INSERT INTO holding_changes VALUES(?,?,?,?,?,?,?,?,?,?,?)',
              (row['id'],previous['id'],d['security_id'],d['position_key'],d['status'],d['shares_delta'],d['shares_delta_pct'],d['market_value_delta'],d['portfolio_weight_delta_pp'],d['raw_shares_delta'],d['corporate_action']))
        previous=row


def import_snapshot(repo,meta,rows,checksum):
    validate_meta(meta)
    if not rows:
        raise ValueError('Empty data requires review; refusing to infer complete liquidation')
    if len(rows)>100000:
        raise ValueError('Too many holdings')
    with repo.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        found=c.execute('SELECT id FROM portfolio_snapshots WHERE fund_id=? AND as_of_date=? AND published_at IS ? AND checksum=?',
                        (meta['fund_id'],meta['as_of_date'],meta['published_at'],checksum)).fetchone()
        if found:
            return found[0],False
        fund=c.execute('SELECT * FROM funds WHERE id=?',(meta['fund_id'],)).fetchone()
        if not fund:
            raise ValueError('Unknown fund')
        values=[]
        value_ids=[]
        seen=set()
        for row in rows:
            sid=resolve_security(c,row,meta['source_type'])
            pk=row.get('position_key','')
            if (sid,pk) in seen:
                raise ValueError('Duplicate position; aggregate explicit identical instruments first')
            seen.add((sid,pk))
            nums=[number(row.get(f)) for f in ('shares','market_value','weight','ownership_pct')]
            if nums[0] is not None and Decimal(nums[0])<0:
                raise ValueError('Negative long share count')
            if nums[2] is not None and not Decimal(0)<=Decimal(nums[2])<=Decimal(100):
                raise ValueError('Invalid percentage weight')
            value=[sid,pk,*nums,row.get('currency',meta['currency'])]
            h=digest(value)
            compact_hash=bytes.fromhex(h)
            c.execute('INSERT OR IGNORE INTO holding_values(hash,security_id,position_key,shares,market_value,weight,ownership_pct,currency) VALUES(?,?,?,?,?,?,?,?)',(compact_hash,*value))
            value_ids.append(c.execute('SELECT id FROM holding_values WHERE hash=?',(compact_hash,)).fetchone()[0])
            values.append(h)
        state=digest(sorted(values))
        is_new=c.execute('INSERT OR IGNORE INTO holding_sets(hash,count) VALUES(?,?)',(state,len(values))).rowcount
        if is_new:
            set_id=c.execute('SELECT id FROM holding_sets WHERE hash=?',(state,)).fetchone()[0]
            c.executemany('INSERT INTO holding_members VALUES(?,?)',[(set_id,i) for i in value_ids])
        cur=c.execute('INSERT INTO portfolio_snapshots(fund_id,institution_id,as_of_date,published_at,fetched_at,source_url,source_type,checksum,currency,data_quality,disclosure_scope,set_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
          (fund['id'],fund['institution_id'],meta['as_of_date'],meta['published_at'],meta.get('fetched_at',now()),meta['source_url'],meta['source_type'],checksum,meta['currency'],meta.get('data_quality','Official'),meta['disclosure_scope'],state))
        rebuild_changes(c,fund['id'])
        return cur.lastrowid,True


def import_allocation(repo,meta,rows,checksum):
    validate_meta(meta)
    if not rows:
        raise ValueError('Empty allocation')
    with repo.connect() as c:
        institution=c.execute('SELECT institution_id FROM funds WHERE id=?',(meta['fund_id'],)).fetchone()[0]
        added=0
        for row in rows:
            weight=number(row.get('weight'))
            if weight is not None and not Decimal(0)<=Decimal(weight)<=100:
                raise ValueError('Invalid allocation weight')
            added+=c.execute('INSERT OR IGNORE INTO asset_allocation_snapshots(institution_id,as_of_date,published_at,fetched_at,original_category,standard_category,dimension,weight,target_weight,market_value,currency,source_url,checksum,data_quality) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
             (institution,meta['as_of_date'],meta['published_at'],now(),row['original_category'],row['standard_category'],row.get('dimension','asset'),weight,number(row.get('target_weight')),number(row.get('market_value')),meta['currency'],meta['source_url'],checksum,meta.get('data_quality','Official'))).rowcount
        return added
