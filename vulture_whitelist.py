"""Names vulture reports as unused at 100% confidence but that are used by frameworks.

Attribute access marks a name as used for vulture. The file only has to parse and
type-check: it is never executed, and coverage is measured on guard_agent only.
"""

from typing import Any

_: Any = None

_.Config
_.Field
_.validator
_.root_validator
_.model_config
_.field_validator
_.model_validator
_.fixture
_.mark
_.parametrize
_.raises
_.warns
_.deprecated_call
_.approx
_.skip
_.xfail
_.usefixtures
_.filterwarnings
_.pattern
_.request
_.scenario
_.ctx
_.exc_tb
_.exc_type
_.exc_val
_.__aenter__
_.__aexit__
_.__aiter__
_.__anext__
_.asend
_.athrow
_.aclose
_.__getitem__
_.__setitem__
_.__delitem__
_.__contains__
_.__len__
_.__iter__
_.__next__
_.__reversed__
_.__missing__
_.TypeVar
_.Generic
_.Protocol
_.TypedDict
_.Literal
_.Any
_.NoReturn
_.ClassVar
_.debug
_.info
_.warning
_.error
_.critical
_.exception
_.log
_.mock
_.patch
_.MagicMock
_.AsyncMock
_.PropertyMock
_.call
_.call_args
_.call_args_list
_.called
_.call_count
