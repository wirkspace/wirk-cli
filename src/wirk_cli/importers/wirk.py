"""The importer against WIRK (docs/plans/importers.md §3.4–§3.7). Not built yet."""


class Wirk:
    def __init__(self, service, workspace=None):
        self.service, self.workspace = service, workspace


class Importer:
    def __init__(self, wirk, ctx, **options):
        raise NotImplementedError
