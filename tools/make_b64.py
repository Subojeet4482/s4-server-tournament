"""python tools/make_b64.py serviceAccount.json  ->  prints the base64 text for DBx_FIREBASE_CREDENTIALS_B64"""
import sys, base64
print(base64.b64encode(open(sys.argv[1], "rb").read()).decode())
