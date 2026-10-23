import re


class SubagentPolicy:
    def __init__(self, model="gpt-6-luna", effort="high", max_threads=2):
        if not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}", model):
            raise ValueError("Set an explicit subagent model.")
        if effort not in ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            raise ValueError("Invalid subagent reasoning effort.")
        if type(max_threads) is not int or not 1 <= max_threads <= 4:
            raise ValueError("Subagent concurrency must be between 1 and 4.")
        self.model, self.effort, self.max_threads = model, effort, max_threads

    def config(self):
        return {
            "features.multi_agent": True, "agents.enabled": True,
            "agents.default_subagent_model": self.model,
            "agents.default_subagent_reasoning_effort": self.effort,
            "agents.max_concurrent_threads_per_session": self.max_threads,
            "agents.max_depth": 1,
        }
