"""Lazy feature launchers; installing/importing ldcon never opens an owner."""
import importlib.util


def require_extra(extra, modules):
    missing = [name for name in modules if importlib.util.find_spec(name) is None]
    if missing:
        raise SystemExit("Missing optional dependencies %s; install ldcon[%s]" % (", ".join(missing), extra))


def instruments_main():
    from ldcon.owners.instrument_service import main
    return main()


def web_main():
    require_extra("web", ("fastapi", "uvicorn"))
    from ldcon.web.web_server import main
    return main()
