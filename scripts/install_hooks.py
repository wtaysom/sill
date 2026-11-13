"""Render selected-instance hook wiring while preserving unrelated commands."""
import argparse
import copy
import json
import os
from pathlib import Path
import shlex
import tempfile


def render(template, plugin, python, env_file):
    data = json.loads(template.read_text())
    for entries in data["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                prefix = "{{SILL_PYTHON}} {{SILL_PLUGIN_DIR}}/"
                command = hook["command"]
                if not command.startswith(prefix):
                    raise ValueError("Unsupported hook template command")
                hook["command"] = shlex.join(["env", f"SILL_INSTANCE_ENV_FILE={env_file}", python, str(plugin / command[len(prefix):])])
    return data


def managed(command, plugin, names):
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    roots = {plugin.resolve(), Path.home() / ".claude/plugins/local/sill-plugin"}
    return any(Path(word).name in names and any(Path(word) == root / "hooks" / Path(word).name for root in roots) for word in words)


def review_changes(current, rendered, plugin, names):
    """Describe only known template hooks; never serialize operator values.

    Command arguments, environment assignments, matchers, and arbitrary hook
    metadata can all contain credentials. Compare them internally, but show
    only the template's trusted script/event names and the replacement action.
    """
    def records(data, name):
        return [(event, {key: value for key, value in entry.items() if key != "hooks"}, hook)
                for event, entries in data.get("hooks", {}).items()
                for entry in entries for hook in entry.get("hooks", [])
                if managed(hook.get("command", ""), plugin, {name})]

    print("Sill-managed diff (command arguments and setting values omitted):")
    changed = False
    for name in sorted(names):
        before, after = records(current, name), records(rendered, name)
        if before == after:
            continue
        # Event names come solely from our rendered template, never settings.
        events = ", ".join(sorted({event for event, _, _ in after}))
        action = "Refresh" if before else "Add"
        print(f"  {action} {events}: {name} using the current template and selected-instance handoff.")
        changed = True
    if not changed:
        print("  Normalize managed hook order/grouping to the current template.")


def refresh(template, dst, plugin, python, env_file, *, codex=False, dry=False, force=False):
    rendered = render(template, plugin, python, env_file)
    current_text = dst.read_text() if dst.exists() else ""
    current = json.loads(current_text or "{}")
    if current.get("hooks") is False:
        print(f"{dst}: hooks=false; leaving disabled")
        return
    if not isinstance(current.get("hooks", {}), dict):
        raise ValueError(f"Invalid hooks object in {dst}")
    names = {Path(shlex.split(hook["command"])[-1]).name for entries in rendered["hooks"].values() for entry in entries for hook in entry["hooks"]}
    result = copy.deepcopy(current)
    result.setdefault("hooks", {})
    for event, entries in list(result["hooks"].items()):
        kept = []
        for entry in entries:
            other = [h for h in entry.get("hooks", []) if not managed(h.get("command", ""), plugin, names)]
            if other:
                kept.append({**entry, "hooks": other})
            elif not entry.get("hooks"):
                kept.append(entry)
        result["hooks"][event] = kept
    for event, entries in rendered["hooks"].items():
        result["hooks"].setdefault(event, []).extend(entries)
    if current == result:
        print(f"{dst} already up to date; nothing to do")
        return
    output = json.dumps(result, indent=2) + "\n"
    # Existing Codex file needs the historic diff/force gate. For Claude,
    # gate hand-edited Sill entries but update plain legacy commands safely.
    expected = {}
    for event, entries in rendered["hooks"].items():
        for entry in entries:
            for hook in entry["hooks"]:
                expected[Path(shlex.split(hook["command"])[-1]).name] = (event, {k: v for k, v in entry.items() if k != "hooks"}, {k: v for k, v in hook.items() if k != "command"})
    edited = False
    for event, entries in current.get("hooks", {}).items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                command = hook.get("command", "")
                if managed(command, plugin, names):
                    words = shlex.split(command)
                    plain = (len(words) == 2 and words[0] in {python, "python3"}) or (
                        len(words) == 4 and words[:1] == ["env"] and words[1] == "SILL_INSTANCE_ENV_FILE=" + env_file and words[2] == python)
                    name = next(Path(word).name for word in words if Path(word).name in names)
                    shape = (event, {k: v for k, v in entry.items() if k != "hooks"}, {k: v for k, v in hook.items() if k != "command"})
                    if not plain or shape != expected[name]:
                        edited = True
    if current_text:
        print(f"STALE: {dst} differs from the current template")
        review_changes(current, rendered, plugin, names)
    if current_text and (codex or edited) and not force:
        print(f"Leaving {dst} untouched; use --force-hooks to apply the diff")
        return
    if dry:
        print(f"DRY: would refresh {dst}")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".sill-hooks-", dir=dst.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(output)
        if dst.exists():
            os.chmod(temp, dst.stat().st_mode & 0o777)
        os.replace(temp, dst)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)
    print(f"Wrote {dst}")
    if codex:
        print("Codex SHA-256 hook trust changed; approve the refreshed commands on next use.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("template", "destination", "plugin", "python", "env_file"):
        parser.add_argument(name)
    parser.add_argument("--codex", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    refresh(Path(args.template), Path(args.destination), Path(args.plugin), args.python,
            str(Path(args.env_file).resolve()), codex=args.codex, dry=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
