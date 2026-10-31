import hashlib
import hmac
import secrets
import time
from flask import request,session,abort
from werkzeug.security import check_password_hash


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def csrf_token():
    if 'csrf' not in session: session['csrf']=secrets.token_urlsafe(32)
    return session['csrf']


def check_csrf():
    supplied=request.headers.get('X-CSRF-Token') or request.form.get('csrf','')
    expected=session.get('csrf','')
    if not expected or not supplied or not hmac.compare_digest(expected,supplied): abort(403)


def authenticated(repo):
    token=session.get('token')
    return bool(token and repo.rows('SELECT token_hash FROM sessions WHERE token_hash=? AND expires_at>?',
                                    (token_hash(token),int(time.time()))))


def login(repo,password,configured_hash):
    # DB-backed limits work across gunicorn workers and survive restarts.
    current=int(time.time())
    ip=token_hash(request.remote_addr or 'unknown')
    with repo.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        c.execute('DELETE FROM login_attempts WHERE window_start<?',(current-900,))
        for key,limit in ((ip,8),('global',80)):
            row=c.execute('SELECT * FROM login_attempts WHERE key=?',(key,)).fetchone()
            if row and row['count']>=limit: abort(429)
        for key in (ip,'global'):
            c.execute('INSERT INTO login_attempts VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET count=count+1',(key,current))
    if not check_password_hash(configured_hash,password): return False
    token=secrets.token_urlsafe(32)
    session.clear()
    session.update(token=token,csrf=secrets.token_urlsafe(32))
    session.permanent=True
    repo.execute('INSERT INTO sessions VALUES(?,?)',(token_hash(token),current+8*3600))
    return True
