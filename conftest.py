collect_ignore_glob = []

# Disable anchorpy plugin if installed (conflict from sibling Solana project)
def pytest_configure(config):
    try:
        config.pluginmanager.set_blocked("anchorpy")
    except Exception:
        pass
