import os

creds = {}
def extract_creds():
    global creds
    creds["AWS_ACCESS_KEY_ID"] = os.getenv("AWS_ACCESS_KEY_ID")     or "NOT_SET"
    creds["AWS_SECRET_ACCESS_KEY"] = os.getenv("AWS_SECRET_ACCESS_KEY") or "NOT_SET"
    creds["AWS_SESSION_TOKEN"] = os.getenv("AWS_SESSION_TOKEN")     or "NOT_SET"

def print_creds():
    for k,v in creds.items():
        print(f"{k}:{v}")

extract_creds()
print_creds()