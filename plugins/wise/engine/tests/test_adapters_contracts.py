import importlib
import json
import re
from pathlib import Path

import pytest

from wise_engine.spawn import SpawnExit


def expand(value):
    if isinstance(value, dict):
        if "$fixture" in value:
            text = (
                Path(__file__).parents[1] / "test/fixtures/adapters" / value["$fixture"]
            ).read_text()
            if "line" in value:
                text = text.splitlines()[value["line"]]
            return json.loads(text) if value.get("parsed") else text
        if "$repeat" in value:
            return value["$repeat"][0] * value["$repeat"][1]
        if "$chunks" in value:
            return "".join(expand(chunk) for chunk in value["$chunks"])
        if "$undefined" in value:
            return None
        return {key: expand(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item) for item in value]
    return value


def snake(name):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


CASES = json.loads((Path(__file__).parent / "fixtures/adapters/contracts.json").read_text())


@pytest.mark.parametrize(
    "case", CASES, ids=lambda case: f"{case['harness']}:{case['test']}:{case['fn']}"
)
def test_captured_typescript_adapter_contract(case):
    case = expand(case)
    module = importlib.import_module("wise_engine.adapters." + case["harness"])
    if case["fn"] == "parser":
        opts = {snake(key): value for key, value in case["opts"].items()}
        parser = module.create_stream_parser(**opts, now=lambda: "T")
        for call in case["calls"]:
            args = call["args"]
            if call["method"] == "finish":
                args = [SpawnExit(**{snake(key): value for key, value in args[0].items()})]
            assert getattr(parser, call["method"])(*args) == call["result"]
        return
    fn = getattr(module, snake(case["fn"]))
    args = case["args"]
    kwargs = {}
    if case["fn"] == "buildArgv" and len(args) > 1:
        kwargs = {snake(key): value for key, value in args.pop().items()}
    if "error" in case:
        with pytest.raises(ValueError, match=re.escape(case["error"])):
            fn(*args, **kwargs)
    else:
        assert fn(*args, **kwargs) == case["result"]
