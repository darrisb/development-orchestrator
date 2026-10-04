"""Files the orchestrator ships and installs into a worker (concern 81).

Not Python: the runtime probe is a Node program, copied into a run's worktree
and executed there. It lives here so it is version-controlled and reviewed
alongside the service that runs it, rather than generated as a string.
"""
