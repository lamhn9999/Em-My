# Zalo Booking Agent

## Installation
```bash
cd zalo-booking-agent

python -m venv myvenv
source myvenv/bin/activate  # Windows: myvenv\Scripts\activate

pip install -r requirements.txt
pip install -e .

cp .env.example .env
# Fill in your credentials in .env
```

## Run
```bash
uvicorn main:app --reload --port 8000
```