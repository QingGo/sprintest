import uvicorn
from fastapi import FastAPI
import threading
import time
import os
import signal
import sys

app = FastAPI()

def handle_exit(sig, frame):
    print("handle_exit CALLED")
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_exit)

@app.get("/")
def read_root():
    return {"Hello": "World"}

def kill_later():
    time.sleep(1)
    print("SENDING SIGTERM")
    os.kill(os.getpid(), signal.SIGTERM)

threading.Thread(target=kill_later, daemon=True).start()

try:
    uvicorn.run(app, port=8000)
finally:
    print("FINALLY BLOCK EXECUTED")
