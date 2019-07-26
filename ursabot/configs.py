import toolz


class ProjectConfig:

    def __init__(self, name, workers, builders, schedulers, pollers=None,
                 reporters=None, images=None):
        self.name = name
        self.workers = workers
        self.builders = builders
        self.schedulers = schedulers
        self.images = images or []
        self.pollers = pollers or []
        self.reporters = reporters or []

    def __repr__(self):
        return f'<{self.__class}: {self.name}>'


def MasterConfig(title, url, webui_port, worker_port, projects,
                 database_url=None, auth=None, authz=None, change_hook=None,
                 secret_providers=None):
    """Returns with the dictionary that the buildmaster pays attention to."""

    def component(key):
        return list(toolz.concat(getattr(p, key) for p in projects))

    if change_hook is None:
        hook_dialect_config = {}
    else:
        hook_dialect_config = change_hook._as_hook_dialect_config()

    buildmaster_config = {
        'buildbotNetUsageData': None,
        'title': title,
        'titleURL': url,
        'buildbotURL': url,
        'workers': component('workers'),
        'builders': component('builders'),
        'schedulers': component('schedulers'),
        'services': component('reporters'),
        'change_source': component('pollers'),
        'secretsProviders': secret_providers or [],
        'protocols': {'pb': {'port': worker_port}},
        'db': {'db_url': database_url},
        'www': {
            'port': webui_port,
            'change_hook_dialects': hook_dialect_config,
            'plugins': {
                'waterfall_view': {},
                'console_view': {},
                'grid_view': {}
            }
        }
    }

    # buildbot raises errors for None or empty dict values so only set of they
    # are passed
    if auth is not None:
        buildmaster_config['www']['auth'] = auth
    if authz is not None:
        buildmaster_config['www']['authz'] = authz

    return buildmaster_config