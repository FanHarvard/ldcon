"""`python -m ldcon.web_server` stays valid for the 10 units; the module is ldcon.web.web_server."""
from ldcon._moved import alias
_module = alias('ldcon.web_server')
if __name__ == '__main__': _module.main()
