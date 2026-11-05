"""Квадратний наставник. Python 3.11+, standard library only."""
import csv
import io
import json
import logging
import os
from pathlib import Path
import secrets
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from math_core import BANKS, parse_answer, error_kind

ROOT = Path(__file__).resolve().parent
HELP = ('Команди:\n/task — завдання\n/hint — підказка\n/solution — завершити навчальне завдання з розв’язанням'
        '\n/theory — теорія\n/stats — мій поступ\n/mode standard або /mode ai — режим у вільному навчанні'
        '\n/privacy — дані\n/delete — видалення моїх даних\n/id — мій Telegram ID'
        '\nВідповідь: 2; -1/2 або немає. Кома означає десятковий дріб, НЕ роздільник коренів.')
THEORY = ('Квадратне рівняння: ax² + bx + c = 0, a ≠ 0.\n'
          'D = b² − 4ac.\nD > 0: x₁,₂ = (−b ± √D)/(2a).\n'
          'D = 0: x = −b/(2a).\nD < 0: дійсних коренів немає.\n'
          'Якщо c = 0, винеси x за дужки. Не діли на x: так можна втратити корінь 0.\n'
          'Обидва знайдені корені перевір підстановкою. У нашому банку корені раціональні.')
PRIVACY = ('Бот зберігає на комп’ютері керівника Telegram ID, випадковий код учасника, режим, етап, '
           'правильність відповідей, кількість спроб, підказки й часові позначки. Ім’я та username не записуються. '
           'Це псевдонімізовані, а не повністю анонімні дані. У CSV немає Telegram ID. '
           'У режимі ШІ OpenAI отримує тільки рівняння, номер підказки, кількість спроб і категорію помилки. '
           'Текст твоїх повідомлень та ідентифікатори не передаються OpenAI. Сам Telegram обробляє повідомлення '
           'за власними правилами. /delete видаляє записи з поточної бази бота, але не з Telegram, '
           'раніше експортованих файлів або резервних копій. Не надсилай особисті відомості. '
           'Участь у шкільному дослідженні погоджується окремо з керівником; команда /agree цього не замінює.')
MENU = {'keyboard': [[{'text': '/task'}, {'text': '/hint'}],
                     [{'text': '/theory'}, {'text': '/stats'}], [{'text': '/help'}]],
        'resize_keyboard': True}


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def load_env(path):
    if not path.exists():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        k, sep, v = line.partition('=')
        if not sep or not k.strip().replace('_', '').isalnum():
            raise ValueError('Некоректний рядок .env')
        os.environ.setdefault(k.strip(), v.strip().strip('\"\''))


class ApiError(Exception):
    def __init__(self, status=0, retry_after=0):
        # Never include raw URLs or response bodies: tokens may occur there.
        super().__init__(f'HTTP/API {status}')
        self.status, self.retry_after = status, retry_after


def request_json(url, payload, headers=None, timeout=40):
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        delay = 0
        try:
            delay = int(json.loads(e.read()).get('parameters', {}).get('retry_after', 0))
        except (ValueError, TypeError):
            pass
        raise ApiError(e.code, delay) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise ApiError() from None


class Telegram:
    def __init__(self, token):
        self.base = f'https://api.telegram.org/bot{token}/'

    def call(self, method, **payload):
        result = request_json(self.base + method, payload)
        if not result.get('ok'):
            raise ApiError(result.get('error_code', 0))
        return result['result']

    def send(self, uid, text):
        # Plain text, no HTML/Markdown injection, messages below Telegram limit.
        self.call('sendMessage', chat_id=uid, text=text[:3900], reply_markup=MENU)

    def document(self, uid, content):
        boundary = secrets.token_hex(16)
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n{uid}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="document"; filename="results.csv"\r\n'
                'Content-Type: text/csv\r\n\r\n').encode()
        body = head + content + f'\r\n--{boundary}--\r\n'.encode()
        req = urllib.request.Request(self.base + 'sendDocument', body,
              {'Content-Type': 'multipart/form-data; boundary=' + boundary})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.load(r)
            if not data.get('ok'):
                raise ApiError(data.get('error_code', 0))
        except urllib.error.HTTPError as e:
            raise ApiError(e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise ApiError() from None


def ai_hint(task, level, attempts, error, key, model):
    # Only program-generated fields go to the model. No chat history or free text.
    response = request_json('https://api.openai.com/v1/responses', {
        'model': model, 'store': False, 'max_output_tokens': 350,
        'instructions': ('Ти українськомовний наставник з алгебри. Дай одну коротку підказку '
            'до 600 символів і запитання для наступного кроку. Не повідомляй кінцеві корені. '
            'Спирайся на перевірену базову підказку. За категорією root_count запропонуй перевірити '
            'кількість коренів; за root_values — знаки та обчислення. Не стверджуй, що знаєш '
            'конкретний хід розв’язання учня. Без LaTeX. Не додавай нових задач.'),
        'input': json.dumps({'equation': task.equation, 'hint_number': level+1,
                            'attempts': attempts, 'error_category': error,
                            'verified_hint': task.hint(level)}, ensure_ascii=False)
    }, {'Authorization': 'Bearer ' + key}, timeout=25)
    parts = [c.get('text', '') for item in response.get('output', [])
             if item.get('type') == 'message' for c in item.get('content', [])
             if c.get('type') == 'output_text']
    result = '\n'.join(parts).strip()
    if not result or response.get('status') != 'completed':
        raise ApiError()
    return result[:1200]


class Bot:
    def __init__(self, db, tg, admins=(), study=False, key='', model='gpt-4.1-mini', ai_limit=100):
        self.db, self.tg, self.admins = db, tg, set(admins)
        self.study, self.key, self.model, self.ai_limit = study, key, model, ai_limit
        db.row_factory = sqlite3.Row
        db.executescript('''
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS users(
          uid INTEGER PRIMARY KEY, code TEXT UNIQUE NOT NULL, mode TEXT NOT NULL DEFAULT 'standard',
          phase TEXT NOT NULL, created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          id INTEGER PRIMARY KEY, uid INTEGER NOT NULL REFERENCES users(uid) ON DELETE CASCADE,
          phase TEXT NOT NULL, task_no INTEGER NOT NULL, mode TEXT NOT NULL,
          started TEXT NOT NULL, ended TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          hints INTEGER NOT NULL DEFAULT 0, correct INTEGER, outcome TEXT,
          last_error TEXT NOT NULL DEFAULT 'none', UNIQUE(uid,phase,task_no));
        CREATE TABLE IF NOT EXISTS hints(
          id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
          created TEXT NOT NULL, source TEXT NOT NULL, model TEXT NOT NULL, content TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS ai_budget(day TEXT PRIMARY KEY, used INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        db.commit()

    def user(self, uid):
        return self.db.execute('SELECT * FROM users WHERE uid=?', (uid,)).fetchone()

    def active(self, uid, phase):
        return self.db.execute('SELECT * FROM sessions WHERE uid=? AND phase=? AND ended IS NULL',
                               (uid, phase)).fetchone()

    def say(self, uid, text):
        self.tg.send(uid, text)

    def export(self):
        rows = self.db.execute('''SELECT u.code,s.phase,s.task_no,s.mode,s.started,s.ended,
          s.attempts,s.hints,s.correct,s.outcome,s.last_error,
          (SELECT count(*) FROM hints h WHERE h.session_id=s.id AND source='ai') ai_hints,
          (SELECT count(*) FROM hints h WHERE h.session_id=s.id AND source='fallback') fallback_hints
          FROM sessions s JOIN users u ON s.uid=u.uid ORDER BY u.code,s.id''')
        stream = io.StringIO(newline='')
        writer = csv.writer(stream, delimiter=';')
        writer.writerow([col[0] for col in rows.description])
        writer.writerows(rows)
        return stream.getvalue().encode('utf-8-sig')

    def admin(self, uid, parts):
        if uid not in self.admins:
            self.say(uid, 'Команда доступна лише керівнику.')
            return
        cmd = parts[0]
        if cmd == '/admin':
            self.say(uid, '/participants — коди учасників\n/assign КОД standard|ai — режим до початку навчання'
                     '\n/phase КОД pre|training|post|delayed|closed — етап\n/export — CSV'
                     '\nЕтапи й групи для дослідження задає керівник. /phase доступна тільки при STUDY_MODE=true.')
        elif cmd == '/participants':
            rows = self.db.execute('SELECT code,mode,phase FROM users ORDER BY created').fetchall()
            lines = [f'{r[0]} | {r[1]} | {r[2]}' for r in rows]
            if not lines:
                self.say(uid, 'Учасників ще немає.')
            for i in range(0, len(lines), 70):
                self.say(uid, '\n'.join(lines[i:i+70]))
        elif cmd == '/export':
            self.tg.document(uid, self.export())
        elif cmd in {'/assign', '/phase'}:
            if len(parts) != 3:
                self.say(uid, 'Формат: /assign КОД standard|ai або /phase КОД ЕТАП')
                return
            u = self.db.execute('SELECT * FROM users WHERE code=?', (parts[1].upper(),)).fetchone()
            if not u:
                self.say(uid, 'Код не знайдено.')
                return
            value = parts[2]
            if cmd == '/assign':
                if value not in {'standard', 'ai'} or (value == 'ai' and not self.key):
                    self.say(uid, 'Потрібен standard або ai. Для ai налаштуй OPENAI_API_KEY.')
                    return
                trained = self.db.execute("SELECT 1 FROM sessions WHERE uid=? AND phase='training'", (u['uid'],)).fetchone()
                if trained:
                    self.say(uid, 'Групу після початку навчання змінювати не можна.')
                    return
                with self.db:
                    self.db.execute('UPDATE users SET mode=? WHERE uid=?', (value, u['uid']))
            else:
                if not self.study or value not in {'pre', 'training', 'post', 'delayed', 'closed'}:
                    self.say(uid, 'Увімкни STUDY_MODE; етап: pre, training, post, delayed або closed.')
                    return
                # Sequential transitions prevent exposure to training before the pre-test.
                allowed = {'waiting': 'pre', 'pre': 'training', 'training': 'post',
                           'post': 'delayed', 'delayed': 'closed'}
                if allowed.get(u['phase']) != value:
                    self.say(uid, 'Дозволений порядок: waiting → pre → training → post → delayed → closed.')
                    return
                if self.active(u['uid'], u['phase']):
                    self.say(uid, 'Спочатку учасник має завершити поточне завдання.')
                    return
                if u['phase'] in {'pre', 'post', 'delayed'}:
                    n = self.db.execute('SELECT count(*) FROM sessions WHERE uid=? AND phase=? AND ended IS NOT NULL',
                                        (u['uid'], u['phase'])).fetchone()[0]
                    if n != len(BANKS[u['phase']]):
                        self.say(uid, 'Спочатку потрібно завершити всі завдання цього тесту.')
                        return
                with self.db:
                    self.db.execute('UPDATE users SET phase=? WHERE uid=?', (value, u['uid']))
            self.say(uid, 'Збережено. Учасник побачить стан через /stats або /task.')

    def handle(self, message):
        if message.get('chat', {}).get('type') != 'private':
            return
        uid = message['from']['id']
        raw = message.get('text', '').strip()
        if not raw:
            self.say(uid, 'Надішли текстову відповідь або /help.')
            return
        parts = raw.split()
        cmd = parts[0].split('@')[0].lower()
        parts[0] = cmd
        if cmd == '/id':
            self.say(uid, f'Твій Telegram ID: {uid}')
            return
        if cmd in {'/admin','/participants','/assign','/phase','/export'}:
            self.admin(uid, parts)
            return
        if cmd == '/privacy':
            self.say(uid, PRIVACY)
            return
        u = self.user(uid)
        if cmd == '/delete':
            if len(parts) == 2 and parts[1] == 'ТАК':
                with self.db:
                    self.db.execute('DELETE FROM users WHERE uid=?', (uid,))
                self.say(uid, 'Твої записи видалено з поточної бази. Для нового початку: /start.')
            else:
                self.say(uid, 'Для видалення твоїх результатів надішли /delete ТАК. Відновлення через бота немає.')
            return
        if not u:
            if cmd == '/agree':
                with self.db:
                    self.db.execute('INSERT INTO users(uid,code,phase,created) VALUES(?,?,?,?)',
                                    (uid, secrets.token_hex(5).upper(), 'waiting' if self.study else 'training', now()))
                u = self.user(uid)
                self.say(uid, f'Твій код: {u["code"]}. Передай його керівнику.\n' + HELP)
            else:
                self.say(uid, 'Вітаю! Я «Квадратний наставник».\n' + PRIVACY + '\n\nПісля ознайомлення: /agree.')
            return
        phase = u['phase']
        if cmd in {'/start','/help','/agree'}:
            self.say(uid, f'Код: {u["code"]}. Етап: {phase}. Режим: {u["mode"]}.\n' + HELP)
            return
        if cmd == '/stats':
            rows = self.db.execute('''SELECT phase,count(*) total,sum(ended IS NOT NULL) done,
                 sum(CASE WHEN correct=1 THEN 1 ELSE 0 END) score FROM sessions WHERE uid=? GROUP BY phase''', (uid,))
            lines = [f'{r[0]}: завершено {r[2]}/{len(BANKS[r[0]])}; правильних {r[3]}' for r in rows]
            self.say(uid, f'Код: {u["code"]}\nЕтап: {phase}; режим: {u["mode"]}\n' + '\n'.join(lines))
            return
        if cmd == '/mode':
            mode = parts[1] if len(parts) == 2 else ''
            if self.study:
                self.say(uid, 'У дослідженні режим призначає керівник.')
            elif mode not in {'standard','ai'} or (mode == 'ai' and not self.key):
                self.say(uid, '/mode standard або /mode ai. Для ШІ потрібен ключ OpenAI у налаштуваннях.')
            elif self.active(uid, phase):
                self.say(uid, 'Заверши поточне завдання перед зміною режиму.')
            else:
                with self.db:
                    self.db.execute('UPDATE users SET mode=? WHERE uid=?', (mode, uid))
                self.say(uid, 'Режим змінено: ' + mode)
            return
        if phase in {'waiting', 'closed'}:
            self.say(uid, 'Навчання ще не відкрито або вже завершено. Звернися до керівника.')
            return
        exam = phase != 'training'
        if cmd in {'/hint','/solution','/theory'} and exam:
            self.say(uid, 'Під час тесту підказки, теорія та розв’язання вимкнені. Працюй самостійно.')
            return
        if cmd == '/theory':
            self.say(uid, THEORY)
            return
        s = self.active(uid, phase)
        if cmd == '/task':
            if not s:
                n = self.db.execute('SELECT count(*) FROM sessions WHERE uid=? AND phase=?', (uid,phase)).fetchone()[0]
                if n >= len(BANKS[phase]):
                    self.say(uid, 'Усі завдання етапу завершено. /stats — результат.')
                    return
                with self.db:
                    self.db.execute('INSERT INTO sessions(uid,phase,task_no,mode,started) VALUES(?,?,?,?,?)',
                                    (uid,phase,n,u['mode'],now()))
                s = self.active(uid, phase)
            task = BANKS[phase][s['task_no']]
            self.say(uid, f'Завдання {s["task_no"]+1}/{len(BANKS[phase])}: {task.equation}\n'
                     'Введи всі дійсні корені: 2; -1/2 або немає.\n' +
                     ('Тест: одна прийнята відповідь на завдання; підказок немає.' if exam else 'Для допомоги: /hint.'))
            return
        if not s:
            self.say(uid, 'Натисни /task, щоб отримати завдання.')
            return
        task = BANKS[phase][s['task_no']]
        if cmd == '/hint':
            self.give_hint(uid, s, task)
            return
        if cmd == '/solution':
            with self.db:
                self.db.execute("UPDATE sessions SET ended=?,correct=0,outcome='revealed' WHERE id=?", (now(), s['id']))
            self.say(uid, task.solution() + '\nЗавдання завершено з розв’язанням. Далі: /task.')
            return
        if raw.startswith('/'):
            self.say(uid, 'Невідома команда. /help — список.')
            return
        try:
            answer = parse_answer(raw)
        except ValueError as e:
            self.say(uid, str(e) + '\nЦе повідомлення не зараховано як спробу.')
            return
        kind = error_kind(answer, task)
        correct = kind == 'correct'
        done = exam or correct
        with self.db:
            self.db.execute('''UPDATE sessions SET attempts=attempts+1,last_error=?,ended=?,correct=?,outcome=? WHERE id=?''',
                (kind, now() if done else None, int(correct) if done else None,
                 'answered' if done else None, s['id']))
        if exam:
            self.say(uid, 'Відповідь зафіксовано. Наступне завдання: /task. Підсумок: /stats.')
        elif correct:
            self.say(uid, 'Правильно! Наступне завдання: /task.')
        else:
            self.say(uid, 'Поки що неправильно. Перевір кількість коренів, знаки та обчислення. Спробуй ще або /hint.')

    def give_hint(self, uid, s, task):
        if s['hints'] >= 3:
            self.say(uid, 'Три підказки вже використано. Спробуй відповісти або /solution.')
            return
        level = s['hints']
        text, source, model = task.hint(level), 'standard', ''
        if s['mode'] == 'ai':
            day = now()[:10]
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO ai_budget VALUES(?,0)', (day,))
                used = self.db.execute('SELECT used FROM ai_budget WHERE day=?', (day,)).fetchone()[0]
                if self.key and used < self.ai_limit:
                    self.db.execute('UPDATE ai_budget SET used=used+1 WHERE day=?', (day,))
            source = 'fallback'
            if self.key and used < self.ai_limit:
                try:
                    text = ai_hint(task, level, s['attempts'], s['last_error'], self.key, self.model)
                    source, model = 'ai', self.model
                except ApiError:
                    logging.warning('AI unavailable; standard fallback used')
        with self.db:
            self.db.execute('UPDATE sessions SET hints=hints+1 WHERE id=?', (s['id'],))
            self.db.execute('INSERT INTO hints(session_id,created,source,model,content) VALUES(?,?,?,?,?)',
                            (s['id'], now(), source, model, text))
        prefix = {'standard':'Підказка', 'ai':'ШІ-підказка (перевір міркування)',
                  'fallback':'ШІ зараз недоступний або досягнуто ліміту; стандартна підказка'}[source]
        self.say(uid, prefix + ':\n' + text)


def main():
    load_env(ROOT / '.env')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if not token or ':' not in token or token.startswith('PASTE'):
        raise SystemExit('Заповни TELEGRAM_BOT_TOKEN у файлі .env поруч із bot.py.')
    admins = {int(x.strip()) for x in os.getenv('ADMIN_IDS','').split(',') if x.strip()}
    study = os.getenv('STUDY_MODE','false').lower() == 'true'
    if study and not admins:
        raise SystemExit('Для STUDY_MODE=true потрібен ADMIN_IDS.')
    path = Path(os.getenv('DB_PATH', str(ROOT/'data'/'bot.sqlite3'))).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Separate SQLite lock file works on Windows and Linux; released on process exit.
    lock = sqlite3.connect(str(path) + '.lock', timeout=0)
    lock.execute('CREATE TABLE IF NOT EXISTS lock(id INTEGER)')
    try:
        lock.execute('BEGIN EXCLUSIVE')
    except sqlite3.OperationalError:
        raise SystemExit('Інший процес уже використовує цю базу.') from None
    db = sqlite3.connect(path)
    tg = Telegram(token)
    app = Bot(db,tg,admins,study,os.getenv('OPENAI_API_KEY',''),
              os.getenv('OPENAI_MODEL','gpt-4.1-mini'),int(os.getenv('AI_DAILY_LIMIT','100')))
    # Pin operating mode; do not silently turn a practice database into a study.
    old_mode = db.execute("SELECT value FROM meta WHERE key='study_mode'").fetchone()
    if old_mode and old_mode[0] != str(study):
        raise SystemExit('STUDY_MODE відрізняється від режиму бази. Вкажи новий DB_PATH; стару базу збережи.')
    with db:
        db.execute("INSERT OR IGNORE INTO meta VALUES('study_mode',?)", (str(study),))
    try:
        tg.call('getMe')
        info = tg.call('getWebhookInfo')
        if info.get('url'):
            raise SystemExit('Для цього токена активний webhook. Вимкни попередню інтеграцію перед polling.')
    except ApiError as e:
        raise SystemExit(f'Не вдалося підключитися до Telegram ({e}). Перевір токен і мережу.') from None
    logging.info('Bot started. Stop with Ctrl+C.')
    try:
        while True:
            item = db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
            offset = int(item[0]) if item else 0
            try:
                updates = tg.call('getUpdates', offset=offset, timeout=25, allowed_updates=['message'])
                for update in updates:
                    # Claim before processing: avoids double scoring on restart; see README.
                    with db:
                        db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(update['update_id']+1),))
                    try:
                        if 'message' in update:
                            app.handle(update['message'])
                    except ApiError as e:
                        logging.warning('Message/API delivery failed (%s); use /task to resume', e)
                        if e.retry_after:
                            time.sleep(min(e.retry_after,60))
                    except Exception as e:
                        # No exception text/tracebacks with tokens or student content in logs.
                        logging.error('Handler failure: %s', type(e).__name__)
                
            except ApiError as e:
                if e.status in (401,409):
                    raise SystemExit('Telegram відхилив доступ або бот запущений ще десь. Зупини другий процес / перевір токен.') from None
                logging.warning('Polling unavailable (%s); retrying', e)
                time.sleep(min(max(e.retry_after,5),60))
    except KeyboardInterrupt:
        logging.info('Stopped')
    finally:
        db.close()
        lock.close()


if __name__ == '__main__':
    main()
