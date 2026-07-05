"""In-process cache of cursor-sdk Agent handles keyed by session (agent) id."""
from __future__ import annotations

import sys
from collections import OrderedDict
from typing import Optional

from cursor_sdk import Agent, LocalAgentOptions
from cursor_sdk.errors import AgentNotFoundError, NotFoundError

MAX_POOL_SIZE = 5
RESUME_ERRORS = (AgentNotFoundError, NotFoundError)


def is_resume_error(exc: Exception) -> bool:
    return isinstance(exc, RESUME_ERRORS)


class AgentPool:
    def __init__(self, repo_root: str, model: str) -> None:
        self._repo_root = repo_root
        self._model = model
        self._agents: OrderedDict[str, Agent] = OrderedDict()
        self._active_id: Optional[str] = None

    @property
    def active_id(self) -> Optional[str]:
        return self._active_id

    def set_model(self, model: str) -> None:
        self._model = model

    def _local_options(self) -> LocalAgentOptions:
        return LocalAgentOptions(cwd=self._repo_root)

    def create_session(self, model: Optional[str] = None) -> str:
        agent = Agent.create(
            model=model or self._model,
            local=self._local_options(),
        )
        session_id = agent.agent_id
        self._put(session_id, agent)
        self._active_id = session_id
        return session_id

    def set_active(self, session_id: str) -> Agent:
        agent = self.get(session_id)
        self._active_id = session_id
        return agent

    def get(self, session_id: str) -> Agent:
        if session_id in self._agents:
            self._agents.move_to_end(session_id)
            return self._agents[session_id]
        agent = Agent.resume(session_id)
        self._put(session_id, agent)
        return agent

    def drop_session(self, session_id: str) -> None:
        """Remove a session from the pool and clear active if it matches."""
        if self._active_id == session_id:
            self._active_id = None
        agent = self._agents.pop(session_id, None)
        if agent:
            try:
                agent.close()
            except Exception:
                pass

    def warm(self, session_id: str) -> bool:
        """Pre-load active session on bot startup. Returns False if resume failed."""
        try:
            self.set_active(session_id)
            print("SDK agent resumed: %s..." % session_id[:20], file=sys.stderr)
            return True
        except Exception as e:
            self.drop_session(session_id)
            print("Could not resume SDK agent %s: %s" % (session_id[:20], e), file=sys.stderr)
            return False

    def _put(self, session_id: str, agent: Agent) -> None:
        if session_id in self._agents:
            self._agents.move_to_end(session_id)
            self._agents[session_id] = agent
        else:
            self._agents[session_id] = agent
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        while len(self._agents) > MAX_POOL_SIZE:
            evict_sid = None
            for sid in self._agents:
                if sid != self._active_id:
                    evict_sid = sid
                    break
            if evict_sid is None:
                evict_sid = next(iter(self._agents))
            agent = self._agents.pop(evict_sid)
            if evict_sid == self._active_id:
                self._active_id = None
            try:
                agent.close()
            except Exception:
                pass

    def close_all(self) -> None:
        for agent in self._agents.values():
            try:
                agent.close()
            except Exception:
                pass
        self._agents.clear()
