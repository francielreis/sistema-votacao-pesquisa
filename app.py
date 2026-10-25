import os
import hashlib
from datetime import datetime
from functools import wraps

from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-change-me')

database_url = os.getenv('DATABASE_URL', '').strip().strip('"').strip("'")
if database_url.startswith('postgres://'):
    database_url = database_url.replace('postgres://', 'postgresql://', 1)
if database_url.startswith('postgresql+psycopg://'):
    database_url = database_url.replace('postgresql+psycopg://', 'postgresql+psycopg2://', 1)

valid_db_prefixes = ('postgresql://', 'postgresql+psycopg2://', 'sqlite:///', 'sqlite://')
if not database_url or not database_url.startswith(valid_db_prefixes):
    database_url = 'sqlite:///votacao.db'

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

VALID_NUMBERS = {'13', '22'}
SPECIAL_CANDIDATES = {
    'branco': {'number': '__BRANCO__', 'name': 'Voto em Branco'},
    'nulo': {'number': '__NULO__', 'name': 'Voto Nulo'},
}


class Poll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(180), nullable=False, default='Pesquisa de intenção de voto')
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Candidate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    number = db.Column(db.String(10), nullable=True)
    photo_url = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    votes = db.relationship('Vote', backref='candidate', cascade='all, delete-orphan', lazy=True)


class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey('poll.id'), nullable=False)
    candidate_id = db.Column(db.Integer, db.ForeignKey('candidate.id'), nullable=False)
    voter_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('poll_id', 'voter_hash', name='uq_poll_voter'),
    )


def current_poll():
    poll = Poll.query.filter_by(active=True).order_by(Poll.id.desc()).first()
    if not poll:
        poll = Poll(title='Pesquisa de intenção de voto', active=True)
        db.session.add(poll)
        db.session.commit()
    return poll


def voter_fingerprint():
    # Identificador anônimo por navegador/sessão. Evita bloquear pessoas diferentes
    # que estejam na mesma rede/IP durante uma pesquisa simulada.
    token = session.get('voter_token')
    if not token:
        token = os.urandom(32).hex()
        session['voter_token'] = token
    raw = f"{token}|{app.config['SECRET_KEY']}"
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get('admin_ok'):
            return redirect(url_for('admin_login'))
        return fn(*args, **kwargs)
    return wrapper


def get_special_candidate(kind):
    data = SPECIAL_CANDIDATES[kind]
    candidate = Candidate.query.filter_by(number=data['number']).first()
    if not candidate:
        candidate = Candidate(name=data['name'], number=data['number'])
        db.session.add(candidate)
        db.session.commit()
    return candidate


def register_vote(candidate, success_message='Voto simulado registrado com sucesso.'):
    poll = current_poll()
    admin_test_mode = bool(session.get('admin_ok'))

    if admin_test_mode:
        # No modo de teste do administrador, cada confirmação recebe um identificador
        # único para permitir vários votos sem liberar essa possibilidade ao público.
        raw = f"admin-test|{os.urandom(32).hex()}|{app.config['SECRET_KEY']}"
        fp = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    else:
        fp = voter_fingerprint()
        if Vote.query.filter_by(poll_id=poll.id, voter_hash=fp).first():
            flash('Este navegador já registrou um voto nesta pesquisa.', 'warning')
            return redirect(url_for('results'))

    db.session.add(Vote(poll_id=poll.id, candidate_id=candidate.id, voter_hash=fp))
    db.session.commit()

    if admin_test_mode:
        flash(f'{success_message} Modo teste do administrador: novo voto permitido.', 'success')
    else:
        flash(success_message, 'success')
    return redirect(url_for('results'))


@app.route('/')
def index():
    poll = current_poll()
    candidates = Candidate.query.filter(Candidate.number.in_(VALID_NUMBERS)).order_by(
        Candidate.number.asc(), Candidate.name.asc()
    ).all()
    admin_test_mode = bool(session.get('admin_ok'))
    already_voted = False if admin_test_mode else (
        Vote.query.filter_by(poll_id=poll.id, voter_hash=voter_fingerprint()).first() is not None
    )
    return render_template(
        'index.html',
        poll=poll,
        candidates=candidates,
        already_voted=already_voted,
        admin_test_mode=admin_test_mode,
    )


@app.post('/votar/<int:candidate_id>')
def vote(candidate_id):
    candidate = Candidate.query.get_or_404(candidate_id)
    if candidate.number not in VALID_NUMBERS:
        flash('Número inválido. Use 13, 22, BRANCO ou confirme um número diferente como NULO.', 'warning')
        return redirect(url_for('index'))
    return register_vote(candidate)


@app.post('/votar-especial/<kind>')
def vote_special(kind):
    if kind not in SPECIAL_CANDIDATES:
        return redirect(url_for('index'))

    candidate = get_special_candidate(kind)
    if kind == 'branco':
        return register_vote(candidate, 'Voto em branco registrado com sucesso.')
    return register_vote(candidate, 'Voto nulo registrado com sucesso.')


@app.route('/resultados')
def results():
    poll = current_poll()
    total = Vote.query.filter_by(poll_id=poll.id).count()
    candidates = Candidate.query.filter(Candidate.number.in_(VALID_NUMBERS)).order_by(
        Candidate.number.asc(), Candidate.name.asc()
    ).all()

    rows = []
    for c in candidates:
        count = Vote.query.filter_by(poll_id=poll.id, candidate_id=c.id).count()
        pct = (count / total * 100) if total else 0
        rows.append({'candidate': c, 'votes': count, 'pct': pct})
    rows.sort(key=lambda x: x['votes'], reverse=True)

    blank_candidate = Candidate.query.filter_by(number=SPECIAL_CANDIDATES['branco']['number']).first()
    null_candidate = Candidate.query.filter_by(number=SPECIAL_CANDIDATES['nulo']['number']).first()
    blank_votes = Vote.query.filter_by(poll_id=poll.id, candidate_id=blank_candidate.id).count() if blank_candidate else 0
    null_votes = Vote.query.filter_by(poll_id=poll.id, candidate_id=null_candidate.id).count() if null_candidate else 0
    blank_pct = (blank_votes / total * 100) if total else 0
    null_pct = (null_votes / total * 100) if total else 0

    return render_template(
        'results.html',
        poll=poll,
        rows=rows,
        total=total,
        blank_votes=blank_votes,
        null_votes=null_votes,
        blank_pct=blank_pct,
        null_pct=null_pct,
    )


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        expected = os.getenv('ADMIN_PASSWORD', 'admin123')
        if password == expected:
            session['admin_ok'] = True
            return redirect(url_for('admin'))
        flash('Senha inválida.', 'danger')
    return render_template('admin_login.html')


@app.route('/admin/logout')
def admin_logout():
    session.clear()
    return redirect(url_for('index'))


@app.route('/admin', methods=['GET', 'POST'])
@admin_required
def admin():
    poll = current_poll()
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'poll_title':
            title = request.form.get('title', '').strip()
            if title:
                poll.title = title[:180]
                db.session.commit()
                flash('Título atualizado.', 'success')
        elif action == 'add_candidate':
            name = request.form.get('name', '').strip()
            number = request.form.get('number', '').strip()
            photo_url = request.form.get('photo_url', '').strip()
            if not name:
                flash('Informe o nome do candidato/opção.', 'danger')
            else:
                db.session.add(Candidate(name=name[:120], number=number[:10] or None, photo_url=photo_url[:500] or None))
                db.session.commit()
                flash('Candidato/opção adicionado.', 'success')
        elif action == 'reset_votes':
            Vote.query.filter_by(poll_id=poll.id).delete()
            db.session.commit()
            flash('Votos da pesquisa atual foram zerados.', 'success')

    candidates = Candidate.query.filter(~Candidate.number.in_([SPECIAL_CANDIDATES['branco']['number'], SPECIAL_CANDIDATES['nulo']['number']])).order_by(Candidate.number.asc(), Candidate.name.asc()).all()
    total = Vote.query.filter_by(poll_id=poll.id).count()
    return render_template('admin.html', poll=poll, candidates=candidates, total=total)


@app.post('/admin/candidato/<int:candidate_id>/excluir')
@admin_required
def delete_candidate(candidate_id):
    candidate = Candidate.query.get_or_404(candidate_id)
    db.session.delete(candidate)
    db.session.commit()
    flash('Candidato/opção removido.', 'success')
    return redirect(url_for('admin'))


@app.get('/health')
def health():
    return {
        'status': 'ok',
        'database': db.engine.url.get_backend_name()
    }


with app.app_context():
    db.create_all()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=os.getenv('FLASK_DEBUG') == '1')
