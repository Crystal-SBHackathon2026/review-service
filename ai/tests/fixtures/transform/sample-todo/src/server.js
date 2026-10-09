import path from 'node:path';
import express from 'express';
import session from 'express-session';
import bcrypt from 'bcryptjs';
import Database from 'better-sqlite3';
import { SqliteSessionStore } from './sqlite-session-store.js';

const PORT = Number(process.env.PORT ?? 3000);
const { DATA_DIR, SESSION_SECRET } = process.env;
if (!DATA_DIR) throw new Error('DATA_DIR is required (persistent volume mount)');
if (!SESSION_SECRET) throw new Error('SESSION_SECRET is required');

const db = new Database(path.join(DATA_DIR, 'todo.db'));
db.pragma('journal_mode = WAL');
db.exec(`
  CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
  );
  CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
  );
`);

const app = express();

app.set('trust proxy', 1);
app.use(express.json());
app.use(express.urlencoded({ extended: false }));
app.use(session({
  store: new SqliteSessionStore(db),
  secret: SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: { httpOnly: true, sameSite: 'lax' },
}));
app.use(express.static('public'));

app.get('/livez', (_req, res) => res.json({ status: 'ok' }));
app.get('/readyz', (_req, res) => {
  try {
    db.prepare('SELECT 1').get();
    res.json({ status: 'ready' });
  } catch (err) {
    res.status(503).json({ status: 'not ready', error: err.message });
  }
});

function requireLogin(req, res, next) {
  if (!req.session.userId) return res.status(401).json({ error: 'login required' });
  next();
}

app.post('/api/signup', async (req, res) => {
  const { username, password } = req.body;
  if (!username || !password) return res.status(400).json({ error: 'username and password required' });
  const hash = await bcrypt.hash(password, 10);
  const row = db.prepare('INSERT INTO users (username, password_hash) VALUES (?, ?) ON CONFLICT (username) DO NOTHING RETURNING id')
    .get(username, hash);
  if (!row) return res.status(409).json({ error: 'username taken' });
  res.status(201).json({ id: row.id, username });
});

app.post('/api/login', async (req, res) => {
  const { username, password } = req.body;
  const user = db.prepare('SELECT * FROM users WHERE username = ?').get(username);
  if (!user || !(await bcrypt.compare(password ?? '', user.password_hash))) {
    return res.status(401).json({ error: 'invalid credentials' });
  }
  req.session.userId = user.id;
  if (req.is('application/x-www-form-urlencoded')) return res.redirect('/');
  res.json({ id: user.id, username: user.username });
});

app.post('/api/logout', (req, res) => {
  req.session.destroy(() => res.status(204).end());
});

app.get('/api/todos', requireLogin, (req, res) => {
  const rows = db.prepare('SELECT id, title, created_at FROM todos WHERE user_id = ? ORDER BY id').all(req.session.userId);
  res.json(rows);
});

app.post('/api/todos', requireLogin, (req, res) => {
  const { title } = req.body;
  if (!title) return res.status(400).json({ error: 'title required' });
  const row = db.prepare('INSERT INTO todos (user_id, title) VALUES (?, ?) RETURNING id, title').get(req.session.userId, title);
  res.status(201).json(row);
});

app.use((err, _req, res, _next) => {
  console.error(err);
  res.status(500).json({ error: 'internal error' });
});

const server = app.listen(PORT, () => console.log(`todo listening on :${PORT}`));

process.on('SIGTERM', () => {
  server.close(() => {
    db.close();
    process.exit(0);
  });
});
