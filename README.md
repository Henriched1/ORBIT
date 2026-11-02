# ORBIT

**The infrastructure that turns intentions into verified outcomes.**

ORBIT is an early-stage platform for submitting, organizing and tracking user intentions.

## Technology
- Python
- FastAPI
- SQLite

## Run locally
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000

## API
- GET /health
- GET /api/tasks
- POST /api/tasks

## Safety
This prototype records intentions. It does not independently execute payments, bookings or external actions.
