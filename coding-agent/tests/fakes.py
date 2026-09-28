"""A scripted stand-in for the Anthropic client, so the loop can be tested with no API calls."""
import copy
from types import SimpleNamespace as NS


def text(t):
    return NS(type="text", text=t)


def tool(id, name, **inp):
    return NS(type="tool_use", id=id, name=name, input=inp)


def reply(*blocks, stop="tool_use", tokens=(100, 20)):
    return NS(content=list(blocks), stop_reason=stop,
              usage=NS(input_tokens=tokens[0], output_tokens=tokens[1]))


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []                       # snapshot of `messages` at each call
        self.messages = NS(create=self._create)

    def _create(self, **kw):
        self.calls.append(copy.deepcopy(kw["messages"]))
        if not self.replies:
            raise RuntimeError("fake client is out of scripted replies")
        return self.replies.pop(0)
