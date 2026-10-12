import csv
import io
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import ImportProblem, import_csv

HEADER = 'sale_id,sold_on,product,quantity,unit_price\n'


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / 'test.sqlite3')) as client:
        yield client


def upload(client, rows, name='test.csv'):
    return client.post('/api/imports', files={'file': (name, (HEADER + rows).encode('utf-8'), 'text/csv')})


def test_totals_and_date_filter(client):
    assert upload(client, 'a,2026-10-01,coffee,2,450\nb,2026-10-02,coffee,1,450\nc,2026-10-02,cake,3,500\n').status_code == 200
    assert client.get('/api/summary').json()['total'] == {'revenue': 2850, 'quantity': 6, 'rows': 3}
    data = client.get('/api/summary?start=2026-10-02&end=2026-10-02').json()
    assert data['total']['revenue'] == 1950
    assert data['products'][0]['product'] == 'cake'
    assert client.get('/api/summary?start=2026-10-03&end=2026-10-01').status_code == 422


@pytest.mark.parametrize('row', [
    'a,2026-10-01,,1,450', 'a,2026-02-30,coffee,1,450',
    'a,2026-10-01,coffee,-1,450', 'a,2026-10-01,coffee,1,NaN',
    'a,2026-10-01,coffee,1,4.5', 'a,2026-10-01,coffee,0,450',
    'a,2026-10-01,coffee,1,-1', 'a,2026-10-01,coffee,1,450,extra',
    'a,2026-10-01,coffee,1', 'a,2026-10-01,coffee,10001,450',
])
def test_invalid_rows_roll_back_entire_file(client, row):
    result = upload(client, 'good,2026-10-01,coffee,1,450\n' + row + '\n')
    assert result.status_code == 422
    assert client.get('/api/summary').json()['total']['rows'] == 0
    assert client.get('/api/imports').json()[0]['status'] == 'failed'


def test_duplicate_file_and_sale_ids(client):
    assert upload(client, 'a,2026-10-01,coffee,1,450\n').status_code == 200
    assert upload(client, 'a,2026-10-01,coffee,1,450\n', 'renamed.csv').status_code == 422
    assert upload(client, 'b,2026-10-01,cake,2,500\na,2026-10-01,coffee,2,450\n').status_code == 422
    assert client.get('/api/summary').json()['total']['rows'] == 1


def test_duplicate_inside_file(client):
    assert upload(client, 'a,2026-10-01,coffee,1,450\na,2026-10-02,cake,1,500\n').status_code == 422
    assert client.get('/api/summary').json()['total']['rows'] == 0


def test_bom_and_quoted_comma(client):
    raw = (HEADER + 'a,2026-10-01,"coffee, large",2,450\n').encode('utf-8-sig')
    assert client.post('/api/imports', files={'file': ('bom.csv', raw)}).status_code == 200
    assert client.get('/api/summary').json()['products'][0]['product'] == 'coffee, large'


@pytest.mark.parametrize('raw', [b'', b'wrong,header\n1,2', b'\xff\xfe', HEADER.encode(), b'x' * (2 * 1024 * 1024 + 1)], ids=['empty', 'header', 'encoding', 'no-rows', 'too-large'])
def test_bad_file(client, raw):
    assert client.post('/api/imports', files={'file': ('bad.csv', raw)}).status_code == 422


def test_report_lifecycle_conflicts_and_export(client):
    upload(client, 'a,2026-10-01,coffee,2,450\n')
    draft = client.post('/api/reports/2026-10-01/draft').json()
    assert '900円' in draft['content']
    assert client.get('/api/reports/2026-10-01/export').status_code == 404
    saved = client.put('/api/reports/2026-10-01', json={'content': draft['content'] + '\n雨天', 'version': draft['version']}).json()
    assert saved['version'] == 2
    assert client.put('/api/reports/2026-10-01', json={'content': 'stale', 'version': 1}).status_code == 409
    confirmed = client.post('/api/reports/2026-10-01/confirm', json={'content': saved['content'], 'version': 2})
    assert confirmed.status_code == 200
    assert client.put('/api/reports/2026-10-01', json={'content': 'overwrite', 'version': 3}).status_code == 409
    assert upload(client, 'b,2026-10-01,cake,1,500\n').status_code == 422
    payload = client.get('/api/reports/2026-10-01/export').json()
    assert payload['event_id'] == 'report:2026-10-01:v3'
    assert payload['report']['status'] == 'confirmed'


def test_more_sales_make_draft_stale_and_preserve_notes(client):
    upload(client, 'a,2026-10-01,coffee,2,450\n')
    draft = client.post('/api/reports/2026-10-01/draft').json()
    client.put('/api/reports/2026-10-01', json={'content': draft['content'].replace('記入してください。', '明日は豆を補充。'), 'version': draft['version']})
    upload(client, 'b,2026-10-01,cake,1,500\n')
    assert client.get('/api/reports').json()[0]['status'] == 'stale'
    assert client.post('/api/reports/2026-10-01/confirm', json={'content': draft['content'], 'version': 2}).status_code == 409
    fresh = client.post('/api/reports/2026-10-01/draft').json()
    assert '1,400円' in fresh['content']
    assert '明日は豆を補充。' in fresh['content']


def test_blank_report_and_missing_day(client):
    assert client.post('/api/reports/2026-10-01/draft').status_code == 404
    upload(client, 'a,2026-10-01,coffee,1,450\n')
    client.post('/api/reports/2026-10-01/draft')
    assert client.post('/api/reports/2026-10-01/confirm', json={'content': '  ', 'version': 1}).status_code == 422


def test_csv_formula_escaped_and_ui_assets(client):
    upload(client, 'a,2026-10-01,=1+1,1,450\n')
    response = client.get('/api/exports/products.csv')
    rows = list(csv.reader(io.StringIO(response.content.decode('utf-8-sig'))))
    assert rows[1][0] == "'=1+1"
    assert client.get('/').status_code == 200
    assert "script-src 'self'" in client.get('/').headers['content-security-policy']
    assert client.get('/static/app.js').status_code == 200


def test_cross_origin_and_host(client):
    assert client.post('/api/reports/2026-10-01/draft', headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/api/summary', headers={'Host': 'evil.example'}).status_code == 400


def test_concurrent_import_only_once(tmp_path):
    path = tmp_path / 'race.sqlite3'
    create_app(path)
    raw = (HEADER + 'a,2026-10-01,coffee,1,450\n').encode()
    def run(_):
        try:
            import_csv(path, 'race.csv', raw)
            return 'success'
        except ImportProblem:
            return 'rejected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, range(2))) == ['rejected', 'success']


def test_database_persists_across_app_instances(tmp_path):
    path = tmp_path / 'persist.sqlite3'
    with TestClient(create_app(path)) as first:
        upload(first, 'a,2026-10-01,coffee,1,450\n')
    with TestClient(create_app(path)) as second:
        assert second.get('/api/summary').json()['total']['revenue'] == 450


def test_product_search_with_dates_and_export(client):
    upload(client, 'a,2026-10-01,ホットコーヒー,2,450\nb,2026-10-02,アイスコーヒー,1,500\nc,2026-10-02,ケーキ,3,600\n')
    params = {'product': ' コーヒー ', 'start': '2026-10-02', 'end': '2026-10-02'}
    result = client.get('/api/summary', params=params).json()
    assert result['total'] == {'revenue': 500, 'quantity': 1, 'rows': 1}
    assert result['products'][0]['product'] == 'アイスコーヒー'
    rows = list(csv.reader(io.StringIO(client.get('/api/exports/products.csv', params=params).content.decode('utf-8-sig'))))
    assert rows == [['商品', '販売点数', '売上金額（円）'], ['アイスコーヒー', '1', '500']]
    assert client.get('/api/summary', params={'product': '該当なし'}).json()['total']['rows'] == 0
    assert client.get('/api/summary', params={'product': '  '}).json()['total']['rows'] == 3
    # A dashboard filter must not reduce the full-day report.
    draft = client.post('/api/reports/2026-10-02/draft').json()
    assert '2,300円' in draft['content']


@pytest.mark.parametrize('keyword', ['%', '_', "' OR 1=1 --"])
def test_search_metacharacters_are_literal(client, keyword):
    upload(client, 'a,2026-10-01,coffee,1,450\n')
    assert client.get('/api/summary', params={'product': keyword}).json()['total']['rows'] == 0


def test_product_search_length_and_portfolio(client):
    for endpoint in ['/api/summary', '/api/exports/products.csv']:
        assert client.get(endpoint, params={'product': 'a' * 101}).status_code == 422
    assert client.get('/portfolio').status_code == 200
    assert 'Restaurant Ops' in client.get('/portfolio').text
    assert client.get('/static/portfolio.css').status_code == 200
