"""Host-owned schedule and tool declarations for Lantern's private chat worker."""

from datetime import datetime, timedelta, timezone


SCHEMA = """
    CREATE TABLE IF NOT EXISTS scheduler (
        id INTEGER PRIMARY KEY CHECK (id=1),
        heartbeat_minutes INTEGER, next_heartbeat TEXT, budget_until TEXT);
    INSERT OR IGNORE INTO scheduler(id) VALUES(1);
    CREATE TABLE IF NOT EXISTS alarms (
        id INTEGER PRIMARY KEY, at_utc TEXT NOT NULL, note TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending');
    CREATE INDEX IF NOT EXISTS due_alarms ON alarms(at_utc,id) WHERE status='pending';
    CREATE TABLE IF NOT EXISTS schedule_calls (
        id TEXT PRIMARY KEY, request TEXT NOT NULL, result TEXT NOT NULL);
"""


def _tool(name, description, properties):
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "additionalProperties": False,
                            "properties": properties, "required": list(properties)}}


SCHEDULE_TOOLS = [
    _tool("heartbeat", "Choose the interval between your independent wakeups, in minutes. "
          "Use 0 to pause, or 5 through 10080 to enable or adjust. One-shot alarms are separate.",
          {"minutes": {"type": "integer", "minimum": 0, "maximum": 10080}}),
    _tool("set_alarm", "Set a one-time reminder to yourself. Supply a future ISO 8601 timestamp "
          "with a timezone, and a brief note. You can read it when the worker wakes.",
          {"at_utc": {"type": "string"}, "note": {"type": "string", "minLength": 1, "maxLength": 1000}}),
    _tool("cancel_alarm", "Cancel a pending one-time reminder using its alarm ID.",
          {"alarm_id": {"type": "integer", "minimum": 1}}),
    {"name": "schedule_status", "description": "Read UTC time, heartbeat settings, pending alarms and "
     "any daily budget pause. Use next_after_id as after_id to see another page.",
     "inputSchema": {"type": "object", "additionalProperties": False, "required": [],
                     "properties": {"after_id": {"type": "integer", "minimum": 0}}}},
]


def dispatch_schedule(store, call_id, name, arguments):
    if name == "schedule_status" and set(arguments) <= {"after_id"}:
        return store.schedule_status(**arguments)
    if name == "heartbeat" and set(arguments) == {"minutes"}:
        return store.configure_heartbeat(arguments["minutes"], call_id=call_id)
    if name == "set_alarm" and set(arguments) == {"at_utc", "note"}:
        return store.set_alarm(arguments["at_utc"], arguments["note"], call_id=call_id)
    if name == "cancel_alarm" and set(arguments) == {"alarm_id"}:
        return store.cancel_alarm(arguments["alarm_id"], call_id=call_id)
    raise ValueError("Unrecognized schedule tool or arguments")


def next_reset(now):
    return (now.astimezone(timezone.utc) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


class DailyBudgetHold(RuntimeError):
    """A local daily cap refused a request. Upstream failures use the ordinary error path."""

    def __init__(self, *, dispatched: bool, reset_at: datetime | None = None):
        super().__init__("Daily budget reached")
        self.dispatched = dispatched
        self.reset_at = reset_at or next_reset(datetime.now(timezone.utc))
