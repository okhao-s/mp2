import datetime


class TimerUtils:
    @staticmethod
    def get_random_time(*a, **kw):
        return "0 8 * * *"

    @staticmethod
    def random_scheduler(num_executions: int = 1, begin_hour: int = 7, end_hour: int = 23,
                         min_interval: int = 20, max_interval: int = 40):
        """真实实现返回 datetime 列表（app/utils/timer.py），这里固定两个时间便于断言。"""
        slots = [(begin_hour, 15), (begin_hour + 2, 45)]
        return [datetime.datetime(2026, 1, 1, h, m) for h, m in slots][:num_executions]
