"""`python -m ldcon.agent_tools <context>` stays valid for ENTRY_MODULES and external callers; the module is ldcon.agents.agent_tools."""
from ldcon._moved import alias
_module = alias('ldcon.agent_tools')
if __name__ == '__main__': _module.main()
