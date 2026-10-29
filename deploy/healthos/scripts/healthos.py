#!/usr/bin/env python3
"""HealthOS: a small, dependency-free Apple Health archive and query service.

The importer is deliberately streaming: the Apple export can be several GB and
must never be read into memory as one XML document.
"""

from __future__ import annotations

import argparse
import contextlib
from collections import Counter
import datetime as dt
import html
import hashlib
import hmac
import http.server
import json
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
import zipfile
import gzip
import re
import xml.etree.ElementTree as ET


SCHEMA_VERSION = "0.4.0"
DEFAULT_DB = "private/healthos/healthos.sqlite3"
DEFAULT_RAW = "private/healthos/raw"
SESSION_TTL_SECONDS = 30 * 60
SESSION_COOKIE = "healthos_session"

_SESSIONS: dict[str, dict[str, object]] = {}
_SESSIONS_LOCK = threading.Lock()

TRAINING_PAGE_SCRIPT = r"""<script>
const form = document.getElementById('training-form');
const list = document.getElementById('training-records');
const addButton = document.getElementById('add-training-record');
const recordsField = document.getElementById('training-records-json');
const timingField = document.getElementById('training-timing-json');
const timerLabel = document.getElementById('training-timer');
const startButton = document.getElementById('training-start');
const finishButton = document.getElementById('training-finish');
const editing = Boolean(form.querySelector('[name=session_id]'));
const remoteWrite = Boolean(form.querySelector('[name=training_source]'));
const draftable = !editing || remoteWrite;
const remoteDraftId = form.querySelector('[name=session_id]')?.value || 'new';
const draftKey = remoteWrite
  ? `healthos.training.ironbook.${remoteDraftId}.draft.v1`
  : 'healthos.training.draft.v1';
const exerciseOptions = document.getElementById('exercise-options');
let suggestionTimer = null;
const lastTrainingCache = new Map();
let timing = {};
let timerHandle = null;

function csv(value) {
  return value.split(/[，,]/).map(item => item.trim()).filter(Boolean).map(Number);
}
function collectRecords() {
  return [...document.querySelectorAll('.training-record-row')].map((row, index) => {
    const original = JSON.parse(row.dataset.original || '{}');
    const name = row.querySelector('.exercise-name').value.trim();
    const isDropSet = row.querySelector('.drop-set').checked;
    const extra = Array.isArray(original.extra_data)
      ? (isDropSet ? {structure: 'drop_set_group', legacy_extra_data: original.extra_data} : original.extra_data)
      : {...(original.extra_data && typeof original.extra_data === 'object' ? original.extra_data : {})};
    if (!Array.isArray(extra)) {
      if (isDropSet) extra.structure = 'drop_set_group';
      else delete extra.structure;
    }
    const segments = isDropSet ? [...row.querySelectorAll('.drop-set-segment')].map(segment => ({
      weight_kg: segment.querySelector('.segment-weight').value || null,
      reps: segment.querySelector('.segment-reps').value || null
    })) : null;
    if (isDropSet) extra.segments = segments;
    else if (!Array.isArray(extra)) delete extra.segments;
    return {
      ...original,
      name,
      raw_name: original.raw_exercise_name || original.raw_name || name,
      weight_kg: isDropSet ? null : row.querySelector('.exercise-weight').value || null,
      reps: isDropSet ? segments.map(segment => Number(segment.reps)).filter(Number.isFinite) : csv(row.querySelector('.exercise-reps').value),
      rpe: isDropSet ? [] : csv(row.querySelector('.exercise-rpe').value),
      sets_count: isDropSet ? 1 : Number(row.querySelector('.exercise-sets').value) || null,
      exercise_order: index + 1,
      note: row.querySelector('.exercise-note').value.trim() || null,
      extra_data: extra
    };
  }).filter(record => record.name);
}
function addRow(record = {}) {
  const row = document.createElement('div');
  row.className = 'form-grid training-record-row';
  const remoteRequired = remoteWrite ? ' required' : '';
  row.innerHTML = `<label>动作名称<input class="exercise-name" list="exercise-options" maxlength="128" required></label><p class="muted last-training" aria-live="polite"></p><div class="ordinary-fields form-grid"><label>重量（kg）<input class="exercise-weight" type="number" min="0" step="0.1"${remoteRequired}></label><label>次数（逗号分隔）<input class="exercise-reps" type="text"${remoteRequired}></label><label>RPE（逗号分隔）<input class="exercise-rpe" type="text"${remoteRequired}></label><label>组数<input class="exercise-sets" type="number" min="0" step="1"></label></div><label><input class="drop-set" type="checkbox"> 递减组</label><div class="drop-set-fields" hidden><div class="drop-set-segments"></div><button class="secondary add-drop-set-segment" type="button">添加递减段</button></div><label>备注<input class="exercise-note" maxlength="1000"></label><button class="secondary remove-record" type="button">移除此组</button>`;
  row.dataset.original = JSON.stringify(record);
  row.querySelector('.exercise-name').value = record.standard_name || record.name || '';
  row.querySelector('.exercise-weight').value = record.weight_kg ?? '';
  row.querySelector('.exercise-reps').value = Array.isArray(record.reps) ? record.reps.join(', ') : '';
  row.querySelector('.exercise-rpe').value = Array.isArray(record.rpe) ? record.rpe.join(', ') : '';
  row.querySelector('.exercise-sets').value = record.sets_count ?? '';
  row.querySelector('.exercise-note').value = record.note || '';
  const extra = record.extra_data && !Array.isArray(record.extra_data) ? record.extra_data : {};
  const isDropSet = extra.structure === 'drop_set_group';
  const addSegment = (segment = {}) => {
    const item = document.createElement('div');
    item.className = 'drop-set-segment form-grid';
    item.innerHTML = `<label>递减段重量（kg）<input class="segment-weight" type="number" min="0" step="0.1"></label><label>次数<input class="segment-reps" type="number" min="1" step="1"></label><button class="secondary remove-segment" type="button">移除此段</button>`;
    item.querySelector('.segment-weight').value = segment.weight_kg ?? '';
    item.querySelector('.segment-reps').value = segment.reps ?? '';
    item.querySelectorAll('input').forEach(input => input.addEventListener('input', saveDraft));
    item.querySelector('.remove-segment').onclick = () => { item.remove(); saveDraft(); };
    row.querySelector('.drop-set-segments').append(item);
    return item;
  };
  const toggleStructure = () => {
    const active = row.querySelector('.drop-set').checked;
    row.querySelector('.ordinary-fields').hidden = active;
    row.querySelector('.drop-set-fields').hidden = !active;
    row.querySelectorAll('.ordinary-fields input').forEach(input => {
      input.required = remoteWrite && !active && ['exercise-weight', 'exercise-reps', 'exercise-rpe'].some(name => input.classList.contains(name));
    });
    row.querySelectorAll('.drop-set-segment input').forEach(input => input.required = remoteWrite && active);
    if (active && !row.querySelectorAll('.drop-set-segment').length) addSegment();
    saveDraft();
  };
  row.querySelector('.drop-set').checked = isDropSet;
  if (Array.isArray(extra.segments)) extra.segments.forEach(addSegment);
  row.querySelector('.drop-set').addEventListener('change', toggleStructure);
  row.querySelector('.add-drop-set-segment').onclick = () => { addSegment(); toggleStructure(); };
  toggleStructure();
  row.querySelector('.remove-record').onclick = () => { row.remove(); saveDraft(); };
  row.querySelectorAll('input').forEach(input => input.addEventListener('input', saveDraft));
  row.querySelector('.exercise-name').addEventListener('input', () => {
    if (remoteWrite) scheduleSuggestions(row.querySelector('.exercise-name').value);
  });
  if (remoteWrite && !editing) {
    row.querySelector('.exercise-name').addEventListener('change', () => loadLastTraining(row));
  }
  list.append(row);
}
function formatLastTraining(data) {
  if (!data || !Array.isArray(data.sets) || !data.sets.length) return 'Ironbook 暂无此动作记录';
  const sets = data.sets.slice(0, 8).map((set, index) => {
    const extra = set.extra_data || {};
    if (extra.structure === 'drop_set_group') {
      const segments = Array.isArray(extra.segments) ? extra.segments.slice(0, 4) : [];
      const details = segments.map(item => `${item.weight_kg ?? '-'}kg×${item.reps ?? '-'}`).join(' / ');
      return `第${index + 1}组递减 ${details || '无分段数据'}`;
    }
    const reps = Array.isArray(set.reps) && set.reps.length ? set.reps.join('/') : '-';
    const rpe = Array.isArray(set.rpe) && set.rpe.length ? set.rpe.join('/') : '-';
    return `第${index + 1}组 ${set.weight_kg ?? '-'}kg × ${reps} 次 @RPE ${rpe}`;
  });
  return `上次训练 ${data.session_date || ''}：${sets.join('；')}`;
}
async function loadLastTraining(row) {
  const name = row.querySelector('.exercise-name').value.trim();
  const target = row.querySelector('.last-training');
  if (!name || name === '热身' || name === '放松') { target.textContent = ''; return; }
  const requestName = name;
  row.dataset.lastTrainingRequest = requestName;
  target.textContent = '正在读取上次训练…';
  try {
    let result = lastTrainingCache.get(requestName);
    if (!result) {
      const response = await fetch(`/owner/api/training/exercises/last?name=${encodeURIComponent(requestName)}`, {headers: {Accept: 'application/json'}});
      if (!response.ok) throw new Error('last_training_unavailable');
      result = await response.json();
      lastTrainingCache.set(requestName, result);
    }
    if (row.dataset.lastTrainingRequest === requestName && row.querySelector('.exercise-name').value.trim() === requestName) {
      target.textContent = formatLastTraining(result);
    }
  } catch (_) {
    if (row.dataset.lastTrainingRequest === requestName) target.textContent = '上次训练数据暂不可用';
  }
}
function scheduleSuggestions(query) {
  if (suggestionTimer) clearTimeout(suggestionTimer);
  suggestionTimer = setTimeout(async () => {
    const bodyPart = form.elements.body_part.value;
    const params = new URLSearchParams({q: query.trim(), body_part: bodyPart});
    try {
      const response = await fetch(`/owner/api/training/exercises?${params}`, {headers: {Accept: 'application/json'}});
      if (!response.ok) return;
      const items = await response.json();
      exerciseOptions.replaceChildren(...items.map(item => {
        const option = document.createElement('option');
        option.value = item.standard_name;
        return option;
      }));
    } catch (_) { /* Keep manual entry available when Ironbook suggestions are offline. */ }
  }, 180);
}
function readForm() {
  const fields = {};
  new FormData(form).forEach((value, key) => { if (key !== 'csrf' && key !== 'records_json') fields[key] = value; });
  return fields;
}
function saveDraft() {
  if (!draftable) return;
  recordsField.value = JSON.stringify(collectRecords());
  try {
    localStorage.setItem(draftKey, JSON.stringify({fields: readForm(), records: collectRecords(), timing}));
  } catch (_) { /* Browser storage can be disabled or full. */ }
}
function setTiming(value) {
  timing = value && typeof value === 'object' ? value : {};
  timingField.value = JSON.stringify(timing);
  updateTimer();
}
function updateTimer() {
  const start = timing.started_at ? Date.parse(timing.started_at) : NaN;
  const end = timing.ended_at ? Date.parse(timing.ended_at) : Date.now();
  if (!Number.isFinite(start)) {
    timerLabel.textContent = '尚未开始';
    startButton.disabled = false;
    finishButton.disabled = true;
    return;
  }
  const seconds = Math.max(0, Math.floor((end - start) / 1000));
  const hours = String(Math.floor(seconds / 3600)).padStart(2, '0');
  const minutes = String(Math.floor((seconds % 3600) / 60)).padStart(2, '0');
  const remainder = String(seconds % 60).padStart(2, '0');
  timerLabel.textContent = `${hours}:${minutes}:${remainder}${timing.ended_at ? ' · 已结束' : ' · 计时中'}`;
  startButton.disabled = !timing.ended_at;
  finishButton.disabled = Boolean(timing.ended_at);
  if (timing.ended_at) {
    form.elements.duration_minutes.value = String(Math.max(1, Math.round(seconds / 60)));
    if (timerHandle) clearInterval(timerHandle);
    timerHandle = null;
  }
}
function startTimer() {
  setTiming({started_at: new Date().toISOString(), ended_at: null});
  if (timerHandle) clearInterval(timerHandle);
  timerHandle = setInterval(updateTimer, 1000);
  saveDraft();
}
function finishTimer() {
  if (!timing.started_at || timing.ended_at) return;
  setTiming({...timing, ended_at: new Date().toISOString()});
  saveDraft();
}

let initialRecords = [];
try { initialRecords = JSON.parse(recordsField.value || '[]'); } catch (_) {}
initialRecords.forEach(addRow);
if (!initialRecords.length) addRow();
try { timing = JSON.parse(timingField.value || '{}'); } catch (_) { timing = {}; }
if (draftable && new URLSearchParams(location.search).has('saved')) {
  try { localStorage.removeItem(draftKey); } catch (_) {}
  history.replaceState(null, '', location.pathname + location.search.replace(/([?&])saved=1(&|$)/, '$1').replace(/[?&]$/, ''));
}
if (draftable) {
  try {
    const draft = JSON.parse(localStorage.getItem(draftKey) || 'null');
    if (draft && draft.fields) {
      Object.entries(draft.fields).forEach(([key, value]) => {
        if (form.elements[key]) form.elements[key].value = value;
      });
      if (Array.isArray(draft.records) && draft.records.length) {
        list.replaceChildren();
        draft.records.forEach(addRow);
      }
      if (draft.timing) timing = draft.timing;
    }
  } catch (_) { /* Ignore malformed browser drafts. */ }
}
setTiming(timing);
if (timing.started_at && !timing.ended_at) timerHandle = setInterval(updateTimer, 1000);
addButton.onclick = () => { addRow(); saveDraft(); };
if (remoteWrite) scheduleSuggestions('');
startButton.onclick = startTimer;
finishButton.onclick = finishTimer;
form.querySelectorAll('input, select, textarea').forEach(input => input.addEventListener('input', saveDraft));
form.addEventListener('submit', event => {
  const records = collectRecords();
  if (!records.length) {
    event.preventDefault();
    document.getElementById('training-form-error').textContent = '请至少填写一个动作组。';
    return;
  }
  recordsField.value = JSON.stringify(records);
  timingField.value = JSON.stringify(timing);
  saveDraft();
});
</script>"""


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def parse_number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def hash_password(password: str, *, iterations: int = 310_000) -> str:
    if not password:
        raise ValueError("password must not be empty")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    try:
        algorithm, iterations_text, salt_hex, digest_hex = (encoded or "").split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def owner_password_hash() -> str | None:
    configured = os.environ.get("HEALTHOS_OWNER_PASSWORD_HASH", "").strip()
    return configured or None


def create_session() -> tuple[str, str]:
    session_id = secrets.token_urlsafe(32)
    csrf_token = secrets.token_urlsafe(32)
    with _SESSIONS_LOCK:
        _SESSIONS[session_id] = {
            "csrf": csrf_token,
            "expires_at": time.time() + SESSION_TTL_SECONDS,
        }
    return session_id, csrf_token


def session_data(session_id: str | None) -> dict[str, object] | None:
    if not session_id:
        return None
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(session_id)
        if not session:
            return None
        if float(session["expires_at"]) <= time.time():
            _SESSIONS.pop(session_id, None)
            return None
        session["expires_at"] = time.time() + SESSION_TTL_SECONDS
        return dict(session)


def destroy_session(session_id: str | None) -> None:
    if session_id:
        with _SESSIONS_LOCK:
            _SESSIONS.pop(session_id, None)


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Create the PRD canonical tables and migrate legacy Apple samples once."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )"""
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    if "0.3.0-canonical" not in applied:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY,
                metric TEXT NOT NULL,
                value_num REAL,
                value_text TEXT,
                unit TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                source TEXT,
                source_device TEXT,
                source_record_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                raw_reference TEXT,
                identity_hash TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                UNIQUE(identity_hash, content_hash)
            );
            CREATE INDEX IF NOT EXISTS idx_observations_metric_time
                ON observations(metric, start_time);
            CREATE TABLE IF NOT EXISTS lab_results (
                id INTEGER PRIMARY KEY,
                collection_time TEXT NOT NULL,
                report_time TEXT,
                test_name TEXT NOT NULL,
                canonical_name TEXT,
                value_num REAL,
                value_text TEXT,
                unit TEXT,
                reference_low REAL,
                reference_high REAL,
                reference_text TEXT,
                flag TEXT,
                institution TEXT,
                source TEXT,
                document_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_lab_results_collection_time
                ON lab_results(collection_time);
            CREATE TABLE IF NOT EXISTS medications (
                id INTEGER PRIMARY KEY,
                drug_name TEXT NOT NULL,
                generic_name TEXT,
                dose REAL,
                dose_unit TEXT,
                frequency TEXT,
                start_time TEXT NOT NULL,
                end_time TEXT,
                reason TEXT,
                source TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_medications_start_time
                ON medications(start_time);
            CREATE TABLE IF NOT EXISTS health_events (
                id INTEGER PRIMARY KEY,
                event_type TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT,
                title TEXT NOT NULL,
                notes TEXT,
                structured_payload TEXT,
                source TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_health_events_start_time
                ON health_events(start_time);
            CREATE TABLE IF NOT EXISTS medical_documents (
                id INTEGER PRIMARY KEY,
                document_type TEXT NOT NULL,
                document_date TEXT,
                institution TEXT,
                original_filename TEXT NOT NULL,
                storage_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                mime_type TEXT,
                imported_at TEXT NOT NULL,
                extraction_status TEXT NOT NULL DEFAULT 'not_started'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_medical_documents_sha256
                ON medical_documents(sha256);
            CREATE TABLE IF NOT EXISTS share_snapshots (
                id INTEGER PRIMARY KEY,
                token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                scope_type TEXT NOT NULL,
                scope_config TEXT NOT NULL,
                snapshot_payload TEXT NOT NULL,
                snapshot_format TEXT NOT NULL DEFAULT 'json',
                max_reads INTEGER NOT NULL DEFAULT 5,
                read_count INTEGER NOT NULL DEFAULT 0,
                last_accessed_at TEXT,
                created_by TEXT NOT NULL DEFAULT 'owner'
            );
            CREATE INDEX IF NOT EXISTS idx_share_snapshots_expiry
                ON share_snapshots(expires_at);
            """
        )
        # Preserve the original import table while making its data canonical.
        conn.execute(
            """INSERT OR IGNORE INTO observations
            (id, metric, value_num, value_text, unit, start_time, end_time, source,
             source_device, source_record_id, created_at, updated_at, raw_reference,
             identity_hash, content_hash)
            SELECT id, metric, value_num, value_text, unit, start_time, end_time,
                   source, device_name, CAST(id AS TEXT), first_seen_at, last_seen_at,
                   metadata_json, identity_hash, content_hash
            FROM health_samples"""
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ("0.3.0-canonical", utc_now()),
        )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    if "0.4.0-training-domain" not in applied:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS training_exercises (
                id INTEGER PRIMARY KEY,
                source_ironbook_id INTEGER UNIQUE,
                standard_name TEXT NOT NULL UNIQUE,
                body_part TEXT NOT NULL,
                is_compound INTEGER,
                primary_muscle TEXT,
                secondary_muscles TEXT NOT NULL DEFAULT '[]',
                extra_data TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_training_exercises_body_part
                ON training_exercises(body_part);
            CREATE TABLE IF NOT EXISTS training_exercise_aliases (
                id INTEGER PRIMARY KEY,
                source_ironbook_id INTEGER UNIQUE,
                exercise_id INTEGER NOT NULL REFERENCES training_exercises(id) ON DELETE CASCADE,
                alias_name TEXT NOT NULL,
                normalized_alias TEXT NOT NULL UNIQUE,
                alias_type TEXT NOT NULL DEFAULT 'colloquial',
                source TEXT NOT NULL DEFAULT 'user',
                confidence REAL,
                confirmed_at TEXT,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_training_alias_exercise
                ON training_exercise_aliases(exercise_id);
            CREATE TABLE IF NOT EXISTS training_alias_review_queue (
                id INTEGER PRIMARY KEY,
                source_ironbook_id INTEGER UNIQUE,
                raw_name TEXT NOT NULL,
                normalized_raw TEXT NOT NULL,
                candidate_exercise_id INTEGER REFERENCES training_exercises(id) ON DELETE SET NULL,
                suggested_standard_name TEXT,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                decision_note TEXT,
                created_at TEXT NOT NULL,
                decided_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_training_alias_review_status
                ON training_alias_review_queue(status, created_at);
            CREATE TABLE IF NOT EXISTS training_sessions (
                id INTEGER PRIMARY KEY,
                request_id TEXT UNIQUE,
                source_ironbook_id INTEGER UNIQUE,
                session_date TEXT NOT NULL,
                weekday TEXT,
                body_part TEXT NOT NULL,
                sleep_hours REAL,
                stress_level INTEGER,
                hrv_score INTEGER,
                total_volume_kg REAL,
                duration_minutes INTEGER,
                status_note TEXT,
                feeling TEXT,
                raw_markdown TEXT,
                source_file TEXT,
                extra_data TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_training_sessions_date
                ON training_sessions(session_date DESC, id DESC);
            CREATE INDEX IF NOT EXISTS idx_training_sessions_body_part
                ON training_sessions(body_part, session_date DESC);
            CREATE TABLE IF NOT EXISTS training_exercise_records (
                id INTEGER PRIMARY KEY,
                source_ironbook_id INTEGER UNIQUE,
                session_id INTEGER NOT NULL REFERENCES training_sessions(id) ON DELETE CASCADE,
                exercise_id INTEGER NOT NULL REFERENCES training_exercises(id) ON DELETE RESTRICT,
                raw_exercise_name TEXT,
                weight_kg REAL,
                reps TEXT NOT NULL DEFAULT '[]',
                sets_count INTEGER,
                total_reps INTEGER,
                volume_kg REAL,
                rpe TEXT NOT NULL DEFAULT '[]',
                is_warmup INTEGER NOT NULL DEFAULT 0,
                exercise_order INTEGER,
                note TEXT,
                raw_line TEXT,
                extra_data TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_training_records_session
                ON training_exercise_records(session_id, exercise_order, id);
            CREATE INDEX IF NOT EXISTS idx_training_records_exercise
                ON training_exercise_records(exercise_id, id);
            CREATE TABLE IF NOT EXISTS training_apple_workout_matches (
                id INTEGER PRIMARY KEY,
                training_session_id INTEGER NOT NULL UNIQUE REFERENCES training_sessions(id) ON DELETE CASCADE,
                apple_workout_id INTEGER NOT NULL REFERENCES workouts(id) ON DELETE CASCADE,
                status TEXT NOT NULL DEFAULT 'matched',
                match_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(apple_workout_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ("0.4.0-training-domain", utc_now()),
        )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    if "0.5.0-observation-count" not in applied:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS owner_dashboard_counts (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                observations INTEGER NOT NULL
            )"""
        )
        refresh_observation_count(conn)
        install_observation_count_triggers(conn)
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            ("0.5.0-observation-count", utc_now()),
        )


def refresh_observation_count(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO owner_dashboard_counts(id, observations)
           SELECT 1, COUNT(*) FROM observations WHERE 1
           ON CONFLICT(id) DO UPDATE SET observations=excluded.observations"""
    )


def install_observation_count_triggers(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS observations_count_after_insert
        AFTER INSERT ON observations
        BEGIN
            UPDATE owner_dashboard_counts SET observations=observations+1 WHERE id=1;
        END;
        CREATE TRIGGER IF NOT EXISTS observations_count_after_delete
        AFTER DELETE ON observations
        BEGIN
            UPDATE owner_dashboard_counts SET observations=observations-1 WHERE id=1;
        END;
        """
    )
def init_db(path: str | Path, *, check_same_thread: bool = True, bulk: bool = False) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    if bulk:
        # The source export is replayable. Bulk mode trades crash durability
        # during this import for much faster local indexing; import_xml restores
        # WAL after the replay finishes.
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-262144")
    else:
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS ingest_batches (
            batch_id TEXT PRIMARY KEY,
            received_at TEXT NOT NULL,
            device TEXT,
            window_start TEXT,
            window_end TEXT,
            sample_count INTEGER NOT NULL DEFAULT 0,
            schema_version TEXT NOT NULL,
            raw_path TEXT,
            payload_hash TEXT,
            source_kind TEXT NOT NULL DEFAULT 'shortcut',
            status TEXT NOT NULL DEFAULT 'accepted'
        );
        CREATE TABLE IF NOT EXISTS health_samples (
            id INTEGER PRIMARY KEY,
            metric TEXT NOT NULL,
            value_num REAL,
            value_text TEXT,
            unit TEXT,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            creation_time TEXT,
            source TEXT,
            source_name TEXT,
            device_name TEXT,
            identity_hash TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            raw_type TEXT NOT NULL,
            metadata_json TEXT,
            UNIQUE(identity_hash, content_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_health_samples_metric_time
            ON health_samples(metric, start_time);
        CREATE INDEX IF NOT EXISTS idx_health_samples_identity
            ON health_samples(identity_hash);
        CREATE TABLE IF NOT EXISTS workouts (
            id INTEGER PRIMARY KEY,
            activity_type TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            duration REAL,
            duration_unit TEXT,
            total_distance REAL,
            total_distance_unit TEXT,
            total_energy REAL,
            total_energy_unit TEXT,
            source_name TEXT,
            device_name TEXT,
            identity_hash TEXT NOT NULL UNIQUE,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            metadata_json TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_workouts_start ON workouts(start_time);
        CREATE TABLE IF NOT EXISTS activity_summaries (
            id INTEGER PRIMARY KEY,
            date_components TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            identity_hash TEXT NOT NULL UNIQUE,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL
        );
            CREATE TABLE IF NOT EXISTS migration_runs (
                id INTEGER PRIMARY KEY,
                source_kind TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                backup_path TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                sessions_seen INTEGER NOT NULL,
                records_seen INTEGER NOT NULL,
                exercises_seen INTEGER NOT NULL,
                status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_migration_runs_completed
                ON migration_runs(completed_at DESC, id DESC);
        """
    )
    existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(ingest_batches)")}
    if "payload_hash" not in existing_columns:
        conn.execute("ALTER TABLE ingest_batches ADD COLUMN payload_hash TEXT")
    apply_migrations(conn)
    conn.commit()
    return conn


def backup_sqlite_database(
    db_path: str | Path,
    backup_dir: str | Path | None = None,
) -> Path:
    """Create and integrity-check a point-in-time SQLite backup before migration."""
    source_path = Path(db_path)
    if not source_path.exists():
        raise ValueError("HealthOS database does not exist")
    target_dir = Path(backup_dir) if backup_dir is not None else source_path.parent / "migration-backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / (
        "healthos-pre-migration-"
        + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + secrets.token_hex(4)
        + ".sqlite3"
    )
    source = sqlite3.connect(f"file:{source_path.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(target_path)
    try:
        source.backup(target)
        integrity = target.execute("PRAGMA integrity_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise RuntimeError("HealthOS migration backup failed integrity check")
        target.commit()
    except Exception:
        target.close()
        source.close()
        target_path.unlink(missing_ok=True)
        raise
    target.close()
    source.close()
    return target_path


def extract_metadata(element: ET.Element) -> dict[str, object] | None:
    """Extract child metadata while the parent element is still available."""
    entries: dict[str, str] = {}
    children: list[dict[str, object]] = []
    for child in list(element):
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "MetadataEntry":
            key = child.attrib.get("key")
            if key:
                entries[key] = child.attrib.get("value", "")
            continue
        child_data: dict[str, object] = {"tag": tag, "attributes": dict(child.attrib)}
        descendants = []
        for descendant in list(child):
            descendants.append({
                "tag": descendant.tag.rsplit("}", 1)[-1],
                "attributes": dict(descendant.attrib),
            })
        if descendants:
            child_data["children"] = descendants
        children.append(child_data)
    if not entries and not children:
        return None
    metadata: dict[str, object] = {}
    if entries:
        metadata["entries"] = entries
    if children:
        metadata["children"] = children
    return metadata


def record_payload(attrs: dict[str, str], metadata: dict[str, object] | None = None) -> dict[str, object]:
    # Keep the original HealthKit identifier and source metadata.  A later
    # mapping layer can add friendly metric names without losing provenance.
    identity_fields = {
        key: attrs.get(key)
        for key in ("type", "startDate", "endDate", "sourceName", "device")
    }
    metadata = metadata or {}
    metadata_entries = metadata.get("entries")
    if isinstance(metadata_entries, dict) and metadata_entries.get("HKMetadataKeySyncIdentifier"):
        identity_fields["sync_identifier"] = metadata_entries["HKMetadataKeySyncIdentifier"]
    content_fields = dict(identity_fields)
    content_fields.update({key: attrs.get(key) for key in ("value", "unit")})
    content_fields["metadata"] = metadata
    return {
        "metric": attrs.get("type", ""),
        "value_num": parse_number(attrs.get("value")),
        "value_text": attrs.get("value"),
        "unit": attrs.get("unit"),
        "start_time": attrs.get("startDate", ""),
        "end_time": attrs.get("endDate", ""),
        "creation_time": attrs.get("creationDate"),
        "source": "apple_health",
        "source_name": attrs.get("sourceName"),
        "device_name": attrs.get("device"),
        "raw_type": attrs.get("type", ""),
        "metadata_json": canonical_json(metadata) if metadata else None,
        "identity_hash": sha256_text(canonical_json(identity_fields)),
        "content_hash": sha256_text(canonical_json(content_fields)),
    }


def insert_sample(conn: sqlite3.Connection, sample: dict[str, object], seen_at: str) -> bool:
    before = conn.total_changes
    conn.execute(
        """INSERT INTO health_samples
        (metric,value_num,value_text,unit,start_time,end_time,creation_time,source,
         source_name,device_name,identity_hash,content_hash,first_seen_at,last_seen_at,
         raw_type,metadata_json)
        VALUES (:metric,:value_num,:value_text,:unit,:start_time,:end_time,:creation_time,
                :source,:source_name,:device_name,:identity_hash,:content_hash,:seen_at,
                :seen_at,:raw_type,:metadata_json)
        ON CONFLICT(identity_hash, content_hash) DO NOTHING""",
        {**sample, "seen_at": seen_at},
    )
    inserted = conn.total_changes > before
    if not inserted:
        conn.execute(
            "UPDATE health_samples SET last_seen_at=? WHERE identity_hash=? AND content_hash=?",
            (seen_at, sample["identity_hash"], sample["content_hash"]),
        )
    conn.execute(
        """INSERT INTO observations
        (metric,value_num,value_text,unit,start_time,end_time,source,source_device,
         source_record_id,created_at,updated_at,raw_reference,identity_hash,content_hash)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(identity_hash, content_hash) DO UPDATE SET updated_at=excluded.updated_at""",
        (
            sample["metric"], sample["value_num"], sample["value_text"], sample["unit"],
            sample["start_time"], sample["end_time"], sample["source"], sample["device_name"],
            None, seen_at, seen_at, sample["metadata_json"], sample["identity_hash"], sample["content_hash"],
        ),
    )
    return inserted


def insert_workout(conn: sqlite3.Connection, attrs: dict[str, str], seen_at: str, metadata: dict[str, object] | None = None) -> bool:
    identity_fields = {key: attrs.get(key) for key in ("workoutActivityType", "startDate", "endDate", "sourceName", "device")}
    if metadata:
        identity_fields["metadata"] = metadata
    identity_hash = sha256_text(canonical_json(identity_fields))
    before = conn.total_changes
    conn.execute(
        """INSERT INTO workouts
        (activity_type,start_time,end_time,duration,duration_unit,total_distance,total_distance_unit,
         total_energy,total_energy_unit,source_name,device_name,identity_hash,first_seen_at,last_seen_at,metadata_json)
        VALUES (:activity_type,:start_time,:end_time,:duration,:duration_unit,:total_distance,:total_distance_unit,
                :total_energy,:total_energy_unit,:source_name,:device_name,:identity_hash,:seen_at,:seen_at,:metadata_json)
        ON CONFLICT(identity_hash) DO NOTHING""",
        {
            "activity_type": attrs.get("workoutActivityType", ""),
            "start_time": attrs.get("startDate", ""),
            "end_time": attrs.get("endDate", ""),
            "duration": parse_number(attrs.get("duration")),
            "duration_unit": attrs.get("durationUnit"),
            "total_distance": parse_number(attrs.get("totalDistance")),
            "total_distance_unit": attrs.get("totalDistanceUnit"),
            "total_energy": parse_number(attrs.get("totalEnergyBurned")),
            "total_energy_unit": attrs.get("totalEnergyBurnedUnit"),
            "source_name": attrs.get("sourceName"),
            "device_name": attrs.get("device"),
            "identity_hash": identity_hash,
            "seen_at": seen_at,
            "metadata_json": canonical_json({"attributes": attrs, "metadata": metadata or {}}),
        },
    )
    inserted = conn.total_changes > before
    if not inserted:
        conn.execute("UPDATE workouts SET last_seen_at=? WHERE identity_hash=?", (seen_at, identity_hash))
    return inserted


def insert_activity_summary(conn: sqlite3.Connection, attrs: dict[str, str], seen_at: str) -> bool:
    date_components = attrs.get("dateComponents", "")
    identity_hash = sha256_text(canonical_json(attrs))
    before = conn.total_changes
    conn.execute(
        """INSERT INTO activity_summaries
        (date_components,payload_json,identity_hash,first_seen_at,last_seen_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(identity_hash) DO UPDATE SET last_seen_at=excluded.last_seen_at""",
        (date_components, canonical_json(attrs), identity_hash, seen_at, seen_at),
    )
    return conn.total_changes > before


def require_text(payload: dict[str, object], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def insert_lab_result(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    now = utc_now()
    collection_time = require_text(payload, "collection_time")
    test_name = require_text(payload, "test_name")
    cursor = conn.execute(
        """INSERT INTO lab_results
        (collection_time,report_time,test_name,canonical_name,value_num,value_text,unit,
         reference_low,reference_high,reference_text,flag,institution,source,document_id,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            collection_time, payload.get("report_time"), test_name, payload.get("canonical_name"),
            parse_number(str(payload.get("value_num"))) if payload.get("value_num") is not None else None,
            payload.get("value_text"), payload.get("unit"),
            parse_number(str(payload.get("reference_low"))) if payload.get("reference_low") is not None else None,
            parse_number(str(payload.get("reference_high"))) if payload.get("reference_high") is not None else None,
            payload.get("reference_text"), payload.get("flag"), payload.get("institution"),
            payload.get("source") or "manual", payload.get("document_id"), now, now,
        ),
    )
    return {"id": cursor.lastrowid, "collection_time": collection_time, "test_name": test_name}


def insert_medication(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    now = utc_now()
    drug_name = require_text(payload, "drug_name")
    start_time = require_text(payload, "start_time")
    cursor = conn.execute(
        """INSERT INTO medications
        (drug_name,generic_name,dose,dose_unit,frequency,start_time,end_time,reason,source,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (
            drug_name, payload.get("generic_name"),
            parse_number(str(payload.get("dose"))) if payload.get("dose") is not None else None,
            payload.get("dose_unit"), payload.get("frequency"), start_time, payload.get("end_time"),
            payload.get("reason"), payload.get("source") or "manual", now, now,
        ),
    )
    return {"id": cursor.lastrowid, "drug_name": drug_name, "start_time": start_time}


def insert_health_event(conn: sqlite3.Connection, payload: dict[str, object]) -> dict[str, object]:
    now = utc_now()
    event_type = require_text(payload, "event_type")
    start_time = require_text(payload, "start_time")
    title = require_text(payload, "title")
    structured_payload = payload.get("structured_payload")
    if structured_payload is not None and not isinstance(structured_payload, (dict, list)):
        raise ValueError("structured_payload must be an object or array")
    cursor = conn.execute(
        """INSERT INTO health_events
        (event_type,start_time,end_time,title,notes,structured_payload,source,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            event_type, start_time, payload.get("end_time"), title, payload.get("notes"),
            canonical_json(structured_payload) if structured_payload is not None else None,
            payload.get("source") or "manual", now, now,
        ),
    )
    return {"id": cursor.lastrowid, "event_type": event_type, "start_time": start_time, "title": title}


# The query layer is deliberately small and explicit.  Owner pages and read
# projections call these functions instead of embedding SQL with subtly
# different filtering/ordering rules.
def _date_bounds(from_date: str | None, to_date: str | None) -> tuple[str | None, str | None]:
    if from_date:
        dt.date.fromisoformat(from_date)
    if to_date:
        dt.date.fromisoformat(to_date)
    if from_date and to_date and from_date > to_date:
        raise ValueError("from must not be after to")
    return from_date, to_date


def query_observations(
    conn: sqlite3.Connection,
    *,
    metric: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 1000,
) -> list[dict[str, object]]:
    from_date, to_date = _date_bounds(from_date, to_date)
    limit = min(max(int(limit), 1), 5000)
    clauses: list[str] = []
    params: list[object] = []
    if metric:
        clauses.append("metric = ?")
        params.append(metric)
    if from_date:
        clauses.append("substr(start_time, 1, 10) >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("substr(start_time, 1, 10) <= ?")
        params.append(to_date)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM observations{where} ORDER BY start_time DESC, id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def query_labs(
    conn: sqlite3.Connection,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 1000,
) -> list[dict[str, object]]:
    from_date, to_date = _date_bounds(from_date, to_date)
    limit = min(max(int(limit), 1), 5000)
    clauses: list[str] = []
    params: list[object] = []
    if from_date:
        clauses.append("substr(collection_time, 1, 10) >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("substr(collection_time, 1, 10) <= ?")
        params.append(to_date)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM lab_results{where} ORDER BY collection_time DESC, id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def query_medications(
    conn: sqlite3.Connection,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 1000,
) -> list[dict[str, object]]:
    from_date, to_date = _date_bounds(from_date, to_date)
    limit = min(max(int(limit), 1), 5000)
    clauses: list[str] = []
    params: list[object] = []
    if to_date:
        clauses.append("substr(start_time, 1, 10) <= ?")
        params.append(to_date)
    if from_date:
        clauses.append("(end_time IS NULL OR substr(end_time, 1, 10) >= ?)")
        params.append(from_date)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM medications{where} ORDER BY start_time DESC, id DESC LIMIT ?", (*params, limit)
    ).fetchall()
    return [dict(row) for row in rows]


def query_health_events(
    conn: sqlite3.Connection,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 1000,
) -> list[dict[str, object]]:
    from_date, to_date = _date_bounds(from_date, to_date)
    limit = min(max(int(limit), 1), 5000)
    clauses: list[str] = []
    params: list[object] = []
    if from_date:
        clauses.append("substr(start_time, 1, 10) >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("substr(start_time, 1, 10) <= ?")
        params.append(to_date)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM health_events{where} ORDER BY start_time DESC, id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(row) for row in rows]


_TRAINING_BODY_PARTS = {"胸", "背", "肩", "手臂", "腿", "其他"}
_TRAINING_BODY_PART_CYCLE = ("胸", "背", "肩", "手臂", "腿")


def next_training_body_part(last_body_part: object) -> str:
    try:
        index = _TRAINING_BODY_PART_CYCLE.index(str(last_body_part or "").strip())
    except ValueError:
        return _TRAINING_BODY_PART_CYCLE[0]
    return _TRAINING_BODY_PART_CYCLE[(index + 1) % len(_TRAINING_BODY_PART_CYCLE)]


def _training_json(value: object, default: object) -> str:
    return canonical_json(default if value is None else value)


def _training_number(value: object, field: str, *, integer: bool = False) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not (number == number and abs(number) != float("inf")) or number < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    if integer and not number.is_integer():
        raise ValueError(f"{field} must be an integer")
    return int(number) if integer else number


def create_training_session(
    conn: sqlite3.Connection,
    payload: dict[str, object],
    *,
    commit: bool = True,
) -> dict[str, object]:
    """Create or idempotently replace a training session and its set records."""
    session_date = str(payload.get("session_date") or payload.get("date") or dt.date.today().isoformat())
    try:
        dt.date.fromisoformat(session_date)
    except ValueError as exc:
        raise ValueError("session_date must be YYYY-MM-DD") from exc
    body_part = str(payload.get("body_part") or "其他").strip()
    if body_part not in _TRAINING_BODY_PARTS:
        raise ValueError("body_part is invalid")
    records = payload.get("records", [])
    if not isinstance(records, list) or len(records) > 500:
        raise ValueError("records must be a list of at most 500 items")
    request_id = str(payload.get("request_id") or "").strip() or None
    if request_id and len(request_id) > 128:
        raise ValueError("request_id must be at most 128 characters")
    source_id = _training_number(payload.get("source_ironbook_id"), "source_ironbook_id", integer=True)
    now = utc_now()
    normalized_records: list[dict[str, object]] = []
    computed_volume = 0.0
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError("each record must be an object")
        name = str(record.get("name") or record.get("standard_name") or record.get("raw_name") or "").strip()
        if not name:
            raise ValueError("each record requires an exercise name")
        if len(name) > 128:
            raise ValueError("exercise name must be at most 128 characters")
        reps = record.get("reps") or []
        rpe = record.get("rpe") or []
        if isinstance(reps, str):
            reps = [part.strip() for part in reps.replace("，", ",").split(",") if part.strip()]
        if isinstance(rpe, str):
            rpe = [part.strip() for part in rpe.replace("，", ",").split(",") if part.strip()]
        if not isinstance(reps, list) or not isinstance(rpe, list):
            raise ValueError("reps and rpe must be lists")
        reps = [_training_number(item, "reps", integer=True) for item in reps]
        rpe = [_training_number(item, "rpe") for item in rpe]
        if any(item is not None and item > 10 for item in rpe):
            raise ValueError("rpe must be between 0 and 10")
        weight = _training_number(record.get("weight_kg"), "weight_kg")
        total_reps = _training_number(record.get("total_reps"), "total_reps", integer=True)
        if total_reps is None and reps:
            total_reps = sum(int(item or 0) for item in reps)
        volume = _training_number(record.get("volume_kg"), "volume_kg")
        if volume is None and weight is not None and total_reps:
            volume = weight * int(total_reps)
        computed_volume += float(volume or 0)
        extra_data = record.get("extra_data") or {}
        if not isinstance(extra_data, (dict, list)):
            raise ValueError("record extra_data must be an object or array")
        normalized_records.append({
            "name": name,
            "raw_name": str(record.get("raw_name") or name)[:128],
            "body_part": str(record.get("body_part") or body_part),
            "source_exercise_id": _training_number(record.get("source_exercise_id"), "source_exercise_id", integer=True),
            "weight_kg": weight,
            "reps": reps,
            "sets_count": _training_number(record.get("sets_count"), "sets_count", integer=True),
            "total_reps": total_reps,
            "volume_kg": volume,
            "rpe": rpe,
            "is_warmup": bool(record.get("is_warmup")),
            "exercise_order": _training_number(record.get("exercise_order"), "exercise_order", integer=True) or index,
            "note": record.get("note"),
            "raw_line": record.get("raw_line"),
            "extra_data": extra_data,
            "source_ironbook_id": _training_number(record.get("source_ironbook_id"), "source_ironbook_id", integer=True),
        })
    extra_data = payload.get("extra_data") or {}
    if not isinstance(extra_data, (dict, list)):
        raise ValueError("extra_data must be an object or array")
    duration = _training_number(payload.get("duration_minutes"), "duration_minutes", integer=True)
    sleep_hours = _training_number(payload.get("sleep_hours"), "sleep_hours")
    stress = _training_number(payload.get("stress_level"), "stress_level", integer=True)
    hrv = _training_number(payload.get("hrv_score"), "hrv_score", integer=True)
    if stress is not None and not 1 <= stress <= 10:
        raise ValueError("stress_level must be between 1 and 10")
    if sleep_hours is not None and sleep_hours > 24:
        raise ValueError("sleep_hours must be at most 24")
    volume = _training_number(payload.get("total_volume_kg"), "total_volume_kg")
    if volume is None:
        volume = computed_volume or None

    transaction = conn if commit else contextlib.nullcontext(conn)
    with transaction:
        existing = None
        update_session_id = _training_number(payload.get("update_session_id"), "update_session_id", integer=True)
        if update_session_id is not None:
            existing = conn.execute(
                "SELECT id,request_id,source_ironbook_id FROM training_sessions WHERE id=?",
                (update_session_id,),
            ).fetchone()
            if existing is None:
                raise ValueError("training session does not exist")
        elif request_id:
            existing = conn.execute("SELECT id FROM training_sessions WHERE request_id=?", (request_id,)).fetchone()
        if update_session_id is None and existing is None and source_id is not None:
            existing = conn.execute("SELECT id FROM training_sessions WHERE source_ironbook_id=?", (source_id,)).fetchone()
        if existing:
            session_id = int(existing["id"])
            if update_session_id is not None:
                request_id = existing["request_id"]
                source_id = existing["source_ironbook_id"]
            conn.execute(
                """UPDATE training_sessions SET session_date=?,weekday=?,body_part=?,sleep_hours=?,stress_level=?,
                   hrv_score=?,total_volume_kg=?,duration_minutes=?,status_note=?,feeling=?,raw_markdown=?,
                   source_file=?,extra_data=?,updated_at=? WHERE id=?""",
                (session_date, payload.get("weekday"), body_part, sleep_hours, stress, hrv, volume, duration,
                 payload.get("status_note"), payload.get("feeling"), payload.get("raw_markdown"),
                 payload.get("source_file"), _training_json(extra_data, {}), now, session_id),
            )
            conn.execute("DELETE FROM training_exercise_records WHERE session_id=?", (session_id,))
        else:
            cursor = conn.execute(
                """INSERT INTO training_sessions
                   (request_id,source_ironbook_id,session_date,weekday,body_part,sleep_hours,stress_level,hrv_score,
                    total_volume_kg,duration_minutes,status_note,feeling,raw_markdown,source_file,extra_data,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (request_id, source_id, session_date, payload.get("weekday"), body_part, sleep_hours, stress, hrv,
                 volume, duration, payload.get("status_note"), payload.get("feeling"), payload.get("raw_markdown"),
                 payload.get("source_file"), _training_json(extra_data, {}), now, now),
            )
            session_id = int(cursor.lastrowid)
        for record in normalized_records:
            source_exercise_id = record["source_exercise_id"]
            exercise = None
            if source_exercise_id is not None:
                exercise = conn.execute(
                    "SELECT id,standard_name,body_part FROM training_exercises WHERE source_ironbook_id=?",
                    (source_exercise_id,),
                ).fetchone()
                if exercise is not None and (
                    str(exercise["standard_name"]) != str(record["name"])
                    or str(exercise["body_part"]) != str(record["body_part"])
                ):
                    raise ValueError(f"source exercise ID {source_exercise_id} conflicts with its catalog entry")
            if exercise is None:
                exercise = conn.execute(
                    "SELECT id,standard_name,body_part,source_ironbook_id FROM training_exercises WHERE standard_name=?",
                    (record["name"],),
                ).fetchone()
                if exercise is not None:
                    existing_source_id = exercise["source_ironbook_id"]
                    if source_exercise_id is not None and str(exercise["body_part"]) != str(record["body_part"]):
                        raise ValueError(f"existing action {record['name']} has a conflicting body part")
                    if source_exercise_id is not None and existing_source_id not in (None, source_exercise_id):
                        raise ValueError(f"action {record['name']} is linked to a different source exercise ID")
                    if source_exercise_id is not None and existing_source_id is None:
                        conn.execute(
                            "UPDATE training_exercises SET source_ironbook_id=?,updated_at=? WHERE id=?",
                            (source_exercise_id, now, exercise["id"]),
                        )
            if exercise is None:
                exercise = conn.execute(
                    """SELECT e.id FROM training_exercise_aliases a
                       JOIN training_exercises e ON e.id=a.exercise_id
                       WHERE a.normalized_alias=?""",
                    (normalize_exercise_name(record["name"]),),
                ).fetchone()
            if exercise:
                exercise_id = int(exercise["id"])
            else:
                cursor = conn.execute(
                    "INSERT INTO training_exercises(source_ironbook_id,standard_name,body_part,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (source_exercise_id, record["name"], record["body_part"], now, now),
                )
                exercise_id = int(cursor.lastrowid)
                conn.execute(
                    "INSERT OR IGNORE INTO training_exercise_aliases(exercise_id,alias_name,normalized_alias,alias_type,source,confirmed_at,created_at) VALUES(?,?,?,?,?,?,?)",
                    (exercise_id, record["name"], normalize_exercise_name(record["name"]), "user_confirmed", "user", now, now),
                )
            conn.execute(
                """INSERT INTO training_exercise_records
                   (source_ironbook_id,session_id,exercise_id,raw_exercise_name,weight_kg,reps,sets_count,total_reps,
                    volume_kg,rpe,is_warmup,exercise_order,note,raw_line,extra_data,created_at,updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (record["source_ironbook_id"], session_id, exercise_id, record["raw_name"], record["weight_kg"],
                 _training_json(record["reps"], []), record["sets_count"], record["total_reps"], record["volume_kg"],
                 _training_json(record["rpe"], []), int(bool(record["is_warmup"])), record["exercise_order"],
                 record["note"], record["raw_line"], _training_json(record["extra_data"], {}), now, now),
            )
    return get_training_session(conn, session_id)  # type: ignore[return-value]


def normalize_exercise_name(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().casefold())


def add_training_exercise_alias(
    conn: sqlite3.Connection,
    exercise_id: int,
    alias_name: str,
) -> dict[str, object]:
    alias_name = alias_name.strip()
    normalized = normalize_exercise_name(alias_name)
    if not alias_name or len(alias_name) > 128 or not normalized:
        raise ValueError("alias_name must contain 1 to 128 non-whitespace characters")
    now = utc_now()
    with conn:
        exercise = conn.execute(
            "SELECT id,standard_name FROM training_exercises WHERE id=?", (exercise_id,)
        ).fetchone()
        if exercise is None:
            raise ValueError("exercise does not exist")
        for other in conn.execute(
            "SELECT id,standard_name FROM training_exercises WHERE id<>?", (exercise_id,)
        ):
            if normalize_exercise_name(str(other["standard_name"])) == normalized:
                raise ValueError("alias conflicts with another exercise's standard name")
        existing = conn.execute(
            "SELECT id,exercise_id,alias_name FROM training_exercise_aliases WHERE normalized_alias=?",
            (normalized,),
        ).fetchone()
        if existing:
            if int(existing["exercise_id"]) != exercise_id:
                raise ValueError("alias is already assigned to another exercise")
            return {"id": int(existing["id"]), "exercise_id": exercise_id,
                    "standard_name": exercise["standard_name"], "alias_name": existing["alias_name"]}
        cursor = conn.execute(
            """INSERT INTO training_exercise_aliases
               (exercise_id,alias_name,normalized_alias,alias_type,source,confirmed_at,created_at)
               VALUES(?,?,?,'user_confirmed','user',?,?)""",
            (exercise_id, alias_name, normalized, now, now),
        )
        return {"id": int(cursor.lastrowid), "exercise_id": exercise_id,
                "standard_name": exercise["standard_name"], "alias_name": alias_name}


def query_training_sessions(
    conn: sqlite3.Connection,
    *,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 80,
    offset: int = 0,
    search: str = "",
) -> list[dict[str, object]]:
    from_date, to_date = _date_bounds(from_date, to_date)
    limit = min(max(int(limit), 1), 200)
    offset = min(max(int(offset), 0), 1_000_000)
    clauses: list[str] = []
    params: list[object] = []
    if from_date:
        clauses.append("s.session_date >= ?")
        params.append(from_date)
    if to_date:
        clauses.append("s.session_date <= ?")
        params.append(to_date)
    search = search.strip()[:128]
    if search:
        clauses.append("(s.session_date LIKE ? OR s.body_part LIKE ? OR s.feeling LIKE ? OR EXISTS (SELECT 1 FROM training_exercise_records r JOIN training_exercises e ON e.id=r.exercise_id LEFT JOIN training_exercise_aliases a ON a.exercise_id=e.id WHERE r.session_id=s.id AND (e.standard_name LIKE ? OR r.raw_exercise_name LIKE ? OR a.alias_name LIKE ?)) )")
        pattern = f"%{search}%"
        params.extend([pattern] * 6)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"""SELECT s.id,s.session_date,s.body_part,s.total_volume_kg,s.duration_minutes,s.feeling,s.sleep_hours,
                   s.stress_level,s.hrv_score,s.created_at,s.updated_at,COUNT(r.id) AS record_count,
                   COALESCE(SUM(CASE WHEN json_extract(r.extra_data,'$.structure')='drop_set_group' THEN 1
                     WHEN EXISTS (SELECT 1 FROM json_each(r.rpe) WHERE CAST(value AS REAL)>=7) THEN 1 ELSE 0 END),0) AS effective_sets
            FROM training_sessions s LEFT JOIN training_exercise_records r ON r.session_id=s.id{where}
            GROUP BY s.id ORDER BY s.session_date DESC,s.id DESC LIMIT ? OFFSET ?""",
        (*params, limit, offset),
    ).fetchall()
    return [dict(row) for row in rows]


def training_analytics(
    conn: sqlite3.Connection,
    *,
    as_of: dt.date | None = None,
    exercise_limit: int = 12,
) -> dict[str, object]:
    """Build transparent training trends from stored sessions and records."""
    as_of = as_of or dt.date.today()
    current_week = as_of - dt.timedelta(days=as_of.weekday())
    first_week = current_week - dt.timedelta(weeks=11)
    from_date = first_week.isoformat()
    sessions = conn.execute(
        """SELECT s.session_date,s.body_part,s.total_volume_kg,
                  COALESCE(SUM(CASE WHEN json_extract(r.extra_data,'$.structure')='drop_set_group' THEN 1
                    WHEN EXISTS (SELECT 1 FROM json_each(r.rpe) WHERE CAST(value AS REAL)>=7) THEN 1 ELSE 0 END),0) AS effective_sets
           FROM training_sessions s LEFT JOIN training_exercise_records r ON r.session_id=s.id
           WHERE s.session_date >= ? AND s.session_date <= ?
           GROUP BY s.id ORDER BY s.session_date""",
        (from_date, as_of.isoformat()),
    ).fetchall()

    weeks: list[dict[str, object]] = []
    for offset in range(12):
        week_start = first_week + dt.timedelta(weeks=offset)
        week_end = min(week_start + dt.timedelta(days=6), as_of)
        weeks.append({
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "sessions": 0,
            "volume_kg": 0.0,
            "effective_sets": 0,
        })
    parts: dict[str, int] = {}
    for session in sessions:
        try:
            day = dt.date.fromisoformat(str(session["session_date"]))
        except ValueError:
            continue
        index = (day - first_week).days // 7
        if 0 <= index < len(weeks):
            week = weeks[index]
            week["sessions"] = int(week["sessions"]) + 1
            week["volume_kg"] = float(week["volume_kg"]) + float(session["total_volume_kg"] or 0)
            week["effective_sets"] = int(week["effective_sets"]) + int(session["effective_sets"] or 0)
        if day >= as_of - dt.timedelta(days=89):
            part = str(session["body_part"] or "其他")
            parts[part] = parts.get(part, 0) + 1

    records = conn.execute(
        """SELECT s.id AS session_id,s.session_date,e.standard_name,
                  MAX(r.weight_kg) AS max_weight_kg,
                  SUM(COALESCE(r.volume_kg,0)) AS volume_kg,
                  SUM(COALESCE(r.sets_count,1)) AS sets_count,
                  COUNT(r.id) AS record_count,
                  GROUP_CONCAT(r.weight_kg || ':' || r.reps || ':' || r.rpe || ':' || r.extra_data, char(10)) AS metric_rows
           FROM training_sessions s
           JOIN training_exercise_records r ON r.session_id=s.id
           JOIN training_exercises e ON e.id=r.exercise_id
           WHERE s.session_date >= ? AND s.session_date <= ?
           GROUP BY s.id,e.id
           ORDER BY s.session_date DESC,e.standard_name COLLATE NOCASE""",
        (from_date, as_of.isoformat()),
    ).fetchall()
    by_exercise: dict[str, list[dict[str, object]]] = {}
    e1rm_by_exercise: dict[str, dict[str, float]] = {}
    for row in records:
        name = str(row["standard_name"])
        row_data = dict(row)
        metric_rows = str(row_data.pop("metric_rows") or "").splitlines()
        by_exercise.setdefault(name, []).append(row_data)
        best_e1rm: float | None = None
        for metric_row in metric_rows:
            weight_raw, reps_raw, rpe_raw, extra_raw = (metric_row.split(":", 3) + [""] * 4)[:4]
            try:
                weight = float(weight_raw)
                reps_values = json.loads(reps_raw)
                rpe_values = json.loads(rpe_raw)
                extra = json.loads(extra_raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not weight or not isinstance(reps_values, list) or not isinstance(rpe_values, list):
                continue
            if isinstance(extra, dict) and extra.get("structure") == "drop_set_group":
                continue
            for index, reps_value in enumerate(reps_values):
                try:
                    reps = int(reps_value)
                    rpe = float(rpe_values[index]) if index < len(rpe_values) and rpe_values[index] is not None else None
                except (TypeError, ValueError):
                    continue
                if reps <= 0:
                    continue
                rir = max(0.0, min(5.0, 10.0 - rpe)) if rpe is not None else 0.0
                estimated = weight * (1 + (reps + rir) / 30)
                best_e1rm = max(best_e1rm or 0.0, estimated)
        if best_e1rm is not None:
            week_start = (dt.date.fromisoformat(str(row_data["session_date"])) - dt.timedelta(
                days=dt.date.fromisoformat(str(row_data["session_date"])).weekday()
            )).isoformat()
            week_values = e1rm_by_exercise.setdefault(name, {})
            week_values[week_start] = max(week_values.get(week_start, 0.0), best_e1rm)
    exercise_trends = [
        {"name": name, "sessions": rows[:8]}
        for name, rows in sorted(
            by_exercise.items(),
            key=lambda item: (str(item[1][0]["session_date"]), item[0]),
            reverse=True,
        )[:max(1, min(exercise_limit, 50))]
    ]
    e1rm_trends = [
        {"name": name, "points": [
            {"week_start": week["week_start"], "e1rm": round(values["week_start"], 1) if "week_start" in values else None}
            for week in weeks
        ]}
        for name, values in sorted(e1rm_by_exercise.items())
    ]
    return {
        "weeks": weeks,
        "body_parts_90d": sorted(parts.items(), key=lambda item: (-item[1], item[0])),
        "exercise_trends": exercise_trends,
        "e1rm_trends": e1rm_trends,
    }


def get_training_session(conn: sqlite3.Connection, session_id: int) -> dict[str, object] | None:
    row = conn.execute("SELECT * FROM training_sessions WHERE id=?", (session_id,)).fetchone()
    if not row:
        return None
    session = dict(row)
    for key in ("extra_data",):
        try:
            session[key] = json.loads(str(session.get(key) or "{}"))
        except json.JSONDecodeError:
            session[key] = {}
    records = conn.execute(
        """SELECT r.*,e.standard_name,e.body_part AS exercise_body_part
           FROM training_exercise_records r JOIN training_exercises e ON e.id=r.exercise_id
           WHERE r.session_id=? ORDER BY r.exercise_order,r.id""",
        (session_id,),
    ).fetchall()
    session["records"] = [dict(record) for record in records]
    for record in session["records"]:
        for key, default in (("reps", []), ("rpe", []), ("extra_data", {})):
            try:
                record[key] = json.loads(str(record.get(key) or canonical_json(default)))
            except json.JSONDecodeError:
                record[key] = default
    return session


def delete_training_session(conn: sqlite3.Connection, session_id: int) -> bool:
    with conn:
        cursor = conn.execute("DELETE FROM training_sessions WHERE id=?", (session_id,))
        return cursor.rowcount > 0


_CONTEXT_METRICS = {
    "hrv": ("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "ms", "average"),
    "resting_heart_rate": ("HKQuantityTypeIdentifierRestingHeartRate", "count/min", "average"),
    "sleep_duration": ("HKCategoryTypeIdentifierSleepAnalysis", "hour", "duration"),
    "steps": ("HKQuantityTypeIdentifierStepCount", "count", "sum"),
    "active_energy": ("HKQuantityTypeIdentifierActiveEnergyBurned", "kcal", "sum"),
}


def _context_metric_rows(
    conn: sqlite3.Connection, name: str, from_date: str | None, to_date: str | None
) -> list[dict[str, object]]:
    source_metric, unit, aggregation = _CONTEXT_METRICS[name]
    rows = query_observations(conn, metric=source_metric, from_date=from_date, to_date=to_date, limit=5000)
    daily: dict[str, list[tuple[float, float]]] = {}
    for row in rows:
        day = str(row.get("start_time") or "")[:10]
        value = row.get("value_num")
        if not day or (value is None and aggregation != "duration"):
            continue
        try:
            numeric = float(value or 0)
        except (TypeError, ValueError):
            continue
        start = row.get("start_time")
        end = row.get("end_time") or start
        duration_hours = 0.0
        if aggregation == "duration":
            try:
                duration_hours = max(
                    0.0,
                    (parse_datetime(end) - parse_datetime(start)).total_seconds() / 3600,
                ) if parse_datetime(start) and parse_datetime(end) else 0.0
            except (AttributeError, TypeError):
                duration_hours = 0.0
        daily.setdefault(day, []).append((numeric, duration_hours))
    values: list[dict[str, object]] = []
    for day in sorted(daily):
        entries = daily[day]
        if aggregation == "average":
            value = sum(item[0] for item in entries) / len(entries)
        elif aggregation == "duration":
            value = sum(item[1] for item in entries)
        else:
            value = sum(item[0] for item in entries)
        values.append({"date": day, "value": round(value, 3), "unit": unit})
    return values


def build_health_context(
    conn: sqlite3.Connection,
    scope: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build a privacy-minimal, deterministic reasoning projection."""
    scope = dict(scope or {})
    preset = str(scope.get("preset") or "custom")
    today = dt.datetime.now(dt.timezone.utc).date()
    if preset == "recovery_30d":
        scope.setdefault("from", (today - dt.timedelta(days=29)).isoformat())
        scope.setdefault("to", today.isoformat())
        scope.setdefault("domains", ["metrics"])
        scope.setdefault("metrics", ["hrv", "resting_heart_rate", "sleep_duration", "steps", "active_energy"])
    elif preset == "clinical_90d":
        scope.setdefault("from", (today - dt.timedelta(days=89)).isoformat())
        scope.setdefault("to", today.isoformat())
        scope.setdefault("domains", ["labs", "medications", "events"])
    elif preset == "labs_all":
        scope.setdefault("domains", ["labs"])
    from_date = scope.get("from")
    to_date = scope.get("to")
    from_date = str(from_date) if from_date else None
    to_date = str(to_date) if to_date else None
    _date_bounds(from_date, to_date)
    domains = [str(item) for item in scope.get("domains", ["metrics"]) if str(item) in {"metrics", "labs", "medications", "events", "training"}]
    if scope.get("include_training") and "training" not in domains:
        domains.append("training")
    context: dict[str, object] = {
        "schema_version": "1",
        "generated_at": utc_now(),
        "scope": {"preset": preset, "from": from_date, "to": to_date, "domains": domains},
        "metrics": {}, "labs": [], "medications": [], "events": [], "training": [],
    }
    if "metrics" in domains:
        requested = scope.get("metrics") or list(_CONTEXT_METRICS)
        metric_values = {}
        for name in requested:
            if str(name) in _CONTEXT_METRICS:
                metric_values[str(name)] = _context_metric_rows(conn, str(name), from_date, to_date)
        context["metrics"] = metric_values
    if "labs" in domains:
        context["labs"] = query_labs(conn, from_date=from_date, to_date=to_date)
    if "medications" in domains:
        context["medications"] = query_medications(conn, from_date=from_date, to_date=to_date)
    if "events" in domains:
        context["events"] = query_health_events(conn, from_date=from_date, to_date=to_date)
    if "training" in domains:
        sessions = query_training_sessions(conn, from_date=from_date, to_date=to_date, limit=100)
        training = []
        for summary in sessions:
            session = get_training_session(conn, int(summary["id"]))
            if not session:
                continue
            training.append({
                "date": session["session_date"],
                "body_part": session["body_part"],
                "duration_minutes": session["duration_minutes"],
                "volume_kg": session["total_volume_kg"],
                "exercises": [{
                    "name": record["standard_name"],
                    "weight_kg": record["weight_kg"],
                    "reps": record["reps"],
                    "rpe": record["rpe"],
                    "sets": record["sets_count"],
                    "structure": (record["extra_data"].get("structure")
                                  if isinstance(record.get("extra_data"), dict) else None),
                } for record in session["records"]],
            })
        context["training"] = training
    return context


def format_health_context_markdown(context: dict[str, object]) -> str:
    """Render the reasoning projection as compact, user-pasteable Markdown."""
    scope = context.get("scope") if isinstance(context.get("scope"), dict) else {}
    scope = scope if isinstance(scope, dict) else {}
    preset = str(scope.get("preset") or "custom")
    period = " to ".join(str(value) for value in (scope.get("from"), scope.get("to")) if value)
    lines = ["# HealthOS Health Context", ""]
    generated_at = context.get("generated_at")
    if generated_at:
        lines.append(f"Generated: {generated_at}")
    lines.append(f"Scope: {preset}")
    if period:
        lines.append(f"Period: {period}")
    lines.extend(["", "Use this as user-provided health context. Do not diagnose; identify uncertainty and ask clarifying questions."])

    metric_labels = {
        "hrv": "HRV", "resting_heart_rate": "Resting heart rate",
        "sleep_duration": "Sleep duration", "steps": "Steps", "active_energy": "Active energy",
    }
    metrics = context.get("metrics") if isinstance(context.get("metrics"), dict) else {}
    if metrics:
        lines.extend(["", "## Metrics"])
        for name in sorted(metrics):
            rows = metrics.get(name)
            if not isinstance(rows, list) or not rows:
                continue
            lines.extend(["", f"### {metric_labels.get(str(name), str(name))}"])
            for row in rows:
                if not isinstance(row, dict):
                    continue
                value = row.get("value")
                unit = str(row.get("unit") or "").strip()
                rendered = f"{value} {unit}".strip()
                lines.append(f"- {str(row.get('date') or '')}: {rendered}")

    labs = context.get("labs") if isinstance(context.get("labs"), list) else []
    if labs:
        lines.extend(["", "## Labs"])
        for row in labs:
            if not isinstance(row, dict):
                continue
            value = row.get("value_num") if row.get("value_num") is not None else row.get("value_text")
            result = " | ".join(str(item) for item in (
                row.get("collection_time"), row.get("test_name"), value,
                row.get("unit"), row.get("flag")
            ) if item not in (None, ""))
            reference = row.get("reference_text")
            if reference:
                result += f" (reference: {reference})"
            lines.append(f"- {result}")

    medications = context.get("medications") if isinstance(context.get("medications"), list) else []
    if medications:
        lines.extend(["", "## Medications"])
        for row in medications:
            if not isinstance(row, dict):
                continue
            dose = " ".join(str(item) for item in (row.get("dose"), row.get("dose_unit")) if item not in (None, ""))
            details = " | ".join(str(item) for item in (
                row.get("drug_name"), dose, row.get("frequency"),
                f"{row.get('start_time')} to {row.get('end_time')}" if row.get("end_time") else row.get("start_time"),
            ) if item not in (None, ""))
            lines.append(f"- {details}")

    events = context.get("events") if isinstance(context.get("events"), list) else []
    if events:
        lines.extend(["", "## Events"])
        for row in events:
            if not isinstance(row, dict):
                continue
            details = " | ".join(str(item) for item in (row.get("start_time"), row.get("event_type"), row.get("title")) if item not in (None, ""))
            notes = str(row.get("notes") or "").replace("\r", " ").replace("\n", " ").strip()
            lines.append(f"- {details}{f' | {notes}' if notes else ''}")

    training = context.get("training") if isinstance(context.get("training"), list) else []
    if training:
        lines.extend(["", "## Strength Training"])
        for session in training:
            if not isinstance(session, dict):
                continue
            details = " | ".join(str(item) for item in (
                session.get("date"), session.get("body_part"),
                f"{session.get('duration_minutes')} min" if session.get("duration_minutes") is not None else None,
                f"{session.get('volume_kg')} kg volume" if session.get("volume_kg") is not None else None,
            ) if item not in (None, ""))
            lines.append(f"- {details}")
            exercises = session.get("exercises") if isinstance(session.get("exercises"), list) else []
            for exercise in exercises:
                if not isinstance(exercise, dict):
                    continue
                reps = ", ".join(str(value) for value in exercise.get("reps", []))
                rpe = ", ".join(str(value) for value in exercise.get("rpe", []))
                metrics = " | ".join(str(item) for item in (
                    f"{exercise.get('weight_kg')} kg" if exercise.get("weight_kg") is not None else None,
                    f"{reps} reps" if reps else None,
                    f"RPE {rpe}" if rpe else None,
                    f"{exercise.get('sets')} sets" if exercise.get("sets") is not None else None,
                    "drop set" if exercise.get("structure") == "drop_set_group" else None,
                ) if item not in (None, ""))
                lines.append(f"  - {exercise.get('name')}{f' | {metrics}' if metrics else ''}")
    return "\n".join(lines).strip() + "\n"


def create_share_snapshot(
    conn: sqlite3.Connection,
    scope: dict[str, object] | None = None,
    *,
    ttl_minutes: int = 15,
    max_reads: int = 5,
    snapshot_format: str = "json",
) -> tuple[str, dict[str, object]]:
    """Materialize an immutable projection and return the plaintext token once."""
    if ttl_minutes not in {5, 15, 60}:
        raise ValueError("ttl_minutes must be 5, 15, or 60")
    if max_reads < 0 or max_reads > 50:
        raise ValueError("max_reads must be 0 (unlimited) or between 1 and 50")
    if snapshot_format not in {"json", "html"}:
        raise ValueError("snapshot_format must be json or html")
    token = secrets.token_urlsafe(32)
    now = dt.datetime.now(dt.timezone.utc)
    expires = now + dt.timedelta(minutes=ttl_minutes)
    context = build_health_context(conn, scope)
    scope_value = dict(scope or {})
    scope_type = str(scope_value.get("preset") or "custom")
    if scope_value.get("include_training"):
        scope_type += "+training"
    conn.execute(
        """INSERT INTO share_snapshots
        (token_hash,created_at,expires_at,scope_type,scope_config,snapshot_payload,
         snapshot_format,max_reads,read_count,created_by)
        VALUES (?,?,?,?,?,?,?,?,0,'owner')""",
        (sha256_text(token), now.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds"),
         scope_type, canonical_json(scope_value), canonical_json(context), snapshot_format, max_reads),
    )
    conn.commit()
    return token, {"created_at": now.isoformat(timespec="seconds"), "expires_at": expires.isoformat(timespec="seconds"), "scope_type": scope_type, "max_reads": max_reads, "snapshot_format": snapshot_format}


def cleanup_expired_share_payloads(
    conn: sqlite3.Connection, *, grace_minutes: int = 15, batch_size: int = 25
) -> int:
    """Remove expired snapshot contents while retaining minimal audit metadata."""
    if batch_size < 1:
        return 0
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=max(0, grace_minutes))
    cursor = conn.execute(
        """UPDATE share_snapshots SET snapshot_payload='{}'
           WHERE id IN (
               SELECT id FROM share_snapshots
               WHERE expires_at <= ? AND snapshot_payload != '{}'
               ORDER BY expires_at LIMIT ?
           )""",
        (cutoff.isoformat(timespec="seconds"), batch_size),
    )
    conn.commit()
    return cursor.rowcount


def consume_share_snapshot(conn: sqlite3.Connection, token: str) -> tuple[int, dict[str, object]]:
    """Consume one read atomically; raises LookupError for all unavailable states."""
    if not token or len(token) > 512:
        raise LookupError("share_not_found")
    token_hash = sha256_text(token)
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute("SELECT * FROM share_snapshots WHERE token_hash = ?", (token_hash,)).fetchone()
    if row is None:
        conn.rollback()
        raise LookupError("share_not_found")
    now = utc_now()
    if row["revoked_at"] or str(row["expires_at"]) <= now or (int(row["max_reads"]) > 0 and int(row["read_count"]) >= int(row["max_reads"])):
        conn.rollback()
        raise LookupError("share_gone")
    updated = conn.execute(
        """UPDATE share_snapshots SET read_count=read_count+1,last_accessed_at=?
           WHERE id=? AND revoked_at IS NULL AND expires_at > ? AND (max_reads = 0 OR read_count < max_reads)""",
        (now, row["id"], now),
    )
    if updated.rowcount != 1:
        conn.rollback()
        raise LookupError("share_gone")
    conn.commit()
    return int(row["id"]), json.loads(row["snapshot_payload"])


def revoke_share_snapshot(conn: sqlite3.Connection, token: str) -> bool:
    now = utc_now()
    cursor = conn.execute(
        "UPDATE share_snapshots SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
        (now, sha256_text(token)),
    )
    conn.commit()
    return cursor.rowcount == 1


def revoke_share_snapshot_id(conn: sqlite3.Connection, snapshot_id: int) -> bool:
    cursor = conn.execute(
        "UPDATE share_snapshots SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
        (utc_now(), int(snapshot_id)),
    )
    conn.commit()
    return cursor.rowcount == 1


@contextlib.contextmanager
def iter_xml_source(path: Path):
    """Yield a binary stream and close it when iteration is finished."""
    if path.suffix.lower() != ".zip":
        with path.open("rb") as stream:
            yield stream
        return
    with zipfile.ZipFile(path) as archive:
        candidates = [
            info for info in archive.infolist()
            if info.filename.lower().endswith(".xml") and not info.filename.lower().endswith("export_cda.xml")
        ]
        if not candidates:
            raise ValueError("ZIP does not contain an Apple Health XML export")
        # The main export is the largest XML member; CDA is intentionally excluded.
        member = max(candidates, key=lambda info: info.file_size)
        with archive.open(member, "r") as stream:
            yield stream


def import_xml(path: str | Path, db_path: str | Path, max_records: int | None = None, progress_every: int = 100_000) -> dict[str, int]:
    source = Path(path)
    conn = init_db(db_path, bulk=True)
    conn.execute("DROP TRIGGER IF EXISTS observations_count_after_insert")
    conn.execute("DROP TRIGGER IF EXISTS observations_count_after_delete")
    seen_at = utc_now()
    counts = {"records": 0, "new_samples": 0, "workouts": 0, "new_workouts": 0, "activity_summaries": 0}
    try:
        with iter_xml_source(source) as stream:
            stack: list[ET.Element] = []
            for event, element in ET.iterparse(stream, events=("start", "end")):
                if event == "start":
                    stack.append(element)
                    continue
                tag = element.tag.rsplit("}", 1)[-1]
                attrs = dict(element.attrib)
                record_counted = False
                if tag == "Record" and "type" in attrs and "startDate" in attrs:
                    counts["records"] += 1
                    record_counted = True
                    if insert_sample(conn, record_payload(attrs, extract_metadata(element)), seen_at):
                        counts["new_samples"] += 1
                elif tag == "Workout" and "workoutActivityType" in attrs:
                    counts["workouts"] += 1
                    if insert_workout(conn, attrs, seen_at, extract_metadata(element)):
                        counts["new_workouts"] += 1
                elif tag == "ActivitySummary" and "dateComponents" in attrs:
                    counts["activity_summaries"] += 1
                    insert_activity_summary(conn, attrs, seen_at)
                # Keep nested metadata available until its Record/Workout
                # parent has been processed. Clear only direct HealthData
                # children so the multi-GB export stays streaming.
                if len(stack) == 2:
                    element.clear()
                stack.pop()
                if record_counted and counts["records"] % 100_000 == 0:
                    conn.commit()
                if record_counted and progress_every and counts["records"] % progress_every == 0:
                    print(json.dumps(counts, ensure_ascii=False), file=sys.stderr, flush=True)
                if max_records and counts["records"] >= max_records:
                    break
        conn.commit()
        if conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "off":
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.commit()
    finally:
        conn.rollback()
        refresh_observation_count(conn)
        install_observation_count_triggers(conn)
        conn.commit()
        conn.close()
    return counts


def validate_batch_id(batch_id: str) -> None:
    if not 1 <= len(batch_id) <= 160 or not re.fullmatch(r"[A-Za-z0-9._-]+", batch_id):
        raise ValueError("batch_id must contain only letters, numbers, '.', '_' or '-'")


def archive_body(body: bytes, raw_dir: Path, batch_id: str) -> tuple[Path, str]:
    validate_batch_id(batch_id)
    raw_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(body).hexdigest()
    path = raw_dir / f"{batch_id}-{digest[:16]}.json.gz"
    if not path.exists():
        with gzip.open(path, "wb", compresslevel=6) as stream:
            stream.write(body)
    return path, digest


def ingest_batch(payload: dict[str, object], conn: sqlite3.Connection, raw_dir: Path) -> dict[str, object]:
    batch_id = str(payload.get("batch_id") or "")
    if not batch_id or not isinstance(payload.get("samples"), list):
        raise ValueError("batch_id and samples[] are required")
    validate_batch_id(batch_id)
    if len(payload["samples"]) > 50_000:
        raise ValueError("samples[] exceeds the 50000 item limit")
    for item in payload["samples"]:
        if not isinstance(item, dict):
            raise ValueError("each sample must be an object")
        required = ("metric", "start_time", "end_time")
        if any(not item.get(key) for key in required):
            raise ValueError("each sample requires metric, start_time and end_time")
    body = canonical_json(payload).encode("utf-8")
    raw_path, payload_hash = archive_body(body, raw_dir, batch_id)
    received_at = utc_now()
    existing = conn.execute("SELECT payload_hash FROM ingest_batches WHERE batch_id=?", (batch_id,)).fetchone()
    if existing is not None:
        if existing[0] and existing[0] != payload_hash:
            raise ValueError("batch_id already exists with a different payload")
        conn.execute("UPDATE ingest_batches SET received_at=? WHERE batch_id=?", (received_at, batch_id))
    else:
        conn.execute(
            """INSERT INTO ingest_batches(batch_id,received_at,device,window_start,window_end,sample_count,schema_version,raw_path,payload_hash)
            VALUES(?,?,?,?,?,?,?,?,?)""",
            (batch_id, received_at, payload.get("device"), payload.get("window_start"), payload.get("window_end"),
             len(payload["samples"]), payload.get("schema_version", SCHEMA_VERSION), str(raw_path), payload_hash),
        )
    inserted = 0
    for item in payload["samples"]:
        attrs = {
            "type": str(item["metric"]), "value": str(item.get("value")) if item.get("value") is not None else None,
            "unit": item.get("unit"), "startDate": str(item["start_time"]), "endDate": str(item["end_time"]),
            "creationDate": item.get("creation_time"), "sourceName": item.get("source_name") or item.get("source"),
            "device": item.get("device_name"),
        }
        sample = record_payload({key: value for key, value in attrs.items() if value is not None})
        if insert_sample(conn, sample, received_at):
            inserted += 1
    conn.commit()
    return {"batch_id": batch_id, "received": len(payload["samples"]), "inserted": inserted, "raw_path": str(raw_path)}


def parse_datetime(value: object) -> dt.datetime | None:
    """Parse the ISO-like timestamps used by Ironbook and Apple Health."""
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S"):
            try:
                parsed = dt.datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def workout_public_payload(row: sqlite3.Row | dict[str, object]) -> dict[str, object]:
    """Expose the useful Apple workout fields without leaking internal hashes."""
    return {
        key: row[key]
        for key in (
            "id", "activity_type", "start_time", "end_time", "duration",
            "duration_unit", "total_distance", "total_distance_unit",
            "total_energy", "total_energy_unit", "source_name", "device_name",
        )
        if key in row.keys()
    }


def fetch_ironbook_sessions(base_url: str, timeout_seconds: float) -> list[dict[str, object]]:
    """Read Ironbook sessions through its existing read-only API."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HEALTHOS_IRONBOOK_BASE_URL must be an http(s) URL")
    url = base_url.rstrip("/") + "/api/sessions?limit=100"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise RuntimeError(f"Ironbook returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ironbook returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Ironbook is unreachable") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Ironbook returned an invalid sessions payload")
    return [item for item in payload if isinstance(item, dict)]


def fetch_ironbook_session_page(
    base_url: str,
    timeout_seconds: float,
    *,
    offset: int = 0,
    page_size: int = 50,
) -> dict[str, object]:
    """Read one session page, with local paging fallback for the legacy API."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HEALTHOS_IRONBOOK_BASE_URL must be an http(s) URL")
    if not 1 <= page_size <= 100 or offset < 0 or offset % page_size:
        raise ValueError("Ironbook session page offset or size is invalid")
    params = urllib.parse.urlencode({
        "limit": 100,
        "offset": offset,
        "format": "page",
    })
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/sessions?" + params,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise RuntimeError(f"Ironbook returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ironbook returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Ironbook is unreachable") from exc

    if isinstance(payload, dict):
        raw_items = payload.get("items")
        raw_total = payload.get("total")
        if (
            not isinstance(raw_items, list)
            or not isinstance(raw_total, int)
            or isinstance(raw_total, bool)
            or raw_total < 0
            or payload.get("offset") != offset
        ):
            raise RuntimeError("Ironbook returned invalid paginated sessions")
        if any(not isinstance(item, dict) for item in raw_items):
            raise RuntimeError("Ironbook returned malformed items in paginated sessions")
        items = raw_items[:page_size]
        return {
            "items": items,
            "offset": offset,
            "total": raw_total,
            "has_next": offset + len(items) < raw_total,
            "api_cap_reached": False,
        }

    if not isinstance(payload, list):
        raise RuntimeError("Ironbook returned an invalid sessions payload")
    if any(not isinstance(item, dict) for item in payload):
        raise RuntimeError("Ironbook returned malformed items in sessions")
    visible_items = payload
    page_items = visible_items[offset:offset + page_size] if offset < 100 else []
    api_cap_reached = len(visible_items) >= 100 and offset + page_size >= 100
    total = len(visible_items) if len(visible_items) < 100 else None
    return {
        "items": page_items,
        "offset": offset,
        "total": total,
        "has_next": offset + len(page_items) < len(visible_items) or (
            len(visible_items) >= 100 and offset < 100 - page_size
        ),
        "api_cap_reached": api_cap_reached,
    }


def fetch_ironbook_session_history(
    base_url: str,
    timeout_seconds: float,
    *,
    page_size: int = 50,
) -> dict[str, object]:
    """Read all sessions when the source supports pages, or report legacy coverage."""
    sessions: list[dict[str, object]] = []
    offset = 0
    source_total: int | None = None
    paginated_source: bool | None = None
    api_cap_reached = False

    while True:
        page = fetch_ironbook_session_page(
            base_url, timeout_seconds, offset=offset, page_size=page_size
        )
        items = page["items"]
        if not isinstance(items, list):
            raise RuntimeError("Ironbook returned an invalid sessions page")
        if paginated_source is None:
            paginated_source = page.get("total") is not None
        elif paginated_source != (page.get("total") is not None):
            raise RuntimeError("Ironbook changed pagination format during history read")

        page_total = page.get("total")
        if paginated_source:
            if not isinstance(page_total, int) or isinstance(page_total, bool):
                raise RuntimeError("Ironbook returned invalid paginated history metadata")
            if source_total is None:
                source_total = page_total
            elif source_total != page_total:
                raise RuntimeError("Ironbook session total changed during history read")
        sessions.extend(items)
        api_cap_reached = api_cap_reached or bool(page.get("api_cap_reached"))

        if not page.get("has_next"):
            break
        if not items:
            raise RuntimeError("Ironbook returned an empty page before the end of history")
        if len(items) != page_size:
            raise RuntimeError("Ironbook returned a short page before the end of history")
        offset += len(items)

    ids = [item.get("id") for item in sessions]
    valid_ids = all(isinstance(value, int) and not isinstance(value, bool) for value in ids)
    unique_ids = len(set(ids)) == len(ids) if valid_ids else False
    complete = (
        not api_cap_reached
        and valid_ids
        and unique_ids
        and (len(sessions) == source_total if paginated_source else len(sessions) < 100)
    )
    return {
        "sessions": sessions,
        "full_history_confirmed": complete,
        "source_total": source_total if paginated_source else None,
        "source_limit": None if paginated_source else 100,
        "paginated_source": bool(paginated_source),
        "api_cap_reached": api_cap_reached,
    }


def fetch_ironbook_exercise_suggestions(
    base_url: str,
    timeout_seconds: float,
    query: str,
    body_part: str,
) -> list[dict[str, str]]:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HEALTHOS_IRONBOOK_BASE_URL must be an http(s) URL")
    query = query.strip()[:80]
    if body_part not in _TRAINING_BODY_PARTS:
        raise ValueError("body_part is invalid")
    params = urllib.parse.urlencode({"q": query, "body_part": body_part, "limit": 20})
    request = urllib.request.Request(
        base_url.rstrip("/") + "/api/exercises/suggest?" + params,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ironbook returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Ironbook is unreachable") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Ironbook returned an invalid exercise suggestions payload")
    suggestions: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        name = item.get("standard_name")
        if isinstance(name, str) and name.strip():
            suggestions.append({
                "standard_name": name.strip()[:128],
                "body_part": str(item.get("body_part") or body_part)[:32],
            })
    return suggestions[:30]


def fetch_ironbook_last_exercise(
    base_url: str,
    timeout_seconds: float,
    name: str,
) -> dict[str, object]:
    """Return a minimal, read-only previous-performance summary from Ironbook."""
    name = name.strip()[:128]
    if not name:
        raise ValueError("exercise name is required")
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HEALTHOS_IRONBOOK_BASE_URL must be an http(s) URL")
    path = "/api/exercises/last?" + urllib.parse.urlencode({"name": name})
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            if response.status != 200:
                raise RuntimeError(f"Ironbook returned HTTP {response.status}")
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ironbook returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Ironbook is unreachable") from exc
    if payload is None:
        return {"session_date": None, "sets": []}
    if not isinstance(payload, dict):
        raise RuntimeError("Ironbook returned an invalid last-exercise response")
    raw_sets = payload.get("sets")
    if raw_sets is None:
        raw_sets = []
    if not isinstance(raw_sets, list):
        raise RuntimeError("Ironbook returned invalid last-exercise sets")

    def values(value: object) -> list[object]:
        if not isinstance(value, list):
            return []
        return [item for item in value[:20] if isinstance(item, (int, float, str)) and not isinstance(item, bool)]

    sets: list[dict[str, object]] = []
    for item in raw_sets[:8]:
        if not isinstance(item, dict):
            continue
        extra = item.get("extra_data")
        clean_extra: dict[str, object] = {}
        if isinstance(extra, dict) and extra.get("structure") == "drop_set_group":
            clean_extra["structure"] = "drop_set_group"
            segments = extra.get("segments")
            if isinstance(segments, list):
                clean_extra["segments"] = [
                    {key: segment.get(key) for key in ("weight_kg", "reps") if isinstance(segment.get(key), (int, float, str)) and not isinstance(segment.get(key), bool)}
                    for segment in segments[:4] if isinstance(segment, dict)
                ]
        sets.append({
            "weight_kg": item.get("weight_kg") if isinstance(item.get("weight_kg"), (int, float, str)) and not isinstance(item.get("weight_kg"), bool) else None,
            "reps": values(item.get("reps")),
            "rpe": values(item.get("rpe")),
            "extra_data": clean_extra,
        })
    session_date = payload.get("session_date")
    return {
        "session_date": session_date[:32] if isinstance(session_date, str) else None,
        "sets": sets,
    }


def fetch_ironbook_session_details(
    base_url: str,
    sessions: list[dict[str, object]],
    timeout_seconds: float,
) -> list[dict[str, object]]:
    """Expand summaries with Ironbook's existing per-session read endpoint."""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HEALTHOS_IRONBOOK_BASE_URL must be an http(s) URL")
    expanded: list[dict[str, object]] = []
    for session in sessions:
        session_id = session.get("id")
        if session_id is None:
            expanded.append(session)
            continue
        url = base_url.rstrip("/") + f"/api/sessions/{urllib.parse.quote(str(session_id), safe='')}"
        request = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                if response.status != 200:
                    raise RuntimeError(f"Ironbook returned HTTP {response.status} for session {session_id}")
                detail = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Ironbook returned HTTP {exc.code} for session {session_id}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise RuntimeError(f"Ironbook session {session_id} is unreachable") from exc
        if not isinstance(detail, dict):
            raise RuntimeError(f"Ironbook returned an invalid session {session_id} payload")
        if str(detail.get("id")) != str(session_id):
            raise RuntimeError(f"Ironbook returned a mismatched session ID for {session_id}")
        expanded.append(detail)
    return expanded


def request_ironbook_json(
    base_url: str,
    path: str,
    timeout_seconds: float,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HEALTHOS_IRONBOOK_BASE_URL must be an http(s) URL")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        base_url.rstrip("/") + path,
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Ironbook returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Ironbook is unreachable") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Ironbook returned an invalid response")
    return result


def save_ironbook_training_session(
    base_url: str,
    timeout_seconds: float,
    payload: dict[str, object],
    *,
    request_id: str,
    session_id: int | None = None,
) -> dict[str, object]:
    """Write through to Ironbook using its natural key so retries converge."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", request_id):
        raise ValueError("请求标识无效，请刷新训练页面后重试。")
    session_date = str(payload.get("session_date") or "")
    try:
        dt.date.fromisoformat(session_date)
    except ValueError as exc:
        raise ValueError("session_date must be YYYY-MM-DD") from exc
    body_part = str(payload.get("body_part") or "").strip()
    if body_part not in _TRAINING_BODY_PARTS:
        raise ValueError("body_part is invalid")
    records = payload.get("records")
    if not isinstance(records, list) or not records or len(records) > 500:
        raise ValueError("请至少填写一个动作组，且不超过 500 组。")
    for index, record in enumerate(records, start=1):
        if not isinstance(record, dict):
            raise ValueError(f"第 {index} 组格式无效。")
        name = str(record.get("name") or record.get("raw_name") or "").strip()
        if not name:
            raise ValueError(f"第 {index} 组缺少动作名称。")
        extra = record.get("extra_data") or {}
        if not isinstance(extra, dict):
            raise ValueError(f"第 {index} 组的附加数据格式不兼容 Ironbook。")
        if extra.get("structure") == "drop_set_group":
            segments = extra.get("segments")
            if not isinstance(segments, list) or not segments:
                raise ValueError(f"第 {index} 组递减组至少需要一个分段。")
            normalized_segments: list[dict[str, int | float]] = []
            for segment_index, segment in enumerate(segments, start=1):
                if not isinstance(segment, dict):
                    raise ValueError(f"第 {index} 组第 {segment_index} 个递减段格式无效。")
                segment_weight = _training_number(segment.get("weight_kg"), f"第 {index} 组第 {segment_index} 段重量")
                segment_reps = _training_number(segment.get("reps"), f"第 {index} 组第 {segment_index} 段次数", integer=True)
                if segment_weight is None:
                    raise ValueError(f"第 {index} 组第 {segment_index} 个递减段缺少重量。")
                if segment_reps is None or segment_reps < 1:
                    raise ValueError(f"第 {index} 组第 {segment_index} 个递减段次数必须大于 0。")
                normalized_segments.append({"weight_kg": segment_weight, "reps": segment_reps})
            normalized_extra = dict(extra)
            normalized_extra.update({
                "source": "ironbook",
                "structure": "drop_set_group",
                "drop_set_group": extra.get("drop_set_group") or f"第 {index} 大组",
                "segments": normalized_segments,
            })
            record["name"] = name
            record["weight_kg"] = None
            record["reps"] = [segment["reps"] for segment in normalized_segments]
            record["rpe"] = []
            record["sets_count"] = 1
            record["total_reps"] = sum(segment["reps"] for segment in normalized_segments)
            record["volume_kg"] = sum(segment["weight_kg"] * segment["reps"] for segment in normalized_segments)
            record["extra_data"] = normalized_extra
            continue
        if record.get("weight_kg") in (None, ""):
            raise ValueError(f"第 {index} 组缺少重量；Ironbook 当前接口要求记录重量。")
        weight = _training_number(record.get("weight_kg"), f"第 {index} 组重量")
        if weight is None:
            raise ValueError(f"第 {index} 组重量无效。")
        reps = record.get("reps")
        if not isinstance(reps, list) or not reps:
            raise ValueError(f"第 {index} 组缺少次数；Ironbook 当前接口要求记录次数。")
        normalized_reps = [
            _training_number(value, f"第 {index} 组次数", integer=True)
            for value in reps
        ]
        if any(value is None for value in normalized_reps):
            raise ValueError(f"第 {index} 组次数无效。")
        rpe = record.get("rpe")
        if not isinstance(rpe, list) or not rpe:
            raise ValueError(f"第 {index} 组缺少 RPE；Ironbook 当前接口要求记录 RPE。")
        normalized_rpe = [_training_number(value, f"第 {index} 组 RPE") for value in rpe]
        if any(value is None or value > 10 for value in normalized_rpe):
            raise ValueError(f"第 {index} 组 RPE 必须在 0 到 10 之间。")
        record["name"] = name
        record["weight_kg"] = weight
        record["reps"] = normalized_reps
        record["rpe"] = normalized_rpe

    if session_id is not None:
        existing = request_ironbook_json(
            base_url,
            f"/api/sessions/{session_id}",
            timeout_seconds,
        )
        if str(existing.get("session_date") or "") != session_date or str(existing.get("body_part") or "") != body_part:
            raise ValueError("为避免在 Ironbook 生成重复训练，暂不支持通过 HealthOS 修改已有训练的日期或部位。")
        ironbook_source = str(existing.get("source_file") or "")
        if not ironbook_source:
            raise ValueError("Ironbook 原记录缺少 source_file，HealthOS 暂不能安全更新它。")
        preserved_weekday = payload.get("weekday", existing.get("weekday"))
        preserved_status_note = payload.get("status_note", existing.get("status_note"))
        preserved_raw_markdown = payload.get("raw_markdown", existing.get("raw_markdown"))
    else:
        ironbook_source = f"healthos-request-{request_id}"
        preserved_weekday = payload.get("weekday")
        preserved_status_note = payload.get("status_note")
        preserved_raw_markdown = payload.get("raw_markdown")
    fields: dict[str, object] = {
        "session_date": session_date,
        "weekday": preserved_weekday,
        "body_part": body_part,
        "sleep_hours": payload.get("sleep_hours"),
        "stress_level": payload.get("stress_level"),
        "hrv_score": payload.get("hrv_score"),
        "total_volume_kg": payload.get("total_volume_kg"),
        "duration_minutes": payload.get("duration_minutes"),
        "status_note": preserved_status_note,
        "raw_markdown": preserved_raw_markdown,
        "feeling": payload.get("feeling"),
        "source_file": ironbook_source,
        "extra_data": payload.get("extra_data") or {},
    }
    create_payload: dict[str, object] = {
        "date": session_date,
        "weekday": fields["weekday"],
        "body_part": body_part,
        "sleep_hours": fields["sleep_hours"],
        "stress_level": fields["stress_level"],
        "hrv_score": fields["hrv_score"],
        "total_volume_kg": fields["total_volume_kg"],
        "duration_minutes": fields["duration_minutes"],
        "status_note": fields["status_note"],
        "raw_markdown": fields["raw_markdown"],
        "feeling": fields["feeling"],
        "source_file": ironbook_source,
        "extra_data": fields["extra_data"],
        "records": records,
    }
    result = request_ironbook_json(
        base_url,
        "/api/sessions",
        timeout_seconds,
        method="POST",
        payload=create_payload,
    )
    if session_id is not None and result.get("id") != session_id:
        raise RuntimeError("Ironbook returned a different session after update; check Ironbook before retrying.")
    return result


def delete_ironbook_training_session(base_url: str, timeout_seconds: float, session_id: int) -> None:
    result = request_ironbook_json(
        base_url,
        f"/api/sessions/{session_id}",
        timeout_seconds,
        method="DELETE",
    )
    if result.get("ok") is not True or result.get("deleted") != session_id:
        raise RuntimeError("Ironbook did not confirm the training deletion")


def analyze_ironbook_training_migration(
    conn: sqlite3.Connection,
    sessions: list[dict[str, object]],
    *,
    source_full_history_confirmed: bool | None = None,
    source_total: int | None = None,
    source_limit: int | None = 100,
    paginated_source: bool = False,
) -> dict[str, object]:
    """Return a read-only reconciliation report for Ironbook session payloads."""
    source_fingerprint = hashlib.sha256(
        canonical_json(sessions).encode("utf-8")
    ).hexdigest()
    session_ids: list[int] = []
    record_ids: list[int] = []
    missing_session_fields: dict[str, int] = {}
    missing_record_fields: dict[str, int] = {}
    unmapped_names: dict[str, int] = {}
    catalog_names: dict[str, int] = {}
    catalog_body_parts: dict[str, Counter[str]] = {}
    catalog_missing_body_part: Counter[str] = Counter()
    source_standardized_records = 0
    records_without_source_standard_name = 0
    record_count = 0
    volume_sum = 0.0
    session_volume_count = 0
    malformed_sessions = 0
    malformed_records = 0

    def missing(target: dict[str, int], fields: tuple[str, ...], item: dict[str, object]) -> None:
        for field in fields:
            if item.get(field) is None or item.get(field) == "":
                target[field] = target.get(field, 0) + 1

    for session in sessions:
        source_id = session.get("id")
        if isinstance(source_id, int) and not isinstance(source_id, bool):
            session_ids.append(source_id)
        else:
            malformed_sessions += 1
        missing(missing_session_fields, ("id", "session_date", "body_part"), session)
        session_volume = session.get("total_volume_kg")
        if isinstance(session_volume, (int, float)) and not isinstance(session_volume, bool):
            volume_sum += float(session_volume)
            session_volume_count += 1
        records = session.get("records")
        if not isinstance(records, list):
            missing_record_fields["records"] = missing_record_fields.get("records", 0) + 1
            continue
        for record in records:
            if not isinstance(record, dict):
                malformed_records += 1
                continue
            record_count += 1
            record_id = record.get("id")
            if isinstance(record_id, int) and not isinstance(record_id, bool):
                record_ids.append(record_id)
            else:
                malformed_records += 1
            missing(missing_record_fields, ("id", "raw_exercise_name", "reps", "sets_count"), record)
            raw_name = record.get("raw_exercise_name")
            source_standard_name = record.get("standard_name")
            if isinstance(source_standard_name, str) and source_standard_name.strip():
                source_standardized_records += 1
                source_standard_name = source_standard_name.strip()
                normalized = normalize_exercise_name(source_standard_name)
                match = conn.execute(
                    """SELECT 1 FROM training_exercises WHERE standard_name=? COLLATE NOCASE
                       UNION ALL
                       SELECT 1 FROM training_exercise_aliases WHERE normalized_alias=?
                       LIMIT 1""",
                    (source_standard_name, normalized),
                ).fetchone()
                if match is None:
                    catalog_names[source_standard_name] = catalog_names.get(source_standard_name, 0) + 1
                    source_body_part = record.get("body_part")
                    if isinstance(source_body_part, str) and source_body_part.strip():
                        catalog_body_parts.setdefault(source_standard_name, Counter())[source_body_part.strip()] += 1
                    else:
                        catalog_missing_body_part[source_standard_name] += 1
            else:
                records_without_source_standard_name += 1
                if isinstance(raw_name, str) and raw_name.strip():
                    normalized = normalize_exercise_name(raw_name)
                    match = conn.execute(
                        """SELECT 1 FROM training_exercises WHERE standard_name=? COLLATE NOCASE
                           UNION ALL
                           SELECT 1 FROM training_exercise_aliases WHERE normalized_alias=?
                           LIMIT 1""",
                        (raw_name.strip(), normalized),
                    ).fetchone()
                    if match is None:
                        unmapped_names[raw_name.strip()] = unmapped_names.get(raw_name.strip(), 0) + 1

    existing_sessions = 0
    existing_records = 0
    for source_id in set(session_ids):
        existing_sessions += conn.execute(
            "SELECT 1 FROM training_sessions WHERE source_ironbook_id=?", (source_id,)
        ).fetchone() is not None
    for source_id in set(record_ids):
        existing_records += conn.execute(
            "SELECT 1 FROM training_exercise_records WHERE source_ironbook_id=?", (source_id,)
        ).fetchone() is not None

    duplicate_sessions = sum(count > 1 for count in Counter(session_ids).values())
    duplicate_records = sum(count > 1 for count in Counter(record_ids).values())
    payload_is_valid = (
        len(sessions) < 100
        and malformed_sessions == 0
        and duplicate_sessions == 0
        and len(session_ids) == len(sessions)
    )
    if source_full_history_confirmed is None:
        full_history_confirmed = payload_is_valid
    else:
        full_history_confirmed = (
            source_full_history_confirmed
            and malformed_sessions == 0
            and duplicate_sessions == 0
            and len(session_ids) == len(sessions)
            and (source_total is None or source_total == len(sessions))
        )
    limitations = []
    if not full_history_confirmed:
        limitations.append(
            "Ironbook history coverage is incomplete or could not be confirmed; "
            "this report may not cover full history."
        )
    return {
        "read_only": True,
        "scope": {
            "sessions_in_payload": len(sessions),
            "source_fingerprint": source_fingerprint,
            "source_api_max_sessions": source_limit,
            "source_total": source_total,
            "paginated_source": paginated_source,
            "coverage": "all_current_sessions" if full_history_confirmed else "up_to_100_unconfirmed",
            "full_history_confirmed": full_history_confirmed,
        },
        "sessions": {
            "valid_source_ids": len(session_ids),
            "missing_required_fields": missing_session_fields,
            "duplicate_source_ids": duplicate_sessions,
            "existing_in_healthos": existing_sessions,
            "with_volume": session_volume_count,
            "total_volume_kg": round(volume_sum, 2),
        },
        "records": {
            "count": record_count,
            "valid_source_ids": len(record_ids),
            "malformed_records": malformed_records,
            "missing_required_fields": missing_record_fields,
            "duplicate_source_ids": duplicate_records,
            "existing_in_healthos": existing_records,
            "source_standardized_records": source_standardized_records,
            "records_without_source_standard_name": records_without_source_standard_name,
            "catalog_creation_record_count": sum(catalog_names.values()),
            "catalog_creation_exercise_names": [
                {
                    "name": name,
                    "record_count": count,
                    "body_part_counts": dict(sorted(catalog_body_parts.get(name, Counter()).items())),
                    "missing_body_part_record_count": catalog_missing_body_part.get(name, 0),
                }
                for name, count in sorted(catalog_names.items(), key=lambda item: item[0].casefold())
            ],
            "unmapped_exercise_names": [
                {"name": name, "record_count": count}
                for name, count in sorted(unmapped_names.items(), key=lambda item: item[0].casefold())
            ],
            "unmapped_record_count": sum(unmapped_names.values()),
        },
        "malformed_sessions": malformed_sessions,
        "limitations": limitations,
    }


def migrate_ironbook_training_sessions(
    conn: sqlite3.Connection,
    sessions: list[dict[str, object]],
    *,
    expected_fingerprint: str,
    source_full_history_confirmed: bool,
    source_total: int | None = None,
    backup_path: str | Path | None = None,
) -> dict[str, object]:
    """Import a preflight-approved Ironbook snapshot atomically and idempotently.

    This function intentionally accepts an already-fetched snapshot. Callers must
    obtain it through the read-only source adapter, retain the approved fingerprint,
    and never pass a legacy/capped payload as an approved snapshot.
    """
    if not source_full_history_confirmed:
        raise ValueError("Ironbook full history is not confirmed")
    if not isinstance(expected_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_fingerprint):
        raise ValueError("expected_fingerprint must be a SHA-256 hex digest")
    actual_fingerprint = hashlib.sha256(canonical_json(sessions).encode("utf-8")).hexdigest()
    if not hmac.compare_digest(actual_fingerprint, expected_fingerprint):
        raise ValueError("Ironbook source fingerprint changed; rerun migration preflight")
    if source_total is not None and source_total != len(sessions):
        raise ValueError("Ironbook source total does not match the approved snapshot")

    session_ids: list[int] = []
    record_ids: list[int] = []
    exercise_ids_to_names: dict[int, str] = {}
    names_to_parts: dict[str, str] = {}
    normalized_names: dict[str, str] = {}
    payloads: list[dict[str, object]] = []
    for session in sessions:
        if not isinstance(session, dict):
            raise ValueError("Ironbook session payload is malformed")
        session_id = session.get("id")
        if not isinstance(session_id, int) or isinstance(session_id, bool) or session_id <= 0:
            raise ValueError("Ironbook session ID is invalid")
        if session_id in session_ids:
            raise ValueError("Ironbook session IDs are not unique")
        session_ids.append(session_id)
        session_date = session.get("session_date")
        body_part = session.get("body_part")
        if (
            not isinstance(session_date, str)
            or not isinstance(body_part, str)
            or body_part.strip() not in _TRAINING_BODY_PARTS
        ):
            raise ValueError("Ironbook session is missing date or body part")
        records = session.get("records")
        if not isinstance(records, list):
            raise ValueError(f"Ironbook session {session_id} has invalid records")
        mapped_records: list[dict[str, object]] = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError(f"Ironbook session {session_id} has a malformed record")
            record_id = record.get("id")
            if not isinstance(record_id, int) or isinstance(record_id, bool) or record_id <= 0:
                raise ValueError("Ironbook record ID is invalid")
            if record_id in record_ids:
                raise ValueError("Ironbook record IDs are not unique")
            record_ids.append(record_id)
            standard_name = record.get("standard_name")
            record_body_part = record.get("body_part")
            if not isinstance(standard_name, str) or not standard_name.strip():
                raise ValueError(f"Ironbook record {record_id} has no standard name")
            if not isinstance(record_body_part, str) or record_body_part.strip() not in _TRAINING_BODY_PARTS:
                raise ValueError(f"Ironbook record {record_id} has an invalid body part")
            name = standard_name.strip()[:128]
            part = record_body_part.strip()
            source_exercise_id = record.get("exercise_id")
            if source_exercise_id is not None:
                if not isinstance(source_exercise_id, int) or isinstance(source_exercise_id, bool) or source_exercise_id <= 0:
                    raise ValueError(f"Ironbook record {record_id} has an invalid exercise ID")
                prior_exercise_name = exercise_ids_to_names.get(source_exercise_id)
                if prior_exercise_name is not None and prior_exercise_name != name:
                    raise ValueError(f"Ironbook exercise ID {source_exercise_id} maps to multiple standard names")
                exercise_ids_to_names[source_exercise_id] = name
            prior_part = names_to_parts.get(name)
            if prior_part is not None and prior_part != part:
                raise ValueError(f"Ironbook action {name} has conflicting body parts")
            normalized_name = normalize_exercise_name(name)
            prior_name = normalized_names.get(normalized_name)
            if prior_name is not None and prior_name != name:
                raise ValueError(f"Ironbook actions {prior_name} and {name} normalize to the same name")
            normalized_names[normalized_name] = name
            existing_exercise = conn.execute(
                "SELECT id,body_part FROM training_exercises WHERE standard_name=? COLLATE NOCASE",
                (name,),
            ).fetchone()
            if existing_exercise is not None and str(existing_exercise["body_part"]) != part:
                raise ValueError(f"Existing HealthOS action {name} has a conflicting body part")
            if existing_exercise is None:
                alias_match = conn.execute(
                    "SELECT e.standard_name FROM training_exercise_aliases a "
                    "JOIN training_exercises e ON e.id=a.exercise_id WHERE a.normalized_alias=?",
                    (normalized_name,),
                ).fetchone()
                if alias_match is not None:
                    raise ValueError(
                        f"Ironbook standard action {name} conflicts with HealthOS alias "
                        f"for {alias_match['standard_name']}"
                    )
            names_to_parts[name] = part
            mapped_records.append({
                "name": name,
                "raw_name": str(record.get("raw_exercise_name") or name)[:128],
                "body_part": part,
                "source_ironbook_id": record_id,
                "source_exercise_id": source_exercise_id,
                "weight_kg": record.get("weight_kg"),
                "reps": record.get("reps") or [],
                "sets_count": record.get("sets_count"),
                "total_reps": record.get("total_reps"),
                "volume_kg": record.get("volume_kg"),
                "rpe": record.get("rpe") or [],
                "is_warmup": bool(record.get("is_warmup")),
                "exercise_order": record.get("exercise_order"),
                "note": record.get("note"),
                "raw_line": record.get("raw_line"),
                "extra_data": record.get("extra_data") or {},
            })
        payloads.append({
            "source_ironbook_id": session_id,
            "session_date": session_date,
            "weekday": session.get("weekday"),
            "body_part": body_part.strip(),
            "sleep_hours": session.get("sleep_hours"),
            "stress_level": session.get("stress_level"),
            "hrv_score": session.get("hrv_score"),
            "total_volume_kg": session.get("total_volume_kg"),
            "duration_minutes": session.get("duration_minutes"),
            "status_note": session.get("status_note"),
            "feeling": session.get("feeling"),
            "raw_markdown": session.get("raw_markdown"),
            "source_file": session.get("source_file") or "ironbook",
            "extra_data": session.get("extra_data") or {},
            "records": mapped_records,
        })

    before = int(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0])
    migration_started_at = utc_now()
    migration_run_id: int | None = None
    with conn:
        for payload in payloads:
            create_training_session(conn, payload, commit=False)
        source_session_set = set(session_ids)
        source_record_set = set(record_ids)
        target_session_set = {
            int(row[0]) for row in conn.execute(
                "SELECT source_ironbook_id FROM training_sessions WHERE source_ironbook_id IS NOT NULL"
            )
        }
        target_record_set = {
            int(row[0]) for row in conn.execute(
                "SELECT source_ironbook_id FROM training_exercise_records WHERE source_ironbook_id IS NOT NULL"
            )
        }
        source_exercise_set = set(exercise_ids_to_names)
        target_exercise_set = {
            int(row[0]) for row in conn.execute(
                "SELECT source_ironbook_id FROM training_exercises WHERE source_ironbook_id IS NOT NULL"
            )
        }
        missing_session_ids = source_session_set - target_session_set
        missing_record_ids = source_record_set - target_record_set
        missing_exercise_ids = source_exercise_set - target_exercise_set
        if missing_session_ids or missing_record_ids or missing_exercise_ids:
            raise RuntimeError("HealthOS migration reconciliation failed before commit")
        after = int(conn.execute("SELECT COUNT(*) FROM training_sessions").fetchone()[0])
        cursor = conn.execute(
            """INSERT INTO migration_runs
               (source_kind,source_fingerprint,backup_path,started_at,completed_at,
                sessions_seen,records_seen,exercises_seen,status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                "ironbook",
                actual_fingerprint,
                str(backup_path) if backup_path is not None else None,
                migration_started_at,
                utc_now(),
                len(payloads),
                len(record_ids),
                len(source_exercise_set),
                "completed",
            ),
        )
        migration_run_id = int(cursor.lastrowid)
    return {
        "source_fingerprint": actual_fingerprint,
        "sessions_seen": len(payloads),
        "records_seen": len(record_ids),
        "sessions_created_or_updated": len(payloads),
        "catalog_names": len(names_to_parts),
        "sessions_before": before,
        "sessions_after": after,
        "sessions_reconciled": len(source_session_set),
        "records_reconciled": len(source_record_set),
        "exercises_reconciled": len(source_exercise_set),
        "missing_sessions": 0,
        "missing_records": 0,
        "missing_exercises": 0,
        "migration_run_id": migration_run_id,
    }


def render_ironbook_training_analytics(stats: dict[str, object]) -> str:
    summary = stats.get("summary") if isinstance(stats.get("summary"), dict) else {}
    performance = stats.get("performance_summary") if isinstance(stats.get("performance_summary"), dict) else {}

    def value(item: object, suffix: str = "") -> str:
        if item is None or item == "":
            return "—"
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            formatted = f"{item:,.1f}".rstrip("0").rstrip(".")
            return html.escape(formatted + suffix)
        return html.escape(str(item))

    metrics = (
        ("总训练次数", summary.get("total_sessions"), ""),
        ("近 30 天", summary.get("sessions_last_30d"), " 次"),
        ("平均每周", summary.get("avg_sessions_per_week"), " 次"),
        ("累计有效组", summary.get("total_effective_sets"), " 组"),
        ("最近训练 e1RM", performance.get("latest_session_score"), " kg"),
        ("近 6 次平均 e1RM", performance.get("avg_last_6_sessions"), " kg"),
    )
    content = "<div class='panel'><p class='eyebrow'>Ironbook 数据</p><h2>训练分析</h2>"
    content += f"<p class='muted'>数据范围：{value(summary.get('date_range'))} · 平均时长 {value(summary.get('avg_duration_minutes'), ' 分钟')}</p>"
    content += "<div class='grid'>" + "".join(
        f"<div class='stat'><span>{label}</span><strong>{value(item, suffix)}</strong></div>"
        for label, item, suffix in metrics
    ) + "</div>"

    weekly = stats.get("weekly_frequency")
    content += "<h3>每周训练趋势</h3><div class='table-wrap'><table><thead><tr><th>周起始</th><th>训练次数</th><th>训练量（kg）</th></tr></thead><tbody>"
    if isinstance(weekly, list) and weekly:
        for row in weekly[:12]:
            if isinstance(row, dict):
                content += "<tr>" + "".join(
                    f"<td>{value(row.get(key), suffix)}</td>"
                    for key, suffix in (("week_start", ""), ("sessions", ""), ("volume_kg", ""))
                ) + "</tr>"
    else:
        content += "<tr><td colspan='3' class='empty'>暂无训练趋势</td></tr>"
    content += "</tbody></table></div>"

    progression = stats.get("exercise_progression")
    content += "<h3>动作进步与最高重量</h3><div class='table-wrap'><table><thead><tr><th>动作</th><th>部位</th><th>训练次数</th><th>最高重量（kg）</th><th>最近重量（kg）</th><th>估算 1RM（kg）</th><th>最近训练</th></tr></thead><tbody>"
    if isinstance(progression, list) and progression:
        for row in progression[:16]:
            if isinstance(row, dict):
                content += "<tr>" + "".join(
                    f"<td>{value(row.get(key), suffix)}</td>"
                    for key, suffix in (
                        ("standard_name", ""), ("body_part", ""), ("sessions", ""),
                        ("max_weight", ""), ("last_weight", ""), ("max_e1rm", ""), ("last_date", ""),
                    )
                ) + "</tr>"
    else:
        content += "<tr><td colspan='7' class='empty'>暂无重复训练动作数据</td></tr>"
    content += "</tbody></table></div>"

    trackers = stats.get("performance_exercises")
    content += "<h3>动作表现追踪</h3><div class='table-wrap'><table><thead><tr><th>动作</th><th>部位</th><th>状态</th><th>当前 e1RM（kg）</th><th>近期变化（kg）</th><th>训练次数</th><th></th></tr></thead><tbody>"
    if isinstance(trackers, list) and trackers:
        for row in trackers[:16]:
            if isinstance(row, dict):
                content += "<tr>" + "".join(
                    f"<td>{value(row.get(key), suffix)}</td>"
                    for key, suffix in (
                        ("standard_name", ""), ("body_part", ""), ("status", ""),
                        ("latest_score", ""), ("trend_delta", ""), ("sessions", ""),
                    )
                ) + f"<td>{'新高' if row.get('is_pr') else ''}</td></tr>"
    else:
        content += "<tr><td colspan='7' class='empty'>暂无足够的动作表现数据</td></tr>"
    content += "</tbody></table></div><p class='muted'>e1RM 使用 Ironbook 的估算口径；递减组计入训练量和有效组，不参与 e1RM 估算。</p></div>"
    return content


def render_ironbook_migration_preflight(report: dict[str, object]) -> str:
    scope = report.get("scope") if isinstance(report.get("scope"), dict) else {}
    sessions = report.get("sessions") if isinstance(report.get("sessions"), dict) else {}
    records = report.get("records") if isinstance(report.get("records"), dict) else {}
    full_history = scope.get("full_history_confirmed") is True
    coverage_text = "已确认覆盖当前全部训练历史" if full_history else "训练历史覆盖尚未确认"
    count_label = "源端总数" if scope.get("source_total") is not None else "当前读取"
    session_count = scope.get("source_total")
    if session_count is None:
        session_count = scope.get("sessions_in_payload", 0)
    source_limit = scope.get("source_api_max_sessions")
    source_note = (
        f"旧接口读取上限：{html.escape(str(source_limit))} 节。"
        if source_limit is not None else "源接口提供分页总数。"
    )
    content = "<p><a href='/training?source=ironbook'>&larr; 返回力量训练</a></p>"
    content += "<div class='panel'><p class='eyebrow'>只读检查</p><h2>Ironbook 训练迁移预检</h2>"
    fingerprint = scope.get("source_fingerprint")
    fingerprint_note = (
        f"源数据指纹：<code>{html.escape(str(fingerprint))}</code>。"
        if isinstance(fingerprint, str) and fingerprint else ""
    )
    content += f"<p><strong>{coverage_text}</strong></p><p class='muted'>{count_label}：{html.escape(str(session_count))} 节。{source_note}{fingerprint_note}本检查不会导入或修改数据。</p></div>"
    content += "<div class='grid'>"
    for label, value in (
        ("训练课次", sessions.get("valid_source_ids", 0)),
        ("重复训练 ID", sessions.get("duplicate_source_ids", 0)),
        ("异常训练记录", report.get("malformed_sessions", 0)),
        ("已存在于 HealthOS", sessions.get("existing_in_healthos", 0)),
        ("动作组", records.get("count", 0)),
        ("重复动作组 ID", records.get("duplicate_source_ids", 0)),
        ("异常动作组", records.get("malformed_records", 0)),
        ("Ironbook 已标准化动作组", records.get("source_standardized_records", 0)),
        ("待加入 HealthOS 动作目录", records.get("catalog_creation_record_count", 0)),
        (
            "待加入动作名称",
            len(records.get("catalog_creation_exercise_names", []))
            if isinstance(records.get("catalog_creation_exercise_names"), list) else 0,
        ),
        ("需要人工映射动作组", records.get("unmapped_record_count", 0)),
    ):
        content += f"<div class='panel stat'><span>{label}</span><strong>{html.escape(str(value))}</strong></div>"
    content += "</div>"
    missing_sessions = sessions.get("missing_required_fields")
    missing_records = records.get("missing_required_fields")
    for title, missing in (("训练课次缺失字段", missing_sessions), ("动作组缺失字段", missing_records)):
        content += f"<div class='panel'><h2>{title}</h2>"
        if isinstance(missing, dict) and missing:
            content += "<table><thead><tr><th>字段</th><th>数量</th></tr></thead><tbody>"
            content += "".join(
                f"<tr><td>{html.escape(str(key))}</td><td>{html.escape(str(value))}</td></tr>"
                for key, value in sorted(missing.items())
            ) + "</tbody></table>"
        else:
            content += "<p class='muted'>无</p>"
        content += "</div>"
    catalog_names = records.get("catalog_creation_exercise_names")
    content += "<div class='panel'><h2>待加入 HealthOS 动作目录</h2>"
    if isinstance(catalog_names, list) and catalog_names:
        content += "<table><thead><tr><th>Ironbook 标准动作</th><th>动作组数</th><th>Ironbook 动作分类</th><th>目录准备状态</th></tr></thead><tbody>"
        for item in catalog_names:
            if isinstance(item, dict):
                body_parts = item.get("body_part_counts")
                body_parts = body_parts if isinstance(body_parts, dict) else {}
                missing_parts = item.get("missing_body_part_record_count", 0)
                classification = "、".join(
                    f"{html.escape(str(name))}（{html.escape(str(count))}）"
                    for name, count in sorted(body_parts.items(), key=lambda pair: str(pair[0]).casefold())
                ) or "未提供"
                needs_review = len(body_parts) != 1 or bool(missing_parts)
                status = "需人工确认" if needs_review else "源端分类明确"
                if missing_parts:
                    classification += f"；缺分类 {html.escape(str(missing_parts))} 组"
                content += (
                    f"<tr><td>{html.escape(str(item.get('name') or ''))}</td>"
                    f"<td>{html.escape(str(item.get('record_count') or 0))}</td>"
                    f"<td>{classification}</td><td>{status}</td></tr>"
                )
        content += "</tbody></table>"
    else:
        content += "<p class='muted'>无</p>"
    content += "</div>"
    unmapped = records.get("unmapped_exercise_names")
    content += "<div class='panel'><h2>需要人工映射的动作名称</h2>"
    if isinstance(unmapped, list) and unmapped:
        content += "<table><thead><tr><th>动作名称</th><th>动作组数</th></tr></thead><tbody>"
        for item in unmapped:
            if isinstance(item, dict):
                content += f"<tr><td>{html.escape(str(item.get('name') or ''))}</td><td>{html.escape(str(item.get('record_count') or 0))}</td></tr>"
        content += "</tbody></table>"
    else:
        content += "<p class='muted'>无</p>"
    content += "</div>"
    limitations = report.get("limitations")
    if isinstance(limitations, list) and limitations:
        content += "<div class='panel error'><h2>覆盖限制</h2><ul>" + "".join(
            f"<li>{html.escape(str(item))}</li>" for item in limitations
        ) + "</ul></div>"
    return content


def load_workouts_for_dates(conn: sqlite3.Connection, dates: set[str]) -> list[dict[str, object]]:
    if not dates:
        return []
    placeholders = ",".join("?" for _ in dates)
    rows = conn.execute(
        f"""SELECT id, activity_type, start_time, end_time, duration, duration_unit,
                   total_distance, total_distance_unit, total_energy, total_energy_unit,
                   source_name, device_name
            FROM workouts
            WHERE substr(start_time, 1, 10) IN ({placeholders})
            ORDER BY start_time ASC, id ASC""",
        tuple(sorted(dates)),
    ).fetchall()
    return [workout_public_payload(row) for row in rows]


def session_timing(session: dict[str, object]) -> tuple[dt.datetime | None, dt.datetime | None]:
    extra = session.get("extra_data")
    if isinstance(extra, str):
        try:
            extra = json.loads(extra)
        except json.JSONDecodeError:
            extra = None
    timing = extra.get("training_timing") if isinstance(extra, dict) else None
    if not isinstance(timing, dict):
        return None, None
    return parse_datetime(timing.get("started_at")), parse_datetime(timing.get("ended_at"))


def training_record_weight_reps(record: dict[str, object]) -> tuple[str, str]:
    extra = record.get("extra_data")
    if isinstance(extra, dict) and extra.get("structure") == "drop_set_group":
        segments = extra.get("segments")
        if isinstance(segments, list) and segments:
            valid_segments = [segment for segment in segments if isinstance(segment, dict)]
            if valid_segments:
                weights = [str(segment.get("weight_kg") if segment.get("weight_kg") is not None else "") for segment in valid_segments]
                reps = [str(segment.get("reps") if segment.get("reps") is not None else "") for segment in valid_segments]
                return " → ".join(weights), " → ".join(reps)
    weight = record.get("weight_kg")
    reps = record.get("reps")
    return (
        str(weight) if weight is not None else "",
        ", ".join(map(str, reps)) if isinstance(reps, list) else "",
    )


def workout_match(session: dict[str, object], workout: dict[str, object]) -> dict[str, object]:
    """Score a same-day Apple workout against one Ironbook session."""
    score = 0
    reasons: list[str] = []
    is_strength = workout.get("activity_type") == "HKWorkoutActivityTypeTraditionalStrengthTraining"
    if is_strength:
        score += 50
        reasons.append("strength_training_type")
    else:
        score += 10
        reasons.append("same_day_other_workout")

    started, ended = session_timing(session)
    workout_started = parse_datetime(workout.get("start_time"))
    workout_ended = parse_datetime(workout.get("end_time"))
    if started and ended and workout_started and workout_ended:
        start_diff = abs((started - workout_started).total_seconds())
        end_diff = abs((ended - workout_ended).total_seconds())
        latest_start = max(started, workout_started)
        earliest_end = min(ended, workout_ended)
        overlap = max(0.0, (earliest_end - latest_start).total_seconds())
        if overlap > 0:
            score += 40
            reasons.append("time_overlap")
        elif start_diff <= 30 * 60:
            score += 25
            reasons.append("start_within_30m")
        elif start_diff <= 2 * 60 * 60:
            score += 10
            reasons.append("start_within_2h")
        if start_diff <= 10 * 60 and end_diff <= 10 * 60:
            score += 10
            reasons.append("time_close")

    session_duration = parse_number(str(session.get("duration_minutes")))
    workout_duration = parse_number(str(workout.get("duration")))
    if workout_duration is not None and str(workout.get("duration_unit") or "").lower().startswith("sec"):
        workout_duration /= 60
    if session_duration is not None and workout_duration is not None:
        duration_diff = abs(session_duration - workout_duration)
        if duration_diff <= 10:
            score += 10
            reasons.append("duration_within_10m")
        elif duration_diff <= 20:
            score += 5
            reasons.append("duration_within_20m")

    return {
        "score": score,
        "reasons": reasons,
        "workout": workout,
        "is_strength": is_strength,
    }


def training_link_payload(
    sessions: list[dict[str, object]],
    workouts: list[dict[str, object]],
) -> dict[str, object]:
    by_date: dict[str, list[dict[str, object]]] = {}
    for workout in workouts:
        date_text = str(workout.get("start_time") or "")[:10]
        by_date.setdefault(date_text, []).append(workout)

    links: list[dict[str, object]] = []
    for session in sorted(sessions, key=lambda item: (str(item.get("session_date") or ""), int(item.get("id") or 0))):
        session_date = str(session.get("session_date") or "")
        candidates = [workout_match(session, workout) for workout in by_date.get(session_date, [])]
        candidates.sort(key=lambda item: (int(item["is_strength"]), int(item["score"])), reverse=True)
        strength_candidates = [item for item in candidates if item["is_strength"]]
        ranked = strength_candidates or candidates
        selected = ranked[0] if ranked else None
        if not selected:
            status = "unmatched"
            reason = "no_same_day_workout"
        elif not strength_candidates:
            status = "unmatched"
            reason = "no_strength_training_workout"
        elif int(selected["score"]) < 50:
            status = "ambiguous"
            reason = "weak_time_match"
        else:
            status = "matched"
            reason = "+".join(selected["reasons"])
        links.append({
            "ironbook_session": session,
            "apple_workout": selected["workout"] if selected and status != "unmatched" else None,
            "match": {
                "status": status,
                "reason": reason,
                "score": selected["score"] if selected else 0,
                "same_day_workout_count": len(candidates),
                "strength_workout_count": len(strength_candidates),
                "candidate_workout_ids": [item["workout"]["id"] for item in ranked],
            },
        })

    # Do not silently attach one Apple workout to multiple Ironbook sessions.
    selected_ids: dict[int, list[dict[str, object]]] = {}
    for link in links:
        workout = link.get("apple_workout")
        if isinstance(workout, dict) and isinstance(workout.get("id"), int):
            selected_ids.setdefault(workout["id"], []).append(link)
    for workout_id, duplicate_links in selected_ids.items():
        if len(duplicate_links) <= 1:
            continue
        for link in duplicate_links:
            link["match"]["status"] = "ambiguous"  # type: ignore[index]
            link["match"]["reason"] = "multiple_ironbook_sessions_same_workout"  # type: ignore[index]

    counts = {"matched": 0, "ambiguous": 0, "unmatched": 0}
    for link in links:
        counts[str(link["match"]["status"])] += 1  # type: ignore[index]
    return {"sessions": links, "summary": {"ironbook_sessions": len(links), **counts}}


class HealthOSHandler(http.server.BaseHTTPRequestHandler):
    server_version = f"HealthOS/{SCHEMA_VERSION}"

    def log_message(self, fmt: str, *args: object) -> None:
        # Never log request bodies or Authorization headers.
        path = self.path.split("?", 1)[0]
        if path.startswith("/share/"):
            path = "/share/<REDACTED>"
        sys.stderr.write(f"{self.address_string()} {self.command} {path} {fmt % args}\n")

    def authorized(self) -> bool:
        expected = os.environ.get("HEALTHOS_API_TOKEN")
        if not expected and os.environ.get("HEALTHOS_API_TOKEN_FILE"):
            try:
                expected = Path(os.environ["HEALTHOS_API_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
            except OSError:
                expected = None
        supplied = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")

    def send_json(self, status: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def wants_json(self) -> bool:
        return "application/json" in self.headers.get("Accept", "").lower()

    def send_html(self, status: int, body: str, *, headers: list[tuple[str, str]] | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Robots-Tag", "noindex, nofollow, noarchive")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'self' 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'")
        if headers:
            for key, value in headers:
                self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def send_javascript(self, status: int, body: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/javascript; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, private")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str, *, cookie: str | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def cookie_secure(self) -> bool:
        return os.environ.get("HEALTHOS_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}

    def session_cookie(self, session_id: str, *, max_age: int = SESSION_TTL_SECONDS) -> str:
        secure = "; Secure" if self.cookie_secure() else ""
        return f"{SESSION_COOKIE}={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}{secure}"

    def request_cookie(self, name: str) -> str | None:
        raw = self.headers.get("Cookie", "")
        for item in raw.split(";"):
            key, separator, value = item.strip().partition("=")
            if separator and key == name:
                return value
        return None

    def owner_session(self) -> dict[str, object] | None:
        return session_data(self.request_cookie(SESSION_COOKIE))

    def require_owner(self) -> dict[str, object] | None:
        session = self.owner_session()
        if session is None:
            self.redirect("/login")
        return session

    def page(self, title: str, content: str, *, session: dict[str, object] | None = None) -> str:
        csrf = html.escape(str(session.get("csrf", ""))) if session else ""
        nav = ""
        if session:
            nav = f"""
            <nav aria-label='主导航'><a href='/'><span class='nav-mark'>总览</span></a><a href='/data?domain=metrics'>健康数据</a>
            <a href='/training'>力量训练</a><a href='/add'>手动录入</a><a href='/shares'>分享与导出</a>
            <form method='post' action='/logout' class='inline'><input type='hidden' name='csrf' value='{csrf}'><button class='nav-logout'>退出登录</button></form></nav>
            """
        return f"""<!doctype html>
        <html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
        <title>{html.escape(title)} · HealthOS</title>
        <style>
        :root {{ color-scheme:light; font-family:Inter,-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',sans-serif; color:#182b31; background:#f3f7f5; font-synthesis:none; }}
        * {{ box-sizing:border-box; }} body {{ margin:0; min-height:100vh; }}
        header {{ background:#fff; border-bottom:1px solid #dce7e2; }} header .bar {{ max-width:1180px; margin:0 auto; min-height:68px; padding:.7rem 1.25rem; display:flex; align-items:center; justify-content:space-between; gap:1.5rem; }}
        .brand {{ display:flex; gap:.7rem; align-items:center; color:#183b35; text-decoration:none; font-size:1.05rem; font-weight:750; white-space:nowrap; }} .brand-mark {{ width:30px; height:30px; display:grid; place-items:center; border-radius:8px; background:#dff1e8; color:#167154; font-size:.8rem; font-weight:800; }}
        nav {{ display:flex; gap:.35rem; align-items:center; flex-wrap:wrap; }} nav a {{ color:#45605a; text-decoration:none; font-size:.92rem; padding:.55rem .75rem; border-radius:5px; }} nav a:hover {{ color:#126849; background:#edf6f1; }} .nav-mark {{ display:none; }}
        main {{ max-width:1180px; margin:0 auto; padding:2rem 1.25rem 4rem; }} h1 {{ margin:0 0 1.4rem; font-size:1.65rem; line-height:1.25; letter-spacing:0; font-weight:720; color:#183b35; }} h2 {{ margin:0 0 .8rem; font-size:1.05rem; letter-spacing:0; }} p {{ line-height:1.65; }}
        .panel {{ background:#fff; border:1px solid #dce7e2; border-radius:6px; padding:1.2rem; margin:1rem 0; box-shadow:0 1px 2px #183b3508; }}
        .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:.8rem; }} .stat {{ min-height:112px; display:flex; flex-direction:column; justify-content:space-between; color:#526b64; font-size:.9rem; }} .stat strong {{ display:block; color:#183b35; font-size:1.8rem; font-weight:700; font-variant-numeric:tabular-nums; }}
        table {{ width:100%; border-collapse:collapse; background:white; font-size:.9rem; }} .panel:has(table) {{ overflow-x:auto; padding:0; }} th,td {{ padding:.75rem .85rem; border-bottom:1px solid #e8efec; text-align:left; vertical-align:top; }} th {{ color:#4b655e; background:#f7faf8; font-weight:650; white-space:nowrap; }} tr:last-child td {{ border-bottom:0; }}
        label {{ display:block; color:#314b44; font-size:.9rem; font-weight:620; margin:.85rem 0 .35rem; }} input,select,textarea {{ width:100%; box-sizing:border-box; padding:.65rem .75rem; color:#182b31; background:white; border:1px solid #cbdad4; border-radius:4px; font:inherit; }} input:focus,select:focus,textarea:focus {{ outline:3px solid #d5eee2; border-color:#3b9872; }} textarea {{ line-height:1.55; }}
        button {{ padding:.62rem .95rem; border:0; border-radius:4px; background:#167154; color:white; font:inherit; font-weight:620; cursor:pointer; }} button:hover {{ background:#105c43; }} button:focus-visible,a:focus-visible {{ outline:3px solid #9dd9bb; outline-offset:2px; }} button.secondary,.nav-logout {{ background:#edf4f0; color:#33554a; }} button.secondary:hover,.nav-logout:hover {{ background:#e0ece6; }} .nav-logout {{ padding:.5rem .7rem; }}
        .inline {{ display:inline; }} .muted {{ color:#657b74; }} .error {{ color:#a22d2d; }} .tabs {{ display:flex; gap:.35rem; margin:0 0 1rem; padding-bottom:.55rem; border-bottom:1px solid #dce7e2; flex-wrap:wrap; }} .tabs a {{ color:#45605a; padding:.5rem .75rem; text-decoration:none; border-radius:4px; }} .tabs a:hover {{ background:#e7f2ec; color:#126849; }}
        .eyebrow {{ color:#43816b; font-size:.78rem; font-weight:700; letter-spacing:.04em; margin:0 0 .45rem; }} .empty {{ color:#657b74; padding:1.25rem; text-align:center; }} .form-grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:0 1rem; }}
        @media(max-width:700px) {{ header .bar {{ align-items:flex-start; flex-direction:column; gap:.35rem; padding:.75rem 1rem; }} nav {{ width:100%; gap:.1rem; }} nav a {{ padding:.48rem .55rem; }} .nav-mark {{ display:inline; }} main {{ padding:1.4rem 1rem 3rem; }} h1 {{ font-size:1.45rem; }} .panel {{ padding:1rem; }} .form-grid {{ grid-template-columns:1fr; }} }}
        </style></head><body><header><div class='bar'><a class='brand' href='/'><span class='brand-mark'>H</span><span>HealthOS</span></a>{nav}</div></header><main><h1>{html.escape(title)}</h1>{content}</main></body></html>"""

    def owner_counts(self, conn: sqlite3.Connection) -> dict[str, object]:
        counts = {
            "observations": conn.execute(
                "SELECT observations FROM owner_dashboard_counts WHERE id=1"
            ).fetchone()[0]
        }
        for key, table in (
            ("lab_results", "lab_results"),
            ("medications", "medications"), ("health_events", "health_events"),
            ("training_sessions", "training_sessions"),
        ):
            counts[key] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        counts["active_shares"] = conn.execute(
            "SELECT COUNT(*) FROM share_snapshots WHERE revoked_at IS NULL AND expires_at > ?",
            (utc_now(),),
        ).fetchone()[0]
        counts["last_import"] = conn.execute("SELECT MAX(received_at) FROM ingest_batches").fetchone()[0]
        return counts

    def render_owner_get(self, parsed: urllib.parse.ParseResult, session: dict[str, object]) -> None:
        conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
        try:
            if parsed.path == "/training-demo":
                self.redirect("/training")
                return
            if parsed.path == "/training":
                demo_path = Path(__file__).with_name("training.html")
                try:
                    training_html = demo_path.read_text(encoding="utf-8")
                except OSError:
                    self.send_html(503, self.page("力量训练", "<div class='panel error'>训练页面暂不可用。</div>", session=session))
                    return
                training_html = training_html.replace("__CSRF_TOKEN__", html.escape(str(session.get("csrf", "")), quote=True))
                self.send_html(200, training_html)
                return
            if parsed.path == "/training-ui.js":
                try:
                    training_script = Path(__file__).with_name("training-ui.js").read_text(encoding="utf-8")
                except OSError:
                    self.send_javascript(503, "")
                    return
                self.send_javascript(200, training_script)
                return
            if parsed.path == "/":
                counts = self.owner_counts(conn)
                content = "<p class='eyebrow'>个人健康档案</p><div class='grid'>"
                labels = {
                    "observations": "健康指标记录", "lab_results": "检验结果",
                    "medications": "用药记录", "health_events": "健康事件",
                    "training_sessions": "训练课次",
                    "active_shares": "有效分享链接",
                }
                for key, label in labels.items():
                    content += f"<div class='panel stat'><span>{label}</span><strong>{counts[key]}</strong></div>"
                content += "</div><div class='panel'><h2>系统状态</h2><p><strong style='color:#167154'>运行正常</strong></p>"
                content += f"<p class='muted'>最近导入：{html.escape(str(counts['last_import'] or '暂无记录'))}</p></div>"
                self.send_html(200, self.page("健康总览", content, session=session))
                return
            if parsed.path == "/data":
                domain = urllib.parse.parse_qs(parsed.query).get("domain", ["metrics"])[0]
                domain = domain if domain in {"metrics", "labs", "medications", "events"} else "metrics"
                links = "".join(
                    f"<a href='/data?domain={name}'>{label}</a>"
                    for name, label in (("metrics", "健康指标"), ("labs", "检验结果"), ("medications", "用药记录"), ("events", "健康事件"))
                )
                content = f"<div class='tabs'>{links}</div>"
                if domain == "metrics":
                    rows = conn.execute(
                        "SELECT metric,value_num,value_text,unit,start_time,end_time,source FROM observations ORDER BY start_time DESC LIMIT 100"
                    ).fetchall()
                    headers = ("指标", "数值", "单位", "开始时间", "结束时间", "来源")
                    values = [
                        (row["metric"], row["value_num"] if row["value_num"] is not None else row["value_text"], row["unit"], row["start_time"], row["end_time"], row["source"])
                        for row in rows
                    ]
                elif domain == "labs":
                    rows = conn.execute("SELECT collection_time,test_name,canonical_name,value_num,value_text,unit,flag,institution FROM lab_results ORDER BY collection_time DESC LIMIT 100").fetchall()
                    headers = ("采样时间", "项目", "标准名称", "结果", "单位", "标记", "机构")
                    values = [(r["collection_time"], r["test_name"], r["canonical_name"], r["value_num"] if r["value_num"] is not None else r["value_text"], r["unit"], r["flag"], r["institution"]) for r in rows]
                elif domain == "medications":
                    rows = conn.execute("SELECT drug_name,generic_name,dose,dose_unit,frequency,start_time,end_time,reason FROM medications ORDER BY start_time DESC LIMIT 100").fetchall()
                    headers = ("药品", "通用名", "剂量", "频率", "开始时间", "结束时间", "用途")
                    values = [(r["drug_name"], r["generic_name"], f"{r['dose']} {r['dose_unit'] or ''}".strip(), r["frequency"], r["start_time"], r["end_time"], r["reason"]) for r in rows]
                else:
                    rows = conn.execute("SELECT event_type,start_time,end_time,title,notes,source FROM health_events ORDER BY start_time DESC LIMIT 100").fetchall()
                    headers = ("类型", "开始时间", "结束时间", "标题", "备注", "来源")
                    values = [(r["event_type"], r["start_time"], r["end_time"], r["title"], r["notes"], r["source"]) for r in rows]
                content += "<div class='panel'><table><thead><tr>" + "".join(f"<th>{html.escape(str(header))}</th>" for header in headers) + "</tr></thead><tbody>"
                content += "".join("<tr>" + "".join(f"<td>{html.escape(str(value or ''))}</td>" for value in row) + "</tr>" for row in values)
                content += "</tbody></table></div>"
                self.send_html(200, self.page("健康数据", content, session=session))
                return
            if parsed.path == "/training/migration-preflight":
                base_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
                if not base_url:
                    content = "<div class='panel error'>尚未配置 Ironbook 连接。</div>"
                else:
                    try:
                        timeout_seconds = float(getattr(self.server, "ironbook_timeout_seconds", 8.0))  # type: ignore[attr-defined]
                        history = fetch_ironbook_session_history(base_url, timeout_seconds)
                        summaries = history["sessions"]
                        if not isinstance(summaries, list):
                            raise RuntimeError("Ironbook returned invalid session history")
                        details = fetch_ironbook_session_details(base_url, summaries, timeout_seconds)
                        report = analyze_ironbook_training_migration(
                            conn,
                            details,
                            source_full_history_confirmed=bool(history["full_history_confirmed"]),
                            source_total=history["source_total"],
                            source_limit=history["source_limit"],
                            paginated_source=bool(history["paginated_source"]),
                        )
                        content = render_ironbook_migration_preflight(report)
                        if (
                            not bool(getattr(self.server, "ironbook_read_only", False))  # type: ignore[attr-defined]
                            and bool(getattr(self.server, "ironbook_migration_enabled", False))  # type: ignore[attr-defined]
                            and report.get("scope", {}).get("full_history_confirmed") is True
                            and not report.get("limitations")
                            and not report.get("records", {}).get("unmapped_record_count")
                            and report.get("malformed_sessions") == 0
                            and report.get("sessions", {}).get("duplicate_source_ids") == 0
                            and report.get("sessions", {}).get("valid_source_ids") == report.get("scope", {}).get("sessions_in_payload")
                            and report.get("records", {}).get("duplicate_source_ids") == 0
                            and report.get("records", {}).get("malformed_records") == 0
                            and report.get("records", {}).get("valid_source_ids") == report.get("records", {}).get("count")
                        ):
                            fingerprint = html.escape(str(report["scope"]["source_fingerprint"]), quote=True)
                            csrf = html.escape(str(session["csrf"]), quote=True)
                            content += (
                                "<div class='panel'><p class='eyebrow'>最后一步</p>"
                                "<h2>导入到 HealthOS</h2>"
                                "<p class='muted'>提交时会重新读取 Ironbook 并校验源数据指纹。"
                                "只有指纹和预检结果都一致时才会写入本地 HealthOS。</p>"
                                "<form method='post' action='/training/migration'>"
                                f"<input type='hidden' name='csrf' value='{csrf}'>"
                                f"<input type='hidden' name='source_fingerprint' value='{fingerprint}'>"
                                "<label>请输入 <strong>确认迁移</strong>"
                                "<input name='confirmation' required autocomplete='off'></label>"
                                "<button type='submit'>确认并导入</button></form></div>"
                            )
                        if urllib.parse.parse_qs(parsed.query).get("migrated", [""])[0] == "1":
                            imported_count = urllib.parse.parse_qs(parsed.query).get("sessions", [""])[0]
                            content += f"<div class='panel success'><strong>迁移已完成。</strong>本次写入或更新 {html.escape(imported_count)} 节训练，未修改 Ironbook。</div>"
                    except (ValueError, RuntimeError) as exc:
                        content = "<p><a href='/training?source=ironbook'>&larr; 返回力量训练</a></p>"
                        content += f"<div class='panel error'>无法完成预检：{html.escape(str(exc))}</div>"
                self.send_html(200, self.page("Ironbook 迁移预检", content, session=session))
                return
            if parsed.path == "/training-legacy":
                csrf = html.escape(str(session["csrf"]))
                exercise_names = conn.execute(
                    """SELECT standard_name AS name FROM training_exercises
                       UNION SELECT alias_name AS name FROM training_exercise_aliases
                       ORDER BY name COLLATE NOCASE LIMIT 600"""
                ).fetchall()
                exercise_options = "".join(
                    f"<option value='{html.escape(str(row['name']), quote=True)}'>"
                    for row in exercise_names
                )
                exercise_catalog = conn.execute(
                    "SELECT id,standard_name FROM training_exercises ORDER BY standard_name COLLATE NOCASE LIMIT 300"
                ).fetchall()
                alias_rows = conn.execute(
                    """SELECT a.alias_name,e.standard_name FROM training_exercise_aliases a
                       JOIN training_exercises e ON e.id=a.exercise_id
                       ORDER BY e.standard_name COLLATE NOCASE,a.alias_name COLLATE NOCASE LIMIT 200"""
                ).fetchall()
                exercise_catalog_options = "".join(
                    f"<option value='{int(row['id'])}'>{html.escape(str(row['standard_name']))}</option>"
                    for row in exercise_catalog
                )
                totals = conn.execute(
                    """SELECT COUNT(*) AS sessions,
                              COALESCE(SUM(CASE WHEN session_date >= date('now','-29 days') THEN 1 ELSE 0 END),0) AS recent_sessions,
                              COALESCE(SUM(CASE WHEN session_date >= date('now','-29 days') THEN total_volume_kg ELSE 0 END),0) AS recent_volume
                       FROM training_sessions"""
                ).fetchone()
                selected_id = urllib.parse.parse_qs(parsed.query).get("session", [""])[0]
                selected = None
                edit_mode = urllib.parse.parse_qs(parsed.query).get("edit", [""])[0] == "1"
                if selected_id:
                    try:
                        selected = get_training_session(conn, int(selected_id))
                    except ValueError:
                        selected = None
                query_args = urllib.parse.parse_qs(parsed.query)
                requested_source = query_args.get("source", [""])[0]
                configured_ironbook_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
                ironbook_read_only = bool(getattr(self.server, "ironbook_read_only", False))  # type: ignore[attr-defined]
                ironbook_mode = requested_source == "ironbook" or bool(
                    configured_ironbook_url and requested_source != "local"
                )
                ironbook_entry_mode = ironbook_mode and not ironbook_read_only and (
                    query_args.get("new", [""])[0] == "1"
                    or query_args.get("edit", [""])[0] == "1"
                )
                ironbook_sessions: list[dict[str, object]] = []
                ironbook_page: dict[str, object] = {}
                ironbook_selected: dict[str, object] | None = None
                ironbook_error = ""
                if ironbook_mode:
                    base_url = configured_ironbook_url
                    if not base_url:
                        ironbook_error = "尚未配置 Ironbook 只读连接。"
                    else:
                        try:
                            timeout = float(getattr(self.server, "ironbook_timeout_seconds", 8.0))  # type: ignore[attr-defined]
                            page_offset = int(query_args.get("offset", ["0"])[0])
                            ironbook_page = fetch_ironbook_session_page(
                                base_url, timeout, offset=page_offset, page_size=50
                            )
                            ironbook_sessions = ironbook_page["items"]  # type: ignore[assignment]
                            ironbook_id = query_args.get("ironbook_session", [""])[0]
                            if ironbook_id:
                                if not ironbook_id.isdigit():
                                    ironbook_error = "Ironbook 训练编号无效。"
                                else:
                                    expanded = fetch_ironbook_session_details(
                                        base_url, [{"id": int(ironbook_id)}], timeout
                                    )
                                    ironbook_selected = expanded[0] if expanded else None
                        except (ValueError, RuntimeError) as exc:
                            ironbook_error = f"无法读取 Ironbook：{html.escape(str(exc))}"
                ironbook_stats: dict[str, object] | None = None
                ironbook_stats_error = ""
                if ironbook_mode and not ironbook_entry_mode and configured_ironbook_url:
                    try:
                        timeout = float(getattr(self.server, "ironbook_timeout_seconds", 8.0))  # type: ignore[attr-defined]
                        ironbook_stats = request_ironbook_json(configured_ironbook_url, "/api/stats", timeout)
                    except (ValueError, RuntimeError) as exc:
                        ironbook_stats_error = f"暂时无法读取 Ironbook 训练分析：{html.escape(str(exc))}"
                form_session = (ironbook_selected if ironbook_mode and edit_mode else selected if edit_mode else None)
                form_date = html.escape(str(form_session["session_date"] if form_session else dt.date.today().isoformat()), quote=True)
                form_body_part = str(form_session["body_part"] if form_session else "胸")
                if not form_session:
                    if ironbook_entry_mode and configured_ironbook_url:
                        try:
                            timeout = float(getattr(self.server, "ironbook_timeout_seconds", 8.0))  # type: ignore[attr-defined]
                            suggestion = request_ironbook_json(
                                configured_ironbook_url, "/api/training/next-body-part", timeout
                            ).get("suggested_body_part")
                            if isinstance(suggestion, str) and suggestion in _TRAINING_BODY_PARTS:
                                form_body_part = suggestion
                        except (ValueError, RuntimeError):
                            pass
                    else:
                        cycle_marks = ",".join("?" for _ in _TRAINING_BODY_PART_CYCLE)
                        latest = conn.execute(
                            f"""SELECT body_part FROM training_sessions
                                WHERE body_part IN ({cycle_marks})
                                ORDER BY session_date DESC,id DESC LIMIT 1""",
                            _TRAINING_BODY_PART_CYCLE,
                        ).fetchone()
                        form_body_part = next_training_body_part(latest["body_part"] if latest else None)
                body_part_options = "".join(
                    f"<option{' selected' if option == form_body_part else ''}>{option}</option>"
                    for option in ("胸", "背", "肩", "手臂", "腿", "其他")
                )
                form_records = canonical_json(form_session["records"] if form_session else [])
                form_records = html.escape(form_records, quote=True)
                existing_extra = form_session.get("extra_data") if form_session else {}
                existing_timing = existing_extra.get("training_timing", {}) if isinstance(existing_extra, dict) else {}
                form_timing = html.escape(canonical_json(existing_timing), quote=True)
                form_extra_data = html.escape(canonical_json(existing_extra if isinstance(existing_extra, dict) else {}), quote=True)
                form_session_id = f"<input type='hidden' name='session_id' value='{int(form_session['id'])}'>" if form_session else ""
                form_source = "<input type='hidden' name='training_source' value='ironbook'>" if ironbook_entry_mode else ""
                remote_edit_mode = ironbook_mode and edit_mode
                ironbook_form_available = not remote_edit_mode or ironbook_selected is not None
                if remote_edit_mode and form_session:
                    form_date_controls = (
                        f"<label>训练日期<input type='date' value='{form_date}' readonly aria-readonly='true'></label>"
                        f"<input type='hidden' name='session_date' value='{form_date}'>"
                    )
                    form_body_part_controls = (
                        f"<label>训练部位<select disabled>{body_part_options}</select></label>"
                        f"<input type='hidden' name='body_part' value='{html.escape(form_body_part, quote=True)}'>"
                    )
                else:
                    form_date_controls = f"<label>训练日期<input name='session_date' type='date' value='{form_date}' required></label>"
                    form_body_part_controls = f"<label>训练部位<select name='body_part'>{body_part_options}</select></label>"
                analytics = training_analytics(conn)
                content = f"""<p><a class='button secondary' href='/training-demo'>预览新版训练界面（演示数据）</a></p>
                <div {'hidden' if ironbook_mode else ''} class='grid'><div class='panel stat'><span>累计训练</span><strong>{totals['sessions']}</strong></div><div class='panel stat'><span>近 30 天训练</span><strong>{totals['recent_sessions']}</strong></div><div class='panel stat'><span>近 30 天训练量</span><strong>{round(float(totals['recent_volume'] or 0))} kg</strong></div></div>
                <p {'hidden' if ironbook_mode else ''}><a class='button secondary' href='/training?source=ironbook'>查看 Ironbook 训练历史</a></p>
                <div {'hidden' if (ironbook_mode and not ironbook_entry_mode) or not ironbook_form_available else ''} class='panel'><p class='eyebrow'>{'Ironbook 写回' if ironbook_entry_mode else 'HealthOS 本地记录'}</p><h2>{'编辑训练' if form_session else '记录一次训练'}</h2>
                {"<p class='muted'>保存会直接写入 Ironbook。Ironbook 当前要求每组填写重量、次数和 RPE；通过 HealthOS 暂不能修改已有训练的日期或部位。</p>" if ironbook_entry_mode else ""}
                <form method='post' action='/training' id='training-form'><input type='hidden' name='csrf' value='{csrf}'><input type='hidden' name='request_id' value='{secrets.token_urlsafe(24)}'>{form_source}{form_session_id}<input type='hidden' name='ironbook_extra_data' value="{form_extra_data}"><input type='hidden' name='records_json' id='training-records-json' value="{form_records}"><input type='hidden' name='training_timing_json' id='training-timing-json' value="{form_timing}">
                <div class='form-grid'>{form_date_controls}{form_body_part_controls}
                <label>时长（分钟）<input name='duration_minutes' type='number' min='0' step='1' value='{html.escape(str(form_session.get('duration_minutes') or '') if form_session else '', quote=True)}'></label><label>睡眠（小时）<input name='sleep_hours' type='number' min='0' step='0.1' value='{html.escape(str(form_session.get('sleep_hours') or '') if form_session else '', quote=True)}'></label>
                <label>压力（1–10）<input name='stress_level' type='number' min='1' max='10' step='1' value='{html.escape(str(form_session.get('stress_level') or '') if form_session else '', quote=True)}'></label><label>HRV（毫秒）<input name='hrv_score' type='number' min='0' step='1' value='{html.escape(str(form_session.get('hrv_score') or '') if form_session else '', quote=True)}'></label></div>
                <label>状态备注<textarea name='feeling' rows='2' maxlength='2000' placeholder='训练状态、身体感受'>{html.escape(str(form_session.get('feeling') or '') if form_session else '')}</textarea></label>
                <div class='form-grid'><strong id='training-timer'>尚未开始</strong><div><button type='button' class='secondary' id='training-start'>开始计时</button> <button type='button' class='secondary' id='training-finish' disabled>结束训练</button></div></div>
                <h3>动作组</h3><datalist id='exercise-options'>{exercise_options}</datalist><div id='training-records'></div><button type='button' class='secondary' id='add-training-record'>添加一组</button>
                <p><button type='submit'>保存训练</button> <span id='training-form-error' class='error' role='status'></span></p></form></div>"""
                aliases_saved = "<p class='success'>动作别名已保存。</p>" if urllib.parse.parse_qs(parsed.query).get("aliases", [""])[0] == "saved" else ""
                content += f"<div {'hidden' if ironbook_mode else ''} class='panel'><h2>动作名称与别名</h2>{aliases_saved}<form method='post' action='/training/aliases'><input type='hidden' name='csrf' value='{csrf}'><label>标准动作<select name='exercise_id' required>{exercise_catalog_options}</select></label><label>新别名<input name='alias_name' maxlength='128' required placeholder='例如：杠铃卧推'></label><button type='submit'>添加别名</button></form><div class='table-wrap'><table><thead><tr><th>标准动作</th><th>别名</th></tr></thead><tbody>"
                if alias_rows:
                    for alias in alias_rows:
                        content += "<tr>" + "".join(
                            f"<td>{html.escape(str(value))}</td>"
                            for value in (alias["standard_name"], alias["alias_name"])
                        ) + "</tr>"
                else:
                    content += "<tr><td colspan='2' class='empty'>暂无已配置别名</td></tr>"
                content += "</tbody></table></div></div>"
                content += f"<div {'hidden' if ironbook_mode else ''} class='panel'><p class='eyebrow'>训练分析</p><h2>近 12 周趋势</h2><div class='table-wrap'><table><thead><tr><th>周起始</th><th>训练次数</th><th>训练量（kg）</th><th>有效组</th></tr></thead><tbody>"
                for week in analytics["weeks"]:
                    content += "<tr>" + "".join(
                        f"<td>{html.escape(str(value))}</td>"
                        for value in (
                            week["week_start"], week["sessions"],
                            round(float(week["volume_kg"])), week["effective_sets"],
                        )
                    ) + "</tr>"
                content += "</tbody></table></div><h3>近 90 天部位分布</h3>"
                if analytics["body_parts_90d"]:
                    content += "<div class='form-grid'>" + "".join(
                        f"<p>{html.escape(str(part))}：{count} 次</p>"
                        for part, count in analytics["body_parts_90d"]
                    ) + "</div>"
                else:
                    content += "<p class='empty'>暂无部位分布数据</p>"
                content += "<h3>动作表现</h3><div class='table-wrap'><table><thead><tr><th>动作</th><th>日期</th><th>最高重量（kg）</th><th>训练量（kg）</th><th>组数</th></tr></thead><tbody>"
                trend_rows = [
                    (exercise["name"], session)
                    for exercise in analytics["exercise_trends"]
                    for session in exercise["sessions"]
                ]
                if trend_rows:
                    for name, trend in trend_rows[:48]:
                        content += "<tr>" + "".join(
                            f"<td>{html.escape(str(value if value is not None else ''))}</td>"
                            for value in (
                                name, trend["session_date"], trend["max_weight_kg"],
                                round(float(trend["volume_kg"] or 0)), trend["sets_count"],
                            )
                        ) + "</tr>"
                else:
                    content += "<tr><td colspan='5' class='empty'>暂无动作表现数据</td></tr>"
                content += "</tbody></table></div></div>"
                if selected:
                    content += f"<div class='panel'><h2>{html.escape(str(selected['session_date']))} · {html.escape(str(selected['body_part']))}</h2><p class='muted'>{html.escape(str(selected.get('duration_minutes') or '未记录'))} 分钟 · 训练量 {html.escape(str(selected.get('total_volume_kg') or '未记录'))} kg · {len(selected['records'])} 组</p><p><a href='/training?session={int(selected['id'])}&amp;edit=1'>编辑此训练</a></p><form method='post' action='/training/delete' onsubmit=\"return confirm('删除这节训练及其所有动作记录吗？此操作无法撤销。')\"><input type='hidden' name='csrf' value='{csrf}'><input type='hidden' name='session_id' value='{int(selected['id'])}'><button class='secondary' type='submit'>删除此训练</button></form>"
                    if selected.get("feeling"):
                        content += f"<p>{html.escape(str(selected['feeling']))}</p>"
                    content += "<table><thead><tr><th>动作</th><th>重量</th><th>次数</th><th>RPE</th><th>组型</th><th>备注</th></tr></thead><tbody>"
                    for record in selected["records"]:
                        record_extra = record.get("extra_data") if isinstance(record.get("extra_data"), dict) else {}
                        weight_display, reps_display = training_record_weight_reps(record)
                        content += "<tr>" + "".join(f"<td>{html.escape(str(value if value is not None else ''))}</td>" for value in (
                            record.get("standard_name"), weight_display, reps_display,
                            ", ".join(map(str, record.get("rpe") or [])),
                            "递减组" if record_extra.get("structure") == "drop_set_group" else "普通组",
                            record.get("note"),
                        )) + "</tr>"
                    content += "</tbody></table></div>"
                sessions = query_training_sessions(conn, limit=50)
                content += f"<div {'hidden' if ironbook_mode else ''} class='panel'><h2>训练历史</h2>"
                if sessions:
                    content += "<table><thead><tr><th>日期</th><th>部位</th><th>时长</th><th>训练量</th><th>动作组</th></tr></thead><tbody>"
                    for item in sessions:
                        content += f"<tr><td><a href='/training?session={item['id']}'>{html.escape(str(item['session_date']))}</a></td><td>{html.escape(str(item['body_part']))}</td><td>{html.escape(str(item['duration_minutes'] or ''))}</td><td>{html.escape(str(item['total_volume_kg'] or ''))}</td><td>{item['record_count']}</td></tr>"
                    content += "</tbody></table>"
                else:
                    content += "<p class='empty'>还没有训练记录</p>"
                if ironbook_mode:
                    if ironbook_read_only:
                        content += "<div class='panel'><p class='muted'>Ironbook 只读预览：当前不会创建、编辑或删除训练记录。</p></div>"
                    if ironbook_stats is not None:
                        content += render_ironbook_training_analytics(ironbook_stats)
                    elif ironbook_stats_error:
                        content += f"<div class='panel error'>{ironbook_stats_error}</div>"
                    new_session_link = "" if ironbook_read_only else "<p><a class='button' href='/training?source=ironbook&amp;new=1'>新建训练</a></p>"
                    content += f"<div class='panel'><p class='eyebrow'>Ironbook</p><h2>训练历史</h2>{new_session_link}<p><a class='button secondary' href='/training/migration-preflight'>查看迁移预检</a></p>"
                    if ironbook_error:
                        content += f"<p class='error'>{ironbook_error}</p>"
                    elif ironbook_sessions:
                        write_status = "只读预览不会修改训练数据。" if ironbook_read_only else "通过此页保存的训练会写回 Ironbook。"
                        page_total = ironbook_page.get("total")
                        range_start = int(ironbook_page.get("offset") or 0) + 1
                        range_end = int(ironbook_page.get("offset") or 0) + len(ironbook_sessions)
                        if isinstance(page_total, int):
                            range_summary = f"显示第 {range_start}–{range_end} 条，共 {page_total} 条。"
                        elif ironbook_page.get("api_cap_reached"):
                            range_summary = "旧版 Ironbook API 最多返回最近 100 条，无法继续翻到更早记录。"
                        else:
                            range_summary = f"显示第 {range_start}–{range_end} 条；当前 API 未提供总数。"
                        content += f"<p class='muted'>{range_summary} {write_status}</p><div class='table-wrap'><table><thead><tr><th>日期</th><th>部位</th><th>时长</th><th>训练量</th><th>有效组</th><th></th></tr></thead><tbody>"
                        for item in ironbook_sessions:
                            try:
                                source_id = int(item.get("id") or 0)
                            except (TypeError, ValueError):
                                continue
                            edit_action = "" if ironbook_read_only else f" · <a href='/training?source=ironbook&amp;ironbook_session={source_id}&amp;edit=1'>编辑</a>"
                            content += "<tr>" + "".join(
                                f"<td>{html.escape(str(value if value is not None else ''))}</td>"
                                for value in (
                                    item.get("session_date"), item.get("body_part"),
                                    item.get("duration_minutes"), item.get("total_volume_kg"),
                                    item.get("effective_sets"),
                                )
                            ) + f"<td><a href='/training?source=ironbook&amp;ironbook_session={source_id}'>查看</a>{edit_action}</td></tr>"
                        content += "</tbody></table></div>"
                        page_offset = int(ironbook_page.get("offset") or 0)
                        if page_offset or ironbook_page.get("has_next"):
                            previous_link = (
                                f"<a class='button secondary' href='/training?source=ironbook&amp;offset={page_offset - 50}'>上一页</a>"
                                if page_offset else ""
                            )
                            next_link = (
                                f"<a class='button secondary' href='/training?source=ironbook&amp;offset={page_offset + 50}'>下一页</a>"
                                if ironbook_page.get("has_next") else ""
                            )
                            content += f"<nav class='form-grid' aria-label='Ironbook 训练历史分页'>{previous_link}{next_link}</nav>"
                    else:
                        empty_message = (
                            "旧版 Ironbook API 的 100 条范围已经翻完，更早记录需要支持分页的 API。"
                            if ironbook_page.get("api_cap_reached")
                            else "Ironbook 暂无可显示的训练记录。"
                        )
                        content += f"<p class='empty'>{empty_message}</p>"
                    if ironbook_selected:
                        content += f"<h3>{html.escape(str(ironbook_selected.get('session_date') or ''))} · {html.escape(str(ironbook_selected.get('body_part') or ''))}</h3>"
                        records = ironbook_selected.get("records")
                        if isinstance(records, list) and records:
                            content += "<div class='table-wrap'><table><thead><tr><th>动作</th><th>重量（kg）</th><th>次数</th><th>RPE</th><th>组数</th></tr></thead><tbody>"
                            for record in records:
                                if not isinstance(record, dict):
                                    continue
                                weight_display, reps_display = training_record_weight_reps(record)
                                content += "<tr>" + "".join(
                                    f"<td>{html.escape(str(value if value is not None else ''))}</td>"
                                    for value in (
                                        record.get("raw_exercise_name"), weight_display,
                                        reps_display,
                                        ", ".join(map(str, record.get("rpe") or [])),
                                        record.get("sets_count"),
                                    )
                                ) + "</tr>"
                            content += "</tbody></table></div>"
                        else:
                            content += "<p class='empty'>没有动作组详情。</p>"
                        if not ironbook_read_only:
                            content += f"<form method='post' action='/training/delete' onsubmit=\"return confirm('从 Ironbook 永久删除这节训练及所有动作组吗？此操作无法撤销。')\"><input type='hidden' name='csrf' value='{csrf}'><input type='hidden' name='training_source' value='ironbook'><input type='hidden' name='session_id' value='{int(ironbook_selected['id'])}'><button class='secondary' type='submit'>删除这节训练</button></form>"
                    content += "</div>"
                if not ironbook_mode or ironbook_entry_mode:
                    content += TRAINING_PAGE_SCRIPT
                elif query_args.get("saved", [""])[0] == "1":
                    content += "<script>try{localStorage.removeItem('healthos.training.ironbook.new.draft.v1');}</script>"
                    if ironbook_selected:
                        content += f"<script>try{{localStorage.removeItem('healthos.training.ironbook.{int(ironbook_selected['id'])}.draft.v1');}}</script>"
                self.send_html(200, self.page("力量训练", content, session=session))
                return
            if parsed.path == "/add":
                csrf = html.escape(str(session["csrf"]))
                content = f"""<p class='muted'>将检查结果、用药和健康事件补充到个人档案。</p>
                <div class='panel'><h2>检验结果</h2><form method='post' action='/add'>
                <input type='hidden' name='csrf' value='{csrf}'><input type='hidden' name='data_type' value='lab'>
                <label>采样时间<input name='collection_time' required placeholder='2026-10-09'></label>
                <label>检查项目<input name='test_name' required></label><label>标准名称<input name='canonical_name'></label>
                <label>结果数值<input name='value_num' type='number' step='any'></label><label>单位<input name='unit'></label><label>异常标记<input name='flag'></label>
                <button>保存检验结果</button></form></div>
                <div class='panel'><h2>用药记录</h2><form method='post' action='/add'>
                <input type='hidden' name='csrf' value='{csrf}'><input type='hidden' name='data_type' value='medication'>
                <label>药品名称<input name='drug_name' required></label><label>通用名<input name='generic_name'></label>
                <label>剂量<input name='dose' type='number' step='any'></label><label>剂量单位<input name='dose_unit'></label><label>服用频率<input name='frequency'></label>
                <label>开始时间<input name='start_time' required placeholder='2026-10-09'></label><label>结束时间<input name='end_time'></label><label>用途 / 原因<input name='reason'></label>
                <button>保存用药记录</button></form></div>
                <div class='panel'><h2>健康事件</h2><form method='post' action='/add'>
                <input type='hidden' name='csrf' value='{csrf}'><input type='hidden' name='data_type' value='event'>
                <label>事件类型<input name='event_type' required placeholder='症状、就诊、备注等'></label><label>开始时间<input name='start_time' required></label><label>结束时间<input name='end_time'></label>
                <label>标题<input name='title' required></label><label>备注<textarea name='notes' rows='3'></textarea></label>
                <button>保存健康事件</button></form></div>"""
                self.send_html(200, self.page("手动录入", content, session=session))
                return
            if parsed.path == "/shares":
                cleanup_expired_share_payloads(conn)
                rows = conn.execute("SELECT id,scope_type,created_at,expires_at,read_count,max_reads,revoked_at FROM share_snapshots ORDER BY created_at DESC LIMIT 50").fetchall()
                csrf = html.escape(str(session["csrf"]))
                content = f"<div class='panel'><p class='eyebrow'>复制到 ChatGPT</p><h2>生成健康摘要</h2><p class='muted'>在本机生成限定范围的 Markdown 文本，复制后粘贴到 ChatGPT 对话中。</p><form method='post' action='/shares/copy'><input type='hidden' name='csrf' value='{csrf}'><label>数据范围<select name='preset'><option value='recovery_30d'>近期恢复 · 30 天</option><option value='clinical_90d'>临床信息 · 90 天</option><option value='labs_all'>全部检验结果</option></select></label><label><input type='checkbox' name='include_training' value='1'> 包含力量训练（日期、动作、重量、次数、RPE）</label><button>生成摘要文本</button></form></div><div class='panel'><p class='eyebrow'>临时访问</p><h2>创建分享链接</h2><form method='post' action='/shares'><input type='hidden' name='csrf' value='{csrf}'><label>数据范围<select name='preset'><option value='recovery_30d'>近期恢复 · 30 天</option><option value='clinical_90d'>临床信息 · 90 天</option><option value='labs_all'>全部检验结果</option></select></label><label><input type='checkbox' name='include_training' value='1'> 包含力量训练（日期、动作、重量、次数、RPE）</label><label>有效时长<select name='ttl_minutes'><option>15 分钟</option><option>5 分钟</option><option>60 分钟</option></select></label><label>输出格式<select name='snapshot_format'><option value='json'>JSON</option><option value='html'>HTML</option></select></label><label>读取次数<select name='max_reads'><option value='5'>5 次</option><option value='0'>不限次数</option></select></label><button>创建临时链接</button></form></div><div class='panel'><h2>分享记录</h2><table><thead><tr><th>范围</th><th>创建时间</th><th>过期时间</th><th>读取次数</th><th>状态 / 操作</th></tr></thead><tbody>"
                share_rows = []
                for row in rows:
                    status = "已撤销" if row["revoked_at"] else (
                        f"<form method='post' action='/shares/revoke'><input type='hidden' name='csrf' value='{csrf}'><input type='hidden' name='snapshot_id' value='{row['id']}'><button class='secondary'>撤销链接</button></form>"
                    )
                    share_rows.append(
                        f"<tr><td>{html.escape(str(row['scope_type']))}</td><td>{html.escape(str(row['created_at']))}</td><td>{html.escape(str(row['expires_at']))}</td><td>{row['read_count']} / {'不限' if row['max_reads'] == 0 else row['max_reads']}</td><td>{status}</td></tr>"
                    )
                content += "".join(share_rows)
                content += "</tbody></table></div>"
                self.send_html(200, self.page("分享与导出", content, session=session))
                return
            self.send_json(404, {"error": "not_found"})
        finally:
            conn.close()

    def read_json_body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            length = -1
        if length < 0 or length > 10 * 1024 * 1024:
            raise ValueError("body_too_large")
        payload = json.loads(self.rfile.read(length))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == "/healthz":
            self.send_json(200, {"ok": True, "service": "HealthOS", "version": SCHEMA_VERSION})
            return
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/login":
            if self.owner_session() is not None:
                self.redirect("/")
                return
            if owner_password_hash() is None:
                content = "<div class='panel'><p class='error'>尚未配置管理员身份验证。</p><p>请配置 HEALTHOS_OWNER_PASSWORD_HASH 后重新启动服务。</p></div>"
                self.send_html(503, self.page("管理员登录", content))
                return
            content = """<div class='panel'><form method='post' action='/login'>
            <p class='eyebrow'>个人健康档案</p><div class='panel'><p class='muted'>登录以查看和管理你的健康数据。</p><form method='post' action='/login'>
            <label>管理员密码<input type='password' name='password' required autofocus autocomplete='current-password'></label>
            <button>登录 HealthOS</button></form></div>"""
            self.send_html(200, self.page("管理员登录", content))
            return
        if parsed.path.startswith("/share/"):
            token = urllib.parse.unquote(parsed.path[len("/share/"):])
            output_format = urllib.parse.parse_qs(parsed.query).get("format", ["json"])[0]
            conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
            payload = None
            try:
                _, payload = consume_share_snapshot(conn, token)
            except LookupError:
                self.send_json(410, {"error": "share_gone"})
            finally:
                conn.close()
            if payload is not None:
                if output_format == "html":
                    rendered = html.escape(json.dumps(payload, ensure_ascii=False, indent=2))
                    self.send_html(200, f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>HealthOS 健康摘要</title></head><body><main><h1>HealthOS 健康摘要</h1><pre>{rendered}</pre></main></body></html>")
                else:
                    self.send_json(200, payload)
            return
        if parsed.path.startswith("/owner/api/training/"):
            if self.owner_session() is None:
                self.send_json(401, {"error": "owner_auth_required"})
                return
            query = urllib.parse.parse_qs(parsed.query)
            conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
            try:
                if parsed.path == "/owner/api/training/sessions":
                    try:
                        limit = min(max(int(query.get("limit", ["50"])[0]), 1), 100)
                        offset = min(max(int(query.get("offset", ["0"])[0]), 0), 1_000_000)
                    except ValueError:
                        self.send_json(400, {"error": "invalid_pagination"})
                        return
                    search = query.get("q", [""])[0].strip()
                    if len(search) > 128:
                        self.send_json(400, {"error": "invalid_query"})
                        return
                    count_clauses = ""
                    count_params: list[object] = []
                    if search:
                        pattern = f"%{search}%"
                        count_clauses = " WHERE (s.session_date LIKE ? OR s.body_part LIKE ? OR s.feeling LIKE ? OR EXISTS (SELECT 1 FROM training_exercise_records r JOIN training_exercises e ON e.id=r.exercise_id LEFT JOIN training_exercise_aliases a ON a.exercise_id=e.id WHERE r.session_id=s.id AND (e.standard_name LIKE ? OR r.raw_exercise_name LIKE ? OR a.alias_name LIKE ?)))"
                        count_params = [pattern] * 6
                    total = int(conn.execute(f"SELECT COUNT(*) FROM training_sessions s{count_clauses}", count_params).fetchone()[0])
                    items = query_training_sessions(conn, limit=limit, offset=offset, search=search)
                    self.send_json(200, {"items": items, "total": total, "limit": limit, "offset": offset})
                    return
                if parsed.path == "/owner/api/training/analytics":
                    today = dt.date.today().isoformat()
                    month_start = (dt.date.today() - dt.timedelta(days=29)).isoformat()
                    summary = conn.execute(
                        """SELECT COUNT(*) AS total_sessions,
                                  COALESCE(SUM(CASE WHEN session_date>=? AND session_date<=? THEN 1 ELSE 0 END),0) AS recent_sessions,
                                  COALESCE(SUM(CASE WHEN session_date>=? AND session_date<=? THEN total_volume_kg ELSE 0 END),0) AS recent_volume
                           FROM training_sessions""",
                        (month_start, today, month_start, today),
                    ).fetchone()
                    analytics = training_analytics(conn)
                    last = conn.execute("SELECT body_part FROM training_sessions ORDER BY session_date DESC,id DESC LIMIT 1").fetchone()
                    self.send_json(200, {"summary": dict(summary), "suggested_body_part": next_training_body_part(last["body_part"] if last else None), **analytics})
                    return
                if parsed.path == "/owner/api/training/exercises":
                    raw_query = query.get("q", [""])[0].strip()
                    body_part = query.get("body_part", [""])[0].strip()
                    if len(raw_query) > 128 or (body_part and body_part not in _TRAINING_BODY_PARTS):
                        self.send_json(400, {"error": "invalid_query"})
                        return
                    ironbook_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
                    if ironbook_url and getattr(self.server, "ironbook_read_only", False):  # type: ignore[attr-defined]
                        try:
                            suggestions = fetch_ironbook_exercise_suggestions(
                                ironbook_url,
                                float(getattr(self.server, "ironbook_timeout_seconds", 8.0)),  # type: ignore[attr-defined]
                                raw_query,
                                body_part or "其他",
                            )
                            self.send_json(200, suggestions)
                        except (ValueError, RuntimeError, urllib.error.URLError) as exc:
                            self.send_json(502, {"error": "ironbook_unavailable", "detail": str(exc)})
                        return
                    pattern = f"%{raw_query.casefold()}%"
                    rows = conn.execute(
                        """SELECT DISTINCT e.id,e.standard_name,e.body_part
                           FROM training_exercises e
                           LEFT JOIN training_exercise_aliases a ON a.exercise_id=e.id
                           WHERE (?='' OR lower(e.standard_name) LIKE ? OR lower(a.alias_name) LIKE ?)
                             AND (?='' OR e.body_part=?)
                           ORDER BY e.standard_name COLLATE NOCASE LIMIT 100""",
                        (raw_query, pattern, pattern, body_part, body_part),
                    ).fetchall()
                    result = []
                    for row in rows:
                        aliases = [item[0] for item in conn.execute(
                            "SELECT alias_name FROM training_exercise_aliases WHERE exercise_id=? ORDER BY alias_name COLLATE NOCASE",
                            (row["id"],),
                        )]
                        result.append({"id": int(row["id"]), "name": row["standard_name"], "body_part": row["body_part"], "aliases": aliases})
                    self.send_json(200, {"items": result})
                    return
                if parsed.path == "/owner/api/training/exercises/last":
                    name = query.get("name", [""])[0].strip()
                    if not name or len(name) > 128:
                        self.send_json(400, {"error": "invalid_exercise_name"})
                        return
                    ironbook_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
                    if ironbook_url and getattr(self.server, "ironbook_read_only", False):  # type: ignore[attr-defined]
                        try:
                            previous = fetch_ironbook_last_exercise(
                                ironbook_url,
                                float(getattr(self.server, "ironbook_timeout_seconds", 8.0)),  # type: ignore[attr-defined]
                                name,
                            )
                            self.send_json(200, {"exercise": name, **previous})
                        except (ValueError, RuntimeError, urllib.error.URLError) as exc:
                            self.send_json(502, {"error": "ironbook_unavailable", "detail": str(exc)})
                        return
                    normalized = normalize_exercise_name(name)
                    exercise = conn.execute(
                        """SELECT e.id,e.standard_name FROM training_exercises e
                           LEFT JOIN training_exercise_aliases a ON a.exercise_id=e.id
                           WHERE lower(e.standard_name)=lower(?) OR a.normalized_alias=?
                           ORDER BY e.id LIMIT 1""",
                        (name, normalized),
                    ).fetchone()
                    if not exercise:
                        self.send_json(200, {"exercise": name, "session_date": None, "sets": []})
                        return
                    previous = conn.execute(
                        """SELECT s.id,s.session_date FROM training_sessions s
                           JOIN training_exercise_records r ON r.session_id=s.id
                           WHERE r.exercise_id=? ORDER BY s.session_date DESC,s.id DESC LIMIT 1""",
                        (exercise["id"],),
                    ).fetchone()
                    sets: list[dict[str, object]] = []
                    if previous:
                        session = get_training_session(conn, int(previous["id"]))
                        if session:
                            for record in session["records"]:
                                if int(record["exercise_id"]) != int(exercise["id"]):
                                    continue
                                extra = record.get("extra_data") if isinstance(record.get("extra_data"), dict) else {}
                                sets.append({"weight_kg": record.get("weight_kg"), "reps": record.get("reps") or [],
                                             "rpe": record.get("rpe") or [], "extra_data": extra})
                    self.send_json(200, {"exercise": exercise["standard_name"], "session_date": previous["session_date"] if previous else None, "sets": sets})
                    return
                if parsed.path == "/owner/api/training/aliases":
                    rows = conn.execute(
                        """SELECT e.id AS exercise_id,e.standard_name,a.alias_name
                           FROM training_exercise_aliases a JOIN training_exercises e ON e.id=a.exercise_id
                           ORDER BY e.standard_name COLLATE NOCASE,a.alias_name COLLATE NOCASE LIMIT 500"""
                    ).fetchall()
                    self.send_json(200, {"items": [dict(row) for row in rows]})
                    return
                match = re.fullmatch(r"/owner/api/training/sessions/(\d+)", parsed.path)
                if match:
                    session = get_training_session(conn, int(match.group(1)))
                    if session is None:
                        self.send_json(404, {"error": "training_session_not_found"})
                    else:
                        self.send_json(200, session)
                    return
            finally:
                conn.close()
        if parsed.path == "/owner/api/training/exercises":
            if self.owner_session() is None:
                self.send_json(401, {"error": "owner_auth_required"})
                return
            base_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
            if not base_url:
                self.send_json(503, {"error": "ironbook_not_configured"})
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                suggestions = fetch_ironbook_exercise_suggestions(
                    base_url,
                    float(getattr(self.server, "ironbook_timeout_seconds", 8.0)),  # type: ignore[attr-defined]
                    query.get("q", [""])[0],
                    query.get("body_part", ["其他"])[0],
                )
            except ValueError as exc:
                self.send_json(400, {"error": "invalid_query", "detail": str(exc)})
                return
            except RuntimeError as exc:
                self.send_json(502, {"error": "ironbook_unavailable", "detail": str(exc)})
                return
            self.send_json(200, suggestions)
            return
        if parsed.path == "/owner/api/training/exercises/last":
            if self.owner_session() is None:
                self.send_json(401, {"error": "owner_auth_required"})
                return
            base_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
            if not base_url:
                self.send_json(503, {"error": "ironbook_not_configured"})
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                result = fetch_ironbook_last_exercise(
                    base_url,
                    float(getattr(self.server, "ironbook_timeout_seconds", 8.0)),  # type: ignore[attr-defined]
                    query.get("name", [""])[0],
                )
            except ValueError as exc:
                self.send_json(400, {"error": "invalid_query", "detail": str(exc)})
                return
            except RuntimeError as exc:
                self.send_json(502, {"error": "ironbook_unavailable", "detail": str(exc)})
                return
            self.send_json(200, result)
            return
        if parsed.path in {"/", "/data", "/training", "/training-legacy", "/training-demo", "/training-ui.js", "/training/migration-preflight", "/add", "/shares"}:
            session = self.require_owner()
            if session is not None:
                self.render_owner_get(parsed, session)
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        collection_tables = {
            "/api/v1/health/labs": "lab_results",
            "/api/v1/health/medications": "medications",
            "/api/v1/health/events": "health_events",
        }
        if parsed.path in collection_tables:
            query = urllib.parse.parse_qs(parsed.query)
            try:
                limit = min(max(int(query.get("limit", ["100"])[0]), 1), 1000)
            except ValueError:
                self.send_json(400, {"error": "invalid_limit"})
                return
            conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
            try:
                table = collection_tables[parsed.path]
                rows = [dict(row) for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()]
            finally:
                conn.close()
            self.send_json(200, {"items": rows})
            return
        if parsed.path == "/api/v1/health/context":
            query = urllib.parse.parse_qs(parsed.query)
            raw_scope: dict[str, object] = {}
            for key in ("preset", "from", "to"):
                if query.get(key):
                    raw_scope[key] = query[key][0]
            if query.get("domains"):
                raw_scope["domains"] = [item for item in query["domains"][0].split(",") if item]
            if query.get("metrics"):
                raw_scope["metrics"] = [item for item in query["metrics"][0].split(",") if item]
            try:
                conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
                try:
                    context = build_health_context(conn, raw_scope)
                finally:
                    conn.close()
            except ValueError as exc:
                self.send_json(400, {"error": "invalid_scope", "detail": str(exc)})
                return
            self.send_json(200, context)
            return
        if parsed.path != "/api/v1/health/query":
            if parsed.path == "/api/v1/training/migration-preflight":
                base_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
                if not base_url:
                    self.send_json(503, {"error": "ironbook_not_configured"})
                    return
                try:
                    timeout_seconds = float(getattr(self.server, "ironbook_timeout_seconds", 8.0))  # type: ignore[attr-defined]
                    history = fetch_ironbook_session_history(base_url, timeout_seconds)
                    summaries = history["sessions"]
                    if not isinstance(summaries, list):
                        raise RuntimeError("Ironbook returned invalid session history")
                    details = fetch_ironbook_session_details(base_url, summaries, timeout_seconds)
                except ValueError as exc:
                    self.send_json(400, {"error": "invalid_configuration", "detail": str(exc)})
                    return
                except RuntimeError as exc:
                    self.send_json(502, {"error": "ironbook_unavailable", "detail": str(exc)})
                    return
                conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
                try:
                    report = analyze_ironbook_training_migration(
                        conn,
                        details,
                        source_full_history_confirmed=bool(history["full_history_confirmed"]),
                        source_total=history["source_total"],
                        source_limit=history["source_limit"],
                        paginated_source=bool(history["paginated_source"]),
                    )
                finally:
                    conn.close()
                self.send_json(200, report)
                return
            if parsed.path == "/api/v1/training/sessions":
                base_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
                if not base_url:
                    self.send_json(503, {"error": "ironbook_not_configured"})
                    return
                query = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = min(max(int(query.get("limit", ["100"])[0]), 1), 100)
                    from_date = query.get("from", [None])[0]
                    to_date = query.get("to", [None])[0]
                    if from_date:
                        dt.date.fromisoformat(from_date)
                    if to_date:
                        dt.date.fromisoformat(to_date)
                    if from_date and to_date and from_date > to_date:
                        raise ValueError("from must not be after to")
                    include_records = query.get("include_records", ["true"])[0].lower() in {"1", "true", "yes"}
                    timeout_seconds = float(getattr(self.server, "ironbook_timeout_seconds", 8.0))  # type: ignore[attr-defined]
                    sessions = fetch_ironbook_sessions(base_url, timeout_seconds)
                except ValueError as exc:
                    self.send_json(400, {"error": "invalid_query", "detail": str(exc)})
                    return
                except RuntimeError as exc:
                    self.send_json(502, {"error": "ironbook_unavailable", "detail": str(exc)})
                    return
                sessions = [
                    session for session in sessions
                    if (not from_date or str(session.get("session_date") or "") >= from_date)
                    and (not to_date or str(session.get("session_date") or "") <= to_date)
                ]
                sessions = sorted(
                    sessions,
                    key=lambda item: (str(item.get("session_date") or ""), int(item.get("id") or 0)),
                    reverse=True,
                )[:limit]
                if include_records:
                    try:
                        sessions = fetch_ironbook_session_details(base_url, sessions, timeout_seconds)
                    except ValueError as exc:
                        self.send_json(400, {"error": "invalid_configuration", "detail": str(exc)})
                        return
                    except RuntimeError as exc:
                        self.send_json(502, {"error": "ironbook_unavailable", "detail": str(exc)})
                        return
                dates = {str(session.get("session_date") or "") for session in sessions}
                conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
                try:
                    workouts = load_workouts_for_dates(conn, dates)
                    result = training_link_payload(sessions, workouts)
                    latest = conn.execute("SELECT MAX(substr(start_time, 1, 10)) FROM workouts").fetchone()[0]
                finally:
                    conn.close()
                result["range"] = {
                    "from": from_date,
                    "to": to_date,
                    "limit": limit,
                    "include_records": include_records,
                }
                result["source"] = {
                    "ironbook": {"sessions_endpoint": "/api/sessions"},
                    "apple_health": {"store": "HealthOS.workouts", "latest_workout_date": latest},
                }
                self.send_json(200, result)
                return
            self.send_json(404, {"error": "not_found"})
            return
        query = urllib.parse.parse_qs(parsed.query)
        metric = query.get("metric", [None])[0]
        try:
            limit = min(max(int(query.get("limit", ["100"])[0]), 1), 1000)
        except ValueError:
            self.send_json(400, {"error": "invalid_limit"})
            return
        conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
        try:
            rows = query_observations(conn, metric=metric, limit=limit)
        finally:
            conn.close()
        # Keep the legacy `samples` envelope while exposing canonical naming.
        self.send_json(200, {"observations": rows, "samples": rows})

    def do_POST(self) -> None:
        parsed_path = urllib.parse.urlparse(self.path).path
        if parsed_path == "/login":
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > 64 * 1024:
                    raise ValueError("body_too_large")
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                password = form.get("password", [""])[0]
            except (ValueError, UnicodeDecodeError) as exc:
                self.send_html(400, self.page("管理员登录", f"<div class='panel error'>{html.escape(str(exc))}</div>"))
                return
            if not owner_password_hash():
                self.send_html(503, self.page("管理员登录", "<div class='panel error'>尚未配置管理员身份验证。</div>"))
                return
            if not verify_password(password, owner_password_hash()):
                self.send_html(401, self.page("管理员登录", "<div class='panel error'>密码不正确，请重试。</div>"))
                return
            session_id, _ = create_session()
            self.redirect("/", cookie=self.session_cookie(session_id))
            return
        if parsed_path in {"/logout", "/training", "/training/aliases", "/training/delete", "/training/migration", "/add", "/shares", "/shares/copy", "/shares/revoke"}:
            session_id = self.request_cookie(SESSION_COOKIE)
            session = self.require_owner()
            if session is None:
                return
            try:
                length = int(self.headers.get("Content-Length", "-1"))
                if length < 0 or length > 64 * 1024:
                    raise ValueError("body_too_large")
                form = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            except (ValueError, UnicodeDecodeError) as exc:
                self.send_html(400, self.page("HealthOS", f"<div class='panel error'>请求内容无效：{html.escape(str(exc))}</div>", session=session))
                return
            supplied_csrf = form.get("csrf", [""])[0]
            if not hmac.compare_digest(supplied_csrf, str(session.get("csrf", ""))):
                if self.wants_json():
                    self.send_json(403, {"error": "csrf_invalid"})
                    return
                self.send_html(403, self.page("HealthOS", "<div class='panel error'>页面验证已失效，请刷新后重试。</div>", session=session))
                return
            if (
                getattr(self.server, "ironbook_read_only", False)  # type: ignore[attr-defined]
                and form.get("training_source", [""])[0] == "ironbook"
                and parsed_path in {"/training", "/training/delete"}
            ):
                self.send_html(403, self.page("力量训练", "<div class='panel error'>Ironbook 当前处于只读预览，训练记录未修改。</div>", session=session))
                return
            if parsed_path == "/logout":
                destroy_session(session_id)
                self.redirect("/login", cookie=self.session_cookie("", max_age=0))
                return
            if parsed_path == "/training":
                if form.get("training_source", [""])[0] == "ironbook":
                    try:
                        records = json.loads(form.get("records_json", ["[]"])[0])
                        incoming_timing = json.loads(form.get("training_timing_json", ["{}"])[0] or "{}")
                        extra_data = json.loads(form.get("ironbook_extra_data", ["{}"])[0] or "{}")
                        if not isinstance(incoming_timing, dict) or not isinstance(extra_data, dict):
                            raise ValueError("训练时间或附加信息格式无效。")
                        if incoming_timing:
                            extra_data["training_timing"] = incoming_timing
                        base_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
                        if not base_url:
                            raise ValueError("尚未配置 Ironbook 连接，训练未保存。")
                        raw_session_id = form.get("session_id", [""])[0]
                        remote_session_id = int(raw_session_id) if raw_session_id else None
                        if remote_session_id is not None and remote_session_id <= 0:
                            raise ValueError("Ironbook 训练编号无效。")
                        result = save_ironbook_training_session(
                            base_url,
                            float(getattr(self.server, "ironbook_timeout_seconds", 8.0)),  # type: ignore[attr-defined]
                            {
                                "session_date": form.get("session_date", [""])[0],
                                "body_part": form.get("body_part", ["其他"])[0],
                                "duration_minutes": form.get("duration_minutes", [""])[0] or None,
                                "sleep_hours": form.get("sleep_hours", [""])[0] or None,
                                "stress_level": form.get("stress_level", [""])[0] or None,
                                "hrv_score": form.get("hrv_score", [""])[0] or None,
                                "feeling": form.get("feeling", [""])[0] or None,
                                "records": records,
                                "extra_data": extra_data,
                            },
                            request_id=form.get("request_id", [""])[0],
                            session_id=remote_session_id,
                        )
                        saved_id = result.get("id")
                        if not isinstance(saved_id, int) or isinstance(saved_id, bool):
                            raise RuntimeError("Ironbook 未返回有效训练编号；请检查 Ironbook 后再重试。")
                    except (ValueError, RuntimeError, TypeError, json.JSONDecodeError) as exc:
                        content = f"<div class='panel error'>Ironbook 未确认训练保存：{html.escape(str(exc))}</div><p><a href='/training?source=ironbook'>返回 Ironbook 训练</a></p>"
                        self.send_html(400, self.page("力量训练", content, session=session))
                        return
                    self.redirect(f"/training?source=ironbook&ironbook_session={saved_id}&saved=1")
                    return
                try:
                    records = json.loads(form.get("records_json", ["[]"])[0])
                    if not isinstance(records, list) or not records:
                        raise ValueError("请至少添加一个动作组。")
                    payload: dict[str, object] = {
                        "request_id": form.get("request_id", [""])[0],
                        "update_session_id": form.get("session_id", [""])[0] or None,
                        "session_date": form.get("session_date", [""])[0],
                        "body_part": form.get("body_part", ["其他"])[0],
                        "duration_minutes": form.get("duration_minutes", [""])[0] or None,
                        "sleep_hours": form.get("sleep_hours", [""])[0] or None,
                        "stress_level": form.get("stress_level", [""])[0] or None,
                        "hrv_score": form.get("hrv_score", [""])[0] or None,
                        "feeling": form.get("feeling", [""])[0] or None,
                        "records": records,
                        "extra_data": {"entry_model": "healthos-training-v1"},
                    }
                    conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
                    try:
                        incoming_timing = json.loads(form.get("training_timing_json", ["{}"])[0] or "{}")
                        if not isinstance(incoming_timing, dict):
                            raise ValueError("training_timing_json must be an object")
                        started_at = parse_datetime(incoming_timing.get("started_at"))
                        ended_at = parse_datetime(incoming_timing.get("ended_at"))
                        if ended_at and not started_at:
                            raise ValueError("training end time requires a start time")
                        if started_at and ended_at and ended_at < started_at:
                            raise ValueError("training end time cannot precede its start time")
                        if payload["update_session_id"] is not None:
                            prior = get_training_session(conn, int(str(payload["update_session_id"])))
                            if prior is None:
                                raise ValueError("训练记录不存在，无法编辑。")
                            for key in ("weekday", "status_note", "raw_markdown", "source_file"):
                                payload[key] = prior.get(key)
                            prior_extra = prior.get("extra_data")
                            payload["extra_data"] = dict(prior_extra) if isinstance(prior_extra, dict) else {}
                        else:
                            payload["extra_data"] = {}
                        payload_extra = payload["extra_data"]
                        assert isinstance(payload_extra, dict)
                        payload_extra["entry_model"] = "healthos-training-v1"
                        if started_at:
                            payload_extra["training_timing"] = incoming_timing
                        created = create_training_session(conn, payload)
                    finally:
                        conn.close()
                except (ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
                    if self.wants_json():
                        self.send_json(400, {"error": "training_not_saved", "detail": str(exc)})
                        return
                    content = f"<div class='panel error'>训练记录未保存：{html.escape(str(exc))}</div><p><a href='/training'>返回力量训练</a></p>"
                    self.send_html(400, self.page("力量训练", content, session=session))
                    return
                if self.wants_json():
                    self.send_json(200, {"item": created})
                    return
                self.redirect(f"/training?session={created['id']}&saved=1")
                return
            if parsed_path == "/training/migration":
                if getattr(self.server, "ironbook_read_only", False):  # type: ignore[attr-defined]
                    self.send_html(403, self.page("Ironbook 训练迁移", "<div class='panel error'>Ironbook 当前处于只读预览，迁移未执行。</div>", session=session))
                    return
                if not getattr(self.server, "ironbook_migration_enabled", False):  # type: ignore[attr-defined]
                    self.send_html(403, self.page("Ironbook 训练迁移", "<div class='panel error'>正式迁移尚未启用。请先完成并验收 HealthOS 的迁移前开发工作。</div>", session=session))
                    return
                if form.get("confirmation", [""])[0] != "确认迁移":
                    self.send_html(400, self.page("Ironbook 训练迁移", "<div class='panel error'>请输入“确认迁移”后再提交。</div>", session=session))
                    return
                base_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
                try:
                    if not base_url:
                        raise ValueError("尚未配置 Ironbook 连接。")
                    approved_fingerprint = form.get("source_fingerprint", [""])[0]
                    timeout_seconds = float(getattr(self.server, "ironbook_timeout_seconds", 8.0))  # type: ignore[attr-defined]
                    history = fetch_ironbook_session_history(base_url, timeout_seconds)
                    summaries = history["sessions"]
                    if not isinstance(summaries, list):
                        raise RuntimeError("Ironbook returned invalid session history")
                    details = fetch_ironbook_session_details(base_url, summaries, timeout_seconds)
                    db_path = Path(self.server.db_path)  # type: ignore[attr-defined]
                    backup_path = backup_sqlite_database(
                        db_path,
                        os.environ.get("HEALTHOS_MIGRATION_BACKUP_DIR") or None,
                    )
                    conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
                    try:
                        result = migrate_ironbook_training_sessions(
                            conn,
                            details,
                            expected_fingerprint=approved_fingerprint,
                            source_full_history_confirmed=bool(history["full_history_confirmed"]),
                            source_total=history["source_total"],
                            backup_path=backup_path,
                        )
                        result["backup_path"] = str(backup_path)
                    finally:
                        conn.close()
                except (ValueError, RuntimeError, sqlite3.Error) as exc:
                    self.send_html(409, self.page("Ironbook 训练迁移", f"<div class='panel error'>迁移未执行：{html.escape(str(exc))}</div><p><a href='/training/migration-preflight'>返回迁移预检</a></p>", session=session))
                    return
                self.redirect(
                    "/training/migration-preflight?migrated=1&sessions="
                    + urllib.parse.quote(str(result["sessions_seen"]))
                )
                return
            if parsed_path == "/training/delete":
                if form.get("training_source", [""])[0] == "ironbook":
                    try:
                        session_id = int(form.get("session_id", [""])[0])
                        if session_id <= 0:
                            raise ValueError("Ironbook 训练编号无效。")
                        base_url = getattr(self.server, "ironbook_base_url", "")  # type: ignore[attr-defined]
                        if not base_url:
                            raise ValueError("尚未配置 Ironbook 连接，训练未删除。")
                        delete_ironbook_training_session(
                            base_url,
                            float(getattr(self.server, "ironbook_timeout_seconds", 8.0)),  # type: ignore[attr-defined]
                            session_id,
                        )
                    except (ValueError, RuntimeError, TypeError) as exc:
                        content = f"<div class='panel error'>Ironbook 未确认训练删除：{html.escape(str(exc))}</div><p><a href='/training?source=ironbook'>返回 Ironbook 训练</a></p>"
                        self.send_html(400, self.page("力量训练", content, session=session))
                        return
                    self.redirect("/training?source=ironbook")
                    return
                try:
                    session_id = int(form.get("session_id", [""])[0])
                    conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
                    try:
                        deleted = delete_training_session(conn, session_id)
                    finally:
                        conn.close()
                except (ValueError, sqlite3.Error) as exc:
                    content = f"<div class='panel error'>训练记录未删除：{html.escape(str(exc))}</div><p><a href='/training'>返回力量训练</a></p>"
                    self.send_html(400, self.page("力量训练", content, session=session))
                    return
                if not deleted:
                    if self.wants_json():
                        self.send_json(404, {"error": "training_session_not_found"})
                        return
                    self.send_html(404, self.page("力量训练", "<div class='panel error'>训练记录不存在，可能已被删除。</div><p><a href='/training'>返回力量训练</a></p>", session=session))
                    return
                if self.wants_json():
                    self.send_json(200, {"deleted": session_id})
                    return
                self.redirect("/training")
                return
            if parsed_path == "/training/aliases":
                try:
                    exercise_id = int(form.get("exercise_id", [""])[0])
                    alias_name = form.get("alias_name", [""])[0]
                    conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
                    try:
                        add_training_exercise_alias(conn, exercise_id, alias_name)
                    finally:
                        conn.close()
                except (ValueError, sqlite3.Error) as exc:
                    if self.wants_json():
                        self.send_json(400, {"error": "alias_not_saved", "detail": str(exc)})
                        return
                    content = f"<div class='panel error'>动作别名未保存：{html.escape(str(exc))}</div><p><a href='/training'>返回力量训练</a></p>"
                    self.send_html(400, self.page("力量训练", content, session=session))
                    return
                if self.wants_json():
                    self.send_json(200, {"saved": True})
                    return
                self.redirect("/training?aliases=saved")
                return
            if parsed_path == "/shares":
                preset = form.get("preset", ["recovery_30d"])[0]
                try:
                    ttl = int(form.get("ttl_minutes", ["15"])[0])
                    max_reads = int(form.get("max_reads", ["5"])[0])
                    snapshot_format = form.get("snapshot_format", ["json"])[0]
                    conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
                    try:
                        token, metadata = create_share_snapshot(
                            conn,
                            {"preset": preset, "include_training": form.get("include_training", [""])[0] == "1"},
                            ttl_minutes=ttl, max_reads=max_reads, snapshot_format=snapshot_format,
                        )
                    finally:
                        conn.close()
                except (ValueError, sqlite3.Error) as exc:
                    self.send_html(400, self.page("分享与导出", f"<div class='panel error'>{html.escape(str(exc))}</div>", session=session))
                    return
                base = f"{self.headers.get('X-Forwarded-Proto', 'http')}://{self.headers.get('Host', 'localhost')}"
                suffix = "?format=html" if metadata["snapshot_format"] == "html" else ""
                link = f"{base}/share/{urllib.parse.quote(token, safe='')}{suffix}"
                content = f"<div class='panel'><p>临时链接已创建，将于 {html.escape(str(metadata['expires_at']))} 过期。</p><label>分享链接<input value='{html.escape(link)}' readonly></label></div>"
                self.send_html(200, self.page("分享链接已创建", content, session=session))
                return
            if parsed_path == "/shares/copy":
                preset = form.get("preset", ["recovery_30d"])[0]
                if preset not in {"recovery_30d", "clinical_90d", "labs_all"}:
                    self.send_html(400, self.page("分享与导出", "<div class='panel error'>所选数据范围无效。</div>", session=session))
                    return
                conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
                try:
                    markdown = format_health_context_markdown(build_health_context(conn, {
                        "preset": preset,
                        "include_training": form.get("include_training", [""])[0] == "1",
                    }))
                finally:
                    conn.close()
                escaped = html.escape(markdown)
                content = f"<div class='panel'><h2>健康摘要文本</h2><textarea id='health-context' rows='24' readonly>{escaped}</textarea><p><button type='button' id='copy-context'>复制摘要</button> <span id='copy-status' class='muted' role='status'></span></p><script>const text=document.getElementById('health-context');const button=document.getElementById('copy-context');const status=document.getElementById('copy-status');button.addEventListener('click',async()=>{{try{{if(navigator.clipboard&&window.isSecureContext){{await navigator.clipboard.writeText(text.value);}}else{{text.focus();text.select();document.execCommand('copy');}}status.textContent='已复制';}}catch(error){{status.textContent='复制失败，请手动选中文本。';text.focus();text.select();}}}});</script></div><p><a href='/shares'>返回分享与导出</a></p>"
                self.send_html(200, self.page("复制到 ChatGPT", content, session=session))
                return
            if parsed_path == "/shares/revoke":
                try:
                    snapshot_id = int(form.get("snapshot_id", [""])[0])
                    conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
                    try:
                        revoke_share_snapshot_id(conn, snapshot_id)
                    finally:
                        conn.close()
                except (ValueError, sqlite3.Error) as exc:
                    self.send_html(400, self.page("分享与导出", f"<div class='panel error'>{html.escape(str(exc))}</div>", session=session))
                    return
                self.redirect("/shares")
                return
            data_type = form.get("data_type", [""])[0]
            values = {key: items[0] for key, items in form.items() if key not in {"csrf", "data_type"}}
            handlers = {
                "lab": (insert_lab_result, "labs"),
                "medication": (insert_medication, "medications"),
                "event": (insert_health_event, "events"),
            }
            if data_type not in handlers:
                self.send_html(400, self.page("手动录入", "<div class='panel error'>无法识别这类记录。</div>", session=session))
                return
            conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
            try:
                result = handlers[data_type][0](conn, values)
                conn.commit()
            except ValueError as exc:
                self.send_html(400, self.page("手动录入", f"<div class='panel error'>{html.escape(str(exc))}</div>", session=session))
                return
            finally:
                conn.close()
            self.redirect(f"/data?domain={handlers[data_type][1]}")
            return
        if not self.authorized():
            self.send_json(401, {"error": "unauthorized"})
            return
        manual_routes = {
            "/api/v1/health/labs": insert_lab_result,
            "/api/v1/health/medications": insert_medication,
            "/api/v1/health/events": insert_health_event,
        }
        if parsed_path not in manual_routes and parsed_path != "/api/v1/health/ingest":
            self.send_json(404, {"error": "not_found"})
            return
        try:
            payload = self.read_json_body()
            conn = init_db(self.server.db_path)  # type: ignore[attr-defined]
            try:
                if parsed_path == "/api/v1/health/ingest":
                    result = ingest_batch(payload, conn, self.server.raw_dir)  # type: ignore[attr-defined]
                else:
                    result = manual_routes[parsed_path](conn, payload)
                    conn.commit()
            finally:
                conn.close()
            self.send_json(200, result)
        except (ValueError, json.JSONDecodeError) as exc:
            status = 413 if str(exc) == "body_too_large" else 400
            self.send_json(status, {"error": str(exc)})
        except Exception:
            self.send_json(500, {"error": "ingest_failed"})


def serve(db_path: str, raw_dir: str, host: str, port: int) -> None:
    conn = init_db(db_path)
    conn.close()
    server = http.server.ThreadingHTTPServer((host, port), HealthOSHandler)
    server.db_path = db_path  # type: ignore[attr-defined]
    server.raw_dir = Path(raw_dir)  # type: ignore[attr-defined]
    server.ironbook_base_url = os.environ.get("HEALTHOS_IRONBOOK_BASE_URL", "").strip()  # type: ignore[attr-defined]
    server.ironbook_read_only = os.environ.get("HEALTHOS_IRONBOOK_READ_ONLY", "").strip().lower() in {"1", "true", "yes", "on"}  # type: ignore[attr-defined]
    server.ironbook_migration_enabled = os.environ.get("HEALTHOS_IRONBOOK_MIGRATION_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}  # type: ignore[attr-defined]
    try:
        server.ironbook_timeout_seconds = max(1.0, float(os.environ.get("HEALTHOS_IRONBOOK_TIMEOUT_SECONDS", "8")))  # type: ignore[attr-defined]
    except ValueError:
        server.ironbook_timeout_seconds = 8.0  # type: ignore[attr-defined]
    print(f"HealthOS listening on {host}:{port}", file=sys.stderr)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("import-xml", "query", "serve", "hash-password"):
        sub.add_parser(command)
    imp = sub.choices["import-xml"]
    imp.add_argument("path")
    imp.add_argument("--db", default=DEFAULT_DB)
    imp.add_argument("--max-records", type=int)
    imp.add_argument("--progress-every", type=int, default=100_000)
    q = sub.choices["query"]
    q.add_argument("--db", default=DEFAULT_DB)
    q.add_argument("--metric")
    q.add_argument("--limit", type=int, default=20)
    s = sub.choices["serve"]
    s.add_argument("--db", default=DEFAULT_DB)
    s.add_argument("--raw-dir", default=DEFAULT_RAW)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8789)
    args = parser.parse_args()
    if args.command == "import-xml":
        print(json.dumps(import_xml(args.path, args.db, args.max_records, args.progress_every), ensure_ascii=False, indent=2))
    elif args.command == "query":
        conn = init_db(args.db)
        print(json.dumps(query_observations(conn, metric=args.metric, limit=max(1, min(args.limit, 1000))), ensure_ascii=False, indent=2))
        conn.close()
    elif args.command == "hash-password":
        import getpass
        print(hash_password(getpass.getpass("Owner password: ")))
    else:
        if not os.environ.get("HEALTHOS_API_TOKEN") and not os.environ.get("HEALTHOS_API_TOKEN_FILE"):
            parser.error("serve requires HEALTHOS_API_TOKEN or HEALTHOS_API_TOKEN_FILE")
        serve(args.db, args.raw_dir, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
