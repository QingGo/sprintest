import sys
import requests

DAEMON_URL = "http://localhost:8000/v1/test/run"

def main():
    args = sys.argv[1:]
    payload = {"args": args, "target_pkg": "my_project"}
    
    response = requests.post(DAEMON_URL, json=payload)
    response.raise_for_status()
    
    data = response.json()
    print(data["output"])
    sys.exit(data["exit_code"])
