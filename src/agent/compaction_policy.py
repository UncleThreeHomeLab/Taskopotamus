class CompactionPolicy:
    def __init__(self, percent=None):
        if percent is not None and (type(percent) is not int or not 1 <= percent <= 100):
            raise ValueError("Auto-compaction percentage must be an integer between 1 and 100.")
        self.percent = percent

    def config(self, context_window):
        if self.percent is None or type(context_window) is not int or context_window <= 0:
            return {}
        return {
            "model_auto_compact_token_limit": max(1, context_window * self.percent // 100),
            "model_auto_compact_token_limit_scope": "total",
        }
