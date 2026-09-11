"""
No handler may reference a name that does not exist.

On 11 September 2026 /add was dead in the client's live group. The UTR step
read `party["id"]` while `party` was not one of its parameters, so aiogram
never injected it and the lookup fell through to the module globals, where
there is no `party` either. Every client who answered the UTR prompt got a
NameError and no reply at all.

Python does not catch this. The name is only resolved when the line runs, and
that line runs exactly once per payment — in production, with money involved.
The unit tests did not catch it either, because nothing exercised /add end to
end after the trade-by-account change moved the account lookup into that step.

A LOAD_GLOBAL for a name that is not a global is, in this codebase, always one
of two mistakes: an aiogram dependency left out of the signature, or a missing
import. Both are fatal at runtime and both are invisible until then. So rather
than write a test per handler, this walks the bytecode of every function in
bot/, core/, db/ and monitor/ and asserts that every global it loads actually
resolves.
"""

import builtins
import dis
import importlib
import inspect
import pkgutil
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PACKAGES = ["bot", "core", "db", "monitor"]


def _modules():
    for package_name in PACKAGES:
        package = importlib.import_module(package_name)
        yield package
        for info in pkgutil.iter_modules(package.__path__):
            yield importlib.import_module(f"{package_name}.{info.name}")


def _code_objects(code):
    """The function's own code plus every nested one — comprehensions,
    closures, lambdas. A NameError hides just as well inside a generator."""
    yield code
    for const in code.co_consts:
        if isinstance(const, types.CodeType):
            yield from _code_objects(const)


def _functions(module):
    for name, obj in vars(module).items():
        if isinstance(obj, types.FunctionType) and obj.__module__ == module.__name__:
            yield name, obj


def _unresolved(func, module) -> list[str]:
    names = set(vars(module)) | set(dir(builtins))
    bad = []
    for code in _code_objects(func.__code__):
        for instruction in dis.get_instructions(code):
            if instruction.opname == "LOAD_GLOBAL":
                # 3.11+ packs a flag into the low bit of the oparg; argval is
                # already the clean name.
                name = instruction.argval
                if name not in names:
                    bad.append(name)
    return bad


@pytest.mark.parametrize(
    "module_name", [m.__name__ for m in _modules()]
)
def test_every_global_resolves(module_name):
    module = importlib.import_module(module_name)
    offenders = []
    for name, func in _functions(module):
        for missing in _unresolved(func, module):
            line = func.__code__.co_firstlineno
            offenders.append(f"{module_name}.{name} (line {line}) uses `{missing}`")

    assert not offenders, (
        "name used but never defined — an aiogram dependency missing from the "
        "signature, or a missing import:\n  " + "\n  ".join(sorted(set(offenders)))
    )


def test_the_add_flow_declares_party():
    """
    The specific regression, named so the failure says what broke rather than
    only that something did.
    """
    from bot import client_bot

    params = inspect.signature(client_bot.add_utr).parameters
    assert "party" in params, (
        "add_utr reads party['id'] — without it in the signature aiogram "
        "injects nothing and /add raises NameError on every payment"
    )
