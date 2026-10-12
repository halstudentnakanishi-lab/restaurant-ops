"""CSV validation and transactional persistence. Amounts are integer JPY."""
import csv
import hashlib
import io
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

HEADERS = ['sale_id', 'sold_on', 'product', 'quantity', 'unit_price']
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10000


class ImportProblem(Exception):
    def __init__(self, errors):
        self.errors = errors


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with db:
            yield db
    finally:
        db.close()


def initialize(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS imports (
          id INTEGER PRIMARY KEY, filename TEXT NOT NULL, digest TEXT NOT NULL,
          status TEXT NOT NULL, row_count INTEGER NOT NULL DEFAULT 0,
          message TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE UNIQUE INDEX IF NOT EXISTS successful_digest
          ON imports(digest) WHERE status='success';
        CREATE TABLE IF NOT EXISTS sales (
          sale_id TEXT PRIMARY KEY, sold_on TEXT NOT NULL, product TEXT NOT NULL,
          quantity INTEGER NOT NULL CHECK(quantity > 0),
          unit_price INTEGER NOT NULL CHECK(unit_price >= 0),
          import_id INTEGER NOT NULL REFERENCES imports(id));
        CREATE INDEX IF NOT EXISTS sales_day ON sales(sold_on);
        CREATE TABLE IF NOT EXISTS reports (
          sold_on TEXT PRIMARY KEY, content TEXT NOT NULL, status TEXT NOT NULL,
          version INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL);
        ''')


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def parse_csv(raw):
    if not raw or len(raw) > MAX_BYTES:
        raise ImportProblem(['空のファイル、または上限2MBを超えるファイルです。'])
    try:
        text = raw.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise ImportProblem(['文字コードはUTF-8で保存してください。'])
    reader = csv.DictReader(io.StringIO(text, newline=''), strict=True)
    errors, records, seen = [], [], set()
    try:
        if reader.fieldnames != HEADERS:
            raise ImportProblem(['ヘッダーと順序を確認してください: ' + ','.join(HEADERS)])
        for row in reader:
            line = reader.line_num
            if len(records) >= MAX_ROWS:
                raise ImportProblem(['1ファイルの上限は10,000行です。'])
            if None in row or any(v is None for v in row.values()):
                errors.append(f'{line}行目: 列数が一致しません。')
                if len(errors) >= 20:
                    break
                continue
            row = {k: v.strip() for k, v in row.items()}
            issue = []
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', row['sale_id']):
                issue.append('sale_idは半角英数字・ハイフン・アンダースコアの1〜64文字')
            if row['sale_id'] in seen:
                issue.append('sale_idがファイル内で重複')
            seen.add(row['sale_id'])
            try:
                if date.fromisoformat(row['sold_on']).isoformat() != row['sold_on']:
                    raise ValueError
            except ValueError:
                issue.append('sold_onは実在する日付YYYY-MM-DD')
            if not row['product'] or len(row['product']) > 100 or any(ord(c) < 32 for c in row['product']):
                issue.append('productは改行を含まない1〜100文字')
            for field, low, high in [('quantity', 1, 10000), ('unit_price', 0, 1000000)]:
                value = row[field]
                if not re.fullmatch(r'[0-9]{1,7}', value) or not low <= int(value) <= high:
                    issue.append(f'{field}は{low}〜{high}の整数')
            if issue:
                errors.append(f'{line}行目: ' + ' / '.join(issue))
            else:
                records.append((row['sale_id'], row['sold_on'], row['product'], int(row['quantity']), int(row['unit_price'])))
            if len(errors) >= 20:
                break
    except csv.Error:
        raise ImportProblem(['CSVの引用符や改行の形式を確認してください。'])
    if errors:
        raise ImportProblem(errors)
    if not records:
        raise ImportProblem(['データ行がありません。'])
    return records


def import_csv(path, filename, raw):
    digest = hashlib.sha256(raw).hexdigest()
    filename = filename.replace('\\', '/').split('/')[-1][:150] or 'upload.csv'
    try:
        records = parse_csv(raw)
        with connect(path) as db:
            # Serialize validation + insertion to prevent double import races.
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM imports WHERE digest=? AND status='success'", (digest,)).fetchone():
                raise ImportProblem(['同じファイルは取り込み済みです。'])
            duplicates = [r[0] for r in records if db.execute('SELECT 1 FROM sales WHERE sale_id=?', (r[0],)).fetchone()]
            if duplicates:
                raise ImportProblem(['登録済みのsale_idがあります: ' + ', '.join(duplicates[:10])])
            days = sorted(set(r[1] for r in records))
            locked = [d for d in days if db.execute("SELECT 1 FROM reports WHERE sold_on=? AND status='confirmed'", (d,)).fetchone()]
            if locked:
                raise ImportProblem(['日報が確定済みの日付には追加できません: ' + ', '.join(locked)])
            cursor = db.execute('INSERT INTO imports(filename,digest,status,row_count,message,created_at) VALUES(?,?,?,?,?,?)',
                                (filename, digest, 'success', len(records), '全行を取り込みました。', now()))
            import_id = cursor.lastrowid
            db.executemany('INSERT INTO sales VALUES(?,?,?,?,?,?)', [(*r, import_id) for r in records])
            # Existing drafts must be regenerated after a changed aggregate.
            db.executemany("UPDATE reports SET status='stale',version=version+1,updated_at=? WHERE sold_on=?", [(now(), d) for d in days])
        return {'id': import_id, 'row_count': len(records), 'dates': days}
    except ImportProblem as exc:
        with connect(path) as db:
            db.execute('INSERT INTO imports(filename,digest,status,row_count,message,created_at) VALUES(?,?,?,?,?,?)',
                       (filename, digest, 'failed', 0, '\n'.join(exc.errors), now()))
        raise


def summary(path, start=None, end=None, product=None):
    where, args = [], []
    if start:
        where.append('sold_on >= ?')
        args.append(start)
    if end:
        where.append('sold_on <= ?')
        args.append(end)
    if product and product.strip():
        # instr treats %, _ and quotes as literal text, not SQL wildcards.
        where.append('instr(product, ?) > 0')
        args.append(product.strip())
    clause = ' WHERE ' + ' AND '.join(where) if where else ''
    with connect(path) as db:
        total = dict(db.execute('SELECT COALESCE(SUM(quantity*unit_price),0) revenue, COALESCE(SUM(quantity),0) quantity, COUNT(*) rows FROM sales' + clause, args).fetchone())
        days = [dict(r) for r in db.execute('SELECT sold_on, SUM(quantity*unit_price) revenue, SUM(quantity) quantity FROM sales' + clause + ' GROUP BY sold_on ORDER BY sold_on', args)]
        products = [dict(r) for r in db.execute('SELECT product, SUM(quantity*unit_price) revenue, SUM(quantity) quantity FROM sales' + clause + ' GROUP BY product ORDER BY revenue DESC, product', args)]
    return {'total': total, 'days': days, 'products': products}


def draft_text(day, result):
    total = result['total']
    lines = [f'{day} 営業日報', '', f"売上合計：{total['revenue']:,}円", f"販売点数：{total['quantity']:,}点", '', '【商品別の売上】']
    lines += [f"・{r['product']}：{r['revenue']:,}円（{r['quantity']}点）" for r in result['products']]
    lines += ['', '【気づき・引き継ぎ】', '記入してください。']
    return '\n'.join(lines)
