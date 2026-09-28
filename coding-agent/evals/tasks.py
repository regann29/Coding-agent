"""The 5 evaluation tasks from the Design Spec: fixture files, prompt, and an automatic checker."""
import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(root, args, timeout=60):
    p = subprocess.run([sys.executable, *args], cwd=root, capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout + p.stderr


def _pytest_ok(root):
    rc, out = _run(root, ["-m", "pytest", "-q", "-p", "no:cacheprovider"])
    return rc == 0, out[-300:]


# ---------------------------------------------------------------- 1: docstring
T1_FILES = {"mathutils.py": "def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n\n\ndef double(x):\n    return x * 2\n"}


def check_1(root):
    src = (root / "mathutils.py").read_text()
    try:
        funcs = {n.name: n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef)}
    except SyntaxError as e:
        return False, f"syntax error: {e}"
    if set(funcs) != {"clamp", "double"}:
        return False, "function set changed"
    if not ast.get_docstring(funcs["clamp"]):
        return False, "clamp has no docstring"
    if ast.get_docstring(funcs["double"]):
        return False, "double was modified (out of scope)"
    rc, out = _run(root, ["-c", "from mathutils import clamp, double; assert clamp(5,0,3)==3 and clamp(-1,0,3)==0 and double(4)==8"])
    return rc == 0, "behaviour preserved" if rc == 0 else out[-200:]


# ---------------------------------------------------------------- 2: off-by-one
T2_TESTS = ("from lists import sum_first_n\n\n"
            "def test_basic():\n    assert sum_first_n([1, 2, 3, 4], 2) == 3\n\n"
            "def test_all():\n    assert sum_first_n([1, 2, 3, 4], 4) == 10\n\n"
            "def test_zero():\n    assert sum_first_n([1, 2, 3], 0) == 0\n\n"
            "def test_n_larger_than_list():\n    assert sum_first_n([1, 2], 5) == 3\n")
T2_FILES = {"lists.py": "def sum_first_n(nums, n):\n    return sum(nums[:n - 1])\n", "test_lists.py": T2_TESTS}


def check_2(root):
    if (root / "test_lists.py").read_text() != T2_TESTS:
        return False, "tests were modified"
    return _pytest_ok(root)


# ---------------------------------------------------------------- 3: CLI flag
T3_TESTS = ("import greet\n\ndef test_message():\n    assert greet.build_message('Ann') == 'Hello, Ann!'\n\n"
            "def test_main(capsys):\n    greet.main(['--name', 'Bo'])\n    assert capsys.readouterr().out.strip() == 'Hello, Bo!'\n")
T3_FILES = {
    "greet.py": ("import argparse\n\n\ndef build_message(name):\n    return f\"Hello, {name}!\"\n\n\n"
                 "def main(argv=None):\n    parser = argparse.ArgumentParser()\n    parser.add_argument('--name', default='world')\n"
                 "    args = parser.parse_args(argv)\n    print(build_message(args.name))\n\n\nif __name__ == '__main__':\n    main()\n"),
    "test_greet.py": T3_TESTS,
}


def check_3(root):
    ok, msg = _pytest_ok(root)
    if not ok:
        return False, "existing tests fail: " + msg
    rc, out = _run(root, ["greet.py", "--name", "Ann", "--verbose"])
    if rc != 0 or "Hello, Ann!" not in out or "Greeting generated for Ann" not in out:
        return False, f"--verbose output wrong: {out[-200:]}"
    rc, out = _run(root, ["greet.py", "--name", "Ann"])
    if "Greeting generated" in out:
        return False, "verbose text printed without the flag"
    return True, "flag works"


# ---------------------------------------------------------------- 4: implement + test
T4_FILES = {"slug.py": "def slugify(text):\n    \"\"\"Convert text to a URL slug.\"\"\"\n    raise NotImplementedError\n"}
HIDDEN_SLUG_CASES = [("Hello, World!", "hello-world"), ("  Multiple   spaces ", "multiple-spaces"), ("a--b", "a-b"),
                     ("", ""), ("!!!", ""), ("Python 3.12", "python-3-12")]
MUTANTS = ["def slugify(text):\n    return text.lower()\n",
           "def slugify(text):\n    return '-'.join(text.lower().split())\n"]


def check_4(root):
    cases = repr(HIDDEN_SLUG_CASES)
    rc, out = _run(root, ["-c", f"from slug import slugify\nfor i,o in {cases}:\n    assert slugify(i)==o, (i, slugify(i), o)"])
    if rc != 0:
        return False, "reference cases fail: " + out[-200:]
    tests = list(root.glob("test_*.py"))
    n = sum(f.read_text().count("def test_") for f in tests)
    if n < 3:
        return False, f"only {n} test functions written (need >= 3)"
    ok, msg = _pytest_ok(root)
    if not ok:
        return False, "agent's own tests fail: " + msg
    for i, mutant in enumerate(MUTANTS):
        tmp = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(root, tmp / "p")
            (tmp / "p" / "slug.py").write_text(mutant)
            if _pytest_ok(tmp / "p")[0]:
                return False, f"tests are too weak: they still pass against broken implementation #{i + 1}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return True, f"{n} tests; reference cases pass; mutants caught"


# ---------------------------------------------------------------- 5: cross-file root cause
T5_FILES = {
    "models.py": "from dataclasses import dataclass\n\n\n@dataclass\nclass Item:\n    name: str\n    price: float\n    qty: int = 1\n    discount_pct: float = 0.0\n",
    "pricing.py": "def apply_discount(price, pct):\n    \"\"\"Return price after discount. pct is a percentage between 0 and 100.\"\"\"\n    return round(price * (1 - pct), 2)\n",
    "cart.py": "from pricing import apply_discount\n\n\ndef total(items):\n    return round(sum(apply_discount(i.price, i.discount_pct) * i.qty for i in items), 2)\n",
}
T5_TESTS = ("from models import Item\nfrom cart import total\n\n"
            "def test_total_with_discount():\n    items = [Item('a', 10.0, 2, discount_pct=10), Item('b', 5.0)]\n    assert total(items) == 23.0\n\n"
            "def test_total_no_discount():\n    assert total([Item('b', 5.0, 3)]) == 15.0\n\n"
            "def test_empty():\n    assert total([]) == 0\n")
T5_FILES["test_cart.py"] = T5_TESTS


def check_5(root):
    if (root / "test_cart.py").read_text() != T5_TESTS:
        return False, "tests were modified"
    ok, msg = _pytest_ok(root)
    if not ok:
        return False, msg
    rc, out = _run(root, ["-c", "from pricing import apply_discount as d; assert d(200,25)==150 and d(80,0)==80 and d(50,100)==0"])
    return rc == 0, "root cause fixed in pricing.py" if rc == 0 else "symptom patched elsewhere; pricing.apply_discount still violates its documented contract"


TASKS = [
    dict(id=1, name="Add a docstring", files=T1_FILES, check=check_1,
         prompt="Add a one-line docstring to the function `clamp` in mathutils.py. Do not change anything else."),
    dict(id=2, name="Fix off-by-one bug", files=T2_FILES, check=check_2,
         prompt="The test suite in this project is failing. Find and fix the bug in the source code. Do not modify the tests."),
    dict(id=3, name="Add --verbose flag", files=T3_FILES, check=check_3,
         prompt="Add a --verbose flag to greet.py. When it is passed, after printing the greeting also print 'Greeting generated for <name>'. Existing behaviour and tests must keep working."),
    dict(id=4, name="Implement + test slugify", files=T4_FILES, check=check_4,
         prompt="Implement slugify(text) in slug.py: lowercase the text, replace every run of non-alphanumeric characters (ASCII letters and digits count as alphanumeric) with a single hyphen, strip leading and trailing hyphens, and return an empty string for empty or all-symbol input. Then write pytest tests for it in test_slug.py covering the main rules."),
    dict(id=5, name="Cross-file root cause", files=T5_FILES, check=check_5,
         prompt="Running the tests shows that cart totals are wrong when items have a discount. Find the root cause and fix it. Do not modify the tests."),
]
