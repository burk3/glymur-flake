import pytest


class FakeJournal:
    def __init__(self, identifier="test"):
        self.identifier = identifier
        self.entries = []

    def send(self, message, priority=6, identifier=None, **fields):
        self.entries.append({"MESSAGE": message, "PRIORITY": priority, "IDENT": identifier or self.identifier, **fields})

    def messages(self, ident=None):
        return [e["MESSAGE"] for e in self.entries if ident is None or e["IDENT"] == ident]


@pytest.fixture
def journal():
    return FakeJournal()
