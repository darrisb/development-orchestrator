"""Command policy (build.md sections 11, 12 and 18).

Section 12 draws the line this module enforces: *the agent may request an
operation, but the service decides whether it is permitted*, and *do not expose
a raw unrestricted host shell to the model*. So a configured command is not a
string handed to a shell -- it is parsed into an argument vector, checked
against the worker profile's allow-list, and executed directly or refused.

Four consequences worth stating, because each one will look like a bug the
first time it fires:

* **There is no shell, so there are no shell features.** ``npm run build && npm
  test`` is refused: list two commands. A pipe, a redirect, a subshell, a
  backtick or a ``$(...)`` is refused for the same reason. A manifest is
  project-controlled, but "project-controlled" is not "arbitrary": the
  orchestrator runs these commands unattended against a repository it can also
  push.
* **A glob is refused rather than passed through.** Without a shell nothing
  would expand ``tests/*.py``, and passing the literal asterisk to a test
  runner produces a confusing failure instead of a clear refusal.
* **``git`` is not a verification command.** Section 10 gives every Git
  operation to ``GitService`` with a fixed argument vector. A worker that can
  run Git can commit, reset and push, which is the one thing the isolation is
  there to prevent.
* **The allow-list is per profile.** A Node project may run ``npm``; it may not
  run ``mvn``, and neither may run ``curl``. A project that needs something
  else adds it explicitly, which makes the addition a decision someone made.

Pure: parsing and policy only. Running a command is ``services.worker_service``.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .enums import WorkerProfile

#: Characters that only mean anything to a shell. Their presence in a parsed
#: token means the command was written for a shell that will not be there.
SHELL_METACHARACTERS: frozenset[str] = frozenset("&|;<>`$()\n\r*?!{}[]~")

#: Executables permitted per worker profile. Deliberately short: this is the
#: list of things a verification command is, not the list of things a developer
#: might type.
PROFILE_EXECUTABLES: dict[WorkerProfile, frozenset[str]] = {
    WorkerProfile.NODE: frozenset(
        {
            "npm", "npx", "node", "yarn", "pnpm", "corepack",
            "tsc", "eslint", "prettier", "vitest", "jest", "make",
        }
    ),
    WorkerProfile.PYTHON: frozenset(
        {
            "python", "python3", "pip", "pip3", "uv", "uvx",
            "pytest", "ruff", "mypy", "black", "flake8", "tox", "make",
        }
    ),
    WorkerProfile.JAVA: frozenset(
        {"mvn", "./mvnw", "gradle", "./gradlew", "java", "javac", "make"}
    ),
}

#: Refused for every profile, whatever else a project allows. Privilege
#: escalation, container control, the network, the filesystem, and Git.
FORBIDDEN_EXECUTABLES: frozenset[str] = frozenset(
    {
        "sudo", "su", "doas", "pkexec",
        "docker", "podman", "nerdctl", "kubectl", "helm",
        "ssh", "scp", "sftp", "rsync", "curl", "wget", "nc", "netcat", "telnet",
        "rm", "mv", "cp", "dd", "mkfs", "mount", "umount", "chmod", "chown",
        "kill", "killall", "pkill", "reboot", "shutdown", "systemctl", "service",
        "apt", "apt-get", "yum", "dnf", "apk", "brew",
        "sh", "bash", "zsh", "dash", "fish", "env", "eval", "exec", "source",
        "git",
    }
)

#: Arguments refused wherever they appear. ``--unsafe-perm`` and friends undo
#: the container's own restrictions from inside a permitted command.
FORBIDDEN_ARGUMENTS: frozenset[str] = frozenset(
    {"--unsafe-perm", "--allow-root", "--privileged", "--no-sandbox"}
)

#: A label for a log file name: ``npm run compile`` -> ``npm-run-compile``.
_LABEL_UNSAFE = re.compile(r"[^a-z0-9]+")
MAX_LABEL_LENGTH = 40


class CommandRejected(ValueError):
    """A configured command may not be run.

    Carries the command and the reason so both can reach an operator: a
    rejected verification command is a manifest defect, and a message that
    only says "not permitted" makes it a guessing game.
    """

    def __init__(self, command: str, reason: str) -> None:
        super().__init__(f"Command {command!r} is not permitted: {reason}")
        self.command = command
        self.reason = reason


@dataclass(frozen=True, slots=True)
class ApprovedCommand:
    """A command that passed policy, with the vector that will be executed.

    Constructing one of these is the only way to reach a runner, which is what
    makes "approved" a type rather than a convention.
    """

    source: str
    argv: tuple[str, ...]
    label: str

    @property
    def executable(self) -> str:
        return self.argv[0]

    @property
    def display(self) -> str:
        return " ".join(self.argv)

    def describe(self) -> dict[str, object]:
        return {"command": self.source, "argv": list(self.argv), "label": self.label}


@dataclass(frozen=True, slots=True)
class CommandPolicy:
    """What this project's worker is allowed to run.

    Attributes:
        profile: the worker profile, which selects the base allow-list.
        extra_allowed: executables a project added deliberately.
        allow_relative_scripts: permit ``./script.sh``-style executables. Off
            by default: a repository script is arbitrary code, and a coder that
            can edit a permitted script has a shell.
    """

    profile: WorkerProfile = WorkerProfile.NODE
    extra_allowed: frozenset[str] = field(default_factory=frozenset)
    allow_relative_scripts: bool = False

    @property
    def allowed(self) -> frozenset[str]:
        """The profile's list plus the project's additions, minus the floor.

        The subtraction is last and has no override: an operator who adds
        ``git`` to ``extra_allowed`` wants something section 10 forbids, and a
        configurable exception would make that a typo away.
        """
        base = PROFILE_EXECUTABLES.get(self.profile, frozenset())
        return (base | self.extra_allowed) - FORBIDDEN_EXECUTABLES

    def approve(self, command: str) -> ApprovedCommand:
        """Parse and check ``command``.

        Raises:
            CommandRejected: the command is empty, unparseable, uses shell
                syntax, or names an executable this profile does not allow.
        """
        raw = command.strip()
        if not raw:
            raise CommandRejected(command, "it is empty")
        if "\n" in raw or "\r" in raw:
            raise CommandRejected(command, "it spans more than one line")

        try:
            argv = shlex.split(raw)
        except ValueError as error:
            raise CommandRejected(command, f"it could not be parsed ({error})") from error
        if not argv:
            raise CommandRejected(command, "it parses to no arguments")

        offending = _unquoted_metacharacters(raw)
        if offending:
            raise CommandRejected(
                command,
                f"the command contains unquoted shell syntax "
                f"({''.join(sorted(offending))}), and commands run without a shell",
            )
        for token in argv:
            if token in FORBIDDEN_ARGUMENTS:
                raise CommandRejected(command, f"the argument {token!r} is never permitted")

        executable = argv[0]
        self._assert_executable(command, executable)
        return ApprovedCommand(source=raw, argv=tuple(argv), label=label_for(raw))

    def approve_all(self, commands: Iterable[str]) -> tuple[ApprovedCommand, ...]:
        """Approve every command, or raise on the first that fails.

        All-or-nothing on purpose: running the first two commands of a
        verification profile and then discovering the third was never
        permitted reports a pass for a check that did not happen.
        """
        return tuple(self.approve(command) for command in commands)

    def _assert_executable(self, command: str, executable: str) -> None:
        name = executable.rsplit("/", 1)[-1]
        if executable.startswith("/"):
            raise CommandRejected(
                command, f"{executable!r} is an absolute path; name the executable instead"
            )
        if ".." in executable.split("/"):
            raise CommandRejected(command, f"{executable!r} traverses out of the repository")
        if name in FORBIDDEN_EXECUTABLES or executable in FORBIDDEN_EXECUTABLES:
            raise CommandRejected(
                command,
                f"{name!r} is never permitted in a worker"
                + (
                    " (every Git operation belongs to GitService, section 10)"
                    if name == "git"
                    else ""
                ),
            )
        if executable in self.allowed:
            return
        if "/" in executable:
            if self.allow_relative_scripts:
                return
            raise CommandRejected(
                command,
                f"{executable!r} is a repository script; set "
                f"WORKER_ALLOW_RELATIVE_SCRIPTS=true to permit one",
            )
        raise CommandRejected(
            command,
            f"{executable!r} is not allowed for the {self.profile} profile "
            f"(allowed: {', '.join(sorted(self.allowed))})",
        )


def _unquoted_metacharacters(command: str) -> set[str]:
    """Return shell characters outside quotes; quoted ones are argv data."""
    found: set[str] = set()
    quote: str | None = None
    escaped = False
    for character in command:
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote != "'":
            escaped = True
            continue
        if quote:
            if character == quote:
                quote = None
            continue
        if character in {"'", '"'}:
            quote = character
        elif character in SHELL_METACHARACTERS:
            found.add(character)
    return found


def label_for(command: str) -> str:
    """A filesystem-safe label for a command, for naming its log artifact."""
    slug = _LABEL_UNSAFE.sub("-", command.casefold()).strip("-")
    return (slug[:MAX_LABEL_LENGTH].rstrip("-") or "command")


def unique_labels(commands: Sequence[ApprovedCommand]) -> tuple[str, ...]:
    """Labels for a run of commands, numbered so two logs cannot collide.

    ``npm test`` twice in one profile is unusual but legal, and the second log
    silently replacing the first would lose a result.
    """
    return tuple(
        f"{index:02d}-{command.label}" for index, command in enumerate(commands, start=1)
    )


__all__ = [
    "FORBIDDEN_ARGUMENTS",
    "FORBIDDEN_EXECUTABLES",
    "MAX_LABEL_LENGTH",
    "PROFILE_EXECUTABLES",
    "SHELL_METACHARACTERS",
    "ApprovedCommand",
    "CommandPolicy",
    "CommandRejected",
    "label_for",
    "unique_labels",
]
