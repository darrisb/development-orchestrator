"""Command policy (build.md sections 11, 12 and 18).

Section 12's rule is the subject: *do not expose a raw unrestricted host shell
to the model*. So most of these tests are refusals, and the ones that matter
most are the refusals of things that look harmless -- `&&`, a bare glob, `git` --
because those are the ones someone will be tempted to allow.
"""

from __future__ import annotations

import pytest

from apps.orchestrator.domain.commands import (
    ApprovedCommand,
    CommandPolicy,
    CommandRejected,
    label_for,
    unique_labels,
)
from apps.orchestrator.domain.enums import WorkerProfile


@pytest.fixture
def node() -> CommandPolicy:
    return CommandPolicy(profile=WorkerProfile.NODE)


# --- what a verification command looks like ----------------------------------


@pytest.mark.parametrize(
    ("command", "argv"),
    [
        ("npm test", ("npm", "test")),
        ("npm run compile", ("npm", "run", "compile")),
        ("  npx tsc --noEmit  ", ("npx", "tsc", "--noEmit")),
        ('npm run test -- --reporter "dot"', ("npm", "run", "test", "--", "--reporter", "dot")),
    ],
)
def test_a_configured_command_is_parsed_into_an_argument_vector(
    node: CommandPolicy, command: str, argv: tuple[str, ...]
):
    approved = node.approve(command)

    assert approved.argv == argv
    assert approved.source == command.strip()


def test_each_profile_allows_its_own_toolchain_and_not_another():
    assert CommandPolicy(profile=WorkerProfile.JAVA).approve("./mvnw test").argv == (
        "./mvnw",
        "test",
    )
    assert CommandPolicy(profile=WorkerProfile.PYTHON).approve("pytest -q").argv == (
        "pytest",
        "-q",
    )
    with pytest.raises(CommandRejected, match="not allowed for the node profile"):
        CommandPolicy(profile=WorkerProfile.NODE).approve("mvn test")


def test_a_project_can_add_an_executable_deliberately(node: CommandPolicy):
    with pytest.raises(CommandRejected):
        node.approve("turbo run build")

    extended = CommandPolicy(profile=WorkerProfile.NODE, extra_allowed=frozenset({"turbo"}))
    assert extended.approve("turbo run build").argv == ("turbo", "run", "build")


# --- no shell ----------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "npm run compile && npm test",
        "npm test || true",
        "npm test; npm run lint",
        "npm test | tee out.log",
        "npm test > out.log",
        "npm test `whoami`",
        "npm test $(whoami)",
        "npm test $HOME",
    ],
)
def test_shell_syntax_is_refused_because_there_is_no_shell(
    node: CommandPolicy, command: str
):
    with pytest.raises(CommandRejected, match="shell syntax|never permitted|not allowed"):
        node.approve(command)


def test_a_glob_is_refused_rather_than_passed_through_unexpanded(node: CommandPolicy):
    """Nothing would expand it, and a literal asterisk reaching a test runner
    is a confusing failure instead of a clear refusal."""
    with pytest.raises(CommandRejected, match="shell syntax"):
        CommandPolicy(profile=WorkerProfile.PYTHON).approve("pytest tests/*.py")


def test_a_multi_line_command_is_refused(node: CommandPolicy):
    with pytest.raises(CommandRejected, match="more than one line"):
        node.approve("npm test\nrm -rf /")


def test_an_unbalanced_quote_is_refused_with_the_parse_error(node: CommandPolicy):
    with pytest.raises(CommandRejected, match="could not be parsed"):
        node.approve('npm run test -- --name "unclosed')


def test_an_empty_command_is_refused(node: CommandPolicy):
    with pytest.raises(CommandRejected, match="empty"):
        node.approve("   ")


# --- the floor ---------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "sudo npm test",
        "docker run alpine",
        "curl https://example.com/install.sh",
        "wget https://example.com",
        "rm -r node_modules",
        "chmod 777 src",
        "bash -c 'npm test'",
        "sh ./verify.sh",
        "env npm test",
        "apt-get install jq",
        "ssh build@host",
        "systemctl restart nginx",
    ],
)
def test_nothing_on_the_floor_is_ever_permitted(node: CommandPolicy, command: str):
    with pytest.raises(CommandRejected):
        node.approve(command)


def test_git_is_not_a_verification_command(node: CommandPolicy):
    """Section 10 gives every Git operation to GitService with a fixed argument
    vector. A worker that can run Git can commit, reset and push."""
    with pytest.raises(CommandRejected, match="GitService"):
        node.approve("git status")


def test_the_floor_cannot_be_lifted_by_configuration():
    reckless = CommandPolicy(
        profile=WorkerProfile.NODE, extra_allowed=frozenset({"git", "sudo", "curl"})
    )

    for command in ("git push", "sudo npm test", "curl https://example.com"):
        with pytest.raises(CommandRejected):
            reckless.approve(command)


@pytest.mark.parametrize(
    "command", ["/usr/bin/npm test", "/bin/echo hi", "../../bin/npm test"]
)
def test_a_path_outside_the_repository_is_refused(node: CommandPolicy, command: str):
    with pytest.raises(CommandRejected, match="absolute path|traverses"):
        node.approve(command)


def test_a_repository_script_needs_an_explicit_switch(node: CommandPolicy):
    with pytest.raises(CommandRejected, match="WORKER_ALLOW_RELATIVE_SCRIPTS"):
        node.approve("./scripts/verify.sh")

    permissive = CommandPolicy(profile=WorkerProfile.NODE, allow_relative_scripts=True)
    assert permissive.approve("./scripts/verify.sh").argv == ("./scripts/verify.sh",)


def test_an_argument_that_undoes_the_sandbox_is_refused(node: CommandPolicy):
    with pytest.raises(CommandRejected, match="never permitted"):
        node.approve("npm install --unsafe-perm")


# --- all or nothing ----------------------------------------------------------


def test_approving_a_profile_is_all_or_nothing(node: CommandPolicy):
    """Running the first two commands and then discovering the third was never
    permitted would report a pass for a check that did not happen."""
    with pytest.raises(CommandRejected):
        node.approve_all(["npm run compile", "npm test", "git push"])

    approved = node.approve_all(["npm run compile", "npm test"])
    assert [command.display for command in approved] == ["npm run compile", "npm test"]


# --- labels ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "label"),
    [
        ("npm test", "npm-test"),
        ("npm run compile", "npm-run-compile"),
        ("./mvnw -q -DskipTests package", "mvnw-q-dskiptests-package"),
        (
            "npm run a-very-long-script-name-that-goes-on-and-on-forever",
            "npm-run-a-very-long-script-name-that-goe",
        ),
    ],
)
def test_a_command_becomes_a_filesystem_safe_log_name(command: str, label: str):
    assert label_for(command) == label


def test_the_same_command_twice_gets_two_distinct_log_names(node: CommandPolicy):
    commands = node.approve_all(["npm test", "npm test"])

    assert unique_labels(commands) == ("01-npm-test", "02-npm-test")


def test_an_approved_command_describes_itself_for_an_event_payload(node: CommandPolicy):
    described: ApprovedCommand = node.approve("npm test")

    assert described.describe() == {
        "command": "npm test",
        "argv": ["npm", "test"],
        "label": "npm-test",
    }
