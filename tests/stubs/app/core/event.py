class Event:
    def __init__(self, **kw):
        self.event_data = kw


class _EventManager:
    def __init__(self):
        self.sent = []

    def register(self, etype):
        def deco(fn):
            return fn
        return deco

    def send_event(self, etype, data):
        self.sent.append((etype, data))


eventmanager = _EventManager()
