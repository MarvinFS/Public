"""Both log parsers read a transcript once and, on the next refresh, only what it gained.

The logs are append-only, and a refresh used to parse every one of them again (1.7 GB of
Claude Code transcripts and 700 MB of Codex sessions on the machine this was measured on).
What must not change is the answer: after any mix of appended lines, half-written lines,
rewrites, replacements, deletions and new files, the incremental result is the one a full
parse of the same directory returns.
"""

import builtins
import json
import os
import random
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import codex_log_parser as cparser
import codex_pricing
import log_parser as lparser

NOW = datetime(2026, 11, 12, 12, 0)                  # the Codex series ends on this day
BASE_MTIME_NS = int(datetime(2026, 11, 12, 13, 0).timestamp() * 1e9)


# --------------------------------------------------------------------------- helpers

class OpenedFiles:
    """The .jsonl files opened while a spy is installed, and the JSON lines parsed."""

    def __init__(self, monkeypatch):
        self.paths = []
        self.parsed = 0
        real_open, real_loads = builtins.open, json.loads

        def spy_open(file, *args, **kwargs):
            if str(file).endswith(".jsonl"):
                self.paths.append(Path(file).name)
            return real_open(file, *args, **kwargs)

        def spy_loads(text, *args, **kwargs):
            self.parsed += 1
            return real_loads(text, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", spy_open)
        monkeypatch.setattr(json, "loads", spy_loads)

    def clear(self):
        self.paths.clear()
        self.parsed = 0


def stamp(path, step):
    """Move the file's mtime to a value no earlier step used, as a real write does."""
    ns = BASE_MTIME_NS + step * 1_000_000_000
    os.utime(path, ns=(ns, ns))


class Tree:
    """A directory of append-only files that a test edits, one step at a time."""

    def __init__(self, root, subdir, make_line):
        self.root = root
        self.subdir = subdir               # callable(index) -> relative directory
        self.make_line = make_line         # callable() -> one JSON line, no newline
        self.files = {}                    # path -> pending ("partial", rest) / ("bare",) / None
        self.step = 0
        self.serial = 0

    def _touch(self, path):
        self.step += 1
        stamp(path, self.step)

    def add_file(self, lines=3):
        self.serial += 1
        directory = self.root / self.subdir(self.serial)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"rollout-{self.serial}.jsonl"
        path.write_bytes(b"".join(self.make_line().encode() + b"\n" for _ in range(lines)))
        self.files[path] = None
        self._touch(path)
        return path

    def _finish(self, path):
        """Complete a half-written or unterminated last line, as the writer eventually does."""
        pending = self.files[path]
        if pending is None:
            return
        with open(path, "ab") as f:
            f.write((pending[1] if pending[0] == "partial" else b"") + b"\n")
        self.files[path] = None

    def append(self, path, lines=2):
        self._finish(path)
        with open(path, "ab") as f:
            f.write(b"".join(self.make_line().encode() + b"\n" for _ in range(lines)))
        self._touch(path)

    def partial(self, path, rng):
        self._finish(path)
        line = self.make_line().encode()
        cut = rng.randrange(1, len(line))
        with open(path, "ab") as f:
            f.write(line[:cut])
        self.files[path] = ("partial", line[cut:])
        self._touch(path)

    def finish(self, path):
        self._finish(path)
        self._touch(path)

    def bare(self, path):
        """A complete last line whose newline has not been written yet."""
        self._finish(path)
        with open(path, "ab") as f:
            f.write(self.make_line().encode())
        self.files[path] = ("bare",)
        self._touch(path)

    def rewrite_shorter(self, path):
        path.write_bytes(self.make_line().encode() + b"\n")
        self.files[path] = None
        self._touch(path)

    def replace_longer(self, path):
        replacement = path.with_name(path.name + ".new")
        replacement.write_bytes(b"".join(self.make_line().encode() + b"\n" for _ in range(40)))
        os.replace(replacement, path)
        self.files[path] = None
        self._touch(path)

    def delete(self, path):
        path.unlink()
        del self.files[path]

    def touch(self, path):
        self._touch(path)

    def copy_to(self, destination):
        shutil.copytree(self.root, destination)
        return destination


def random_step(rng, tree):
    """One random edit of the tree; returns its name for the failure message."""
    files = sorted(tree.files)
    if not files:
        tree.add_file()
        return "add_file"
    path = rng.choice(files)
    op = rng.choice(["append", "append", "partial", "finish", "bare", "rewrite_shorter",
                     "replace_longer", "delete", "add_file", "touch"])
    if op == "append":
        tree.append(path, rng.randint(1, 4))
    elif op == "partial":
        tree.partial(path, rng)
    elif op == "finish":
        tree.finish(path)
    elif op == "bare":
        tree.bare(path)
    elif op == "rewrite_shorter":
        tree.rewrite_shorter(path)
    elif op == "replace_longer":
        tree.replace_longer(path)
    elif op == "delete":
        tree.delete(path)
    elif op == "add_file":
        tree.add_file(rng.randint(0, 5))
    else:
        tree.touch(path)
    return op


# --------------------------------------------------------------------------- Claude

class ClaudeLines:
    """Assistant lines over a small pool of API calls, so keys repeat inside a file and across
    files, with output tokens that make some claims better than others and some equal."""

    def __init__(self, rng):
        self.rng = rng
        self.n = 0

    def __call__(self):
        self.n += 1
        rng = self.rng
        kind = rng.random()
        if kind < 0.12:
            return json.dumps({"type": "user", "message": {"content": "hello " * rng.randint(1, 5)}})
        if kind < 0.17:
            return "{not json at all"
        if kind < 0.2:
            return '["assistant"]'                          # JSON, but not a record
        stamp_ = datetime(2026, 11, 1, tzinfo=timezone.utc) + timedelta(seconds=self.n * 37)
        return json.dumps({
            "type": "assistant", "timestamp": stamp_.isoformat().replace("+00:00", "Z"),
            "requestId": f"req_{rng.randint(1, 12)}", "uuid": f"u{self.n}",
            "message": {"id": f"msg_{rng.randint(1, 12)}", "model": rng.choice(["claude-opus-5", "claude-sonnet-5"]),
                        "usage": {"input_tokens": rng.randint(1, 50), "output_tokens": rng.choice([1, 5, 5, 40, 900]),
                                  "cache_read_input_tokens": rng.randint(0, 500),
                                  "cache_creation_input_tokens": rng.randint(0, 80)}},
        })


def claude_reference(projects):
    """What collect_entries always meant: every file in directory order, the claim with the most
    output tokens wins and the later line among equals. Built from the uncached parse of one file."""
    latest = {}
    for path in lparser.find_jsonl_files(projects):
        for rows, timestamp, key in lparser.parse_jsonl_file(path):
            held = latest.get(key)
            if held is None or lparser._output_tokens(rows) >= lparser._output_tokens(held[0]):
                latest[key] = (rows, timestamp)
    return latest


def assert_same_claude(got, expected, note):
    assert list(got.items()) == list(expected.items()), note


@pytest.mark.parametrize("seed", range(8))
def test_claude_incremental_result_equals_a_full_parse_after_every_step(tmp_path, seed):
    rng = random.Random(seed)
    tree = Tree(tmp_path / "projects", lambda i: f"proj-{i % 3}", ClaudeLines(rng))
    for _ in range(4):
        tree.add_file(rng.randint(0, 6))
    done = []
    for step in range(70):
        done.append(random_step(rng, tree))
        note = f"seed {seed}, step {step}, after {done[-6:]}"
        got = lparser.collect_entries(tree.root)
        assert_same_claude(got, claude_reference(tree.root), note)
        if step % 5 == 4:
            # A cold parse of a copy: a different set of paths, so it cannot lean on this cache.
            fresh = lparser.collect_entries(tree.copy_to(tmp_path / f"copy-{step}"))
            assert_same_claude(got, fresh, note + " (cold parse)")
    lparser.reset_cache()
    assert_same_claude(lparser.collect_entries(tree.root), claude_reference(tree.root), "after reset_cache")


def claude_file(tmp_path, n_lines, name="a.jsonl", seed=1):
    path = tmp_path / "proj" / name
    path.parent.mkdir(exist_ok=True)
    lines = ClaudeLines(random.Random(seed))
    path.write_text("\n".join(lines() for _ in range(n_lines)) + "\n", encoding="utf-8")
    return path, lines


def test_an_unchanged_claude_file_is_not_opened_again(tmp_path, monkeypatch):
    claude_file(tmp_path, 30)
    lparser.collect_entries(tmp_path)
    seen = OpenedFiles(monkeypatch)
    lparser.collect_entries(tmp_path)
    assert seen.paths == [] and seen.parsed == 0


def test_a_grown_claude_file_costs_only_its_new_lines(tmp_path, monkeypatch):
    path, lines = claude_file(tmp_path, 200)
    lparser.collect_entries(tmp_path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(lines() + "\n" + lines() + "\n")
    seen = OpenedFiles(monkeypatch)
    lparser.collect_entries(tmp_path)
    assert seen.paths == ["a.jsonl"]
    assert seen.parsed <= 2                  # the two new lines, not the 200 before them


def test_a_claude_line_still_being_written_waits_for_its_newline(tmp_path):
    path, _ = claude_file(tmp_path, 3)
    before = lparser.collect_entries(tmp_path)
    assistant = json.dumps({"type": "assistant", "timestamp": "2026-11-02T10:00:00Z", "requestId": "r",
                            "message": {"id": "m", "model": "claude-opus-5",
                                        "usage": {"output_tokens": 7}}})
    with open(path, "a", encoding="utf-8") as f:
        f.write(assistant[:40])                                   # the writer is half way through
    assert lparser.collect_entries(tmp_path) == before
    with open(path, "a", encoding="utf-8") as f:
        f.write(assistant[40:] + "\n")
    after = lparser.collect_entries(tmp_path)
    assert ("m", "r") in after and ("m", "r") not in before


def test_a_complete_claude_last_line_without_a_newline_counts_at_once(tmp_path):
    """A full parse always counted it, so the incremental one must too."""
    path, _ = claude_file(tmp_path, 1)
    assistant = json.dumps({"type": "assistant", "timestamp": "2026-11-02T10:00:00Z", "requestId": "r",
                            "message": {"id": "m", "model": "claude-opus-5",
                                        "usage": {"output_tokens": 7}}})
    with open(path, "a", encoding="utf-8") as f:
        f.write(assistant)
    assert ("m", "r") in lparser.collect_entries(tmp_path)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n")                                              # the newline arrives later
    assert [k for k in lparser.collect_entries(tmp_path)].count(("m", "r")) == 1


def test_a_claude_file_cut_in_the_middle_of_a_multibyte_character(tmp_path):
    path, _ = claude_file(tmp_path, 1)
    record = json.dumps({"type": "assistant", "timestamp": "2026-11-02T10:00:00Z", "requestId": "r",
                         "message": {"id": "m", "model": "claude-opus-5", "usage": {"output_tokens": 7}},
                         "note": "café"}, ensure_ascii=False).encode("utf-8")
    cut = record.index("é".encode("utf-8")) + 1                # between the two bytes of the e-acute
    with open(path, "ab") as f:
        f.write(record[:cut])
    lparser.collect_entries(tmp_path)
    with open(path, "ab") as f:
        f.write(record[cut:] + b"\n")
    assert ("m", "r") in lparser.collect_entries(tmp_path)


def test_a_claude_file_that_shrank_is_read_again_from_the_start(tmp_path):
    path, lines = claude_file(tmp_path, 40)
    lparser.collect_entries(tmp_path)
    path.write_text(lines() + "\n", encoding="utf-8")
    stamp(path, 5)
    assert_same_claude(lparser.collect_entries(tmp_path), claude_reference(tmp_path), "shrunk")


def test_a_claude_file_replaced_by_a_longer_one_is_read_again_from_the_start(tmp_path):
    path, lines = claude_file(tmp_path, 5)
    lparser.collect_entries(tmp_path)
    other = ClaudeLines(random.Random(99))
    path.write_text("\n".join(other() for _ in range(60)) + "\n", encoding="utf-8")
    stamp(path, 6)
    assert_same_claude(lparser.collect_entries(tmp_path), claude_reference(tmp_path), "replaced")


def test_a_deleted_claude_file_stops_counting(tmp_path):
    a, _ = claude_file(tmp_path, 10, "a.jsonl")
    b, _ = claude_file(tmp_path, 10, "b.jsonl", seed=2)
    both = lparser.collect_entries(tmp_path)
    b.unlink()
    only_a = lparser.collect_entries(tmp_path)
    assert len(only_a) < len(both)
    assert_same_claude(only_a, claude_reference(tmp_path), "deleted")


def test_what_claude_collect_entries_returns_is_the_callers_to_change(tmp_path):
    claude_file(tmp_path, 10)
    first = lparser.collect_entries(tmp_path)
    expected = dict(first)
    first.clear()
    assert lparser.collect_entries(tmp_path) == expected


def test_reset_cache_makes_the_next_collect_read_everything_again(tmp_path, monkeypatch):
    claude_file(tmp_path, 10)
    lparser.collect_entries(tmp_path)
    lparser.reset_cache()
    seen = OpenedFiles(monkeypatch)
    lparser.collect_entries(tmp_path)
    assert seen.paths == ["a.jsonl"]


def test_a_line_that_is_json_but_not_a_record_does_not_break_the_parse(tmp_path):
    path = tmp_path / "proj" / "a.jsonl"
    path.parent.mkdir()
    path.write_text('["assistant"]\n"assistant"\n', encoding="utf-8")
    assert lparser.collect_entries(tmp_path) == {}


# --------------------------------------------------------------------------- Codex

class CodexLines:
    """session_meta, turn_context and token_count lines with timestamps on the days of the
    series (and some with none, which fall back to the file's own day)."""

    def __init__(self, rng):
        self.rng = rng

    def __call__(self):
        rng = self.rng
        kind = rng.random()
        if kind < 0.08:
            return json.dumps({"type": "session_meta",
                               "payload": {"model_provider": rng.choice(["openai", "ollama"])}})
        if kind < 0.2:
            return json.dumps({"type": "turn_context",
                               "payload": {"model": rng.choice(["gpt-6-astra", "gpt-5.6-luna", "gpt-5.5"])}})
        if kind < 0.25:
            return "{broken"
        if kind < 0.3:
            return json.dumps({"type": "response_item", "payload": {"text": "token_count"}})
        entry = {"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {
            "input_tokens": rng.randint(1, 300_000), "cached_input_tokens": rng.randint(0, 1000),
            "output_tokens": rng.randint(0, 5000), "reasoning_output_tokens": rng.randint(0, 100)}}}}
        if rng.random() < 0.9:
            moment = datetime(2026, 11, 9, 12, tzinfo=timezone.utc) + timedelta(hours=rng.randint(0, 96))
            entry["timestamp"] = moment.isoformat().replace("+00:00", "Z")
        return json.dumps(entry)


def codex_series(root):
    return cparser.get_codex_daily_usage(root, days=4, end=NOW)


def codex_reference(root):
    """The series as get_codex_daily_usage built it before it kept anything: every request of
    every file, added in file order. Built from the uncached per-file generator."""
    last_day = NOW.date()
    first_day = last_day - timedelta(days=3)
    series = {first_day + timedelta(days=i): cparser.CodexTokenUsage() for i in range(4)}
    window_start = datetime(first_day.year, first_day.month, first_day.day).timestamp()
    for path in root.glob("*/*/*/*.jsonl"):
        file_day = date(*(int(part) for part in path.parts[-4:-1]))
        if file_day < first_day and path.stat().st_mtime < window_start:
            continue
        for timestamp, turn in cparser._turns(path):
            day = cparser._local_day(timestamp, file_day)
            if day in series:
                series[day] = series[day] + turn
    return list(series.values())


def assert_same_codex(got, expected, note, exact):
    for g, e in zip(got, expected):
        assert (g.input_tokens, g.cached_input_tokens, g.output_tokens, g.reasoning_tokens) == \
               (e.input_tokens, e.cached_input_tokens, e.output_tokens, e.reasoning_tokens), note
        if exact:
            assert g.cost_usd == e.cost_usd, note
        else:
            assert g.cost_usd == pytest.approx(e.cost_usd, rel=1e-9, abs=1e-12), note
    assert len(got) == len(expected), note


@pytest.mark.parametrize("seed", range(8))
def test_codex_incremental_series_equals_a_full_parse_after_every_step(tmp_path, seed):
    rng = random.Random(seed)
    tree = Tree(tmp_path / "sessions", lambda i: f"2026/11/{10 + i % 3}", CodexLines(rng))
    for _ in range(4):
        tree.add_file(rng.randint(0, 6))
    done = []
    for step in range(70):
        done.append(random_step(rng, tree))
        note = f"seed {seed}, step {step}, after {done[-6:]}"
        got = codex_series(tree.root)
        assert_same_codex(got, codex_reference(tree.root), note, exact=False)
        if step % 5 == 4:
            fresh = codex_series(tree.copy_to(tmp_path / f"copy-{step}"))
            assert_same_codex(got, fresh, note + " (cold parse)", exact=True)
    cparser.reset_cache()
    assert_same_codex(codex_series(tree.root), codex_reference(tree.root), "after reset_cache", exact=False)


def codex_file(tmp_path, lines, name="rollout-x.jsonl", day="2026/11/12"):
    path = tmp_path / day / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    stamp(path, 1)
    return path


def turn(inp, out=10, at="2026-11-12T10:00:00Z"):
    return {"type": "event_msg", "timestamp": at, "payload": {"type": "token_count", "info": {
        "last_token_usage": {"input_tokens": inp, "cached_input_tokens": 0, "output_tokens": out,
                             "reasoning_output_tokens": 0}}}}


def test_an_unchanged_codex_file_is_not_opened_again(tmp_path, monkeypatch):
    codex_file(tmp_path, [turn(100)] * 30)
    codex_series(tmp_path)
    seen = OpenedFiles(monkeypatch)
    codex_series(tmp_path)
    assert seen.paths == [] and seen.parsed == 0


def test_a_grown_codex_file_costs_only_its_new_lines(tmp_path, monkeypatch):
    path = codex_file(tmp_path, [turn(100)] * 200)
    codex_series(tmp_path)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(turn(5)) + "\n" + json.dumps(turn(6)) + "\n")
    stamp(path, 2)
    seen = OpenedFiles(monkeypatch)
    series = codex_series(tmp_path)
    assert seen.paths == ["rollout-x.jsonl"] and seen.parsed <= 2
    assert series[-1].input_tokens == 200 * 100 + 11


def test_a_resumed_codex_parse_keeps_the_model_and_the_provider_of_the_lines_before_it(tmp_path):
    """Neither is written again with every turn: a turn appended later is priced at the model
    in force at the end of the part already read, and costs nothing on a provider with no price."""
    def model(name):
        return {"type": "turn_context", "payload": {"model": name}}
    meta = lambda provider: {"type": "session_meta", "payload": {"model_provider": provider}}

    priced = codex_file(tmp_path, [meta("openai"), model("gpt-5.6-luna"), turn(100_000)], name="a.jsonl")
    free = codex_file(tmp_path, [meta("ollama"), model("glm-5"), turn(100_000)], name="b.jsonl")
    codex_series(tmp_path)
    for path in (priced, free):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(turn(100_000)) + "\n")
        stamp(path, 2)

    series = codex_series(tmp_path)
    expected = 2 * codex_pricing.calculate_cost("gpt-5.6-luna", 100_000, 10)
    assert series[-1].input_tokens == 400_000
    assert series[-1].cost_usd == pytest.approx(expected)


def test_a_codex_turn_still_being_written_waits_for_its_newline(tmp_path):
    path = codex_file(tmp_path, [turn(100)])
    line = json.dumps(turn(7))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line[:30])
    assert codex_series(tmp_path)[-1].input_tokens == 100
    with open(path, "a", encoding="utf-8") as f:
        f.write(line[30:] + "\n")
    stamp(path, 3)
    assert codex_series(tmp_path)[-1].input_tokens == 107


def test_a_changed_price_table_is_not_served_from_the_old_costs(tmp_path, monkeypatch):
    """The cost of a turn is worked out when it is read, at the prices in force then. models.dev
    changes the table while the app runs, so a price change has to make the next read start over."""
    codex_file(tmp_path, [{"type": "turn_context", "payload": {"model": "gpt-5.5"}}, turn(100_000, 0)])
    before = codex_series(tmp_path)[-1].cost_usd
    table = dict(codex_pricing.CODEX_PRICING)
    table["gpt-5.5"] = dict(table["gpt-5.5"], input=table["gpt-5.5"]["input"] * 2)
    monkeypatch.setattr(codex_pricing, "CODEX_PRICING", table)
    assert codex_series(tmp_path)[-1].cost_usd == pytest.approx(before * 2)


def test_a_codex_file_that_shrank_or_was_replaced_is_read_again(tmp_path):
    path = codex_file(tmp_path, [turn(100)] * 20)
    codex_series(tmp_path)
    path.write_text(json.dumps(turn(3)) + "\n", encoding="utf-8")
    stamp(path, 2)
    assert codex_series(tmp_path)[-1].input_tokens == 3
    path.write_text("".join(json.dumps(turn(9)) + "\n" for _ in range(50)), encoding="utf-8")
    stamp(path, 3)
    assert codex_series(tmp_path)[-1].input_tokens == 450


def test_a_deleted_codex_file_stops_counting(tmp_path):
    a = codex_file(tmp_path, [turn(100)], name="a.jsonl")
    codex_file(tmp_path, [turn(10)], name="b.jsonl")
    assert codex_series(tmp_path)[-1].input_tokens == 110
    a.unlink()
    assert codex_series(tmp_path)[-1].input_tokens == 10


def test_an_old_codex_file_outside_the_window_is_still_not_opened(tmp_path, monkeypatch):
    """The skip that was already there: dated before the window and not written to since."""
    path = codex_file(tmp_path, [turn(100)], name="old.jsonl", day="2026/09/01")
    ancient = int(datetime(2026, 9, 1).timestamp() * 1e9)
    os.utime(path, ns=(ancient, ancient))
    seen = OpenedFiles(monkeypatch)
    codex_series(tmp_path)
    assert seen.paths == []


def test_codex_reset_cache_makes_the_next_series_read_everything_again(tmp_path, monkeypatch):
    codex_file(tmp_path, [turn(100)])
    codex_series(tmp_path)
    cparser.reset_cache()
    seen = OpenedFiles(monkeypatch)
    codex_series(tmp_path)
    assert seen.paths == ["rollout-x.jsonl"]


# --------------------------------------------------------------- a line finished mid-read

class CatchingUpFile:
    """A real file whose writer finishes the line it was half way through between two reads of
    the reader. A buffered binary file has no sticky end of file, so the reader's next read
    returns what the writer added: the rest of that line, then whatever followed it."""

    def __init__(self, f, finish):
        self._f, self._finish = f, finish

    def __enter__(self):
        self._f.__enter__()
        return self

    def __exit__(self, *exc):
        return self._f.__exit__(*exc)

    def seek(self, *args):
        return self._f.seek(*args)

    def read(self, *args):
        return self._f.read(*args)

    def __iter__(self):
        for raw in iter(self._f.readline, b""):
            yield raw
            if not raw.endswith(b"\n"):
                self._finish()          # runs only if the reader asks for another line


class LateWriter:
    """What the writer appends to `path` to finish its last line and add more, once."""

    def __init__(self, path, rest):
        self.path, self.rest, self.done = path, rest, False

    def finish(self):
        if not self.done:
            self.done = True
            with open(self.path, "ab") as f:
                f.write(self.rest)

    def install(self, monkeypatch):
        import jsonl_tail
        real_open = open

        def catching_open(file, mode="r", *args, **kwargs):
            f = real_open(file, mode, *args, **kwargs)
            return CatchingUpFile(f, self.finish) if Path(file) == self.path and mode == "rb" else f

        monkeypatch.setattr(jsonl_tail, "open", catching_open, raising=False)


def split_last_line(complete, last, cut):
    """The bytes a file holds when the writer is `cut` bytes into `last`, and the bytes it adds."""
    line = last.encode()
    return (b"".join(c.encode() + b"\n" for c in complete) + line[:cut], line[cut:] + b"\n")


def test_a_line_finished_between_two_reads_is_read_once_and_whole(tmp_path, monkeypatch):
    """Nothing after an unterminated chunk belongs to the same read: the rest of the line, as a
    second chunk, was taken for a line of its own (not JSON, so lost) and moved the offset by its
    length alone, into the middle of the real line."""
    import jsonl_tail

    class Recording(jsonl_tail.TailedFile):
        def reset(self):
            self.lines = []

        def add_line(self, line):
            self.lines.append(line)

        def peek_line(self, line):
            return line

    path = tmp_path / "log.jsonl"
    before, rest = split_last_line(['{"n": 1}', '{"n": 2}'], '{"n": 3, "pad": "xxxxxxxxxx"}', 15)
    after = b'{"n": 4}\n{"n": 5}\n'
    path.write_bytes(before)
    writer = LateWriter(path, rest + after)
    writer.install(monkeypatch)

    tailed = Recording()
    tailed.refresh(path)
    writer.finish()                                  # a no-op when the read already saw it
    tailed.refresh(path)

    assert tailed.lines == [f'{{"n": {n}' + (', "pad": "xxxxxxxxxx"' if n == 3 else "") + "}\n"
                            for n in (1, 2, 3, 4, 5)]
    assert tailed.offset == path.stat().st_size


def test_claude_line_finished_between_two_reads_gives_what_a_full_parse_gives(tmp_path, monkeypatch):
    lines = ClaudeLines(random.Random(3))
    complete = [lines() for _ in range(2)]
    last = json.dumps({"type": "assistant", "timestamp": "2026-11-02T10:00:00Z", "requestId": "r1",
                       "message": {"id": "m1", "model": "claude-opus-5", "usage": {"output_tokens": 7}},
                       "pad": "x" * 400})                  # longer than the lines that follow it
    tail = [json.dumps({"type": "assistant", "timestamp": "2026-11-02T10:01:00Z", "requestId": f"r{n}",
                        "message": {"id": f"m{n}", "model": "claude-opus-5", "usage": {"output_tokens": 9}}})
            for n in (2, 3)]
    path = tmp_path / "proj" / "a.jsonl"
    path.parent.mkdir()
    before, rest = split_last_line(complete, last, 300)
    path.write_bytes(before)
    writer = LateWriter(path, rest + b"".join(t.encode() + b"\n" for t in tail))
    writer.install(monkeypatch)

    lparser.collect_entries(tmp_path)
    writer.finish()
    stamp(path, 9)
    got = lparser.collect_entries(tmp_path)

    assert ("m1", "r1") in got, "the line the writer finished was lost"
    assert_same_claude(got, claude_reference(tmp_path), "after the writer caught up")


def test_codex_line_finished_between_two_reads_is_not_counted_twice(tmp_path, monkeypatch):
    """Codex sums its turns, so a line read again after the offset slipped back is added again."""
    path = codex_file(tmp_path, [turn(100), turn(200)])
    last = json.dumps({**turn(7), "pad": "x" * 400})       # longer than the lines that follow it
    tail = [json.dumps(turn(n)) for n in (30, 40)]
    path.write_bytes(path.read_bytes() + last.encode()[:300])
    writer = LateWriter(path, last.encode()[300:] + b"\n" + b"".join(t.encode() + b"\n" for t in tail))
    writer.install(monkeypatch)

    codex_series(tmp_path)
    writer.finish()
    stamp(path, 9)
    series = codex_series(tmp_path)

    assert series[-1].input_tokens == 100 + 200 + 7 + 30 + 40
    assert_same_codex(series, codex_reference(tmp_path), "after the writer caught up", exact=False)
