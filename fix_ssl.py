import ssl
import os
os.environ['PYTHONHTTPSVERIFY'] = '0'
ssl._create_default_https_context = ssl._create_unverified_context

import urllib3
urllib3.disable_warnings()