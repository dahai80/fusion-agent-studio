"""Dispatch contract regression (#707 acceptance finding): every handler
exposed by a SubDispatcher must be a coroutine function. DaemonServer
dispatch awaits handler results unconditionally (daemon_server.py), so a
sync handler crashes at RPC time with "object dict can't be used in 'await'
expression" even if unit tests that call the handler directly all pass.
"""

import inspect
from unittest.mock import MagicMock

import pytest

from agent_runtime.dispatchers import (
    AgentDispatcher,
    ArtifactDispatcher,
    BundleDispatcher,
    SkillsDispatcher,
    SubAgentDispatcher,
    TeamsDispatcher,
)

# Every SubDispatcher registered in DaemonServer._init_sub_dispatchers.
# Keep in sync with that list; the point is to catch ANY sync handler
# before it ships, not just teams.*.
_ALL_DISPATCHERS = [
    AgentDispatcher,
    ArtifactDispatcher,
    BundleDispatcher,
    SkillsDispatcher,
    SubAgentDispatcher,
    TeamsDispatcher,
]


@pytest.mark.parametrize("dispatcher_cls", _ALL_DISPATCHERS)
def test_all_handlers_are_coroutine_functions(dispatcher_cls):
    sd = dispatcher_cls(MagicMock())
    sync = [
        name
        for name, fn in sd.get_handlers().items()
        if not inspect.iscoroutinefunction(fn)
    ]
    assert sync == [], (
        "daemon dispatch awaits every handler; these handlers are sync and "
        "will 500 at RPC time: %s" % sync
    )
