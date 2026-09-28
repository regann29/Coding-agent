"""Validate the eval checkers themselves: unsolved fixtures must FAIL, known-good solutions must PASS."""
import pytest
from evals.tasks import TASKS
from evals.run_evals import materialize

BY_ID = {t["id"]: t for t in TASKS}


@pytest.mark.parametrize("tid", [1, 2, 3, 4, 5])
def test_unsolved_fixture_fails(tid):
    t = BY_ID[tid]
    ok, note = t["check"](materialize(t["files"]))
    assert not ok, f"checker for task {tid} passes an untouched fixture: {note}"


def solve(tid, root):
    def sub(f, a, b):
        p = root / f
        assert a in p.read_text(), (f, a)
        p.write_text(p.read_text().replace(a, b))
    if tid == 1:
        sub("mathutils.py", "def clamp(x, lo, hi):\n", 'def clamp(x, lo, hi):\n    """Limit x to the range [lo, hi]."""\n')
    elif tid == 2:
        sub("lists.py", "nums[:n - 1]", "nums[:n]")
    elif tid == 3:
        sub("greet.py", "parser.add_argument('--name', default='world')\n", "parser.add_argument('--name', default='world')\n    parser.add_argument('--verbose', action='store_true')\n")
        sub("greet.py", "print(build_message(args.name))\n", "print(build_message(args.name))\n    if args.verbose:\n        print(f'Greeting generated for {args.name}')\n")
    elif tid == 4:
        (root / "slug.py").write_text("import re\n\n\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n")
        (root / "test_slug.py").write_text(
            "from slug import slugify\n\ndef test_basic():\n    assert slugify('Hello, World!') == 'hello-world'\n\n"
            "def test_runs_collapse():\n    assert slugify('a--b  c') == 'a-b-c'\n\n"
            "def test_strip_and_empty():\n    assert slugify('  x  ') == 'x'\n    assert slugify('!!!') == ''\n    assert slugify('') == ''\n")
    elif tid == 5:
        sub("pricing.py", "(1 - pct)", "(1 - pct / 100)")


@pytest.mark.parametrize("tid", [1, 2, 3, 4, 5])
def test_known_good_solution_passes(tid):
    t = BY_ID[tid]
    root = materialize(t["files"])
    solve(tid, root)
    ok, note = t["check"](root)
    assert ok, note


def test_task5_symptom_patch_is_rejected():
    """Patching cart.py instead of the root cause must NOT pass."""
    t = BY_ID[5]
    root = materialize(t["files"])
    (root / "cart.py").write_text("from pricing import apply_discount\n\n\ndef total(items):\n    return round(sum(apply_discount(i.price, i.discount_pct / 100) * i.qty for i in items), 2)\n")
    ok, note = t["check"](root)
    assert not ok and "contract" in note


def test_task4_weak_tests_are_rejected():
    t = BY_ID[4]
    root = materialize(t["files"])
    solve(4, root)
    (root / "test_slug.py").write_text("from slug import slugify\n\ndef test_a():\n    assert True\n\ndef test_b():\n    assert True\n\ndef test_c():\n    assert slugify('') == ''\n")
    ok, note = t["check"](root)
    assert not ok and "too weak" in note
