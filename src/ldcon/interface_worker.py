"""`python -m ldcon.interface_worker` stays valid: job_service/web_runtime spawn it and agents.card_fallback matches that argv; the module is ldcon.jobs.interface_worker."""
from ldcon._moved import alias
_module = alias('ldcon.interface_worker')
if __name__ == '__main__': _module.main()
