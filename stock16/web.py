import csv
import io
import os
import re
from datetime import timedelta
from flask import Flask,Blueprint,abort,jsonify,redirect,render_template,request,session,url_for
from .settings import load_env
from .db import Repository
from .analytics import Analytics,PERIODS
from .auth import authenticated,check_csrf,csrf_token,login,token_hash
from .storage import storage_status


def create_app(test_config=None):
    load_env()
    prefix=os.environ.get('STOCK16_BASE_PATH','/stock16-7').rstrip('/')
    app=Flask(__name__,static_url_path=prefix+'/static')
    app.config.update(SECRET_KEY=os.environ.get('STOCK16_SECRET_KEY'),PASSWORD_HASH=os.environ.get('STOCK16_PASSWORD_HASH'),
      SESSION_COOKIE_NAME='stock16_session',SESSION_COOKIE_HTTPONLY=True,SESSION_COOKIE_SECURE=os.environ.get('STOCK16_SECURE_COOKIE','1')=='1',
      SESSION_COOKIE_SAMESITE='Strict',SESSION_COOKIE_PATH=prefix+'/',PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
      MAX_CONTENT_LENGTH=65536,DATA_DIR=None)
    if test_config: app.config.update(test_config)
    if not app.config['SECRET_KEY'] or len(app.config['SECRET_KEY'])<32 or not app.config['PASSWORD_HASH']:
        raise RuntimeError('Run python -m stock16 setup to create server-only secrets')
    repo=Repository(app.config['DATA_DIR'])
    repo.initialize()
    app.extensions['repo']=repo
    a=Analytics(repo)
    bp=Blueprint('main',__name__,url_prefix=prefix)

    @app.before_request
    def protect():
        if request.endpoint in ('main.signin','static'):
            if request.method=='POST': check_csrf()
            return
        if request.endpoint is None: return
        if not authenticated(repo):
            if '/api/' in request.path: return jsonify(error='authentication_required'),401
            return redirect(url_for('main.signin'))
        if request.method not in ('GET','HEAD','OPTIONS'): check_csrf()

    @app.after_request
    def headers(response):
        response.headers.update({'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
         'X-Frame-Options':'DENY','Referrer-Policy':'no-referrer',
         'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'"})
        return response

    @bp.route('/login',methods=['GET','POST'])
    def signin():
        error=None
        if request.method=='POST':
            if login(repo,request.form.get('password',''),app.config['PASSWORD_HASH']): return redirect(url_for('main.index'))
            error='비밀번호를 확인해 주세요.'
        return render_template('login.html',csrf=csrf_token(),error=error,prefix=prefix),401 if error else 200

    @bp.post('/logout')
    def signout():
        repo.execute('DELETE FROM sessions WHERE token_hash=?',(token_hash(session.get('token','')),))
        session.clear()
        return redirect(url_for('main.signin'))

    @bp.get('/')
    def index(): return render_template('index.html',csrf=csrf_token(),prefix=prefix)

    @bp.get('/api/dashboard')
    def dashboard(): return jsonify(a.dashboard())

    @bp.get('/api/institutions')
    def institutions(): return jsonify(a.institutions())

    @bp.get('/api/institutions/<identifier>')
    def institution(identifier):
        rows=repo.rows('SELECT * FROM institutions WHERE id=?',(identifier,))
        if not rows: abort(404)
        return jsonify(**rows[0],funds=repo.rows('SELECT * FROM funds WHERE institution_id=?',(identifier,)))

    def validate_date(value):
        if value:
            from datetime import date
            try: date.fromisoformat(value)
            except ValueError: abort(400)
        return value

    def filters(rows):
        query=request.args.get('q','').casefold()
        status=request.args.get('status')
        country=request.args.get('country')
        sector=request.args.get('sector')
        rows=[r for r in rows if (not query or query in str(r.get('name','')).casefold() or query in str(r.get('ticker','')).casefold()) and
              (not status or r.get('status')==status) and (not country or r.get('country')==country) and (not sector or r.get('sector')==sector)]
        sort=request.args.get('sort')
        if sort and sort in ('shares','weight','market_value','shares_delta','portfolio_weight_delta_pp','market_value_delta'):
            from decimal import Decimal
            rows.sort(key=lambda r:Decimal(r.get(sort) or '-1e31'),reverse=request.args.get('order','desc')!='asc')
        return rows

    def output(payload):
        if request.args.get('format')!='csv': return jsonify(payload)
        rows=payload.get('rows',payload) if isinstance(payload,dict) else payload
        f=io.StringIO()
        if rows:
            # Every exported row includes dates/source metadata from the relevant snapshot.
            keys=list(rows[0])
            w=csv.DictWriter(f,fieldnames=keys,extrasaction='ignore')
            w.writeheader()
            for r in rows:
                clean={k:("'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) and not re.fullmatch(r'-?\d+(?:\.\d+)?',v) else v) for k,v in r.items()}
                w.writerow(clean)
        return app.response_class('\ufeff'+f.getvalue(),mimetype='text/csv',headers={'Content-Disposition':'attachment; filename="stock16.csv"'})

    @bp.get('/api/institutions/<identifier>/holdings')
    def holdings(identifier):
        result=a.holdings(request.args.get('fund',identifier),validate_date(request.args.get('as_of')))
        result['rows']=filters(result['rows'])
        if result['snapshot']:
            for row in result['rows']:
                row.update({k:result['snapshot'][k] for k in ('as_of_date','published_at','fetched_at','source_url','data_quality','disclosure_scope')})
        return output(result)

    @bp.get('/api/institutions/<identifier>/changes')
    @bp.get('/api/etfs/<identifier>/changes')
    def changes(identifier):
        period=request.args.get('period','previous')
        if period not in (*PERIODS,'previous','5b','20b'): abort(400)
        result=a.changes(request.args.get('fund',identifier),period,validate_date(request.args.get('as_of')),validate_date(request.args.get('from')))
        result['rows']=filters(result['rows'])
        return output(result)

    @bp.get('/api/institutions/<identifier>/allocation')
    def allocation(identifier): return output(filters(a.allocation(identifier)))

    @bp.get('/api/etfs')
    def etfs(): return jsonify(repo.rows('SELECT f.*,w.enabled,(SELECT max(as_of_date) FROM effective_snapshots p WHERE p.fund_id=f.id) latest_as_of FROM funds f JOIN etf_watchlist w ON w.fund_id=f.id'))

    @bp.get('/api/gurus')
    def gurus(): return jsonify(repo.rows('SELECT i.*,g.cik,g.corp_code,g.enabled,g.disclosure_level FROM institutions i JOIN guru_watchlist g ON g.institution_id=i.id'))

    @bp.route('/api/watchlist/<kind>',methods=['POST','DELETE'])
    def watchlist(kind):
        body=request.get_json(silent=True) or {}
        identifier=body.get('id','')
        if not re.fullmatch(r'[a-z0-9-]{1,64}',identifier): abort(400)
        if kind not in ('etf','guru'): abort(404)
        with repo.connect() as c:
            if request.method=='DELETE':
                table='etf_watchlist' if kind=='etf' else 'guru_watchlist'
                field='fund_id' if kind=='etf' else 'institution_id'
                c.execute('UPDATE '+table+' SET enabled=0 WHERE '+field+'=?',(identifier,))
            else:
                name=str(body.get('name','')).strip()[:200]
                source=body.get('source_url','')
                if not name or not source.startswith('https://'): abort(400)
                if kind=='etf':
                    code=body.get('code','')
                    if not re.fullmatch(r'\d{6}',code): abort(400)
                    c.execute('INSERT INTO funds VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,source_url=excluded.source_url',
                              (identifier,'kr-etf',name,'ETF',code,source))
                    c.execute('INSERT INTO etf_watchlist VALUES(?,1) ON CONFLICT(fund_id) DO UPDATE SET enabled=1',(identifier,))
                else:
                    cik=body.get('cik') or None
                    corp=body.get('corp_code') or None
                    if not ((cik and re.fullmatch(r'\d{1,10}',cik)) or (corp and re.fullmatch(r'\d{8}',corp))): abort(400)
                    if body.get('disclosure_level','Medium') not in ('High','Medium','Low'): abort(400)
                    c.execute('INSERT OR IGNORE INTO institutions VALUES(?,?,?,?,?,?,?)',
                              (identifier,name,'US Guru' if cik else 'Korea Guru','US' if cik else 'KR','공식 공개 범위','quarterly' if cik else 'events',source))
                    c.execute('INSERT OR IGNORE INTO funds VALUES(?,?,?,?,?,?)',(identifier,identifier,name,'13F' if cik else 'DART',None,source))
                    c.execute('INSERT INTO guru_watchlist VALUES(?,?,?,1,?) ON CONFLICT(institution_id) DO UPDATE SET enabled=1,cik=excluded.cik,corp_code=excluded.corp_code,disclosure_level=excluded.disclosure_level',
                              (identifier,cik,corp,body.get('disclosure_level','Medium')))
        return jsonify(ok=True)

    @bp.get('/api/securities')
    def securities():
        q='%'+request.args.get('q','')[:100]+'%'
        return jsonify(repo.rows('SELECT * FROM securities WHERE name LIKE ? OR ticker LIKE ? OR kr_code LIKE ? OR isin LIKE ? OR cusip LIKE ? LIMIT 100',(q,)*5))

    @bp.get('/api/securities/<int:sid>')
    def security(sid):
        rows=repo.rows('SELECT * FROM securities WHERE id=?',(sid,))
        if not rows: abort(404)
        positions=a.security_holdings(sid)
        from decimal import Decimal
        return jsonify(**rows[0],attention=dict(
            holding_institutions=len({p['institution_id'] for p in positions if p['kind']!='ETF'}),
            etf_count=len({p['fund_id'] for p in positions if p['kind']=='ETF'}),
            new_institutions=len({p['institution_id'] for p in positions if p['status']=='NEW'}),
            increased_institutions=len({p['institution_id'] for p in positions if p['shares_delta'] is not None and Decimal(p['shares_delta'])>0}),
            decreased_institutions=len({p['institution_id'] for p in positions if p['shares_delta'] is not None and Decimal(p['shares_delta'])<0})))

    @bp.get('/api/rankings')
    def rankings():
        from decimal import Decimal
        rows=repo.rows('''SELECT ch.*,s.name,s.country,s.sector,p.fund_id,p.as_of_date,p.published_at,p.source_url,f.name fund_name,p.currency
          FROM holding_changes ch JOIN effective_snapshots p ON p.id=ch.snapshot_id
          JOIN securities s ON s.id=ch.security_id JOIN funds f ON f.id=p.fund_id
          WHERE p.id=(SELECT e.id FROM effective_snapshots e WHERE e.fund_id=p.fund_id ORDER BY e.as_of_date DESC LIMIT 1)''')
        field=request.args.get('metric','shares_delta')
        if field not in ('shares_delta','portfolio_weight_delta_pp','market_value_delta'): abort(400)
        rows=filters(rows)
        if request.args.get('currency'): rows=[r for r in rows if r['currency']==request.args['currency']]
        rows=[r for r in rows if r[field] is not None]
        rows.sort(key=lambda r:Decimal(r[field]),reverse=request.args.get('direction','desc')!='asc')
        return output({'rows':rows[:500],'limitation':'통화·측정단위가 다른 수량/금액의 크기 비교에 주의. CU당 ETF 수량은 전체 펀드 수량이 아닙니다.'})

    @bp.get('/api/securities/<int:sid>/institution-holdings')
    def security_positions(sid): return output(a.security_holdings(sid))

    @bp.get('/api/consensus')
    def consensus():
        try: minimum=max(1,min(int(request.args.get('minimum','2')),100))
        except ValueError: abort(400)
        mode=request.args.get('mode','increased')
        if mode not in ('held','increased','weight','new'): abort(400)
        result=a.consensus(request.args.get('period','1m'),minimum,mode,request.args.getlist('institution') or None)
        return jsonify(filters(result))

    @bp.get('/api/disclosures')
    def disclosures(): return output(a.disclosures(validate_date(request.args.get('since')) or '0000-01-01',request.args.get('institution')))

    @bp.get('/api/data-status')
    def data_status():
        return jsonify(storage=storage_status(repo),runs=repo.rows('SELECT * FROM data_fetch_runs ORDER BY id DESC LIMIT 100'),
          collectors=repo.rows("SELECT collector,max(CASE WHEN status IN ('success','no_new_data') THEN finished_at END) last_success,max(CASE WHEN status='partial' THEN finished_at END) last_partial,max(started_at) last_check FROM data_fetch_runs GROUP BY collector"),
          snapshots=repo.rows('SELECT fund_id,count(*) count,max(as_of_date) as_of_date,max(published_at) published_at FROM portfolio_snapshots GROUP BY fund_id'),
          pending_review=repo.rows("SELECT count(*) count FROM securities WHERE review_status='review'")[0]['count'])

    app.register_blueprint(bp)

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def error(e):
        return jsonify(error=e.name,status=e.code),e.code

    return app
