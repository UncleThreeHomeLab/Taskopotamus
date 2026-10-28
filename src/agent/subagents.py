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
        policy = (
            "Proactive multi-agent delegation is active. The native default requiring an explicit delegation "
            "request no longer applies. Handle routine questions, lookups, small edits, and clear-cause debugging "
            "directly. Keep compact, self-contained implementation and testing with the parent, even across several "
            "small files or modules. Independence or file count alone does not justify delegation. Delegate only "
            "when a substantial branch benefits enough from separate attention or parallel execution to outweigh "
            "startup, coordination, and review overhead. Start with one substantial child branch and retain meaningful "
            "work with the parent. Add another child only when clear remaining parallel benefit "
            "justifies its overhead. Keep integration, verification, and the final answer with the parent. Honor explicit "
            "requests for helpers or independent reviews within application permissions. "
            "For independent children use fresh context (fork_turns=none); include needed history in a self-contained "
            "brief instead of a full-history fork, which inherits the parent model and reasoning. "
            f"The configured child defaults are model {self.model} and reasoning {self.effort}. "
            "The deployment owner authorizes choosing another available child model or reasoning effort when the "
            "task warrants it, without asking the user to choose a model. Use fresh context for those overrides too. "
            "Application and owner rules and current user restrictions remain authoritative. "
            "Delegation grants no additional tool permissions or authority. "
            "Preserve configured concurrency and depth limits; children must not spawn children."
        )
        return {
            "features.multi_agent": True, "agents.enabled": True,
            "features.multi_agent_v2.multi_agent_mode_hint_text": policy,
            "agents.default_subagent_model": self.model,
            "agents.default_subagent_reasoning_effort": self.effort,
            "agents.max_concurrent_threads_per_session": self.max_threads,
            "agents.max_depth": 1,
        }
