from __future__ import annotations

import json
import sqlite3
import time
from .events import event_from_raw, quoted_message

STATUSES = {'queued', 'downloading', 'ready', 'processing', 'processed', 'failed'}
REACTIONS = {'processing': '👀', 'processed': '👍', 'failed': '👎'}


class Store:
    def __init__(self, path, clock=time.time):
        self.clock = clock
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
                seq INTEGER PRIMARY KEY, chat INTEGER NOT NULL, message INTEGER NOT NULL,
                raw TEXT NOT NULL, event TEXT, status TEXT NOT NULL DEFAULT 'queued',
                turn TEXT, error TEXT, UNIQUE(chat,message));
            CREATE TABLE IF NOT EXISTS outbox (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                result TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS lifecycle (
                id INTEGER PRIMARY KEY, seq INTEGER NOT NULL, status TEXT NOT NULL,
                at REAL NOT NULL, reason TEXT);
            CREATE TABLE IF NOT EXISTS messages (
                chat INTEGER NOT NULL, message INTEGER NOT NULL, direction TEXT NOT NULL,
                event TEXT NOT NULL, PRIMARY KEY(chat,message));
            CREATE TABLE IF NOT EXISTS outgoing_fragments (
                delivery TEXT NOT NULL, part INTEGER NOT NULL, message INTEGER NOT NULL,
                PRIMARY KEY(delivery,part));
        ''')
        # Additive migration preserves the original durable queue and outbox.
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(inbox)')}
        additions = {
            'created_at': 'REAL', 'updated_at': 'REAL', 'processing_at': 'REAL',
            'reminded_at': 'REAL', 'deadline_at': 'REAL', 'replied_at': 'REAL',
            'resolution': 'TEXT', 'ignored_at': 'REAL', 'ignore_reason': 'TEXT',
            'dispatch_state': "TEXT NOT NULL DEFAULT 'idle'", 'reminder_state': "TEXT NOT NULL DEFAULT 'idle'",
            'dispatch_at': 'REAL', 'reminder_at': 'REAL', 'attempts': 'INTEGER NOT NULL DEFAULT 0',
            'download_retry_at': 'REAL', 'reaction': "TEXT NOT NULL DEFAULT ''",
            'reaction_dirty': 'INTEGER NOT NULL DEFAULT 0', 'reaction_error': 'TEXT', 'reaction_retry_at': 'REAL'}
        with self.db:
            for name, definition in additions.items():
                if name not in columns:
                    self.db.execute(f'ALTER TABLE inbox ADD COLUMN {name} {definition}')
            now = self.clock()
            self.db.execute('UPDATE inbox SET created_at=COALESCE(created_at,?),updated_at=COALESCE(updated_at,?)', (now, now))
            self.db.execute("UPDATE inbox SET status='processed',reaction='👍',reaction_dirty=1 WHERE status='done'")
            self.db.execute("UPDATE inbox SET resolution='replied' WHERE status='processed' AND replied_at IS NOT NULL AND resolution IS NULL")
            self.db.execute("UPDATE inbox SET status='processing',reaction='👀',reaction_dirty=1,processing_at=?,deadline_at=? WHERE status='running'", (now, now+300))
            self.db.execute("UPDATE inbox SET status='ready',dispatch_state='uncertain' WHERE status IN ('starting','uncertain')")
            self.db.execute("UPDATE inbox SET dispatch_state='uncertain' WHERE dispatch_state='inflight'")
            self.db.execute("UPDATE inbox SET reminder_state='uncertain' WHERE reminder_state='inflight'")
            self.db.execute("UPDATE outbox SET status='uncertain',error='process exited during send' WHERE status='sending'")
            for status, emoji in REACTIONS.items():
                self.db.execute('UPDATE inbox SET reaction=?,reaction_dirty=1 WHERE status=? AND reaction!=?', (emoji,status,emoji))
            for row in self.db.execute('SELECT * FROM inbox').fetchall():
                event = json.loads(row['event']) if row['event'] else self._raw_event(row['raw'])
                if row['event']:
                    # Upgrade saved events with metadata from their original Telegram update.
                    original = self._raw_event(row['raw'])
                    event = {**original,**event}
                    for key in ('author','username','mention','reply_to','quote'):
                        if key not in event and key in original:
                            event[key] = original[key]
                    event = self.with_reply_context(event)
                    self.db.execute('UPDATE inbox SET event=? WHERE seq=?',(json.dumps(event,ensure_ascii=False),row['seq']))
                self._save_message(row['chat'], row['message'], 'in', event)

    @staticmethod
    def _raw_event(raw):
        return event_from_raw(raw)

    def with_reply_context(self, event):
        event = dict(event)
        parent_id = event.get('reply_to_message_id')
        if parent_id and not event.get('reply_to'):
            parent = self.db.execute('SELECT event FROM messages WHERE chat=? AND message=?', (event['chat_id'],parent_id)).fetchone()
            if parent:
                event['reply_to'] = quoted_message(json.loads(parent['event']),'local_history')
            else:
                event['reply_to'] = {'chat_id':event['chat_id'],'message_id':parent_id,'text':None,'author':None,'available':False}
        return event

    def _save_message(self, chat, message, direction, event):
        event = self.with_reply_context({'chat_id':chat,'message_id':message,**event})
        self.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?)',
                        (chat, message, direction, json.dumps(event, ensure_ascii=False)))

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def put(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))

    def ingest(self, update_id, message=None):
        with self.db:
            if message:
                now = self.clock()
                result = self.db.execute('INSERT OR IGNORE INTO inbox(chat,message,raw,created_at,updated_at) VALUES (?,?,?,?,?)',
                    (message.chat.id, message.message_id, message.model_dump_json(), now, now))
                if result.rowcount:
                    self._save_message(message.chat.id, message.message_id, 'in', self._raw_event(message.model_dump_json()))
                    self.db.execute("INSERT INTO lifecycle(seq,status,at) VALUES (?,'queued',?)", (result.lastrowid, now))
            offset = max(int(self.get('offset', '0')), update_id+1)
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset',?)", (str(offset),))

    def row(self, seq):
        return self.db.execute('SELECT * FROM inbox WHERE seq=?', (seq,)).fetchone()

    def head(self):
        return self.db.execute("SELECT * FROM inbox WHERE status NOT IN ('processed','failed') ORDER BY seq LIMIT 1").fetchone()

    def download_head(self):
        return self.db.execute("SELECT * FROM inbox WHERE status IN ('queued','downloading') OR (status='processed' AND event IS NULL) ORDER BY seq LIMIT 1").fetchone()

    def dispatch_head(self):
        # A downloading message forms a barrier, but a processing one does not.
        return self.db.execute("SELECT * FROM inbox WHERE status IN ('queued','downloading','ready') OR (status='processed' AND event IS NULL) ORDER BY seq LIMIT 1").fetchone()

    def _update(self, seq, values):
        allowed = {'event','status','turn','error','updated_at','processing_at','reminded_at','deadline_at','replied_at',
                   'resolution','ignored_at','ignore_reason',
                   'dispatch_state','reminder_state','dispatch_at','reminder_at','attempts','download_retry_at',
                   'reaction','reaction_dirty','reaction_error','reaction_retry_at'}
        if not values or not set(values) <= allowed:
            raise ValueError('Invalid inbox update')
        self.db.execute('UPDATE inbox SET '+','.join(f'{key}=?' for key in values)+' WHERE seq=?', (*values.values(), seq))

    def update(self, seq, **values):
        # Internal transport fields only; status changes always go through transition().
        if 'status' in values:
            raise ValueError('Use transition for lifecycle changes')
        with self.db:
            self._update(seq, values)
            if 'event' in values:
                row = self.row(seq)
                self._save_message(row['chat'], row['message'], 'in', json.loads(values['event']))

    def _transition(self, seq, status, reason=None, **values):
        if status not in STATUSES:
            raise ValueError('Invalid lifecycle status')
        previous = self.row(seq)
        now = self.clock()
        if previous['status'] != status:
            self.db.execute('INSERT INTO lifecycle(seq,status,at,reason) VALUES (?,?,?,?)', (seq,status,now,reason))
        self._update(seq, {'status':status,'updated_at':now,**values})
        desired = REACTIONS.get(status, '')
        if desired != previous['reaction']:
            self._update(seq, {'reaction':desired,'reaction_dirty':1,'reaction_error':None,'reaction_retry_at':None})

    def transition(self, seq, status, reason=None, **values):
        with self.db:
            self._transition(seq, status, reason, **values)

    def downloaded(self, seq, event):
        with self.db:
            row = self.row(seq)
            event = self.with_reply_context(event)
            encoded = json.dumps(event, ensure_ascii=False)
            self._update(seq, {'event':encoded,'download_retry_at':None,'error':None})
            self._save_message(row['chat'],row['message'],'in',event)
            if row['status'] != 'processed':
                self._transition(seq, 'ready')

    def delivered_input(self, seq, turn, reminder=False, at=None):
        with self.db:
            row = self.row(seq)
            now = self.clock() if at is None else at
            if reminder:
                self._update(seq, {'reminder_state':'sent','turn':turn})
                if row['status'] == 'processing':
                    self._update(seq, {'reminded_at':now,'deadline_at':now+300,'error':None})
            else:
                self._update(seq, {'dispatch_state':'sent','turn':turn})
                if row['status'] not in {'processed','failed'}:
                    self._transition(seq,'processing',processing_at=now,deadline_at=now+300,error=None)

    def processing(self):
        return self.db.execute("SELECT * FROM inbox WHERE status='processing' ORDER BY seq").fetchall()

    def reactions(self):
        return self.db.execute('SELECT * FROM inbox WHERE reaction_dirty=1 AND COALESCE(reaction_retry_at,0)<=? ORDER BY seq', (self.clock(),)).fetchall()

    def reaction_done(self, seq, emoji, error=None):
        with self.db:
            # A reply may change the desired reaction while Telegram request is in flight.
            self.db.execute('UPDATE inbox SET reaction_dirty=0,reaction_error=?,reaction_retry_at=NULL WHERE seq=? AND reaction=?', (error,seq,emoji))

    def enqueue_send(self, key, payload):
        data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO outbox(id,payload) VALUES (?,?)', (key,data))
        row = self.outgoing(key)
        if row['payload'] != data:
            raise ValueError('request_id already used for another payload')
        return row

    def outgoing(self, key):
        return self.db.execute('SELECT * FROM outbox WHERE id=?', (key,)).fetchone()

    def delivery(self, key, status, result=None, error=None):
        with self.db:
            self.db.execute('UPDATE outbox SET status=?,result=?,error=? WHERE id=?',
                            (status,json.dumps(result) if result is not None else None,error,key))

    def ignore(self, chat, message_id, reason=''):
        """Idempotently acknowledge a known input without pretending to send a reply."""
        if type(message_id) is not int or message_id<=0:
            raise ValueError('message_id must be a positive integer')
        if not isinstance(reason,str) or len(reason)>500:
            raise ValueError('Ignore reason must be text up to 500 characters')
        with self.db:
            row=self.db.execute('SELECT * FROM inbox WHERE chat=? AND message=?',(chat,message_id)).fetchone()
            if row is None:
                raise ValueError('Unknown incoming message in this chat')
            changed=row['status']!='processed'
            if changed:
                self._transition(row['seq'],'processed',reason='Agent acknowledged without a reply',
                    resolution='ignored',ignored_at=self.clock(),ignore_reason=reason,deadline_at=None,error=None)
            result=self.row(row['seq'])
            return {'message_id':message_id,'status':result['status'],'resolution':result['resolution'],
                    'ignored_at':result['ignored_at'],'changed':changed}

    def record_fragment(self, key, part, message_id, payload, text='', attachments=None, sender=None):
        # Telegram success receipt, history and processed status commit atomically.
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO outgoing_fragments VALUES (?,?,?)', (key,part,message_id))
            event = {'source':'telegram','chat_id':payload['chat_id'],'message_id':message_id,
                     'date':self.clock(),**(sender or {'sender':'Codex','author':{'type':'bot','name':'Codex','is_bot':True}}),
                     'text':text,'attachments':attachments or [],
                     'reply_to_message_id':payload.get('reply_to')}
            self._save_message(payload['chat_id'],message_id,'out',event)
            ids = [r[0] for r in self.db.execute('SELECT message FROM outgoing_fragments WHERE delivery=? ORDER BY part',(key,))]
            self.db.execute('UPDATE outbox SET result=? WHERE id=?', (json.dumps(ids),key))
            if payload.get('reply_to'):
                row = self.db.execute('SELECT * FROM inbox WHERE chat=? AND message=?', (payload['chat_id'],payload['reply_to'])).fetchone()
                if row:
                    self._transition(row['seq'],'processed',reason='Telegram reply delivered',
                        resolution='replied',replied_at=self.clock(),deadline_at=None,error=None)

    def history(self, chat, limit=50, before=None, after=None, message_id=None, status=None):
        if not isinstance(limit,int) or not 1 <= limit <= 100:
            raise ValueError('History limit must be 1..100')
        if status is not None and status not in STATUSES:
            raise ValueError('Invalid history status')
        if any(value is not None and (not isinstance(value,int) or value<=0) for value in (before,after,message_id)):
            raise ValueError('History message IDs must be positive integers')
        clauses, values = ['m.chat=?'], [chat]
        for value, expression in [(before,'m.message<?'),(after,'m.message>?'),(message_id,'m.message=?'),(status,'i.status=?')]:
            if value is not None:
                clauses.append(expression);values.append(value)
        ascending = after is not None
        sql = ('SELECT m.*,i.status,i.error,i.processing_at,i.reminded_at,i.replied_at,i.resolution,i.ignored_at,i.ignore_reason,i.dispatch_state,i.reminder_state FROM messages m '
               'LEFT JOIN inbox i ON i.chat=m.chat AND i.message=m.message WHERE '+' AND '.join(clauses)+
               ' ORDER BY m.message '+('ASC' if ascending else 'DESC')+' LIMIT ?')
        rows = self.db.execute(sql,(*values,limit+1)).fetchall()
        more = len(rows)>limit
        rows = rows[:limit]
        if not ascending:
            rows.reverse()
        messages = [{**json.loads(r['event']), 'direction':r['direction'],'status':r['status'],
                     'error':r['error'],'processing_at':r['processing_at'],'reminded_at':r['reminded_at'],
                     'replied_at':r['replied_at'],'resolution':r['resolution'],'ignored_at':r['ignored_at'],
                     'ignore_reason':r['ignore_reason'],'delivery_state':r['dispatch_state'],'reminder_state':r['reminder_state']} for r in rows]
        return {'messages':messages,'has_more':more,'next_cursor':(rows[-1 if ascending else 0]['message'] if rows and more else None),
                'cursor_direction':'after' if ascending else 'before','scope':'locally received messages and confirmed bot sends'}

    def close(self):
        self.db.close()
