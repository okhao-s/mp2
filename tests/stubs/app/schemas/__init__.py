class Response:
    def __init__(self, success=False, message="", data=None):
        self.success = success
        self.message = message
        self.data = data
