#!/usr/bin/env python3
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict

DB_FILE = 'business_context.db'


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def context_store_path(root: str) -> str:
    return os.path.join(root, 'state', DB_FILE)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def _connect(root: str) -> sqlite3.Connection:
    path = context_store_path(root)
    _ensure_parent(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def init_schema(root: str) -> Dict[str, Any]:
    conn = _connect(root)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS customers (
                id TEXT PRIMARY KEY,
                name TEXT,
                requirements TEXT,
                tech_stack TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS papers (
                id TEXT PRIMARY KEY,
                title TEXT,
                authors TEXT,
                arxiv_id TEXT,
                difficulty_score REAL,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE IF NOT EXISTS reproduction_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id TEXT,
                success INTEGER,
                issues TEXT,
                lessons_learned TEXT,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reproduction_history_paper_id
            ON reproduction_history(paper_id, id DESC);
            """
        )
    finally:
        conn.close()
    return {'ok': True, 'dbPath': context_store_path(root)}


def _row_to_customer(row: sqlite3.Row | None) -> Dict[str, Any]:
    if row is None:
        return {}
    return {
        'id': row['id'],
        'name': row['name'] or '',
        'requirements': row['requirements'] or '',
        'techStack': row['tech_stack'] or '',
        'createdAt': row['created_at'] or '',
        'updatedAt': row['updated_at'] or '',
    }


def _row_to_paper(row: sqlite3.Row | None) -> Dict[str, Any]:
    if row is None:
        return {}
    return {
        'id': row['id'],
        'title': row['title'] or '',
        'authors': row['authors'] or '',
        'arxivId': row['arxiv_id'] or '',
        'difficultyScore': row['difficulty_score'],
        'createdAt': row['created_at'] or '',
        'updatedAt': row['updated_at'] or '',
    }


def _row_to_history(row: sqlite3.Row | None) -> Dict[str, Any]:
    if row is None:
        return {}
    return {
        'id': row['id'],
        'paperId': row['paper_id'] or '',
        'success': bool(row['success']),
        'issues': row['issues'] or '',
        'lessonsLearned': row['lessons_learned'] or '',
        'createdAt': row['created_at'] or '',
    }


def upsert_customer(root: str, customer_id: str, name: str = '', requirements: str = '', tech_stack: str = '') -> Dict[str, Any]:
    init_schema(root)
    ts = now_iso()
    conn = _connect(root)
    try:
        existing = conn.execute('SELECT created_at FROM customers WHERE id = ?', (customer_id,)).fetchone()
        created_at = existing['created_at'] if existing and existing['created_at'] else ts
        conn.execute(
            '''
            INSERT INTO customers(id, name, requirements, tech_stack, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              requirements=excluded.requirements,
              tech_stack=excluded.tech_stack,
              updated_at=excluded.updated_at
            ''',
            (customer_id, name, requirements, tech_stack, created_at, ts),
        )
        conn.commit()
        row = conn.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()
    finally:
        conn.close()
    return {'ok': True, 'customer': _row_to_customer(row)}


def get_customer(root: str, customer_id: str) -> Dict[str, Any]:
    init_schema(root)
    conn = _connect(root)
    try:
        row = conn.execute('SELECT * FROM customers WHERE id = ?', (customer_id,)).fetchone()
    finally:
        conn.close()
    return {'ok': True, 'customer': _row_to_customer(row)}


def upsert_paper(root: str, paper_id: str, title: str = '', authors: str = '', arxiv_id: str = '', difficulty_score: float | None = None) -> Dict[str, Any]:
    init_schema(root)
    ts = now_iso()
    conn = _connect(root)
    try:
        existing = conn.execute('SELECT created_at FROM papers WHERE id = ?', (paper_id,)).fetchone()
        created_at = existing['created_at'] if existing and existing['created_at'] else ts
        conn.execute(
            '''
            INSERT INTO papers(id, title, authors, arxiv_id, difficulty_score, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              title=excluded.title,
              authors=excluded.authors,
              arxiv_id=excluded.arxiv_id,
              difficulty_score=excluded.difficulty_score,
              updated_at=excluded.updated_at
            ''',
            (paper_id, title, authors, arxiv_id, difficulty_score, created_at, ts),
        )
        conn.commit()
        row = conn.execute('SELECT * FROM papers WHERE id = ?', (paper_id,)).fetchone()
    finally:
        conn.close()
    return {'ok': True, 'paper': _row_to_paper(row)}


def get_paper(root: str, paper_id: str) -> Dict[str, Any]:
    init_schema(root)
    conn = _connect(root)
    try:
        row = conn.execute('SELECT * FROM papers WHERE id = ?', (paper_id,)).fetchone()
    finally:
        conn.close()
    return {'ok': True, 'paper': _row_to_paper(row)}


def add_reproduction_history(root: str, paper_id: str, success: bool, issues: str = '', lessons_learned: str = '') -> Dict[str, Any]:
    init_schema(root)
    ts = now_iso()
    conn = _connect(root)
    try:
        cur = conn.execute(
            'INSERT INTO reproduction_history(paper_id, success, issues, lessons_learned, created_at) VALUES (?, ?, ?, ?, ?)',
            (paper_id, 1 if success else 0, issues, lessons_learned, ts),
        )
        conn.commit()
        row = conn.execute('SELECT * FROM reproduction_history WHERE id = ?', (cur.lastrowid,)).fetchone()
    finally:
        conn.close()
    return {'ok': True, 'item': _row_to_history(row)}


def list_reproduction_history(root: str, paper_id: str, limit: int = 5) -> Dict[str, Any]:
    init_schema(root)
    conn = _connect(root)
    try:
        rows = conn.execute(
            'SELECT * FROM reproduction_history WHERE paper_id = ? ORDER BY id DESC LIMIT ?',
            (paper_id, max(1, int(limit))),
        ).fetchall()
    finally:
        conn.close()
    return {'ok': True, 'items': [_row_to_history(row) for row in rows]}


def build_prompt_context(root: str, customer_id: str = '', paper_id: str = '', history_limit: int = 3) -> Dict[str, Any]:
    customer = get_customer(root, customer_id).get('customer') if customer_id else {}
    paper = get_paper(root, paper_id).get('paper') if paper_id else {}
    history = list_reproduction_history(root, paper_id, limit=history_limit).get('items') if paper_id else []
    payload: Dict[str, Any] = {}
    if customer:
        payload['customer'] = customer
    if paper:
        payload['paper'] = paper
    if history:
        payload['reproductionHistory'] = history
    return payload
