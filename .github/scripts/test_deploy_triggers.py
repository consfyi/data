#!/usr/bin/env python3
"""Checks that deploy.yml's workflow_run list matches the import workflows.

Imports commit to main with the workflow GITHUB_TOKEN, which never fires
deploy.yml's push trigger, so an import workflow missing from the
workflow_run list imports data that never publishes (CON-27). workflow_run
matches on each workflow's name: field, so this compares names, not file
names. Every workflow whose file name or name: starts with import_ must be
listed by its name:, and every listed name must belong to an import
workflow that exists. A workflow without name: goes by its file path, as on
GitHub, and two workflows sharing a name are reported.

Also runs the check against altered copies of the workflows directory to
prove it catches each kind of drift.

Needs PyYAML: uv run -q --no-project --with pyyaml -- python3 .github/scripts/test_deploy_triggers.py
"""

import pathlib
import shutil
import sys
import tempfile

import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)


def trigger_block(workflow):
    # PyYAML (YAML 1.1) reads the bare key `on` as the boolean True.
    return workflow.get("on", workflow.get(True))


def problems(workflows_dir):
    """Returns a list of mismatches between deploy.yml and the import workflows."""
    out = []
    names = {}
    for path in sorted(workflows_dir.glob("*.y*ml")):
        # GitHub names a workflow after its file path when name: is missing.
        name = load(path).get("name") or ".github/workflows/" + path.name
        if name in names:
            out.append("%s and %s are both named %s" % (names[name], path.name, name))
        names[name] = path.name
    imports = {n for n, f in names.items() if n.startswith("import_") or f.startswith("import_")}
    if not imports:
        return out + ["found no import_* workflows in %s" % workflows_dir]

    deploy = load(workflows_dir / "deploy.yml")
    listed = set(trigger_block(deploy)["workflow_run"]["workflows"])

    for name in sorted(imports - listed):
        out.append("%s (%s) is not in deploy.yml's workflow_run list, so its imports never publish" % (name, names[name]))
    for name in sorted(listed - imports):
        if name in names:
            out.append("deploy.yml's workflow_run lists %s (%s), which is not an import workflow" % (name, names[name]))
        else:
            out.append("deploy.yml's workflow_run lists %s, but no workflow has that name" % name)
    return out


FAILURES = 0


def check(label, ok):
    global FAILURES
    print("%s: %s" % ("PASS" if ok else "FAIL", label))
    if not ok:
        FAILURES += 1


def altered_copy(tmp, edit):
    """Copies the workflows dir into tmp, applies edit(dir), returns the dir."""
    d = pathlib.Path(tmp) / "workflows"
    shutil.copytree(WORKFLOWS, d)
    edit(d)
    return d


def main():
    real = problems(WORKFLOWS)
    for p in real:
        print("  " + p)
    check("deploy.yml's workflow_run list matches the import workflows", not real)

    # An import workflow that deploy doesn't list.
    with tempfile.TemporaryDirectory() as tmp:
        def add_import(d):
            (d / "import_newsource.yml").write_text("name: import_newsource\non:\n  workflow_dispatch:\n")
        found = problems(altered_copy(tmp, add_import))
        check("catches an unlisted import workflow",
              any("import_newsource" in p and "not in" in p for p in found))

    # An import_* file whose name: lacks the prefix is still an import.
    with tempfile.TemporaryDirectory() as tmp:
        def add_unprefixed(d):
            (d / "import_other.yml").write_text("name: othersource\non:\n  workflow_dispatch:\n")
        found = problems(altered_copy(tmp, add_unprefixed))
        check("catches an import_* file whose name: lacks the prefix",
              any(p.startswith("othersource (import_other.yml)") for p in found))

    # A workflow without name: goes by its file path.
    with tempfile.TemporaryDirectory() as tmp:
        def add_nameless(d):
            (d / "import_noname.yml").write_text("on:\n  workflow_dispatch:\n")
        found = problems(altered_copy(tmp, add_nameless))
        check("treats a workflow without name: as named by its path",
              any(p.startswith(".github/workflows/import_noname.yml (import_noname.yml) is not in") for p in found))

    # Two workflows with the same name.
    with tempfile.TemporaryDirectory() as tmp:
        def add_duplicate(d):
            (d / "zz_copy.yml").write_text("name: deploy\non:\n  workflow_dispatch:\n")
        found = problems(altered_copy(tmp, add_duplicate))
        check("reports two workflows with the same name",
              "deploy.yml and zz_copy.yml are both named deploy" in found)

    # A listed workflow that no longer exists (renamed or deleted).
    with tempfile.TemporaryDirectory() as tmp:
        renamed = {}
        def rename_import(d):
            src = sorted(d.glob("import_*.yml"))[0]
            wf = load(src)
            renamed["old"] = wf["name"]
            wf["name"] = "import_renamed"
            (d / "import_renamed.yml").write_text(yaml.safe_dump(wf, sort_keys=False))
            src.unlink()
        found = problems(altered_copy(tmp, rename_import))
        check("catches a listed name with no matching workflow",
              "deploy.yml's workflow_run lists %s, but no workflow has that name" % renamed["old"] in found)

    # A listed workflow that exists but isn't an import.
    with tempfile.TemporaryDirectory() as tmp:
        def list_validate(d):
            wf = load(d / "deploy.yml")
            trigger_block(wf)["workflow_run"]["workflows"].append("validate")
            (d / "deploy.yml").write_text(yaml.safe_dump(wf, sort_keys=False))
        found = problems(altered_copy(tmp, list_validate))
        check("catches a listed workflow that is not an import",
              "deploy.yml's workflow_run lists validate (validate.yml), which is not an import workflow" in found)

    # No import workflows at all must not pass silently.
    with tempfile.TemporaryDirectory() as tmp:
        def drop_imports(d):
            for p in d.glob("import_*.yml"):
                p.unlink()
        found = problems(altered_copy(tmp, drop_imports))
        check("fails when it finds no import workflows",
              any("found no import_* workflows" in p for p in found))

    if FAILURES:
        print("%d check(s) failed" % FAILURES)
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
