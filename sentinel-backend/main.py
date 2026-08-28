"""
Ponto de entrada do backend do Sentinel.

Uso:
    pip install -r requirements.txt
    python main.py

A API sobe em http://localhost:8000 — documentação interativa automática em
http://localhost:8000/docs
"""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("app.api:app", host="0.0.0.0", port=8000, reload=False)
