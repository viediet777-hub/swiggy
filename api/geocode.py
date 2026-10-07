import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.getcwd()):
    if _p and _p not in sys.path:
        sys.path.insert(0, _p)

from offer_core import BaseHandler  # noqa: E402


class handler(BaseHandler):
    endpoint = "geocode"
