import re
import secrets
import pytest
from werkzeug.security import generate_password_hash
from stock16.web import create_app


@pytest.fixture
def app(tmp_path):
    return create_app(dict(TESTING=True,SECRET_KEY=secrets.token_hex(32),PASSWORD_HASH=generate_password_hash('fixture-only-password'),DATA_DIR=tmp_path,SESSION_COOKIE_SECURE=True))


def signin(client):
    page=client.get('/stock16-7/login',base_url='https://localhost')
    token=re.search('name="csrf" value="([^"]+)"',page.text)[1]
    return client.post('/stock16-7/login',data={'csrf':token,'password':'fixture-only-password'},base_url='https://localhost')


def test_auth_and_cookie(app):
    with app.test_client() as c:
        response=signin(c);assert response.status_code==302
        cookie=response.headers['Set-Cookie']
        assert all(x in cookie for x in ('HttpOnly','Secure','SameSite=Strict','Path=/stock16-7/'))
        assert c.get('/stock16-7/api/dashboard',base_url='https://localhost').status_code==200


def test_unauthorized_all_api(app):
    with app.test_client() as c:
        for path in ('dashboard','institutions','data-status','consensus','etfs','gurus','securities','disclosures','institutions/nps/holdings'):
            assert c.get('/stock16-7/api/'+path).status_code==401
        assert c.get('/stock16-7/').status_code==302


def test_csrf_on_login_and_mutation(app):
    with app.test_client() as c:
        assert c.post('/stock16-7/login',data={'password':'fixture-only-password'}).status_code==403
        signin(c)
        assert c.post('/stock16-7/api/watchlist/etf',json={}).status_code==403
        assert c.post('/stock16-7/logout').status_code==403


def test_bruteforce(app):
    with app.test_client() as c:
        c.get('/stock16-7/login',base_url='https://localhost')
        with c.session_transaction(path="/stock16-7/",base_url="https://localhost") as s: token=s['csrf']
        statuses=[c.post('/stock16-7/login',data={'csrf':token,'password':'wrong'},base_url='https://localhost').status_code for _ in range(9)]
        assert statuses[-1]==429


def test_logout_revokes_session(app):
    with app.test_client() as c:
        signin(c)
        with c.session_transaction(path="/stock16-7/",base_url="https://localhost") as s: token=s['csrf']
        old=c.get_cookie('stock16_session',path='/stock16-7/').value
        assert c.post('/stock16-7/logout',data={'csrf':token},base_url='https://localhost').status_code==302
        c.set_cookie('stock16_session',old,path='/stock16-7/')
        assert c.get('/stock16-7/api/dashboard',base_url='https://localhost').status_code==401


def test_watchlist_changes_and_export(app):
    with app.test_client() as c:
        signin(c)
        with c.session_transaction(path="/stock16-7/",base_url="https://localhost") as s: token=s['csrf']
        headers={'X-CSRF-Token':token}
        b=dict(id='test-fund',code='123456',name='TEST',source_url='https://data.krx.co.kr/')
        assert c.post('/stock16-7/api/watchlist/etf',json=b,headers=headers).status_code==200
        assert c.delete('/stock16-7/api/watchlist/etf',json={'id':'test-fund'},headers=headers).status_code==200
        assert c.get('/stock16-7/api/institutions/nps/holdings?format=csv').mimetype=='text/csv'


def test_date_filter_validation(app):
    with app.test_client() as c:
        signin(c)
        assert c.get('/stock16-7/api/institutions/nps/holdings?as_of=broken').status_code==400


def test_mobile_and_secure_headers(app):
    with app.test_client() as c:
        signin(c);r=c.get('/stock16-7/')
        assert 'width=device-width' in r.text
        assert 'frame-ancestors' in r.headers['Content-Security-Policy']
        css=c.get('/stock16-7/static/app.css').text
        assert '@media(max-width:700px)' in css
