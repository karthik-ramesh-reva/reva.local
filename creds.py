creds = {}
def extract_creds():
    global creds
    creds["AWS_ACCESS_KEY_ID"] = "hsdjhvsdjhds"
    creds["AWS_SECRET_ACCESS_KEY"] = "sdfbsdf,sbdf,d"
    creds["AWS_SESSION_TOKEN"] = "sdcsdc/cascd/dcsdc/3e23r/qwd!@<span>@#</span>#%%$^&"

def print_creds():
    for k,v in creds.items():
        print(f"{k}={v}")

extract_creds()
print_creds()