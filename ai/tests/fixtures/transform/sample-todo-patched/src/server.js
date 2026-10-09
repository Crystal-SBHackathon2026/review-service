import express from 'express';
import session from 'express-session';
import connectPgSimple from 'connect-pg-simple';
import bcrypt from 'bcryptjs';
import pg from 'pg';
import client from 'prom-client';

const PORT = Number(process.env.PORT ?? 3000);
const { DATABASE_URL, SESSION_SECRET } = process.env;
if (!DATABASE_URL) throw new Error('DATABASE_URL is required');
if (!SESSION_SECRET) throw new Error('SESSION_SECRET is required');

const pool = new pg.Pool({
  connectionString: DATABASE_URL,
  ssl: process.env.DATABASE_SSL === 'require' ? { rejectUnauthorized: false } : undefined,
});


const PgStore = connectPgSimple(session);
const app = express();

// Prometheus 지표 — route 라벨은 실제 URL 이 아니라 등록한 경로라 라벨 수가 늘지 않는다
client.collectDefaultMetrics();
const httpRequests = new client.Counter({
  name: 'http_requests_total', help: 'HTTP 요청 수', labelNames: ['method', 'route', 'status'],
});
const httpDuration = new client.Histogram({
  name: 'http_request_duration_seconds', help: 'HTTP 처리 시간', labelNames: ['method', 'route', 'status'],
});
app.use((req, res, next) => {
  const end = httpDuration.startTimer();
  res.on('finish', () => {
    const labels = { method: req.method, route: req.route?.path ?? 'unmatched', status: res.statusCode };
    httpRequests.inc(labels);
    end(labels);
  });
  next();
});
app.get('/metrics', async (_req, res) => {
  res.set('Content-Type', client.register.contentType);
  res.end(await client.register.metrics());
});

app.set('trust proxy', 1);
app.use(express.json());
app.use(express.urlencoded({ extended: false }));
app.use(session({
  store: new PgStore({ pool }),
  secret: SESSION_SECRET,
  resave: false,
  saveUninitialized: false,
  cookie: { httpOnly: true, sameSite: 'lax' },
}));
app.use(express.static('public'));

app.get('/livez', (_req, res) => res.json({ status: 'ok' }));
app.get('/readyz', async (_req, res) => {
  try {
    await pool.query('SELECT 1');
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
  const { rows } = await pool.query(
    'INSERT INTO users (username, password_hash) VALUES ($1, $2) ON CONFLICT (username) DO NOTHING RETURNING id',
    [username, hash],
  );
  if (rows.length === 0) return res.status(409).json({ error: 'username taken' });
  res.status(201).json({ id: rows[0].id, username });
});

app.post('/api/login', async (req, res) => {
  const { username, password } = req.body;
  const { rows } = await pool.query('SELECT * FROM users WHERE username = $1', [username]);
  const user = rows[0];
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

app.get('/api/todos', requireLogin, async (req, res) => {
  const { rows } = await pool.query(
    'SELECT id, title, created_at FROM todos WHERE user_id = $1 ORDER BY id',
    [req.session.userId],
  );
  res.json(rows);
});

app.post('/api/todos', requireLogin, async (req, res) => {
  const { title } = req.body;
  if (!title) return res.status(400).json({ error: 'title required' });
  const { rows } = await pool.query(
    'INSERT INTO todos (user_id, title) VALUES ($1, $2) RETURNING id, title',
    [req.session.userId, title],
  );
  res.status(201).json(rows[0]);
});

app.use((err, _req, res, _next) => {
  console.error(err);
  res.status(500).json({ error: 'internal error' });
});

const server = app.listen(PORT, () => console.log(`todo listening on :${PORT}`));

process.on('SIGTERM', () => {
  server.close(() => pool.end().then(() => process.exit(0)));
});
