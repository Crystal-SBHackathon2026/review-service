import session from 'express-session';

const DAY_MS = 24 * 60 * 60 * 1000;

// express-session 저장소 — 세션을 앱 DB 와 같은 SQLite 파일에 둔다 (replicas 1 전제)
export class SqliteSessionStore extends session.Store {
  constructor(db) {
    super();
    this.db = db;
    db.exec('CREATE TABLE IF NOT EXISTS sessions (sid TEXT PRIMARY KEY, sess TEXT NOT NULL, expires INTEGER NOT NULL)');
  }

  get(sid, cb) {
    const row = this.db.prepare('SELECT sess, expires FROM sessions WHERE sid = ?').get(sid);
    if (!row || row.expires < Date.now()) return cb(null, null);
    cb(null, JSON.parse(row.sess));
  }

  set(sid, sess, cb) {
    const expires = sess.cookie?.expires ? new Date(sess.cookie.expires).getTime() : Date.now() + DAY_MS;
    this.db.prepare('INSERT INTO sessions (sid, sess, expires) VALUES (?, ?, ?) ON CONFLICT(sid) DO UPDATE SET sess = excluded.sess, expires = excluded.expires')
      .run(sid, JSON.stringify(sess), expires);
    cb?.(null);
  }

  destroy(sid, cb) {
    this.db.prepare('DELETE FROM sessions WHERE sid = ?').run(sid);
    cb?.(null);
  }
}
