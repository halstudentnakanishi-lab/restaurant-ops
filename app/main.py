"""Local-first demo; bind to 127.0.0.1. No authentication for public deployment."""
import csv
import io
import os
from datetime import date
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .service import MAX_BYTES, ImportProblem, connect, draft_text, import_csv, initialize, now, summary

ROOT = Path(__file__).resolve().parent.parent


class ReportEdit(BaseModel):
    content: str = Field(min_length=1, max_length=10000)
    version: int = Field(ge=1)


def create_app(db_path=None):
    path = Path(db_path or os.environ.get('RESTAURANT_DB', ROOT / 'data' / 'restaurant.sqlite3'))
    initialize(path)
    app = FastAPI(title='Restaurant Ops', version='1.0.0')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])

    @app.middleware('http')
    async def local_requests(request, call_next):
        origin = request.headers.get('origin')
        if request.method not in ('GET', 'HEAD', 'OPTIONS') and origin and origin != str(request.base_url).rstrip('/'):
            return Response('Cross-origin write denied', status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if request.url.path == '/':
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

    @app.get('/')
    def home():
        return FileResponse(ROOT / 'static' / 'index.html')

    @app.get('/api/health')
    def health():
        return {'status': 'ok'}

    @app.get('/api/sample')
    def sample():
        return FileResponse(ROOT / 'samples' / 'sales.csv', filename='sample-sales.csv', media_type='text/csv')

    @app.post('/api/imports')
    async def upload(file: UploadFile = File(...)):
        try:
            raw = await file.read(MAX_BYTES + 1)
        finally:
            await file.close()
        try:
            return import_csv(path, file.filename or 'upload.csv', raw)
        except ImportProblem as exc:
            raise HTTPException(422, detail=exc.errors)

    @app.get('/api/imports')
    def history():
        with connect(path) as db:
            return [dict(r) for r in db.execute('SELECT id,filename,status,row_count,message,created_at FROM imports ORDER BY id DESC LIMIT 100')]

    @app.get('/api/summary')
    def totals(start: date | None = None, end: date | None = None):
        if start and end and start > end:
            raise HTTPException(422, '開始日は終了日以前にしてください。')
        return summary(path, str(start) if start else None, str(end) if end else None)

    @app.get('/api/exports/products.csv')
    def export(start: date | None = None, end: date | None = None):
        data = totals(start, end)
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['商品', '販売点数', '売上金額（円）'])
        for r in data['products']:
            product = r['product']
            # Defuse spreadsheet formulas when downloaded and opened in Excel.
            if product.startswith(('=', '+', '-', '@')):
                product = "'" + product
            writer.writerow([product, r['quantity'], r['revenue']])
        return Response(output.getvalue().encode('utf-8-sig'), media_type='text/csv', headers={'Content-Disposition': 'attachment; filename="product-summary.csv"'})

    @app.get('/api/reports')
    def reports():
        with connect(path) as db:
            return [dict(r) for r in db.execute('SELECT * FROM reports ORDER BY sold_on DESC')]

    @app.post('/api/reports/{day}/draft')
    def draft(day: date):
        day = str(day)
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            previous = db.execute('SELECT * FROM reports WHERE sold_on=?', (day,)).fetchone()
            if previous and previous['status'] != 'stale':
                return dict(previous)
            result = summary(path, day, day)
            if not result['total']['rows']:
                raise HTTPException(404, 'この日の売上データがありません。')
            content = draft_text(day, result)
            if previous:
                # Preserve user-written notes when regenerating aggregate text.
                marker = '【気づき・引き継ぎ】'
                if marker in previous['content']:
                    content = content.split(marker)[0] + marker + previous['content'].split(marker, 1)[1]
            db.execute('''INSERT INTO reports(sold_on,content,status,version,updated_at) VALUES(?,?,?,1,?)
              ON CONFLICT(sold_on) DO UPDATE SET content=excluded.content,status='draft',version=reports.version+1,updated_at=excluded.updated_at''', (day, content, 'draft', now()))
            return dict(db.execute('SELECT * FROM reports WHERE sold_on=?', (day,)).fetchone())

    @app.put('/api/reports/{day}')
    def edit(day: date, body: ReportEdit):
        if not body.content.strip():
            raise HTTPException(422, '日報を入力してください。')
        with connect(path) as db:
            cursor = db.execute("UPDATE reports SET content=?,version=version+1,updated_at=? WHERE sold_on=? AND status='draft' AND version=?", (body.content, now(), str(day), body.version))
            if cursor.rowcount != 1:
                raise HTTPException(409, '日報が更新されたか、編集できない状態です。開き直してください。')
            return dict(db.execute('SELECT * FROM reports WHERE sold_on=?', (str(day),)).fetchone())

    @app.post('/api/reports/{day}/confirm')
    def confirm(day: date, body: ReportEdit):
        if not body.content.strip():
            raise HTTPException(422, '日報を入力してください。')
        with connect(path) as db:
            cursor = db.execute("UPDATE reports SET content=?,status='confirmed',version=version+1,updated_at=? WHERE sold_on=? AND status='draft' AND version=?", (body.content, now(), str(day), body.version))
            if cursor.rowcount != 1:
                raise HTTPException(409, '日報が更新されたか、確定できない状態です。開き直してください。')
            return dict(db.execute('SELECT * FROM reports WHERE sold_on=?', (str(day),)).fetchone())

    @app.get('/api/reports/{day}/export')
    def report_export(day: date):
        with connect(path) as db:
            row = db.execute("SELECT * FROM reports WHERE sold_on=? AND status='confirmed'", (str(day),)).fetchone()
            if not row:
                raise HTTPException(404, '確定済みの日報がありません。')
            return {'event': 'report.confirmed', 'event_id': f'report:{day}:v{row["version"]}', 'report': dict(row)}

    return app
