#!/usr/bin/env python3
"""Read-only Codex log dashboard. Python 3.9+, no external dependencies."""
import argparse
import collections
import datetime as dt
import json
import pathlib
import re
import sqlite3
import statistics
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parent
UUID = re.compile(r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")
UUID_ANY = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
TURN_ID = re.compile(r'turn\.id="?([0-9a-f-]{36})')
ITEM = re.compile(r'item_type="([^"]+)" item_id="([^"]+)"')


def connect(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=3)


def timestamp(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def rollout_metadata(path):
    """Return segment ID, owning chat ID, and the stored history pointer."""
    identifiers = UUID_ANY.findall(path.name)
    segment_id = identifiers[-1] if identifiers else None
    try:
        with path.open(encoding="utf-8") as source:
            payload = json.loads(next(source)).get("payload", {})
    except (OSError, StopIteration, ValueError, TypeError):
        payload = {}
    return segment_id, payload.get("id"), payload.get("history_base")


def rollout_chain(paths, primary):
    """Follow the visible history chain and apply each child segment's parent cutoff."""
    segments = {}
    for path in paths:
        segment_id, _, history_base = rollout_metadata(path)
        if segment_id:
            segments[segment_id] = (path, history_base)
    current, _, _ = rollout_metadata(primary)
    chain = []
    limit = None
    seen = set()
    while current and current not in seen and current in segments:
        seen.add(current)
        path, history_base = segments[current]
        chain.append((path, limit))
        if not history_base:
            break
        limit = history_base.get("end_ordinal_exclusive")
        current = history_base.get("thread_id")
    return list(reversed(chain)) if chain else [(path, None) for path in sorted(paths)]


def parse_rollout(path, end_ordinal_exclusive=None):
    """Parse turns by ID, preferring per-response usage records over legacy counters."""
    turns = {}
    order = []
    current_id = None
    cumulative = None
    origin_thread_id = None
    with path.open(encoding="utf-8") as source:
        for line in source:
            try:
                data = json.loads(line)
                ordinal = data.get("ordinal")
                if (end_ordinal_exclusive is not None and ordinal is not None
                        and ordinal >= end_ordinal_exclusive):
                    continue
                payload = data.get("payload", {})
                event = payload.get("type")
                stamp = timestamp(data["timestamp"])
            except (ValueError, KeyError, TypeError):
                continue
            if data.get("type") == "session_meta":
                origin_thread_id = payload.get("id", origin_thread_id)
            elif data.get("type") == "event_msg":
                if event == "task_started":
                    current_id = payload["turn_id"]
                    current = turns.setdefault(current_id, {
                        # Payload started_at/completed_at are integer seconds. Keep the
                        # JSONL event timestamp for sub-second request/item boundaries.
                        "turn_id": current_id, "origin_thread_id": origin_thread_id,
                        "start": stamp, "end": None,
                        "duration_s": None, "first_visible_delay_s": None,
                        "outcome": None, "usage": [], "_usage_records": {},
                        "_fallback_usage": [], "model": None, "effort": None})
                    if current_id not in order:
                        order.append(current_id)
                elif event == "token_count" and payload.get("info"):
                    info = payload["info"]
                    total = info.get("total_token_usage", {}).get("output_tokens")
                    usage = info.get("last_token_usage")
                    current = turns.get(current_id)
                    if usage and total is not None and total != cumulative and current:
                        current["_fallback_usage"].append(dict(usage))
                    if total is not None:
                        cumulative = total
                elif event in ("task_complete", "turn_aborted"):
                    turn_id = payload.get("turn_id", current_id)
                    current = turns.get(turn_id)
                    if current:
                        current["end"] = stamp
                        duration = payload.get("duration_ms")
                        current["duration_s"] = (duration / 1000
                                                   if isinstance(duration, (int, float)) else None)
                        delay = payload.get("time_to_first_token_ms")
                        current["first_visible_delay_s"] = (delay / 1000
                                if isinstance(delay, (int, float)) else None)
                        current["outcome"] = "completed" if event == "task_complete" else "aborted"
            elif data.get("type") == "token_usage_record":
                turn_id = payload.get("turn_id")
                current = turns.get(turn_id)
                usage = payload.get("usage")
                response_id = payload.get("response_id")
                if current and usage and response_id:
                    current["_usage_records"][response_id] = dict(usage)
            elif data.get("type") == "turn_context":
                current = turns.get(payload.get("turn_id", current_id))
                if not current:
                    continue
                current.setdefault('_models', set()).add(payload.get('model'))
                current.setdefault('_efforts', set()).add(payload.get('effort', payload.get('reasoning_effort')))
                current["model"] = payload.get('model') if len(current['_models']) == 1 else None
                current["effort"] = (payload.get('effort', payload.get('reasoning_effort'))
                                     if len(current['_efforts']) == 1 else 'mixed')
    result = []
    for turn_id in order:
        turn = turns[turn_id]
        records = turn.pop("_usage_records")
        fallback = turn.pop("_fallback_usage")
        turn["usage"] = list(records.values()) if records else fallback
        result.append(turn)
    return result


def parse_rollouts(paths, primary=None):
    """Merge all rollout segments for a chat and deduplicate turns by ID."""
    merged = {}
    selected = rollout_chain(paths, primary) if primary else [(path, None) for path in sorted(paths)]
    for path, limit in selected:
        for turn in parse_rollout(path, limit):
            existing = merged.get(turn['turn_id'])
            score = (turn['end'] is not None, len(turn['usage']), bool(turn['model']))
            old_score = ((existing['end'] is not None), len(existing['usage']),
                         bool(existing['model'])) if existing else None
            if existing is None or score > old_score:
                merged[turn['turn_id']] = turn
    return sorted(merged.values(), key=lambda turn: (turn['start'], turn['turn_id']))


def parse_logs(rows):
    grouped = collections.defaultdict(list)
    for seconds, nanos, target, body in rows:
        match = TURN_ID.search(body or "")
        if not match:
            continue
        stamp = seconds + nanos / 1e9
        if (target == "feedback_tags" and "auth_header_attached" in body
                and "stream_request{" in body):
            grouped[match[1]].append({"time": stamp, "kind": "request_start"})
        elif target == "codex_core::stream_events_utils":
            item = ITEM.search(body)
            done = 'from="output_item_done"' in body
            if item or done:
                grouped[match[1]].append({"time": stamp,
                    "kind": "done" if done else "item_start",
                    "item_type": item[1] if item else "tool",
                    "item_id": item[2] if item else None})
    return grouped


def rate(tokens, seconds):
    return tokens / seconds if seconds and seconds > 0 else None


def summarize_turns(turns):
    """Summarize completed turns, prioritizing user-perceived latency."""
    completed = [t for t in turns if t['status'] == 'completed']
    wall_turns = [t for t in completed if t['wall_duration_s'] is not None]
    wall_tps_turns = [t for t in completed if t['wall_tps'] is not None]
    first_visible_turns = [t for t in completed if t['first_visible_delay_s'] is not None]
    model_time_turns = [t for t in completed if t['request_duration_s'] is not None]
    request_turns = [t for t in completed if t['request_average_tps'] is not None]
    body_turns = [t for t in completed if t['body_tps_estimate'] is not None]

    def median(items, key):
        return statistics.median(t[key] for t in items) if items else None

    request_tokens = sum(t['output_tokens'] for t in request_turns)
    request_seconds = sum(t['request_duration_s'] for t in request_turns)
    body_tokens = sum(t['body_tokens'] for t in body_turns)
    body_seconds = sum(t['body_duration_s'] for t in body_turns)
    return {
        'completed_turns': len(completed),
        'wall_turns': len(wall_turns),
        'wall_turn_mean_s': (statistics.mean(t['wall_duration_s'] for t in wall_turns)
                             if wall_turns else None),
        'wall_median_s': median(wall_turns, 'wall_duration_s'),
        'wall_tps_turns': len(wall_tps_turns),
        'wall_turn_mean_tps': (statistics.mean(t['wall_tps'] for t in wall_tps_turns)
                               if wall_tps_turns else None),
        'wall_median_tps': median(wall_tps_turns, 'wall_tps'),
        'first_visible_turns': len(first_visible_turns),
        'first_visible_turn_mean_s': (
            statistics.mean(t['first_visible_delay_s'] for t in first_visible_turns)
            if first_visible_turns else None),
        'first_visible_median_s': median(first_visible_turns, 'first_visible_delay_s'),
        'model_time_turns': len(model_time_turns),
        'model_time_turn_mean_s': (
            statistics.mean(t['request_duration_s'] for t in model_time_turns)
            if model_time_turns else None),
        'outside_request_turn_mean_s': (
            statistics.mean(t['outside_request_s'] for t in model_time_turns)
            if model_time_turns else None),
        'request_turns': len(request_turns),
        'body_turns': len(body_turns),
        'request_count': sum(t['request_count'] for t in request_turns),
        'body_request_count': sum(t['body_request_count'] for t in body_turns),
        'request_output_tokens': request_tokens,
        'request_duration_s': request_seconds,
        'body_tokens': body_tokens,
        'body_duration_s': body_seconds,
        'request_pooled_tps': rate(request_tokens, request_seconds),
        'body_pooled_tps': rate(body_tokens, body_seconds),
        'request_turn_mean_tps': (statistics.mean(t['request_average_tps'] for t in request_turns)
                                  if request_turns else None),
        'body_turn_mean_tps': (statistics.mean(t['body_tps_estimate'] for t in body_turns)
                               if body_turns else None),
        'request_median_tps': median(request_turns, 'request_average_tps'),
        'body_median_tps': median(body_turns, 'body_tps_estimate')}


def aggregate_models(records):
    """Group completed turns across chats with latency-first summaries."""
    # A fork repeats inherited turns in its visible history. Keep each actual turn once
    # and attribute it to the chat where it originally ran.
    unique = {}
    for sequence, (visible_thread_id, turn) in enumerate(records):
        recorded_origin = turn.get('origin_thread_id')
        origin = recorded_origin or visible_thread_id
        key = turn['turn_id'] if recorded_origin else ('legacy', sequence)
        row = (origin, turn)
        existing = unique.get(key)
        if existing is None or (recorded_origin and visible_thread_id == origin):
            unique[key] = row
    groups = collections.defaultdict(list)
    for thread_id, turn in unique.values():
        if turn['status'] == 'completed' and turn['model']:
            groups[turn['model']].append((thread_id, turn))

    def summarize(rows):
        turns = [t for _, t in rows]
        summary = summarize_turns(turns)
        return {'chat_count': len({tid for tid, _ in rows}), **summary,
            'first_turn_at': min(t['start'] for t in turns),
            'last_turn_at': max(t['start'] for t in turns)}
    result = []
    for model, rows in groups.items():
        efforts = collections.defaultdict(list)
        for row in rows:
            efforts[row[1]['effort'] or 'unknown'].append(row)
        result.append({'model': model, **summarize(rows), 'by_effort': [
            {'effort': effort, **summarize(items)} for effort, items in sorted(efforts.items())]})
    return sorted(result, key=lambda r: (
        r['wall_turn_mean_s'] if r['wall_turn_mean_s'] is not None else float('inf'),
        r['model']))


def analyze_turn(turn, events):
    starts = sorted(set(e["time"] for e in events if e["kind"] == "request_start"))
    complete = turn.get("outcome") == "completed" or (
        turn.get("outcome") is None and turn["end"] is not None)
    usage = turn["usage"]
    message_starts = [e['time'] for e in events if e['kind'] == 'item_start'
                      and e.get('item_type') == 'message'
                      and e['time'] >= turn['start']
                      and (turn['end'] is None or e['time'] <= turn['end'])]
    # A count mismatch can shift every request. Do not invent an alignment.
    aligned = complete and bool(usage) and len(starts) == len(usage)
    measured_delay = turn.get("first_visible_delay_s")
    logged_visible_at = min(message_starts) if message_starts else None
    first_visible_delay = measured_delay
    if first_visible_delay is None and aligned and logged_visible_at is not None:
        first_visible_delay = logged_visible_at - turn['start']
    first_visible_at = (turn['start'] + first_visible_delay
                        if first_visible_delay is not None else None)
    requests = []
    if aligned:
        for i, (start, u) in enumerate(zip(starts, usage)):
            limit = starts[i + 1] if i + 1 < len(starts) else turn["end"]
            batch = [e for e in events if start <= e["time"] < limit]
            done = [e["time"] for e in batch if e["kind"] == "done"]
            end = max(done) if done else None
            duration = end - start if end is not None else None
            output = u.get("output_tokens", 0)
            reasoning = u.get("reasoning_output_tokens")
            messages = [e for e in batch if e.get("item_type") == "message"
                        and e["kind"] == "item_start"]
            tools = [e for e in batch if e.get("item_type")
                     and e.get("item_type") not in ("message", "reasoning")]
            body_seconds = body_tokens = body_tps = None
            body_note = "请求含工具调用或多段输出，无法分配正文 Token"
            if len(messages) == 1 and not tools and reasoning is not None:
                message = messages[0]
                endings = [e["time"] for e in batch if e["kind"] == "done"
                           and e.get("item_id") == message["item_id"]]
                if endings and output >= reasoning:
                    body_seconds = max(endings) - message["time"]
                    body_tokens = output - reasoning
                    body_tps = rate(body_tokens, body_seconds)
                    body_note = "正文输出项开始至完成；不是首末 Token 的精确时间"
            elif reasoning is None:
                body_note = "日志未提供推理 Token 数，无法可靠分离正文"
            requests.append({"number": i + 1, "start": start, "end": end,
                "output_tokens": output, "reasoning_tokens": reasoning,
                "duration_s": duration, "average_tps": rate(output, duration),
                "body_tokens": body_tokens, "body_duration_s": body_seconds,
                "body_tps_estimate": body_tps, "body_note": body_note})
    output = sum(u.get("output_tokens", 0) for u in usage)
    wall = turn.get("duration_s") if complete else None
    if wall is None and complete:
        wall = turn["end"] - turn["start"]
    all_durations = bool(requests) and all(r["average_tps"] is not None for r in requests)
    request_seconds = sum(r["duration_s"] for r in requests) if all_durations else None
    outside_request = (max(0, wall - request_seconds)
                       if wall is not None and request_seconds is not None else None)
    body_requests = [r for r in requests if r["body_tps_estimate"] is not None]
    body_seconds = sum(r["body_duration_s"] for r in body_requests)
    body_tokens = sum(r["body_tokens"] for r in body_requests)
    notes = []
    if len(turn.get('_models', [])) > 1:
        notes.append('本轮包含多个模型，不归入单一模型汇总')
    if turn.get("outcome") == "aborted":
        notes.append("本轮已中断，不计算速度")
    elif not complete:
        notes.append("本轮尚未完成，不计算速度")
    elif not aligned:
        notes.append("请求起点与用量记录不匹配（%s / %s），日志可能已清理；不计算请求 TPS" %
                     (len(starts), len(usage)))
    if aligned and not all_durations:
        notes.append("部分请求缺少输出完成事件，不计算本轮请求平均 TPS")
    if not body_requests:
        notes.append("没有可独立统计的正文输出项")
    reasoning = ([u.get("reasoning_output_tokens") for u in usage] if usage else [])
    status = "completed" if complete else (turn.get("outcome") or "unfinished")
    return {"turn_id": turn["turn_id"], "start": turn["start"], "end": turn["end"],
        "origin_thread_id": turn.get("origin_thread_id"),
        "status": status, "model": turn["model"],
        "effort": turn["effort"], "output_tokens": output,
        "reasoning_tokens": (sum(reasoning) if reasoning and all(v is not None for v in reasoning)
                             else None),
        "wall_duration_s": wall, "aborted_duration_s": (turn.get("duration_s")
                                                          if status == "aborted" else None),
        "wall_tps": rate(output, wall) if complete and usage else None,
        "first_visible_at": first_visible_at, "first_visible_delay_s": first_visible_delay,
        "request_count": len(usage), "request_duration_s": request_seconds,
        "outside_request_s": outside_request,
        "request_average_tps": rate(output, request_seconds),
        "body_tokens": body_tokens if body_requests else None,
        "body_duration_s": body_seconds if body_requests else None,
        "body_tps_estimate": rate(body_tokens, body_seconds),
        "body_request_count": len(body_requests), "notes": notes, "requests": requests}


class Store:
    def __init__(self, codex_home):
        self.home = pathlib.Path(codex_home).expanduser().resolve()
        self.cache = {}
        self.lock = threading.Lock()
        self.summary_cache = None
        self.summary_lock = threading.Lock()
        self.rollout_index = None

    def rollout_paths(self, thread_id, primary):
        now = time.monotonic()
        if not self.rollout_index or now - self.rollout_index[0] >= 8:
            index = collections.defaultdict(set)
            for root in (self.home / 'sessions', self.home / 'archived_sessions'):
                if not root.exists():
                    continue
                for candidate in root.rglob('*.jsonl'):
                    for identifier in UUID_ANY.findall(candidate.name):
                        index[identifier].add(candidate.resolve())
                    segment_id, owner_id, _ = rollout_metadata(candidate)
                    for identifier in (segment_id, owner_id):
                        if identifier:
                            index[identifier].add(candidate.resolve())
            self.rollout_index = (now, index)
        index = self.rollout_index[1]
        selected = set(index.get(thread_id, set())) | {primary}
        pending = list(selected)
        while pending:
            _, _, history_base = rollout_metadata(pending.pop())
            parent_id = history_base.get('thread_id') if history_base else None
            for candidate in index.get(parent_id, set()):
                if candidate not in selected:
                    selected.add(candidate)
                    pending.append(candidate)
        return sorted(selected)

    def models(self, force=False):
        with self.summary_lock:
            if not force and self.summary_cache and time.monotonic() - self.summary_cache[0] < 60:
                return self.summary_cache[1]
            # Whole-turn metrics come from every visible chat. Request/body metrics remain
            # available only where the shorter-lived runtime log is still retained.
            with connect(self.home / 'logs_2.sqlite') as db:
                retained = {r[0] for r in db.execute(
                    "SELECT DISTINCT thread_id FROM logs WHERE target IN "
                    "('feedback_tags','codex_core::stream_events_utils')")}
            with connect(self.home / 'state_5.sqlite') as db:
                threads = db.execute(
                    "SELECT id FROM threads WHERE source NOT LIKE '%\"subagent\"%' "
                    "AND COALESCE(thread_source,'') NOT IN ('subagent','guardian_review') "
                    "ORDER BY updated_at DESC").fetchall()
            records, warnings, scanned = [], [], 0
            for (tid,) in threads:
                try:
                    stats = self.stats(tid, include_logs=tid in retained)
                    scanned += 1
                    records.extend((tid, turn) for turn in stats['turns'])
                    warnings.extend(stats['warnings'])
                except (OSError, ValueError, sqlite3.Error) as exc:
                    warnings.append(tid + ': ' + str(exc))
            valid = [t for _, t in records if t['status'] == 'completed']
            result = {'generated_at': time.time(), 'cache_seconds': 60,
                'scanned_chats': scanned, 'models': aggregate_models(records),
                'measurement_start': min((t['start'] for t in valid), default=None),
                'measurement_end': max((t['start'] for t in valid), default=None),
                'warnings': warnings,
                'scope': '全部本机用户聊天，含归档，排除子代理与审批聊天；请求与正文指标仅覆盖仍有运行日志的轮次；未完成轮次不参与'}
            self.summary_cache = (time.monotonic(), result)
            return result

    def threads(self):
        with connect(self.home / "state_5.sqlite") as db:
            db.row_factory = sqlite3.Row
            rows = db.execute("SELECT id,COALESCE(NULLIF(name,''),title) AS title,model,updated_at,archived FROM threads "
                              "WHERE source NOT LIKE '%\"subagent\"%' "
                              "ORDER BY COALESCE(NULLIF(recency_at_ms,0),updated_at_ms,updated_at*1000) DESC, "
                              "created_at DESC,id DESC LIMIT 100").fetchall()
            result = [dict(row) for row in rows]
            for row in result:
                row['title'] = row['title'][:160]
            return result

    def stats(self, thread_id, include_logs=True):
        if not UUID.fullmatch(thread_id):
            raise ValueError("聊天 ID 格式无效")
        with self.lock:
            with connect(self.home / "state_5.sqlite") as db:
                row = db.execute("SELECT rollout_path,COALESCE(NULLIF(name,''),title) FROM threads WHERE id=?",
                                 (thread_id,)).fetchone()
            if not row:
                raise ValueError("未找到这个聊天")
            path = pathlib.Path(row[0]).resolve()
            roots = (self.home / "sessions", self.home / "archived_sessions")
            if not any(parent in path.parents for parent in roots):
                raise ValueError("会话日志路径不在 Codex 日志目录内")
            paths = [candidate for candidate in self.rollout_paths(thread_id, path)
                     if candidate.exists() and any(root in candidate.parents for root in roots)]
            signature = tuple((str(candidate), candidate.stat().st_mtime_ns,
                               candidate.stat().st_size) for candidate in paths)
            cache_key = (thread_id, include_logs)
            cached = self.cache.get(cache_key)
            if cached and cached[0] == signature and time.monotonic() - cached[1] < 8:
                return cached[2]
            turns = parse_rollouts(paths, path)
            warnings = []
            grouped = {}
            if include_logs:
                try:
                    origins = sorted({t.get('origin_thread_id') or thread_id for t in turns})
                    placeholders = ','.join('?' for _ in origins)
                    with connect(self.home / "logs_2.sqlite") as db:
                        rows = db.execute("SELECT ts,ts_nanos,target,feedback_log_body FROM logs "
                            "WHERE thread_id IN (" + placeholders + ") AND target IN (?,?) ORDER BY id",
                            tuple(origins) + ("feedback_tags",
                                              "codex_core::stream_events_utils")).fetchall()
                    grouped = parse_logs(rows)
                except sqlite3.Error as exc:
                    warnings.append("运行日志不可读：" + str(exc))
            analyzed = [analyze_turn(t, grouped.get(t["turn_id"], [])) for t in turns]
            result = {"thread_id": thread_id, "title": row[1][:160], "generated_at": time.time(),
                      "warnings": warnings, "summary": summarize_turns(analyzed),
                      "turns": analyzed}
            self.cache[cache_key] = (signature, time.monotonic(), result)
            # Avoid retaining every inspected conversation indefinitely.
            if len(self.cache) > 20:
                self.cache.pop(next(iter(self.cache)))
            return result


def handler(store, default_thread):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            # Loopback bind + strict Host + same-origin policy; no log API exposed to other sites.
            if self.headers.get("Host") not in ("localhost:%s" % self.server.server_port,
                                               "127.0.0.1:%s" % self.server.server_port):
                self.respond(403, {"error": "仅允许本机访问"})
                return
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.respond(403, {"error": "不允许跨站读取本地日志"})
                return
            url = urllib.parse.urlparse(self.path)
            try:
                if url.path == "/":
                    self.respond(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/api/threads":
                    self.respond(200, {"threads": store.threads(), "default_thread": default_thread})
                elif url.path == "/api/models":
                    self.respond(200, store.models(force=urllib.parse.parse_qs(url.query).get(
                        'refresh', ['0'])[0] == '1'))
                elif url.path == "/api/stats":
                    tid = urllib.parse.parse_qs(url.query).get("thread", [default_thread])[0]
                    self.respond(200, store.stats(tid))
                else:
                    self.respond(404, {"error": "页面不存在"})
            except (ValueError, OSError, sqlite3.Error) as exc:
                self.respond(400, {"error": str(exc)})

        def respond(self, status, value, mime="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(
                value, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            pass
    return Handler


def main():
    parser = argparse.ArgumentParser(description="Codex 本地 TPS 统计网页（只读日志）")
    parser.add_argument("--codex-home", default=str(pathlib.Path.home() / ".codex"))
    parser.add_argument("--thread", help="默认聊天 ID；未指定时使用最近聊天")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--open", action="store_true", help="在默认浏览器打开")
    parser.add_argument("--export", metavar="JSON_PATH", help="导出所选聊天的统计后退出")
    args = parser.parse_args()
    store = Store(args.codex_home)
    threads = store.threads()
    thread = args.thread or (threads[0]["id"] if threads else None)
    if not thread:
        parser.error("没有找到聊天日志")
    if args.export:
        pathlib.Path(args.export).write_text(json.dumps(store.stats(thread), ensure_ascii=False,
                                                       indent=2), encoding="utf-8")
        print("已导出：" + args.export)
        return
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler(store, thread))
    server.daemon_threads = True
    url = "http://127.0.0.1:%s" % server.server_port
    print("Codex TPS 网页：" + url, flush=True)
    print("只读本地日志；按 Ctrl+C 停止。", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
