from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import sqlite3, os

app = FastAPI(title='TradeForce AI')
app.mount('/static', StaticFiles(directory='static'), name='static')
templates = Jinja2Templates(directory='templates')
DB = os.getenv('DB_PATH', 'tradeforce.db')

def init_db():
    with sqlite3.connect(DB) as c:
        c.execute('''CREATE TABLE IF NOT EXISTS workers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, trade TEXT NOT NULL, location TEXT,
            experience INTEGER DEFAULT 0, certifications TEXT,
            phone TEXT, email TEXT, available INTEGER DEFAULT 1
        )''')
        c.execute('''CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company TEXT NOT NULL, contact TEXT, trade TEXT NOT NULL,
            workers_needed INTEGER DEFAULT 1, location TEXT,
            start_date TEXT, duration TEXT, pay_range TEXT, notes TEXT
        )''')

init_db()

@app.get('/', response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse('index.html', {'request': request})

@app.get('/worker', response_class=HTMLResponse)
def worker_form(request: Request):
    return templates.TemplateResponse('worker.html', {'request': request})

@app.post('/worker')
def create_worker(
    name: str = Form(...), trade: str = Form(...), location: str = Form(''),
    experience: int = Form(0), certifications: str = Form(''),
    phone: str = Form(''), email: str = Form('')
):
    with sqlite3.connect(DB) as c:
        c.execute('INSERT INTO workers(name,trade,location,experience,certifications,phone,email) VALUES(?,?,?,?,?,?,?)',
                  (name, trade, location, experience, certifications, phone, email))
    return RedirectResponse('/thanks?type=worker', status_code=303)

@app.get('/contractor', response_class=HTMLResponse)
def contractor_form(request: Request):
    return templates.TemplateResponse('contractor.html', {'request': request})

@app.post('/contractor')
def create_request(
    company: str = Form(...), contact: str = Form(''), trade: str = Form(...),
    workers_needed: int = Form(1), location: str = Form(''), start_date: str = Form(''),
    duration: str = Form(''), pay_range: str = Form(''), notes: str = Form('')
):
    with sqlite3.connect(DB) as c:
        c.execute('INSERT INTO requests(company,contact,trade,workers_needed,location,start_date,duration,pay_range,notes) VALUES(?,?,?,?,?,?,?,?,?)',
                  (company, contact, trade, workers_needed, location, start_date, duration, pay_range, notes))
    return RedirectResponse('/thanks?type=contractor', status_code=303)

@app.get('/admin', response_class=HTMLResponse)
def admin(request: Request):
    with sqlite3.connect(DB) as c:
        c.row_factory = sqlite3.Row
        workers = c.execute('SELECT * FROM workers ORDER BY id DESC').fetchall()
        requests = c.execute('SELECT * FROM requests ORDER BY id DESC').fetchall()
    return templates.TemplateResponse('admin.html', {'request': request, 'workers': workers, 'requests': requests})

@app.get('/thanks', response_class=HTMLResponse)
def thanks(request: Request, type: str = 'worker'):
    return templates.TemplateResponse('thanks.html', {'request': request, 'type': type})

@app.get('/health')
def health():
    return {'status': 'ok', 'service': 'TradeForce AI'}
