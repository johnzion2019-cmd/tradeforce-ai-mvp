# TradeForce AI MVP

Deploy-ready FastAPI MVP for a skilled-trades workforce marketplace.

## Features
- Industrial landing page
- Tradesperson registration
- Contractor manpower requests
- Admin dashboard at `/admin`
- SQLite persistence
- Render deployment config
- Health endpoint at `/health`

## Local run
```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

## Render
Build: `pip install -r requirements.txt`
Start: `uvicorn main:app --host 0.0.0.0 --port $PORT`
