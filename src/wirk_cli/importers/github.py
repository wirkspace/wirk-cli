"""The GitHub adapter (docs/plans/importers.md §4). Not built yet."""


class Stop(Exception):
    def __init__(self, message, fix=""):
        super().__init__(message)
        self.fix = fix


class GitHub:
    def __init__(self, run=None, sleep=None):
        raise NotImplementedError
